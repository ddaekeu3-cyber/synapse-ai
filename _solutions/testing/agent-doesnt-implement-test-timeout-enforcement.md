---
layout: solution
title: "Agent Doesn't Implement Test Timeout Enforcement"
category: testing
description: "Agent tests that hang indefinitely waste CI minutes and block deployments. Test timeout enforcement kills stuck tests at a configurable deadline, reports them as failures, and prevents flaky infinite waits from polluting the test suite."
tags: [testing, timeout, ci, pytest, async, asyncio, reliability, flaky-tests]
---

# Agent Doesn't Implement Test Timeout Enforcement

## Problem

Agent tests that call external APIs, wait for tool responses, or run multi-step loops can hang indefinitely when something goes wrong. A single hung test blocks the entire CI pipeline. Without explicit timeouts, a network hiccup turns a 2-second test into a 10-minute stall that kills your deploy.

Test timeout enforcement kills hung tests at a deadline, reports them as failures, and gives you actionable diagnostics.

---

## Option 1: Simple asyncio.wait_for Timeout Wrapper

```python
import asyncio
import anthropic
import pytest

AGENT_TEST_TIMEOUT_SEC = 30.0


async def run_agent_call(prompt: str, max_tokens: int = 128) -> str:
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def run_with_timeout(coro, timeout: float = AGENT_TEST_TIMEOUT_SEC) -> str:
    """Wrap any coroutine with a hard timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Agent call exceeded {timeout}s timeout")


# ---- Tests ----

@pytest.mark.asyncio
async def test_simple_response_within_timeout():
    result = await run_with_timeout(
        run_agent_call("What is 2+2?"),
        timeout=AGENT_TEST_TIMEOUT_SEC,
    )
    assert result
    assert len(result) > 0


@pytest.mark.asyncio
async def test_multi_turn_within_timeout():
    client = anthropic.AsyncAnthropic()
    messages = [{"role": "user", "content": "What is Python?"}]

    async def multi_turn():
        r1 = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": r1.content[0].text})
        messages.append({"role": "user", "content": "Give one example."})
        r2 = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=messages,
        )
        return r2.content[0].text

    result = await run_with_timeout(multi_turn(), timeout=AGENT_TEST_TIMEOUT_SEC)
    assert result


@pytest.mark.asyncio
async def test_timeout_fires_on_slow_operation():
    """Verify that timeout actually fires when an operation is too slow."""
    async def slow_operation():
        await asyncio.sleep(5.0)  # Simulates a hung call
        return "never"

    with pytest.raises(TimeoutError, match="exceeded"):
        await run_with_timeout(slow_operation(), timeout=0.1)


if __name__ == "__main__":
    async def main():
        print("Testing simple response...")
        r = await run_with_timeout(run_agent_call("What is Python?"))
        print(f"✓ Response: {r[:60]}")

        print("\nTesting timeout fires...")
        try:
            await run_with_timeout(asyncio.sleep(10), timeout=0.1)
        except TimeoutError as e:
            print(f"✓ Timeout fired: {e}")

    asyncio.run(main())
# Expected Token Savings: None — timeout prevents hung tests from consuming CI minutes
# Environment: pip install anthropic pytest pytest-asyncio
```

---

## Option 2: pytest Timeout Fixture with Per-Test Budgets

