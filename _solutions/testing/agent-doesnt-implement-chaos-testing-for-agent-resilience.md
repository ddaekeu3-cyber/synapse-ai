---
title: "Agent Doesn't Implement Chaos Testing for Agent Resilience"
description: "Systematically inject failures—API errors, latency spikes, malformed responses—to verify your agent handles adversarial conditions gracefully before they hit production."
difficulty: advanced
category: testing
tags: [chaos-testing, resilience, fault-injection, reliability, testing]
---

## Problem

Agents are tested against happy-path scenarios but never validated under failure conditions. When API timeouts, partial responses, rate limits, or corrupted tool results occur in production, agents crash ungracefully or produce silent incorrect output. Without chaos testing, resilience is assumed rather than proven.

## Solutions

### Option 1: Fault-Injection Wrapper

Intercept API calls and randomly inject configurable failure modes.

```python
import asyncio
import random
import httpx
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ChaosConfig:
    error_rate: float = 0.1          # 10% chance of API error
    timeout_rate: float = 0.05       # 5% chance of timeout
    latency_spike_rate: float = 0.15 # 15% chance of added latency
    latency_spike_ms: int = 3000     # 3s spike
    malformed_rate: float = 0.05     # 5% chance of malformed response
    enabled: bool = True

class ChaosClient:
    def __init__(self, config: ChaosConfig):
        self.client = AsyncAnthropic()
        self.config = config
        self.injected: list[str] = []

    async def create(self, **kwargs) -> Any:
        if not self.config.enabled:
            return await self.client.messages.create(**kwargs)

        roll = random.random()
        cumulative = 0.0

        cumulative += self.config.error_rate
        if roll < cumulative:
            self.injected.append("api_error")
            raise httpx.HTTPStatusError(
                "Injected 500", request=None, response=None
            )

        cumulative += self.config.timeout_rate
        if roll < cumulative:
            self.injected.append("timeout")
            raise httpx.ReadTimeout("Injected timeout")

        cumulative += self.config.latency_spike_rate
        if roll < cumulative:
            self.injected.append("latency_spike")
            await asyncio.sleep(self.config.latency_spike_ms / 1000)

        response = await self.client.messages.create(**kwargs)

        if random.random() < self.config.malformed_rate:
            self.injected.append("malformed_response")
            # Truncate content to simulate partial response
            if response.content:
                response.content[0].text = response.content[0].text[:10] + "��"

        return response

async def run_with_chaos():
    config = ChaosConfig(error_rate=0.2, latency_spike_rate=0.3)
    chaos = ChaosClient(config)

    results = {"success": 0, "error": 0, "timeout": 0}
    for i in range(20):
        try:
            response = await chaos.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": f"Test {i}"}]
            )
            results["success"] += 1
        except httpx.ReadTimeout:
            results["timeout"] += 1
        except Exception:
            results["error"] += 1

    print(f"Results: {results}")
    print(f"Injected faults: {chaos.injected}")
    return results

asyncio.run(run_with_chaos())
```

### Option 2: Structured Chaos Test Suite

Define named chaos scenarios and run them as a test suite with pass/fail assertions.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Callable, Awaitable

@dataclass
class ChaosScenario:
    name: str
    description: str
    fault_fn: Callable
    expected_behavior: str
    max_recovery_ms: int = 5000

class AgentUnderTest:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.retry_count = 0

    async def process(self, message: str) -> str:
        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    messages=[{"role": "user", "content": message}]
                )
                return response.content[0].text
            except Exception as e:
                self.retry_count += 1
                if attempt == 2:
                    return f"[FALLBACK] Could not process: {type(e).__name__}"
                await asyncio.sleep(0.5 * (attempt + 1))

