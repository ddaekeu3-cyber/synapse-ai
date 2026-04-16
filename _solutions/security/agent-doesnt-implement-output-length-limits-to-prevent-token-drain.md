---
title: "Agent Doesn't Implement Output Length Limits to Prevent Token Drain"
description: "Agents without output length controls are vulnerable to prompt injection and runaway responses that exhaust token budgets, inflate costs, and degrade service for other users."
difficulty: intermediate
category: security
tags: [security, token-budget, rate-limiting, cost-control, max-tokens, prompt-injection]
---

# Agent Doesn't Implement Output Length Limits to Prevent Token Drain

## Problem

A malicious or poorly-formed prompt can cause an LLM agent to generate arbitrarily long responses. Without hard output limits, a single request can drain the entire token budget for a billing period, cause latency spikes that starve concurrent users, and expose the system to denial-of-wallet attacks. Even benign users accidentally trigger this with open-ended prompts like "list everything you know about X."

**Symptoms:**
- Single requests consuming 10,000+ output tokens unexpectedly
- Monthly token bills spiking without increased user count
- P99 response latency climbing as context window fills
- Streaming responses that never terminate
- Cost anomalies traced to a handful of sessions

---

## Solution 1: Hard max_tokens Enforcement at API Level

The simplest defense: set `max_tokens` on every API call and never allow callers to override it above a cap.

```python
import asyncio
from dataclasses import dataclass
from typing import Optional
import anthropic

MAX_OUTPUT_TOKENS_DEFAULT = 1024
MAX_OUTPUT_TOKENS_HARD_CAP = 4096   # Never exceed this regardless of request


@dataclass
class OutputPolicy:
    default_max: int = MAX_OUTPUT_TOKENS_DEFAULT
    hard_cap: int = MAX_OUTPUT_TOKENS_HARD_CAP
    truncation_notice: bool = True  # Append notice when capped

    def resolve(self, requested: Optional[int] = None) -> int:
        if requested is None:
            return self.default_max
        return min(requested, self.hard_cap)


class BoundedAnthropicClient:
    def __init__(self, api_key: str, policy: OutputPolicy = OutputPolicy()):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.policy = policy

    async def create(
        self,
        messages: list[dict],
        system: str = "",
        requested_max_tokens: Optional[int] = None,
        model: str = "claude-opus-4-6",
    ) -> dict:
        max_tokens = self.policy.resolve(requested_max_tokens)
        was_capped = (
            requested_max_tokens is not None
            and requested_max_tokens > self.policy.hard_cap
        )

        response = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )

        text = response.content[0].text
        stop_reason = response.stop_reason

        # Detect natural truncation (model hit the cap)
        if stop_reason == "max_tokens" and self.policy.truncation_notice:
            text += "\n\n[Response truncated — output limit reached. Ask a more specific question for complete details.]"

        return {
            "text": text,
            "stop_reason": stop_reason,
            "was_capped": was_capped,
            "output_tokens": response.usage.output_tokens,
            "max_tokens_used": max_tokens,
        }


async def demo():
    client = BoundedAnthropicClient(api_key="sk-...")

    # Attempt to request 100k tokens — silently capped to 4096
    result = await client.create(
        messages=[{"role": "user", "content": "List every programming language ever created."}],
        requested_max_tokens=100_000,
    )
    print(f"capped={result['was_capped']} tokens={result['output_tokens']}")
    print(result["text"][:200])

# asyncio.run(demo())
```

---

## Solution 2: Per-Session Output Token Budget

Track cumulative output tokens per session and reject or throttle requests once the budget is exhausted.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
import anthropic


@dataclass
class SessionBudget:
    session_id: str
    total_budget: int = 20_000          # tokens per session
    per_request_cap: int = 2_048        # max per single call
    used: int = 0
    request_count: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def remaining(self) -> int:
        return max(0, self.total_budget - self.used)

    @property
    def is_exhausted(self) -> bool:
        return self.remaining == 0

    def charge(self, tokens: int) -> None:
        self.used += tokens
        self.request_count += 1

    def allowed_tokens(self) -> int:
        """Effective max_tokens for the next request."""
        return min(self.per_request_cap, self.remaining)


class BudgetedAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self._sessions: dict[str, SessionBudget] = {}
        self._lock = asyncio.Lock()

    def _get_or_create(self, session_id: str) -> SessionBudget:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionBudget(session_id=session_id)
        return self._sessions[session_id]

    async def chat(
        self,
        session_id: str,
        message: str,
        system: str = "You are a helpful assistant.",
    ) -> dict:
        async with self._lock:
            budget = self._get_or_create(session_id)

            if budget.is_exhausted:
                return {
                    "error": "session_budget_exhausted",
                    "message": "Output token budget for this session is exhausted. Start a new session.",
                    "used": budget.used,
                    "total": budget.total_budget,
                }

            max_tokens = budget.allowed_tokens()

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": message}],
        )

        async with self._lock:
            budget.charge(response.usage.output_tokens)
            print(
                f"[budget] session={session_id} used={budget.used}/{budget.total_budget} "
                f"this_request={response.usage.output_tokens}"
            )

        return {
            "text": response.content[0].text,
            "stop_reason": response.stop_reason,
            "budget_remaining": budget.remaining,
        }


async def demo():
    agent = BudgetedAgent(api_key="sk-...")
    sid = "sess_abc123"

    for i in range(5):
        result = await agent.chat(sid, f"Write a 500-word essay on topic {i}.")
        if "error" in result:
            print(f"Turn {i}: {result['message']}")
            break
        print(f"Turn {i}: tokens_remaining={result['budget_remaining']}")

# asyncio.run(demo())
```

---

## Solution 3: Streaming Abort on Length Threshold

For streaming responses, abort the stream once a token count threshold is hit mid-generation.

```python
import asyncio
from typing import AsyncIterator, Optional
import anthropic


class StreamingLengthGuard:
    def __init__(self, api_key: str, max_output_tokens: int = 1024):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.max_output_tokens = max_output_tokens

    async def stream_with_limit(
        self,
        messages: list[dict],
        system: str = "",
    ) -> AsyncIterator[str]:
        """Yields text chunks; aborts stream when token limit is hit."""
        token_count = 0
        aborted = False

        async with self.client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=self.max_output_tokens,
            system=system,
            messages=messages,
        ) as stream:
            async for event in stream:
                if hasattr(event, "type"):
                    if event.type == "content_block_delta":
                        chunk = event.delta.text if hasattr(event.delta, "text") else ""
                        if chunk:
                            # Rough token estimate: 4 chars ≈ 1 token
                            token_count += len(chunk) // 4
                            yield chunk

                        if token_count >= self.max_output_tokens:
                            aborted = True
                            break

                    elif event.type == "message_delta":
                        if hasattr(event, "usage"):
                            token_count = event.usage.output_tokens

        if aborted:
            yield "\n\n[Stream aborted — output length limit reached.]"


async def demo():
    guard = StreamingLengthGuard(api_key="sk-...", max_output_tokens=200)

    async for chunk in guard.stream_with_limit(
        messages=[{"role": "user", "content": "Write a very long story about the ocean."}],
    ):
        print(chunk, end="", flush=True)
    print()

# asyncio.run(demo())
```

---

## Solution 4: LLM Self-Limit Instruction in System Prompt

Instruct the model to be concise and self-police its output length as a soft defense layer.

```python
import asyncio
from dataclasses import dataclass
from typing import Optional
import anthropic


@dataclass
class OutputLengthDirective:
    max_words: Optional[int] = None
    max_sentences: Optional[int] = None
    max_paragraphs: Optional[int] = None
    format_instruction: str = ""

    def to_system_addendum(self) -> str:
        parts = []
        if self.max_words:
            parts.append(f"Keep your response under {self.max_words} words.")
        if self.max_sentences:
            parts.append(f"Use no more than {self.max_sentences} sentences.")
        if self.max_paragraphs:
            parts.append(f"Limit your response to {self.max_paragraphs} paragraph(s).")
        if self.format_instruction:
            parts.append(self.format_instruction)
        parts.append("Do not pad or repeat yourself to fill space.")
        return " ".join(parts)


class SelfLimitingAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def ask(
        self,
        question: str,
        base_system: str = "You are a helpful assistant.",
        directive: Optional[OutputLengthDirective] = None,
        max_tokens: int = 1024,
    ) -> str:
        if directive is None:
            directive = OutputLengthDirective(max_words=300)

        system = base_system.rstrip()
        addendum = directive.to_system_addendum()
        if addendum:
            system = f"{system}\n\n{addendum}"

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": question}],
        )

        text = response.content[0].text
        word_count = len(text.split())
        print(f"[length] words={word_count} tokens={response.usage.output_tokens} stop={response.stop_reason}")
        return text


async def demo():
    agent = SelfLimitingAgent(api_key="sk-...")

    # Potentially open-ended question — directive keeps it tight
    answer = await agent.ask(
        "Tell me everything about machine learning.",
        directive=OutputLengthDirective(
            max_words=150,
            format_instruction="Use exactly 3 bullet points.",
        ),
    )
    print(answer)

# asyncio.run(demo())
```

---

## Solution 5: Per-Request Output Cost Cap

Enforce a dollar cost ceiling per request, dynamically computing max_tokens from the per-output-token price.

```python
import asyncio
from dataclasses import dataclass
from typing import Optional
import anthropic

# claude-opus-4-6 pricing (USD per million tokens)
OUTPUT_PRICE_PER_M = 15.0   # $15 per 1M output tokens
OUTPUT_PRICE_PER_TOKEN = OUTPUT_PRICE_PER_M / 1_000_000


@dataclass
class CostPolicy:
    max_cost_per_request_usd: float = 0.01     # $0.01 hard cap per call
    max_cost_per_session_usd: float = 0.10     # $0.10 per session
    hard_token_floor: int = 64                 # Always allow at least this many tokens
    hard_token_cap: int = 4096                 # Never exceed this

    def max_tokens_for_budget(self, remaining_budget_usd: float) -> int:
        tokens = int(remaining_budget_usd / OUTPUT_PRICE_PER_TOKEN)
        return max(self.hard_token_floor, min(tokens, self.hard_token_cap))


class CostCappedAgent:
    def __init__(self, api_key: str, policy: CostPolicy = CostPolicy()):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.policy = policy
        self._session_costs: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _session_remaining(self, session_id: str) -> float:
        spent = self._session_costs.get(session_id, 0.0)
        return max(0.0, self.policy.max_cost_per_session_usd - spent)

    async def ask(
        self,
        session_id: str,
        message: str,
        system: str = "You are a helpful assistant.",
    ) -> dict:
        async with self._lock:
            session_remaining = self._session_remaining(session_id)
            if session_remaining <= 0:
                return {"error": "session_cost_limit_reached", "session_id": session_id}

            # The tighter of per-request cap and session remainder
            effective_budget = min(self.policy.max_cost_per_request_usd, session_remaining)
            max_tokens = self.policy.max_tokens_for_budget(effective_budget)

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": message}],
        )

        output_cost = response.usage.output_tokens * OUTPUT_PRICE_PER_TOKEN
        input_cost = response.usage.input_tokens * (3.0 / 1_000_000)  # $3/M input
        total_cost = output_cost + input_cost

        async with self._lock:
            self._session_costs[session_id] = (
                self._session_costs.get(session_id, 0.0) + total_cost
            )
            session_remaining_after = self._session_remaining(session_id)

        print(
            f"[cost] session={session_id} request_cost=${total_cost:.5f} "
            f"session_remaining=${session_remaining_after:.5f}"
        )

        return {
            "text": response.content[0].text,
            "cost_usd": total_cost,
            "session_remaining_usd": session_remaining_after,
        }


