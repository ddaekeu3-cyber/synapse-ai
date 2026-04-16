---
title: "Agent doesn't implement golden master testing for agent workflows"
description: "There's no record of what a correct end-to-end agent run looks like. Prompt changes, tool schema updates, and model version bumps silently alter behavior with no automated way to detect regressions."
difficulty: intermediate
category: testing
tags: [golden-master, snapshot-testing, regression-testing, approval-testing, workflow-testing]
---

## Problem

Unit tests verify individual functions in isolation. But for AI agents, the risk is in the composed behavior — the full sequence of tool calls, the final response format, the decision to ask a clarifying question versus proceeding. Without golden master tests, a prompt tweak that was intended to improve tone silently changes the agent's tool selection logic, and nobody notices until a production incident.

Golden master testing (also called "approval testing" or "snapshot testing") captures the entire output of a workflow run and compares future runs against it. Any deviation is flagged for human review.

```python
# BAD: only verifies the response is non-empty — not what it contains
def test_agent_search_workflow():
    result = run_agent("Find the top 3 Python async libraries")
    assert len(result) > 0  # always passes, even if agent broke
```

## Solution 1: File-based snapshot testing with approval workflow

Serialize the complete agent output (response text + tool calls made) to a `.golden` file. On the next run, compare against the stored file. If they differ, the test fails and shows a diff.

```python
import json
import os
import difflib
from pathlib import Path
from typing import Any
import pytest


GOLDEN_DIR = Path("tests/golden")
GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN", "0") == "1"


def golden_path(test_name: str) -> Path:
    return GOLDEN_DIR / f"{test_name}.json"


def normalize_output(output: dict) -> dict:
    """Remove non-deterministic fields before comparison."""
    out = dict(output)
    out.pop("timestamp", None)
    out.pop("request_id", None)
    out.pop("latency_ms", None)
    # Normalize model version stamps in text (e.g. "claude-sonnet-4-6" variants)
    if "response" in out:
        out["response"] = out["response"].strip()
    return out


def assert_matches_golden(test_name: str, actual: dict):
    """
    Compare actual output to golden file.
    Set UPDATE_GOLDEN=1 to regenerate golden files.
    """
    path = golden_path(test_name)
    normalized = normalize_output(actual)

    if UPDATE_GOLDEN or not path.exists():
        path.write_text(json.dumps(normalized, indent=2))
        pytest.skip(f"Golden file written: {path}")
        return

    expected_text = path.read_text()
    actual_text = json.dumps(normalized, indent=2)

    if expected_text != actual_text:
        diff = "\n".join(
            difflib.unified_diff(
                expected_text.splitlines(),
                actual_text.splitlines(),
                fromfile=f"{test_name}.golden",
                tofile="actual",
                lineterm="",
            )
        )
        pytest.fail(
            f"Output diverged from golden master.\n"
            f"Run with UPDATE_GOLDEN=1 to approve changes.\n\n{diff}"
        )


# ── Simulated agent run ───────────────────────────────────────────────
def run_search_workflow(query: str) -> dict:
    """Simulated agent that searches and summarizes."""
    return {
        "query": query,
        "tool_calls": [
            {"tool": "web_search", "args": {"query": query}, "result_count": 5},
            {"tool": "summarize", "args": {"source_count": 5}},
        ],
        "response": f"Here are the top results for '{query}':\n1. asyncio\n2. trio\n3. anyio",
        "model": "claude-sonnet-4-6",
    }


# ── Test ──────────────────────────────────────────────────────────────
def test_search_workflow_golden():
    result = run_search_workflow("top Python async libraries")
    assert_matches_golden("search_workflow_async_libraries", result)
```

## Solution 2: Structured golden master with field-level tolerance

Not all fields need exact matching. Response text can change slightly (whitespace, punctuation) while tool call sequences must match exactly. Define per-field comparison strategies.

