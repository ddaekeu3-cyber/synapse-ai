---
layout: solution
title: "Agent Doesn't Implement Cost-Aware Test Budgeting"
category: testing
description: "Eval and test suites that call Claude without budget controls can silently consume hundreds of dollars in a single CI run. Cost-aware test budgeting caps API spend per suite, skips expensive tests in PR builds, and surfaces cost-per-test metrics for suite optimization."
tags: [testing, cost, budget, evals, ci-cd, token-tracking, anthropic-sdk, pytest]
---

## Problem

Agent test suites that call real Claude APIs lack spend awareness: a single nightly eval run might call Opus hundreds of times, a flaky retry loop might re-run expensive tests, or a PR pipeline might run the full suite when only cheap smoke tests are needed. Without per-test cost tracking, engineers discover the problem on the monthly bill. Cost-aware budgeting caps spend, downgrades models in CI, and records per-test token costs to identify the most expensive tests.

## Solutions

### Option 1: Per-Suite Token Budget with Hard Stop

```python
import anthropic
import pytest
from dataclasses import dataclass, field
from typing import ClassVar

@dataclass
class TestBudget:
    """Shared budget across all tests in a session."""
    max_input_tokens: int = 50_000
    max_output_tokens: int = 10_000
    used_input: int = 0
    used_output: int = 0
    _exceeded: bool = False

    def record(self, usage):
        self.used_input += usage.input_tokens
        self.used_output += usage.output_tokens
        if self.used_input > self.max_input_tokens or self.used_output > self.max_output_tokens:
            self._exceeded = True

    def check(self):
        if self._exceeded:
            raise RuntimeError(
                f"Test budget exceeded: "
                f"input={self.used_input}/{self.max_input_tokens}, "
                f"output={self.used_output}/{self.max_output_tokens}"
            )

    @property
    def summary(self) -> str:
        pct_in = self.used_input / self.max_input_tokens * 100
        pct_out = self.used_output / self.max_output_tokens * 100
        return f"input={self.used_input} ({pct_in:.0f}%), output={self.used_output} ({pct_out:.0f}%)"

# Global budget for the test session
BUDGET = TestBudget(max_input_tokens=20_000, max_output_tokens=5_000)

def budgeted_call(prompt: str, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 128) -> str:
    BUDGET.check()  # fail fast before making the call
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    BUDGET.record(resp.usage)
    return resp.content[0].text

def test_basic_response():
    result = budgeted_call("Say 'hello' in one word.")
    assert len(result) > 0

def test_json_output():
    result = budgeted_call('Return {"ok": true}. JSON only.')
    import json
    data = json.loads(result.strip().strip("`").replace("json", ""))
    assert data.get("ok") is True

def test_classification():
    result = budgeted_call("Is 'puppy' positive or negative? Answer: positive or negative only.")
    assert "positive" in result.lower()

if __name__ == "__main__":
    try:
        test_basic_response()
        test_json_output()
        test_classification()
        print(f"All tests passed. Budget used: {BUDGET.summary}")
    except RuntimeError as e:
        print(f"BUDGET EXCEEDED: {e}")

# Expected Token Savings: hard stop prevents runaway suites; 20k input tokens = ~$0.05 max on haiku
# Environment: any test suite; BUDGET global ensures even unexpected test additions can't exceed cap
```

### Option 2: Per-Test Cost Tracker with SQLite Report

```python
import anthropic
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DB = Path("/tmp/test_costs.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS test_runs (
            id TEXT PRIMARY KEY,
            test_name TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd REAL,
            duration_ms REAL,
            passed INTEGER,
            run_at REAL
        )
    """)
    con.commit()
    con.close()

# Haiku pricing (per million tokens)
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
}

def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["claude-haiku-4-5-20251001"])
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

@contextmanager
def tracked_test(test_name: str, model: str = "claude-haiku-4-5-20251001"):
    """Context manager that records test cost to SQLite."""
    run_id = str(uuid.uuid4())[:8]
    t0 = time.time()
    result = {"passed": True, "input_tokens": 0, "output_tokens": 0}
    try:
        yield result
    except Exception:
        result["passed"] = False
        raise
    finally:
        duration_ms = (time.time() - t0) * 1000
        cost = calculate_cost(model, result["input_tokens"], result["output_tokens"])
        con = sqlite3.connect(DB)
        con.execute("""
            INSERT INTO test_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, test_name, model, result["input_tokens"],
              result["output_tokens"], cost, duration_ms,
              int(result["passed"]), time.time()))
        con.commit()
        con.close()
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status}] {test_name}: ${cost:.5f} ({result['input_tokens']}in/{result['output_tokens']}out) {duration_ms:.0f}ms")

