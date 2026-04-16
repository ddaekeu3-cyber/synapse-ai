---
layout: solution
title: "Agent Doesn't Implement Log Sampling Strategy"
description: "How to selectively sample agent logs to control storage costs and I/O overhead while preserving full visibility for errors, slow requests, and anomalies."
tags: [observability, logging, sampling, performance, cost, structured-logging]
difficulty: intermediate
solution_count: 6
---

## Problem

Agents emit a log line for every token streamed, every tool call argument, and every intermediate reasoning step. At scale this produces gigabytes of logs per hour, most of which are boring success cases. Ingesting everything is expensive, but sampling naively (e.g., "log 1 in 100") means errors — which occur rarely — get dropped too. The result is either crushing cost or blind spots on failures.

```python
# Bad: log everything unconditionally
async def call_tool(name: str, args: dict) -> Any:
    logger.debug(f"Calling tool {name} with args {args}")  # fires for every call
    result = await tools[name](**args)
    logger.debug(f"Tool {name} returned {result}")          # every result too
    return result
# At 10k tool calls/min, this is 20k log writes/min — most of them irrelevant
```

---

## Solution 1 — Head-Based Sampling by Request

Decide at the start of each request whether to log it fully. Use a configurable sampling rate so the decision is consistent for the entire request trace.

```python
import contextvars
import random
import logging
import uuid
import time
from dataclasses import dataclass

# Per-request sampling decision stored in ContextVar
sample_this_request: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sample_this_request", default=False
)
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

class SampledLogger:
    def __init__(self, name: str, sample_rate: float = 0.1):
        self._logger = logging.getLogger(name)
        self._sample_rate = sample_rate

    def _should_log(self, level: str) -> bool:
        # Always log errors and warnings regardless of sampling
        if level in ("ERROR", "WARNING", "CRITICAL"):
            return True
        return sample_this_request.get(False)

    def debug(self, msg: str, **extra) -> None:
        if self._should_log("DEBUG"):
            self._logger.debug(msg, extra={"cid": correlation_id.get(""), **extra})

    def info(self, msg: str, **extra) -> None:
        if self._should_log("INFO"):
            self._logger.info(msg, extra={"cid": correlation_id.get(""), **extra})

    def warning(self, msg: str, **extra) -> None:
        # Always logged
        self._logger.warning(msg, extra={"cid": correlation_id.get(""), **extra})

    def error(self, msg: str, **extra) -> None:
        # Always logged
        self._logger.error(msg, extra={"cid": correlation_id.get(""), **extra})

logger = SampledLogger("agent", sample_rate=0.1)

async def handle_request(message: str) -> str:
    cid = str(uuid.uuid4())
    # Decide sampling at request entry — 10% of requests get full logs
    sampled = random.random() < 0.1
    tok_cid = correlation_id.set(cid)
    tok_smp = sample_this_request.set(sampled)
    try:
        logger.info("Request started", message_len=len(message))  # only in 10%
        result = await run_agent(message)
        logger.info("Request complete")  # only in 10%
        return result
    except Exception as e:
        logger.error(f"Request failed: {e}")  # always logged
        raise
    finally:
        correlation_id.reset(tok_cid)
        sample_this_request.reset(tok_smp)

async def run_agent(message: str) -> str:
    logger.debug("Calling LLM")   # only in sampled requests
    await call_tool("search", {"q": message})
    return "done"

async def call_tool(name: str, args: dict) -> None:
    logger.debug(f"Tool {name} invoked", args=str(args)[:100])  # only in sampled
    pass
```

---

## Solution 2 — Tail-Based Sampling: Retroactively Log Failed Requests

Buffer log entries in memory during request processing. Flush to the log sink only if the request fails, is slow, or is explicitly flagged. Discard buffers for clean, fast requests.

