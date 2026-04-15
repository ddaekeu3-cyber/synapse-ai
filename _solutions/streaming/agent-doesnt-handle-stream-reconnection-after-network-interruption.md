---
layout: solution
title: "Agent Doesn't Handle Stream Reconnection After Network Interruption"
category: streaming
description: "A mid-stream network drop causes the agent to lose all partial output and start from scratch, wasting tokens and increasing latency."
tags: [streaming, resilience, network, reliability, production]
---

## Symptom

During a long streamed response, a transient network error (TCP reset, timeout, proxy disconnect) causes the stream to close. The agent raises an exception, discards any text already received, and either crashes or retries from the very beginning — re-spending all the input tokens and making the user wait the full generation time again.

## Root Cause

The Anthropic streaming API delivers text incrementally over a persistent HTTP connection. If that connection drops, the SDK raises a network-level exception and the partially received content is gone. Without a reconnection strategy — accumulated text buffer, token offset tracking, or resume checkpoint — the agent has no choice but to restart the entire request.

## Fix

### Option 1 — Accumulate buffer, retry on network error, re-stream from scratch with buffered output

```python
import anthropic
import time

client = anthropic.Anthropic()

def stream_with_retry(prompt: str, max_retries: int = 3) -> str:
    accumulated = ""
    attempt = 0

    while attempt < max_retries:
        try:
            with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for delta in stream.text_stream:
                    accumulated += delta
                    print(delta, end="", flush=True)
            print()
            return accumulated  # clean exit
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            attempt += 1
            wait = 2 ** attempt
            print(f"\n[reconnect] network error ({exc}), retry {attempt}/{max_retries} in {wait}s")
            time.sleep(wait)

    raise RuntimeError(f"Stream failed after {max_retries} retries")

result = stream_with_retry("Write a detailed explanation of gradient descent.")
print(f"\n[done] {len(result)} chars received")
```

**Expected Token Savings:** Input tokens are re-spent on retry, but this prevents silent data loss and gives the user partial output rather than nothing.
**Environment:** Simple synchronous agents where occasional retries are acceptable; good baseline for any streaming use case.

---

### Option 2 — Yield-and-checkpoint: persist accumulated text to disk between chunks

```python
import anthropic
import json
import os
import time

client = anthropic.Anthropic()
STREAM_CHECKPOINT = "/tmp/stream_checkpoint.json"

def save_checkpoint(prompt_hash: str, text: str) -> None:
    tmp = STREAM_CHECKPOINT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"prompt_hash": prompt_hash, "text": text}, f)
    os.replace(tmp, STREAM_CHECKPOINT)

def load_checkpoint(prompt_hash: str) -> str:
    if os.path.exists(STREAM_CHECKPOINT):
        with open(STREAM_CHECKPOINT) as f:
            data = json.load(f)
        if data.get("prompt_hash") == prompt_hash:
            print(f"[checkpoint] resuming with {len(data['text'])} chars already received")
            return data["text"]
    return ""

def stream_with_checkpoint(prompt: str) -> str:
    import hashlib
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
    accumulated = load_checkpoint(prompt_hash)

    if accumulated:
        # Already have partial output — just extend with a continuation prompt
        print(f"[resume] replaying {len(accumulated)} chars from checkpoint")
        print(accumulated, end="")

    for attempt in range(4):
        try:
            # Re-run from scratch; checkpoint lets us show progress immediately
            fresh = ""
            with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for delta in stream.text_stream:
                    fresh += delta
                    print(delta, end="", flush=True)
                    # Checkpoint every 200 chars
                    if len(fresh) % 200 < len(delta):
                        save_checkpoint(prompt_hash, fresh)
            print()
            # Clean up checkpoint on success
            if os.path.exists(STREAM_CHECKPOINT):
                os.remove(STREAM_CHECKPOINT)
            return fresh
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            print(f"\n[reconnect] {exc}, retry {attempt + 1}/4 in {2 ** attempt}s")
            time.sleep(2 ** attempt)

    raise RuntimeError("Stream exhausted retries")

result = stream_with_checkpoint("Explain transformer architecture in depth.")
print(f"\n[done] {len(result)} chars")
```

