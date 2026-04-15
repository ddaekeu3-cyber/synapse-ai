---
layout: solution
title: "Agent Retries Indefinitely Without a Circuit Breaker"
category: loop-stuck
description: "Agent keeps retrying a failing service forever, exhausting API quota and blocking the task queue instead of failing fast."
tags: [loop-stuck, circuit-breaker, resilience, retry, fault-tolerance]
---

## Symptom

A downstream service (database, external API, vector store) starts returning errors. Your agent retries in a loop, spending tokens on retry messages and burning API quota. The loop runs until the process is killed or the token limit is hit. Other tasks queued behind this one never execute.

## Root Cause

Retry logic without a circuit breaker treats every failure as transient. But some failures are systemic — the service is down, the credentials are invalid, the quota is exhausted. Continuing to retry a broken service wastes resources and delays detection of the real problem. A circuit breaker tracks the failure rate and "opens" (stops retrying) once the error rate crosses a threshold, then periodically allows a single probe to test recovery.

## Fix

### Option 1 — Simple in-memory circuit breaker

```python
import time
import anthropic

client = anthropic.Anthropic()

class CircuitBreaker:
    CLOSED   = "closed"    # normal — requests pass through
    OPEN     = "open"      # failing — requests rejected immediately
    HALF_OPEN = "half_open"  # testing — one probe allowed

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.failure_threshold  = failure_threshold
        self.recovery_timeout   = recovery_timeout
        self._state             = self.CLOSED
        self._failure_count     = 0
        self._opened_at: float  = 0.0

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                self._state = self.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = self.CLOSED

    def record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._state    = self.OPEN
            self._opened_at = time.monotonic()
            print(f"[circuit] OPENED after {self._failure_count} failures")

    def allow_request(self) -> bool:
        s = self.state
        if s == self.CLOSED:
            return True
        if s == self.HALF_OPEN:
            return True   # allow one probe
        return False      # OPEN — reject


breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)

def call_external_service(query: str) -> str:
    """Simulated external service that might fail."""
    import random
    if random.random() < 0.6:
        raise ConnectionError("service unavailable")
    return f"result for: {query}"

def agent_step(query: str) -> str:
    if not breaker.allow_request():
        raise RuntimeError("[circuit] OPEN — skipping external call, failing fast")

    try:
        result = call_external_service(query)
        breaker.record_success()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Summarise: {result}"}],
        )
        return response.content[0].text
    except ConnectionError as e:
        breaker.record_failure()
        raise RuntimeError(f"external service failed: {e}") from e

for i in range(10):
    try:
        print(agent_step(f"query-{i}"))
    except RuntimeError as e:
        print(f"step {i}: {e}")
```

**Expected Token Savings:** Prevents dozens of wasted `messages.create()` calls during an outage; circuit opens after N failures and stays open until the service recovers.
**Environment:** Any agent that calls an external service (database, API, vector store) inside its tool loop.

---

### Option 2 — Circuit breaker with failure rate (sliding window)

```python
import time
import collections
import anthropic

client = anthropic.Anthropic()

class SlidingWindowBreaker:
    """Opens when failure rate exceeds threshold over a rolling time window."""

    def __init__(
        self,
        window_seconds: float = 60.0,
        failure_rate_threshold: float = 0.5,  # 50% failures → open
        min_calls: int = 4,                    # need at least N calls to evaluate
        recovery_timeout: float = 30.0,
    ):
        self.window_seconds       = window_seconds
        self.failure_rate_threshold = failure_rate_threshold
        self.min_calls            = min_calls
        self.recovery_timeout     = recovery_timeout
        self._calls: collections.deque = collections.deque()
        self._open_until: float   = 0.0

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.window_seconds
        while self._calls and self._calls[0][0] < cutoff:
            self._calls.popleft()

    def allow_request(self) -> bool:
        if time.monotonic() < self._open_until:
            return False
        return True

    def record(self, success: bool) -> None:
        self._prune()
        self._calls.append((time.monotonic(), success))

        total    = len(self._calls)
        failures = sum(1 for _, ok in self._calls if not ok)

        if total >= self.min_calls and failures / total >= self.failure_rate_threshold:
            self._open_until = time.monotonic() + self.recovery_timeout
            print(f"[circuit] OPENED — failure rate={failures/total:.0%} over {total} calls")

breaker = SlidingWindowBreaker()

def call_with_breaker(prompt: str) -> str:
    import random
    if not breaker.allow_request():
        raise RuntimeError("[circuit] open — request rejected")

    try:
        if random.random() < 0.6:
            raise ConnectionError("downstream error")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        breaker.record(True)
        return response.content[0].text
    except ConnectionError as e:
        breaker.record(False)
        raise RuntimeError(str(e)) from e

for i in range(15):
    try:
        print(f"[{i}] {call_with_breaker('Hello')[:60]}")
    except RuntimeError as e:
        print(f"[{i}] ERROR: {e}")
    time.sleep(0.1)
```