class ChaosSuite:
    def __init__(self, agent: AgentUnderTest):
        self.agent = agent
        self.results: list[dict] = []

    async def run_scenario(self, scenario: ChaosScenario) -> dict:
        start = time.monotonic()
        passed = False
        error_msg = ""

        try:
            await scenario.fault_fn(self.agent)
            response = await self.agent.process("Summarize: chaos test")
            elapsed_ms = (time.monotonic() - start) * 1000

            # Agent should always return a non-empty response
            if response and len(response) > 0 and elapsed_ms <= scenario.max_recovery_ms:
                passed = True
            else:
                error_msg = f"Response empty or too slow ({elapsed_ms:.0f}ms)"
        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            error_msg = f"Unhandled exception: {e}"

        result = {
            "scenario": scenario.name,
            "passed": passed,
            "error": error_msg,
            "retries": self.agent.retry_count,
        }
        self.results.append(result)
        return result

    def report(self):
        passed = sum(1 for r in self.results if r["passed"])
        print(f"\nChaos Suite: {passed}/{len(self.results)} passed")
        for r in self.results:
            status = "✓" if r["passed"] else "✗"
            print(f"  {status} {r['scenario']}: {r['error'] or 'OK'} (retries={r['retries']})")

async def run_chaos_suite():
    agent = AgentUnderTest()
    suite = ChaosSuite(agent)

    async def inject_high_concurrency(a):
        # Pre-saturate with concurrent requests
        tasks = [a.process(f"concurrent {i}") for i in range(5)]
        await asyncio.gather(*tasks, return_exceptions=True)
        a.retry_count = 0

    async def inject_rapid_succession(a):
        # Fire requests rapidly without delay
        for i in range(3):
            await a.process(f"rapid {i}")
        a.retry_count = 0

    async def no_fault(a):
        pass

    scenarios = [
        ChaosScenario("baseline", "No faults", no_fault, "normal response"),
        ChaosScenario("high_concurrency", "5 concurrent", inject_high_concurrency, "handles load"),
        ChaosScenario("rapid_succession", "3 rapid calls", inject_rapid_succession, "no errors"),
    ]

    for scenario in scenarios:
        result = await suite.run_scenario(scenario)
        agent.retry_count = 0

    suite.report()

asyncio.run(run_chaos_suite())
```

### Option 3: Network Partition Simulator

Simulate network conditions: packet loss, bandwidth throttling, connection drops mid-stream.

```python
import asyncio
import random
import httpx
from anthropic import AsyncAnthropic, APIConnectionError
from unittest.mock import AsyncMock, patch

class NetworkChaos:
    """Simulate network-level failures at the httpx transport layer."""

    def __init__(self, packet_loss: float = 0.1, drop_mid_stream: float = 0.1):
        self.packet_loss = packet_loss
        self.drop_mid_stream = drop_mid_stream
        self.events: list[str] = []

    def make_transport(self) -> httpx.AsyncHTTPTransport:
        outer = self

        class ChaosTransport(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request):
                # Simulate packet loss (connection refused)
                if random.random() < outer.packet_loss:
                    outer.events.append("packet_loss")
                    raise httpx.ConnectError("Simulated packet loss")

                response = await super().handle_async_request(request)

                # Simulate mid-stream drop by returning truncated body
                if random.random() < outer.drop_mid_stream:
                    outer.events.append("mid_stream_drop")
                    raise httpx.RemoteProtocolError("Simulated stream drop")

                return response

        return ChaosTransport()

class ResilientAgent:
    def __init__(self, transport: httpx.AsyncHTTPTransport | None = None):
        http_client = httpx.AsyncClient(transport=transport) if transport else None
        self.client = AsyncAnthropic(http_client=http_client)
        self.fallback_responses = 0

    async def ask(self, question: str) -> str:
        for attempt in range(4):
            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=150,
                    messages=[{"role": "user", "content": question}]
                )
                return response.content[0].text
            except (httpx.ConnectError, httpx.RemoteProtocolError, APIConnectionError):
                backoff = min(2 ** attempt, 8)
                await asyncio.sleep(backoff * 0.1)  # Shortened for testing
            except Exception:
                break

        self.fallback_responses += 1
        return "Service temporarily unavailable. Please try again."

async def test_network_resilience():
    chaos = NetworkChaos(packet_loss=0.3, drop_mid_stream=0.2)
    transport = chaos.make_transport()
    agent = ResilientAgent(transport=transport)

    questions = [
        "What is 2+2?",
        "Name a color.",
        "What day follows Monday?",
        "Is water wet?",
        "Name a planet.",
    ]

    responses = []
    for q in questions:
        r = await agent.ask(q)
        responses.append(r)

    answered = sum(1 for r in responses if "unavailable" not in r)
    print(f"Network chaos test: {answered}/{len(questions)} answered successfully")
    print(f"Network events: {chaos.events}")
    print(f"Fallback responses: {agent.fallback_responses}")

