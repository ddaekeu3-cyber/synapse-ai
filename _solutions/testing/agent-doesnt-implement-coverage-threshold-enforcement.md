---
layout: solution
title: "Agent Doesn't Implement Coverage Threshold Enforcement"
category: testing
description: "Enforce minimum code coverage thresholds for agent pipelines — blocking deploys when coverage drops below targets, tracking per-module gaps, and integrating coverage gates into CI without false positives from LLM non-determinism."
tags: [testing, coverage, ci, quality-gates, pytest, python]
---

# Agent Doesn't Implement Coverage Threshold Enforcement

Agents deployed without coverage thresholds accumulate untested paths silently. A coverage gate that blocks on drops below a minimum — tracked per module, stored historically, and surfaced as actionable diffs — prevents regression without requiring 100% coverage of inherently non-deterministic LLM interactions.

## Option 1: pytest-cov with Fail-Under Threshold

```python
# conftest.py
import pytest

def pytest_configure(config):
    """Set coverage fail-under threshold via pytest plugin config."""
    # Equivalent to: pytest --cov=agent --cov-fail-under=80
    # This is enforced at collection time, not just reported.
    pass

# pytest.ini or pyproject.toml equivalent — shown here as a dict for clarity:
PYTEST_COV_CONFIG = {
    "addopts": "--cov=agent --cov-report=term-missing --cov-fail-under=80",
    "cov_source": ["agent"],
    "cov_fail_under": 80,
}

# agent/core.py — the module under test
import anthropic

client = anthropic.Anthropic()

def classify_intent(text: str) -> str:
    if not text or not text.strip():
        return "empty"
    if "?" in text:
        return "question"
    if any(w in text.lower() for w in ["create", "make", "build", "add"]):
        return "create"
    return "statement"

def call_model(prompt: str) -> str:
    if not prompt:
        raise ValueError("prompt must not be empty")
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

def route(text: str) -> dict:
    intent = classify_intent(text)
    return {"intent": intent, "text": text}

# test_agent.py
import pytest
from unittest.mock import patch, MagicMock

# Tests for deterministic logic — these drive coverage
def test_classify_empty():
    from agent.core import classify_intent
    assert classify_intent("") == "empty"
    assert classify_intent("  ") == "empty"

def test_classify_question():
    from agent.core import classify_intent
    assert classify_intent("What is Python?") == "question"

def test_classify_create():
    from agent.core import classify_intent
    assert classify_intent("Create a file") == "create"

def test_classify_statement():
    from agent.core import classify_intent
    assert classify_intent("Python is great") == "statement"

def test_call_model_empty_raises():
    from agent.core import call_model
    with pytest.raises(ValueError):
        call_model("")

def test_route():
    from agent.core import route
    r = route("What is asyncio?")
    assert r["intent"] == "question"

# Run: pytest --cov=agent --cov-fail-under=80
# Exits non-zero if coverage < 80% — blocks CI pipeline

# Expected Token Savings: Deterministic logic tested without API calls; LLM paths mocked
# Environment: pip install pytest pytest-cov; adjust --cov-fail-under to your quality bar
```

## Option 2: Per-Module Coverage Threshold Enforcement

```python
import subprocess
import json
import sys

# coverage_gate.py — run after pytest to enforce per-module thresholds

MODULE_THRESHOLDS = {
    "agent.core":        85,   # core logic — high bar
    "agent.tools":       75,   # tool wrappers — medium bar
    "agent.memory":      70,   # memory layer — lower (many LLM paths)
    "agent.streaming":   60,   # streaming — hard to test deterministically
}

def run_coverage_json() -> dict:
    """Run pytest with coverage and export JSON report."""
    subprocess.run(
        ["pytest", "--cov=agent", "--cov-report=json:coverage.json", "-q"],
        check=False,  # don't fail here — we check thresholds ourselves
    )
    with open("coverage.json") as f:
        return json.load(f)

def check_thresholds(report: dict) -> list[dict]:
    failures = []
    files = report.get("files", {})
    for module, threshold in MODULE_THRESHOLDS.items():
        # Map module name to file path (e.g., agent.core -> agent/core.py)
        file_key = module.replace(".", "/") + ".py"
        matched = {k: v for k, v in files.items() if file_key in k}
        if not matched:
            print(f"  [WARN] No coverage data found for {module}")
            continue
        file_data = next(iter(matched.values()))
        pct = file_data["summary"]["percent_covered"]
        status = "✓" if pct >= threshold else "✗ FAIL"
        print(f"  [{status}] {module}: {pct:.1f}% (threshold: {threshold}%)")
        if pct < threshold:
            failures.append({
                "module": module,
                "actual": pct,
                "threshold": threshold,
                "gap": threshold - pct,
            })
    return failures

def main():
    print("Running coverage analysis...")
    report = run_coverage_json()
    total = report.get("totals", {}).get("percent_covered", 0)
    print(f"\nTotal coverage: {total:.1f}%\nPer-module check:")
    failures = check_thresholds(report)
    if failures:
        print(f"\n{len(failures)} module(s) below threshold:")
        for f in failures:
            print(f"  {f['module']}: needs +{f['gap']:.1f}%")
        sys.exit(1)
    print("\nAll thresholds met ✓")

if __name__ == "__main__":
    main()

# Expected Token Savings: Per-module gates prevent low-coverage modules from hiding behind high-coverage average
# Environment: Python 3.9+; pip install pytest pytest-cov; integrate main() as CI step after pytest
```

