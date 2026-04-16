---
title: "Agent Doesn't Implement Tool Circuit Breaker with Half-Open State"
description: "Apply the circuit breaker pattern to tool calls—automatically stopping calls to failing tools, testing recovery with limited traffic, and resuming full operation when the tool is healthy again."
difficulty: intermediate
category: tool-failure
tags: [tool-failure, circuit-breaker, resilience, fault-tolerance, recovery]
---

## Problem

When an external tool (API, database, service) starts failing, agents continue calling it on every request—amplifying load on an already struggling service, burning retry budgets, and wasting latency on guaranteed failures. The circuit breaker pattern solves this by tracking failures, opening the circuit when failure rate is too high, and automatically probing for recovery with limited traffic.

## Solutions

### Option 1: Basic Three-State Circuit Breaker

Implement the canonical circuit breaker with CLOSED, OPEN, and HALF-OPEN states.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from enum import Enum

client = AsyncAnthropic()

class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, rejecting calls
    HALF_OPEN = "half_open" # Testing recovery

@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5      # Failures before opening
    recovery_timeout: float = 30.0  # Seconds before trying half-open
    half_open_max_calls: int = 3    # Calls allowed in half-open

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    half_open_calls: int = 0

    def can_call(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        elif self.state == CircuitState.OPEN:
            if time.monotonic() - self.last_failure_time >= self.recovery_timeout:
                self._transition(CircuitState.HALF_OPEN)
                return True
            return False
        elif self.state == CircuitState.HALF_OPEN:
            return self.half_open_calls < self.half_open_max_calls
        return False

    def record_success(self):
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.half_open_max_calls:
                self._transition(CircuitState.CLOSED)
        elif self.state == CircuitState.CLOSED:
            self.failure_count = max(0, self.failure_count - 1)

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.monotonic()

        if self.state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN)
        elif self.failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN)

    def _transition(self, new_state: CircuitState):
        old_state = self.state
        self.state = new_state
        self.half_open_calls = 0
        self.success_count = 0

        if new_state != CircuitState.CLOSED:
            self.failure_count = 0

        print(f"[Circuit:{self.name}] {old_state.value} → {new_state.value}")

class CircuitBreakerRegistry:
    _breakers: dict[str, CircuitBreaker] = {}

    @classmethod
    def get(cls, tool_name: str) -> CircuitBreaker:
        if tool_name not in cls._breakers:
            cls._breakers[tool_name] = CircuitBreaker(name=tool_name)
        return cls._breakers[tool_name]

async def protected_tool_call(
    tool_name: str,
    tool_fn,
    *args,
    fallback=None,
    **kwargs
):
    """Call a tool with circuit breaker protection."""
    breaker = CircuitBreakerRegistry.get(tool_name)

    if not breaker.can_call():
        print(f"[Circuit:{tool_name}] OPEN — call rejected")
        if fallback is not None:
            return fallback
        raise RuntimeError(f"Circuit breaker OPEN for {tool_name}")

    try:
        result = await tool_fn(*args, **kwargs)
        breaker.record_success()
        return result
    except Exception as e:
        breaker.record_failure()
        raise

# --- Simulated tools ---
call_count = 0

async def flaky_search_api(query: str) -> str:
    global call_count
    call_count += 1
    # Fail 70% of the time during "outage"
    if 3 <= call_count <= 10:
        raise ConnectionError(f"Search API unavailable (call #{call_count})")
    return f"Results for '{query}': [doc1, doc2, doc3]"

async def demo_circuit_breaker():
    global call_count
    call_count = 0

    # Shorten recovery timeout for demo
    breaker = CircuitBreakerRegistry.get("search_api")
    breaker.failure_threshold = 3
    breaker.recovery_timeout = 2.0  # 2 seconds for demo

    queries = [f"query_{i}" for i in range(15)]

    for query in queries:
        try:
            result = await protected_tool_call(
                "search_api",
                flaky_search_api,
                query,
                fallback="[Cached: no live results available]"
            )
            print(f"✓ {query}: {result[:50]}")
        except RuntimeError as e:
            print(f"✗ {query}: {e}")
        except ConnectionError as e:
            print(f"✗ {query}: {e}")

        await asyncio.sleep(0.3)  # Simulate request interval