def claude_call(prompt: str, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 64) -> tuple[str, object]:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text, resp.usage

def print_cost_report():
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT test_name, COUNT(*) runs, SUM(cost_usd) total_cost,
               AVG(cost_usd) avg_cost, MAX(cost_usd) max_cost,
               SUM(CASE WHEN passed=1 THEN 1 ELSE 0 END) passes
        FROM test_runs
        GROUP BY test_name
        ORDER BY total_cost DESC
    """).fetchall()
    con.close()
    print("\n--- Test Cost Report ---")
    for name, runs, total, avg, max_c, passes in rows:
        print(f"  {name:40s} runs={runs} total=${total:.5f} avg=${avg:.5f} max=${max_c:.5f} pass={passes}/{runs}")

if __name__ == "__main__":
    init_db()
    MODEL = "claude-haiku-4-5-20251001"

    with tracked_test("test_sentiment", MODEL) as r:
        text, usage = claude_call("Is 'great' positive or negative?", MODEL)
        r["input_tokens"] = usage.input_tokens
        r["output_tokens"] = usage.output_tokens

    with tracked_test("test_summary", MODEL) as r:
        text, usage = claude_call("Summarize: The cat sat on the mat.", MODEL, max_tokens=32)
        r["input_tokens"] = usage.input_tokens
        r["output_tokens"] = usage.output_tokens

    with tracked_test("test_classification", MODEL) as r:
        text, usage = claude_call("Classify as spam or not: 'Win a prize!'", MODEL)
        r["input_tokens"] = usage.input_tokens
        r["output_tokens"] = usage.output_tokens

    print_cost_report()

# Expected Token Savings: report reveals most expensive tests; haiku for all; SQLite persists across runs
# Environment: eval suites; run after nightly CI to identify which tests to optimize or move to cheaper models
```

### Option 3: Model Tier Selection by CI Environment

```python
import anthropic
import os

client = anthropic.Anthropic()

# CI environments get cheaper/smaller models; local dev and nightly get full models
CI_MODEL_MAP = {
    "pr": "claude-haiku-4-5-20251001",       # cheapest — smoke tests only
    "staging": "claude-haiku-4-5-20251001",   # haiku — integration tests
    "nightly": "claude-sonnet-4-6",           # sonnet — full suite
    "release": "claude-opus-4-6",             # opus — quality gate
    "local": "claude-haiku-4-5-20251001",     # local dev defaults to haiku
}

def get_test_model() -> str:
    env = os.environ.get("TEST_ENV", "local").lower()
    model = CI_MODEL_MAP.get(env, "claude-haiku-4-5-20251001")
    print(f"  [env={env}] using model: {model}")
    return model

def skip_if_expensive(min_env: str):
    """
    Decorator to skip a test if the current environment is cheaper than required.
    Order: pr < staging < nightly < release
    """
    tier_order = {"pr": 0, "staging": 1, "local": 1, "nightly": 2, "release": 3}

    def decorator(fn):
        def wrapper(*args, **kwargs):
            env = os.environ.get("TEST_ENV", "local").lower()
            current_tier = tier_order.get(env, 0)
            required_tier = tier_order.get(min_env, 2)
            if current_tier < required_tier:
                print(f"  [SKIP] {fn.__name__}: requires env>={min_env}, current={env}")
                return
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator

def run_test(name: str, prompt: str, max_tokens: int = 64) -> str:
    model = get_test_model()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    result = resp.content[0].text
    print(f"  [{name}] tokens={resp.usage.input_tokens}in+{resp.usage.output_tokens}out: {result.strip()[:50]}")
    return result

@skip_if_expensive("nightly")
def test_deep_reasoning():
    """Expensive test — only runs in nightly or release environments."""
    result = run_test("deep_reasoning", "Explain the halting problem in detail.", max_tokens=512)
    assert len(result) > 100

def test_smoke():
    """Cheap smoke test — always runs."""
    result = run_test("smoke", "Say OK.")
    assert result.strip()

@skip_if_expensive("staging")
def test_integration():
    """Medium test — runs in staging and above."""
    result = run_test("integration", "List 3 Python data structures.", max_tokens=128)
    assert "list" in result.lower() or "dict" in result.lower()

if __name__ == "__main__":
    # Simulate PR environment
    os.environ["TEST_ENV"] = "pr"
    print("=== PR environment ===")
    test_smoke()
    test_integration()
    test_deep_reasoning()

    os.environ["TEST_ENV"] = "nightly"
    print("\n=== Nightly environment ===")
    test_smoke()
    test_integration()
    test_deep_reasoning()

# Expected Token Savings: PR builds use haiku + skip expensive tests; 10-50x cheaper than full nightly suite
# Environment: CI/CD pipelines; TEST_ENV set by GitHub Actions, Jenkins, or CircleCI environment variable
```

### Option 4: Async Parallel Test Runner with Budget Guard

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class BudgetGuard:
    max_tokens: int = 30_000
    _used: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _results: list[dict] = field(default_factory=list)

    async def record(self, test_name: str, usage, passed: bool):
        async with self._lock:
            tokens = usage.input_tokens + usage.output_tokens
            self._used += tokens
            self._results.append({
                "test": test_name,
                "tokens": tokens,
                "passed": passed,
            })
            if self._used > self.max_tokens:
                raise RuntimeError(
                    f"Budget exceeded at test '{test_name}': "
                    f"used {self._used}/{self.max_tokens} tokens"
                )

    async def check(self):
        async with self._lock:
            if self._used > self.max_tokens:
                raise RuntimeError(f"Budget already exceeded: {self._used}/{self.max_tokens}")

    def report(self) -> str:
        total = sum(r["tokens"] for r in self._results)
        lines = [f"  Total tokens: {total}/{self.max_tokens}"]
        for r in sorted(self._results, key=lambda x: x["tokens"], reverse=True):
            status = "PASS" if r["passed"] else "FAIL"
            lines.append(f"  [{status}] {r['test']:40s} {r['tokens']:5d} tokens")
        return "\n".join(lines)

async def run_test(
    name: str,
    prompt: str,
    budget: BudgetGuard,
    max_tokens: int = 64,
) -> bool:
    await budget.check()
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        passed = len(resp.content[0].text) > 0
        await budget.record(name, resp.usage, passed)
        return passed
    except RuntimeError:
        raise
    except Exception as e:
        print(f"  [ERROR] {name}: {e}")
        return False

async def main():
    budget = BudgetGuard(max_tokens=5_000)
    tests = [
        ("test_greeting", "Say hello in one word.", 16),
        ("test_math", "What is 7 * 8?", 16),
        ("test_capitals", "Capital of Germany?", 16),
        ("test_color", "Is the sky blue? yes/no.", 8),
        ("test_language", "What language is 'bonjour'?", 16),
        ("test_sort", "Sort: [3,1,2]. Return sorted list.", 32),
    ]

    t0 = time.time()
    try:
        results = await asyncio.gather(*[
            run_test(name, prompt, budget, max_tokens)
            for name, prompt, max_tokens in tests
        ], return_exceptions=True)
    except Exception as e:
        print(f"Suite stopped: {e}")
    elapsed = time.time() - t0
    print(f"\nCompleted in {elapsed:.1f}s")
    print(budget.report())

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: parallel execution cuts wall time; budget guard stops all tests if one triggers limit
# Environment: async test suites; gather() runs all tests concurrently with shared budget enforcement
```

### Option 5: Pytest Plugin for Token Cost Fixtures

```python
import anthropic
import json
import time
from pathlib import Path

# Simulate pytest fixture pattern without pytest dependency
# In real usage: place in conftest.py as @pytest.fixture

client = anthropic.Anthropic()

class CostFixture:
    """pytest-style fixture for tracking Claude API costs per test."""
    def __init__(self, model: str = "claude-haiku-4-5-20251001", max_tokens_per_test: int = 512):
        self.model = model
        self.max_tokens_per_test = max_tokens_per_test
        self._calls: list[dict] = []
        self._session_tokens = 0
        self._session_limit = 20_000

    def call(self, prompt: str, max_tokens: int | None = None) -> str:
        if self._session_tokens >= self._session_limit:
            raise RuntimeError(f"Session token limit reached: {self._session_tokens}/{self._session_limit}")
        resp = client.messages.create(
            model=self.model,
            max_tokens=max_tokens or self.max_tokens_per_test,
            messages=[{"role": "user", "content": prompt}],
        )
        used = resp.usage.input_tokens + resp.usage.output_tokens
        self._session_tokens += used
        self._calls.append({
            "prompt_preview": prompt[:50],
            "input": resp.usage.input_tokens,
            "output": resp.usage.output_tokens,
            "total": used,
        })
        return resp.content[0].text

    def assert_within_budget(self, max_tokens: int):
        test_total = sum(c["total"] for c in self._calls)
        assert test_total <= max_tokens, (
            f"Test used {test_total} tokens, exceeding budget of {max_tokens}\n"
            f"Calls: {json.dumps(self._calls, indent=2)}"
        )

    def reset_test(self):
        self._calls = []

# Simulated test functions using the fixture
def run_tests():
    fixture = CostFixture(model="claude-haiku-4-5-20251001", max_tokens_per_test=64)

    # Test 1
    fixture.reset_test()
    r = fixture.call("Is 42 even?")
    assert "yes" in r.lower() or "even" in r.lower()
    fixture.assert_within_budget(200)
    print(f"test_even PASS ({sum(c['total'] for c in fixture._calls)} tokens)")

    # Test 2
    fixture.reset_test()
    r = fixture.call("Name one primary color.")
    assert any(c in r.lower() for c in ["red", "blue", "yellow"])
    fixture.assert_within_budget(200)
    print(f"test_color PASS ({sum(c['total'] for c in fixture._calls)} tokens)")

    # Test 3
    fixture.reset_test()
    r = fixture.call("What is Python?", max_tokens=32)
    assert len(r) > 0
    fixture.assert_within_budget(300)
    print(f"test_python PASS ({sum(c['total'] for c in fixture._calls)} tokens)")

    print(f"\nSession total: {fixture._session_tokens}/{fixture._session_limit} tokens")

if __name__ == "__main__":
    run_tests()

# Expected Token Savings: per-test budget assertions catch individually-expensive tests in CI
# Environment: pytest suites; fixture pattern integrates with existing test infrastructure via conftest.py
```

### Option 6: Golden Token Baseline with Regression Detection

```python
import anthropic
import json
import sqlite3
import time
from pathlib import Path

DB = Path("/tmp/token_baseline.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS token_baselines (
            test_name TEXT NOT NULL,
            run_date TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            PRIMARY KEY (test_name, run_date)
        )
    """)
    con.commit()
    con.close()

