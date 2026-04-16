---
layout: solution
title: "Agent Doesn't Implement Data-Driven Test Generation from Production Logs"
description: "How to automatically extract real-world test cases from production traffic, turning live failures and edge cases into a continuously growing regression suite."
tags: [testing, data-driven, production, logs, regression, automation]
difficulty: intermediate
solution_count: 6
---

## Problem

Agent test suites are written by engineers who imagine failure cases. Real users find edge cases that no one imagined. When a production incident occurs — a strange input, an unexpected tool output, an unusual conversation pattern — it gets fixed and forgotten. The same class of input can break the agent again months later because no test was ever written for it.

```python
# Bad: tests only cover cases engineers thought of
def test_summarize_english(): ...
def test_summarize_empty_string(): ...
# Meanwhile, production saw: summarize(mixed_script + emoji + malformed_json)
# — no test exists, it will break again
```

---

## Solution 1 — Log Capture Decorator That Emits Pytest Fixtures

Wrap every agent entrypoint with a decorator that records inputs/outputs to a JSONL file. A separate script replays them as pytest test cases.

```python
import asyncio
import json
import time
import functools
from pathlib import Path
from typing import Any, Callable, Awaitable

CAPTURE_PATH = Path("/var/log/agent/captured_calls.jsonl")

def capture_for_testing(func: Callable[..., Awaitable[Any]]):
    """Decorator: records every call to JSONL for later test generation."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        t0 = time.time()
        try:
            result = await func(*args, **kwargs)
            entry = {
                "fn": func.__name__,
                "args": _safe_serialize(args),
                "kwargs": _safe_serialize(kwargs),
                "result": _safe_serialize(result),
                "error": None,
                "ts": t0,
                "duration_ms": (time.time() - t0) * 1000,
            }
            _append(entry)
            return result
        except Exception as exc:
            entry = {
                "fn": func.__name__,
                "args": _safe_serialize(args),
                "kwargs": _safe_serialize(kwargs),
                "result": None,
                "error": {"type": type(exc).__name__, "msg": str(exc)},
                "ts": t0,
                "duration_ms": (time.time() - t0) * 1000,
            }
            _append(entry)
            raise
    return wrapper

def _safe_serialize(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)

def _append(entry: dict) -> None:
    with open(CAPTURE_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")

# Production code
@capture_for_testing
async def summarize(text: str, max_length: int = 200) -> str:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_length,
        messages=[{"role": "user", "content": f"Summarize in {max_length} chars: {text}"}],
    )
    return response.content[0].text

# --- Test generator script (run offline) ---
def generate_pytest_file(capture_path: str, output_path: str,
                          sample_n: int = 50) -> None:
    import random
    entries = []
    with open(capture_path) as f:
        for line in f:
            entries.append(json.loads(line))

    # Sample: include all errors, random sample of successes
    errors = [e for e in entries if e["error"]]
    successes = [e for e in entries if not e["error"]]
    selected = errors + random.sample(successes, min(sample_n, len(successes)))

    lines = [
        "import pytest",
        "import asyncio",
        "from agent import summarize",
        "",
        f"# Auto-generated from {len(selected)} production calls",
        "",
        "@pytest.mark.parametrize('text,max_length,expected_error', [",
    ]
    for e in selected:
        text = json.dumps(e["kwargs"].get("text", e["args"][0] if e["args"] else ""))
        ml = e["kwargs"].get("max_length", 200)
        err = json.dumps(e["error"]["type"] if e["error"] else None)
        lines.append(f"    ({text}, {ml}, {err}),")
    lines += [
        "])",
        "async def test_production_replay(text, max_length, expected_error):",
        "    if expected_error:",
        "        with pytest.raises(Exception):",
        "            await summarize(text, max_length)",
        "    else:",
        "        result = await summarize(text, max_length)",
        "        assert isinstance(result, str)",
        "        assert len(result) > 0",
    ]

    Path(output_path).write_text("\n".join(lines))
    print(f"Generated {len(selected)} test cases -> {output_path}")

generate_pytest_file(
    str(CAPTURE_PATH),
    "tests/test_production_replay.py",
    sample_n=100,
)
```

---

## Solution 2 — Error-Cluster Sampling: One Test Per Unique Failure Mode