asyncio.run(test_network_resilience())
```

### Option 4: State Corruption Injector

Test agent behavior when tool results, memory, or context are corrupted mid-execution.

```python
import asyncio
import json
import random
from anthropic import AsyncAnthropic
from typing import Any

def corrupt_string(s: str, corruption_level: float = 0.1) -> str:
    """Introduce random bit-flips and deletions into a string."""
    chars = list(s)
    n_corrupt = max(1, int(len(chars) * corruption_level))
    for _ in range(n_corrupt):
        idx = random.randint(0, len(chars) - 1)
        action = random.choice(["replace", "delete", "insert"])
        if action == "replace":
            chars[idx] = random.choice("!@#$%^&*()_+{}|:<>?")
        elif action == "delete" and len(chars) > 1:
            chars.pop(idx)
        else:
            chars.insert(idx, random.choice("xyz"))
    return "".join(chars)

def corrupt_json(data: Any, corruption_level: float = 0.1) -> Any:
    """Corrupt JSON-serializable data structures."""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if random.random() < corruption_level:
                result[k] = None  # Null out random fields
            else:
                result[k] = corrupt_json(v, corruption_level)
        return result
    elif isinstance(data, list):
        if random.random() < corruption_level:
            return data[:len(data)//2]  # Truncate list
        return [corrupt_json(item, corruption_level) for item in data]
    elif isinstance(data, str):
        if random.random() < corruption_level * 0.5:
            return corrupt_string(data, 0.2)
        return data
    return data

class StateCorruptionChaos:
    def __init__(self, corruption_rate: float = 0.15):
        self.client = AsyncAnthropic()
        self.corruption_rate = corruption_rate
        self.corruptions_applied = 0

    def maybe_corrupt_tool_result(self, result: dict) -> dict:
        if random.random() < self.corruption_rate:
            self.corruptions_applied += 1
            return corrupt_json(result, 0.3)
        return result

    async def run_with_corrupted_tools(self, task: str) -> str:
        """Run agent loop where tool results may be corrupted."""
        tools = [{
            "name": "get_data",
            "description": "Retrieve data by key",
            "input_schema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"]
            }
        }]

        messages = [{"role": "user", "content": task}]
        final_answer = ""

        for _ in range(5):  # Max turns
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                tools=tools,
                messages=messages
            )

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        final_answer = block.text
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        # Simulate tool result that may be corrupted
                        raw_result = {"value": "42", "status": "ok", "data": [1, 2, 3]}
                        possibly_corrupt = self.maybe_corrupt_tool_result(raw_result)
                        result_str = json.dumps(possibly_corrupt)

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str
                        })

                messages.append({"role": "user", "content": tool_results})

        return final_answer or "[Agent could not complete task]"

async def test_state_corruption():
    chaos = StateCorruptionChaos(corruption_rate=0.4)
    task = "Use the get_data tool with key 'result' and tell me what you get."

    results = []
    for trial in range(5):
        answer = await chaos.run_with_corrupted_tools(task)
        results.append(len(answer) > 0)

    success_rate = sum(results) / len(results)
    print(f"State corruption test: {success_rate:.0%} produced non-empty responses")
    print(f"Corruptions applied: {chaos.corruptions_applied}")

asyncio.run(test_state_corruption())
```

### Option 5: Cascading Failure Simulator

Test behavior when multiple systems fail simultaneously or in cascade.

```python
import asyncio
import random
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from enum import Enum