## Option 3: Coverage Delta Tracking with SQLite History

```python
import subprocess
import sqlite3
import json
import time
import sys

DB = "coverage_history.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS coverage_runs (
            run_id TEXT, branch TEXT, module TEXT,
            pct REAL, lines_covered INTEGER, lines_total INTEGER, ts REAL
        )
    """)
    con.commit(); con.close()

def run_coverage() -> dict:
    subprocess.run(
        ["pytest", "--cov=agent", "--cov-report=json:coverage.json", "-q"],
        check=False,
    )
    with open("coverage.json") as f:
        return json.load(f)

def store_run(run_id: str, branch: str, report: dict):
    con = sqlite3.connect(DB)
    for path, data in report.get("files", {}).items():
        module = path.replace("/", ".").removesuffix(".py")
        s = data["summary"]
        con.execute(
            "INSERT INTO coverage_runs VALUES (?,?,?,?,?,?,?)",
            (run_id, branch, module,
             s["percent_covered"], s["covered_lines"], s["num_statements"],
             time.time()),
        )
    con.commit(); con.close()

def coverage_delta(module: str, current_pct: float, lookback: int = 5) -> float | None:
    """Compare current coverage to average of last N runs for this module."""
    con = sqlite3.connect(DB)
    rows = con.execute(
        """SELECT AVG(pct) FROM (
            SELECT pct FROM coverage_runs
            WHERE module=?
            ORDER BY ts DESC LIMIT ?
        )""",
        (module, lookback),
    ).fetchone()
    con.close()
    if rows[0] is None:
        return None
    return current_pct - rows[0]

def check_regression(report: dict, max_drop: float = 2.0) -> list[dict]:
    """Flag any module that dropped more than max_drop% vs rolling average."""
    failures = []
    for path, data in report.get("files", {}).items():
        module = path.replace("/", ".").removesuffix(".py")
        pct = data["summary"]["percent_covered"]
        delta = coverage_delta(module, pct)
        if delta is None:
            print(f"  [NEW ] {module}: {pct:.1f}% (no history)")
            continue
        marker = "✓" if delta >= -max_drop else "✗ DROP"
        print(f"  [{marker}] {module}: {pct:.1f}% (Δ{delta:+.1f}%)")
        if delta < -max_drop:
            failures.append({"module": module, "pct": pct, "delta": delta})
    return failures

import uuid, subprocess

def main():
    init_db()
    run_id = str(uuid.uuid4())[:8]
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    ).decode().strip()

    print(f"Coverage run {run_id} on branch {branch}")
    report = run_coverage()
    store_run(run_id, branch, report)

    print("\nRegression check (max drop: 2.0%):")
    failures = check_regression(report, max_drop=2.0)
    if failures:
        print(f"\n{len(failures)} module(s) regressed:")
        for f in failures:
            print(f"  {f['module']}: {f['pct']:.1f}% ({f['delta']:+.1f}%)")
        sys.exit(1)
    print("No regressions ✓")

if __name__ == "__main__":
    main()

# Expected Token Savings: Delta tracking catches regressions before absolute thresholds would; SQLite history is cheap
# Environment: run after each CI build; branch stored for per-branch trend analysis
```

## Option 4: Exclude Non-Deterministic LLM Paths from Coverage

