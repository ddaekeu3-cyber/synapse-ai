---
layout: solution
title: "Agent Doesn't Implement Streaming Cost Estimation"
category: token-cost
description: "Agents that stream responses without tracking token consumption accumulate surprise costs at scale. Real-time cost estimation during streaming enables budget enforcement and cost-aware routing."
tags: [streaming, cost, token-counting, budget, real-time, sqlite, async]
---

# Agent Doesn't Implement Streaming Cost Estimation

## Problem

Streaming responses improve user experience but obscure token consumption until the stream ends. Without per-stream cost tracking, agents burn through token budgets invisibly, leading to runaway costs on long conversations or high-traffic deployments.

Real-time streaming cost estimation gives you per-request cost visibility, budget enforcement, and data to drive model routing decisions.

---

## Option 1: Simple Post-Stream Cost Calculator

```python
import anthropic
from dataclasses import dataclass

# Pricing per million tokens (as of model release — verify current pricing)
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

@dataclass
class StreamCostResult:
    model: str
    input_tokens: int
    output_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    response_text: str

def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> tuple[float, float, float]:
    pricing = MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return input_cost, output_cost, input_cost + output_cost


def stream_with_cost(prompt: str, model: str = "claude-haiku-4-5-20251001") -> StreamCostResult:
    client = anthropic.Anthropic()

    response_parts = []
    input_tokens = 0
    output_tokens = 0

    with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            response_parts.append(text)
            print(text, end="", flush=True)

        # Usage is available after stream completes
        usage = stream.get_final_message().usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens

    print()  # newline after stream

    input_cost, output_cost, total = estimate_cost(model, input_tokens, output_tokens)

    return StreamCostResult(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        total_cost_usd=total,
        response_text="".join(response_parts),
    )


if __name__ == "__main__":
    result = stream_with_cost(
        "Explain the difference between supervised and unsupervised learning in 3 sentences.",
        model="claude-haiku-4-5-20251001",
    )
    print(f"\n--- Cost Summary ---")
    print(f"Model:         {result.model}")
    print(f"Input tokens:  {result.input_tokens} (${result.input_cost_usd:.6f})")
    print(f"Output tokens: {result.output_tokens} (${result.output_cost_usd:.6f})")
    print(f"Total cost:    ${result.total_cost_usd:.6f}")
# Expected Token Savings: 0% direct — provides cost visibility after each stream
# Environment: pip install anthropic
```

---

## Option 2: Real-Time Token Counter with Live Cost Display

```python
import anthropic
import time
from dataclasses import dataclass, field

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

@dataclass
class LiveCostTracker:
    model: str
    input_tokens: int = 0
    output_tokens_estimated: int = 0
    chars_streamed: int = 0
    start_time: float = field(default_factory=time.time)

    # Rough heuristic: 4 chars ≈ 1 token for English text
    CHARS_PER_TOKEN: int = 4

    def record_input_tokens(self, n: int):
        self.input_tokens = n

    def record_char(self, char: str):
        self.chars_streamed += len(char)
        self.output_tokens_estimated = max(1, self.chars_streamed // self.CHARS_PER_TOKEN)

    def estimated_cost(self) -> dict:
        pricing = MODEL_PRICING.get(self.model, {"input": 3.00, "output": 15.00})
        input_cost = (self.input_tokens / 1_000_000) * pricing["input"]
        output_cost = (self.output_tokens_estimated / 1_000_000) * pricing["output"]
        elapsed = time.time() - self.start_time
        tokens_per_sec = self.output_tokens_estimated / max(elapsed, 0.001)
        return {
            "input_tokens": self.input_tokens,
            "output_tokens_est": self.output_tokens_estimated,
            "input_cost_usd": input_cost,
            "output_cost_usd": output_cost,
            "total_cost_usd": input_cost + output_cost,
            "tokens_per_sec": round(tokens_per_sec, 1),
            "elapsed_sec": round(elapsed, 2),
        }

    def display_live(self):
        est = self.estimated_cost()
        print(
            f"\r  [tokens≈{est['output_tokens_est']} | ${est['total_cost_usd']:.6f} | {est['tokens_per_sec']} tok/s]",
            end="",
            flush=True,
        )


def stream_with_live_cost(prompt: str, model: str = "claude-haiku-4-5-20251001"):
    client = anthropic.Anthropic()
    tracker = LiveCostTracker(model=model)

    print(f"Streaming ({model}):")
    print("-" * 60)

    response_parts = []
    update_interval = 0.25  # seconds between display updates
    last_update = time.time()

    with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        # Capture input token count from first event
        for event in stream:
            if hasattr(event, "type"):
                if event.type == "message_start":
                    tracker.record_input_tokens(event.message.usage.input_tokens)
                elif event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        text = event.delta.text
                        tracker.record_char(text)
                        response_parts.append(text)
                        print(text, end="", flush=True)

                        now = time.time()
                        if now - last_update >= update_interval:
                            tracker.display_live()
                            last_update = now

    print()  # clear line
    final = tracker.estimated_cost()

    print(f"\n--- Final Cost Estimate ---")
    print(f"Input tokens:      {final['input_tokens']}")
    print(f"Output tokens est: {final['output_tokens_est']}")
    print(f"Input cost:        ${final['input_cost_usd']:.6f}")
    print(f"Output cost:       ${final['output_cost_usd']:.6f}")
    print(f"Total cost:        ${final['total_cost_usd']:.6f}")
    print(f"Speed:             {final['tokens_per_sec']} tok/s")
    print(f"Elapsed:           {final['elapsed_sec']}s")

    return "".join(response_parts), final


if __name__ == "__main__":
    stream_with_live_cost(
        "Write a haiku about token costs in AI systems.",
        model="claude-haiku-4-5-20251001",
    )
# Expected Token Savings: 0% direct — real-time display adds no tokens; enables downstream savings via budget gating
# Environment: pip install anthropic
```

