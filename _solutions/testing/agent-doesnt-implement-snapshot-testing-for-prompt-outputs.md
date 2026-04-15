---
layout: solution
title: "Agent Doesn't Implement Snapshot Testing for Prompt Outputs"
category: testing
description: "Agent prompt outputs change silently between deployments — a refactored system prompt or a model upgrade alters tone, format, or content with no test catching the regression. Snapshot testing locks in approved outputs and flags any deviation."
tags: [testing, snapshot, pytest, regression, prompt-engineering, golden-files, llm-eval]
---

# Agent Doesn't Implement Snapshot Testing for Prompt Outputs

## Problem

An agent's system prompt is refactored for clarity. The unit tests still pass because they only check status codes. But in production, the response format changed from JSON to prose, the tone shifted from concise to verbose, and a downstream parser now crashes. Snapshot tests catch these regressions by comparing current outputs against approved baselines — any change requires an explicit human approval step.

## Solutions

### Option 1: File-Based Snapshot Comparison with pytest

```python
# tests/snapshots/test_agent_snapshots.py
"""
Compare agent outputs against approved snapshot files.
To update a snapshot: delete the .snap file and re-run the test.
To approve a new output: review the diff and commit the new .snap file.
"""
import json
import os
from pathlib import Path
import pytest
import anthropic

SNAPSHOTS_DIR = Path("tests/snapshots/data")
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

UPDATE_SNAPSHOTS = os.environ.get("UPDATE_SNAPSHOTS", "").lower() in ("1", "true", "yes")


def get_agent_response(user_message: str, system_prompt: str) -> str:
    """Call the agent and return its text output."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        temperature=0,  # deterministic for snapshot testing
    )
    return response.content[0].text


def assert_matches_snapshot(name: str, actual: str):
    """Assert actual output matches the stored snapshot."""
    snap_path = SNAPSHOTS_DIR / f"{name}.snap"

    if UPDATE_SNAPSHOTS or not snap_path.exists():
        snap_path.write_text(actual)
        print(f"\n[SNAPSHOT] Written: {snap_path}")
        return  # First run or update mode — always pass

    expected = snap_path.read_text()
    if actual != expected:
        diff_lines = []
        exp_lines = expected.splitlines()
        act_lines = actual.splitlines()
        for i, (e, a) in enumerate(zip(exp_lines, act_lines)):
            if e != a:
                diff_lines.append(f"  Line {i+1}:\n    Expected: {e!r}\n    Actual:   {a!r}")
        if len(act_lines) != len(exp_lines):
            diff_lines.append(f"  Line count: expected {len(exp_lines)}, got {len(act_lines)}")
        pytest.fail(
            f"Snapshot mismatch for '{name}':\n"
            + "\n".join(diff_lines)
            + f"\n\nTo update: UPDATE_SNAPSHOTS=1 pytest {__file__}"
        )


SYSTEM_PROMPT = """You are a concise JSON API assistant.
Always respond in this exact format:
{"answer": "<your answer>", "confidence": "<high|medium|low>"}"""


@pytest.mark.parametrize("name,user_message", [
    ("greeting", "Say hello"),
    ("math_simple", "What is 2 + 2?"),
    ("capital_france", "What is the capital of France?"),
])
def test_agent_output_matches_snapshot(name, user_message):
    actual = get_agent_response(user_message, SYSTEM_PROMPT)
    assert_matches_snapshot(name, actual)
```

```bash
# First run — creates snapshot files:
pytest tests/snapshots/test_agent_snapshots.py

# After a system prompt change, review diffs then approve:
UPDATE_SNAPSHOTS=1 pytest tests/snapshots/test_agent_snapshots.py
git diff tests/snapshots/data/  # review what changed
git add tests/snapshots/data/   # approve if intentional
```

**Expected Token Savings:** ~70% vs running full eval suite (haiku + temperature=0)
**Environment:** `pip install anthropic pytest`

---

### Option 2: Structured Snapshot with Field-Level Assertions