**Expected Token Savings:** Stops API calls as soon as failure rate crosses threshold; more responsive than counting consecutive failures.
**Environment:** Services with intermittent failures rather than total outages; sliding window is more accurate than a simple counter.

---

### Option 3 — Async circuit breaker with asyncio.Lock

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

class AsyncCircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self._failures   = 0
        self._open_until = 0.0
        self._lock       = asyncio.Lock()

    async def allow_request(self) -> bool:
        async with self._lock:
            if time.monotonic() < self._open_until:
                return False
            return True

    async def record_success(self) -> None:
        async with self._lock:
            self._failures   = 0
            self._open_until = 0.0

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._open_until = time.monotonic() + self.recovery_timeout
                print(f"[circuit] OPENED — {self._failures} consecutive failures")


breaker = AsyncCircuitBreaker(failure_threshold=3, recovery_timeout=15.0)

async def safe_ask(prompt: str) -> str:
    if not await breaker.allow_request():
        raise RuntimeError("[circuit] open — fast failing")

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        await breaker.record_success()
        return response.content[0].text
    except anthropic.APIError as e:
        await breaker.record_failure()
        raise RuntimeError(f"API error: {e}") from e

async def main():
    tasks = [safe_ask(f"Tell me about topic {i}") for i in range(10)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"[{i}] FAILED: {r}")
        else:
            print(f"[{i}] OK: {r[:60]}")

asyncio.run(main())
```

**Expected Token Savings:** All concurrent tasks behind the open circuit fail immediately without hitting the API.
**Environment:** Async agents making parallel calls; lock ensures the breaker state is consistent across coroutines.

---

### Option 4 — Circuit breaker integrated into the tool-call loop

```python
import json
import time
import random
import anthropic

client = anthropic.Anthropic()

class ToolCircuitBreaker:
    def __init__(self, per_tool_threshold: int = 3, recovery_timeout: float = 60.0):
        self._failures: dict[str, int]   = {}
        self._open_until: dict[str, float] = {}
        self.threshold       = per_tool_threshold
        self.recovery_timeout = recovery_timeout

    def allow(self, tool_name: str) -> bool:
        until = self._open_until.get(tool_name, 0.0)
        if time.monotonic() < until:
            return False
        return True

    def success(self, tool_name: str) -> None:
        self._failures[tool_name] = 0

    def failure(self, tool_name: str) -> None:
        count = self._failures.get(tool_name, 0) + 1
        self._failures[tool_name] = count
        if count >= self.threshold:
            self._open_until[tool_name] = time.monotonic() + self.recovery_timeout
            print(f"[circuit] tool={tool_name!r} OPENED")


breaker = ToolCircuitBreaker(per_tool_threshold=2)

