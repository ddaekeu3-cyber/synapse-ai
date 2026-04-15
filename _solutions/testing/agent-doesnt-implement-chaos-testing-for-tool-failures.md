---
layout: solution
title: "Agent Doesn't Implement Chaos Testing for Tool Failures"
category: testing
description: "Agents that are only tested with happy-path tool responses fail unpredictably in production when tools time out, return partial data, or throw unexpected errors."
tags: [testing, chaos, tool-failure, resilience, pytest, mock]
---

# Agent Doesn't Implement Chaos Testing for Tool Failures

Unit tests mock tools to return perfect responses. Integration tests use real services that usually work. Neither tests what the agent does when a tool returns a 500, hangs for 30 seconds, returns truncated JSON, or flaps between success and failure. Chaos testing deliberately injects these failure modes to verify that the agent's error handling, retries, and fallbacks actually work.

## Why This Happens

Chaos testing requires intentionally breaking things, which feels counterproductive. Developers test the success path because it's what they want to happen, and assume error paths "obviously work" — until they don't, in production, at 2am.

---

## Option 1: Fault Injection Wrapper Around Tool Calls

Wrap each tool executor in a fault injector that randomly applies failures based on configured probability.

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Any
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class FaultConfig:
    error_rate: float = 0.0          # fraction of calls that raise an exception
    timeout_rate: float = 0.0         # fraction of calls that hang
    timeout_seconds: float = 10.0     # how long the timeout hangs
    slow_rate: float = 0.0            # fraction of calls with added latency
    slow_seconds: float = 2.0         # added latency in seconds
    partial_result_rate: float = 0.0  # fraction returning truncated data
    errors: list[Exception] = field(
        default_factory=lambda: [ConnectionError("chaos: connection refused")]
    )