---

## Option 3: Budget-Gated Streaming with Hard Stop

```python
import anthropic
from dataclasses import dataclass

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

@dataclass
class BudgetConfig:
    max_cost_usd: float
    max_output_tokens: int | None = None
    warn_at_pct: float = 0.80  # Warn when 80% of budget consumed

class BudgetExceededError(Exception):
    def __init__(self, cost_usd: float, budget_usd: float):
        super().__init__(f"Cost ${cost_usd:.6f} exceeded budget ${budget_usd:.6f}")
        self.cost_usd = cost_usd
        self.budget_usd = budget_usd


def estimate_output_cost(model: str, tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
    return (tokens / 1_000_000) * pricing["output"]

def estimate_input_cost(model: str, tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
    return (tokens / 1_000_000) * pricing["input"]


def stream_with_budget(
    prompt: str,
    budget: BudgetConfig,
    model: str = "claude-haiku-4-5-20251001",
) -> dict:
    client = anthropic.Anthropic()

    response_parts = []
    input_tokens = 0
    output_chars = 0
    warned = False

    CHARS_PER_TOKEN = 4

    print(f"Budget: ${budget.max_cost_usd:.4f} | Model: {model}")

    try:
        with client.messages.stream(
            model=model,
            max_tokens=budget.max_output_tokens or 1024,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for event in stream:
                if not hasattr(event, "type"):
                    continue

                if event.type == "message_start":
                    input_tokens = event.message.usage.input_tokens
                    input_cost = estimate_input_cost(model, input_tokens)
                    if input_cost > budget.max_cost_usd:
                        raise BudgetExceededError(input_cost, budget.max_cost_usd)

                elif event.type == "content_block_delta" and hasattr(event.delta, "text"):
                    text = event.delta.text
                    output_chars += len(text)
                    response_parts.append(text)
                    print(text, end="", flush=True)

                    # Estimate running cost
                    output_tokens_est = max(1, output_chars // CHARS_PER_TOKEN)
                    current_cost = (
                        estimate_input_cost(model, input_tokens)
                        + estimate_output_cost(model, output_tokens_est)
                    )

                    # Warn threshold
                    if not warned and current_cost >= budget.max_cost_usd * budget.warn_at_pct:
                        print(f"\n[WARNING] {budget.warn_at_pct:.0%} of budget consumed (${current_cost:.6f})")
                        warned = True

                    # Hard stop — note: this aborts the stream mid-response
                    if current_cost > budget.max_cost_usd:
                        print(f"\n[BUDGET EXCEEDED] Stopping stream at ${current_cost:.6f}")
                        raise BudgetExceededError(current_cost, budget.max_cost_usd)

        print()
        final_output_tokens = max(1, output_chars // CHARS_PER_TOKEN)
        total_cost = (
            estimate_input_cost(model, input_tokens)
            + estimate_output_cost(model, final_output_tokens)
        )

        return {
            "status": "completed",
            "text": "".join(response_parts),
            "input_tokens": input_tokens,
            "output_tokens_est": final_output_tokens,
            "total_cost_usd": total_cost,
            "budget_remaining_usd": budget.max_cost_usd - total_cost,
        }

    except BudgetExceededError as e:
        return {
            "status": "budget_exceeded",
            "text": "".join(response_parts),  # Partial response
            "cost_usd": e.cost_usd,
            "budget_usd": e.budget_usd,
        }


if __name__ == "__main__":
    result = stream_with_budget(
        "Write a detailed 500-word essay on the history of computing.",
        budget=BudgetConfig(max_cost_usd=0.0001, warn_at_pct=0.70),
        model="claude-haiku-4-5-20251001",
    )
    print(f"\nResult: {result['status']}")
    if result["status"] == "completed":
        print(f"Total cost: ${result['total_cost_usd']:.6f}")
        print(f"Remaining budget: ${result['budget_remaining_usd']:.6f}")
# Expected Token Savings: Up to 80% when hard stop triggers early on over-budget responses
# Environment: pip install anthropic
```