asyncio.run(demo_circuit_breaker())
```

### Option 2: Failure-Rate-Based Circuit Breaker

Open the circuit based on failure percentage over a sliding window, not just raw failure count.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class RateBasedCircuitBreaker:
    name: str
    failure_rate_threshold: float = 0.5    # 50% failure rate opens circuit
    min_calls_in_window: int = 5           # Don't open until this many calls
    window_seconds: float = 60.0
    recovery_timeout: float = 30.0
    half_open_probe_count: int = 3

    state: CircuitState = CircuitState.CLOSED
    _call_log: deque = field(default_factory=deque)  # (timestamp, success: bool)
    _last_opened: float = 0.0
    _half_open_results: list[bool] = field(default_factory=list)

    def _trim_window(self):
        cutoff = time.monotonic() - self.window_seconds
        while self._call_log and self._call_log[0][0] < cutoff:
            self._call_log.popleft()

    def _current_failure_rate(self) -> float:
        self._trim_window()
        if len(self._call_log) < self.min_calls_in_window:
            return 0.0
        failures = sum(1 for _, success in self._call_log if not success)
        return failures / len(self._call_log)

    def can_call(self) -> tuple[bool, str]:
        if self.state == CircuitState.CLOSED:
            return True, "closed"
        elif self.state == CircuitState.OPEN:
            if time.monotonic() - self._last_opened >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self._half_open_results = []
                print(f"[Circuit:{self.name}] OPEN → HALF_OPEN (probing)")
                return True, "half_open_probe"
            return False, f"open (retry in {self.recovery_timeout - (time.monotonic() - self._last_opened):.0f}s)"
        elif self.state == CircuitState.HALF_OPEN:
            if len(self._half_open_results) < self.half_open_probe_count:
                return True, "half_open_probe"
            return False, "half_open_saturated"
        return False, "unknown"

    def record(self, success: bool):
        now = time.monotonic()

        if self.state == CircuitState.HALF_OPEN:
            self._half_open_results.append(success)
            if len(self._half_open_results) >= self.half_open_probe_count:
                success_rate = sum(self._half_open_results) / len(self._half_open_results)
                if success_rate >= (1 - self.failure_rate_threshold):
                    self.state = CircuitState.CLOSED
                    self._call_log.clear()
                    print(f"[Circuit:{self.name}] HALF_OPEN → CLOSED (recovered)")
                else:
                    self.state = CircuitState.OPEN
                    self._last_opened = now
                    print(f"[Circuit:{self.name}] HALF_OPEN → OPEN (recovery failed)")
            return

        self._call_log.append((now, success))
        self._trim_window()

        rate = self._current_failure_rate()
        if rate >= self.failure_rate_threshold and self.state == CircuitState.CLOSED:
            self.state = CircuitState.OPEN
            self._last_opened = now
            print(f"[Circuit:{self.name}] CLOSED → OPEN "
                  f"(failure rate: {rate:.0%})")

async def demo_rate_based():
    breaker = RateBasedCircuitBreaker(
        name="payment-api",
        failure_rate_threshold=0.6,
        min_calls_in_window=5,
        window_seconds=10.0,
        recovery_timeout=2.0,
    )

    import random
    for i in range(20):
        allowed, reason = breaker.can_call()

        if not allowed:
            print(f"Call {i:2d}: BLOCKED ({reason})")
        else:
            # Simulate 70% failure rate in calls 5-12
            success = random.random() > (0.7 if 5 <= i <= 12 else 0.1)
            breaker.record(success)
            status = "✓" if success else "✗"
            print(f"Call {i:2d}: {status} [{breaker.state.value}]")

        await asyncio.sleep(0.2)

asyncio.run(demo_rate_based())
```