```python
import asyncio
import time
import pytest
import anthropic
from dataclasses import dataclass

@dataclass
class TimeoutConfig:
    default_sec: float = 30.0
    fast_sec: float = 10.0
    slow_sec: float = 60.0


@pytest.fixture
def timeout_config() -> TimeoutConfig:
    return TimeoutConfig()


@pytest.fixture
def agent_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


class TimedTestRunner:
    """Runs synchronous agent tests with wall-clock timeout enforcement."""

    def __init__(self, timeout_sec: float = 30.0):
        self.timeout_sec = timeout_sec
        self._start: float | None = None

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.monotonic() - (self._start or 0)
        if elapsed > self.timeout_sec:
            raise TimeoutError(
                f"Test took {elapsed:.1f}s, exceeded {self.timeout_sec}s budget"
            )
        return False

    def check(self, label: str = ""):
        elapsed = time.monotonic() - (self._start or 0)
        remaining = self.timeout_sec - elapsed
        if remaining < 1.0:
            raise TimeoutError(
                f"{'[' + label + '] ' if label else ''}Timeout: {elapsed:.1f}s elapsed, {remaining:.1f}s remaining"
            )
        return remaining


# ---- Tests ----

def test_fast_response(agent_client, timeout_config):
    """Fast test: should complete in <10s."""
    with TimedTestRunner(timeout_sec=timeout_config.fast_sec):
        r = agent_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": "Say 'hello'."}],
        )
        assert r.content[0].text


def test_multi_step_within_budget(agent_client, timeout_config):
    """Multi-step test with per-step checkpoint."""
    runner = TimedTestRunner(timeout_sec=timeout_config.default_sec)
    with runner:
        # Step 1
        runner.check("step1")
        r1 = agent_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": "What is Python?"}],
        )
        assert r1.content[0].text

        # Step 2
        runner.check("step2")
        r2 = agent_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": "Name a Python library."}],
        )
        assert r2.content[0].text


def test_timeout_budget_exceeded():
    """Verify TimeoutError is raised when budget is blown."""
    with pytest.raises(TimeoutError):
        runner = TimedTestRunner(timeout_sec=0.001)
        with runner:
            time.sleep(0.1)


def test_checkpoint_fires_on_slow_step():
    """Verify checkpoint fires before overall timeout."""
    runner = TimedTestRunner(timeout_sec=5.0)
    runner.__enter__()
    time.sleep(4.5)  # Nearly exhaust budget
    with pytest.raises(TimeoutError, match="remaining"):
        runner.check("late_step")


if __name__ == "__main__":
    client = anthropic.Anthropic()
    cfg = TimeoutConfig()

    print("Running fast test...")
    with TimedTestRunner(cfg.fast_sec):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": "Hi."}],
        )
        print(f"✓ {r.content[0].text[:50]}")
# Expected Token Savings: None — timeout fixtures prevent CI time waste on hung API calls
# Environment: pip install anthropic pytest; time is stdlib
```

---

## Option 3: Async Test Suite with Global Timeout via pytest-timeout

```python
"""
pytest configuration for agent test timeout enforcement.

Install: pip install anthropic pytest pytest-asyncio pytest-timeout

In pytest.ini or pyproject.toml:
[pytest]
timeout = 30
asyncio_mode = auto

Or per-test with @pytest.mark.timeout(N).
"""
import asyncio
import pytest
import anthropic


# --- Agent Under Test ---

async def agent_pipeline(prompt: str) -> dict:
    """Multi-step agent pipeline that could potentially hang."""
    client = anthropic.AsyncAnthropic()

    # Step 1: classify intent
    classification = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        system="Classify the intent as: question, command, or statement. Reply with one word.",
        messages=[{"role": "user", "content": prompt}],
    )
    intent = classification.content[0].text.strip().lower()

    # Step 2: generate response based on intent
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=96,
        system=f"The user has a {intent}. Respond appropriately.",
        messages=[{"role": "user", "content": prompt}],
    )

    return {"intent": intent, "response": response.content[0].text}


# --- Tests ---

@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_pipeline_question():
    result = await agent_pipeline("What is machine learning?")
    assert "intent" in result
    assert "response" in result
    assert len(result["response"]) > 0


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_pipeline_command():
    result = await agent_pipeline("List three sorting algorithms.")
    assert result["response"]


@pytest.mark.asyncio
@pytest.mark.timeout(5)  # Very tight — expects fast response
async def test_single_word_prompt_fast():
    client = anthropic.AsyncAnthropic()
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{"role": "user", "content": "Hello."}],
    )
    assert r.content[0].text


@pytest.mark.asyncio
@pytest.mark.timeout(2)  # Intentionally short — tests timeout behavior
async def test_timeout_behavior():
    """
    This test would fail if the API call exceeds 2s.
    In CI, pytest-timeout kills it and marks as FAILED with 'Timeout'.
    """
    # In real tests, replace with actual agent call
    await asyncio.sleep(0.1)  # Fast enough to pass in demo
    assert True


# --- Timeout annotation examples ---

@pytest.mark.asyncio
@pytest.mark.timeout(45)  # Longer budget for batch tests
async def test_parallel_agent_calls():
    client = anthropic.AsyncAnthropic()
    prompts = ["What is Python?", "Name a fruit.", "What is REST?"]

    tasks = [
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": p}],
        )
        for p in prompts
    ]
    results = await asyncio.gather(*tasks)
    assert all(r.content[0].text for r in results)


if __name__ == "__main__":
    asyncio.run(agent_pipeline("What is Python?"))
# Expected Token Savings: None — pytest-timeout is zero-overhead until a test stalls
# Environment: pip install anthropic pytest pytest-asyncio pytest-timeout
```

