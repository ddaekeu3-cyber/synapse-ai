---
title: "Agent Doesn't Implement Test Flakiness Detection and Quarantine"
description: "How to detect flaky tests in agent evaluation suites, automatically quarantine them, and prevent them from blocking CI while root cause is investigated."
categories: [testing]
difficulty: intermediate
---

LLM-powered agent tests are inherently non-deterministic. The same test may pass 9 times and fail once due to response variation, timing, or external API state—not a real regression. Without flakiness detection, you either block CI on phantom failures or ignore real regressions by assuming every failure is flaky. Systematic flakiness tracking gives you confidence in your test suite.

## Solution 1: Run-N-Times Flakiness Scorer

Run each test multiple times and compute a pass rate; quarantine tests below a stability threshold.

```python
import asyncio
import statistics
from dataclasses import dataclass, field
from typing import Callable, Awaitable

import anthropic

client = anthropic.AsyncAnthropic()

STABILITY_THRESHOLD = 0.90   # < 90% pass rate = flaky
RUNS_PER_TEST = 5


@dataclass
class TestResult:
    name: str
    passed: bool
    error: str | None = None


@dataclass
class FlakinessReport:
    name: str
    runs: int
    passes: int
    pass_rate: float
    is_flaky: bool
    errors: list[str] = field(default_factory=list)


TestFn = Callable[[], Awaitable[TestResult]]


async def run_once(test_fn: TestFn) -> TestResult:
    try:
        return await test_fn()
    except Exception as e:
        return TestResult(name="unknown", passed=False, error=str(e))


async def score_flakiness(name: str, test_fn: TestFn, runs: int = RUNS_PER_TEST) -> FlakinessReport:
    results = await asyncio.gather(*[run_once(test_fn) for _ in range(runs)])
    passes = sum(1 for r in results if r.passed)
    errors = [r.error for r in results if r.error]
    pass_rate = passes / runs
    return FlakinessReport(
        name=name,
        runs=runs,
        passes=passes,
        pass_rate=pass_rate,
        is_flaky=pass_rate < STABILITY_THRESHOLD,
        errors=errors,
    )


# --- Example agent test ---

async def test_sentiment_classification() -> TestResult:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[
            {"role": "user", "content": "Classify: 'I love this product!' → positive/negative/neutral"}
        ],
    )
    text = resp.content[0].text.lower()
    passed = "positive" in text
    return TestResult(name="sentiment_classification", passed=passed)


async def test_json_extraction() -> TestResult:
    import json
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[
            {"role": "user", "content": 'Extract JSON: {"name": "Alice", "age": 30} → return only the JSON'}
        ],
    )
    text = resp.content[0].text.strip()
    try:
        data = json.loads(text)
        passed = data.get("name") == "Alice"
    except json.JSONDecodeError:
        passed = False
    return TestResult(name="json_extraction", passed=passed)


async def main():
    tests = {
        "sentiment_classification": test_sentiment_classification,
        "json_extraction": test_json_extraction,
    }

    reports = await asyncio.gather(*[
        score_flakiness(name, fn) for name, fn in tests.items()
    ])

    quarantined = []
    for r in reports:
        status = "FLAKY" if r.is_flaky else "STABLE"
        print(f"[{status}] {r.name}: {r.pass_rate:.0%} ({r.passes}/{r.runs})")
        if r.is_flaky:
            quarantined.append(r.name)

    if quarantined:
        print(f"\nQuarantined (excluded from CI gate): {quarantined}")


asyncio.run(main())
```

## Solution 2: Historical Pass Rate Tracker with SQLite

Persist per-test pass/fail history to compute rolling flakiness metrics across CI runs.