```python
import asyncio
import time
import uuid
import contextvars
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class BufferedEntry:
    ts: float
    level: str
    message: str
    metadata: dict

# Per-request log buffer stored in ContextVar
request_buffer: contextvars.ContextVar[list[BufferedEntry]] = contextvars.ContextVar(
    "request_buffer", default=None
)

class TailSampledLogger:
    def __init__(self, sink_path: str, slow_threshold_ms: float = 2000.0):
        self._sink = Path(sink_path)
        self._slow_ms = slow_threshold_ms

    def _emit(self, level: str, message: str, **meta) -> None:
        buf = request_buffer.get(None)
        entry = BufferedEntry(time.time(), level, message, meta)
        if buf is not None:
            buf.append(entry)
        elif level in ("ERROR", "WARNING"):
            # Outside a request context — write directly
            self._write([entry])

    def debug(self, msg: str, **meta): self._emit("DEBUG", msg, **meta)
    def info(self, msg: str, **meta): self._emit("INFO", msg, **meta)
    def warning(self, msg: str, **meta): self._emit("WARNING", msg, **meta)
    def error(self, msg: str, **meta): self._emit("ERROR", msg, **meta)

    def _write(self, entries: list[BufferedEntry]) -> None:
        with open(self._sink, "a") as f:
            for e in entries:
                f.write(json.dumps({
                    "ts": e.ts, "level": e.level,
                    "msg": e.message, **e.metadata
                }) + "\n")

    def flush_if_needed(self, buf: list[BufferedEntry],
                        elapsed_ms: float, had_error: bool) -> None:
        if not buf:
            return
        should_flush = (
            had_error
            or elapsed_ms > self._slow_ms
            or any(e.level in ("ERROR", "WARNING") for e in buf)
        )
        if should_flush:
            self._write(buf)
        # else: discard — clean fast request, no need to store logs

logger = TailSampledLogger("/var/log/agent/requests.jsonl", slow_threshold_ms=2000.0)

async def handle_request(message: str) -> str:
    buf: list[BufferedEntry] = []
    token = request_buffer.set(buf)
    t0 = time.monotonic()
    had_error = False
    try:
        logger.info("Request started")
        result = await run_pipeline(message)
        logger.info("Request complete", output_len=len(result))
        return result
    except Exception as e:
        had_error = True
        logger.error(f"Request failed: {e}")
        raise
    finally:
        elapsed_ms = (time.monotonic() - t0) * 1000
        request_buffer.reset(token)
        logger.flush_if_needed(buf, elapsed_ms, had_error)

async def run_pipeline(message: str) -> str:
    logger.debug("Pipeline started")
    await asyncio.sleep(0.05)  # simulate LLM call
    logger.debug("LLM responded")
    return f"Answer to: {message}"
```

---

## Solution 3 — Adaptive Rate-Based Sampling (Token Bucket)

Limit the log write rate using a token bucket. When the system is healthy and high-throughput, only a fraction of INFO/DEBUG lines are written. Under errors or anomalies, the bucket is replenished faster.

```python
import time
import threading
import logging
from typing import Optional

class TokenBucketSampler:
    """Allows up to `rate` log writes per second for a given level."""

    def __init__(self, rate: float, capacity: float):
        self._rate = rate        # tokens added per second
        self._capacity = capacity
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

class AdaptiveSampledLogger:
    def __init__(self, name: str):
        self._logger = logging.getLogger(name)
        # Different buckets per level
        self._buckets = {
            "DEBUG": TokenBucketSampler(rate=10.0, capacity=20.0),  # 10/sec
            "INFO":  TokenBucketSampler(rate=50.0, capacity=100.0), # 50/sec
        }
        self._dropped = {"DEBUG": 0, "INFO": 0}
        self._last_report = time.monotonic()

    def _log(self, level: str, msg: str, *args, **kwargs) -> None:
        bucket = self._buckets.get(level)
        if bucket is None or bucket.allow():
            getattr(self._logger, level.lower())(msg, *args, **kwargs)
        else:
            self._dropped[level] = self._dropped.get(level, 0) + 1

        # Periodically report drop statistics
        if time.monotonic() - self._last_report > 60:
            self._logger.info(
                "Log sampling stats",
                extra={"dropped_debug": self._dropped.get("DEBUG", 0),
                       "dropped_info": self._dropped.get("INFO", 0)}
            )
            self._dropped = {k: 0 for k in self._dropped}
            self._last_report = time.monotonic()

    def debug(self, msg: str, *args, **kwargs): self._log("DEBUG", msg, *args, **kwargs)
    def info(self, msg: str, *args, **kwargs): self._log("INFO", msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        # Warnings always logged — replenish DEBUG bucket as anomaly signal
        self._buckets["DEBUG"]._tokens = self._buckets["DEBUG"]._capacity
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs):
        # Errors always logged — also fully open all buckets for context
        for bucket in self._buckets.values():
            bucket._tokens = bucket._capacity
        self._logger.error(msg, *args, **kwargs)

logger = AdaptiveSampledLogger("agent")
```

