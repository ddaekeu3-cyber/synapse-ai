---
title: "Agent Doesn't Implement Chaos Testing for Agent Resilience"
slug: agent-doesnt-implement-chaos-testing-for-agent-resilience
category: reliability
tags: [chaos-engineering, resilience, testing, fault-injection, anthropic-sdk, reliability]
description: >
  The agent has never been deliberately subjected to adverse conditions —
  network timeouts, random API errors, slow responses, rate limits — so its
  failure modes are unknown until they surface in production. Chaos testing
  injects faults in a controlled environment to verify that retries, fallbacks,
  and circuit breakers actually work as designed.
symptoms:
  - Retry logic was written but never verified against real API error shapes
  - Circuit breaker trips in production but engineers are surprised it happened
  - A 5-second API slowdown cascades to a 30-second user request in ways nobody anticipated
  - Test suite passes but the agent has zero integration coverage of error paths
related_solutions:
  - agent-doesnt-implement-circuit-breaker-per-downstream-dependency
  - agent-doesnt-implement-timeout-cascade-prevention
  - agent-doesnt-implement-fallback-response-when-all-models-fail
---

## Problem

Most agents are tested only on the happy path. Chaos testing — deliberately
injecting latency, errors, rate limits, and malformed responses — reveals
whether your resilience mechanisms (retries, circuit breakers, fallbacks) are
correctly implemented. Without it, you discover failure modes from user
complaints rather than test runs.

---

## Solution 1 — Fault-Injecting HTTP Transport (Error Rate Injection)

Replace the default httpx transport with one that randomly raises errors at a
configurable rate. Run your agent against it in tests to verify error handling.

```python
import anthropic
import asyncio
import httpx
import random
from dataclasses import dataclass


@dataclass
class FaultConfig:
    error_rate:   float = 0.0   # 0.0–1.0 probability of injecting a fault
    latency_ms:   float = 0.0   # extra latency to inject (ms)
    error_type:   str   = "rate_limit"  # "rate_limit" | "timeout" | "server_error" | "connection"


class FaultInjectingTransport(httpx.AsyncBaseTransport):
    """Wraps a real transport and injects configurable faults for chaos testing."""

    def __init__(self, wrapped: httpx.AsyncBaseTransport, config: FaultConfig):
        self._wrapped = wrapped
        self._config  = config
        self._total_calls    = 0
        self._injected_faults = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._total_calls += 1

        # Inject latency
        if self._config.latency_ms > 0:
            await asyncio.sleep(self._config.latency_ms / 1000)

        # Inject error
        if random.random() < self._config.error_rate:
            self._injected_faults += 1
            fault = self._config.error_type
            if fault == "rate_limit":
                return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "Injected rate limit"}})
            elif fault == "server_error":
                return httpx.Response(500, json={"error": {"type": "api_error", "message": "Injected server error"}})
            elif fault == "timeout":
                raise httpx.TimeoutException("Injected timeout", request=request)
            elif fault == "connection":
                raise httpx.ConnectError("Injected connection error", request=request)

        return await self._wrapped.handle_async_request(request)

    @property
    def fault_rate_observed(self) -> float:
        return self._injected_faults / max(self._total_calls, 1)

    def stats(self) -> dict:
        return {
            "total_calls":     self._total_calls,
            "injected_faults": self._injected_faults,
            "fault_rate":      f"{self.fault_rate_observed:.0%}",
        }


def make_chaos_client(config: FaultConfig) -> anthropic.AsyncAnthropic:
    transport = FaultInjectingTransport(httpx.AsyncHTTPTransport(), config)
    http_client = httpx.AsyncClient(transport=transport)
    return anthropic.AsyncAnthropic(http_client=http_client)


async def agent_with_retry(
    client: anthropic.AsyncAnthropic,
    messages: list,
    max_retries: int = 3,
) -> str:
    for attempt in range(max_retries + 1):
        try:
            resp = await client.messages.create(
                model="claude-sonnet-4-6", max_tokens=256, messages=messages
            )
            return resp.content[0].text
        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            if attempt == max_retries:
                raise
            wait = 2 ** attempt
            print(f"[retry] attempt {attempt}  error={type(e).__name__}  waiting {wait}s")
            await asyncio.sleep(wait)
    raise RuntimeError("Unreachable")


async def chaos_test_retry_logic():
    """Verify that retry logic handles 30% rate-limit injection."""
    config = FaultConfig(error_rate=0.30, error_type="rate_limit")
    chaos_client = make_chaos_client(config)
    transport: FaultInjectingTransport = chaos_client._client._transport

    success = 0
    failure = 0
    for i in range(10):
        try:
            text = await agent_with_retry(
                chaos_client,
                [{"role": "user", "content": f"Question {i}: define retry."}],
            )
            success += 1
        except Exception as e:
            failure += 1
            print(f"[chaos] request {i} ultimately failed: {type(e).__name__}")

    print(f"\n[chaos-test] success={success}  failure={failure}")
    print(f"[chaos-test] transport stats: {transport.stats()}")
    assert success > 0, "All requests failed — retry logic broken"
    print("[chaos-test] PASS: retry logic handled injected rate limits")


asyncio.run(chaos_test_retry_logic())
```