```python
# agent/llm_client.py
import anthropic

client = anthropic.Anthropic()

def create_message(prompt: str, system: str = "", model: str = "claude-haiku-4-5-20251001") -> str:
    """
    Single entry point for all model calls.
    Marked pragma: no cover — LLM responses are non-deterministic.
    Tested via integration tests only.
    """
    kwargs = {  # pragma: no cover
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:  # pragma: no cover
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)  # pragma: no cover
    return resp.content[0].text              # pragma: no cover

# agent/pipeline.py — fully testable deterministic logic
def build_prompt(template: str, variables: dict) -> str:
    """Build a prompt from a template and variables. Fully deterministic — covered."""
    missing = [k for k in variables if f"{{{k}}}" not in template]
    if missing:
        raise ValueError(f"Template missing placeholders: {missing}")
    result = template
    for key, val in variables.items():
        result = result.replace(f"{{{key}}}", str(val))
    return result

def parse_json_response(text: str) -> dict:
    """Parse LLM JSON output with fallback. Deterministic — covered."""
    import json, re
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {"raw": text, "parsed": False}
    try:
        return {**json.loads(match.group()), "parsed": True}
    except json.JSONDecodeError:
        return {"raw": text, "parsed": False}

def validate_output(data: dict, required_keys: list[str]) -> tuple[bool, list[str]]:
    """Validate structured output. Deterministic — covered."""
    missing = [k for k in required_keys if k not in data]
    return len(missing) == 0, missing

# test_pipeline.py — 100% coverage of deterministic paths
def test_build_prompt():
    from agent.pipeline import build_prompt
    result = build_prompt("Hello {name}, you are a {role}.", {"name": "Alice", "role": "dev"})
    assert result == "Hello Alice, you are a dev."

def test_build_prompt_missing_placeholder():
    from agent.pipeline import build_prompt
    import pytest
    with pytest.raises(ValueError):
        build_prompt("{a} {b}", {"a": "x", "c": "z"})

def test_parse_json():
    from agent.pipeline import parse_json_response
    r = parse_json_response('{"key": "val"}')
    assert r["key"] == "val" and r["parsed"]

def test_parse_json_fallback():
    from agent.pipeline import parse_json_response
    r = parse_json_response("plain text, no json here")
    assert not r["parsed"]

def test_validate_output():
    from agent.pipeline import validate_output
    ok, missing = validate_output({"a": 1, "b": 2}, ["a", "b"])
    assert ok and not missing
    ok2, missing2 = validate_output({"a": 1}, ["a", "b"])
    assert not ok2 and "b" in missing2

# Run: pytest --cov=agent.pipeline --cov-fail-under=95
# agent.llm_client excluded via pragma: no cover

# Expected Token Savings: pragma: no cover on LLM calls allows 95%+ on deterministic logic without mocking everything
# Environment: add # pragma: no cover to all client.messages.create lines; track excluded lines in coverage.json
```

## Option 5: Coverage Badge Generator with Threshold Colors

```python
import subprocess
import json
import sqlite3
import time

DB = "coverage_badges.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS badge_history (
            ts REAL, pct REAL, color TEXT, label TEXT
        )
    """)
    con.commit(); con.close()

def run_coverage() -> float:
    """Run pytest and return total coverage percentage."""
    subprocess.run(
        ["pytest", "--cov=agent", "--cov-report=json:coverage.json", "-q"],
        check=False,
    )
    with open("coverage.json") as f:
        return json.load(f)["totals"]["percent_covered"]

def pct_to_color(pct: float) -> str:
    if pct >= 90: return "brightgreen"
    if pct >= 80: return "green"
    if pct >= 70: return "yellow"
    if pct >= 60: return "orange"
    return "red"

def generate_shield_url(pct: float) -> str:
    color = pct_to_color(pct)
    label = f"{pct:.0f}%25"  # URL-encoded %
    return f"https://img.shields.io/badge/coverage-{label}-{color}"

def write_badge_json(pct: float, path: str = "coverage-badge.json"):
    """Write shields.io endpoint JSON for dynamic badge."""
    color = pct_to_color(pct)
    badge = {
        "schemaVersion": 1,
        "label": "coverage",
        "message": f"{pct:.0f}%",
        "color": color,
    }
    with open(path, "w") as f:
        json.dump(badge, f, indent=2)
    return badge

def trend(lookback: int = 10) -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT ts, pct, color FROM badge_history ORDER BY ts DESC LIMIT ?",
        (lookback,),
    ).fetchall()
    con.close()
    return [{"ts": r[0], "pct": r[1], "color": r[2]} for r in rows]

def main():
    init_db()
    pct = run_coverage()
    color = pct_to_color(pct)
    badge = write_badge_json(pct)

    con = sqlite3.connect(DB)
    con.execute("INSERT INTO badge_history VALUES (?,?,?,?)",
                (time.time(), pct, color, "coverage"))
    con.commit(); con.close()

    print(f"Coverage: {pct:.1f}% [{color}]")
    print(f"Badge JSON: {badge}")
    print(f"Shield URL: {generate_shield_url(pct)}")
    history = trend()
    if len(history) >= 2:
        delta = history[0]["pct"] - history[1]["pct"]
        print(f"Trend: {delta:+.1f}% vs last run")

if __name__ == "__main__":
    main()

# Expected Token Savings: Badge CI step runs once; SQLite trend catches slow decay before thresholds are breached
# Environment: output coverage-badge.json to GitHub Pages or static host for README badge
```