```python
# tests/snapshots/test_structured_snapshots.py
"""
For agents that return structured data (JSON, YAML), snapshot each field
independently. This gives precise diffs: "confidence changed from high to medium"
rather than a raw text diff that's hard to review.
"""
import json
import re
from pathlib import Path
import pytest
import anthropic


SNAPSHOTS_DIR = Path("tests/snapshots/structured")
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def extract_json(text: str) -> dict:
    """Extract the first JSON object from agent output."""
    match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in: {text!r}")
    return json.loads(match.group())


def load_snapshot(name: str) -> dict | None:
    path = SNAPSHOTS_DIR / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def save_snapshot(name: str, data: dict):
    path = SNAPSHOTS_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


SYSTEM_PROMPT = """Respond in JSON: {"answer": "...", "confidence": "high|medium|low", "category": "..."}"""


@pytest.mark.parametrize("name,user_message,tolerance", [
    ("capital_france", "Capital of France?", {"answer": "exact", "confidence": "ignore", "category": "exact"}),
    ("math_2plus2", "What is 2+2?", {"answer": "exact", "confidence": "exact", "category": "ignore"}),
])
def test_structured_snapshot(name, user_message, tolerance):
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        temperature=0,
    )
    actual = extract_json(response.content[0].text)
    snapshot = load_snapshot(name)

    if snapshot is None:
        save_snapshot(name, actual)
        pytest.skip(f"Created new snapshot for '{name}' — run again to validate")
        return

    mismatches = []
    for field, mode in tolerance.items():
        if mode == "ignore":
            continue
        actual_val = actual.get(field, "<missing>")
        expected_val = snapshot.get(field, "<missing>")
        if mode == "exact" and actual_val != expected_val:
            mismatches.append(f"  {field}: expected {expected_val!r}, got {actual_val!r}")
        elif mode == "contains" and expected_val not in str(actual_val):
            mismatches.append(f"  {field}: expected to contain {expected_val!r}, got {actual_val!r}")

    if mismatches:
        pytest.fail(f"Snapshot mismatch for '{name}':\n" + "\n".join(mismatches))
```

**Expected Token Savings:** ~65% (haiku + targeted field assertions skip full eval)
**Environment:** `pip install anthropic pytest`

---

### Option 3: Semantic Snapshot Comparison (Fuzzy Match)

```python
# tests/snapshots/test_semantic_snapshots.py
"""
For freeform text outputs, exact string matching is too brittle —
a single word change in the model's response fails the test even if
the meaning is identical. Semantic snapshots compare meaning, not bytes.
Uses the Claude API itself as a judge (meta-evaluation).
"""
import json
from pathlib import Path
import pytest
import anthropic


SNAPSHOTS_DIR = Path("tests/snapshots/semantic")
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
_client = anthropic.Anthropic()


def get_agent_response(user_message: str) -> str:
    resp = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are a helpful AI assistant. Answer concisely.",
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text


def semantic_match(actual: str, expected: str, threshold: float = 0.8) -> tuple[bool, float, str]:
    """
    Ask Claude to score semantic similarity between actual and expected.
    Returns (passes, score, reasoning).
    """
    resp = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system='You are a semantic similarity judge. Respond ONLY in JSON: {"score": 0.0-1.0, "reasoning": "..."}',
        messages=[{
            "role": "user",
            "content": (
                f"Compare these two texts for semantic equivalence (same meaning/intent):\n\n"
                f"TEXT A: {expected}\n\nTEXT B: {actual}\n\n"
                f"Score 1.0 = identical meaning, 0.0 = completely different."
            ),
        }],
        temperature=0,
    )
    try:
        result = json.loads(resp.content[0].text)
        score = float(result["score"])
        reasoning = result.get("reasoning", "")
        return score >= threshold, score, reasoning
    except Exception:
        return False, 0.0, "Failed to parse judge response"


def load_snapshot(name: str) -> str | None:
    path = SNAPSHOTS_DIR / f"{name}.txt"
    return path.read_text() if path.exists() else None


def save_snapshot(name: str, text: str):
    (SNAPSHOTS_DIR / f"{name}.txt").write_text(text)


@pytest.mark.parametrize("name,user_message", [
    ("explain_async", "What is async/await in Python? One sentence."),
    ("explain_api_key", "What is an API key? One sentence."),
])
def test_semantic_snapshot(name, user_message):
    actual = get_agent_response(user_message)
    expected = load_snapshot(name)

    if expected is None:
        save_snapshot(name, actual)
        pytest.skip(f"Created semantic snapshot for '{name}'")
        return

    passes, score, reasoning = semantic_match(actual, expected, threshold=0.75)
    if not passes:
        pytest.fail(
            f"Semantic snapshot failed for '{name}' (score={score:.2f}):\n"
            f"  Expected: {expected!r}\n"
            f"  Actual:   {actual!r}\n"
            f"  Reason:   {reasoning}"
        )
```

**Expected Token Savings:** 2 haiku calls per test; much cheaper than GPT-4 eval suites
**Environment:** `pip install anthropic pytest`

---

### Option 4: Snapshot CI Workflow with Approval Gate