Parse production error logs, cluster similar errors by message pattern, and emit one representative test case per cluster. Avoids test bloat from thousands of identical errors.

```python
import json
import re
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict

@dataclass
class ErrorEntry:
    ts: float
    error_type: str
    error_msg: str
    input_text: str
    input_kwargs: dict
    raw: dict

def normalize_error(msg: str) -> str:
    """Strip variable parts to get a stable cluster key."""
    msg = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", msg)  # UUIDs, hashes
    msg = re.sub(r"\d+", "<N>", msg)                  # numbers
    msg = re.sub(r"'[^']{50,}'", "'<long_str>'", msg) # long strings
    return msg.lower().strip()

def cluster_errors(log_path: str) -> dict[str, list[ErrorEntry]]:
    clusters: dict[str, list[ErrorEntry]] = defaultdict(list)

    with open(log_path) as f:
        for line in f:
            entry = json.loads(line)
            if not entry.get("error"):
                continue

            err = ErrorEntry(
                ts=entry["ts"],
                error_type=entry["error"]["type"],
                error_msg=entry["error"]["msg"],
                input_text=entry["kwargs"].get("text", ""),
                input_kwargs=entry["kwargs"],
                raw=entry,
            )
            cluster_key = f"{err.error_type}:{normalize_error(err.error_msg)}"
            fingerprint = hashlib.md5(cluster_key.encode()).hexdigest()[:8]
            clusters[fingerprint].append(err)

    return dict(clusters)

def emit_cluster_tests(clusters: dict[str, list[ErrorEntry]],
                        output_path: str) -> None:
    lines = [
        "import pytest",
        "from agent import summarize",
        "",
        "# One test per unique error cluster from production",
        "",
    ]
    for cluster_id, entries in clusters.items():
        representative = entries[0]
        test_name = re.sub(r"[^a-z0-9_]", "_",
                           f"test_{representative.error_type}_{cluster_id}").lower()
        input_repr = json.dumps(representative.input_text[:200])
        kwargs_repr = {k: v for k, v in representative.input_kwargs.items()
                      if k != "text"}
        lines += [
            f"# Cluster {cluster_id}: {len(entries)} occurrences",
            f"# Error: {representative.error_type}: {representative.error_msg[:80]}",
            f"@pytest.mark.asyncio",
            f"async def {test_name}():",
            f"    # This input caused {len(entries)} production failures",
            f"    with pytest.raises({representative.error_type}):",
            f"        await summarize({input_repr}, **{kwargs_repr!r})",
            "",
        ]

    Path(output_path).write_text("\n".join(lines))
    print(f"Emitted {len(clusters)} cluster tests -> {output_path}")

clusters = cluster_errors("/var/log/agent/captured_calls.jsonl")
emit_cluster_tests(clusters, "tests/test_error_clusters.py")
print(f"Found {len(clusters)} unique error patterns")
for cid, entries in list(clusters.items())[:5]:
    print(f"  [{cid}] {len(entries)}x — {entries[0].error_type}: {entries[0].error_msg[:60]}")
```

---

## Solution 3 — LLM-Augmented Test Case Generation from Production Inputs

Feed sampled production inputs to an LLM to generate variations and expected assertions, producing a richer test suite than replay alone.

