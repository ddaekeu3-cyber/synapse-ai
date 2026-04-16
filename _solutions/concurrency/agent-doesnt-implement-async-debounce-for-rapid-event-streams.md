---
layout: solution
title: "Agent Doesn't Implement Async Debounce for Rapid Event Streams"
description: "How to debounce or throttle high-frequency async events — streaming tokens, user keystrokes, webhook bursts — so downstream processing runs at a controlled rate."
tags: [concurrency, asyncio, debounce, throttle, streaming, events, performance]
difficulty: intermediate
solution_count: 6
---

## Problem

Agents receive high-frequency events: streaming LLM tokens arriving at 50/second, user keystrokes that trigger re-evaluation, webhook callbacks that fire in bursts, or tool results that arrive faster than downstream consumers can process. Processing every event immediately wastes CPU, saturates downstream services, and causes redundant work when only the final state matters.

```python
# Bad: process every token as it arrives
async for token in stream:
    await update_ui(token)      # called 50x/second — floods the UI layer
    await re_rank_results(token) # expensive re-ranking on every partial token
    await log_token(token)       # log volume scales with token rate
```

---

## Solution 1 — Simple Trailing-Edge Debounce

Wait for a quiet period before processing. If new events arrive within the window, reset the timer. Only fires once the stream is quiet for `delay` seconds.

```python
import asyncio
from typing import Any, Callable, Awaitable

class AsyncDebounce:
    """Trailing-edge debounce: fires after `delay` seconds of silence."""

    def __init__(self, delay: float):
        self._delay = delay
        self._task: asyncio.Task | None = None
        self._latest: Any = None

    def call(self, value: Any, fn: Callable[[Any], Awaitable[None]]) -> None:
        """Schedule fn(value) to run after delay seconds of silence."""
        self._latest = value
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._run(fn))

    async def _run(self, fn: Callable[[Any], Awaitable[None]]) -> None:
        try:
            await asyncio.sleep(self._delay)
            await fn(self._latest)
        except asyncio.CancelledError:
            pass  # a new call came in — this one is cancelled

    async def flush(self, fn: Callable[[Any], Awaitable[None]]) -> None:
        """Force immediate execution of the pending call."""
        if self._task and not self._task.done():
            self._task.cancel()
        if self._latest is not None:
            await fn(self._latest)

# Usage: debounce user-triggered re-ranking on streaming tokens
debouncer = AsyncDebounce(delay=0.3)

async def process_stream():
    partial_text = ""
    async for token in llm_token_stream():
        partial_text += token
        # Only re-rank after 300ms of no new tokens
        debouncer.call(partial_text, rerank_results)

    # Ensure final state is always processed
    await debouncer.flush(rerank_results)

async def rerank_results(text: str) -> None:
    print(f"Reranking with {len(text)} chars")
    # expensive operation runs at most every 300ms
```

---

## Solution 2 — Leading-Edge (Immediate) Debounce

Fire immediately on the first event, then suppress subsequent events for `delay` seconds. Best when responsiveness to the first event matters more than processing the latest.

```python
import asyncio
import time
from typing import Any, Callable, Awaitable

class LeadingEdgeDebounce:
    """Leading-edge debounce: fires immediately, then silences for `delay` seconds."""

    def __init__(self, delay: float):
        self._delay = delay
        self._last_fired: float = 0.0

    async def call(self, value: Any, fn: Callable[[Any], Awaitable[None]]) -> None:
        now = time.monotonic()
        if now - self._last_fired >= self._delay:
            self._last_fired = now
            await fn(value)
        # else: suppressed

class AsyncThrottle:
    """Rate-limit: allow at most one call per `interval` seconds."""

    def __init__(self, interval: float):
        self._interval = interval
        self._last_called: float = 0.0
        self._lock = asyncio.Lock()

    async def call(self, fn: Callable[..., Awaitable[Any]], *args, **kwargs) -> Any | None:
        async with self._lock:
            now = time.monotonic()
            if now - self._last_called < self._interval:
                return None  # throttled
            self._last_called = now

        return await fn(*args, **kwargs)

# Usage: stream tokens, update UI immediately on first token, then throttle to 10fps
ui_throttle = AsyncThrottle(interval=0.1)  # max 10 updates/second

async def process_stream():
    buffer = []
    async for token in llm_token_stream():
        buffer.append(token)
        # Show first token instantly, subsequent updates at most every 100ms
        await ui_throttle.call(render_ui, "".join(buffer))

async def render_ui(text: str) -> None:
    print(f"\r{text}", end="", flush=True)
```

---

## Solution 3 — Debounce with Accumulation: Batch Rapid Events

Collect all events that arrive within the debounce window into a batch, then process the full batch once the window expires. Avoids losing intermediate events while still reducing processing frequency.