```python
import asyncio
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
import anthropic

client = anthropic.AsyncAnthropic()
DB_PATH = "/tmp/test_flakiness.db"
FLAKINESS_WINDOW = 20       # last N runs
QUARANTINE_THRESHOLD = 0.80  # < 80% stable = quarantine


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_name TEXT NOT NULL,
                passed INTEGER NOT NULL,
                duration_ms REAL,
                error TEXT,
                run_at REAL DEFAULT (strftime('%s', 'now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quarantine (
                test_name TEXT PRIMARY KEY,
                quarantined_at REAL,
                reason TEXT
            )
        """)
        conn.commit()


def record_run(test_name: str, passed: bool, duration_ms: float, error: str | None = None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO test_runs (test_name, passed, duration_ms, error) VALUES (?, ?, ?, ?)",
            (test_name, int(passed), duration_ms, error),
        )
        conn.commit()


def get_pass_rate(test_name: str, window: int = FLAKINESS_WINDOW) -> float | None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT passed FROM test_runs WHERE test_name = ? ORDER BY run_at DESC LIMIT ?",
            (test_name, window),
        ).fetchall()
    if not rows:
        return None
    return sum(r["passed"] for r in rows) / len(rows)


def is_quarantined(test_name: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM quarantine WHERE test_name = ?", (test_name,)
        ).fetchone()
    return row is not None


def maybe_quarantine(test_name: str, pass_rate: float):
    if pass_rate < QUARANTINE_THRESHOLD:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO quarantine (test_name, quarantined_at, reason) VALUES (?, ?, ?)",
                (test_name, time.time(), f"pass_rate={pass_rate:.2f} < {QUARANTINE_THRESHOLD}"),
            )
            conn.commit()
        print(f"[quarantine] {test_name} quarantined (pass_rate={pass_rate:.0%})")


async def run_and_record(test_name: str, test_fn) -> bool:
    if is_quarantined(test_name):
        print(f"[skip] {test_name} is quarantined")
        return True  # Don't block CI

    start = time.monotonic()
    error = None
    passed = False
    try:
        result = await test_fn()
        passed = result
    except Exception as e:
        error = str(e)
    duration_ms = (time.monotonic() - start) * 1000

    record_run(test_name, passed, duration_ms, error)

    rate = get_pass_rate(test_name)
    if rate is not None:
        maybe_quarantine(test_name, rate)

    return passed


async def example_test() -> bool:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        messages=[{"role": "user", "content": "Say 'yes' only."}],
    )
    return "yes" in resp.content[0].text.lower()


async def main():
    init_db()
    for i in range(10):
        result = await run_and_record("example_test", example_test)
        rate = get_pass_rate("example_test")
        print(f"Run {i+1}: {'pass' if result else 'fail'} | rolling rate: {rate:.0%}" if rate else f"Run {i+1}: {'pass' if result else 'fail'}")
        await asyncio.sleep(0.1)


asyncio.run(main())
```

## Solution 3: Flakiness-Aware CI Gate

Run tests in two phases: stable tests gate the build, quarantined tests run in informational mode only.

```python
import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable

import anthropic

client = anthropic.AsyncAnthropic()


class TestStatus(Enum):
    STABLE = "stable"
    QUARANTINED = "quarantined"
    NEW = "new"


@dataclass
class TestCase:
    name: str
    fn: Callable[[], Awaitable[bool]]
    status: TestStatus = TestStatus.NEW
    known_pass_rate: float = 1.0


@dataclass
class CIResult:
    gated_passed: int = 0
    gated_failed: int = 0
    quarantined_passed: int = 0
    quarantined_failed: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def build_passes(self) -> bool:
        return self.gated_failed == 0


async def run_test_case(tc: TestCase) -> bool:
    try:
        return await tc.fn()
    except Exception:
        return False


async def run_ci_suite(tests: list[TestCase]) -> CIResult:
    result = CIResult()

    stable = [t for t in tests if t.status != TestStatus.QUARANTINED]
    quarantined = [t for t in tests if t.status == TestStatus.QUARANTINED]

    # Run stable tests (gate the build)
    stable_results = await asyncio.gather(*[run_test_case(t) for t in stable])
    for tc, passed in zip(stable, stable_results):
        if passed:
            result.gated_passed += 1
        else:
            result.gated_failed += 1
            result.failures.append(tc.name)

    # Run quarantined tests (informational only)
    if quarantined:
        q_results = await asyncio.gather(*[run_test_case(t) for t in quarantined])
        for tc, passed in zip(quarantined, q_results):
            if passed:
                result.quarantined_passed += 1
            else:
                result.quarantined_failed += 1

    return result


# --- Test implementations ---

async def test_always_passes() -> bool:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        messages=[{"role": "user", "content": "What is 2+2? Answer with just the number."}],
    )
    return "4" in resp.content[0].text


async def test_flaky_format() -> bool:
    """Occasionally fails if the model adds extra formatting."""
    import re
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": "Reply with exactly: DONE"}],
    )
    return resp.content[0].text.strip() == "DONE"


async def main():
    tests = [
        TestCase("arithmetic", test_always_passes, TestStatus.STABLE),
        TestCase("exact_format", test_flaky_format, TestStatus.QUARANTINED, known_pass_rate=0.70),
    ]

    result = await run_ci_suite(tests)

    print(f"\nCI Result: {'PASS' if result.build_passes else 'FAIL'}")
    print(f"  Stable:     {result.gated_passed} passed, {result.gated_failed} failed")
    print(f"  Quarantine: {result.quarantined_passed} passed, {result.quarantined_failed} failed (informational)")
    if result.failures:
        print(f"  Failing tests: {result.failures}")


asyncio.run(main())
```