```python
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable
import re


@dataclass
class FieldRule:
    exact: bool = True
    normalize: Callable[[Any], Any] | None = None
    ignore: bool = False


FIELD_RULES: dict[str, FieldRule] = {
    "response": FieldRule(
        exact=False,
        normalize=lambda s: re.sub(r"\s+", " ", s.lower().strip()),
    ),
    "tool_calls": FieldRule(exact=True),
    "model": FieldRule(ignore=True),   # model version allowed to change
    "latency_ms": FieldRule(ignore=True),
    "query": FieldRule(exact=True),
}


def structured_diff(golden: dict, actual: dict) -> list[str]:
    diffs = []
    all_keys = set(golden) | set(actual)

    for key in sorted(all_keys):
        rule = FIELD_RULES.get(key, FieldRule(exact=True))
        if rule.ignore:
            continue

        g_val = golden.get(key, "<missing>")
        a_val = actual.get(key, "<missing>")

        if rule.normalize:
            g_val = rule.normalize(g_val) if isinstance(g_val, str) else g_val
            a_val = rule.normalize(a_val) if isinstance(a_val, str) else a_val

        if g_val != a_val:
            match_type = "EXACT" if rule.exact else "NORMALIZED"
            diffs.append(f"[{match_type}] {key}:\n  golden: {g_val!r}\n  actual: {a_val!r}")

    return diffs


class StructuredGoldenStore:
    def __init__(self, base_dir: str = "tests/golden"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def record(self, name: str, output: dict):
        path = self.base_dir / f"{name}.json"
        path.write_text(json.dumps(output, indent=2, default=str))
        print(f"Golden recorded: {path}")

    def verify(self, name: str, actual: dict) -> list[str]:
        path = self.base_dir / f"{name}.json"
        if not path.exists():
            self.record(name, actual)
            return []  # first run — record as baseline

        golden = json.loads(path.read_text())
        return structured_diff(golden, actual)

    def assert_matches(self, name: str, actual: dict):
        diffs = self.verify(name, actual)
        if diffs:
            raise AssertionError(
                f"Golden master mismatch for '{name}':\n" + "\n".join(diffs)
            )


# ── Usage ────────────────────────────────────────────────────────────
store = StructuredGoldenStore()

def test_structured_golden():
    result = {
        "query": "top Python async libraries",
        "tool_calls": [{"tool": "web_search"}, {"tool": "summarize"}],
        "response": "The top async libraries are asyncio, trio, and anyio.",
        "model": "claude-sonnet-4-6",
        "latency_ms": 1234,  # ignored
    }
    store.assert_matches("async_library_search", result)
```

## Solution 3: Interaction-level golden master — tool call sequence recorder

Record not just the final output but the complete agent interaction log: every tool call, every argument, every result, every model turn. Any change in decision-making is detected.

```python
import asyncio
import json
import hashlib
from pathlib import Path
from typing import Any
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

GOLDEN_DIR = Path("tests/golden/interactions")
GOLDEN_DIR.mkdir(parents=True, exist_ok=True)


class InteractionRecorder:
    """Records every step of an agentic loop."""

    def __init__(self):
        self.steps: list[dict] = []

    def record_tool_call(self, tool_name: str, args: dict, result: Any):
        self.steps.append({
            "type": "tool_call",
            "tool": tool_name,
            "args": args,
            "result_hash": hashlib.sha256(
                json.dumps(result, sort_keys=True, default=str).encode()
            ).hexdigest()[:12],
        })

    def record_model_turn(self, role: str, content_summary: str, stop_reason: str):
        self.steps.append({
            "type": "model_turn",
            "role": role,
            "content_length": len(content_summary),
            "content_prefix": content_summary[:100],
            "stop_reason": stop_reason,
        })

    def to_dict(self) -> dict:
        return {"step_count": len(self.steps), "steps": self.steps}


async def run_agent_with_recorder(
    prompt: str, recorder: InteractionRecorder
) -> str:
    """Simulated agentic loop with recording."""
    tools = [{"name": "web_search", "description": "Search the web"}]

    # Turn 1: model decides to search
    recorder.record_model_turn("assistant", "I'll search for that information.", "tool_use")
    recorder.record_tool_call("web_search", {"query": prompt}, ["result1", "result2"])
    # Turn 2: model summarizes
    recorder.record_model_turn("assistant", "Based on the search results...", "end_turn")
    return "Final answer based on search results."


def golden_path_for(prompt: str) -> Path:
    key = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    return GOLDEN_DIR / f"{key}.json"


async def assert_interaction_matches_golden(prompt: str):
    recorder = InteractionRecorder()
    result = await run_agent_with_recorder(prompt, recorder)
    interaction = recorder.to_dict()
    interaction["prompt"] = prompt
    interaction["result_prefix"] = result[:200]

    path = golden_path_for(prompt)
    if not path.exists():
        path.write_text(json.dumps(interaction, indent=2))
        print(f"Golden interaction recorded: {path}")
        return

    golden = json.loads(path.read_text())

    # Compare step count and step types
    if golden["step_count"] != interaction["step_count"]:
        raise AssertionError(
            f"Step count changed: {golden['step_count']} → {interaction['step_count']}"
        )

    for i, (g_step, a_step) in enumerate(zip(golden["steps"], interaction["steps"])):
        if g_step["type"] != a_step["type"]:
            raise AssertionError(f"Step {i} type changed: {g_step['type']} → {a_step['type']}")
        if g_step["type"] == "tool_call" and g_step["tool"] != a_step["tool"]:
            raise AssertionError(f"Step {i} tool changed: {g_step['tool']} → {a_step['tool']}")

    print(f"Golden interaction matches: {path.name}")


asyncio.run(assert_interaction_matches_golden("Find top Python async libraries"))
```