### Option 3: Per-Tool Circuit Breaker for Agent Tool Loops

Integrate circuit breakers into the agent's tool-use loop with automatic fallback strategies.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from enum import Enum

client = AsyncAnthropic()

class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class SimpleBreaker:
    name: str
    threshold: int = 3
    timeout: float = 10.0
    state: State = State.CLOSED
    failures: int = 0
    opened_at: float = 0.0

    def available(self) -> bool:
        if self.state == State.CLOSED:
            return True
        if self.state == State.OPEN:
            if time.monotonic() - self.opened_at > self.timeout:
                self.state = State.HALF_OPEN
                self.failures = 0
                return True
            return False
        return True  # HALF_OPEN: allow one probe

    def success(self):
        if self.state == State.HALF_OPEN:
            self.state = State.CLOSED
        self.failures = 0

    def fail(self):
        self.failures += 1
        if self.state == State.HALF_OPEN or self.failures >= self.threshold:
            self.state = State.OPEN
            self.opened_at = time.monotonic()

TOOL_BREAKERS: dict[str, SimpleBreaker] = {}

def get_breaker(tool: str) -> SimpleBreaker:
    if tool not in TOOL_BREAKERS:
        TOOL_BREAKERS[tool] = SimpleBreaker(name=tool)
    return TOOL_BREAKERS[tool]

# Simulated tool implementations
_fail_until = {"search": 7, "calculator": 0, "database": 0}
_call_counts: dict[str, int] = {}

async def execute_tool(tool_name: str, tool_input: dict) -> str:
    _call_counts[tool_name] = _call_counts.get(tool_name, 0) + 1
    count = _call_counts[tool_name]

    if count <= _fail_until.get(tool_name, 0):
        raise ConnectionError(f"{tool_name} unavailable (call #{count})")

    if tool_name == "search":
        return f"Search results for: {tool_input.get('query', '')}"
    elif tool_name == "calculator":
        expr = tool_input.get("expression", "0")
        try:
            return str(eval(expr, {"__builtins__": {}}, {}))
        except Exception:
            return "Error evaluating expression"
    return f"Unknown tool: {tool_name}"

async def circuit_protected_tool_call(tool_name: str, tool_input: dict) -> str:
    breaker = get_breaker(tool_name)

    if not breaker.available():
        return f"[Circuit OPEN] {tool_name} is currently unavailable. Skipping."

    try:
        result = await execute_tool(tool_name, tool_input)
        breaker.success()
        return result
    except Exception as e:
        breaker.fail()
        state = breaker.state.value
        return f"[Circuit:{state}] {tool_name} failed: {str(e)}"

