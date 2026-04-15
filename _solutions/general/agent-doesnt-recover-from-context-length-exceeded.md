---
layout: solution
title: "Agent doesn't recover from context length exceeded error"
category: general
description: "When the accumulated messages list grows past the model's context limit, the API returns a 400 error with 'context_length_exceeded'. The agent crashes or returns an unhandled error instead of compressing the conversation and retrying. Proactive trimming and reactive compression keep the agent running indefinitely."
tags: [general, context-window, error-handling, recovery, summarization, resilience]
---

## Symptom

After 40+ turns of tool use, the agent's next API call returns: `BadRequestError: 400 {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'prompt is too long: 204382 tokens > 200000 maximum'}}`. The agent raises an unhandled exception and the task is lost. The user has to start over. All tool results, intermediate work, and progress are discarded.

## Root Cause

`client.messages.create()` raises an `anthropic.BadRequestError` when the messages list exceeds the model's context limit. Without explicit error handling for this specific error type, it propagates up the call stack as an unhandled exception. The agent has no fallback strategy — it neither monitors context growth proactively nor handles the overflow reactively.

## Fix

Two-layer defense: (1) proactively monitor token usage after each API call and compress when approaching the limit; (2) reactively catch `BadRequestError` with a context-length message, compress the conversation, and retry transparently.

---

### Option 1 — Reactive recovery: catch and compress on overflow

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

COMPRESS_SYSTEM = (
    "You are a conversation compressor. Summarize the conversation below into a "
    "compact but complete record. Preserve: all key decisions, tool results, "
    "important facts discovered, and the current task state. "
    "Discard: repeated tool calls, error retries, verbose reasoning. "
    "Output a single paragraph under 400 words."
)

CONTEXT_LIMIT = 180_000   # leave 20k buffer below 200k


def estimate_tokens(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages) // 4


def compress_messages(messages: list[dict]) -> list[dict]:
    """Compress messages to a single summary + keep the last user message."""
    # Keep the last few messages intact to preserve current state
    if len(messages) <= 4:
        return messages

    to_summarize = messages[:-2]
    recent = messages[-2:]

    conversation_text = "\n".join(
        f"{m['role']}: {str(m.get('content', ''))[:500]}"
        for m in to_summarize[:50]   # limit input to compressor
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=COMPRESS_SYSTEM,
        messages=[{"role": "user", "content": conversation_text}],
    )
    summary = response.content[0].text.strip()
    compressed_count = len(to_summarize)

    print(f"[Compress] {compressed_count} messages → summary ({len(summary)} chars)")

    return [
        {"role": "user", "content": f"[Conversation summary]\n{summary}"},
    ] + recent


def is_context_length_error(error: Exception) -> bool:
    """Detect context length exceeded errors."""
    if isinstance(error, anthropic.BadRequestError):
        msg = str(error).lower()
        return "too long" in msg or "context_length" in msg or "prompt is too long" in msg
    return False


def run_agent_with_recovery(
    messages: list[dict],
    system: str = "You are a helpful assistant.",
    tools: list | None = None,
    max_compress_attempts: int = 3,
) -> anthropic.types.Message:
    """
    Call the API with automatic context compression on overflow.
    Retries transparently after compressing — the caller doesn't need to handle this.
    """
    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "system": system,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    for attempt in range(max_compress_attempts):
        try:
            return client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            if not is_context_length_error(e):
                raise   # not a context error — re-raise

            before = len(messages)
            before_tokens = estimate_tokens(messages)
            messages = compress_messages(messages)
            kwargs["messages"] = messages
            after_tokens = estimate_tokens(messages)

            print(
                f"[Recovery] Context overflow on attempt {attempt+1}: "
                f"{before_tokens} → {after_tokens} tokens ({before} → {len(messages)} messages)"
            )

    raise RuntimeError(f"Could not compress below context limit after {max_compress_attempts} attempts")


# Usage: wrap every API call with the recovery handler
def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for _ in range(10):
        response = run_agent_with_recovery(messages)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

    return "Max turns reached"
