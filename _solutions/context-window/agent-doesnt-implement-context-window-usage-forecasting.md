---
layout: solution
title: "Agent Doesn't Implement Context Window Usage Forecasting"
category: context-window
description: "Predict when the context window will be exhausted before it happens, enabling proactive summarization, truncation, or user warnings rather than hard failures."
tags: [context-window, forecasting, token-counting, proactive, summarization, limits]
---

# Agent Doesn't Implement Context Window Usage Forecasting

An agent that runs out of context window mid-task silently truncates important history or throws an error that confuses users. Without forecasting, the agent acts as if the window is infinite and only discovers the limit at the worst possible moment — mid-reasoning, mid-tool-call, or mid-response. Context window forecasting tracks token consumption rate, predicts when the limit will be hit, and triggers proactive action before the failure occurs.

## Option 1: Per-Turn Token Counter with Warning Threshold

```python
import anthropic

client = anthropic.Anthropic()

MODEL_LIMITS = {
    "claude-haiku-4-5-20251001": 200_000,
    "claude-sonnet-4-6":         200_000,
    "claude-opus-4-6":           200_000,
}

WARNING_THRESHOLD = 0.80   # Warn at 80% usage
CRITICAL_THRESHOLD = 0.90  # Truncate at 90% usage


def count_tokens(text: str) -> int:
    """Rough estimate: ~4 chars per token."""
    return len(text) // 4


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    total += count_tokens(block["text"])
        total += 4  # Role + overhead per message
    return total


def check_context_usage(messages: list[dict], system: str = "",
                         model: str = "claude-haiku-4-5-20251001") -> dict:
    limit = MODEL_LIMITS.get(model, 200_000)
    used = estimate_messages_tokens(messages) + count_tokens(system)
    pct = used / limit

    return {
        "tokens_used": used,
        "limit": limit,
        "usage_pct": pct,
        "status": (
            "critical" if pct >= CRITICAL_THRESHOLD else
            "warning"  if pct >= WARNING_THRESHOLD else
            "ok"
        ),
    }


def run_agent_with_forecast(system: str, question: str, messages: list[dict] | None = None) -> tuple[str, list[dict]]:
    if messages is None:
        messages = []

    messages.append({"role": "user", "content": question})

    usage = check_context_usage(messages, system)
    print(f"[context] {usage['tokens_used']:,} / {usage['limit']:,} tokens ({usage['usage_pct']:.1%}) — {usage['status']}")

    if usage["status"] == "critical":
        print("[CRITICAL] Context nearly full — truncating oldest messages")
        # Keep system + last 4 messages
        messages = messages[-4:]

    elif usage["status"] == "warning":
        print("[WARNING] Context window at 80%+ — consider summarizing soon")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=messages,
    )
    reply = response.content[0].text
    messages.append({"role": "assistant", "content": reply})

    # Use actual token counts from API response
    actual_used = response.usage.input_tokens
    limit = MODEL_LIMITS.get("claude-haiku-4-5-20251001", 200_000)
    print(f"[actual]  {actual_used:,} input tokens used ({actual_used/limit:.1%} of limit)")

    return reply, messages


system = "You are a helpful assistant that answers detailed technical questions."
messages: list[dict] = []

for i in range(5):
    question = f"Question {i+1}: Explain a different Python concurrency concept in detail."
    answer, messages = run_agent_with_forecast(system, question, messages)
    print(f"Turn {i+1}: {answer[:80]}\n")

# Expected Token Savings: N/A (forecast pattern); proactive warnings prevent surprise failures on turn 15 of a long task
# Environment: Python 3.11+; use response.usage.input_tokens for exact counts; estimate only for pre-call checks
```

## Option 2: Linear Growth Rate Extrapolation

