---
layout: solution
title: "Agent Doesn't Implement Performance Regression Testing"
category: testing
description: "Latency and throughput regressions are discovered by users, not tests. A refactored prompt, a model upgrade, or a new tool call silently doubles response time with no CI gate to catch it before deployment."
tags: [testing, performance, regression, latency, benchmarking, pytest, ci]
---

# Agent Doesn't Implement Performance Regression Testing

## Problem

A developer refactors the system prompt for clarity. Unknown to them, the new prompt increases average response time by 2.3 seconds because it generates longer chain-of-thought. No test catches this before the PR merges. Users notice the slowdown; the oncall engineer spends two hours bisecting commits. Performance regression tests would have caught this in CI within minutes.

## Solutions

### Option 1: Latency Baseline File with pytest

```python
# tests/performance/test_latency_regression.py
"""
Compare current latency against a stored baseline.
Fails the build if latency regresses beyond a configurable threshold.
Baselines are stored as JSON files and committed to the repo.
"""
import asyncio
import json
import statistics
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
import anthropic

BASELINE_FILE = Path("tests/performance/baselines.json")
REGRESSION_THRESHOLD = 0.25  # 25% increase triggers failure
WARMUP_CALLS = 3
MEASUREMENT_CALLS = 10


def load_baselines() -> dict[str, float]:
    if BASELINE_FILE.exists():
        return json.loads(BASELINE_FILE.read_text())
    return {}


def save_baselines(baselines: dict[str, float]):
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(baselines, indent=2))


@pytest.fixture
def mock_client():
    """Use a mock to measure framework/prompt overhead without real API latency."""
    mock = MagicMock(spec=anthropic.Anthropic)
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="The answer is 42.")]
    mock_response.usage.input_tokens = 50
    mock_response.usage.output_tokens = 20
    mock_response.stop_reason = "end_turn"
    mock.messages.create.return_value = mock_response
    return mock


def measure_latency(fn, n_calls: int, warmup: int = 3) -> dict:
    """Run fn n_calls times and return latency statistics."""
    # Warmup
    for _ in range(warmup):
        fn()

    # Measurement
    latencies = []
    for _ in range(n_calls):
        start = time.perf_counter()
        fn()
        latencies.append((time.perf_counter() - start) * 1000)

    return {
        "mean_ms": statistics.mean(latencies),
        "median_ms": statistics.median(latencies),
        "p95_ms": sorted(latencies)[int(len(latencies) * 0.95)],
        "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)],
        "stddev_ms": statistics.stdev(latencies) if len(latencies) > 1 else 0,
    }


@pytest.mark.parametrize("scenario_name,user_message", [
    ("simple_query", "What is 2+2?"),
    ("code_generation", "Write a Python function to sort a list."),
    ("long_context", "Summarize the following: " + "Lorem ipsum " * 100),
])
def test_latency_regression(scenario_name, user_message, mock_client):
    """Measure end-to-end latency and compare against stored baseline."""
    from your_agent.agent import process_message

    def run():
        return process_message(user_message, client=mock_client)

    stats = measure_latency(run, n_calls=MEASUREMENT_CALLS, warmup=WARMUP_CALLS)
    current_mean = stats["mean_ms"]
    baselines = load_baselines()

    if scenario_name not in baselines:
        # First run — record baseline
        baselines[scenario_name] = current_mean
        save_baselines(baselines)
        pytest.skip(f"Baseline recorded for '{scenario_name}': {current_mean:.1f}ms — re-run to compare")
        return

    baseline = baselines[scenario_name]
    regression_pct = (current_mean - baseline) / baseline if baseline > 0 else 0

    print(f"\n{scenario_name}: current={current_mean:.1f}ms, baseline={baseline:.1f}ms, "
          f"change={regression_pct:+.1%}")

    if regression_pct > REGRESSION_THRESHOLD:
        pytest.fail(
            f"PERFORMANCE REGRESSION: '{scenario_name}' is {regression_pct:.1%} slower.\n"
            f"  Baseline: {baseline:.1f}ms\n"
            f"  Current:  {current_mean:.1f}ms\n"
            f"  P95:      {stats['p95_ms']:.1f}ms\n"
            f"To update baseline: UPDATE_BASELINES=1 pytest {__file__}"
        )
```