---

## Solution 2 — Latency Injection to Test Timeout Behaviour

Inject configurable latency to verify that timeouts fire correctly and that
slow responses don't cascade to hung callers.

```python
import anthropic
import asyncio
import httpx
import random
import time


class LatencyInjectingTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        wrapped: httpx.AsyncBaseTransport,
        base_latency_ms: float = 0,
        jitter_ms: float = 0,
        slow_request_rate: float = 0.0,
        slow_latency_ms: float = 10_000,
    ):
        self._wrapped = wrapped
        self._base = base_latency_ms
        self._jitter = jitter_ms
        self._slow_rate = slow_request_rate
        self._slow_ms = slow_latency_ms
        self._slow_count = 0
        self._total_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._total_count += 1
        latency = self._base + random.uniform(0, self._jitter)

        if random.random() < self._slow_rate:
            self._slow_count += 1
            latency += self._slow_ms
            print(f"[chaos-latency] injecting {latency:.0f}ms slow request")

        if latency > 0:
            await asyncio.sleep(latency / 1000)

        return await self._wrapped.handle_async_request(request)

    def stats(self) -> dict:
        return {
            "total_requests": self._total_count,
            "slow_injected":  self._slow_count,
            "slow_rate":      f"{self._slow_count / max(self._total_count, 1):.0%}",
        }


async def create_with_timeout(
    client: anthropic.AsyncAnthropic,
    messages: list,
    timeout_s: float = 5.0,
) -> str:
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model="claude-sonnet-4-6", max_tokens=128, messages=messages
            ),
            timeout=timeout_s,
        )
        return resp.content[0].text
    except asyncio.TimeoutError:
        return f"[TIMEOUT after {timeout_s}s]"


async def chaos_test_timeout():
    """Verify timeouts fire within the expected window under injected latency."""
    transport = LatencyInjectingTransport(
        httpx.AsyncHTTPTransport(),
        base_latency_ms=100,
        jitter_ms=200,
        slow_request_rate=0.20,
        slow_latency_ms=8_000,
    )
    http_client = httpx.AsyncClient(transport=transport)
    client = anthropic.AsyncAnthropic(http_client=http_client)

    timeout_count = 0
    start = time.monotonic()
    for i in range(5):
        t0 = time.monotonic()
        result = await create_with_timeout(
            client,
            [{"role": "user", "content": f"Q{i}: what is idempotency?"}],
            timeout_s=3.0,
        )
        elapsed = time.monotonic() - t0
        timed_out = result.startswith("[TIMEOUT")
        if timed_out:
            timeout_count += 1
        print(f"[chaos] request {i}  elapsed={elapsed:.2f}s  timed_out={timed_out}")

    total_elapsed = time.monotonic() - start
    print(f"\n[chaos-test] timeouts={timeout_count}/5  total_elapsed={total_elapsed:.1f}s")
    print(f"[chaos-test] latency stats: {transport.stats()}")
    assert total_elapsed < 20, "Timeouts did not fire — hung requests"
    print("[chaos-test] PASS: timeouts fired correctly")


asyncio.run(chaos_test_timeout())
```

---

## Solution 3 — Malformed Response Injector

Inject syntactically valid but semantically malformed API responses (wrong
field types, missing required fields, truncated JSON) to verify that your
response parsing and validation code handles unexpected shapes gracefully.