## Solution 4: Parameterized golden master test suite from a scenario catalog

Define a catalog of test scenarios in YAML. Each scenario has a prompt and optional expected properties. The test runner executes each and compares to stored golden files.

```python
import asyncio
import json
import yaml
from pathlib import Path
from typing import Any
import pytest


SCENARIO_CATALOG = """
scenarios:
  - id: simple_factual
    prompt: "What is the capital of Japan?"
    expect:
      contains: ["Tokyo"]
      tool_calls_max: 1

  - id: code_debug
    prompt: "Fix this Python bug: def add(a, b) return a + b"
    expect:
      contains: ["def add", "return"]
      tool_calls_max: 2

  - id: multi_step_research
    prompt: "Compare asyncio and trio for production use"
    expect:
      min_length: 200
      tool_calls_min: 1
"""


class ScenarioCatalog:
    def __init__(self, yaml_str: str):
        self.scenarios = yaml.safe_load(yaml_str)["scenarios"]

    def get(self, scenario_id: str) -> dict | None:
        return next((s for s in self.scenarios if s["id"] == scenario_id), None)


catalog = ScenarioCatalog(SCENARIO_CATALOG)
GOLDEN_DIR = Path("tests/golden/scenarios")
GOLDEN_DIR.mkdir(parents=True, exist_ok=True)


def run_scenario(prompt: str) -> dict:
    """Simulated agent execution — replace with real agent call."""
    return {
        "response": f"Answer to: {prompt}. Tokyo is the capital of Japan.",
        "tool_calls": [{"tool": "web_search"}],
        "tokens": 150,
    }


def check_expectations(result: dict, expect: dict) -> list[str]:
    failures = []
    if "contains" in expect:
        for phrase in expect["contains"]:
            if phrase.lower() not in result["response"].lower():
                failures.append(f"Response missing: '{phrase}'")
    if "min_length" in expect:
        if len(result["response"]) < expect["min_length"]:
            failures.append(f"Response too short: {len(result['response'])} < {expect['min_length']}")
    if "tool_calls_max" in expect:
        if len(result["tool_calls"]) > expect["tool_calls_max"]:
            failures.append(f"Too many tool calls: {len(result['tool_calls'])} > {expect['tool_calls_max']}")
    if "tool_calls_min" in expect:
        if len(result["tool_calls"]) < expect["tool_calls_min"]:
            failures.append(f"Too few tool calls: {len(result['tool_calls'])} < {expect['tool_calls_min']}")
    return failures


@pytest.mark.parametrize("scenario", catalog.scenarios, ids=[s["id"] for s in catalog.scenarios])
def test_scenario_golden(scenario):
    result = run_scenario(scenario["prompt"])
    golden_file = GOLDEN_DIR / f"{scenario['id']}.json"

    # Expectation checks
    if "expect" in scenario:
        failures = check_expectations(result, scenario["expect"])
        if failures:
            pytest.fail(f"Expectation failures:\n" + "\n".join(failures))

    # Golden comparison
    normalized = {"response": result["response"].strip(), "tool_count": len(result["tool_calls"])}
    if not golden_file.exists():
        golden_file.write_text(json.dumps(normalized, indent=2))
        pytest.skip(f"Golden file created: {golden_file}")

    stored = json.loads(golden_file.read_text())
    assert stored == normalized, (
        f"Golden mismatch for {scenario['id']}:\n"
        f"Expected: {stored}\nActual: {normalized}"
    )
```

## Solution 5: LLM-judged golden master with semantic equivalence threshold

Instead of exact string matching, use a judge model to assess whether the current output is semantically equivalent to the golden master. Tolerates rewording while catching behavioral regressions.