## Solution 4: Variance-Based Flakiness Detector Using Output Similarity

Instead of binary pass/fail, measure output similarity across runs to detect inconsistency in agent responses.

```python
import asyncio
import re
from difflib import SequenceMatcher
import anthropic

client = anthropic.AsyncAnthropic()

SIMILARITY_VARIANCE_THRESHOLD = 0.15  # flag if std dev of similarity > 15%
BASELINE_RUNS = 5


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


async def get_response(prompt: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return normalize(resp.content[0].text)


async def measure_output_variance(test_name: str, prompt: str, runs: int = BASELINE_RUNS) -> dict:
    responses = await asyncio.gather(*[get_response(prompt) for _ in range(runs)])

    # Pairwise similarity
    sims = []
    for i in range(len(responses)):
        for j in range(i + 1, len(responses)):
            sims.append(similarity(responses[i], responses[j]))

    if not sims:
        return {"name": test_name, "variance": 0.0, "is_flaky": False}

    mean_sim = sum(sims) / len(sims)
    variance = (sum((s - mean_sim) ** 2 for s in sims) / len(sims)) ** 0.5  # std dev
    is_flaky = variance > SIMILARITY_VARIANCE_THRESHOLD

    return {
        "name": test_name,
        "mean_similarity": mean_sim,
        "std_dev": variance,
        "is_flaky": is_flaky,
        "sample_responses": responses[:2],
    }


async def main():
    tests = [
        ("stable_factual", "What is the capital of France? Answer in one word."),
        ("unstable_creative", "Write a creative one-sentence story about a robot."),
    ]

    reports = await asyncio.gather(*[measure_output_variance(name, prompt) for name, prompt in tests])

    for r in reports:
        flag = "FLAKY" if r["is_flaky"] else "STABLE"
        print(f"[{flag}] {r['name']}: mean_sim={r['mean_similarity']:.2f}, std_dev={r['std_dev']:.3f}")


asyncio.run(main())
```

## Solution 5: Retry-and-Demote Strategy

On first failure, automatically retry the test; if the retry passes, count it as a flakiness signal rather than a failure.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable
import anthropic

client = anthropic.AsyncAnthropic()

RETRY_ON_FAILURE = True
MAX_RETRIES = 2
FLAKINESS_SIGNAL_THRESHOLD = 3  # N flakiness signals → quarantine


@dataclass
class TestRegistry:
    _signals: dict[str, int] = field(default_factory=dict)

    def record_signal(self, test_name: str):
        self._signals[test_name] = self._signals.get(test_name, 0) + 1

    def signal_count(self, test_name: str) -> int:
        return self._signals.get(test_name, 0)

    def should_quarantine(self, test_name: str) -> bool:
        return self.signal_count(test_name) >= FLAKINESS_SIGNAL_THRESHOLD


registry = TestRegistry()


@dataclass
class RunOutcome:
    passed: bool
    is_flaky_signal: bool  # True if failed then retried+passed
    retries: int


async def run_with_retry(
    test_name: str,
    test_fn: Callable[[], Awaitable[bool]],
) -> RunOutcome:
    first_result = await test_fn()

    if first_result:
        return RunOutcome(passed=True, is_flaky_signal=False, retries=0)

    # First attempt failed — retry
    for attempt in range(MAX_RETRIES):
        retry_result = await test_fn()
        if retry_result:
            # Passed on retry → flakiness signal
            registry.record_signal(test_name)
            count = registry.signal_count(test_name)
            print(f"[flaky-signal] {test_name}: signal #{count}")
            if registry.should_quarantine(test_name):
                print(f"[quarantine] {test_name} promoted to quarantine after {count} signals")
            return RunOutcome(passed=True, is_flaky_signal=True, retries=attempt + 1)

    # All retries failed — real failure
    return RunOutcome(passed=False, is_flaky_signal=False, retries=MAX_RETRIES)


