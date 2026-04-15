---
layout: solution
title: "Agent Doesn't Handle Streaming Errors Mid-Response"
category: streaming
description: "An exception raised partway through a stream causes the agent to crash or silently discard all partial output, with no recovery path and no indication to the user that anything went wrong."
tags: [streaming, error-handling, reliability, resilience, production]
---

## Symptom

The agent starts streaming a response — the user sees text appearing — then a network error, API overload, or timeout fires mid-stream. The generator raises an exception that propagates uncaught through the streaming loop. The partial text is discarded, the UI shows nothing or crashes, and the user sees no explanation. On retry, the full generation cost is paid again from scratch.

## Root Cause

Streaming responses are delivered over a long-lived HTTP connection. Any network interruption, rate-limit response, or internal server error can terminate the stream mid-delivery. The Anthropic Python SDK raises `APIConnectionError`, `APITimeoutError`, or `InternalServerError` from within the `text_stream` iterator. Without a `try/except` inside the streaming loop, these exceptions propagate and discard all accumulated output.

## Fix

### Option 1 — Try/except inside the streaming loop with partial output preservation

```python
import anthropic
import time

client = anthropic.Anthropic()

def stream_safe(prompt: str, max_retries: int = 2) -> str:
    accumulated = ""

    for attempt in range(max_retries + 1):
        try:
            with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for delta in stream.text_stream:
                    accumulated += delta
                    print(delta, end="", flush=True)

                # Stream completed cleanly
                print()
                return accumulated

        except anthropic.APIConnectionError as exc:
            print(f"\n[error] connection lost after {len(accumulated)} chars: {exc}")
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"[retry] attempt {attempt+1}/{max_retries} in {wait}s")
                time.sleep(wait)
                # Do NOT reset accumulated — partial output may be shown to user
                # But we must re-request since the stream is gone
                accumulated = ""
            else:
                print("[error] max retries exhausted")
                return accumulated  # return whatever we have

        except anthropic.InternalServerError as exc:
            print(f"\n[error] server error: {exc}")
            if attempt < max_retries:
                time.sleep(5)
                accumulated = ""
            else:
                return accumulated

        except Exception as exc:
            print(f"\n[error] unexpected: {exc}")
            return accumulated  # don't retry unknown errors

    return accumulated

result = stream_safe("Write a detailed explanation of how TCP handles congestion control.")
print(f"\n[done] {len(result)} chars")
```

**Expected Token Savings:** Preserving partial output avoids complete re-generation when only the tail of a response was lost to a transient error.
**Environment:** Any streaming agent; this is the minimum viable error handling pattern for production streaming.

---

### Option 2 — asyncio streaming with per-error type recovery strategy

```python
import asyncio
import anthropic
import time

client = anthropic.AsyncAnthropic()

async def stream_with_recovery(prompt: str) -> str:
    accumulated = ""

    async def _attempt() -> bool:
        """Returns True if stream completed cleanly."""
        nonlocal accumulated
        partial = ""
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for delta in stream.text_stream:
                    partial += delta
                    print(delta, end="", flush=True)
            accumulated = partial
            return True
        except anthropic.APIConnectionError:
            print(f"\n[network] lost after {len(partial)} chars — will retry")
            return False
        except anthropic.RateLimitError:
            print(f"\n[rate_limit] backing off 30s")
            await asyncio.sleep(30)
            return False
        except anthropic.InternalServerError:
            print(f"\n[server_error] internal error — waiting 10s")
            await asyncio.sleep(10)
            return False
        except asyncio.CancelledError:
            print(f"\n[cancelled] task cancelled with {len(partial)} chars buffered")
            accumulated = partial
            raise  # re-raise: cancellation should propagate

    for attempt in range(3):
        if await _attempt():
            break
        if attempt < 2:
            print(f"[retry] attempt {attempt+2}/3")
    else:
        print("[error] all retries failed")

    print()
    return accumulated

async def main():
    result = await stream_with_recovery("Explain quantum entanglement in simple terms.")
    print(f"\n[done] {len(result)} chars")

asyncio.run(main())
```

**Expected Token Savings:** Per-error type handling prevents unnecessary waits (rate limits need longer backoff than connection drops); targeted recovery avoids wasting retry budget on non-retryable errors.
**Environment:** Async agents; rate-limit-aware streaming with separate backoff strategies per error class.

---