```python
# tests/snapshots/snapshot_manager.py
"""
CLI tool for managing snapshot lifecycle:
  - show: display current snapshot vs latest output diff
  - approve: replace snapshot with latest output
  - reset: delete snapshot (will be recreated on next test run)
  - audit: list snapshots older than N days (stale warning)
"""
import argparse
import difflib
import json
import os
import time
from pathlib import Path
import anthropic

SNAPSHOTS_DIR = Path("tests/snapshots/data")


def _run_agent(prompt: str) -> str:
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return resp.content[0].text


def cmd_show(args):
    snap_path = SNAPSHOTS_DIR / f"{args.name}.snap"
    if not snap_path.exists():
        print(f"No snapshot found for '{args.name}'")
        return
    expected = snap_path.read_text()
    print(f"Running agent for '{args.name}'...")
    actual = _run_agent(args.prompt)
    if expected == actual:
        print("MATCH — output identical to snapshot.")
    else:
        diff = list(difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"{args.name}.snap (expected)",
            tofile=f"{args.name}.snap (actual)",
        ))
        print("DIFF:")
        print("".join(diff))


def cmd_approve(args):
    snap_path = SNAPSHOTS_DIR / f"{args.name}.snap"
    print(f"Running agent for '{args.name}'...")
    actual = _run_agent(args.prompt)
    snap_path.write_text(actual)
    print(f"Approved and saved: {snap_path}")


def cmd_reset(args):
    snap_path = SNAPSHOTS_DIR / f"{args.name}.snap"
    if snap_path.exists():
        snap_path.unlink()
        print(f"Deleted snapshot: {snap_path}")
    else:
        print(f"No snapshot to delete for '{args.name}'")


def cmd_audit(args):
    max_age_days = args.days
    now = time.time()
    print(f"Snapshots older than {max_age_days} days:")
    for snap in sorted(SNAPSHOTS_DIR.glob("*.snap")):
        age_days = (now - snap.stat().st_mtime) / 86400
        if age_days > max_age_days:
            print(f"  {snap.name}  ({age_days:.0f} days old)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Snapshot manager for agent outputs")
    sub = parser.add_subparsers()
    for cmd, fn in [("show", cmd_show), ("approve", cmd_approve), ("reset", cmd_reset)]:
        p = sub.add_parser(cmd)
        p.add_argument("name")
        p.add_argument("prompt")
        p.set_defaults(func=fn)
    audit_p = sub.add_parser("audit")
    audit_p.add_argument("--days", type=int, default=30)
    audit_p.set_defaults(func=cmd_audit)
    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()
```

```yaml
# .github/workflows/snapshot_check.yml
name: Snapshot Tests
on: [push, pull_request]
jobs:
  snapshots:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install anthropic pytest
      - run: pytest tests/snapshots/ -v
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      - name: Upload snapshots as artifact on failure
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: snapshot-diffs
          path: tests/snapshots/data/
```

**Expected Token Savings:** ~75% (haiku + temperature=0 + targeted test set)
**Environment:** `pip install anthropic pytest`

---

### Option 5: Multi-Turn Conversation Snapshots

```python
# tests/snapshots/test_conversation_snapshots.py
"""
Snapshot entire multi-turn conversations, not just single responses.
Critical for catching regressions in memory, context tracking,
and multi-step reasoning that single-turn tests miss.
"""
import json
from pathlib import Path
import pytest
import anthropic


SNAPSHOTS_DIR = Path("tests/snapshots/conversations")
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
UPDATE = __import__("os").environ.get("UPDATE_SNAPSHOTS", "") == "1"


def run_conversation(turns: list[str], system: str = "") -> list[dict]:
    """Run a multi-turn conversation and return all messages + responses."""
    client = anthropic.Anthropic()
    messages = []
    transcript = []

    for user_turn in turns:
        messages.append({"role": "user", "content": user_turn})
        kwargs = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 256,
            "messages": messages,
            "temperature": 0,
        }
        if system:
            kwargs["system"] = system
        resp = client.messages.create(**kwargs)
        assistant_text = resp.content[0].text
        messages.append({"role": "assistant", "content": assistant_text})
        transcript.append({"user": user_turn, "assistant": assistant_text})

    return transcript


def load_snapshot(name: str) -> list[dict] | None:
    path = SNAPSHOTS_DIR / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def save_snapshot(name: str, transcript: list[dict]):
    path = SNAPSHOTS_DIR / f"{name}.json"
    path.write_text(json.dumps(transcript, indent=2))


@pytest.mark.parametrize("name,turns,system", [
    (
        "preference_memory",
        [
            "My name is Alice and I prefer short answers.",
            "What is Python?",
            "What did I say my preference was?",
        ],
        "You are a helpful assistant. Remember what the user tells you.",
    ),
    (
        "math_chain",
        [
            "Start with the number 10.",
            "Add 5 to it.",
            "Multiply the result by 2.",
            "What is the final number?",
        ],
        "You are a math assistant. Track the running value precisely.",
    ),
])
def test_conversation_snapshot(name, turns, system):
    transcript = run_conversation(turns, system)
    snapshot = load_snapshot(name)

    if snapshot is None or UPDATE:
        save_snapshot(name, transcript)
        if UPDATE:
            print(f"\n[UPDATED] {name}")
        return

    assert len(transcript) == len(snapshot), (
        f"Turn count mismatch: {len(transcript)} vs {len(snapshot)}"
    )
    for i, (actual_turn, expected_turn) in enumerate(zip(transcript, snapshot)):
        assert actual_turn["user"] == expected_turn["user"], f"Turn {i}: user input changed"
        if actual_turn["assistant"] != expected_turn["assistant"]:
            pytest.fail(
                f"Turn {i} response changed for '{name}':\n"
                f"  Expected: {expected_turn['assistant']!r}\n"
                f"  Actual:   {actual_turn['assistant']!r}"
            )
```