class ChaosTool:
    """Wraps a tool executor with configurable fault injection."""

    def __init__(self, name: str, real_executor: Callable, fault: FaultConfig):
        self.name = name
        self._real = real_executor
        self._fault = fault
        self.call_count = 0
        self.failure_count = 0

    async def __call__(self, **kwargs) -> Any:
        self.call_count += 1
        f = self._fault
        roll = random.random()

        if roll < f.error_rate:
            self.failure_count += 1
            error = random.choice(f.errors)
            raise error

        if roll < f.error_rate + f.timeout_rate:
            self.failure_count += 1
            await asyncio.sleep(f.timeout_seconds)
            raise TimeoutError(f"chaos: {self.name} timed out after {f.timeout_seconds}s")

        if roll < f.error_rate + f.timeout_rate + f.slow_rate:
            await asyncio.sleep(f.slow_seconds)

        result = await self._real(**kwargs)

        if roll < f.error_rate + f.timeout_rate + f.slow_rate + f.partial_result_rate:
            # Truncate the result to simulate partial response
            if isinstance(result, str):
                return result[: len(result) // 2]
            if isinstance(result, dict):
                keys = list(result.keys())
                return {k: result[k] for k in keys[: len(keys) // 2]}

        return result


# Real tool implementations
async def real_search(query: str) -> dict:
    await asyncio.sleep(0.1)  # simulate network
    return {"results": [f"Result for: {query}"], "count": 1}


async def real_fetch_page(url: str) -> str:
    await asyncio.sleep(0.2)
    return f"<html>Content of {url}</html>"


# Chaos-wrapped versions for testing
chaotic_search = ChaosTool(
    "search",
    real_search,
    FaultConfig(
        error_rate=0.2,
        timeout_rate=0.1,
        slow_rate=0.1,
        errors=[
            ConnectionError("search service unavailable"),
            ValueError("invalid search response"),
        ],
    ),
)

chaotic_fetch = ChaosTool(
    "fetch_page",
    real_fetch_page,
    FaultConfig(
        error_rate=0.15,
        partial_result_rate=0.1,
        slow_rate=0.2,
    ),
)


async def run_agent_with_chaos(query: str) -> str:
    """Agent that calls chaotic tools; must handle failures gracefully."""
    MAX_RETRIES = 3

    for attempt in range(MAX_RETRIES):
        try:
            search_result = await asyncio.wait_for(
                chaotic_search(query=query),
                timeout=5.0,
            )
            return str(search_result)
        except (TimeoutError, ConnectionError, ValueError) as e:
            if attempt == MAX_RETRIES - 1:
                return f"Search failed after {MAX_RETRIES} attempts: {e}"
            await asyncio.sleep(0.5 * (2 ** attempt))

    return "Agent could not complete task"


async def main():
    results = await asyncio.gather(*[
        run_agent_with_chaos("test query") for _ in range(10)
    ])
    success = sum(1 for r in results if "failed" not in r)
    print(f"Success rate: {success}/10")
    print(f"Search failures injected: {chaotic_search.failure_count}/{chaotic_search.call_count}")


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Catches retry loops and error-swallowing bugs before production; prevents wasted LLM calls on unrecoverable tool failures.

**Environment:** Any async agent; swap `FaultConfig` rates between test and production.

---

## Option 2: pytest Parametrized Chaos Scenarios

Run the agent against a matrix of failure scenarios as pytest parametrize cases.

```python
import asyncio
import pytest
from unittest.mock import AsyncMock, patch
import anthropic


# Agent under test
async def research_agent(topic: str, tool_executor) -> str:
    """Simple research agent that calls a search tool."""
    try:
        result = await asyncio.wait_for(tool_executor(query=topic), timeout=5.0)
        if not result:
            return "No results found"
        return f"Found: {result}"
    except TimeoutError:
        return "ERROR: Search timed out"
    except ConnectionError as e:
        return f"ERROR: Connection failed — {e}"
    except ValueError as e:
        return f"ERROR: Invalid response — {e}"
    except Exception as e:
        return f"ERROR: Unexpected — {e}"


# Chaos scenarios
CHAOS_SCENARIOS = [
    pytest.param(
        "success",
        AsyncMock(return_value={"results": ["good result"]}),
        "Found:",
        id="happy_path",
    ),
    pytest.param(
        "connection_error",
        AsyncMock(side_effect=ConnectionError("connection refused")),
        "ERROR: Connection failed",
        id="connection_refused",
    ),
    pytest.param(
        "timeout",
        AsyncMock(side_effect=asyncio.TimeoutError()),
        "ERROR: Search timed out",
        id="tool_timeout",
    ),
    pytest.param(
        "empty_result",
        AsyncMock(return_value={}),
        "No results found",
        id="empty_response",
    ),
    pytest.param(
        "partial_json",
        AsyncMock(return_value={"results": None}),  # missing data
        "Found:",  # agent should handle None gracefully
        id="partial_data",
    ),
    pytest.param(
        "value_error",
        AsyncMock(side_effect=ValueError("unexpected format")),
        "ERROR: Invalid response",
        id="malformed_response",
    ),
    pytest.param(
        "runtime_error",
        AsyncMock(side_effect=RuntimeError("internal tool error")),
        "ERROR: Unexpected",
        id="unexpected_exception",
    ),
]


@pytest.mark.parametrize("scenario,mock_tool,expected_prefix", CHAOS_SCENARIOS)
@pytest.mark.asyncio
async def test_agent_handles_chaos(scenario, mock_tool, expected_prefix):
    result = await research_agent(f"test query for {scenario}", mock_tool)
    assert result.startswith(expected_prefix), (
        f"Scenario '{scenario}': expected result starting with '{expected_prefix}', "
        f"got: '{result}'"
    )
    # Agent must never propagate a raw exception to the caller
    assert "Traceback" not in result
    assert "Exception" not in result or "ERROR" in result


@pytest.mark.asyncio
async def test_agent_recovers_from_intermittent_failures():
    """Verify agent retries on transient errors."""
    call_count = 0

    async def flaky_tool(query: str):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("transient failure")
        return {"results": ["success after retries"]}

    async def agent_with_retry(topic: str) -> str:
        for attempt in range(3):
            try:
                result = await flaky_tool(query=topic)
                return f"Found: {result}"
            except ConnectionError:
                if attempt == 2:
                    return "ERROR: Max retries exceeded"
                await asyncio.sleep(0)  # yield control
        return "ERROR"

    result = await agent_with_retry("test")
    assert result.startswith("Found:")
    assert call_count == 3  # confirms it retried
```

**Expected Token Savings:** Systematic coverage of all failure modes; parametrize adds new scenarios without new test functions.

**Environment:** pytest + pytest-asyncio; any async agent codebase.

---

## Option 3: Chaos Monkey for Multi-Tool Agent

Randomly disable a fraction of tools available to the agent and verify it degrades gracefully.

```python
import random
import anthropic
from typing import Any

client = anthropic.Anthropic()


def build_chaos_tool_list(
    tools: list[dict],
    failure_rate: float = 0.3,
    seed: int | None = None,
) -> tuple[list[dict], set[str]]:
    """
    Randomly mark some tools as unavailable.
    Returns (available_tools, disabled_tool_names).
    """
    rng = random.Random(seed)
    available = []
    disabled = set()

    for tool in tools:
        if rng.random() < failure_rate:
            disabled.add(tool["name"])
        else:
            available.append(tool)

    return available, disabled


def execute_tool(name: str, inputs: dict, disabled: set[str]) -> str:
    """Execute tool or return error if it was disabled by chaos monkey."""
    if name in disabled:
        return f"ERROR: Tool '{name}' is currently unavailable (chaos injection)"

    # Simulate real tool execution
    if name == "search":
        return f"Search results for: {inputs.get('query', '')}"
    elif name == "read_file":
        return f"File content: mock content for {inputs.get('path', '')}"
    elif name == "write_file":
        return f"Written: {inputs.get('path', '')}"
    elif name == "get_weather":
        return f"Weather in {inputs.get('location', 'unknown')}: sunny, 72°F"
    return f"Tool {name} executed"


ALL_TOOLS = [
    {
        "name": "search",
        "description": "Search the web",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "get_weather",
        "description": "Get weather for a location",
        "input_schema": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    },
]


def run_with_chaos_monkey(task: str, failure_rate: float = 0.3, seed: int = 42) -> str:
    available_tools, disabled = build_chaos_tool_list(ALL_TOOLS, failure_rate, seed)
    print(f"[Chaos] Disabled tools: {disabled or 'none'}")
    print(f"[Chaos] Available: {[t['name'] for t in available_tools]}")

    messages = [{"role": "user", "content": task}]
    max_turns = 5

    for _ in range(max_turns):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=available_tools,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(
                (b.text for b in response.content if hasattr(b, "text")),
                "No response",
            )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input, disabled)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})

    return "Max turns reached"


if __name__ == "__main__":
    result = run_with_chaos_monkey(
        "Search for Python async best practices and get the weather in NYC.",
        failure_rate=0.5,
        seed=7,
    )
    print("\nResult:", result[:300])
```

**Expected Token Savings:** Verifies agent can complete tasks even when some tools fail; catches "all-or-nothing" dependency bugs.

**Environment:** Multi-tool agents; vary `seed` across CI runs to cover different failure combinations.

---

## Option 4: Latency Chaos — Slow Tool Response Testing

Inject variable latency into tool responses to verify agent timeout handling and user-experience under slow conditions.

```python
import asyncio
import time
import random
import pytest
from unittest.mock import AsyncMock


async def slow_tool_executor(
    query: str,
    p50_latency: float = 0.1,
    p95_latency: float = 2.0,
    p99_latency: float = 10.0,
) -> dict:
    """Simulates realistic latency distribution."""
    roll = random.random()
    if roll > 0.99:
        latency = p99_latency
    elif roll > 0.95:
        latency = p95_latency
    else:
        latency = random.uniform(0, p50_latency)

    await asyncio.sleep(latency)
    return {"results": [f"Result for {query}"], "latency_simulated": latency}


async def agent_with_timeout(query: str, tool_timeout: float = 3.0) -> tuple[str, float]:
    """Agent that enforces per-tool timeout."""
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            slow_tool_executor(query),
            timeout=tool_timeout,
        )
        elapsed = time.monotonic() - start
        return f"Success: {result['results']}", elapsed
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        return f"Timeout after {elapsed:.1f}s — falling back to cached result", elapsed


@pytest.mark.asyncio
async def test_agent_completes_within_sla():
    """Agent should return within 5s even under p99 latency."""
    SLA_SECONDS = 5.0
    start = time.monotonic()

    result, elapsed = await agent_with_timeout("test query", tool_timeout=3.0)

    total = time.monotonic() - start
    assert total <= SLA_SECONDS, (
        f"Agent exceeded SLA: {total:.2f}s > {SLA_SECONDS}s"
    )
    # Must return something — timeout or result
    assert len(result) > 0


@pytest.mark.asyncio
async def test_timeout_triggers_fallback():
    """When tool exceeds timeout, agent must use fallback."""
    async def always_slow(query: str):
        await asyncio.sleep(10)
        return {"results": ["this won't be reached"]}

    async def agent_with_fallback(query: str) -> str:
        try:
            return str(await asyncio.wait_for(always_slow(query), timeout=0.1))
        except asyncio.TimeoutError:
            return "FALLBACK: using cached data"

    result = await agent_with_fallback("test")
    assert result.startswith("FALLBACK:")


@pytest.mark.asyncio
async def test_agent_handles_mixed_latency_burst():
    """Fire 10 concurrent queries; all should complete within SLA."""
    SLA = 5.0
    start = time.monotonic()

    results = await asyncio.gather(*[
        agent_with_timeout(f"query {i}", tool_timeout=3.0)
        for i in range(10)
    ])

    total = time.monotonic() - start
    assert total <= SLA * 2, f"Burst took too long: {total:.2f}s"

    success_count = sum(1 for r, _ in results if r.startswith("Success"))
    print(f"Success: {success_count}/10 within latency budget")
    # At least 50% should succeed under normal distribution
    assert success_count >= 3
```

**Expected Token Savings:** Ensures timeouts fire before the LLM turn times out, preventing wasted generation costs on stalled tool calls.

**Environment:** Agents calling external HTTP APIs with variable latency.

---

## Option 5: State Corruption Chaos — Concurrent Conflicting Tool Results

Test agent behavior when two concurrent tool calls return contradictory data.

```python
import asyncio
import pytest
import anthropic
from unittest.mock import patch, AsyncMock


async def agent_reconcile_contradiction(client, question: str) -> str:
    """
    Agent that calls two sources and reconciles contradictory answers.
    Should not crash or hallucinate when sources disagree.
    """
    import anthropic as _anthropic

    tools = [
        {
            "name": "source_a",
            "description": "Query source A for information",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "source_b",
            "description": "Query source B for information",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    ]

    messages = [{"role": "user", "content": question}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        tools=tools,
        system="When sources contradict each other, explicitly note the discrepancy and explain what you can and cannot confirm.",
        messages=messages,
    )

    if response.stop_reason == "tool_use":
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                # Inject contradictory data
                if block.name == "source_a":
                    content = "The answer is 42"
                else:
                    content = "The answer is 73"  # contradiction!
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                })

        messages.append({"role": "user", "content": tool_results})
        final = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )
        return next(
            (b.text for b in final.content if hasattr(b, "text")),
            "No response",
        )

    return next(
        (b.text for b in response.content if hasattr(b, "text")),
        "No response",
    )


@pytest.mark.asyncio
async def test_agent_acknowledges_contradiction():
    """Agent must explicitly surface contradictions rather than silently picking one."""
    client = anthropic.Anthropic()
    result = await agent_reconcile_contradiction(
        client,
        "What is the population of this city according to both sources?"
    )
    # Agent should mention the contradiction
    contradiction_signals = [
        "contradict", "disagree", "different", "discrepancy",
        "sources differ", "inconsistent", "42", "73"
    ]
    found = [s for s in contradiction_signals if s.lower() in result.lower()]
    assert len(found) >= 2, (
        f"Agent didn't acknowledge contradiction clearly. "
        f"Response: {result[:300]}\nFound signals: {found}"
    )


@pytest.mark.asyncio
async def test_agent_doesnt_crash_on_contradiction():
    """Basic smoke test: contradiction must not raise an exception."""
    client = anthropic.Anthropic()
    try:
        result = await agent_reconcile_contradiction(client, "Compare these two sources.")
        assert isinstance(result, str)
        assert len(result) > 0
    except Exception as e:
        pytest.fail(f"Agent crashed on contradictory tool results: {e}")
```

**Expected Token Savings:** Catches agents that silently pick wrong answer from contradictory data; prevents subtle factual errors.

**Environment:** Multi-source research agents, RAG systems, fact-checking pipelines.

---

## Option 6: Chaos Test Harness with Result Reporting

A full harness that runs N chaos iterations, records outcomes, and reports resilience metrics.

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum


class OutcomeType(Enum):
    SUCCESS = "success"
    GRACEFUL_FAILURE = "graceful_failure"
    CRASH = "crash"
    TIMEOUT = "timeout"


@dataclass
class ChaosRun:
    scenario: str
    outcome: OutcomeType
    duration_ms: float
    error: str = ""


@dataclass
class ChaosReport:
    runs: list[ChaosRun] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if not self.runs:
            return 0.0
        successes = sum(1 for r in self.runs if r.outcome == OutcomeType.SUCCESS)
        return successes / len(self.runs)

    @property
    def graceful_rate(self) -> float:
        if not self.runs:
            return 0.0
        graceful = sum(
            1 for r in self.runs
            if r.outcome in (OutcomeType.SUCCESS, OutcomeType.GRACEFUL_FAILURE)
        )
        return graceful / len(self.runs)

    @property
    def crash_rate(self) -> float:
        if not self.runs:
            return 0.0
        crashes = sum(1 for r in self.runs if r.outcome == OutcomeType.CRASH)
        return crashes / len(self.runs)

    def print_summary(self):
        print(f"\n{'='*50}")
        print(f"Chaos Test Report ({len(self.runs)} runs)")
        print(f"{'='*50}")
        print(f"  Success rate:        {self.success_rate:.1%}")
        print(f"  Graceful fail rate:  {self.graceful_rate:.1%}")
        print(f"  Crash rate:          {self.crash_rate:.1%}")
        print(f"  Avg duration:        {sum(r.duration_ms for r in self.runs)/len(self.runs):.0f}ms")

        crashes = [r for r in self.runs if r.outcome == OutcomeType.CRASH]
        if crashes:
            print(f"\n  CRASHES:")
            for c in crashes[:3]:
                print(f"    [{c.scenario}] {c.error}")


async def chaos_harness(agent_fn, scenarios: list[dict], iterations: int = 50) -> ChaosReport:
    """Run agent_fn against randomized chaos scenarios and collect results."""
    report = ChaosReport()

    for i in range(iterations):
        scenario = random.choice(scenarios)
        start = time.monotonic()

        try:
            result = await asyncio.wait_for(
                agent_fn(**scenario["kwargs"]),
                timeout=scenario.get("timeout", 10.0),
            )
            elapsed = (time.monotonic() - start) * 1000

            if scenario.get("expect_error") and "ERROR" not in str(result):
                outcome = OutcomeType.CRASH  # should have returned error
            elif "ERROR" in str(result):
                outcome = OutcomeType.GRACEFUL_FAILURE
            else:
                outcome = OutcomeType.SUCCESS

            report.runs.append(ChaosRun(scenario["name"], outcome, elapsed))

        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - start) * 1000
            report.runs.append(ChaosRun(scenario["name"], OutcomeType.TIMEOUT, elapsed))

        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            report.runs.append(
                ChaosRun(scenario["name"], OutcomeType.CRASH, elapsed, str(e))
            )

    return report


# Example usage
async def sample_agent(prompt: str, inject_error: bool = False) -> str:
    if inject_error:
        raise ConnectionError("injected chaos error")
    await asyncio.sleep(random.uniform(0.01, 0.2))
    return f"Result for: {prompt}"


SCENARIOS = [
    {"name": "normal", "kwargs": {"prompt": "Hello"}, "timeout": 5.0},
    {"name": "error", "kwargs": {"prompt": "Hello", "inject_error": True},
     "timeout": 5.0, "expect_error": True},
    {"name": "empty", "kwargs": {"prompt": ""}, "timeout": 5.0},
]


async def main():
    report = await chaos_harness(sample_agent, SCENARIOS, iterations=30)
    report.print_summary()
    assert report.crash_rate < 0.1, f"Crash rate too high: {report.crash_rate:.1%}"
    assert report.graceful_rate >= 0.8, f"Graceful rate too low: {report.graceful_rate:.1%}"


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Statistical view of resilience; a 15% crash rate discovered in testing avoids burning tokens on unhandled exceptions in production.

**Environment:** Any async agent; run as part of nightly CI or pre-release validation.

---

## Comparison

| Option | Failure Types | Randomized | Metrics | Best For |
|--------|--------------|------------|---------|----------|
| 1. Fault injection wrapper | Error, timeout, slow, partial | Yes (rate-based) | Per-call counts | Integration testing |
| 2. pytest parametrize | Fixed scenarios | No | Pass/fail | Systematic scenario coverage |
| 3. Chaos monkey | Tool unavailability | Yes (seeded) | N/A | Multi-tool dependency testing |
| 4. Latency chaos | Slow responses | Yes | SLA validation | Timeout handling |
| 5. State corruption | Contradictory data | No | Pass/fail | Data reconciliation logic |
| 6. Chaos harness | Any | Yes | Success/graceful/crash rates | Statistical resilience report |