def record_run(test_name: str, model: str, usage) -> int:
    total = usage.input_tokens + usage.output_tokens
    con = sqlite3.connect(DB)
    con.execute("""
        INSERT OR REPLACE INTO token_baselines
        (test_name, run_date, model, input_tokens, output_tokens, total_tokens)
        VALUES (?, date('now'), ?, ?, ?, ?)
    """, (test_name, model, usage.input_tokens, usage.output_tokens, total))
    con.commit()
    con.close()
    return total

def get_baseline(test_name: str, lookback_days: int = 7) -> int | None:
    con = sqlite3.connect(DB)
    row = con.execute("""
        SELECT AVG(total_tokens) FROM token_baselines
        WHERE test_name = ? AND run_date >= date('now', ?)
    """, (test_name, f"-{lookback_days} days")).fetchone()
    con.close()
    return int(row[0]) if row and row[0] else None

def run_with_regression_check(
    test_name: str,
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 128,
    regression_threshold: float = 2.0,  # flag if 2x above baseline
) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    total = record_run(test_name, model, resp.usage)
    baseline = get_baseline(test_name, lookback_days=7)

    if baseline and total > baseline * regression_threshold:
        print(f"  [TOKEN REGRESSION] {test_name}: {total} tokens vs baseline {baseline:.0f} ({total/baseline:.1f}x)")
    else:
        trend = f" (baseline={baseline:.0f})" if baseline else " (no baseline)"
        print(f"  [OK] {test_name}: {total} tokens{trend}")

    return resp.content[0].text