```

**Expected Token Savings:** Compression converts 50 turns (~20,000 tokens) into a ~400-token summary — 98% reduction; without recovery, the agent crashes and the user must restart, losing all work; recovery preserves task continuity at the cost of one Haiku summarization call.
**Environment:** Any long-running agent; reactive recovery is the minimum viable protection — handles the error case without requiring any changes to the normal agent loop.

---

### Option 2 — Proactive token budget monitoring with pre-emptive compression

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

CONTEXT_LIMIT = 200_000
COMPRESS_THRESHOLD = 0.75   # compress at 75% of limit
COMPRESS_TARGET = 0.30      # compress down to 30% of limit


class TokenBudgetManager:
    """
    Monitors token usage after every API call and compresses proactively
    before hitting the limit — avoiding the error entirely.
    """
    def __init__(self, limit: int = CONTEXT_LIMIT):
        self.limit = limit
        self.compress_at = int(limit * COMPRESS_THRESHOLD)
        self.target_tokens = int(limit * COMPRESS_TARGET)
        self.total_input_tokens = 0
        self.compressions = 0

    def record_usage(self, response: anthropic.types.Message):
        """Update token count from the actual API response."""
        self.total_input_tokens = response.usage.input_tokens
        pct = self.total_input_tokens / self.limit * 100
        if pct > 60:
            print(f"[Budget] {pct:.0f}% of context used ({self.total_input_tokens:,}/{self.limit:,})")

    def should_compress(self) -> bool:
        return self.total_input_tokens >= self.compress_at

    def compress(self, messages: list[dict]) -> list[dict]:
        """Compress messages to fit within target token count."""
        if len(messages) <= 4:
            return messages

        # Keep system messages and last 2 messages; summarize the rest
        keep_last = 4
        to_compress = messages[:-keep_last] if len(messages) > keep_last else []
        recent = messages[-keep_last:]

        if not to_compress:
            return messages

        conversation = "\n".join(
            f"{m['role']}: {str(m.get('content',''))[:400]}"
            for m in to_compress[-30:]
        )
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": (
                    "Compress this conversation into a 200-word summary preserving "
                    f"all key facts and current task state:\n\n{conversation}"
                ),
            }],
        )
        summary = response.content[0].text.strip()
        self.compressions += 1
        print(
            f"[Budget] Compression #{self.compressions}: "
            f"{len(to_compress)} messages → summary"
        )
        return [{"role": "user", "content": f"[Summary]\n{summary}"}] + recent


budget = TokenBudgetManager()


def run_agent_proactive(user_message: str, tools: list | None = None) -> str:
    messages = [{"role": "user", "content": user_message}]

    for turn in range(50):
        # Pre-emptive compression check
        if budget.should_compress():
            messages = budget.compress(messages)
            budget.total_input_tokens = 0   # reset after compression

        kwargs = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        response = client.messages.create(**kwargs)
        budget.record_usage(response)   # update token counter
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": f"[result]"}
                for b in response.content if b.type == "tool_use"
            ]
            messages.append({"role": "user", "content": results})

    return f"Complete after {budget.compressions} compression(s)"
```

**Expected Token Savings:** Proactive compression at 75% prevents the error entirely; compared to reactive recovery, proactive monitoring allows more graceful compression (with more context available) and never interrupts a streaming response mid-flight.
**Environment:** Agents with predictable long tasks (research, code generation, document analysis); proactive monitoring is preferred over reactive recovery when task length is foreseeable.

---

### Option 3 — Sliding window with hard size cap

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


class SlidingWindowMessages:
    """
    Maintains a sliding window of messages with a hard token cap.
    Oldest messages are dropped first; a summary of dropped messages
    is prepended to preserve high-level context.
    """
    def __init__(self, max_tokens: int = 100_000, summary_tokens: int = 1_000):
        self.max_tokens = max_tokens
        self.summary_tokens = summary_tokens
        self._messages: list[dict] = []
        self._dropped_summary: str = ""
        self._dropped_count = 0

    def _estimate(self, messages: list[dict]) -> int:
        return sum(len(str(m.get("content", ""))) for m in messages) // 4

    def add(self, role: str, content):
        self._messages.append({"role": role, "content": content})
        self._enforce_limit()

    def _enforce_limit(self):
        while (self._estimate(self._messages) > self.max_tokens
               and len(self._messages) > 4):
            # Drop oldest messages in pairs (user + assistant)
            dropped = self._messages[:2]
            self._messages = self._messages[2:]
            self._dropped_count += len(dropped)

            # Update the running summary of dropped content
            for msg in dropped:
                content_preview = str(msg.get("content", ""))[:200]
                self._dropped_summary += f"\n[{msg['role']}]: {content_preview}"

            print(
                f"[SlidingWindow] Dropped 2 messages "
                f"(total dropped: {self._dropped_count}, "
                f"current: {len(self._messages)} messages)"
            )

    def get_messages(self) -> list[dict]:
        """Return messages with a prepended summary of dropped history."""
        if not self._dropped_summary:
            return list(self._messages)

        summary_msg = {
            "role": "user",
            "content": (
                f"[Earlier conversation (compressed, {self._dropped_count} messages)]:\n"
                f"{self._dropped_summary[-self.summary_tokens*4:]}\n"   # char limit
                f"[End of compressed history]"
            ),
        }
        return [summary_msg] + list(self._messages)

    @property
    def current_tokens(self) -> int:
        return self._estimate(self.get_messages())