**Expected Token Savings:** Checkpoint lets you show cached output while the retry re-generates; the user sees progress immediately on reconnect instead of blank screen.
**Environment:** Long-form content generation where partial results have value (documents, reports, essays).

---

### Option 3 — asyncio streaming with automatic reconnect and backoff

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def stream_with_backoff(prompt: str, max_attempts: int = 4) -> str:
    accumulated = ""

    for attempt in range(max_attempts):
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for delta in stream.text_stream:
                    accumulated += delta
                    print(delta, end="", flush=True)
            print()
            return accumulated
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            if attempt == max_attempts - 1:
                raise
            wait = min(2 ** attempt, 16)
            print(f"\n[reconnect] {exc.__class__.__name__}, attempt {attempt + 1}/{max_attempts}, wait {wait}s")
            await asyncio.sleep(wait)

    raise RuntimeError("Stream reconnect exhausted")

async def main():
    tasks = [
        "Explain neural networks in detail.",
        "Describe the history of machine learning.",
        "What is reinforcement learning?",
    ]
    results = await asyncio.gather(
        *[stream_with_backoff(t) for t in tasks],
        return_exceptions=True,
    )
    for task, result in zip(tasks, results):
        if isinstance(result, Exception):
            print(f"[error] {task!r}: {result}")
        else:
            print(f"[ok] {task!r}: {len(result)} chars")

asyncio.run(main())
```

**Expected Token Savings:** Multiple streams run concurrently; reconnect logic applies to each independently so a single drop doesn't stall the whole batch.
**Environment:** Async agents processing multiple streams in parallel; API gateways handling concurrent user requests.

---

### Option 4 — Stream collector class with event hooks and reconnect

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class StreamCollector:
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1024
    max_retries: int = 3
    on_delta: Callable[[str], None] = lambda d: print(d, end="", flush=True)
    on_reconnect: Callable[[int, Exception], None] = lambda attempt, exc: print(
        f"\n[reconnect] attempt {attempt}: {exc}"
    )
    _buffer: str = field(default="", init=False)

    def stream(self, prompt: str) -> str:
        self._buffer = ""
        for attempt in range(self.max_retries + 1):
            try:
                with client.messages.stream(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ) as s:
                    for delta in s.text_stream:
                        self._buffer += delta
                        self.on_delta(delta)
                return self._buffer
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                if attempt == self.max_retries:
                    raise
                self.on_reconnect(attempt + 1, exc)
                time.sleep(2 ** attempt)
        return self._buffer

collector = StreamCollector(
    model="claude-haiku-4-5-20251001",
    max_tokens=512,
    on_reconnect=lambda a, e: print(f"\n[alert] stream drop #{a}: {e}"),
)
text = collector.stream("Summarise the key ideas in deep learning.")
print(f"\n[done] received {len(text)} chars")
```

**Expected Token Savings:** Encapsulated retry logic reusable across agent components; on_reconnect hook enables alerting or metrics without coupling retry logic to business code.
**Environment:** Production agents with observability requirements; teams wanting a drop-in streaming wrapper.

---

### Option 5 — Server-Sent Events (SSE) reconnect simulation with last-event tracking