```python
import anthropic
import asyncio
import httpx
import json
import random


MALFORMED_RESPONSES = [
    # Missing content field
    {"id": "msg_chaos", "type": "message", "role": "assistant",
     "model": "claude-sonnet-4-6", "stop_reason": "end_turn",
     "usage": {"input_tokens": 10, "output_tokens": 5}},
    # Content is empty list
    {"id": "msg_chaos", "type": "message", "role": "assistant",
     "content": [], "model": "claude-sonnet-4-6", "stop_reason": "end_turn",
     "usage": {"input_tokens": 10, "output_tokens": 0}},
    # Truncated JSON
    '{"id": "msg_chaos", "type": "mess',
]


class MalformedResponseTransport(httpx.AsyncBaseTransport):
    def __init__(self, wrapped, malform_rate: float = 0.20):
        self._wrapped    = wrapped
        self._malform_rate = malform_rate
        self._malform_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "/messages" in str(request.url) and random.random() < self._malform_rate:
            self._malform_count += 1
            bad = random.choice(MALFORMED_RESPONSES)
            body = bad if isinstance(bad, str) else json.dumps(bad)
            print(f"[chaos-malform] injecting malformed response #{self._malform_count}")
            return httpx.Response(200, text=body,
                                  headers={"content-type": "application/json"})
        return await self._wrapped.handle_async_request(request)


def safe_parse_response(resp) -> str | None:
    """Safely extract text from a response, returning None on any error."""
    try:
        if not resp.content:
            return None
        return resp.content[0].text
    except (AttributeError, IndexError, TypeError):
        return None


async def chaos_test_malformed():
    transport = MalformedResponseTransport(httpx.AsyncHTTPTransport(), malform_rate=0.30)
    http_client = httpx.AsyncClient(transport=transport)
    client = anthropic.AsyncAnthropic(http_client=http_client)

    parse_failures = 0
    api_errors = 0
    successes = 0

    for i in range(8):
        try:
            resp = await client.messages.create(
                model="claude-sonnet-4-6", max_tokens=64,
                messages=[{"role": "user", "content": f"Q{i}: hello"}],
            )
            text = safe_parse_response(resp)
            if text is None:
                parse_failures += 1
                print(f"[chaos] request {i}: parse failure (empty content)")
            else:
                successes += 1
        except (anthropic.APIStatusError, anthropic.APIResponseValidationError, Exception) as e:
            api_errors += 1
            print(f"[chaos] request {i}: {type(e).__name__}")

    print(f"\n[chaos-test] success={successes}  parse_fail={parse_failures}  api_err={api_errors}")
    assert api_errors + parse_failures < 8, "All requests failed — no resilience to malformed responses"
    print("[chaos-test] PASS: agent handled malformed responses without crashing")


asyncio.run(chaos_test_malformed())
```

---

## Solution 4 — Chaos Scenario Runner with Assertions

Define named chaos scenarios (network partition, rate limit storm, cascading
slowdown) as dataclasses and run them with assertion checks to produce a
pass/fail resilience report.

```python
import anthropic
import asyncio
import httpx
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable


@dataclass
class ChaosScenario:
    name:        str
    description: str
    error_rate:  float = 0.0
    latency_ms:  float = 0.0
    timeout_s:   float = 30.0
    n_requests:  int   = 10
    min_success_rate: float = 0.70   # assertion: at least X% must succeed


class ScenarioTransport(httpx.AsyncBaseTransport):
    def __init__(self, wrapped, scenario: ChaosScenario):
        self._w = wrapped
        self._s = scenario

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._s.latency_ms:
            await asyncio.sleep(self._s.latency_ms / 1000)
        if random.random() < self._s.error_rate:
            return httpx.Response(
                429 if random.random() < 0.5 else 500,
                json={"error": {"type": "chaos_fault", "message": "Injected fault"}},
            )
        return await self._w.handle_async_request(request)


async def run_scenario(scenario: ChaosScenario) -> dict:
    transport = ScenarioTransport(httpx.AsyncHTTPTransport(), scenario)
    http_client = httpx.AsyncClient(transport=transport)
    client = anthropic.AsyncAnthropic(http_client=http_client)

    results = {"success": 0, "failure": 0, "latencies": []}
    messages = [{"role": "user", "content": "What is consistent hashing?"}]

    for i in range(scenario.n_requests):
        t0 = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                client.messages.create(model="claude-sonnet-4-6", max_tokens=64, messages=messages),
                timeout=scenario.timeout_s,
            )
            results["success"] += 1
            results["latencies"].append(time.monotonic() - t0)
        except Exception:
            results["failure"] += 1

    success_rate = results["success"] / scenario.n_requests
    p50 = sorted(results["latencies"])[len(results["latencies"]) // 2] if results["latencies"] else 0
    passed = success_rate >= scenario.min_success_rate

    return {
        "scenario":     scenario.name,
        "success_rate": f"{success_rate:.0%}",
        "p50_latency":  f"{p50:.2f}s",
        "passed":       passed,
        "assertion":    f">= {scenario.min_success_rate:.0%} success rate",
    }


SCENARIOS = [
    ChaosScenario("baseline",      "No faults",                error_rate=0.00, latency_ms=0,    min_success_rate=0.99),
    ChaosScenario("low_errors",    "10% error rate",           error_rate=0.10, latency_ms=0,    min_success_rate=0.85),
    ChaosScenario("high_errors",   "50% error rate",           error_rate=0.50, latency_ms=0,    min_success_rate=0.40),
    ChaosScenario("slow_network",  "500ms added latency",      error_rate=0.00, latency_ms=500,  min_success_rate=0.90),
    ChaosScenario("storm",         "30% errors + 200ms",       error_rate=0.30, latency_ms=200,  min_success_rate=0.50),
]


async def run_chaos_suite():
    print("Running chaos test suite...\n")
    results = []
    for scenario in SCENARIOS:
        print(f"  [{scenario.name}] {scenario.description}...", end=" ", flush=True)
        result = await run_scenario(scenario)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status}  success={result['success_rate']}  p50={result['p50_latency']}")
        results.append(result)

    passed = sum(1 for r in results if r["passed"])
    print(f"\nChaos suite: {passed}/{len(results)} scenarios passed")
    return results


asyncio.run(run_chaos_suite())
```