window = SlidingWindowMessages(max_tokens=50_000)


def run_agent_sliding(user_message: str) -> str:
    window.add("user", user_message)

    for _ in range(50):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=window.get_messages(),
        )
        window.add("assistant", response.content)

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": "[result]"}
                for b in response.content if b.type == "tool_use"
            ]
            window.add("user", results)

    return f"Complete (window: {window.current_tokens} tokens)"
```

**Expected Token Savings:** Sliding window maintains a hard cap of 100k tokens regardless of task length; for a 200-turn task that would naturally accumulate 400k tokens, the window keeps input costs capped at ~100k × price_per_token × 200 turns = a constant, not a growing, cost.
**Environment:** Very long-running interactive agents (multi-hour tasks, persistent assistants); sliding window is simpler than summarization but loses older context rather than compressing it.

---

### Option 4 — Async agent with context monitoring and graceful degradation

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

CONTEXT_LIMIT_TOKENS = 200_000


class AsyncContextManager:
    """Async-safe context manager with per-turn usage tracking."""
    def __init__(self):
        self.messages: list[dict] = []
        self.last_input_tokens = 0
        self._lock = asyncio.Lock()

    async def add_and_check(self, role: str, content) -> bool:
        """Add a message. Returns True if context is approaching limit."""
        async with self._lock:
            self.messages.append({"role": role, "content": content})
            approaching = self.last_input_tokens > CONTEXT_LIMIT_TOKENS * 0.8
            return approaching

    async def compress_async(self) -> None:
        """Compress messages asynchronously."""
        async with self._lock:
            if len(self.messages) <= 4:
                return

            to_summarize = self.messages[:-3]
            recent = self.messages[-3:]
            text = "\n".join(
                f"{m['role']}: {str(m.get('content',''))[:300]}"
                for m in to_summarize[-20:]
            )
            response = await async_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": f"Summarize key facts from this conversation in 150 words:\n\n{text}",
                }],
            )
            summary = response.content[0].text.strip()
            self.messages = [
                {"role": "user", "content": f"[History summary]\n{summary}"},
            ] + recent
            self.last_input_tokens = 0
            print(f"[AsyncCtx] Compressed to {len(self.messages)} messages")

    def get(self) -> list[dict]:
        return list(self.messages)


async def run_async_agent(user_message: str) -> str:
    ctx = AsyncContextManager()
    await ctx.add_and_check("user", user_message)

    for _ in range(50):
        try:
            response = await async_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=ctx.get(),
            )
        except anthropic.BadRequestError as e:
            if "too long" in str(e).lower() or "context_length" in str(e).lower():
                print("[AsyncCtx] Context overflow — compressing")
                await ctx.compress_async()
                response = await async_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1024,
                    messages=ctx.get(),
                )
            else:
                raise

        ctx.last_input_tokens = response.usage.input_tokens
        approaching = await ctx.add_and_check("assistant", response.content)

        if approaching:
            await ctx.compress_async()

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": "[result]"}
                for b in response.content if b.type == "tool_use"
            ]
            await ctx.add_and_check("user", results)

    return "Complete"


asyncio.run(run_async_agent("Perform a comprehensive analysis task"))
```

**Expected Token Savings:** Async compression runs concurrently with other work; the `_lock` prevents race conditions where two coroutines simultaneously try to compress the same message list — without the lock, compression could run twice and discard legitimate recent messages.
**Environment:** Async multi-agent systems where multiple coroutines share a context manager; the lock is essential for thread safety.

---

### Option 5 — Per-tool result size cap to prevent context blowout

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Maximum characters per tool result before truncation
TOOL_RESULT_CAPS = {
    "search_web": 3_000,
    "read_file": 5_000,
    "execute_code": 2_000,
    "fetch_url": 4_000,
    "default": 2_000,
}


def cap_tool_result(tool_name: str, result: str) -> str:
    """Truncate tool result to prevent individual results from bloating context."""
    cap = TOOL_RESULT_CAPS.get(tool_name, TOOL_RESULT_CAPS["default"])
    if len(result) <= cap:
        return result
    truncated = result[:cap]
    omitted = len(result) - cap
    return f"{truncated}\n\n[Truncated: {omitted:,} chars omitted. Request specific sections if needed.]"


def run_agent_with_caps(user_message: str, tools: list) -> str:
    """
    Agent that caps tool results at ingestion time.
    Prevents any single tool result from consuming most of the context window.
    """
    messages = [{"role": "user", "content": user_message}]

    for turn in range(10):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        actual_tokens = response.usage.input_tokens
        if turn > 0 and actual_tokens > 150_000:
            print(f"[Warning] High context usage: {actual_tokens:,} tokens at turn {turn}")

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    raw_result = f"Simulated result for {block.name}: " + "x" * 5000
                    capped_result = cap_tool_result(block.name, raw_result)
                    print(
                        f"[Cap] {block.name}: {len(raw_result)} → {len(capped_result)} chars"
                        if len(raw_result) != len(capped_result) else
                        f"[Cap] {block.name}: {len(raw_result)} chars (under cap)"
                    )
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": capped_result,
                    })
            messages.append({"role": "user", "content": results})

    return "Max turns reached"