---

## Option 4: SQLite Session Cost Aggregator

```python
import sqlite3
import json
import time
import anthropic
from dataclasses import dataclass
from datetime import datetime

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

class StreamCostDB:
    """Tracks every stream call's cost in SQLite for session and daily rollup."""

    def __init__(self, db_path: str = "stream_costs.db"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS stream_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                model TEXT,
                prompt_preview TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                input_cost_usd REAL,
                output_cost_usd REAL,
                total_cost_usd REAL,
                duration_ms INTEGER,
                called_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS session_budgets (
                session_id TEXT PRIMARY KEY,
                budget_usd REAL,
                spent_usd REAL DEFAULT 0.0,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def set_budget(self, session_id: str, budget_usd: float):
        self.conn.execute(
            "INSERT OR REPLACE INTO session_budgets (session_id, budget_usd) VALUES (?, ?)",
            (session_id, budget_usd),
        )
        self.conn.commit()

    def record_call(
        self, session_id: str, model: str, prompt: str,
        input_tokens: int, output_tokens: int, duration_ms: int,
    ) -> dict:
        pricing = MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        total = input_cost + output_cost

        self.conn.execute(
            """INSERT INTO stream_calls
               (session_id, model, prompt_preview, input_tokens, output_tokens,
                input_cost_usd, output_cost_usd, total_cost_usd, duration_ms)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (session_id, model, prompt[:80], input_tokens, output_tokens,
             input_cost, output_cost, total, duration_ms),
        )
        self.conn.execute(
            "UPDATE session_budgets SET spent_usd = spent_usd + ? WHERE session_id = ?",
            (total, session_id),
        )
        self.conn.commit()
        return {"input_cost": input_cost, "output_cost": output_cost, "total": total}

    def session_summary(self, session_id: str) -> dict:
        budget_row = self.conn.execute(
            "SELECT budget_usd, spent_usd FROM session_budgets WHERE session_id=?",
            (session_id,),
        ).fetchone()

        calls = self.conn.execute(
            """SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(total_cost_usd), AVG(duration_ms)
               FROM stream_calls WHERE session_id=?""",
            (session_id,),
        ).fetchone()

        return {
            "session_id": session_id,
            "budget_usd": budget_row[0] if budget_row else None,
            "spent_usd": budget_row[1] if budget_row else calls[3],
            "remaining_usd": (budget_row[0] - budget_row[1]) if budget_row else None,
            "call_count": calls[0],
            "total_input_tokens": calls[1] or 0,
            "total_output_tokens": calls[2] or 0,
            "total_cost_usd": calls[3] or 0,
            "avg_duration_ms": round(calls[4] or 0, 1),
        }

    def check_budget(self, session_id: str) -> tuple[bool, float]:
        """Returns (within_budget, remaining_usd). True if no budget set."""
        row = self.conn.execute(
            "SELECT budget_usd, spent_usd FROM session_budgets WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if not row:
            return True, float("inf")
        remaining = row[0] - row[1]
        return remaining > 0, remaining


def stream_tracked(
    prompt: str,
    session_id: str,
    db: StreamCostDB,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    within_budget, remaining = db.check_budget(session_id)
    if not within_budget:
        raise RuntimeError(f"Session {session_id} budget exhausted")

    client = anthropic.Anthropic()
    response_parts = []
    start_ms = int(time.time() * 1000)

    with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            response_parts.append(text)
            print(text, end="", flush=True)

        usage = stream.get_final_message().usage

    duration_ms = int(time.time() * 1000) - start_ms
    cost = db.record_call(
        session_id, model, prompt,
        usage.input_tokens, usage.output_tokens, duration_ms,
    )

    print(f"\n[Cost] ${cost['total']:.6f} | remaining budget: ${remaining - cost['total']:.6f}")
    return "".join(response_parts)


if __name__ == "__main__":
    db = StreamCostDB(db_path=":memory:")
    session = "user-42-2026-04-16"
    db.set_budget(session, budget_usd=0.01)

    prompts = [
        "What is a neural network?",
        "Name 3 Python web frameworks.",
        "Explain REST in one sentence.",
    ]

    for p in prompts:
        try:
            stream_tracked(p, session_id=session, db=db)
        except RuntimeError as e:
            print(f"[BLOCKED] {e}")

    summary = db.session_summary(session)
    print(f"\nSession Summary: {json.dumps(summary, indent=2)}")
# Expected Token Savings: 0% direct — tracking overhead is negligible; enables budget enforcement that prevents overruns
# Environment: pip install anthropic; sqlite3 and time are stdlib
```