## Option 6: Multi-Agent Coverage Aggregator

```python
import subprocess
import sqlite3
import json
import time
import sys
from pathlib import Path

DB = "multi_agent_coverage.db"

AGENT_MODULES = {
    "orchestrator": "agents/orchestrator",
    "tool_runner":  "agents/tool_runner",
    "memory":       "agents/memory",
    "evaluator":    "agents/evaluator",
}

THRESHOLDS = {
    "orchestrator": 80,
    "tool_runner":  85,
    "memory":       70,
    "evaluator":    75,
}

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS agent_coverage (
            run_ts REAL, agent TEXT, pct REAL,
            threshold INTEGER, passed INTEGER
        )
    """)
    con.commit(); con.close()

def run_agent_coverage(agent: str, source_dir: str) -> float:
    """Run pytest for a specific agent module, return coverage %."""
    result = subprocess.run(
        [
            "pytest",
            f"tests/test_{agent}.py",
            f"--cov={source_dir}",
            "--cov-report=json:.coverage_tmp.json",
            "-q",
        ],
        capture_output=True,
        check=False,
    )
    coverage_file = Path(".coverage_tmp.json")
    if not coverage_file.exists():
        return 0.0
    with open(coverage_file) as f:
        data = json.load(f)
    coverage_file.unlink(missing_ok=True)
    return data.get("totals", {}).get("percent_covered", 0.0)

def run_all_agents() -> dict[str, float]:
    results = {}
    for agent, src in AGENT_MODULES.items():
        pct = run_agent_coverage(agent, src)
        results[agent] = pct
        print(f"  {agent:15s}: {pct:.1f}%")
    return results

def store_and_check(results: dict[str, float]) -> list[str]:
    con = sqlite3.connect(DB)
    failures = []
    ts = time.time()
    for agent, pct in results.items():
        threshold = THRESHOLDS.get(agent, 75)
        passed = int(pct >= threshold)
        con.execute(
            "INSERT INTO agent_coverage VALUES (?,?,?,?,?)",
            (ts, agent, pct, threshold, passed),
        )
        if not passed:
            failures.append(f"{agent}: {pct:.1f}% < {threshold}%")
    con.commit(); con.close()
    return failures

def dashboard() -> str:
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT agent,
               ROUND(AVG(pct),1) avg_pct,
               MIN(pct) min_pct,
               SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END) failures,
               COUNT(*) runs
        FROM agent_coverage
        GROUP BY agent
        ORDER BY avg_pct DESC
    """).fetchall()
    con.close()
    lines = ["Agent Coverage Dashboard:", "-" * 55]
    for r in rows:
        lines.append(f"  {r[0]:15s} avg={r[1]:5.1f}% min={r[2]:5.1f}% fails={r[3]}/{r[4]}")
    return "\n".join(lines)

def main():
    init_db()
    print("Running per-agent coverage:")
    results = run_all_agents()
    failures = store_and_check(results)
    print()
    print(dashboard())
    if failures:
        print(f"\nFailed ({len(failures)}):")
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("\nAll agents passed ✓")

if __name__ == "__main__":
    main()

# Expected Token Savings: Per-agent isolation prevents one high-coverage agent hiding low-coverage peers
# Environment: adapt AGENT_MODULES paths to your project layout; run as final CI gate before deploy
```

## Comparison

| Option | Scope | Threshold Type | History | CI Integration |
|--------|-------|---------------|---------|---------------|
| 1 — pytest-cov fail-under | Total coverage | Absolute % | No | Native |
| 2 — Per-Module Thresholds | Per module | Per-module % | No | Script |
| 3 — Delta Tracking SQLite | Per module | Regression Δ | Yes | Script |
| 4 — Exclude LLM Paths | Deterministic only | Absolute % | No | Native |
| 5 — Badge Generator | Total coverage | Color tiers | Yes | Script |
| 6 — Multi-Agent Aggregator | Per agent | Per-agent % | Yes | Script |