```python
import anthropic
import time

client = anthropic.Anthropic()

MODEL_LIMIT = 200_000  # tokens
TARGET_REMAINING = 2_000  # Leave this many tokens for final response


class ContextGrowthTracker:
    """Track token usage history and extrapolate when limit will be hit."""

    def __init__(self, limit: int = MODEL_LIMIT) -> None:
        self.limit = limit
        self.history: list[tuple[float, int]] = []  # (timestamp, tokens_used)

    def record(self, tokens_used: int) -> None:
        self.history.append((time.monotonic(), tokens_used))

    @property
    def current_tokens(self) -> int:
        return self.history[-1][1] if self.history else 0

    @property
    def turns_remaining_estimate(self) -> float | None:
        """Estimate turns left before hitting limit, based on recent growth rate."""
        if len(self.history) < 2:
            return None
        # Average tokens added per turn over last 3 turns
        recent = self.history[-3:]
        if len(recent) < 2:
            return None
        token_deltas = [recent[i+1][1] - recent[i][1] for i in range(len(recent) - 1)]
        avg_growth = sum(token_deltas) / len(token_deltas)
        if avg_growth <= 0:
            return float("inf")
        remaining_tokens = self.limit - self.current_tokens - TARGET_REMAINING
        return remaining_tokens / avg_growth

    @property
    def usage_pct(self) -> float:
        return self.current_tokens / self.limit if self.limit > 0 else 0.0


def run_multi_turn_agent(initial_prompt: str, follow_ups: list[str]) -> None:
    tracker = ContextGrowthTracker()
    messages: list[dict] = [{"role": "user", "content": initial_prompt}]

    all_turns = [initial_prompt] + follow_ups
    for turn_num, user_msg in enumerate(all_turns, 1):
        if turn_num > 1:
            messages.append({"role": "user", "content": user_msg})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages,
        )

        actual_tokens = response.usage.input_tokens + response.usage.output_tokens
        tracker.record(actual_tokens)
        messages.append({"role": "assistant", "content": response.content[0].text})

        turns_left = tracker.turns_remaining_estimate
        forecast_str = f"~{turns_left:.1f} turns left" if turns_left is not None else "estimating..."

        print(f"[turn {turn_num}] {tracker.current_tokens:,} tokens ({tracker.usage_pct:.1%}) | {forecast_str}")

        if turns_left is not None and turns_left < 3:
            print(f"[FORECAST] ALERT: Only ~{turns_left:.1f} turns before context limit!")
            print("[ACTION] Triggering proactive summarization now...")
            break

        print(f"  Answer: {response.content[0].text[:80]}")


run_multi_turn_agent(
    "Explain how Python's GIL works in detail.",
    [
        "Now explain asyncio event loops in similar depth.",
        "What about multiprocessing? How does that differ?",
        "Explain thread-safety and locks in Python.",
        "How do concurrent.futures work?",
    ]
)

# Expected Token Savings: N/A; extrapolation fires 3+ turns early, leaving time for summarization without losing data
# Environment: Python 3.11+; use actual response.usage counts (not estimates) for accurate extrapolation
```

## Option 3: Budget-Aware Conversation Manager with Auto-Summarize