---

## Solution 4 — Structured Sampling with Field-Level Redaction

Sample at configurable rates per log category (tool calls, LLM responses, user events) and strip expensive fields (full prompts, raw API responses) from sampled records.

```python
import random
import json
import time
import logging
from dataclasses import dataclass
from typing import Any, Callable

@dataclass
class LogCategory:
    name: str
    sample_rate: float          # fraction to keep
    max_field_length: int       # truncate long string fields
    strip_fields: set[str]      # fields to omit entirely from sampled records

CATEGORIES = {
    "tool_call":      LogCategory("tool_call",      0.05, 500,  {"full_args", "raw_response"}),
    "llm_response":   LogCategory("llm_response",   0.02, 300,  {"full_prompt", "system_prompt"}),
    "user_event":     LogCategory("user_event",      0.20, 200,  set()),
    "health_check":   LogCategory("health_check",    0.001, 100, {"stack_trace"}),
    "error":          LogCategory("error",           1.0,  5000, set()),  # always
}

class CategorizedSampledLogger:
    def __init__(self, sink_fn: Callable[[dict], None]):
        self._sink = sink_fn
        self._base = logging.getLogger("agent")

    def log(self, category: str, level: str, message: str, **fields) -> None:
        cat = CATEGORIES.get(category, CATEGORIES["error"])

        if level in ("ERROR", "CRITICAL") or random.random() < cat.sample_rate:
            # Apply field redaction and truncation
            cleaned = {}
            for k, v in fields.items():
                if k in cat.strip_fields:
                    continue
                if isinstance(v, str) and len(v) > cat.max_field_length:
                    cleaned[k] = v[:cat.max_field_length] + "…"
                else:
                    cleaned[k] = v

            entry = {
                "ts": time.time(),
                "category": category,
                "level": level,
                "msg": message,
                "sampled": True,
                **cleaned,
            }
            self._sink(entry)

def stdout_sink(entry: dict) -> None:
    print(json.dumps(entry))

logger = CategorizedSampledLogger(sink_fn=stdout_sink)

async def call_tool(name: str, args: dict) -> Any:
    logger.log("tool_call", "INFO", f"Tool invoked: {name}",
               tool=name,
               full_args=json.dumps(args),    # stripped in 95% of samples
               arg_count=len(args))
    result = {"data": "..."}
    logger.log("tool_call", "INFO", f"Tool returned: {name}",
               tool=name,
               raw_response=str(result),       # stripped
               result_size=len(str(result)))
    return result

async def handle_llm_response(prompt: str, response: str) -> None:
    logger.log("llm_response", "INFO", "LLM call complete",
               full_prompt=prompt,             # stripped in 98% of samples
               system_prompt="...",            # always stripped
               response_len=len(response),
               first_50=response[:50])

async def on_error(exc: Exception, context: dict) -> None:
    logger.log("error", "ERROR", str(exc),    # always logged, full context
               **context)
```

---

## Solution 5 — Probabilistic Consistent Sampling (Same Session Always Sampled Together)

Use a consistent hash of the session ID so all log entries for a given session are either all sampled or all dropped — ensuring complete traces rather than random fragments.

```python
import hashlib
import contextvars
import json
import time
import logging
from typing import Any

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

def consistent_sample(session_id: str, rate: float) -> bool:
    """Hash-based: same session_id always gives same True/False for given rate."""
    h = int(hashlib.sha256(session_id.encode()).hexdigest(), 16)
    threshold = int(rate * (2**256))
    return h < threshold

class ConsistentSampledLogger:
    """All logs from the same session are either all kept or all dropped."""

    def __init__(self, name: str, sample_rate: float = 0.1,
                 sink_fn=None):
        self._name = name
        self._rate = sample_rate
        self._sink = sink_fn or (lambda e: print(json.dumps(e)))
        self._session_cache: dict[str, bool] = {}

    def _is_sampled(self, session_id: str) -> bool:
        if session_id not in self._session_cache:
            self._session_cache[session_id] = consistent_sample(session_id, self._rate)
            # Evict old entries to prevent unbounded growth
            if len(self._session_cache) > 10000:
                oldest_keys = list(self._session_cache.keys())[:1000]
                for k in oldest_keys:
                    del self._session_cache[k]
        return self._session_cache[session_id]

    def log(self, level: str, message: str, **fields) -> None:
        cid = correlation_id.get("")
        always_log = level in ("ERROR", "WARNING", "CRITICAL")

        if always_log or self._is_sampled(cid):
            entry = {
                "ts": time.time(),
                "logger": self._name,
                "level": level,
                "cid": cid,
                "msg": message,
                "sampled": not always_log,
                **fields,
            }
            self._sink(entry)

    def debug(self, msg: str, **f): self.log("DEBUG", msg, **f)
    def info(self, msg: str, **f): self.log("INFO", msg, **f)
    def warning(self, msg: str, **f): self.log("WARNING", msg, **f)
    def error(self, msg: str, **f): self.log("ERROR", msg, **f)

logger = ConsistentSampledLogger("agent", sample_rate=0.1)

# If session "abc" is sampled, ALL its log lines appear in the sink
# If not sampled, only ERROR/WARNING lines appear — no partial traces
```