---

## Solution 5 — Property-Based Chaos with Hypothesis

Use the `hypothesis` library to generate random fault combinations and verify
that the agent never crashes (raises an unhandled exception) regardless of API
conditions.

```python
import anthropic
import asyncio
import httpx
import random

# pip install hypothesis
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


class ConfigurableChaosTp(httpx.AsyncBaseTransport):
    def __init__(self, wrapped, error_rate: float, latency_ms: float):
        self._w = wrapped
        self._error_rate = error_rate
        self._latency_ms = latency_ms

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._latency_ms > 0:
            await asyncio.sleep(min(self._latency_ms / 1000, 2.0))
        if random.random() < self._error_rate:
            status = random.choice([429, 500, 503])
            return httpx.Response(status, json={
                "error": {"type": "chaos", "message": "Property-based chaos fault"}
            })
        return await self._w.handle_async_request(request)


def make_chaos_agent(error_rate: float, latency_ms: float) -> anthropic.AsyncAnthropic:
    tp = ConfigurableChaosTp(httpx.AsyncHTTPTransport(), error_rate, latency_ms)
    return anthropic.AsyncAnthropic(http_client=httpx.AsyncClient(transport=tp))


async def _agent_call(error_rate: float, latency_ms: float, prompt: str) -> str:
    """
    Agent wrapper that must NEVER raise an unhandled exception.
    All errors must be caught and return a graceful string.
    """
    client = make_chaos_agent(error_rate, latency_ms)
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model="claude-sonnet-4-6", max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=5.0,
        )
        return resp.content[0].text if resp.content else "[empty]"
    except asyncio.TimeoutError:
        return "[timeout]"
    except anthropic.RateLimitError:
        return "[rate_limited]"
    except anthropic.APIStatusError as e:
        return f"[api_error:{e.status_code}]"
    except Exception as e:
        return f"[error:{type(e).__name__}]"


@given(
    error_rate=st.floats(min_value=0.0, max_value=1.0),
    latency_ms=st.floats(min_value=0.0, max_value=500.0),
    prompt=st.text(min_size=1, max_size=100),
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
def test_agent_never_crashes(error_rate: float, latency_ms: float, prompt: str):
    """Property: agent ALWAYS returns a string, never raises."""
    result = asyncio.run(_agent_call(error_rate, latency_ms, prompt))
    assert isinstance(result, str), f"Agent returned non-string: {result!r}"
    assert len(result) > 0, "Agent returned empty string"


# Run the property test
print("Running property-based chaos test...")
try:
    test_agent_never_crashes()
    print("[hypothesis] PASS: agent never crashed across all generated fault combinations")
except Exception as e:
    print(f"[hypothesis] FAIL: {e}")
```

---

## Solution 6 — Continuous Chaos Probe in Staging

Run a background task in staging that continuously fires test requests with
injected faults and alerts when the agent's resilience metrics degrade below
expected baselines.