```python
import asyncio
import json
import random
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

AUGMENT_PROMPT = """\
You are a test engineer. Given a production input to an AI agent, generate 3 test cases that cover:
1. The original input exactly
2. A slight variation that might expose edge cases
3. A boundary/stress version

Production input: {input_text}
Agent function: {fn_name}

Return JSON array with 3 objects, each having:
- "input": the test input string
- "should_succeed": true/false
- "expected_properties": list of strings describing what a correct response should have
- "test_name": a snake_case test name

Return ONLY the JSON array, no explanation."""

async def augment_production_sample(entry: dict) -> list[dict]:
    """Use LLM to generate test variants from one production call."""
    input_text = entry["kwargs"].get("text", "")
    if len(input_text) < 5:
        return []

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": AUGMENT_PROMPT.format(
                    input_text=input_text[:500],
                    fn_name=entry["fn"],
                )
            }],
        )
        text = response.content[0].text.strip()
        start = text.find("[")
        end = text.rfind("]") + 1
        if start == -1 or end == 0:
            return []
        cases = json.loads(text[start:end])
        return cases
    except Exception as e:
        print(f"Augment failed: {e}")
        return []

async def generate_augmented_tests(log_path: str, output_path: str,
                                    sample_n: int = 20) -> None:
    entries = []
    with open(log_path) as f:
        for line in f:
            entries.append(json.loads(line))

    # Sample a mix of successes and failures
    errors = [e for e in entries if e.get("error")]
    successes = [e for e in entries if not e.get("error")]
    selected = (errors[:10] +
                random.sample(successes, min(sample_n - len(errors), len(successes))))

    # Augment in parallel (rate limited)
    semaphore = asyncio.Semaphore(5)
    async def augment_one(entry: dict) -> list[dict]:
        async with semaphore:
            return await augment_production_sample(entry)

    all_variants = await asyncio.gather(*[augment_one(e) for e in selected])
    flat_variants = [v for variants in all_variants for v in variants]

    # Write pytest file
    lines = [
        "import pytest",
        "from agent import summarize",
        "",
        f"# {len(flat_variants)} LLM-augmented test cases from {len(selected)} production samples",
        "",
        "@pytest.mark.parametrize('input_text,should_succeed,expected_props,test_name', [",
    ]
    for v in flat_variants:
        lines.append(
            f"    ({json.dumps(v.get('input',''))}, "
            f"{v.get('should_succeed', True)}, "
            f"{json.dumps(v.get('expected_properties', []))}, "
            f"{json.dumps(v.get('test_name', 'unnamed'))}),",
        )
    lines += [
        "])",
        "@pytest.mark.asyncio",
        "async def test_augmented(input_text, should_succeed, expected_props, test_name):",
        "    if should_succeed:",
        "        result = await summarize(input_text)",
        "        for prop in expected_props:",
        "            # Properties are documentation — use LLM judge for real assertion",
        "            assert isinstance(result, str), f'Expected str, got {type(result)}'",
        "    else:",
        "        with pytest.raises(Exception):",
        "            await summarize(input_text)",
    ]

    Path(output_path).write_text("\n".join(lines))
    print(f"Generated {len(flat_variants)} augmented tests -> {output_path}")

asyncio.run(generate_augmented_tests(
    "/var/log/agent/captured_calls.jsonl",
    "tests/test_augmented.py",
    sample_n=20,
))
```

---

## Solution 4 — Continuous Test Harvesting with Deduplication

Run a background job that continuously reads new production log entries, deduplicates by input fingerprint, and adds novel test cases to the suite automatically.