```python
import asyncio
from typing import Any, Callable, Awaitable, TypeVar

T = TypeVar("T")

class AccumulatingDebounce:
    """
    Debounce that accumulates all events in the window into a list.
    Processes the full batch after `delay` seconds of silence.
    """

    def __init__(self, delay: float, max_batch: int = 1000):
        self._delay = delay
        self._max_batch = max_batch
        self._buffer: list[Any] = []
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def push(self, event: Any, fn: Callable[[list[Any]], Awaitable[None]]) -> None:
        async def _push_async():
            async with self._lock:
                self._buffer.append(event)
                if self._task and not self._task.done():
                    self._task.cancel()

                if len(self._buffer) >= self._max_batch:
                    # Immediate flush when batch is full
                    batch = self._buffer[:]
                    self._buffer.clear()
                    asyncio.create_task(fn(batch))
                else:
                    self._task = asyncio.create_task(self._run(fn))

        asyncio.create_task(_push_async())

    async def _run(self, fn: Callable[[list[Any]], Awaitable[None]]) -> None:
        try:
            await asyncio.sleep(self._delay)
            async with self._lock:
                if not self._buffer:
                    return
                batch = self._buffer[:]
                self._buffer.clear()
            await fn(batch)
        except asyncio.CancelledError:
            pass

    async def flush(self, fn: Callable[[list[Any]], Awaitable[None]]) -> None:
        async with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:]
            self._buffer.clear()
        await fn(batch)

# Usage: accumulate webhook events from a burst, process as a batch
webhook_debouncer = AccumulatingDebounce(delay=0.5, max_batch=100)

async def on_webhook_event(event: dict) -> None:
    webhook_debouncer.push(event, process_event_batch)

async def process_event_batch(events: list[dict]) -> None:
    print(f"Processing batch of {len(events)} events")
    # One DB write for 100 events instead of 100 individual writes
    await bulk_insert_events(events)
```

---

## Solution 4 — Token-Stream Debounce with Partial Flush

Special-purpose debounce for LLM streaming: accumulate tokens, flush partial output every N tokens OR after a sentence boundary, whichever comes first.

```python
import asyncio
import re
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

SENTENCE_END = re.compile(r"[.!?]\s")

class StreamingDebounce:
    """
    Flush accumulated tokens when:
    1. A sentence boundary is detected
    2. `max_tokens` tokens have accumulated
    3. `timeout` seconds of silence (end of stream)
    """

    def __init__(self, max_tokens: int = 20, timeout: float = 0.2,
                 on_flush: callable = None):
        self._max = max_tokens
        self._timeout = timeout
        self._on_flush = on_flush
        self._buffer = ""
        self._task: asyncio.Task | None = None

    def push_token(self, token: str) -> None:
        self._buffer += token

        if self._task and not self._task.done():
            self._task.cancel()

        # Flush conditions
        should_flush = (
            len(self._buffer) >= self._max
            or SENTENCE_END.search(self._buffer[-5:] if len(self._buffer) >= 5 else self._buffer)
        )

        if should_flush:
            asyncio.create_task(self._flush_now())
        else:
            self._task = asyncio.create_task(self._timeout_flush())

    async def _flush_now(self) -> None:
        if self._buffer and self._on_flush:
            chunk = self._buffer
            self._buffer = ""
            await self._on_flush(chunk)

    async def _timeout_flush(self) -> None:
        try:
            await asyncio.sleep(self._timeout)
            await self._flush_now()
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        """Call after stream ends to flush remaining content."""
        if self._task and not self._task.done():
            self._task.cancel()
        await self._flush_now()

# Usage
rendered_chunks = []

async def render_chunk(chunk: str) -> None:
    rendered_chunks.append(chunk)
    print(chunk, end="", flush=True)

async def stream_with_debounce(prompt: str) -> str:
    debouncer = StreamingDebounce(max_tokens=30, timeout=0.15, on_flush=render_chunk)

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for token in stream.text_stream:
            debouncer.push_token(token)

    await debouncer.close()
    return "".join(rendered_chunks)

result = asyncio.run(stream_with_debounce("Explain quantum entanglement."))
```

---

## Solution 5 — Async Queue Debounce: Replace Old Events with New Ones

Use an asyncio.Queue with a "replace" semantic: if a new event arrives before the old one is processed, discard the old one. Guarantees workers always see the most recent state.