```python
import anthropic
import time

client = anthropic.Anthropic()

class SSEStreamReconnector:
    """Tracks stream position (char offset) so partial output can be reconciled."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 1024):
        self.model = model
        self.max_tokens = max_tokens
        self._total_received = 0
        self._full_text = ""

    def stream(self, prompt: str, max_retries: int = 3) -> str:
        for attempt in range(max_retries + 1):
            fresh = ""
            try:
                with client.messages.stream(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    for delta in stream.text_stream:
                        fresh += delta
                        self._total_received += len(delta)
                        print(delta, end="", flush=True)
                self._full_text = fresh
                return self._full_text
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                if attempt == max_retries:
                    print(f"\n[fail] exhausted {max_retries} retries after {self._total_received} chars")
                    raise
                wait = 2 ** attempt
                print(
                    f"\n[sse-reconnect] offset={self._total_received}, attempt {attempt+1}/{max_retries}, "
                    f"wait={wait}s — {exc.__class__.__name__}"
                )
                time.sleep(wait)
        return self._full_text

reconnector = SSEStreamReconnector(max_tokens=768)
output = reconnector.stream("Explain attention mechanisms in transformers.")
print(f"\n[done] total chars across all attempts: {reconnector._total_received}")
```

**Expected Token Savings:** Logs cumulative bytes received across retries, making it easy to detect runaway reconnect loops that waste tokens.
**Environment:** Agents streaming to a frontend UI; SSE-compatible setups where offset tracking is meaningful for the client.

---

### Option 6 — Circuit-breaker-aware stream: halt retries on sustained outage

```python
import anthropic
import time
from enum import Enum

client = anthropic.Anthropic()

class CBState(Enum):
    CLOSED = "closed"      # normal — requests pass through
    OPEN   = "open"        # failing — requests rejected immediately
    HALF   = "half-open"   # probe — one request allowed to test

class StreamCircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self._failures = 0
        self._state    = CBState.CLOSED
        self._opened_at: float = 0.0

    def _trip(self):
        self._state    = CBState.OPEN
        self._opened_at = time.monotonic()
        print(f"[circuit] OPEN — too many stream failures ({self._failures})")

    def _reset(self):
        self._failures = 0
        self._state    = CBState.CLOSED
        print("[circuit] CLOSED — stream healthy")

    def stream(self, prompt: str, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 512) -> str:
        if self._state == CBState.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed < self.recovery_timeout:
                raise RuntimeError(
                    f"[circuit] OPEN — {self.recovery_timeout - elapsed:.0f}s until probe"
                )
            self._state = CBState.HALF
            print("[circuit] HALF-OPEN — probing")

        accumulated = ""
        try:
            with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for delta in stream.text_stream:
                    accumulated += delta
                    print(delta, end="", flush=True)
            print()
            self._reset()
            return accumulated
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            self._failures += 1
            print(f"\n[circuit] failure #{self._failures}: {exc}")
            if self._failures >= self.failure_threshold:
                self._trip()
            raise

cb = StreamCircuitBreaker(failure_threshold=3, recovery_timeout=20.0)
for prompt in ["Explain backpropagation.", "Describe LSTM networks.", "What is dropout?"]:
    try:
        text = cb.stream(prompt)
        print(f"[ok] {len(text)} chars\n")
    except RuntimeError as e:
        print(f"[blocked] {e}\n")
    time.sleep(0.5)
```

**Expected Token Savings:** During a sustained outage the circuit breaker rejects retries immediately rather than burning tokens on guaranteed-to-fail API calls.
**Environment:** High-traffic production agents; multi-tenant systems where one bad upstream should not exhaust the retry budget for all users.

---

## Comparison

| Option | Reconnect Strategy | State Preserved | Async Safe | Best For |
|---|---|---|---|---|
| 1. Buffer + retry | Re-stream from scratch | Buffer in memory | No | Simple baseline; single-stream agents |
| 2. Disk checkpoint | Atomic file write | Full text to disk | No | Long documents; cron/batch jobs |
| 3. asyncio backoff | Per-stream independent | Buffer in memory | Yes | Parallel stream workloads |
| 4. Collector class | Pluggable hooks | Buffer in memory | No | Reusable wrapper with observability |
| 5. SSE offset tracking | Byte offset logged | Buffer in memory | No | Frontend streaming; debugging reconnect waste |
| 6. Circuit breaker | Fail-fast on outage | None (rejected) | No | High-traffic; protect retry budget during outages |