```python
import asyncio
import hashlib
import json
import time
from pathlib import Path
from dataclasses import dataclass

@dataclass
class TestHarvesterConfig:
    log_path: str
    test_output_dir: str
    poll_interval: float = 300.0  # 5 minutes
    max_tests_per_run: int = 10
    dedup_store: str = "tests/harvested_fingerprints.json"

def fingerprint(entry: dict) -> str:
    """Stable fingerprint of an input, ignoring timestamp and session."""
    key_fields = {
        "fn": entry.get("fn", ""),
        "kwargs": entry.get("kwargs", {}),
    }
    return hashlib.sha256(json.dumps(key_fields, sort_keys=True).encode()).hexdigest()

class TestHarvester:
    def __init__(self, config: TestHarvesterConfig):
        self._config = config
        self._seen: set[str] = self._load_dedup()
        self._test_dir = Path(config.test_output_dir)
        self._test_dir.mkdir(parents=True, exist_ok=True)

    def _load_dedup(self) -> set[str]:
        path = Path(self._config.dedup_store)
        if path.exists():
            return set(json.loads(path.read_text()))
        return set()

    def _save_dedup(self) -> None:
        Path(self._config.dedup_store).write_text(
            json.dumps(list(self._seen), indent=2)
        )

    def harvest_new(self) -> list[dict]:
        novel = []
        with open(self._config.log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fp = fingerprint(entry)
                if fp not in self._seen:
                    self._seen.add(fp)
                    novel.append(entry)
                    if len(novel) >= self._config.max_tests_per_run:
                        break
        self._save_dedup()
        return novel

    def write_test_file(self, entries: list[dict]) -> str:
        ts = int(time.time())
        filename = f"test_harvested_{ts}.py"
        path = self._test_dir / filename

        lines = [
            "import pytest",
            "from agent import summarize",
            f"# Harvested {len(entries)} novel production inputs at {ts}",
            "",
        ]
        for i, entry in enumerate(entries):
            text = entry["kwargs"].get("text", "")
            had_error = bool(entry.get("error"))
            err_type = entry["error"]["type"] if had_error else None
            lines += [
                f"@pytest.mark.asyncio",
                f"async def test_harvested_{ts}_{i}():",
                f"    # Production input (error={had_error})",
                f"    text = {json.dumps(text[:300])}",
            ]
            if had_error:
                lines += [
                    f"    with pytest.raises(Exception):  # was {err_type} in production",
                    f"        await summarize(text)",
                ]
            else:
                lines += [
                    f"    result = await summarize(text)",
                    f"    assert isinstance(result, str)",
                ]
            lines.append("")

        path.write_text("\n".join(lines))
        print(f"Harvested {len(entries)} tests -> {filename}")
        return str(path)

    async def run_forever(self) -> None:
        print(f"Harvester started. Polling every {self._config.poll_interval}s")
        while True:
            novel = self.harvest_new()
            if novel:
                self.write_test_file(novel)
            await asyncio.sleep(self._config.poll_interval)

config = TestHarvesterConfig(
    log_path="/var/log/agent/captured_calls.jsonl",
    test_output_dir="tests/harvested/",
    poll_interval=300.0,
    max_tests_per_run=20,
)
harvester = TestHarvester(config)
asyncio.run(harvester.run_forever())
```

---

## Solution 5 — Slow Request Mining: Extract Tests from High-Latency Calls

Identify production calls that were unusually slow, then use them as test cases with latency assertions — turning performance anomalies into performance regression tests.

```python
import json
import time
from pathlib import Path
from dataclasses import dataclass
from statistics import mean, stdev
from typing import Iterator

@dataclass
class LatencyProfile:
    fn: str
    p50_ms: float
    p95_ms: float
    p99_ms: float

def load_entries(log_path: str) -> Iterator[dict]:
    with open(log_path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass

def compute_latency_profile(log_path: str, fn_name: str) -> LatencyProfile:
    durations = [
        e["duration_ms"]
        for e in load_entries(log_path)
        if e.get("fn") == fn_name and not e.get("error")
    ]
    if not durations:
        return LatencyProfile(fn_name, 0, 0, 0)

    durations.sort()
    n = len(durations)
    return LatencyProfile(
        fn=fn_name,
        p50_ms=durations[int(n * 0.50)],
        p95_ms=durations[int(n * 0.95)],
        p99_ms=durations[int(n * 0.99)],
    )

def mine_slow_requests(log_path: str, fn_name: str,
                        slow_multiplier: float = 3.0,
                        max_tests: int = 20) -> list[dict]:
    """Return entries that were slow_multiplier times the p95 latency."""
    profile = compute_latency_profile(log_path, fn_name)
    threshold_ms = profile.p95_ms * slow_multiplier

    slow_entries = [
        e for e in load_entries(log_path)
        if (e.get("fn") == fn_name
            and not e.get("error")
            and e.get("duration_ms", 0) > threshold_ms)
    ]
    slow_entries.sort(key=lambda e: -e["duration_ms"])
    return slow_entries[:max_tests]

def emit_latency_tests(log_path: str, fn_name: str, output_path: str) -> None:
    profile = compute_latency_profile(log_path, fn_name)
    slow = mine_slow_requests(log_path, fn_name)

    # Budget: inputs that were slow in production should complete within 2x p99
    budget_ms = profile.p99_ms * 2.0

    lines = [
        "import pytest",
        "import asyncio",
        "import time",
        "from agent import summarize",
        "",
        f"# Latency regression tests mined from production slow calls",
        f"# p50={profile.p50_ms:.0f}ms p95={profile.p95_ms:.0f}ms p99={profile.p99_ms:.0f}ms",
        f"LATENCY_BUDGET_MS = {budget_ms:.0f}",
        "",
        "@pytest.mark.parametrize('text,original_duration_ms', [",
    ]
    for e in slow:
        text = e["kwargs"].get("text", "")
        lines.append(f"    ({json.dumps(text[:200])}, {e['duration_ms']:.0f}),")
    lines += [
        "])",
        "@pytest.mark.asyncio",
        "async def test_latency_regression(text, original_duration_ms):",
        "    t0 = time.monotonic()",
        "    await summarize(text)",
        "    elapsed_ms = (time.monotonic() - t0) * 1000",
        "    assert elapsed_ms < LATENCY_BUDGET_MS, (",
        "        f'Latency regression: {elapsed_ms:.0f}ms > budget {LATENCY_BUDGET_MS:.0f}ms'",
        "        f' (original was {original_duration_ms:.0f}ms in production)'",
        "    )",
    ]

    Path(output_path).write_text("\n".join(lines))
    print(f"Emitted {len(slow)} latency regression tests -> {output_path}")

emit_latency_tests(
    "/var/log/agent/captured_calls.jsonl",
    "summarize",
    "tests/test_latency_regression.py",
)
```

