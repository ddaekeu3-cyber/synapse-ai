---
layout: solution
title: "Agent Doesn't Implement Test Parallelization Strategy"
description: "How to parallelize agent test suites to reduce wall-clock time while maintaining isolation, reproducibility, and accurate coverage."
tags: [testing, parallelization, pytest, asyncio, performance, ci]
difficulty: intermediate
solution_count: 6
---

## Problem

Agent test suites run sequentially by default. Each test waits for the previous one to finish, even when tests are completely independent. A suite with 200 tests and average 2-second LLM calls takes 400+ seconds. This slows CI feedback loops, discourages developers from running tests locally, and makes test-driven development impractical for agent workflows.

```python
# Bad: sequential test execution wastes time
# pytest runs test_a -> test_b -> test_c one at a time
def test_summarize(): ...      # 2s
def test_classify(): ...       # 2s
def test_extract_entities(): ...# 2s
# Total: 6s — but all are independent, should take ~2s
```

---

## Solution 1 — pytest-xdist for Multi-Process Parallelism

Use `pytest-xdist` to distribute tests across multiple worker processes. Each worker gets an isolated Python interpreter, preventing shared-state contamination.

```python
# pyproject.toml
# [tool.pytest.ini_options]
# addopts = "-n auto"   # auto-detect CPU count

# conftest.py
import pytest
import os

def pytest_configure(config):
    """Ensure each worker gets a unique temp directory."""
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    os.environ["AGENT_TEST_WORKER"] = worker_id

@pytest.fixture(scope="session")
def worker_id():
    return os.environ.get("PYTEST_XDIST_WORKER", "master")

@pytest.fixture(scope="session")
def tmp_db_path(worker_id, tmp_path_factory):
    """Each worker gets its own isolated database file."""
    return tmp_path_factory.mktemp("db") / f"test_{worker_id}.db"

# Test file — these run in parallel workers
class TestSummarization:
    def test_short_text(self, agent_client):
        result = agent_client.summarize("Hello world")
        assert len(result) < 50

    def test_long_text(self, agent_client):
        result = agent_client.summarize("A" * 10000)
        assert len(result) < 500

class TestClassification:
    def test_positive_sentiment(self, agent_client):
        result = agent_client.classify("I love this product!")
        assert result["label"] == "positive"

# Run with: pytest -n auto --dist=worksteal
# worksteal distributes tests dynamically so fast workers take more tests
```

```bash
# Benchmark: measure speedup
time pytest tests/ -n 1 2>&1 | tail -1   # sequential baseline
time pytest tests/ -n auto 2>&1 | tail -1 # parallel — typically 3-8x faster
```

---

## Solution 2 — asyncio Parallel Test Execution with pytest-asyncio

For async agent tests, run multiple coroutines concurrently within a single test process using `asyncio.gather`.

```python
import asyncio
import pytest
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def invoke_agent(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()

# Helper: run multiple agent calls concurrently in one test
async def run_parallel_assertions(cases: list[tuple[str, callable]]) -> None:
    """Run all (prompt, assertion_fn) pairs concurrently."""
    prompts = [c[0] for c in cases]
    fns = [c[1] for c in cases]
    results = await asyncio.gather(*[invoke_agent(p) for p in prompts])
    for result, fn in zip(results, fns):
        fn(result)

@pytest.mark.asyncio
async def test_sentiment_batch():
    """Test 5 sentiment cases concurrently — takes ~1 LLM RTT instead of 5."""
    await run_parallel_assertions([
        ("Is 'I love this!' positive or negative? Answer one word.", lambda r: assert_contains(r, "positive")),
        ("Is 'I hate this!' positive or negative? Answer one word.", lambda r: assert_contains(r, "negative")),
        ("Is 'It is okay.' positive or negative? Answer one word.", lambda r: r.strip()),
        ("Is 'Amazing product!' positive or negative? Answer one word.", lambda r: assert_contains(r, "positive")),
        ("Is 'Terrible experience.' positive or negative? Answer one word.", lambda r: assert_contains(r, "negative")),
    ])

def assert_contains(text: str, substring: str) -> None:
    assert substring.lower() in text.lower(), f"Expected '{substring}' in '{text}'"

@pytest.mark.asyncio
async def test_extraction_batch():
    """Extract entities from multiple texts in parallel."""
    texts = [
        "Apple Inc. was founded by Steve Jobs.",
        "Google was founded in Menlo Park, California.",
        "Tesla was founded by Elon Musk.",
    ]
    results = await asyncio.gather(*[
        invoke_agent(f"Extract all named entities from: '{text}'. JSON list only.")
        for text in texts
    ])
    for result in results:
        assert "[" in result or "{" in result  # should be JSON-ish
```