if __name__ == "__main__":
    init_db()
    tests = [
        ("greeting_test", "Say hello in 5 words."),
        ("math_test", "What is 12 * 12?"),
        ("reasoning_test", "Why is the sky blue? Brief answer."),
    ]
    for name, prompt in tests:
        result = run_with_regression_check(name, prompt)
        print(f"    Result: {result.strip()[:60]}")

    # Simulate regression: prompt that uses more tokens
    print("\n--- Simulating token regression ---")
    run_with_regression_check(
        "greeting_test",
        "Say hello in 5 words and explain the etymology of 'hello' in detail.",
        max_tokens=256,
    )

# Expected Token Savings: regression detection catches accidentally-expensive prompt changes in CI
# Environment: eval suites with stable prompts; 2x regression flag surfaces prompt bloat before billing
```

## Comparison

| Option | Enforcement | Granularity | Persistence | CI Integration |
|--------|------------|-------------|-------------|---------------|
| 1 — Hard token cap | Global hard stop | Suite-level | None | Simple; raise on exceed |
| 2 — SQLite per-test cost | None (reporting) | Per-test | SQLite (persists) | Nightly cost reports |
| 3 — CI environment tiers | Skip expensive tests | Per-test decorator | None | TEST_ENV env var |
| 4 — Async budget guard | Hard stop + parallel | Per-test async | None | gather() fan-out |
| 5 — Pytest cost fixture | Per-test assertion | Per-call | None | conftest.py fixture |
| 6 — Golden token baseline | Regression warning | Per-test over time | SQLite (persists) | Detects prompt regressions |