async def demo():
    policy = CostPolicy(max_cost_per_request_usd=0.002, max_cost_per_session_usd=0.005)
    agent = CostCappedAgent(api_key="sk-...", policy=policy)

    for i in range(10):
        result = await agent.ask("sess_xyz", f"Explain topic {i} in great detail.")
        if "error" in result:
            print(f"Turn {i}: {result['error']}")
            break
        print(f"Turn {i}: cost=${result['cost_usd']:.5f} remaining=${result['session_remaining_usd']:.5f}")

# asyncio.run(demo())
```

---

## Solution 6: Output Length Audit Log with Anomaly Alerting

Log every response length and alert when a session or user exceeds statistical norms.

```python
import asyncio
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Optional
import anthropic


@dataclass
class OutputRecord:
    session_id: str
    timestamp: float
    output_tokens: int
    stop_reason: str
    prompt_preview: str  # first 50 chars of user message


class OutputAuditLog:
    def __init__(
        self,
        window_size: int = 20,          # rolling window per session
        z_score_threshold: float = 3.0, # alert if z-score exceeds this
        alert_callback: Optional[Callable] = None,
    ):
        self._records: dict[str, deque] = defaultdict(lambda: deque(maxlen=window_size))
        self._z_threshold = z_score_threshold
        self._alert_callback = alert_callback or self._default_alert

    def _default_alert(self, session_id: str, record: OutputRecord, z_score: float):
        print(
            f"[ALERT] Anomalous output: session={session_id} "
            f"tokens={record.output_tokens} z={z_score:.2f} "
            f"prompt='{record.prompt_preview}'"
        )

    def record(self, record: OutputRecord) -> Optional[float]:
        """Record response. Returns z-score if anomalous, else None."""
        history = self._records[record.session_id]
        history.append(record.output_tokens)

        if len(history) < 5:
            return None  # Not enough data yet

        mean = statistics.mean(history)
        stdev = statistics.stdev(history) or 1.0
        z = (record.output_tokens - mean) / stdev

        if z > self._z_threshold:
            self._alert_callback(record.session_id, record, z)
            return z
        return None


class AuditedAgent:
    def __init__(self, api_key: str, max_tokens: int = 1024):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.max_tokens = max_tokens
        self.audit = OutputAuditLog(z_score_threshold=2.5)

    async def ask(self, session_id: str, message: str) -> str:
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": message}],
        )

        record = OutputRecord(
            session_id=session_id,
            timestamp=time.time(),
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
            prompt_preview=message[:50],
        )
        z = self.audit.record(record)
        if z is not None:
            # In production: also block session or require CAPTCHA
            print(f"[audit] Anomaly detected, consider throttling session={session_id}")

        return response.content[0].text


async def demo():
    agent = AuditedAgent(api_key="sk-...", max_tokens=512)
    sid = "user_42"

    for msg in [
        "Hi",
        "What is 2+2?",
        "One sentence about Python.",
        "Write a 10,000 word essay on the history of computing from 1940 to present.",
        "Thanks!",
    ]:
        reply = await agent.ask(sid, msg)
        print(f"Q: {msg[:40]!r} -> {len(reply)} chars")

# asyncio.run(demo())
```

---

## Comparison

| Solution | Mechanism | Prevents Cost Drain | Streaming Support | Per-Session | Complexity |
|---|---|---|---|---|---|
| Hard max_tokens | API parameter cap | Yes | N/A | No | Very Low |
| Session token budget | Cumulative counter | Yes | No | Yes | Low |
| Streaming abort | Mid-stream token count | Yes | Yes | No | Medium |
| LLM self-limit instruction | System prompt directive | Soft only | Yes | No | Very Low |
| Per-request cost cap | USD budget → max_tokens | Yes | No | Yes | Medium |
| Output audit log | Z-score anomaly detection | Alert only | No | Yes | Medium |

**Recommendation:** Layer solutions 1 + 2 + 4 for defense in depth: always set `max_tokens` (Solution 1) with a hard cap, track session budgets (Solution 2) to prevent wallet drain across turns, and include a conciseness directive (Solution 4) in your system prompt to reduce average output length. Add Solution 6 for visibility into abuse patterns.