class ComponentState(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"

@dataclass
class Component:
    name: str
    state: ComponentState = ComponentState.HEALTHY
    failure_probability: float = 0.1
    recovery_time_s: float = 2.0
    failed_at: float = 0.0

    def maybe_fail(self):
        if self.state == ComponentState.HEALTHY and random.random() < self.failure_probability:
            self.state = ComponentState.FAILED
            self.failed_at = time.monotonic()

    def maybe_recover(self):
        if self.state == ComponentState.FAILED:
            elapsed = time.monotonic() - self.failed_at
            if elapsed > self.recovery_time_s:
                self.state = ComponentState.HEALTHY

class CascadeSimulator:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.components = {
            "cache": Component("cache", failure_probability=0.2, recovery_time_s=1.0),
            "retrieval": Component("retrieval", failure_probability=0.15, recovery_time_s=2.0),
            "reranker": Component("reranker", failure_probability=0.1, recovery_time_s=1.5),
        }
        self.degraded_responses = 0
        self.total_requests = 0

    def tick(self):
        """Advance chaos state: fail components and allow recovery."""
        for component in self.components.values():
            component.maybe_fail()
            component.maybe_recover()

    def get_context(self) -> str:
        """Build context based on available components."""
        available = []
        degraded = []

        for name, comp in self.components.items():
            self.tick()
            if comp.state == ComponentState.HEALTHY:
                available.append(name)
            else:
                degraded.append(name)

        if degraded:
            self.degraded_responses += 1

        if available:
            return f"Systems available: {', '.join(available)}. Degraded: {', '.join(degraded)}."
        else:
            return "All systems degraded. Using cached knowledge only."

    async def process_request(self, user_query: str) -> dict:
        self.total_requests += 1
        context = self.get_context()

        healthy_count = sum(
            1 for c in self.components.values()
            if c.state == ComponentState.HEALTHY
        )

        system = (
            f"You are a resilient assistant. Current system state: {context} "
            "If systems are degraded, acknowledge this but still provide best-effort answers."
        )

        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                system=system,
                messages=[{"role": "user", "content": user_query}]
            )
            return {
                "answer": response.content[0].text,
                "healthy_systems": healthy_count,
                "degraded": healthy_count < len(self.components)
            }
        except Exception as e:
            return {"answer": f"Service error: {e}", "healthy_systems": 0, "degraded": True}

async def test_cascade_failures():
    sim = CascadeSimulator()
    queries = [
        "What are the best practices for API design?",
        "Explain microservices architecture.",
        "How does load balancing work?",
        "What is eventual consistency?",
        "Describe CAP theorem.",
    ]

    results = []
    for query in queries:
        result = await sim.process_request(query)
        results.append(result)
        await asyncio.sleep(0.3)  # Allow chaos state to evolve

    answered = sum(1 for r in results if len(r["answer"]) > 20)
    degraded_handled = sum(1 for r in results if r["degraded"] and len(r["answer"]) > 20)

    print(f"Cascade test: {answered}/{len(queries)} answered")
    print(f"Degraded-state handling: {degraded_handled} graceful responses during failures")
    print(f"Total degraded requests: {sim.degraded_responses}/{sim.total_requests}")

asyncio.run(test_cascade_failures())
```

### Option 6: Chaos Regression Guard

Run chaos tests on every PR to catch resilience regressions before they reach production.

```python
import asyncio
import json
import time
import random
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
import httpx

@dataclass
class ResilienceBaseline:
    """Expected resilience thresholds — fail CI if these are not met."""
    min_success_rate: float = 0.85
    max_fallback_rate: float = 0.15
    max_p99_latency_ms: float = 8000.0
    max_unhandled_exceptions: int = 0

@dataclass
class ResilienceReport:
    success_rate: float
    fallback_rate: float
    p99_latency_ms: float
    unhandled_exceptions: int
    fault_distribution: dict = field(default_factory=dict)

    def passes(self, baseline: ResilienceBaseline) -> bool:
        return (
            self.success_rate >= baseline.min_success_rate
            and self.fallback_rate <= baseline.max_fallback_rate
            and self.p99_latency_ms <= baseline.max_p99_latency_ms
            and self.unhandled_exceptions <= baseline.max_unhandled_exceptions
        )

    def to_dict(self) -> dict:
        return {
            "success_rate": f"{self.success_rate:.1%}",
            "fallback_rate": f"{self.fallback_rate:.1%}",
            "p99_latency_ms": f"{self.p99_latency_ms:.0f}",
            "unhandled_exceptions": self.unhandled_exceptions,
            "fault_distribution": self.fault_distribution,
        }