```python
import asyncio
import json
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

JUDGE_PROMPT = """Compare these two AI agent outputs for the same user prompt.

Prompt: {prompt}

Golden (approved) output:
{golden}

Current output:
{current}

Rate semantic equivalence on these dimensions (each 0.0–1.0):
1. factual_accuracy: Same facts, no hallucinations added or removed
2. tool_selection: Same tools called in the same order
3. response_completeness: All key points from golden are present
4. behavioral_consistency: Same decision-making pattern

Respond ONLY with JSON:
{{"factual_accuracy": 0.0, "tool_selection": 0.0, "response_completeness": 0.0, "behavioral_consistency": 0.0, "pass": true/false, "notes": "..."}}
Fail (pass=false) if any dimension < 0.8."""


async def judge_golden_equivalence(
    prompt: str, golden: dict, current: dict
) -> dict:
    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": JUDGE_PROMPT.format(
                prompt=prompt,
                golden=json.dumps(golden, indent=2)[:1000],
                current=json.dumps(current, indent=2)[:1000],
            ),
        }],
    )
    text = message.content[0].text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"pass": False, "notes": "Judge parse error"}


GOLDEN_DIR = Path("tests/golden/semantic")
GOLDEN_DIR.mkdir(parents=True, exist_ok=True)


async def assert_semantic_golden(scenario_id: str, prompt: str, current_output: dict):
    path = GOLDEN_DIR / f"{scenario_id}.json"

    if not path.exists():
        path.write_text(json.dumps({"prompt": prompt, "output": current_output}, indent=2))
        print(f"Semantic golden recorded: {path}")
        return

    stored = json.loads(path.read_text())
    golden_output = stored["output"]

    verdict = await judge_golden_equivalence(prompt, golden_output, current_output)
    if not verdict.get("pass", False):
        raise AssertionError(
            f"Semantic golden regression for '{scenario_id}':\n"
            + json.dumps(verdict, indent=2)
        )

    print(f"Semantic golden passed: {scenario_id} ({verdict.get('notes', '')})")


# ── Usage ────────────────────────────────────────────────────────────
async def main():
    current = {
        "response": "Tokyo is the capital city of Japan.",
        "tool_calls": [],
    }
    await assert_semantic_golden(
        "capital_of_japan",
        "What is the capital of Japan?",
        current,
    )


asyncio.run(main())
```

## Solution 6: Golden master CI gate with automated approval workflow

When a golden master test fails, generate a human-readable diff report and open a PR comment (or Slack alert) asking for explicit approval before the change is merged.

```python
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path("tests/golden")
REPORT_PATH = Path("golden_diff_report.md")


def generate_diff_report(failures: list[dict]) -> str:
    lines = [
        "# Golden Master Test Report",
        "",
        f"**{len(failures)} golden master test(s) failed.**",
        "",
        "Review each diff below. If the change is intentional, run:",
        "```bash",
        "UPDATE_GOLDEN=1 pytest tests/",
        "```",
        "then commit the updated `.golden` files.",
        "",
    ]
    for f in failures:
        lines += [
            f"## {f['test_name']}",
            "```diff",
            f['diff'],
            "```",
            "",
        ]
    return "\n".join(lines)


def run_golden_tests() -> list[dict]:
    """Run pytest and collect golden failures. Returns list of failure dicts."""
    result = subprocess.run(
        ["pytest", "tests/", "-v", "--tb=short", "-q", "--json-report"],
        capture_output=True, text=True
    )
    # In production: parse pytest JSON report for golden failures
    # Here: simplified demo
    return []


def post_github_comment(report: str):
    """Post report as a PR comment using GitHub CLI."""
    pr_number = os.environ.get("GITHUB_PR_NUMBER")
    if not pr_number:
        print("No PR number — skipping GitHub comment")
        return
    subprocess.run(
        ["gh", "pr", "comment", pr_number, "--body", report],
        check=False
    )


def ci_golden_gate():
    """CI entry point: run golden tests, generate report, fail build on regression."""
    failures = run_golden_tests()

    if not failures:
        print("All golden master tests passed.")
        sys.exit(0)

    report = generate_diff_report(failures)
    REPORT_PATH.write_text(report)
    print(report)

    post_github_comment(report)

    print(f"\n{len(failures)} golden master regressions. Review and approve changes.")
    sys.exit(1)


if __name__ == "__main__":
    ci_golden_gate()
```

## Comparison

| Approach | Detects behavior change | Tolerates rewording | CI-friendly | Human approval flow | Cost |
|---|---|---|---|---|---|
| File-based snapshot | Yes (exact) | No | Yes | Via UPDATE_GOLDEN | Zero |
| Structured field-level | Yes (per-field) | Partial | Yes | Via UPDATE_GOLDEN | Zero |
| Interaction sequence recorder | Yes (tool calls) | Yes | Yes | Via file diff | Zero |
| Parameterized scenario catalog | Yes | Partial | Yes | Via YAML + golden | Zero |
| LLM-judged semantic equivalence | Yes | Yes | Yes | No (auto-judge) | Low |
| CI gate with PR comment | Yes | Partial | Yes | PR approval | Zero |

**Recommendation**: Start with **file-based snapshot testing** (Solution 1) for the full response and **interaction sequence recording** (Solution 3) for the tool call trace. Add **LLM-judged equivalence** (Solution 5) for response text comparison to tolerate acceptable rewording. Wire up the **CI gate** (Solution 6) to block merges when golden masters fail without explicit approval.