---

## Option 4: SQLite Test Timing Database for Flaky Detection

```python
import sqlite3
import time
import asyncio
import json
import anthropic
from datetime import datetime

class TestTimingDB:
    """
    Records test execution times across runs.
    Detects tests that consistently approach their timeout budget
    (indicating they're becoming flaky before they start failing).
    """

    def __init__(self, db_path: str = "test_timings.db"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_name TEXT,
                duration_sec REAL,
                timeout_sec REAL,
                pct_of_budget REAL,
                passed INTEGER,
                error TEXT,
                run_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def record(self, test_name: str, duration_sec: float, timeout_sec: float,
               passed: bool, error: str = ""):
        pct = duration_sec / timeout_sec
        self.conn.execute(
            "INSERT INTO test_runs (test_name, duration_sec, timeout_sec, pct_of_budget, passed, error) VALUES (?,?,?,?,?,?)",
            (test_name, round(duration_sec, 3), timeout_sec, round(pct, 4), int(passed), error[:200]),
        )
        self.conn.commit()

    def flaky_candidates(self, min_pct: float = 0.80, min_runs: int = 3) -> list[dict]:
        """Tests that use >80% of their budget consistently are flaky candidates."""
        rows = self.conn.execute("""
            SELECT test_name,
                   COUNT(*) as runs,
                   AVG(pct_of_budget) as avg_pct,
                   MAX(pct_of_budget) as max_pct,
                   SUM(1 - passed) as failures
            FROM test_runs
            GROUP BY test_name
            HAVING runs >= ? AND avg_pct >= ?
            ORDER BY avg_pct DESC
        """, (min_runs, min_pct)).fetchall()
        return [
            {"test": r[0], "runs": r[1], "avg_pct": round(r[2], 3),
             "max_pct": round(r[3], 3), "failures": r[4]}
            for r in rows
        ]

    def recent_report(self, n: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT test_name, duration_sec, timeout_sec, pct_of_budget, passed FROM test_runs ORDER BY run_at DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [{"test": r[0], "duration_sec": r[1], "timeout_sec": r[2],
                 "pct_used": round(r[3] * 100, 1), "passed": bool(r[4])} for r in rows]


class TimeoutEnforcedTest:
    """Context manager for individual test cases with timing capture."""

    def __init__(self, name: str, timeout_sec: float, db: TestTimingDB):
        self.name = name
        self.timeout_sec = timeout_sec
        self.db = db
        self._start: float = 0.0
        self.passed = False
        self.error = ""

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = time.monotonic() - self._start
        self.passed = exc_type is None

        if not self.passed:
            self.error = str(exc_val)[:200]

        self.db.record(self.name, duration, self.timeout_sec, self.passed, self.error)

        if duration > self.timeout_sec:
            raise TimeoutError(f"Test '{self.name}' exceeded {self.timeout_sec}s (took {duration:.2f}s)")

        pct = duration / self.timeout_sec
        if pct > 0.85:
            print(f"⚠️  [{self.name}] Using {pct:.0%} of timeout budget ({duration:.2f}s/{self.timeout_sec}s)")

        return False


def run_timed_test_suite():
    db = TestTimingDB(db_path=":memory:")
    client = anthropic.Anthropic()

    TESTS = [
        ("test_simple_question",  "What is Python?",            15.0),
        ("test_short_answer",     "Say hello.",                  10.0),
        ("test_explanation",      "Explain REST APIs briefly.",  20.0),
        ("test_list_response",    "Name 3 sorting algorithms.",  15.0),
    ]

    print("Running test suite with timeout enforcement...")
    for test_name, prompt, timeout in TESTS:
        with TimeoutEnforcedTest(test_name, timeout, db) as t:
            r = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            )
            assert r.content[0].text, f"{test_name}: empty response"
            print(f"  ✓ {test_name}: {r.content[0].text[:50]}")

    print(f"\nRecent runs:")
    for run in db.recent_report():
        icon = "✓" if run["passed"] else "✗"
        print(f"  {icon} {run['test']} — {run['duration_sec']:.2f}s ({run['pct_used']}% of budget)")

    candidates = db.flaky_candidates(min_pct=0.5, min_runs=1)
    if candidates:
        print(f"\nFlaky candidates: {json.dumps(candidates, indent=2)}")
    else:
        print("\nNo flaky candidates detected.")


if __name__ == "__main__":
    run_timed_test_suite()
# Expected Token Savings: None — timing DB catches slow-drift before tests start timing out
# Environment: pip install anthropic; sqlite3, time, json are stdlib
```