```python
import anthropic
import asyncio
import httpx
import random
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class ResilienceMetrics:
    window: deque = field(default_factory=lambda: deque(maxlen=100))

    def record(self, success: bool, latency_s: float) -> None:
        self.window.append((success, latency_s))

    @property
    def success_rate(self) -> float:
        if not self.window:
            return 1.0
        return sum(1 for s, _ in self.window if s) / len(self.window)

    @property
    def p95_latency(self) -> float:
        latencies = sorted(l for _, l in self.window)
        if not latencies:
            return 0.0
        return latencies[int(0.95 * len(latencies))]


class StagingChaosProbe:
    """
    Continuously fires requests with low-level fault injection to staging.
    Alerts when success_rate < threshold or p95 > latency_threshold.
    """

    def __init__(
        self,
        fault_rate: float = 0.05,
        probe_interval_s: float = 5.0,
        success_threshold: float = 0.90,
        p95_threshold_s: float = 10.0,
    ):
        self._fault_rate       = fault_rate
        self._probe_interval_s = probe_interval_s
        self._success_threshold = success_threshold
        self._p95_threshold_s  = p95_threshold_s
        self._metrics = ResilienceMetrics()
        self._running = False

    def _make_client(self) -> anthropic.AsyncAnthropic:
        class ChaosTransport(httpx.AsyncBaseTransport):
            def __init__(inner_self, wrapped):
                inner_self._w = wrapped

            async def handle_async_request(inner_self, request):
                if random.random() < self._fault_rate:
                    return httpx.Response(429, json={"error": {"type": "chaos"}})
                return await inner_self._w.handle_async_request(request)

        tp = ChaosTransport(httpx.AsyncHTTPTransport())
        return anthropic.AsyncAnthropic(http_client=httpx.AsyncClient(transport=tp))

    async def _single_probe(self) -> None:
        client = self._make_client()
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=16,
                    messages=[{"role": "user", "content": "ping"}],
                ),
                timeout=8.0,
            )
            success = True
        except Exception:
            success = False
        self._metrics.record(success, time.monotonic() - t0)

    async def _check_and_alert(self) -> None:
        sr = self._metrics.success_rate
        p95 = self._metrics.p95_latency
        alerts = []
        if sr < self._success_threshold:
            alerts.append(f"success_rate={sr:.0%} < threshold={self._success_threshold:.0%}")
        if p95 > self._p95_threshold_s:
            alerts.append(f"p95_latency={p95:.1f}s > threshold={self._p95_threshold_s:.1f}s")
        if alerts:
            print(f"[ALERT] Resilience degraded: {'; '.join(alerts)}")
        else:
            print(f"[probe] OK  success={sr:.0%}  p95={p95:.2f}s  n={len(self._metrics.window)}")

    async def run(self, duration_s: float = 30.0) -> None:
        self._running = True
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            await self._single_probe()
            await self._check_and_alert()
            await asyncio.sleep(self._probe_interval_s)
        self._running = False


async def demo_continuous_probe():
    probe = StagingChaosProbe(
        fault_rate=0.10,
        probe_interval_s=2.0,
        success_threshold=0.85,
        p95_threshold_s=5.0,
    )
    print("Running continuous chaos probe for 12 seconds...")
    await probe.run(duration_s=12.0)


asyncio.run(demo_continuous_probe())
```

---

## Comparison

| Approach | Fault types | Automated assertions | CI/CD integration | Production safe | Complexity |
|---|---|---|---|---|---|
| Fault-injecting transport | Errors, rate limits | Yes (manual) | Yes | Yes (test only) | Low |
| Latency injection | Slowdowns, timeouts | Yes (timing) | Yes | Yes (test only) | Low |
| Malformed response injector | Bad API shapes | Yes (parse safety) | Yes | Yes (test only) | Medium |
| Scenario runner with assertions | Named scenarios | Yes (pass/fail) | Yes (report) | Yes | Medium |
| Property-based (Hypothesis) | Random combinations | Yes (never crashes) | Yes | Yes | Medium |
| Continuous staging probe | Error rate + latency | Yes (threshold alerts) | Always-on | Staging only | Medium |

**Rule of thumb:**
- Start with Solution 1 (fault transport) and Solution 2 (latency) in existing unit tests — 30 minutes setup
- Add Solution 4 (scenario runner) to CI so each PR has a pass/fail resilience gate
- Use Solution 5 (Hypothesis) for the most critical paths where you want exhaustive coverage
- Run Solution 6 (continuous probe) in staging permanently so regressions are caught before production