---

## Solution 6 — Dynamic Sampling Rate Driven by Error Budget

Link the sampling rate to the current error budget. When the error rate is high (budget burning fast), sample more aggressively to capture context. When healthy, sample less to save cost.

```python
import time
import threading
import math
from dataclasses import dataclass
from collections import deque
import logging

@dataclass
class ErrorBudgetSampler:
    """
    Sample rate adapts based on recent error rate:
    - Low errors  -> low sample rate (cheap)
    - High errors -> high sample rate (detailed visibility)
    """
    base_rate: float = 0.05       # 5% when healthy
    max_rate: float = 1.0         # 100% when error budget burning fast
    window_seconds: float = 60.0  # rolling window
    target_error_rate: float = 0.01  # SLO: 1% errors allowed

    def __post_init__(self):
        self._lock = threading.Lock()
        self._events: deque[tuple[float, bool]] = deque()  # (ts, is_error)

    def record(self, is_error: bool) -> None:
        with self._lock:
            now = time.monotonic()
            self._events.append((now, is_error))
            cutoff = now - self.window_seconds
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

    @property
    def current_rate(self) -> float:
        with self._lock:
            if len(self._events) < 10:
                return self.base_rate  # not enough data

            error_count = sum(1 for _, e in self._events if e)
            error_rate = error_count / len(self._events)

            # Scale sampling rate proportionally to how far over budget we are
            budget_burn = error_rate / max(self.target_error_rate, 1e-9)
            # budget_burn=1.0 → base_rate; budget_burn=10.0 → max_rate
            scaled = self.base_rate * budget_burn
            return min(scaled, self.max_rate)

    def allow(self) -> bool:
        import random
        return random.random() < self.current_rate

class BudgetDrivenLogger:
    def __init__(self, name: str):
        self._logger = logging.getLogger(name)
        self._sampler = ErrorBudgetSampler()

    def info(self, msg: str, **extra) -> None:
        self._sampler.record(is_error=False)
        if self._sampler.allow():
            self._logger.info(msg, extra={**extra, "sample_rate": f"{self._sampler.current_rate:.2%}"})

    def error(self, msg: str, **extra) -> None:
        self._sampler.record(is_error=True)
        # Always log errors; they also increase future sampling rate
        self._logger.error(msg, extra={**extra, "sample_rate": f"{self._sampler.current_rate:.2%}"})

    def warning(self, msg: str, **extra) -> None:
        self._sampler.record(is_error=False)
        self._logger.warning(msg, extra=extra)

logger = BudgetDrivenLogger("agent")

# Under normal load: ~5% of INFO logs written
# During an error spike: up to 100% written — maximum visibility when you need it most
```

---

## Comparison

| Approach | Always Logs Errors | Complete Traces | Adaptive | Storage Cost | Best For |
|---|---|---|---|---|---|
| Head-based (per-request) | **Yes** | **Yes** (whole request) | No | Low | General log volume reduction |
| Tail-based (flush on failure) | **Yes** | **Yes** (whole request) | No | **Lowest** | Debug-on-failure workflows |
| Token bucket (rate-limited) | **Yes** | No (drops midstream) | Partial | Low | High-throughput tool call logging |
| Categorical + field redaction | **Yes** | No | No | Low | Cost-sensitive structured logs |
| Consistent hash (per-session) | **Yes** | **Yes** (by session) | No | Low | Distributed trace completeness |
| Error-budget driven | **Yes** | No | **Yes** | **Variable** | SLO-aware observability |