---

## Option 5: Parallel Test Runner with Per-Test Timeout

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

@dataclass
class TestResult:
    name: str
    passed: bool
    duration_sec: float
    error: str | None = None
    output: Any = None

    def status(self) -> str:
        if not self.passed:
            return f"FAILED ({self.error[:60]})" if self.error else "FAILED"
        return f"PASSED ({self.duration_sec:.2f}s)"


async def run_test_with_timeout(
    name: str,
    coro_fn: Callable[[], Coroutine],
    timeout_sec: float,
) -> TestResult:
    start = time.monotonic()
    try:
        output = await asyncio.wait_for(coro_fn(), timeout=timeout_sec)
        duration = time.monotonic() - start
        return TestResult(name=name, passed=True, duration_sec=round(duration, 3), output=output)
    except asyncio.TimeoutError:
        duration = time.monotonic() - start
        return TestResult(
            name=name,
            passed=False,
            duration_sec=round(duration, 3),
            error=f"TimeoutError: exceeded {timeout_sec}s",
        )
    except Exception as e:
        duration = time.monotonic() - start
        return TestResult(name=name, passed=False, duration_sec=round(duration, 3), error=str(e))


async def run_parallel_test_suite() -> list[TestResult]:
    client = anthropic.AsyncAnthropic()

    async def make_call(prompt: str, max_tokens: int = 64) -> str:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return r.content[0].text

    # Test definitions: (name, coroutine_factory, timeout_sec)
    test_cases = [
        ("test_greeting",       lambda: make_call("Say hello."),                     15.0),
        ("test_factual_answer", lambda: make_call("What is the capital of France?"), 15.0),
        ("test_list",           lambda: make_call("Name 3 programming languages."),  20.0),
        ("test_long_response",  lambda: make_call("Describe Python.", max_tokens=256), 30.0),
        ("test_timeout_demo",   lambda: asyncio.sleep(5),                             0.5),  # Will timeout
    ]

    # Run all tests concurrently
    tasks = [
        run_test_with_timeout(name, fn, timeout)
        for name, fn, timeout in test_cases
    ]
    results = await asyncio.gather(*tasks)

    # Report
    print("\nTest Suite Results:")
    print("-" * 60)
    passed = 0
    for r in results:
        icon = "✓" if r.passed else "✗"
        print(f"  {icon} {r.name:<35} {r.status()}")
        if r.passed:
            passed += 1

    print("-" * 60)
    print(f"  {passed}/{len(results)} tests passed")

    failed = [r for r in results if not r.passed]
    if failed:
        print(f"\nFailures:")
        for r in failed:
            print(f"  {r.name}: {r.error}")

    return list(results)


if __name__ == "__main__":
    asyncio.run(run_parallel_test_suite())