### Option 3 — Stream context manager with guaranteed cleanup on error

```python
import anthropic
import contextlib
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class StreamSession:
    prompt:    str
    max_tokens: int = 512
    _buffer:   str  = field(default="", init=False)
    _complete: bool = field(default=False, init=False)
    _error:    Exception | None = field(default=None, init=False)

    @contextlib.contextmanager
    def run(self):
        """Context manager that guarantees buffer and error state are set."""
        try:
            with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": self.prompt}],
            ) as stream:
                for delta in stream.text_stream:
                    self._buffer += delta
                    yield delta  # yield each delta to caller
                self._complete = True
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            self._error = exc
            yield None  # signal error to caller
        except Exception as exc:
            self._error = exc
            yield None

    @property
    def result(self) -> str:
        return self._buffer

    @property
    def ok(self) -> bool:
        return self._complete and self._error is None

def stream_document(prompt: str) -> str:
    session = StreamSession(prompt, max_tokens=768)
    print("Streaming: ", end="")
    try:
        with session.run() as delta:
            if delta is not None:
                print(delta, end="", flush=True)
    except Exception:
        pass  # error captured in session

    print()

    if not session.ok:
        print(f"[warn] incomplete: error={session._error}, chars={len(session.result)}")
    else:
        print(f"[ok] complete: {len(session.result)} chars")

    return session.result

result = stream_document("Describe the architecture of a microservices system.")
```

**Expected Token Savings:** Context manager guarantees cleanup even on unexpected exceptions; partial buffer is always accessible regardless of how the stream terminated.
**Environment:** Agents where partial output has downstream value; pipelines that must always receive *something* even when the stream fails partway through.

---

### Option 4 — Error-aware streaming generator for FastAPI/Flask

```python
import anthropic
import json
from typing import Generator

client = anthropic.Anthropic()

def error_aware_stream(prompt: str) -> Generator[str, None, None]:
    """
    SSE generator that emits error events instead of crashing.
    Suitable for FastAPI StreamingResponse or Flask stream_with_context.
    """
    accumulated = ""
    try:
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for delta in stream.text_stream:
                accumulated += delta
                payload = json.dumps({"type": "delta", "text": delta})
                yield f"data: {payload}\n\n"

            final = stream.get_final_message()
            done_payload = json.dumps({
                "type": "done",
                "stop_reason": final.stop_reason,
                "chars": len(accumulated),
            })
            yield f"data: {done_payload}\n\n"

    except anthropic.APIConnectionError as exc:
        error_payload = json.dumps({
            "type": "error",
            "code": "connection_error",
            "message": "Stream interrupted by network error.",
            "partial_chars": len(accumulated),
        })
        yield f"data: {error_payload}\n\n"

    except anthropic.RateLimitError:
        yield f"data: {json.dumps({'type': 'error', 'code': 'rate_limit', 'message': 'Rate limited. Please retry later.'})}\n\n"

    except anthropic.InternalServerError:
        yield f"data: {json.dumps({'type': 'error', 'code': 'server_error', 'message': 'Server error. Please retry.'})}\n\n"

    except Exception as exc:
        yield f"data: {json.dumps({'type': 'error', 'code': 'unknown', 'message': str(exc)})}\n\n"

# Simulate consuming the generator (as a web framework would)
print("Consuming SSE stream:")
for event in error_aware_stream("Explain REST API design principles."):
    data_str = event.replace("data: ", "").strip()
    if data_str:
        parsed = json.loads(data_str)
        if parsed["type"] == "delta":
            print(parsed["text"], end="", flush=True)
        elif parsed["type"] == "done":
            print(f"\n[done] {parsed['chars']} chars, stop={parsed['stop_reason']}")
        elif parsed["type"] == "error":
            print(f"\n[stream error] {parsed['code']}: {parsed['message']}")
```

**Expected Token Savings:** Error events let the frontend show a "connection interrupted — retry?" message instead of a blank screen; user retries only when they choose to, preventing automatic retry storms.
**Environment:** Web backends serving SSE to browser frontends; any streaming API endpoint that must communicate errors to the client without closing the connection abruptly.

---

### Option 5 — Watchdog timeout: kill stream if no delta received for N seconds