---

## Solution 3 — Test Sharding for CI Matrix Parallelism

Split tests into N shards and run each shard in a separate CI job. Each shard runs sequentially within itself but all shards run in parallel across CI jobs.

```python
# conftest.py
import pytest
import os
import hashlib

def pytest_collection_modifyitems(config, items):
    """Distribute tests across shards by stable hash of test node id."""
    total_shards = int(os.environ.get("PYTEST_TOTAL_SHARDS", "1"))
    shard_index = int(os.environ.get("PYTEST_SHARD_INDEX", "0"))

    if total_shards <= 1:
        return  # no sharding

    selected = []
    deselected = []
    for item in items:
        # Hash the test id for stable, even distribution
        h = int(hashlib.md5(item.nodeid.encode()).hexdigest(), 16)
        if h % total_shards == shard_index:
            selected.append(item)
        else:
            deselected.append(item)

    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
    print(f"\nShard {shard_index}/{total_shards}: running {len(selected)}/{len(items)+len(deselected)} tests")
```

```yaml
# .github/workflows/test.yml
name: Parallel Tests
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [0, 1, 2, 3]  # 4 parallel jobs
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[test]"
      - run: pytest tests/
        env:
          PYTEST_TOTAL_SHARDS: 4
          PYTEST_SHARD_INDEX: ${{ matrix.shard }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

---

## Solution 4 — Parallel Test Fixture Setup with asyncio.TaskGroup

When fixtures require slow async initialization (DB connections, agent warm-up), initialize them all concurrently.

```python
import asyncio
import pytest
from anthropic import AsyncAnthropic
from dataclasses import dataclass

@dataclass
class AgentTestFixtures:
    client: AsyncAnthropic
    vector_store: Any
    mock_tools: dict
    system_prompt: str

async def init_client() -> AsyncAnthropic:
    return AsyncAnthropic()

async def init_vector_store() -> Any:
    await asyncio.sleep(0.1)  # simulates slow embedding model load
    return {"index": "loaded"}

async def init_mock_tools() -> dict:
    await asyncio.sleep(0.05)  # simulates tool schema loading
    return {"search": lambda q: f"results for {q}"}

async def load_system_prompt() -> str:
    await asyncio.sleep(0.02)  # simulates file read + template render
    return "You are a helpful agent."

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="session")
async def agent_fixtures() -> AgentTestFixtures:
    """Initialize all fixtures concurrently — faster than sequential."""
    async with asyncio.TaskGroup() as tg:
        t_client = tg.create_task(init_client())
        t_store = tg.create_task(init_vector_store())
        t_tools = tg.create_task(init_mock_tools())
        t_prompt = tg.create_task(load_system_prompt())

    return AgentTestFixtures(
        client=t_client.result(),
        vector_store=t_store.result(),
        mock_tools=t_tools.result(),
        system_prompt=t_prompt.result(),
    )

@pytest.fixture
def agent_client(agent_fixtures):
    return agent_fixtures.client

@pytest.mark.asyncio
async def test_with_parallel_fixtures(agent_fixtures):
    # All fixtures were initialized concurrently — ~0.1s instead of ~0.17s
    assert agent_fixtures.client is not None
    assert agent_fixtures.vector_store["index"] == "loaded"
    assert "search" in agent_fixtures.mock_tools
```

---

## Solution 5 — Parallel Property-Based Testing with Hypothesis

Use Hypothesis with custom profiles for parallel execution, running more examples concurrently during CI.

```python
import asyncio
import pytest
from hypothesis import given, settings, HealthCheck, Phase
from hypothesis import strategies as st

# Hypothesis profiles
from hypothesis import settings as h_settings, HealthCheck

h_settings.register_profile("ci_parallel",
    max_examples=200,
    deadline=5000,  # 5s deadline per example
    suppress_health_check=[HealthCheck.too_slow],
    phases=[Phase.generate, Phase.shrink],
)
h_settings.register_profile("local",
    max_examples=50,
    deadline=2000,
)

import os
h_settings.load_profile("ci_parallel" if os.environ.get("CI") else "local")

# Parallel hypothesis runner
async def run_hypothesis_batch(strategy, test_fn, count: int = 20) -> list[dict]:
    """Draw N examples and test them concurrently."""
    from hypothesis.strategies import SearchStrategy
    import hypothesis._settings as _hs

    examples = [strategy.example() for _ in range(count)]
    results = await asyncio.gather(
        *[test_fn(ex) for ex in examples],
        return_exceptions=True,
    )
    failures = [
        {"example": ex, "error": str(r)}
        for ex, r in zip(examples, results)
        if isinstance(r, Exception)
    ]
    return failures