```python
import anthropic

client = anthropic.Anthropic()

MODEL_LIMIT = 200_000
SUMMARIZE_TRIGGER = 0.70  # Summarize when 70% full
SUMMARY_RESERVE = 0.20    # Keep 20% of limit for summary + future turns


class BudgetAwareConversation:
    """Manages a conversation that proactively summarizes when nearing the context limit."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001", limit: int = MODEL_LIMIT) -> None:
        self.model = model
        self.limit = limit
        self.messages: list[dict] = []
        self.summary: str = ""
        self.tokens_used = 0
        self.summarize_count = 0

    def _system(self) -> str:
        base = "You are a helpful assistant."
        if self.summary:
            return f"{base}\n\nConversation summary so far:\n{self.summary}"
        return base

    def _should_summarize(self) -> bool:
        return (self.tokens_used / self.limit) >= SUMMARIZE_TRIGGER

    def _summarize(self) -> None:
        """Compress conversation history into a summary."""
        history_text = "\n".join(
            f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[complex content]'}"
            for m in self.messages[:-2]  # Keep last exchange verbatim
        )
        if not history_text.strip():
            return

        response = client.messages.create(
            model=self.model,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Summarize this conversation in 5-8 bullet points, preserving key facts and decisions:\n\n{history_text}"
            }],
        )
        new_summary = response.content[0].text
        self.summary = (self.summary + "\n\n" + new_summary).strip() if self.summary else new_summary

        # Keep only last 2 messages
        self.messages = self.messages[-2:]
        self.summarize_count += 1
        print(f"[summarized] Compressed history. Summary now: {len(self.summary)} chars")

    def ask(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model=self.model,
            max_tokens=512,
            system=self._system(),
            messages=self.messages,
        )
        self.tokens_used = response.usage.input_tokens
        reply = response.content[0].text
        self.messages.append({"role": "assistant", "content": reply})

        usage_pct = self.tokens_used / self.limit
        status = "⚠️ " if usage_pct >= SUMMARIZE_TRIGGER else ""
        print(f"[context] {self.tokens_used:,} tokens ({usage_pct:.1%}) {status}")

        if self._should_summarize():
            self._summarize()

        return reply


conv = BudgetAwareConversation()
topics = [
    "Explain Python decorators.",
    "How do class decorators differ from function decorators?",
    "What are some common use cases for decorators in production code?",
    "How would you implement a retry decorator with exponential backoff?",
    "What are the pitfalls of overusing decorators?",
]

for topic in topics:
    print(f"\nUser: {topic[:60]}")
    answer = conv.ask(topic)
    print(f"Agent: {answer[:100]}")

print(f"\nTotal summarizations: {conv.summarize_count}")

# Expected Token Savings: 40-60% reduction in context size after each summarization cycle
# Environment: Python 3.11+; SUMMARIZE_TRIGGER=0.70 triggers early enough to fit summary + response in remaining budget
```

## Option 4: Multi-Turn Context Budget Planner with SQLite Log

```python
import anthropic
import sqlite3
import time
import statistics

client = anthropic.Anthropic()
DB_PATH = ":memory:"
MODEL_LIMIT = 200_000


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS context_usage (
            session_id TEXT NOT NULL,
            turn INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            recorded_at REAL NOT NULL,
            PRIMARY KEY (session_id, turn)
        )
    """)
    conn.commit()


def record_usage(conn: sqlite3.Connection, session_id: str, turn: int,
                 input_t: int, output_t: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO context_usage VALUES (?,?,?,?,?,?)",
        (session_id, turn, input_t, output_t, input_t + output_t, time.time())
    )
    conn.commit()


def forecast_turns_remaining(conn: sqlite3.Connection, session_id: str,
                              model_limit: int = MODEL_LIMIT) -> dict:
    rows = conn.execute(
        "SELECT turn, input_tokens FROM context_usage WHERE session_id=? ORDER BY turn",
        (session_id,)
    ).fetchall()

    if len(rows) < 2:
        return {"status": "insufficient_data", "turns_remaining": None}

    turns = [r[0] for r in rows]
    tokens = [r[1] for r in rows]

    # Growth per turn (last 3 turns)
    recent_tokens = tokens[-3:]
    recent_turns = turns[-3:]
    if len(recent_tokens) >= 2:
        deltas = [recent_tokens[i+1] - recent_tokens[i] for i in range(len(recent_tokens)-1)]
        avg_growth = statistics.mean(deltas) if deltas else 0
    else:
        avg_growth = tokens[-1] - tokens[0]

    current = tokens[-1]
    remaining = model_limit - current - 2000  # 2k reserve
    turns_left = remaining / avg_growth if avg_growth > 0 else float("inf")

    return {
        "status": "ok",
        "current_tokens": current,
        "usage_pct": current / model_limit,
        "avg_growth_per_turn": avg_growth,
        "turns_remaining": turns_left,
        "session_turns": len(rows),
    }


def run_session(conn: sqlite3.Connection, session_id: str, questions: list[str]) -> None:
    messages: list[dict] = []

    for turn_num, question in enumerate(questions, 1):
        messages.append({"role": "user", "content": question})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        record_usage(conn, session_id, turn_num, response.usage.input_tokens, response.usage.output_tokens)
        forecast = forecast_turns_remaining(conn, session_id)

        turns_left = forecast.get("turns_remaining")
        turns_str = f"{turns_left:.1f}" if turns_left and turns_left != float("inf") else "∞"
        print(f"[turn {turn_num}] {forecast.get('current_tokens', 0):,} tokens "
              f"({forecast.get('usage_pct', 0):.1%}) | ~{turns_str} turns remaining "
              f"| growth={forecast.get('avg_growth_per_turn', 0):.0f} tok/turn")

        if turns_left is not None and turns_left < 5:
            print(f"[FORECAST] Only ~{turns_left:.1f} turns before context limit — take action!")


conn = sqlite3.connect(DB_PATH)
init_db(conn)

questions = [
    "Explain the concept of closures in Python.",
    "How do closures differ from classes?",
    "What are common use cases for closures in real applications?",
    "Explain variable scoping (LEGB rule) in detail.",
    "How do closures interact with async functions?",
]

run_session(conn, "session-001", questions)

# Expected Token Savings: N/A; growth tracking lets you plan proactive actions 5+ turns ahead
# Environment: Python 3.11+; store session data in persistent DB; alert ops when sessions approach 90% usage
```