TOOLS = [
    {
        "name": "search_database",
        "description": "Search the product database.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]

def execute_tool(name: str, input_data: dict) -> str:
    if not breaker.allow(name):
        return json.dumps({"error": f"circuit open for {name!r} — service unavailable"})

    try:
        if random.random() < 0.7:
            raise ConnectionError("database unreachable")
        result = {"results": [f"product-{i}" for i in range(3)]}
        breaker.success(name)
        return json.dumps(result)
    except ConnectionError as e:
        breaker.failure(name)
        return json.dumps({"error": str(e)})

def run_agent(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]
    for _ in range(5):  # max 5 agentic steps
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

    return "max steps reached"

print(run_agent("Find me a laptop under $1000."))
```

**Expected Token Savings:** Opens circuit after 2 tool failures, stopping the agentic loop from spinning on a broken tool for dozens of steps.
**Environment:** Tool-using agents where a single tool failure can cause infinite retry loops.

---

### Option 5 — Circuit breaker state persisted to disk (survives restarts)

```python
import json
import time
import os
import anthropic

client = anthropic.Anthropic()

STATE_FILE = "/tmp/circuit_breaker_state.json"

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)

def is_open(service: str) -> bool:
    state = load_state()
    entry = state.get(service, {})
    open_until = entry.get("open_until", 0.0)
    return time.time() < open_until

def record_failure(service: str, threshold: int = 5, timeout: float = 120.0) -> None:
    state = load_state()
    entry = state.setdefault(service, {"failures": 0, "open_until": 0.0})
    entry["failures"] = entry.get("failures", 0) + 1
    if entry["failures"] >= threshold:
        entry["open_until"] = time.time() + timeout
        print(f"[circuit] {service!r} OPENED — will retry after {timeout:.0f}s")
    save_state(state)

def record_success(service: str) -> None:
    state = load_state()
    state[service] = {"failures": 0, "open_until": 0.0}
    save_state(state)

def call_service(service: str, prompt: str) -> str:
    if is_open(service):
        raise RuntimeError(f"[circuit] {service!r} open — fast fail")

    import random
    try:
        if random.random() < 0.5:
            raise ConnectionError(f"{service} down")
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        record_success(service)
        return response.content[0].text
    except ConnectionError as e:
        record_failure(service)
        raise RuntimeError(str(e)) from e

for i in range(12):
    try:
        print(f"[{i}] {call_service('vector-db', 'Explain concept X.')[:60]}")
    except RuntimeError as e:
        print(f"[{i}] {e}")
```

**Expected Token Savings:** Circuit state survives process restarts; a crashed agent won't re-hammer a service that was already marked open.
**Environment:** Long-running agents managed by a process supervisor (systemd, Docker restart policy).

---

### Option 6 — Bulkhead pattern: isolate failing services to a thread pool

```python
import concurrent.futures
import time
import random
import anthropic

client = anthropic.Anthropic()

# Each external service gets its own bounded thread pool (bulkhead)
# A failing service can exhaust only its own pool, not the shared executor
BULKHEADS: dict[str, concurrent.futures.ThreadPoolExecutor] = {
    "database":   concurrent.futures.ThreadPoolExecutor(max_workers=3),
    "vector-db":  concurrent.futures.ThreadPoolExecutor(max_workers=2),
    "cache":      concurrent.futures.ThreadPoolExecutor(max_workers=2),
}

def _call_service_sync(service: str, query: str) -> str:
    time.sleep(random.uniform(0.01, 0.05))
    if random.random() < 0.4:
        raise ConnectionError(f"{service} unavailable")
    return f"result from {service}: {query}"

def call_in_bulkhead(service: str, query: str, timeout: float = 2.0) -> str:
    executor = BULKHEADS.get(service)
    if executor is None:
        raise ValueError(f"No bulkhead for service {service!r}")

    future = executor.submit(_call_service_sync, service, query)
    try:
        result = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise RuntimeError(f"[bulkhead] {service!r} timed out after {timeout}s")
    except ConnectionError as e:
        raise RuntimeError(f"[bulkhead] {service!r} error: {e}") from e

    # Use result in a Claude call
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarise: {result}"}],
    )
    return response.content[0].text

services = ["database", "vector-db", "cache", "database", "vector-db"]
for svc in services:
    try:
        print(f"[{svc}] {call_in_bulkhead(svc, 'user query')[:80]}")
    except RuntimeError as e:
        print(f"[{svc}] {e}")

for ex in BULKHEADS.values():
    ex.shutdown(wait=False)
```

**Expected Token Savings:** A slow or failing service fills only its own bulkhead; other services and their Claude calls continue unimpeded.
**Environment:** Agents integrating multiple external services where one service's failure should not starve others.

---

## Comparison

| Option | State Storage | Granularity | Async Safe | Restart Safe | Best For |
|---|---|---|---|---|---|
| 1. Simple counter | In-memory | Per-agent | No | No | Simple sequential agents |
| 2. Sliding window | In-memory | Per-agent | No | No | Intermittent failures |
| 3. Async lock | In-memory | Per-service | Yes | No | Concurrent async agents |
| 4. Tool loop | In-memory | Per-tool | No | No | Tool-using agents |
| 5. Disk-persisted | File | Per-service | No | Yes | Supervised long-running agents |
| 6. Bulkhead | In-memory | Per-service pool | Partial | No | Multi-service isolation |