---

## Solution 6 — CI Pipeline Integration: Auto-PR with Harvested Tests

Run the test harvester in CI on a schedule, commit new test files, and open a PR for human review before merging into the suite.

```python
#!/usr/bin/env python3
"""
ci_harvest.py — runs in CI to harvest and PR new production tests.
Usage: python ci_harvest.py --log-path ... --output-dir ...
"""
import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()

def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()

async def harvest_and_pr(log_path: str, output_dir: str,
                          max_tests: int = 50) -> None:
    from test_harvester import TestHarvester, TestHarvesterConfig  # from Solution 4

    config = TestHarvesterConfig(
        log_path=log_path,
        test_output_dir=output_dir,
        max_tests_per_run=max_tests,
    )
    harvester = TestHarvester(config)
    novel = harvester.harvest_new()

    if not novel:
        print("No novel production inputs found — skipping PR")
        return

    test_file = harvester.write_test_file(novel)
    branch = f"auto/harvested-tests-{int(time.time())}"

    git("checkout", "-b", branch)
    git("add", test_file, config.dedup_store)
    git("commit", "-m",
        f"test: add {len(novel)} production-harvested regression tests\n\n"
        f"Auto-generated by ci_harvest.py from {log_path}\n"
        f"Review inputs before merging to ensure no PII leaks.")

    git("push", "origin", branch)

    pr_url = gh(
        "pr", "create",
        "--title", f"Auto: {len(novel)} production regression tests",
        "--body", (
            f"## Production Test Harvest\n\n"
            f"Automatically generated {len(novel)} test cases from production logs.\n\n"
            f"**Review checklist:**\n"
            f"- [ ] No PII in test inputs\n"
            f"- [ ] Inputs are representative of real failures\n"
            f"- [ ] Tests pass locally\n\n"
            f"Source: `{log_path}`"
        ),
        "--base", "main",
        "--head", branch,
    )
    print(f"PR created: {pr_url}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--output-dir", default="tests/harvested/")
    parser.add_argument("--max-tests", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(harvest_and_pr(args.log_path, args.output_dir, args.max_tests))
```

```yaml
# .github/workflows/harvest_tests.yml
name: Harvest Production Tests
on:
  schedule:
    - cron: "0 6 * * 1"  # every Monday morning
  workflow_dispatch:

jobs:
  harvest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[test]"
      - run: python ci_harvest.py --log-path ${{ secrets.PROD_LOG_PATH }} --max-tests 50
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

---

## Comparison

| Approach | Effort | Finds Unknown Unknowns | Performance Tests | Auto-PR | Best For |
|---|---|---|---|---|---|
| Log capture + replay | Low | **Yes** | No | No | Quick regression capture |
| Error cluster sampling | Low | **Yes** | No | No | Deduplicating failure modes |
| LLM-augmented variants | Medium | **Yes** (variations) | No | No | Richer test coverage |
| Continuous harvesting | Medium | **Yes** | No | No | Ongoing test growth |
| Slow request mining | Low | No | **Yes** | No | Latency regression prevention |
| CI harvest + auto-PR | High | **Yes** | Partial | **Yes** | Full automation |