@pytest.mark.asyncio
async def test_agent_handles_any_text():
    """Test that agent never crashes on arbitrary text input."""
    text_strategy = st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
        min_size=1,
        max_size=500,
    )

    async def check_one(text: str) -> None:
        # Replace with real agent call in production
        assert isinstance(text, str)
        assert len(text) > 0

    failures = await run_hypothesis_batch(text_strategy, check_one, count=50)
    assert not failures, f"Failures: {failures[:3]}"

@given(
    text=st.text(min_size=1, max_size=200),
    temperature=st.floats(min_value=0.0, max_value=1.0),
)
@settings(max_examples=100)
def test_summarizer_properties(text: str, temperature: float):
    """Property: summary is always shorter than input (for long inputs)."""
    if len(text) < 50:
        pytest.skip("Too short to summarize")
    # Mock check — replace with real agent call
    summary = text[:len(text)//2]
    assert len(summary) <= len(text)
```

---

## Solution 6 — Test Queue with Worker Pool for LLM Rate Limit Awareness

Run tests through a shared async worker pool that respects LLM API rate limits while maximizing throughput.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from anthropic import AsyncAnthropic, RateLimitError

client = AsyncAnthropic()

@dataclass
class TestCase:
    name: str
    coro_fn: Callable[[], Awaitable[Any]]
    priority: int = 0  # lower = higher priority

@dataclass
class TestResult:
    name: str
    passed: bool
    error: str = ""
    duration: float = 0.0

class RateLimitAwareTestRunner:
    def __init__(self, max_concurrent: int = 5, requests_per_minute: int = 50):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._rpm = requests_per_minute
        self._request_times: list[float] = []
        self._lock = asyncio.Lock()

    async def _wait_for_rate_limit(self) -> None:
        async with self._lock:
            now = time.monotonic()
            # Remove timestamps older than 60s
            self._request_times = [t for t in self._request_times if now - t < 60]
            if len(self._request_times) >= self._rpm:
                sleep_time = 60 - (now - self._request_times[0]) + 0.1
                await asyncio.sleep(sleep_time)
            self._request_times.append(time.monotonic())

    async def _run_one(self, case: TestCase) -> TestResult:
        start = time.monotonic()
        async with self._semaphore:
            await self._wait_for_rate_limit()
            for attempt in range(3):
                try:
                    await case.coro_fn()
                    return TestResult(case.name, passed=True,
                                      duration=time.monotonic() - start)
                except RateLimitError:
                    await asyncio.sleep(2 ** attempt)
                except AssertionError as e:
                    return TestResult(case.name, passed=False, error=str(e),
                                      duration=time.monotonic() - start)
                except Exception as e:
                    return TestResult(case.name, passed=False, error=str(e),
                                      duration=time.monotonic() - start)
        return TestResult(case.name, passed=False, error="max retries exceeded",
                          duration=time.monotonic() - start)

    async def run_all(self, cases: list[TestCase]) -> list[TestResult]:
        sorted_cases = sorted(cases, key=lambda c: c.priority)
        results = await asyncio.gather(*[self._run_one(c) for c in sorted_cases])
        return list(results)

    def report(self, results: list[TestResult]) -> None:
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        total_time = sum(r.duration for r in results)
        wall_time = max(r.duration for r in results) if results else 0
        print(f"\n{passed}/{total} passed | "
              f"wall={wall_time:.1f}s | "
              f"saved={total_time - wall_time:.1f}s vs sequential")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.name} ({r.duration:.2f}s)"
                  + (f" — {r.error}" if r.error else ""))

# Usage
async def test_summarize():
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": "Summarize: 'The sky is blue.'"}],
    )
    assert len(resp.content[0].text) > 0

async def main():
    runner = RateLimitAwareTestRunner(max_concurrent=5, requests_per_minute=50)
    cases = [
        TestCase(f"test_summarize_{i}", test_summarize, priority=i)
        for i in range(20)
    ]
    results = await runner.run_all(cases)
    runner.report(results)

asyncio.run(main())
```

---

## Comparison

| Approach | Isolation | Setup Complexity | LLM API Aware | CI Integration | Best For |
|---|---|---|---|---|---|
| pytest-xdist | **Process-level** | Low (1 flag) | No | Simple | General test suites |
| asyncio gather | Coroutine | Low | Partial | Any | Async-heavy agent tests |
| Test sharding | **Process-level** | Medium (CI config) | No | **Matrix jobs** | Large suites, CI cost control |
| Parallel fixtures | Coroutine | Medium | No | Any | Slow fixture initialization |
| Hypothesis parallel | Coroutine | Medium | No | Any | Property-based testing |
| Rate-limit runner | Coroutine | High | **Yes** | Custom | API quota-bound test suites |