```python
import asyncio
from typing import Any, Callable, Awaitable

class LatestValueQueue:
    """
    A queue that always holds the latest value.
    New puts replace unprocessed old values.
    Workers always process the most recent event.
    """

    def __init__(self):
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)

    async def put(self, value: Any) -> None:
        # Drain old value if present
        try:
            self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        await self._queue.put(value)

    async def get(self) -> Any:
        return await self._queue.get()

class ReplaceDebouncer:
    """
    Debounce by discarding intermediate events.
    The worker always processes only the latest value.
    """

    def __init__(self, delay: float, worker: Callable[[Any], Awaitable[None]]):
        self._delay = delay
        self._queue = LatestValueQueue()
        self._running = False
        asyncio.create_task(self._worker_loop(worker))

    async def push(self, value: Any) -> None:
        await self._queue.put(value)

    async def _worker_loop(self, worker: Callable[[Any], Awaitable[None]]) -> None:
        while True:
            value = await self._queue.get()
            await asyncio.sleep(self._delay)  # wait for more events

            # Check if a newer value arrived during the wait
            try:
                value = self._queue._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass  # no newer value — process current one

            await worker(value)

# Usage: user is typing a search query — only search on the latest text
debouncer = ReplaceDebouncer(delay=0.3, worker=perform_semantic_search)

async def on_user_keystroke(current_text: str) -> None:
    await debouncer.push(current_text)

async def perform_semantic_search(query: str) -> None:
    print(f"Searching: {query!r}")
    # Only the latest query is searched — intermediate keystrokes are discarded
```

---

## Solution 6 — Multi-Channel Debounce with Per-Key Windows

Maintain independent debounce windows per key (user ID, session ID, tool name). Events for different keys don't interfere with each other.

```python
import asyncio
import time
from typing import Any, Callable, Awaitable

class PerKeyDebounce:
    """
    Independent debounce window per key.
    Ideal for per-user or per-session rate limiting.
    """

    def __init__(self, delay: float):
        self._delay = delay
        self._tasks: dict[str, asyncio.Task] = {}
        self._latest: dict[str, Any] = {}
        self._call_counts: dict[str, int] = {}
        self._suppressed: dict[str, int] = {}

    def call(self, key: str, value: Any,
             fn: Callable[[str, Any], Awaitable[None]]) -> None:
        self._latest[key] = value
        self._call_counts[key] = self._call_counts.get(key, 0) + 1

        existing = self._tasks.get(key)
        if existing and not existing.done():
            existing.cancel()
            self._suppressed[key] = self._suppressed.get(key, 0) + 1

        self._tasks[key] = asyncio.create_task(self._run(key, fn))

    async def _run(self, key: str, fn: Callable[[str, Any], Awaitable[None]]) -> None:
        try:
            await asyncio.sleep(self._delay)
            await fn(key, self._latest[key])
        except asyncio.CancelledError:
            pass

    def stats(self) -> dict:
        return {
            key: {
                "total_calls": self._call_counts.get(key, 0),
                "suppressed": self._suppressed.get(key, 0),
                "efficiency": (
                    f"{self._suppressed.get(key, 0) / max(self._call_counts.get(key, 1), 1):.1%}"
                ),
            }
            for key in self._call_counts
        }

# Usage: per-user search debounce — user A's typing doesn't delay user B's search
per_user_debounce = PerKeyDebounce(delay=0.25)

async def on_user_input(user_id: str, text: str) -> None:
    per_user_debounce.call(user_id, text, run_user_search)

async def run_user_search(user_id: str, query: str) -> None:
    print(f"[{user_id}] searching: {query!r}")
    # expensive search only runs after user pauses typing

# Simulate 3 users typing simultaneously
async def simulate():
    await asyncio.gather(
        on_user_input("alice", "who"),
        on_user_input("bob", "what"),
        on_user_input("alice", "who is"),      # cancels previous alice call
        on_user_input("charlie", "when"),
        on_user_input("alice", "who is the"),  # cancels previous alice call
        on_user_input("bob", "what is"),       # cancels previous bob call
    )
    await asyncio.sleep(0.5)
    print(per_user_debounce.stats())
    # alice: 3 calls, 2 suppressed (67% efficiency)
    # bob:   2 calls, 1 suppressed (50% efficiency)

asyncio.run(simulate())
```

---

## Comparison

| Approach | Event Loss | Latency | Batch Output | Per-Key | Best For |
|---|---|---|---|---|---|
| Trailing-edge debounce | Latest only | `delay` after last event | No | No | Rerank after typing stops |
| Leading-edge / throttle | Intermediate | Immediate (first) | No | No | Real-time UI updates |
| Accumulating debounce | **None** (batched) | `delay` after last event | **Yes** | No | Webhook burst processing |
| Token-stream debounce | **None** (sentence-aware) | At boundaries | Partial | No | LLM streaming rendering |
| Latest-value queue | All but latest | `delay` after last event | No | No | Live search / typeahead |
| Per-key debounce | Latest per key | `delay` per key | No | **Yes** | Multi-user concurrent input |