---

## Option 5: Model-Router with Cost-Based Selection

```python
import anthropic
from dataclasses import dataclass

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00,  "tier": "fast"},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00, "tier": "balanced"},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00, "tier": "powerful"},
}

@dataclass
class RoutingConfig:
    max_cost_per_call_usd: float
    estimated_output_tokens: int = 256
    prefer_quality: bool = False


def estimate_call_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
    return (
        (input_tokens / 1_000_000) * pricing["input"]
        + (output_tokens / 1_000_000) * pricing["output"]
    )


def select_model(
    prompt: str,
    config: RoutingConfig,
) -> tuple[str, float]:
    """
    Select the highest-quality model that fits within cost budget.
    Returns (model_id, estimated_cost_usd).
    """
    # Rough token estimate: 4 chars per token
    input_tokens_est = max(1, len(prompt) // 4)

    # Try models from most to least capable
    candidates = list(MODEL_PRICING.keys())
    if not config.prefer_quality:
        candidates = list(reversed(candidates))  # Cheapest first

    selected_model = candidates[0]
    selected_cost = float("inf")

    for model in candidates:
        cost = estimate_call_cost(model, input_tokens_est, config.estimated_output_tokens)
        if cost <= config.max_cost_per_call_usd:
            selected_model = model
            selected_cost = cost
            if config.prefer_quality:
                # Keep trying better models
                continue
            else:
                break  # Take first (cheapest) that fits budget

    return selected_model, selected_cost


def stream_cost_routed(prompt: str, config: RoutingConfig) -> dict:
    model, est_cost = select_model(prompt, config)
    print(f"[Router] Selected {model} (est. ${est_cost:.6f}, budget ${config.max_cost_per_call_usd:.6f})")

    client = anthropic.Anthropic()
    response_parts = []

    with client.messages.stream(
        model=model,
        max_tokens=config.estimated_output_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            response_parts.append(text)
            print(text, end="", flush=True)

        usage = stream.get_final_message().usage

    actual_cost = estimate_call_cost(model, usage.input_tokens, usage.output_tokens)

    print(f"\n[Cost] Estimated: ${est_cost:.6f} | Actual: ${actual_cost:.6f}")
    return {
        "model": model,
        "text": "".join(response_parts),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "estimated_cost_usd": est_cost,
        "actual_cost_usd": actual_cost,
    }


if __name__ == "__main__":
    prompts_and_budgets = [
        ("Hi!", 0.000005),                   # Very tight budget — routes to haiku
        ("Explain async/await in Python.", 0.001),  # Medium budget
        ("Analyze the trade-offs of microservices vs monoliths in depth.", 0.10),  # Generous
    ]

    for prompt, budget in prompts_and_budgets:
        print(f"\n{'='*60}")
        print(f"Prompt: {prompt[:60]!r} | Budget: ${budget}")
        result = stream_cost_routed(
            prompt,
            config=RoutingConfig(max_cost_per_call_usd=budget, prefer_quality=True),
        )
        print(f"Model used: {result['model']}")
# Expected Token Savings: 30-80% by routing simple queries to haiku instead of opus
# Environment: pip install anthropic
```

---

## Option 6: Async Multi-Stream Cost Aggregator with Alerts