async def run_agent_with_circuit_breakers(task: str) -> str:
    tools = [
        {
            "name": "search",
            "description": "Search for information",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        },
        {
            "name": "calculator",
            "description": "Evaluate a mathematical expression",
            "input_schema": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"]
            }
        }
    ]

    messages = [{"role": "user", "content": task}]

    for _ in range(6):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "Done")

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    result = await circuit_protected_tool_call(block.name, block.input)
                    breaker = get_breaker(block.name)
                    print(f"  Tool:{block.name} [{breaker.state.value}] → {result[:60]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})

    return "Task completed with circuit breaker protection."

async def demo_agent_circuit_breakers():
    # search will fail 7 times before recovering
    task = "Search for Python best practices and calculate 15 * 23."
    result = await run_agent_with_circuit_breakers(task)
    print(f"\nFinal result: {result.strip()[:150]}")

    print("\nCircuit breaker states:")
    for name, breaker in TOOL_BREAKERS.items():
        print(f"  {name}: {breaker.state.value} (failures: {breaker.failures})")

asyncio.run(demo_agent_circuit_breakers())
```

### Option 4: Bulkhead + Circuit Breaker Combination

Combine circuit breakers with bulkheads to isolate failure impact across different tool categories.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class BulkheadedCircuitBreaker:
    """Circuit breaker + bulkhead: limits concurrent calls AND breaks on failure."""
    name: str
    max_concurrent: int = 3         # Bulkhead limit
    failure_threshold: int = 3
    recovery_timeout: float = 15.0

    state: State = State.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    current_concurrent: int = 0
    total_rejected_bulkhead: int = 0
    total_rejected_circuit: int = 0

    def _maybe_transition_to_half_open(self) -> bool:
        if self.state == State.OPEN:
            if time.monotonic() - self.opened_at >= self.recovery_timeout:
                self.state = State.HALF_OPEN
                self.failures = 0
                print(f"[{self.name}] OPEN → HALF_OPEN")
                return True
        return False

    async def call(self, fn, *args, **kwargs):
        # Check circuit state
        if self.state == State.OPEN:
            self._maybe_transition_to_half_open()
            if self.state == State.OPEN:
                self.total_rejected_circuit += 1
                raise RuntimeError(f"Circuit OPEN for {self.name}")

        # Check bulkhead
        if self.current_concurrent >= self.max_concurrent:
            self.total_rejected_bulkhead += 1
            raise RuntimeError(f"Bulkhead full for {self.name} ({self.current_concurrent}/{self.max_concurrent})")

        self.current_concurrent += 1
        try:
            result = await fn(*args, **kwargs)

            # Success handling
            if self.state == State.HALF_OPEN:
                self.state = State.CLOSED
                print(f"[{self.name}] HALF_OPEN → CLOSED")
            self.failures = max(0, self.failures - 1)
            return result

        except Exception:
            self.failures += 1
            if self.state == State.HALF_OPEN:
                self.state = State.OPEN
                self.opened_at = time.monotonic()
                print(f"[{self.name}] HALF_OPEN → OPEN (probe failed)")
            elif self.failures >= self.failure_threshold:
                self.state = State.OPEN
                self.opened_at = time.monotonic()
                print(f"[{self.name}] CLOSED → OPEN (threshold reached)")
            raise
        finally:
            self.current_concurrent -= 1

    def stats(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "concurrent": self.current_concurrent,
            "failures": self.failures,
            "rejected_circuit": self.total_rejected_circuit,
            "rejected_bulkhead": self.total_rejected_bulkhead,
        }

async def demo_bulkhead_circuit():
    breaker = BulkheadedCircuitBreaker(
        name="external-api",
        max_concurrent=2,
        failure_threshold=3,
        recovery_timeout=1.0,
    )

    call_num = [0]

    async def flaky_api(req_id: int) -> str:
        call_num[0] += 1
        await asyncio.sleep(0.1)
        if 2 <= call_num[0] <= 5:
            raise ConnectionError(f"API error #{call_num[0]}")
        return f"Response to request {req_id}"

    async def make_request(req_id: int) -> str:
        try:
            return await breaker.call(flaky_api, req_id)
        except RuntimeError as e:
            return f"REJECTED: {e}"
        except ConnectionError as e:
            return f"FAILED: {e}"

    # Concurrent requests
    tasks = [make_request(i) for i in range(10)]
    results = await asyncio.gather(*tasks)

    for i, r in enumerate(results):
        print(f"Request {i:2d}: {r}")

    print(f"\nStats: {breaker.stats()}")

asyncio.run(demo_bulkhead_circuit())
```

### Option 5: Adaptive Recovery Timeout

Extend recovery timeout after repeated failures to implement exponential backoff at the circuit level.

```python
import asyncio
import time
import math
from dataclasses import dataclass, field
from enum import Enum

class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class AdaptiveCircuitBreaker:
    name: str
    failure_threshold: int = 3
    base_recovery_timeout: float = 5.0
    max_recovery_timeout: float = 300.0   # 5 minutes max
    backoff_multiplier: float = 2.0

    state: State = State.CLOSED
    failures: int = 0
    consecutive_open_count: int = 0       # How many times we've opened
    opened_at: float = 0.0
    _current_timeout: float = field(init=False)

    def __post_init__(self):
        self._current_timeout = self.base_recovery_timeout

    @property
    def recovery_timeout(self) -> float:
        return self._current_timeout

    def _calculate_timeout(self) -> float:
        timeout = self.base_recovery_timeout * (
            self.backoff_multiplier ** self.consecutive_open_count
        )
        return min(timeout, self.max_recovery_timeout)

    def can_call(self) -> bool:
        if self.state == State.CLOSED:
            return True
        if self.state == State.OPEN:
            if time.monotonic() - self.opened_at >= self._current_timeout:
                self.state = State.HALF_OPEN
                print(f"[{self.name}] OPEN → HALF_OPEN "
                      f"(timeout was {self._current_timeout:.1f}s)")
                return True
            return False
        return True  # HALF_OPEN

    def record_success(self):
        if self.state == State.HALF_OPEN:
            self.state = State.CLOSED
            self.consecutive_open_count = 0
            self._current_timeout = self.base_recovery_timeout
            print(f"[{self.name}] HALF_OPEN → CLOSED (timeout reset to {self._current_timeout}s)")
        self.failures = 0

    def record_failure(self):
        self.failures += 1
        if self.state == State.HALF_OPEN or self.failures >= self.failure_threshold:
            self.consecutive_open_count += 1
            self._current_timeout = self._calculate_timeout()
            self.state = State.OPEN
            self.opened_at = time.monotonic()
            print(f"[{self.name}] → OPEN "
                  f"(attempt #{self.consecutive_open_count}, "
                  f"next retry in {self._current_timeout:.1f}s)")

async def demo_adaptive_timeout():
    breaker = AdaptiveCircuitBreaker(
        name="flaky-service",
        failure_threshold=2,
        base_recovery_timeout=0.5,  # Short for demo
        max_recovery_timeout=5.0,
        backoff_multiplier=2.0,
    )

    fail_count = [0]

    async def always_fails():
        fail_count[0] += 1
        # Recover after 8 failures
        if fail_count[0] > 8:
            return "OK"
        raise ConnectionError("service down")

    for trial in range(20):
        if breaker.can_call():
            try:
                result = await always_fails()
                breaker.record_success()
                print(f"Trial {trial:2d}: ✓ {result} [timeout reset]")
            except Exception as e:
                breaker.record_failure()
                print(f"Trial {trial:2d}: ✗ {e} [timeout={breaker._current_timeout:.1f}s]")
        else:
            remaining = breaker._current_timeout - (time.monotonic() - breaker.opened_at)
            print(f"Trial {trial:2d}: BLOCKED (retry in {remaining:.1f}s)")

        await asyncio.sleep(0.3)

asyncio.run(demo_adaptive_timeout())
```

### Option 6: Metrics-Emitting Circuit Breaker

Circuit breaker that emits structured metrics for dashboards and alerting systems.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

METRICS_LOG = Path(".circuit_breaker_metrics.jsonl")

class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class MetricsEvent:
    timestamp: float
    breaker_name: str
    event_type: str   # "call_success", "call_failure", "state_change", "call_rejected"
    state: str
    data: dict = field(default_factory=dict)

    def log(self):
        record = {
            "ts": self.timestamp,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "breaker": self.breaker_name,
            "event": self.event_type,
            "state": self.state,
            **self.data,
        }
        with open(METRICS_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

@dataclass
class MetricsCircuitBreaker:
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0

    state: State = State.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    _calls_total: int = 0
    _calls_success: int = 0
    _calls_failed: int = 0
    _calls_rejected: int = 0
    _state_changes: list[dict] = field(default_factory=list)

    def _emit(self, event_type: str, data: dict = None):
        MetricsEvent(
            timestamp=time.time(),
            breaker_name=self.name,
            event_type=event_type,
            state=self.state.value,
            data=data or {}
        ).log()

    def can_call(self) -> bool:
        if self.state == State.OPEN:
            if time.monotonic() - self.opened_at >= self.recovery_timeout:
                old = self.state
                self.state = State.HALF_OPEN
                self._state_changes.append({"from": old.value, "to": "half_open", "ts": time.time()})
                self._emit("state_change", {"from": old.value, "to": "half_open"})
                return True
            self._calls_rejected += 1
            self._emit("call_rejected", {"reason": "circuit_open"})
            return False
        return True

    def record_success(self, latency_ms: float = 0):
        self._calls_total += 1
        self._calls_success += 1
        if self.state == State.HALF_OPEN:
            old = self.state
            self.state = State.CLOSED
            self.failures = 0
            self._state_changes.append({"from": old.value, "to": "closed", "ts": time.time()})
            self._emit("state_change", {"from": old.value, "to": "closed"})
        self._emit("call_success", {"latency_ms": latency_ms})

    def record_failure(self, error: str = ""):
        self._calls_total += 1
        self._calls_failed += 1
        self.failures += 1
        if self.state == State.HALF_OPEN or self.failures >= self.failure_threshold:
            old = self.state
            self.state = State.OPEN
            self.opened_at = time.monotonic()
            self._state_changes.append({"from": old.value, "to": "open", "ts": time.time()})
            self._emit("state_change", {"from": old.value, "to": "open", "failures": self.failures})
        self._emit("call_failure", {"error": error, "failure_count": self.failures})

    def dashboard(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "calls": {
                "total": self._calls_total,
                "success": self._calls_success,
                "failed": self._calls_failed,
                "rejected": self._calls_rejected,
                "success_rate": f"{self._calls_success / max(self._calls_total, 1):.0%}",
            },
            "state_changes": len(self._state_changes),
            "current_failures": self.failures,
        }

async def demo_metrics_circuit():
    breaker = MetricsCircuitBreaker(
        name="recommendation-api",
        failure_threshold=3,
        recovery_timeout=1.5,
    )

    import random
    for i in range(15):
        if breaker.can_call():
            start = time.monotonic()
            # 60% failure rate in middle of run
            if 4 <= i <= 9:
                breaker.record_failure(f"500 Internal Server Error")
                print(f"[{i:2d}] ✗ API error [{breaker.state.value}]")
            else:
                latency = (time.monotonic() - start) * 1000
                breaker.record_success(latency)
                print(f"[{i:2d}] ✓ Success [{breaker.state.value}]")
        else:
            print(f"[{i:2d}] BLOCKED [open]")

        await asyncio.sleep(0.2)

    print(f"\nDashboard: {json.dumps(breaker.dashboard(), indent=2)}")
    if METRICS_LOG.exists():
        events = METRICS_LOG.read_text().splitlines()
        print(f"Logged {len(events)} metric events to {METRICS_LOG}")

asyncio.run(demo_metrics_circuit())
```

## Comparison

| Approach | Trigger Logic | Recovery | Observability | Complexity |
|---|---|---|---|---|
| Basic Three-State | Failure count | Fixed timeout | None | Low |
| Failure-Rate-Based | Failure % in window | Fixed timeout | Basic | Low |
| Agent Tool Loop Integration | Failure count | Fixed timeout | Tool-level | Medium |
| Bulkhead + Circuit Breaker | Failure count | Fixed timeout | Stats | Medium |
| Adaptive Recovery Timeout | Failure count | Exponential backoff | None | Medium |
| Metrics-Emitting | Failure count | Fixed timeout | Full JSONL | Medium |

**Choose Basic Three-State** for a first implementation—it handles 80% of use cases in under 50 lines of code. **Choose Failure-Rate-Based** in high-traffic systems where raw failure counts are misleading (a slow service with 100 calls/sec will open faster than one with 1 call/sec even if failure rates are equal). **Choose Adaptive Recovery Timeout** for tools connecting to systems that take progressively longer to recover (database restarts, service restarts) where fixed timeouts cause premature probe attempts.