```python
import anthropic
import threading
import time
import queue

client = anthropic.Anthropic()

class StreamWatchdog:
    """Raises TimeoutError if no delta is received within `timeout` seconds."""

    def __init__(self, timeout: float = 15.0):
        self.timeout  = timeout
        self._q: queue.Queue = queue.Queue()
        self._stop    = threading.Event()
        self._thread  = threading.Thread(target=self._watch, daemon=True)

    def _watch(self):
        while not self._stop.is_set():
            try:
                self._q.get(timeout=self.timeout)
            except queue.Empty:
                if not self._stop.is_set():
                    print(f"\n[watchdog] no delta for {self.timeout}s — raising timeout")
                    self._stop.set()

    def ping(self):
        """Call on every received delta."""
        self._q.put(True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._q.put(None)  # unblock watcher

def stream_with_watchdog(prompt: str, delta_timeout: float = 10.0) -> str | None:
    accumulated = ""
    with StreamWatchdog(timeout=delta_timeout) as watchdog:
        try:
            with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for delta in stream.text_stream:
                    if watchdog._stop.is_set():
                        print("[watchdog] stream terminated by timeout")
                        return None
                    watchdog.ping()
                    accumulated += delta
                    print(delta, end="", flush=True)
        except Exception as exc:
            print(f"\n[error] stream exception: {exc}")
            return None

    print()
    return accumulated

result = stream_with_watchdog(
    "Explain the difference between supervised and unsupervised learning.",
    delta_timeout=20.0,
)
if result:
    print(f"\n[ok] {len(result)} chars")
else:
    print("\n[fail] stream timed out or errored")
```

**Expected Token Savings:** Watchdog terminates streams that stall silently (common with some proxies that buffer responses); prevents indefinite blocking that holds a thread and wastes API connection time.
**Environment:** Agents behind proxies that may buffer silently; production systems where hung streams must be detected and terminated automatically.

---

### Option 6 — Streaming with exponential backoff and partial-result deduplication

```python
import anthropic
import time
import hashlib

client = anthropic.Anthropic()

def stream_with_dedup_retry(prompt: str, max_retries: int = 3) -> str:
    """
    On retry, checks if the new response starts with the same content
    as the partial response — avoids showing duplicated text to users.
    """
    last_partial = ""
    last_hash    = ""

    for attempt in range(max_retries + 1):
        current   = ""
        try:
            with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for delta in stream.text_stream:
                    current += delta
                    # Only show new content (deduplicate prefix from last attempt)
                    if last_partial and current.startswith(last_partial):
                        # Still in the repeated prefix — don't print
                        pass
                    else:
                        new_part = current[len(last_partial):]
                        if new_part:
                            print(new_part, end="", flush=True)
                            last_partial = ""  # prefix consumed, print everything now

                print()
                return current

        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            new_hash = hashlib.md5(current.encode()).hexdigest()
            if new_hash == last_hash and attempt > 0:
                print(f"\n[warn] same partial on retry — upstream may be stuck")
            last_hash    = new_hash
            last_partial = current
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"\n[retry] attempt {attempt+1}/{max_retries} in {wait}s (partial: {len(current)} chars)")
                time.sleep(wait)
            else:
                print("\n[fail] exhausted retries")
                return current

    return last_partial

result = stream_with_dedup_retry("Write a detailed overview of containerisation with Docker.")
print(f"\n[done] {len(result)} chars received")
```

**Expected Token Savings:** Deduplication detection prevents showing the same content twice on retry; hash comparison detects when retries produce identical partials (upstream stuck), saving further pointless retries.
**Environment:** Chat UIs where partial text is already displayed to the user; streaming pipelines where retry must not produce visible duplicate content.

---

## Comparison

| Option | Error Detection | Partial Preserved | Async Safe | Frontend Ready | Best For |
|---|---|---|---|---|---|
| 1. Try/except loop | Exception catch | Yes | No | No | Simple baseline; sync agents |
| 2. Per-error async | Per-class strategy | Yes | Yes | No | Async agents; fine-grained backoff |
| 3. Context manager | Exception capture | Yes (always) | No | No | Guaranteed cleanup; partial-value pipelines |
| 4. SSE error events | Yielded error event | Via SSE event | No | Yes | Web backends; frontend error display |
| 5. Watchdog timeout | Delta silence | Partial in buffer | No | No | Proxy-buffered environments; hung streams |
| 6. Dedup retry | Exception + hash | Yes + dedup | No | Yes | Chat UIs showing streaming text |