```python
import asyncio
import json
import time
import sqlite3
import anthropic
from dataclasses import dataclass
from datetime import datetime

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

@dataclass
class StreamResult:
    stream_id: str
    model: str
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int


class AsyncCostAggregator:
    """Tracks costs across concurrent streams with alert thresholds."""

    def __init__(self, db_path: str = ":memory:", alert_threshold_usd: float = 0.10):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.alert_threshold = alert_threshold_usd
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS stream_results (
                stream_id TEXT PRIMARY KEY,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                duration_ms INTEGER,
                completed_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS cost_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_cost_usd REAL,
                threshold_usd REAL,
                fired_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    async def record(self, result: StreamResult):
        async with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO stream_results VALUES (?,?,?,?,?,?,datetime('now'))",
                (result.stream_id, result.model, result.input_tokens,
                 result.output_tokens, result.cost_usd, result.duration_ms),
            )
            self.conn.commit()

            total = self.conn.execute(
                "SELECT SUM(cost_usd) FROM stream_results"
            ).fetchone()[0] or 0.0

            if total >= self.alert_threshold:
                self.conn.execute(
                    "INSERT INTO cost_alerts (total_cost_usd, threshold_usd) VALUES (?,?)",
                    (total, self.alert_threshold),
                )
                self.conn.commit()
                print(f"\n[ALERT] Total cost ${total:.6f} crossed threshold ${self.alert_threshold:.6f}")

    def summary(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd), AVG(duration_ms) FROM stream_results"
        ).fetchone()
        alerts = self.conn.execute("SELECT COUNT(*) FROM cost_alerts").fetchone()[0]
        return {
            "streams_completed": row[0],
            "total_input_tokens": row[1] or 0,
            "total_output_tokens": row[2] or 0,
            "total_cost_usd": round(row[3] or 0, 8),
            "avg_duration_ms": round(row[4] or 0, 1),
            "alerts_fired": alerts,
        }


async def stream_one(
    stream_id: str,
    prompt: str,
    aggregator: AsyncCostAggregator,
    model: str = "claude-haiku-4-5-20251001",
) -> StreamResult:
    client = anthropic.AsyncAnthropic()
    response_parts = []
    start_ms = int(time.time() * 1000)

    async with client.messages.stream(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            response_parts.append(text)

        usage = (await stream.get_final_message()).usage

    duration_ms = int(time.time() * 1000) - start_ms
    pricing = MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
    cost = (
        (usage.input_tokens / 1_000_000) * pricing["input"]
        + (usage.output_tokens / 1_000_000) * pricing["output"]
    )

    result = StreamResult(
        stream_id=stream_id,
        model=model,
        text="".join(response_parts),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
        duration_ms=duration_ms,
    )
    await aggregator.record(result)
    print(f"[{stream_id}] ${cost:.6f} | {usage.output_tokens} out tokens | {duration_ms}ms")
    return result


async def run_concurrent_streams():
    aggregator = AsyncCostAggregator(alert_threshold_usd=0.001)

    prompts = [
        ("s1", "What is 2+2?"),
        ("s2", "Name a color."),
        ("s3", "Say hello."),
        ("s4", "What day is Monday?"),
    ]

    tasks = [
        stream_one(sid, prompt, aggregator, model="claude-haiku-4-5-20251001")
        for sid, prompt in prompts
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    print(f"\nAll streams completed. Results: {len([r for r in results if not isinstance(r, Exception)])} succeeded")
    summary = aggregator.summary()
    print(f"\nAggregated Cost Summary:")
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    asyncio.run(run_concurrent_streams())
# Expected Token Savings: 0% direct — aggregation enables budget enforcement across parallel streams
# Environment: pip install anthropic; asyncio, sqlite3, json, time are stdlib
```

---

## Comparison

| Option | Cost Timing | Real-Time Display | Budget Enforcement | SQLite | Async | Best For |
|--------|-------------|-------------------|-------------------|--------|-------|----------|
| 1 | Post-stream | No | No | No | No | Simple per-call logging |
| 2 | Per-character (estimated) | Yes (live) | No | No | No | Developer debugging, cost dashboards |
| 3 | Per-character (estimated) | Warn + stop | Hard stop | No | No | Cost-capped production agents |
| 4 | Post-stream (exact) | No | Per-session budget | Yes | No | Multi-call session tracking |
| 5 | Pre-stream (estimated) | No | Model routing | No | No | Cost-optimized model selection |
| 6 | Post-stream (exact) | Per-stream | Alert threshold | Yes | Yes | High-throughput concurrent agents |