## Option 5: Token Budget Allocator with Per-Phase Limits

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MODEL_LIMIT = 200_000

# Budget allocation per agent phase (fractions must sum to <= 1.0)
PHASE_BUDGETS = {
    "system_context":   0.10,   # 10% for system prompt + static context
    "conversation":     0.50,   # 50% for multi-turn dialogue
    "tool_results":     0.20,   # 20% for tool call inputs/outputs
    "final_synthesis":  0.15,   # 15% reserved for final answer generation
    "buffer":           0.05,   # 5% safety buffer
}


class PhaseBudgetManager:
    def __init__(self, limit: int = MODEL_LIMIT) -> None:
        self.limit = limit
        self.phases = {k: int(v * limit) for k, v in PHASE_BUDGETS.items()}
        self.used: dict[str, int] = {k: 0 for k in self.phases}

    def remaining(self, phase: str) -> int:
        return max(0, self.phases.get(phase, 0) - self.used.get(phase, 0))

    def consume(self, phase: str, tokens: int) -> None:
        self.used[phase] = self.used.get(phase, 0) + tokens
        if self.used[phase] > self.phases.get(phase, 0):
            print(f"[OVER BUDGET] Phase '{phase}' exceeded by {self.used[phase] - self.phases[phase]:,} tokens")

    def report(self) -> None:
        print("\nPhase budget report:")
        for phase, budget in self.phases.items():
            used = self.used.get(phase, 0)
            pct = used / budget if budget > 0 else 0
            bar = "█" * int(pct * 20) + "░" * (20 - int(pct * 20))
            print(f"  {phase:20s} [{bar}] {used:>6,}/{budget:>6,} ({pct:.0%})")


async def run_phased_agent(task: str) -> str:
    manager = PhaseBudgetManager()

    # Phase 1: System context
    system = "You are a technical research assistant. Provide detailed, accurate answers."
    manager.consume("system_context", len(system) // 4)

    # Phase 2: Conversation (multi-turn)
    messages: list[dict] = []
    conversation_questions = [
        task,
        "What are the most important caveats or limitations?",
        "Give a concrete practical example.",
    ]

    for q in conversation_questions:
        remaining = manager.remaining("conversation")
        if remaining < 100:
            print("[BUDGET] Conversation budget exhausted — proceeding to synthesis")
            break

        messages.append({"role": "user", "content": q})
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=min(256, remaining),
            system=system,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})
        manager.consume("conversation", response.usage.input_tokens + response.usage.output_tokens)
        print(f"[conversation] Remaining budget: {manager.remaining('conversation'):,} tokens")

    # Phase 3: Final synthesis with reserved budget
    synth_budget = manager.remaining("final_synthesis")
    messages.append({"role": "user", "content": "Provide a concise final summary of everything discussed."})
    final_response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=min(512, synth_budget),
        system=system,
        messages=messages,
    )
    manager.consume("final_synthesis", final_response.usage.input_tokens + final_response.usage.output_tokens)

    manager.report()
    return final_response.content[0].text