# Expected Token Savings: None — parallel execution cuts CI wall time, not token count
# Environment: pip install anthropic; asyncio, time are stdlib
```

---

## Option 6: CI-Integrated Timeout with Structured Failure Reports

```python
import asyncio
import json
import sys
import time
import anthropic
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class CITestReport:
    suite_name: str
    started_at: str
    results: list[dict] = field(default_factory=list)
    total_timeout_sec: float = 120.0
    suite_start_ts: float = field(default_factory=time.monotonic)

    def add(self, name: str, passed: bool, duration_sec: float, error: str = "", output: str = ""):
        self.results.append({
            "name": name,
            "passed": passed,
            "duration_sec": round(duration_sec, 3),
            "error": error[:200] if error else None,
            "output_preview": output[:80] if output else None,
        })

    def suite_elapsed(self) -> float:
        return time.monotonic() - self.suite_start_ts

    def suite_remaining(self) -> float:
        return max(0.0, self.total_timeout_sec - self.suite_elapsed())

    def to_ci_json(self) -> dict:
        passed = sum(1 for r in self.results if r["passed"])
        failed = [r for r in self.results if not r["passed"]]
        return {
            "suite": self.suite_name,
            "started_at": self.started_at,
            "total": len(self.results),
            "passed": passed,
            "failed": len(failed),
            "duration_sec": round(self.suite_elapsed(), 2),
            "exit_code": 0 if not failed else 1,
            "failures": [{"name": r["name"], "error": r["error"]} for r in failed],
            "results": self.results,
        }

    def print_summary(self):
        report = self.to_ci_json()
        icon = "✓" if report["exit_code"] == 0 else "✗"
        print(f"\n{icon} Suite '{report['suite']}': {report['passed']}/{report['total']} passed in {report['duration_sec']}s")
        for f in report["failures"]:
            print(f"  FAIL: {f['name']} — {f['error']}")


async def run_ci_test_suite(suite_name: str = "agent-integration") -> int:
    report = CITestReport(
        suite_name=suite_name,
        started_at=datetime.utcnow().isoformat(),
        total_timeout_sec=120.0,
    )
    client = anthropic.AsyncAnthropic()

    TEST_SPECS = [
        ("test_basic_response",    "Say hello.",                         15.0),
        ("test_factual_question",  "What is the capital of Japan?",      15.0),
        ("test_code_generation",   "Write a Python hello world.",        20.0),
        ("test_summary",           "Summarize: Python is a language.",   15.0),
        ("test_suite_timeout",     "never" * 100,                        0.5),  # Will timeout
    ]

    for test_name, prompt, timeout in TEST_SPECS:
        remaining = report.suite_remaining()
        if remaining < 2.0:
            report.add(test_name, False, 0.0, "Suite timeout: no budget remaining")
            print(f"  ✗ {test_name}: skipped (suite timeout)")
            continue

        effective_timeout = min(timeout, remaining - 1.0)
        t0 = time.monotonic()
        try:
            r = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=effective_timeout,
            )
            duration = time.monotonic() - t0
            text = r.content[0].text
            report.add(test_name, True, duration, output=text)
            print(f"  ✓ {test_name} ({duration:.2f}s): {text[:50]}")

        except asyncio.TimeoutError:
            duration = time.monotonic() - t0
            report.add(test_name, False, duration, f"TimeoutError after {effective_timeout:.1f}s")
            print(f"  ✗ {test_name}: timeout after {duration:.2f}s")

        except Exception as e:
            duration = time.monotonic() - t0
            report.add(test_name, False, duration, str(e))
            print(f"  ✗ {test_name}: {e}")

    report.print_summary()

    # Write CI artifact
    ci_json = report.to_ci_json()
    print(f"\nCI JSON report:")
    print(json.dumps(ci_json, indent=2)[:600])

    return ci_json["exit_code"]


if __name__ == "__main__":
    exit_code = asyncio.run(run_ci_test_suite())
    sys.exit(exit_code)
# Expected Token Savings: None — CI artifact gives structured failure data for post-mortem
# Environment: pip install anthropic; asyncio, json, sys, time are stdlib
```

---

## Comparison

| Option | Timeout Type | Detection | SQLite | CI Integration | Parallel | Best For |
|--------|-------------|-----------|--------|----------------|----------|----------|
| 1 | asyncio.wait_for | TimeoutError | No | No | No | Simple async test protection |
| 2 | Wall-clock context manager | TimeoutError + checkpoint | No | No | No | Multi-step tests with mid-test checks |
| 3 | pytest-timeout decorator | pytest FAILED | No | pytest marks | Yes | Standard pytest suites |
| 4 | Wall-clock + timing DB | Flaky candidate detection | Yes | No | No | Tracking slow drift over many runs |
| 5 | asyncio.wait_for per-test | TestResult dataclass | No | No | Yes | Parallel test suites |
| 6 | asyncio.wait_for + suite budget | JSON CI report | No | Exit code + JSON | No | CI/CD pipeline integration |