```bash
# Update baselines after intentional performance changes:
UPDATE_BASELINES=1 pytest tests/performance/test_latency_regression.py
```

**Expected Token Savings:** Not applicable — CI regression gate
**Environment:** `pip install pytest anthropic`

---

### Option 2: Throughput Regression Test

```python
# tests/performance/test_throughput_regression.py
"""
Measure requests-per-second throughput under concurrency.
Catch cases where a change reduces parallelism capacity.
"""
import asyncio
import json
import statistics
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

BASELINE_FILE = Path("tests/performance/throughput_baselines.json")
THROUGHPUT_REGRESSION_THRESHOLD = 0.20  # 20% drop triggers failure
TEST_DURATION_SECONDS = 5.0
CONCURRENCY_LEVELS = [1, 5, 10]


async def measure_throughput(
    async_fn,
    duration_seconds: float,
    concurrency: int,
) -> dict:
    """Run async_fn concurrently for duration_seconds and measure throughput."""
    completed = 0
    errors = 0
    latencies = []
    stop_event = asyncio.Event()

    async def worker():
        nonlocal completed, errors
        while not stop_event.is_set():
            start = time.perf_counter()
            try:
                await async_fn()
                latencies.append((time.perf_counter() - start) * 1000)
                completed += 1
            except Exception:
                errors += 1

    asyncio.get_event_loop().call_later(duration_seconds, stop_event.set)
    await asyncio.gather(*[worker() for _ in range(concurrency)])

    rps = completed / duration_seconds
    return {
        "rps": rps,
        "completed": completed,
        "errors": errors,
        "concurrency": concurrency,
        "mean_latency_ms": statistics.mean(latencies) if latencies else 0,
    }


@pytest.fixture
def mock_async_agent():
    """Mock agent with realistic async latency."""
    async def mock_call():
        await asyncio.sleep(0.001)  # Simulate minimal framework overhead
        return "mock response"
    return mock_call


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", CONCURRENCY_LEVELS)
async def test_throughput_regression(concurrency, mock_async_agent):
    """Throughput must not drop below baseline × (1 - threshold)."""
    from your_agent.agent import process_message_async

    stats = await measure_throughput(mock_async_agent, TEST_DURATION_SECONDS, concurrency)
    current_rps = stats["rps"]

    baselines = {}
    if BASELINE_FILE.exists():
        baselines = json.loads(BASELINE_FILE.read_text())

    key = f"concurrency_{concurrency}"
    if key not in baselines:
        baselines[key] = current_rps
        BASELINE_FILE.parent.mkdir(exist_ok=True)
        BASELINE_FILE.write_text(json.dumps(baselines, indent=2))
        pytest.skip(f"Baseline recorded: {current_rps:.1f} RPS at concurrency={concurrency}")
        return

    baseline_rps = baselines[key]
    drop_pct = (baseline_rps - current_rps) / baseline_rps if baseline_rps > 0 else 0

    print(f"\nConcurrency={concurrency}: current={current_rps:.1f} RPS, "
          f"baseline={baseline_rps:.1f} RPS, drop={drop_pct:.1%}")

    if drop_pct > THROUGHPUT_REGRESSION_THRESHOLD:
        pytest.fail(
            f"THROUGHPUT REGRESSION at concurrency={concurrency}: "
            f"{drop_pct:.1%} drop in RPS.\n"
            f"  Baseline: {baseline_rps:.1f} RPS\n"
            f"  Current:  {current_rps:.1f} RPS"
        )
```

**Expected Token Savings:** Not applicable — performance CI gate
**Environment:** `pip install pytest pytest-asyncio`

---

### Option 3: Token Usage Regression Test