class ChaosRegressionGuard:
    def __init__(self, n_trials: int = 50):
        self.client = AsyncAnthropic()
        self.n_trials = n_trials
        self.latencies: list[float] = []
        self.outcomes: list[str] = []  # "success", "fallback", "exception"
        self.faults: dict[str, int] = {}

    def _inject_fault(self) -> str | None:
        """Return fault type or None for clean request."""
        r = random.random()
        if r < 0.08:
            return "timeout"
        elif r < 0.13:
            return "error_500"
        elif r < 0.18:
            return "slow"
        return None

    async def _run_trial(self) -> tuple[str, float]:
        fault = self._inject_fault()
        if fault:
            self.faults[fault] = self.faults.get(fault, 0) + 1

        start = time.monotonic()

        try:
            if fault == "timeout":
                await asyncio.sleep(10)  # Will be cancelled by timeout
                raise asyncio.TimeoutError()
            elif fault == "slow":
                await asyncio.sleep(0.5)

            if fault == "error_500":
                raise httpx.HTTPStatusError("500", request=None, response=None)

            response = await asyncio.wait_for(
                self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=50,
                    messages=[{"role": "user", "content": "Reply with: OK"}]
                ),
                timeout=5.0
            )
            elapsed = (time.monotonic() - start) * 1000
            return "success", elapsed

        except (asyncio.TimeoutError, httpx.HTTPStatusError, httpx.ConnectError):
            elapsed = (time.monotonic() - start) * 1000
            return "fallback", elapsed
        except Exception:
            elapsed = (time.monotonic() - start) * 1000
            return "exception", elapsed

    async def run(self) -> ResilienceReport:
        tasks = [self._run_trial() for _ in range(self.n_trials)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                self.outcomes.append("exception")
                self.latencies.append(0)
            else:
                outcome, latency = result
                self.outcomes.append(outcome)
                self.latencies.append(latency)

        success_rate = self.outcomes.count("success") / len(self.outcomes)
        fallback_rate = self.outcomes.count("fallback") / len(self.outcomes)
        exception_count = self.outcomes.count("exception")

        sorted_latencies = sorted(self.latencies)
        p99_idx = int(len(sorted_latencies) * 0.99)
        p99 = sorted_latencies[p99_idx] if sorted_latencies else 0

        return ResilienceReport(
            success_rate=success_rate,
            fallback_rate=fallback_rate,
            p99_latency_ms=p99,
            unhandled_exceptions=exception_count,
            fault_distribution=self.faults,
        )

async def run_chaos_regression_guard():
    baseline = ResilienceBaseline(
        min_success_rate=0.75,   # Relaxed for demo with high fault rates
        max_fallback_rate=0.30,
        max_p99_latency_ms=10000.0,
        max_unhandled_exceptions=2,
    )

    guard = ChaosRegressionGuard(n_trials=20)
    report = await guard.run()

    passes = report.passes(baseline)
    print(f"Chaos Regression Guard: {'PASS' if passes else 'FAIL'}")
    print(json.dumps(report.to_dict(), indent=2))

    # In CI: sys.exit(0 if passes else 1)
    return passes

asyncio.run(run_chaos_regression_guard())
```

## Comparison

| Approach | Fault Coverage | CI Integration | Production Safety | Setup Complexity |
|---|---|---|---|---|
| Fault-Injection Wrapper | API errors, timeouts, malformed | Easy | Safe (synthetic only) | Low |
| Structured Test Suite | Named scenarios, assertions | Native (pytest) | Safe | Low |
| Network Partition Simulator | Packet loss, stream drops | Moderate | Safe (transport-level) | Medium |
| State Corruption Injector | Memory, tool result corruption | Moderate | Safe | Medium |
| Cascading Failure Simulator | Multi-component, recovery | Moderate | Safe | High |
| Chaos Regression Guard | All types, CI gate | Native (exit code) | Safe + Automated | Medium |

**Choose Fault-Injection Wrapper** for quick, low-overhead coverage of the most common failure modes. **Choose Chaos Regression Guard** when you want automated CI gates that prevent resilience regressions from reaching production. **Choose Cascading Failure Simulator** when your agent orchestrates multiple downstream services and you need to verify graceful degradation across components.