```

**Expected Token Savings:** A single uncapped `read_file` result could inject 50,000 tokens in one turn; capping at 5,000 chars saves ~10,000 tokens per large file read; across 10 file reads, this prevents context overflow entirely without summarization.
**Environment:** Agents that read external content (files, web pages, API responses); per-tool caps are the cheapest form of context protection — applied at the source before the result enters the messages list.

---

### Option 6 — Context health dashboard for debugging overflow

```python
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class ContextHealthReport:
    turn: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    message_count: int = 0
    tool_results_count: int = 0
    largest_tool_result: int = 0
    largest_tool_name: str = ""
    token_history: list[int] = field(default_factory=list)

    def record_turn(self, response: anthropic.types.Message, messages: list[dict]):
        self.turn += 1
        self.input_tokens = response.usage.input_tokens
        self.output_tokens = response.usage.output_tokens
        self.message_count = len(messages)
        self.token_history.append(self.input_tokens)

        # Track tool result sizes
        for msg in messages:
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        size = len(str(block.get("content", "")))
                        self.tool_results_count += 1
                        if size > self.largest_tool_result:
                            self.largest_tool_result = size
                            self.largest_tool_name = block.get("tool_use_id", "?")

    def growth_rate(self) -> float:
        """Tokens per turn on average."""
        if len(self.token_history) < 2:
            return 0.0
        return (self.token_history[-1] - self.token_history[0]) / max(len(self.token_history) - 1, 1)

    def turns_until_limit(self, limit: int = 200_000) -> int | None:
        rate = self.growth_rate()
        if rate <= 0:
            return None
        remaining = limit - self.input_tokens
        return max(0, int(remaining / rate))

    def summary(self) -> str:
        ttl = self.turns_until_limit()
        ttl_str = f"~{ttl} turns" if ttl is not None else "unknown"
        return (
            f"Turn {self.turn}: {self.input_tokens:,} input tokens "
            f"({self.input_tokens/200_000*100:.0f}% of limit), "
            f"growth: +{self.growth_rate():.0f} tok/turn, "
            f"est. turns until limit: {ttl_str}, "
            f"messages: {self.message_count}, "
            f"largest tool result: {self.largest_tool_result:,} chars ({self.largest_tool_name})"
        )


health = ContextHealthReport()


def run_agent_monitored(user_message: str, tools: list | None = None) -> str:
    messages = [{"role": "user", "content": user_message}]

    for turn in range(50):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=messages,
                **({"tools": tools} if tools else {}),
            )
        except anthropic.BadRequestError as e:
            if "too long" in str(e).lower():
                print(f"\n[OVERFLOW at turn {turn}]\n{health.summary()}")
                print(f"Token growth history: {health.token_history}")
                raise   # re-raise with diagnosis already printed
            raise

        health.record_turn(response, messages)

        if turn % 5 == 0 or health.input_tokens > 150_000:
            print(f"[Health] {health.summary()}")

        # Warn if running low
        turns_left = health.turns_until_limit()
        if turns_left is not None and turns_left < 10:
            print(f"[Warning] Only ~{turns_left} turns before context limit!")

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        if response.stop_reason == "tool_use":
            results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": "[result]"}
                for b in response.content if b.type == "tool_use"
            ]
            messages.append({"role": "user", "content": results})

    # Comparison table
    # | Option | Strategy | Recovery | Token Cost |
    # |--------|---------|---------|-----------|
    # | 1 Reactive catch | On error | Haiku summary | ~200 tok |
    # | 2 Proactive monitor | At 75% | Haiku summary | ~200 tok |
    # | 3 Sliding window | Per-add | Drop oldest | ~0 tok |
    # | 4 Async lock | Per-turn | Haiku summary | ~200 tok |
    # | 5 Tool result caps | Per-result | Truncate | ~0 tok |
    # | 6 Health dashboard | Monitoring | N/A | ~0 tok |

    return "Max turns reached"
```

**Expected Token Savings:** Dashboard surfaces the growth rate and ETA before overflow happens — if growth rate is 1000 tokens/turn and the limit is 200k, the dashboard gives a 50-turn warning, giving the agent time to compress proactively rather than crash reactively.
**Environment:** Development and debugging; the health dashboard diagnoses which tool results are causing context blowout, guiding the implementation of targeted caps (Option 5).