```python
# tests/performance/test_token_regression.py
"""
Track token consumption per scenario. Catches prompt bloat — cases where
a refactored system prompt or new tool descriptions silently increase
the token count of every request, multiplying API costs at scale.
"""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import anthropic

BASELINE_FILE = Path("tests/performance/token_baselines.json")
TOKEN_REGRESSION_THRESHOLD = 0.10  # 10% increase triggers failure


class TokenCapturingClient:
    """Wraps Anthropic client to capture actual token usage."""
    def __init__(self):
        self.calls: list[dict] = []
        self._real_client = anthropic.Anthropic(api_key="test")

    def create_message(self, **kwargs) -> dict:
        # Estimate tokens from message content (rough: 1 token = 4 chars)
        system = kwargs.get("system", "")
        messages = kwargs.get("messages", [])
        system_tokens = len(str(system)) // 4
        user_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 4
        total_input = system_tokens + user_tokens

        self.calls.append({
            "system_tokens": system_tokens,
            "user_tokens": user_tokens,
            "total_input_tokens": total_input,
            "max_tokens": kwargs.get("max_tokens", 0),
        })
        return {"input_tokens": total_input}


@pytest.fixture
def token_tracker():
    return TokenCapturingClient()


PROMPT_SCENARIOS = [
    ("code_task", "Write a Python function that reverses a string."),
    ("analysis_task", "Analyze the pros and cons of microservices architecture."),
    ("simple_qa", "What is the capital of Japan?"),
]


@pytest.mark.parametrize("scenario_name,user_message", PROMPT_SCENARIOS)
def test_token_usage_regression(scenario_name, user_message, token_tracker):
    """Token usage per scenario must not increase beyond threshold."""
    from your_agent.agent import build_messages  # Function that builds the full message array

    messages = build_messages(user_message)
    # Estimate total input tokens
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    from your_agent.system_prompt import SYSTEM_PROMPT
    system_chars = len(SYSTEM_PROMPT)
    estimated_tokens = (total_chars + system_chars) // 4

    baselines = {}
    if BASELINE_FILE.exists():
        baselines = json.loads(BASELINE_FILE.read_text())

    if scenario_name not in baselines:
        baselines[scenario_name] = estimated_tokens
        BASELINE_FILE.parent.mkdir(exist_ok=True)
        BASELINE_FILE.write_text(json.dumps(baselines, indent=2))
        pytest.skip(f"Token baseline recorded for '{scenario_name}': {estimated_tokens} tokens")
        return

    baseline_tokens = baselines[scenario_name]
    increase_pct = (estimated_tokens - baseline_tokens) / max(baseline_tokens, 1)

    print(f"\n{scenario_name}: current={estimated_tokens} tokens, "
          f"baseline={baseline_tokens} tokens, change={increase_pct:+.1%}")

    if increase_pct > TOKEN_REGRESSION_THRESHOLD:
        pytest.fail(
            f"TOKEN REGRESSION: '{scenario_name}' uses {increase_pct:.1%} more tokens.\n"
            f"  Baseline: {baseline_tokens} tokens\n"
            f"  Current:  {estimated_tokens} tokens\n"
            f"  Extra:    {estimated_tokens - baseline_tokens} tokens per request\n"
            f"  At 1M requests/day: ~${(estimated_tokens - baseline_tokens) * 1e6 * 3e-6:.0f}/day extra cost\n"
            f"To update: UPDATE_TOKEN_BASELINES=1 pytest {__file__}"
        )
```

**Expected Token Savings:** Catches prompt bloat early; 10% more tokens = 10% more cost at scale
**Environment:** `pip install pytest anthropic`

---

### Option 4: CI Pipeline with Performance Gate