async def flaky_test() -> bool:
    import random
    await asyncio.sleep(0.01)
    # Simulate ~70% pass rate
    return random.random() < 0.70


async def stable_test() -> bool:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        messages=[{"role": "user", "content": "Is 5 > 3? Answer yes or no."}],
    )
    return "yes" in resp.content[0].text.lower()


async def main():
    for run in range(15):
        outcome = await run_with_retry("flaky_test", flaky_test)
        stable = await run_with_retry("stable_test", stable_test)
        print(f"Run {run+1:2d}: flaky={'pass' if outcome.passed else 'FAIL'}(signal={outcome.is_flaky_signal}) "
              f"stable={'pass' if stable.passed else 'FAIL'}")


asyncio.run(main())
```

## Solution 6: Flakiness Dashboard with Root Cause Hints

Aggregate flakiness data and use an LLM to generate root cause hypotheses from the failure patterns.

```python
import asyncio
import json
import random
from dataclasses import dataclass, field
from collections import defaultdict
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class FailureRecord:
    test_name: str
    error_message: str
    response_snippet: str
    timestamp: float


@dataclass
class FlakinessDashboard:
    _records: dict[str, list[FailureRecord]] = field(default_factory=lambda: defaultdict(list))

    def record_failure(self, record: FailureRecord):
        self._records[record.test_name].append(record)

    def get_failures(self, test_name: str) -> list[FailureRecord]:
        return self._records.get(test_name, [])

    def flaky_tests(self) -> list[str]:
        return [name for name, records in self._records.items() if len(records) >= 2]


dashboard = FlakinessDashboard()


async def get_root_cause_hypothesis(test_name: str, failures: list[FailureRecord]) -> str:
    failure_summary = json.dumps([
        {"error": f.error_message, "snippet": f.response_snippet[:200]}
        for f in failures[:5]
    ], indent=2)

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Analyze these intermittent test failures for '{test_name}' and "
                    f"suggest the most likely root cause in 2-3 sentences:\n\n{failure_summary}"
                ),
            }
        ],
    )
    return resp.content[0].text


async def simulate_flaky_run(test_name: str, run_id: int) -> bool:
    import time
    passed = random.random() > 0.4
    if not passed:
        errors = [
            "Expected 'positive' but got 'somewhat positive'",
            "JSON parse error: trailing comma",
            "Timeout after 30s",
            "Response contained unexpected preamble",
        ]
        dashboard.record_failure(FailureRecord(
            test_name=test_name,
            error_message=random.choice(errors),
            response_snippet=f"[run {run_id}] partial model output sample",
            timestamp=time.time(),
        ))
    return passed


async def generate_dashboard_report():
    print("\n=== Flakiness Dashboard ===")
    flaky = dashboard.flaky_tests()
    if not flaky:
        print("No flaky tests detected.")
        return

    hypotheses = await asyncio.gather(*[
        get_root_cause_hypothesis(name, dashboard.get_failures(name))
        for name in flaky
    ])

    for name, hypothesis in zip(flaky, hypotheses):
        failures = dashboard.get_failures(name)
        print(f"\n[FLAKY] {name}: {len(failures)} failures")
        print(f"  Root cause hypothesis: {hypothesis.strip()}")


async def main():
    random.seed(42)
    tests = ["sentiment_test", "json_format_test", "routing_decision_test"]

    for run in range(10):
        await asyncio.gather(*[simulate_flaky_run(t, run) for t in tests])

    await generate_dashboard_report()


asyncio.run(main())
```

## Comparison

| Solution | Storage | Automation | LLM cost | CI integration | Best for |
|---|---|---|---|---|---|
| **Run-N-times scorer** | None | Full | Medium | Gate or quarantine | Quick local evaluation |
| **SQLite history tracker** | Persistent | Full | Low | Quarantine by history | Long-running CI pipelines |
| **Flakiness-aware CI gate** | External | Full | Low | Native gate split | Teams with mixed test reliability |
| **Variance-based detector** | None | Full | Medium | Informational | Non-deterministic output tests |
| **Retry-and-demote** | In-memory | Full | Low | Signal accumulation | Lightweight in-process detection |
| **Dashboard + LLM hints** | In-memory | Semi | Medium | Reporting | Investigating root causes |

Start with **retry-and-demote** (Solution 5) — zero infrastructure, immediate signal collection. Add **SQLite history tracker** (Solution 2) when you want persistence across CI runs. Use **dashboard + LLM hints** (Solution 6) when accumulated failures need triage.