result = asyncio.run(run_phased_agent("Explain how Python's memory management and garbage collection work."))
print(f"\nFinal answer:\n{result[:300]}")

# Expected Token Savings: 15-25% by preventing conversation phase from consuming synthesis budget
# Environment: Python 3.11+; tune PHASE_BUDGETS based on your task profile; monitor over-budget events in production
```

## Option 6: Context Pressure Score with Adaptive Response Length

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MODEL_LIMIT = 200_000


def context_pressure(input_tokens: int, limit: int = MODEL_LIMIT) -> float:
    """0.0 = no pressure, 1.0 = at limit."""
    return input_tokens / limit


def adaptive_max_tokens(pressure: float, base: int = 512, minimum: int = 64) -> int:
    """Reduce max_tokens as context pressure increases."""
    if pressure < 0.5:
        return base
    if pressure < 0.7:
        return max(minimum, int(base * 0.75))
    if pressure < 0.85:
        return max(minimum, int(base * 0.50))
    if pressure < 0.95:
        return max(minimum, int(base * 0.25))
    return minimum


def adaptive_instruction(pressure: float) -> str:
    """Add brevity instruction as pressure grows."""
    if pressure < 0.5:
        return ""
    if pressure < 0.7:
        return " Be concise."
    if pressure < 0.85:
        return " Be very brief (2-3 sentences max)."
    return " Answer in one sentence only."


async def run_pressure_aware_agent(topics: list[str]) -> None:
    messages: list[dict] = []
    current_pressure = 0.0

    for i, topic in enumerate(topics, 1):
        brevity = adaptive_instruction(current_pressure)
        full_question = topic + brevity
        messages.append({"role": "user", "content": full_question})

        max_tok = adaptive_max_tokens(current_pressure)

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tok,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        current_pressure = context_pressure(response.usage.input_tokens)
        print(f"[turn {i}] pressure={current_pressure:.2f} | max_tokens={max_tok} | brevity={bool(brevity)}")
        print(f"  Q: {topic[:60]}")
        print(f"  A: {reply[:80]}\n")

        if current_pressure > 0.95:
            print("[PRESSURE] Context at 95%+ — halting to prevent overflow")
            break


topics = [
    "Explain Python's asyncio module.",
    "What is the difference between coroutines and threads?",
    "How does asyncio's event loop work internally?",
    "What are common pitfalls with async/await?",
    "How do you test async code effectively?",
    "What is the role of the asyncio.run() function?",
    "Explain asyncio.gather vs asyncio.wait.",
]

asyncio.run(run_pressure_aware_agent(topics))

# Expected Token Savings: 30-50% from shortened responses at high pressure — buys 2-4 additional turns
# Environment: Python 3.11+; adaptive_max_tokens can also route to a smaller model at high pressure
```

## Comparison

| Option | Forecasting Method | Proactive Action | SQLite | Adaptive | Best For |
|--------|-------------------|-----------------|--------|----------|----------|
| 1. Per-Turn Counter | Usage % threshold | Warning + truncate | No | No | Simple long conversations |
| 2. Growth Extrapolation | Linear extrapolation | Alert 3+ turns early | No | No | Predictable-growth tasks |
| 3. Auto-Summarize | Usage % + compress | Auto-summarize | No | No | Open-ended chat sessions |
| 4. SQLite Budget Planner | Historical growth stats | Alert + log | Yes | No | Analytics-driven planning |
| 5. Phase Budget | Per-phase allocation | Phase-level limits | No | Partial | Structured multi-phase tasks |
| 6. Pressure Score | Pressure ratio | Adaptive response length | No | Yes | Graceful degradation under load |