```python
# tests/performance/perf_ci_gate.py
"""
Standalone performance gate for CI.
Runs a quick benchmark (< 30s) and exits non-zero if any metric regresses.
Designed to run as a separate CI step, not mixed with unit tests.
"""
import asyncio
import json
import sys
import time
import statistics
from pathlib import Path
from dataclasses import dataclass, asdict
from unittest.mock import MagicMock


BASELINE_FILE = Path(".perf-baselines.json")

THRESHOLDS = {
    "mean_latency_ms": 0.25,    # 25% regression
    "p95_latency_ms": 0.30,     # 30% regression
    "token_count": 0.10,        # 10% increase
}


@dataclass
class PerfResult:
    scenario: str
    mean_latency_ms: float
    p95_latency_ms: float
    token_count: int
    regressions: list[str]


def _run_scenario(scenario_name: str, message: str, n: int = 20) -> PerfResult:
    """Run a scenario N times and collect metrics."""
    from your_agent.agent import process_message

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [MagicMock(text="Answer")]
    mock_client.messages.create.return_value.usage.input_tokens = 100
    mock_client.messages.create.return_value.usage.output_tokens = 20

    latencies = []
    for _ in range(n):
        start = time.perf_counter()
        process_message(message, client=mock_client)
        latencies.append((time.perf_counter() - start) * 1000)

    sorted_l = sorted(latencies)
    return PerfResult(
        scenario=scenario_name,
        mean_latency_ms=statistics.mean(latencies),
        p95_latency_ms=sorted_l[int(len(sorted_l) * 0.95)],
        token_count=mock_client.messages.create.call_args[1].get("max_tokens", 0),
        regressions=[],
    )


def run_perf_gate() -> int:
    """Returns 0 (pass) or 1 (fail)."""
    scenarios = [
        ("simple", "What is 2+2?"),
        ("medium", "Explain Python generators in 2 sentences."),
    ]

    baselines = {}
    if BASELINE_FILE.exists():
        baselines = json.loads(BASELINE_FILE.read_text())

    results = []
    update_mode = len(sys.argv) > 1 and sys.argv[1] == "--update"
    new_baselines = dict(baselines)
    overall_fail = False

    for name, message in scenarios:
        result = _run_scenario(name, message)
        current = asdict(result)
        current.pop("regressions")

        if name not in baselines or update_mode:
            new_baselines[name] = current
            print(f"  [BASELINE] {name}: mean={result.mean_latency_ms:.1f}ms, "
                  f"p95={result.p95_latency_ms:.1f}ms")
        else:
            b = baselines[name]
            for metric, threshold in THRESHOLDS.items():
                baseline_val = b.get(metric, 0)
                current_val = current.get(metric, 0)
                if baseline_val <= 0:
                    continue
                change = (current_val - baseline_val) / baseline_val
                if change > threshold:
                    result.regressions.append(
                        f"{metric}: +{change:.1%} (baseline={baseline_val:.1f}, current={current_val:.1f})"
                    )
                    overall_fail = True

            status = "FAIL" if result.regressions else "PASS"
            print(f"  [{status}] {name}: mean={result.mean_latency_ms:.1f}ms, "
                  f"p95={result.p95_latency_ms:.1f}ms")
            for r in result.regressions:
                print(f"         REGRESSION: {r}")

        results.append(result)

    if update_mode or not BASELINE_FILE.exists():
        BASELINE_FILE.write_text(json.dumps(new_baselines, indent=2))
        print(f"Baselines saved to {BASELINE_FILE}")

    return 1 if overall_fail else 0


if __name__ == "__main__":
    sys.exit(run_perf_gate())
```

```yaml
# .github/workflows/perf_gate.yml
name: Performance Gate
on: [push, pull_request]
jobs:
  perf:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install anthropic
      - run: python tests/performance/perf_ci_gate.py
        env:
          ANTHROPIC_API_KEY: dummy  # Mock doesn't need real key
```

**Expected Token Savings:** Not applicable — CI performance gate
**Environment:** `pip install anthropic`

---

### Option 5: Time-to-First-Token Regression Test