**Expected Token Savings:** ~60% vs running full eval; temperature=0 stabilizes outputs
**Environment:** `pip install anthropic pytest`

---

### Option 6: Snapshot Coverage Report

```python
# tests/snapshots/coverage_report.py
"""
Generate a snapshot coverage report: which agent capabilities have snapshots,
which are uncovered, and which snapshots are stale (not run recently).
"""
import json
import time
from pathlib import Path
from dataclasses import dataclass, field


SNAPSHOTS_DIR = Path("tests/snapshots/data")

# Define the full list of capabilities that should have snapshot coverage
REQUIRED_COVERAGE = [
    "greeting",
    "math_simple",
    "math_complex",
    "capital_city",
    "code_generation_python",
    "code_generation_sql",
    "summarization_short",
    "summarization_long",
    "json_output_simple",
    "json_output_nested",
    "error_invalid_input",
    "multilingual_response",
]


@dataclass
class CoverageReport:
    covered: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)  # not run in > 7 days
    extra: list[str] = field(default_factory=list)   # has snapshot but not in required list

    @property
    def coverage_pct(self) -> float:
        total = len(REQUIRED_COVERAGE)
        return len(self.covered) / max(total, 1) * 100

    def print(self):
        print(f"\n{'='*50}")
        print(f"Snapshot Coverage: {self.coverage_pct:.0f}%")
        print(f"  Covered:  {len(self.covered)}/{len(REQUIRED_COVERAGE)}")
        print(f"  Missing:  {len(self.missing)}")
        print(f"  Stale:    {len(self.stale)}")
        print(f"  Extra:    {len(self.extra)}")
        if self.missing:
            print(f"\nMissing snapshots (add tests for these):")
            for name in self.missing:
                print(f"  - {name}")
        if self.stale:
            print(f"\nStale snapshots (not verified recently):")
            for name in self.stale:
                print(f"  - {name}")
        print(f"{'='*50}\n")


def generate_coverage_report(stale_days: int = 7) -> CoverageReport:
    report = CoverageReport()
    existing = {p.stem for p in SNAPSHOTS_DIR.glob("*.snap")}
    now = time.time()
    stale_threshold = now - stale_days * 86400

    for name in REQUIRED_COVERAGE:
        if name in existing:
            snap_path = SNAPSHOTS_DIR / f"{name}.snap"
            if snap_path.stat().st_mtime < stale_threshold:
                report.stale.append(name)
            else:
                report.covered.append(name)
        else:
            report.missing.append(name)

    for name in existing:
        if name not in REQUIRED_COVERAGE:
            report.extra.append(name)

    return report


def test_snapshot_coverage():
    """CI test: fail if coverage drops below 80%."""
    report = generate_coverage_report()
    report.print()
    assert report.coverage_pct >= 80.0, (
        f"Snapshot coverage {report.coverage_pct:.0f}% is below 80%. "
        f"Add snapshots for: {report.missing}"
    )


if __name__ == "__main__":
    report = generate_coverage_report()
    report.print()
```

**Expected Token Savings:** Not applicable — meta-test, no API calls
**Environment:** stdlib + pytest

---

## Comparison Table

| Option | Match Strategy | Multi-Turn | Update Flow | CI-Ready | Stale Detection |
|--------|---------------|------------|-------------|----------|-----------------|
| 1: File-based | Exact string | No | ENV flag | Yes | No |
| 2: Structured fields | Field-level | No | Manual delete | Yes | No |
| 3: Semantic fuzzy | LLM judge | No | Manual delete | Yes | No |
| 4: CLI + approval | Exact + diff | No | CLI approve | Yes | Yes (audit cmd) |
| 5: Conversation | Exact per-turn | Yes | ENV flag | Yes | No |
| 6: Coverage report | N/A (meta) | No | N/A | Yes | Yes |