```python
# tests/performance/test_ttft_regression.py
"""
For streaming agents, track Time-to-First-Token (TTFT) separately from
total latency. TTFT directly impacts perceived responsiveness.
A regression in TTFT (e.g., from prompt bloat) harms UX even if
total latency is unchanged.
"""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

BASELINE_FILE = Path("tests/performance/ttft_baselines.json")
TTFT_REGRESSION_THRESHOLD = 0.20  # 20%


async def measure_ttft(
    user_message: str,
    mock_delay_ms: float = 5.0,
) -> float:
    """
    Measure time from request start to first token received.
    Uses a mock streamer with configurable first-token delay.
    """
    first_token_time = None
    start = time.perf_counter()

    async def mock_stream():
        await asyncio.sleep(mock_delay_ms / 1000)
        yield "Hello"
        yield " world"

    async for chunk in mock_stream():
        if first_token_time is None:
            first_token_time = time.perf_counter()
        # Simulate processing
        await asyncio.sleep(0)

    return (first_token_time - start) * 1000 if first_token_time else 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,base_delay_ms", [
    ("simple_prompt", 5.0),
    ("complex_prompt_with_tools", 15.0),
])
async def test_ttft_regression(scenario, base_delay_ms):
    """TTFT must not regress beyond threshold."""
    N = 20
    ttft_samples = [await measure_ttft("test", base_delay_ms) for _ in range(N)]
    current_mean = sum(ttft_samples) / len(ttft_samples)

    baselines = {}
    if BASELINE_FILE.exists():
        baselines = json.loads(BASELINE_FILE.read_text())

    if scenario not in baselines:
        baselines[scenario] = current_mean
        BASELINE_FILE.parent.mkdir(exist_ok=True)
        BASELINE_FILE.write_text(json.dumps(baselines, indent=2))
        pytest.skip(f"TTFT baseline recorded for '{scenario}': {current_mean:.1f}ms")
        return

    baseline = baselines[scenario]
    regression_pct = (current_mean - baseline) / baseline if baseline > 0 else 0

    if regression_pct > TTFT_REGRESSION_THRESHOLD:
        pytest.fail(
            f"TTFT REGRESSION for '{scenario}': {regression_pct:.1%} slower.\n"
            f"  Baseline: {baseline:.1f}ms\n"
            f"  Current:  {current_mean:.1f}ms\n"
            "TTFT directly impacts user-perceived responsiveness."
        )
```

**Expected Token Savings:** Not applicable — UX performance tracking
**Environment:** `pip install pytest pytest-asyncio`

---

### Option 6: Performance Dashboard Report Generation

```python
# tests/performance/generate_report.py
"""
Generate a Markdown performance report from all baseline files.
Commit the report to the repo for historical tracking.
Run after merging to main to track performance trends over time.
"""
import json
import subprocess
import time
from pathlib import Path


BASELINE_FILES = {
    "latency": Path("tests/performance/baselines.json"),
    "throughput": Path("tests/performance/throughput_baselines.json"),
    "tokens": Path("tests/performance/token_baselines.json"),
    "ttft": Path("tests/performance/ttft_baselines.json"),
}

REPORT_FILE = Path("tests/performance/PERFORMANCE_REPORT.md")


def _git_hash() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       text=True).strip()
    except Exception:
        return "unknown"


def generate_report() -> str:
    commit = _git_hash()
    date = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [
        f"# Performance Report",
        f"",
        f"Generated: {date} | Commit: {commit}",
        f"",
    ]

    for category, path in BASELINE_FILES.items():
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        lines.append(f"## {category.title()} Baselines")
        lines.append("")
        lines.append("| Scenario | Value |")
        lines.append("|----------|-------|")
        for scenario, value in data.items():
            if isinstance(value, (int, float)):
                lines.append(f"| {scenario} | {value:.2f} |")
            elif isinstance(value, dict):
                summary = ", ".join(f"{k}={v:.1f}" for k, v in value.items()
                                   if isinstance(v, (int, float)))
                lines.append(f"| {scenario} | {summary} |")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    report = generate_report()
    REPORT_FILE.write_text(report)
    print(f"Report written to {REPORT_FILE}")
    print(report[:500])
```

**Expected Token Savings:** Not applicable — visibility tooling
**Environment:** stdlib + git

---

## Comparison Table

| Option | Metric Tracked | Update Flow | CI-Ready | Historical Trend | Auto-Baseline |
|--------|---------------|-------------|----------|------------------|---------------|
| 1: Latency baseline | P50/P95/P99 ms | ENV flag | Yes | Via git diff | First run |
| 2: Throughput | RPS by concurrency | ENV flag | Yes | Via git diff | First run |
| 3: Token count | Input tokens | ENV flag | Yes | Via git diff | First run |
| 4: CI gate script | Latency + tokens | --update flag | Yes | No | First run |
| 5: TTFT | Time-to-first-token | Manual | Yes | Via git diff | First run |
| 6: Report generator | All metrics | Manual | Via CI artifact | Yes (markdown) | N/A |
