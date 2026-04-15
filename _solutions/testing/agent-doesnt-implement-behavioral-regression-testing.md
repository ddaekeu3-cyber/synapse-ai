---
layout: solution
title: "Agent Doesn't Implement Behavioral Regression Testing"
category: testing
description: "Agents that only test output quality miss behavioral regressions — changes in which tools the agent calls, in what order, and under what conditions. Behavioral regression testing captures agent decision patterns as baselines and alerts when a new model version or prompt change causes the agent to take materially different actions on the same inputs."
tags: [regression-testing, behavioral-testing, agent-testing, tool-calls, decision-patterns, ci-cd, evaluation]
---

# Agent Doesn't Implement Behavioral Regression Testing

## Problem

Output regression tests check *what* the agent says. Behavioral regression tests check *what the agent does* — which tools it calls, whether it asks clarifying questions, how many iterations it takes, and whether it escalates correctly. A new model version might produce equally good text but silently stop calling the `verify_result` tool, or start using `web_search` for questions it previously answered from memory. These behavioral shifts often indicate subtle alignment or capability regressions that text comparison misses entirely.

**Symptoms:**
- Prompt changes ship without knowing if agent behavior changed
- New model versions break agent workflows without failing text-quality checks
- Agent stops calling safety-check tools after a model upgrade
- Tool call order changes break downstream systems that depend on sequencing
- Agent takes 8 iterations instead of 3 for the same class of tasks after a change

---

## Option 1: Tool Call Sequence Capture and Comparison

```python
import anthropic
import json
import hashlib
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ToolCallTrace:
    turn: int
    tool_name: str
    tool_input: dict
    tool_result: str

@dataclass
class BehavioralBaseline:
    baseline_id: str
    test_case_id: str
    prompt: str
    tool_sequence: list[str]          # Ordered list of tool names called
    tool_call_count: int
    turns_to_complete: int
    model_used: str
    captured_at: float = field(default_factory=time.time)

@dataclass
class BehavioralDiff:
    test_case_id: str
    baseline_tools: list[str]
    current_tools: list[str]
    missing_tools: list[str]          # In baseline but not current
    new_tools: list[str]              # In current but not baseline
    order_changed: bool
    turn_count_delta: int
    passed: bool
    verdict: str

class BehavioralRegressionStore:
    def __init__(self, db_path: str = "/tmp/behavioral_baselines.db"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS baselines (
                baseline_id TEXT PRIMARY KEY,
                test_case_id TEXT NOT NULL,
                prompt TEXT,
                tool_sequence TEXT,
                tool_call_count INTEGER,
                turns_to_complete INTEGER,
                model_used TEXT,
                captured_at REAL
            )
        """)
        self.db.commit()

    def save(self, baseline: BehavioralBaseline):
        self.db.execute(
            "INSERT OR REPLACE INTO baselines VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (baseline.baseline_id, baseline.test_case_id, baseline.prompt,
             json.dumps(baseline.tool_sequence), baseline.tool_call_count,
             baseline.turns_to_complete, baseline.model_used, baseline.captured_at)
        )
        self.db.commit()

    def load(self, test_case_id: str) -> Optional[BehavioralBaseline]:
        row = self.db.execute(
            "SELECT * FROM baselines WHERE test_case_id = ? ORDER BY captured_at DESC LIMIT 1",
            (test_case_id,)
        ).fetchone()
        if not row:
            return None
        return BehavioralBaseline(
            baseline_id=row[0], test_case_id=row[1], prompt=row[2],
            tool_sequence=json.loads(row[3]), tool_call_count=row[4],
            turns_to_complete=row[5], model_used=row[6], captured_at=row[7]
        )

def run_agent_and_capture(
    client: anthropic.Anthropic,
    prompt: str,
    tools: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    max_turns: int = 8
) -> tuple[list[ToolCallTrace], int]:
    """Run the agent and record all tool calls made."""
    messages = [{"role": "user", "content": prompt}]
    traces: list[ToolCallTrace] = []
    turn = 0

    for turn in range(max_turns):
        response = client.messages.create(
            model=model, max_tokens=512, tools=tools, messages=messages
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                # Simulate tool execution
                result = f"Result for {block.name}({list(block.input.values())[:1]})"
                traces.append(ToolCallTrace(turn, block.name, block.input, result))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        if response.stop_reason == "end_turn" or not tool_results:
            break

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return traces, turn + 1

def compare_behavior(
    baseline: BehavioralBaseline,
    current_traces: list[ToolCallTrace],
    current_turns: int,
    turn_tolerance: int = 2
) -> BehavioralDiff:
    current_sequence = [t.tool_name for t in current_traces]
    baseline_set = set(baseline.tool_sequence)
    current_set = set(current_sequence)

    missing = list(baseline_set - current_set)
    new_tools = list(current_set - baseline_set)
    order_changed = baseline.tool_sequence != current_sequence and not missing and not new_tools
    turn_delta = current_turns - baseline.turns_to_complete

    passed = (
        len(missing) == 0 and          # No tools disappeared
        abs(turn_delta) <= turn_tolerance  # Turn count didn't change drastically
    )

    verdict = "PASS" if passed else "FAIL"
    if missing:
        verdict += f" — missing tools: {missing}"
    if new_tools:
        verdict += f" — new tools: {new_tools}"
    if order_changed:
        verdict += " — tool order changed"
    if abs(turn_delta) > turn_tolerance:
        verdict += f" — turns changed by {turn_delta:+d}"

    return BehavioralDiff(
        test_case_id=baseline.test_case_id,
        baseline_tools=baseline.tool_sequence,
        current_tools=current_sequence,
        missing_tools=missing,
        new_tools=new_tools,
        order_changed=order_changed,
        turn_count_delta=turn_delta,
        passed=passed,
        verdict=verdict
    )

def run_behavioral_regression_suite():
    client = anthropic.Anthropic()
    store = BehavioralRegressionStore()

    tools = [
        {"name": "search", "description": "Search for information",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
        {"name": "calculate", "description": "Perform a calculation",
         "input_schema": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}},
        {"name": "verify", "description": "Verify a result or claim",
         "input_schema": {"type": "object", "properties": {"claim": {"type": "string"}}, "required": ["claim"]}},
    ]

    test_cases = [
        {"id": "tc_math_01", "prompt": "What is 15% of 840? Verify your answer."},
        {"id": "tc_research_01", "prompt": "Search for the current Python version and summarize."},
    ]

    model = "claude-haiku-4-5-20251001"

    print("=== Behavioral Regression Test Suite ===\n")

    for tc in test_cases:
        baseline = store.load(tc["id"])

        traces, turns = run_agent_and_capture(client, tc["prompt"], tools, model)
        tool_sequence = [t.tool_name for t in traces]

        if baseline is None:
            # Capture baseline
            new_baseline = BehavioralBaseline(
                baseline_id=str(uuid.uuid4()),
                test_case_id=tc["id"],
                prompt=tc["prompt"],
                tool_sequence=tool_sequence,
                tool_call_count=len(traces),
                turns_to_complete=turns,
                model_used=model
            )
            store.save(new_baseline)
            print(f"[BASELINE] {tc['id']}: captured {len(traces)} tool calls: {tool_sequence}")
        else:
            diff = compare_behavior(baseline, traces, turns)
            status = "✓" if diff.passed else "✗"
            print(f"[{status}] {tc['id']}: {diff.verdict}")
            print(f"    Baseline: {diff.baseline_tools}")
            print(f"    Current:  {diff.current_tools}")

run_behavioral_regression_suite()
# Run again to trigger comparison:
run_behavioral_regression_suite()

# Expected Token Savings: ~0% on test runs; prevents shipping behavioral regressions silently
# Environment: CI/CD pipeline; run on every prompt change or model upgrade
```

---

## Option 2: Decision Pattern Fingerprinting

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class DecisionFingerprint:
    """A compact representation of agent decision patterns."""
    test_id: str
    model: str
    tools_used: list[str]          # Sorted unique tools
    tool_call_count: int
    first_tool: str                # What did agent call first?
    avg_input_tokens_per_call: float
    asked_clarification: bool      # Did agent ask a question?
    used_verification: bool        # Did agent verify before finishing?
    fingerprint_hash: str = ""
    captured_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.fingerprint_hash:
            content = json.dumps({
                "tools": sorted(self.tools_used),
                "first_tool": self.first_tool,
                "asked_clarification": self.asked_clarification,
                "used_verification": self.used_verification,
            }, sort_keys=True)
            self.fingerprint_hash = hashlib.sha256(content.encode()).hexdigest()[:12]

def extract_fingerprint(
    test_id: str,
    model: str,
    tool_traces: list[dict],
    response_text: str
) -> DecisionFingerprint:
    """Extract behavioral fingerprint from an agent run."""
    tools_used = [t["name"] for t in tool_traces]
    unique_tools = list(set(tools_used))
    first_tool = tools_used[0] if tools_used else ""
    avg_tokens = sum(t.get("input_tokens", 50) for t in tool_traces) / len(tool_traces) if tool_traces else 0.0
    asked_q = "?" in response_text and len(response_text.split("?")) > 2
    used_verify = any(t["name"] in ("verify", "check", "validate", "confirm") for t in tool_traces)

    return DecisionFingerprint(
        test_id=test_id, model=model,
        tools_used=unique_tools, tool_call_count=len(tool_traces),
        first_tool=first_tool, avg_input_tokens_per_call=avg_tokens,
        asked_clarification=asked_q, used_verification=used_verify
    )

class FingerprintRegistry:
    def __init__(self):
        self._baselines: dict[str, DecisionFingerprint] = {}
        self._history: dict[str, list[DecisionFingerprint]] = {}

    def register_baseline(self, fp: DecisionFingerprint):
        self._baselines[fp.test_id] = fp

    def check(self, fp: DecisionFingerprint) -> tuple[bool, list[str]]:
        """Compare fingerprint against baseline. Returns (passed, list_of_diffs)."""
        baseline = self._baselines.get(fp.test_id)
        if not baseline:
            self.register_baseline(fp)
            return True, ["No baseline — registered as new baseline"]

        diffs = []
        if fp.fingerprint_hash == baseline.fingerprint_hash:
            return True, []

        if set(fp.tools_used) != set(baseline.tools_used):
            missing = set(baseline.tools_used) - set(fp.tools_used)
            added = set(fp.tools_used) - set(baseline.tools_used)
            if missing:
                diffs.append(f"Missing tools: {missing}")
            if added:
                diffs.append(f"New tools: {added}")

        if fp.first_tool != baseline.first_tool:
            diffs.append(f"First tool changed: {baseline.first_tool!r} -> {fp.first_tool!r}")

        if fp.used_verification != baseline.used_verification:
            diffs.append(f"Verification changed: {baseline.used_verification} -> {fp.used_verification}")

        if fp.asked_clarification != baseline.asked_clarification:
            diffs.append(f"Clarification behavior changed: {baseline.asked_clarification} -> {fp.asked_clarification}")

        count_delta = fp.tool_call_count - baseline.tool_call_count
        if abs(count_delta) > 2:
            diffs.append(f"Tool call count changed: {baseline.tool_call_count} -> {fp.tool_call_count} ({count_delta:+d})")

        self._history.setdefault(fp.test_id, []).append(fp)
        return len(diffs) == 0, diffs

def simulate_agent_run(
    client: anthropic.Anthropic,
    prompt: str,
    model: str,
    tools: list[dict]
) -> tuple[list[dict], str]:
    messages = [{"role": "user", "content": prompt}]
    all_traces = []
    final_text = ""

    for _ in range(6):
        response = client.messages.create(
            model=model, max_tokens=512, tools=tools, messages=messages
        )
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                all_traces.append({
                    "name": block.name,
                    "input": block.input,
                    "input_tokens": len(json.dumps(block.input))
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Simulated result for {block.name}"
                })
            elif block.type == "text":
                final_text = block.text

        if response.stop_reason == "end_turn" or not tool_results:
            break
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return all_traces, final_text

def run_fingerprint_regression():
    client = anthropic.Anthropic()
    registry = FingerprintRegistry()
    model = "claude-haiku-4-5-20251001"

    tools = [
        {"name": "search", "description": "Search for information",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
        {"name": "verify", "description": "Verify a fact",
         "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}},
        {"name": "summarize", "description": "Summarize content",
         "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    ]

    test_cases = [
        ("tc_01", "Research the capital of France and verify it"),
        ("tc_02", "Find the population of Tokyo and summarize the key facts"),
    ]

    # Run 1: establish baselines
    print("=== Run 1: Establishing baselines ===")
    for test_id, prompt in test_cases:
        traces, text = simulate_agent_run(client, prompt, model, tools)
        fp = extract_fingerprint(test_id, model, traces, text)
        passed, diffs = registry.check(fp)
        print(f"  [{test_id}] hash={fp.fingerprint_hash}, tools={fp.tools_used}, "
              f"first={fp.first_tool!r}")
        print(f"    Status: {'BASELINE REGISTERED' if not diffs or diffs[0].startswith('No baseline') else 'PASS'}")

    # Run 2: check for regressions
    print("\n=== Run 2: Regression check ===")
    for test_id, prompt in test_cases:
        traces, text = simulate_agent_run(client, prompt, model, tools)
        fp = extract_fingerprint(test_id, model, traces, text)
        passed, diffs = registry.check(fp)
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  [{status}] {test_id}: hash={fp.fingerprint_hash}")
        for diff in diffs:
            print(f"    -> {diff}")

run_fingerprint_regression()

# Expected Token Savings: ~0% test overhead; catches behavioral shifts before they reach production
# Environment: CI/CD; run after every model upgrade, prompt change, or tool schema modification
```

---

## Option 3: Behavioral Test Suite with Assertion DSL

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class BehavioralAssertion:
    name: str
    check: Callable[[list[str], str, int], bool]
    failure_message: str

@dataclass
class BehavioralTestCase:
    test_id: str
    prompt: str
    assertions: list[BehavioralAssertion]
    tags: list[str] = field(default_factory=list)

@dataclass
class TestResult:
    test_id: str
    passed: bool
    failed_assertions: list[str]
    tool_sequence: list[str]
    turns: int
    final_text: str

# Assertion helpers
def must_call_tool(tool_name: str) -> BehavioralAssertion:
    return BehavioralAssertion(
        name=f"must_call_{tool_name}",
        check=lambda tools, text, turns: tool_name in tools,
        failure_message=f"Agent must call '{tool_name}' but didn't"
    )

def must_not_call_tool(tool_name: str) -> BehavioralAssertion:
    return BehavioralAssertion(
        name=f"must_not_call_{tool_name}",
        check=lambda tools, text, turns: tool_name not in tools,
        failure_message=f"Agent must NOT call '{tool_name}' but did"
    )

def must_call_in_order(first: str, second: str) -> BehavioralAssertion:
    def check(tools, text, turns):
        if first not in tools or second not in tools:
            return False
        return tools.index(first) < tools.index(second)
    return BehavioralAssertion(
        name=f"order_{first}_before_{second}",
        check=check,
        failure_message=f"Agent must call '{first}' before '{second}'"
    )

def max_tool_calls(n: int) -> BehavioralAssertion:
    return BehavioralAssertion(
        name=f"max_{n}_tool_calls",
        check=lambda tools, text, turns: len(tools) <= n,
        failure_message=f"Agent made {'{len(tools)}'} tool calls, max is {n}"
    )

def max_turns(n: int) -> BehavioralAssertion:
    return BehavioralAssertion(
        name=f"max_{n}_turns",
        check=lambda tools, text, turns: turns <= n,
        failure_message=f"Agent took too many turns (limit={n})"
    )

def response_contains(keyword: str) -> BehavioralAssertion:
    return BehavioralAssertion(
        name=f"response_contains_{keyword}",
        check=lambda tools, text, turns: keyword.lower() in text.lower(),
        failure_message=f"Response must contain '{keyword}'"
    )

def run_behavioral_test_suite(test_cases: list[BehavioralTestCase], model: str) -> list[TestResult]:
    client = anthropic.Anthropic()
    results = []

    tools = [
        {"name": "search", "description": "Search the web",
         "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        {"name": "calculate", "description": "Do math",
         "input_schema": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}},
        {"name": "verify", "description": "Verify a fact",
         "input_schema": {"type": "object", "properties": {"claim": {"type": "string"}}, "required": ["claim"]}},
        {"name": "format_output", "description": "Format final output",
         "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    ]

    for tc in test_cases:
        messages = [{"role": "user", "content": tc.prompt}]
        tool_sequence = []
        turns = 0
        final_text = ""

        for turn in range(10):
            turns = turn + 1
            response = client.messages.create(
                model=model, max_tokens=512, tools=tools, messages=messages
            )
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_sequence.append(block.name)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Simulated {block.name} result"
                    })
                elif block.type == "text":
                    final_text = block.text

            if response.stop_reason == "end_turn" or not tool_results:
                break
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        # Run assertions
        failed = []
        for assertion in tc.assertions:
            if not assertion.check(tool_sequence, final_text, turns):
                msg = assertion.failure_message
                if "{len(tools)}" in msg:
                    msg = msg.replace("{len(tools)}", str(len(tool_sequence)))
                failed.append(f"{assertion.name}: {msg}")

        results.append(TestResult(
            test_id=tc.test_id,
            passed=len(failed) == 0,
            failed_assertions=failed,
            tool_sequence=tool_sequence,
            turns=turns,
            final_text=final_text[:100]
        ))

    return results

def print_results(results: list[TestResult]):
    passed = sum(1 for r in results if r.passed)
    print(f"\n=== Behavioral Test Results: {passed}/{len(results)} passed ===\n")
    for r in results:
        status = "✓ PASS" if r.passed else "✗ FAIL"
        print(f"  [{status}] {r.test_id} (tools={r.tool_sequence}, turns={r.turns})")
        for fail in r.failed_assertions:
            print(f"    -> {fail}")

# Define test suite
test_cases = [
    BehavioralTestCase(
        test_id="math_verify_flow",
        prompt="Calculate 25% of 480, then verify the answer is correct.",
        assertions=[
            must_call_tool("calculate"),
            must_call_tool("verify"),
            must_call_in_order("calculate", "verify"),
            max_tool_calls(4),
            max_turns(4),
        ],
        tags=["math", "verification"]
    ),
    BehavioralTestCase(
        test_id="research_then_format",
        prompt="Search for information about Python and format a summary.",
        assertions=[
            must_call_tool("search"),
            must_call_tool("format_output"),
            must_call_in_order("search", "format_output"),
            must_not_call_tool("calculate"),
            max_turns(5),
        ],
        tags=["research", "formatting"]
    ),
    BehavioralTestCase(
        test_id="simple_question",
        prompt="What is 2 + 2?",
        assertions=[
            max_tool_calls(2),  # Should answer directly or with minimal tools
            max_turns(2),
        ],
        tags=["simple"]
    ),
]

results = run_behavioral_test_suite(test_cases, model="claude-haiku-4-5-20251001")
print_results(results)

# Expected Token Savings: ~0% overhead; assertion failures surface behavioral regressions before release
# Environment: CI/CD gates; run entire suite in parallel with pytest-asyncio for fast feedback
```

---

## Option 4: Differential Behavioral Testing — Compare Two Models

```python
import anthropic
import json
from dataclasses import dataclass, field

@dataclass
class ModelRunTrace:
    model: str
    tool_calls: list[dict]
    tool_sequence: list[str]
    turns: int
    response_length: int
    final_text: str

@dataclass
class BehavioralDiff:
    prompt: str
    model_a: str
    model_b: str
    trace_a: ModelRunTrace
    trace_b: ModelRunTrace
    differences: list[str]
    severity: str  # "none" | "minor" | "major" | "critical"

def run_model_trace(
    client: anthropic.Anthropic,
    prompt: str,
    tools: list[dict],
    model: str
) -> ModelRunTrace:
    messages = [{"role": "user", "content": prompt}]
    tool_calls = []
    tool_sequence = []
    turns = 0
    final_text = ""

    for turn in range(8):
        turns = turn + 1
        response = client.messages.create(
            model=model, max_tokens=512, tools=tools, messages=messages
        )
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append({"name": block.name, "input": block.input})
                tool_sequence.append(block.name)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Result for {block.name}"
                })
            elif block.type == "text":
                final_text = block.text

        if response.stop_reason == "end_turn" or not tool_results:
            break
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return ModelRunTrace(
        model=model,
        tool_calls=tool_calls,
        tool_sequence=tool_sequence,
        turns=turns,
        response_length=len(final_text.split()),
        final_text=final_text
    )

def diff_traces(
    prompt: str,
    trace_a: ModelRunTrace,
    trace_b: ModelRunTrace
) -> BehavioralDiff:
    diffs = []
    severity = "none"

    tools_a = set(trace_a.tool_sequence)
    tools_b = set(trace_b.tool_sequence)
    missing_in_b = tools_a - tools_b
    new_in_b = tools_b - tools_a

    if missing_in_b:
        diffs.append(f"Model B dropped tools: {missing_in_b}")
        severity = "major"
    if new_in_b:
        diffs.append(f"Model B added new tools: {new_in_b}")
        severity = max(severity, "minor", key=lambda s: ["none", "minor", "major", "critical"].index(s))

    if trace_a.tool_sequence and trace_b.tool_sequence:
        first_a = trace_a.tool_sequence[0]
        first_b = trace_b.tool_sequence[0]
        if first_a != first_b:
            diffs.append(f"First tool changed: {first_a!r} -> {first_b!r}")
            severity = max(severity, "minor", key=lambda s: ["none", "minor", "major", "critical"].index(s))

    turn_delta = trace_b.turns - trace_a.turns
    if abs(turn_delta) > 2:
        diffs.append(f"Turn count delta: {turn_delta:+d} ({trace_a.turns} -> {trace_b.turns})")
        severity = max(severity, "minor", key=lambda s: ["none", "minor", "major", "critical"].index(s))

    call_delta = len(trace_b.tool_sequence) - len(trace_a.tool_sequence)
    if abs(call_delta) > 3:
        diffs.append(f"Tool call count delta: {call_delta:+d}")
        severity = max(severity, "minor", key=lambda s: ["none", "minor", "major", "critical"].index(s))

    return BehavioralDiff(
        prompt=prompt, model_a=trace_a.model, model_b=trace_b.model,
        trace_a=trace_a, trace_b=trace_b,
        differences=diffs, severity=severity
    )

def run_differential_test(prompts: list[str], model_a: str, model_b: str):
    client = anthropic.Anthropic()
    tools = [
        {"name": "search", "description": "Search the web",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
        {"name": "analyze", "description": "Analyze data",
         "input_schema": {"type": "object", "properties": {"data": {"type": "string"}}, "required": ["data"]}},
        {"name": "verify", "description": "Verify a fact",
         "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}},
    ]

    print(f"Differential behavioral test: {model_a} vs {model_b}\n")
    results = []

    for prompt in prompts:
        print(f"Prompt: {prompt!r[:60]}")
        trace_a = run_model_trace(client, prompt, tools, model_a)
        trace_b = run_model_trace(client, prompt, tools, model_b)
        diff = diff_traces(prompt, trace_a, trace_b)
        results.append(diff)

        print(f"  {model_a}: {trace_a.tool_sequence} ({trace_a.turns} turns)")
        print(f"  {model_b}: {trace_b.tool_sequence} ({trace_b.turns} turns)")
        print(f"  Severity: {diff.severity.upper()}")
        for d in diff.differences:
            print(f"    -> {d}")
        print()

    # Summary
    by_severity = {}
    for r in results:
        by_severity.setdefault(r.severity, []).append(r.prompt[:40])

    print("=== Summary ===")
    for severity in ["critical", "major", "minor", "none"]:
        if severity in by_severity:
            print(f"  {severity.upper()}: {len(by_severity[severity])} prompts")

# Use same model for demo (in production: compare claude-haiku-4-5 vs claude-sonnet-4-6)
prompts = [
    "Research the latest AI developments and verify 3 key facts",
    "What is 18% of 520? Show your work and verify.",
    "Find information about Python and analyze its growth trend",
]
run_differential_test(prompts, "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001")

# Expected Token Savings: 0% (2x calls) — catches behavioral divergence between model versions
# Environment: Pre-deployment gate when upgrading models; run on representative sample of production prompts
```

---

## Option 5: Continuous Behavioral Monitoring in Production

```python
import anthropic
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class ProductionTrace:
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    prompt_hash: str = ""
    tool_sequence: list[str] = field(default_factory=list)
    turns: int = 0
    latency_ms: float = 0.0
    model: str = ""
    timestamp: float = field(default_factory=time.time)

class ProductionBehaviorMonitor:
    def __init__(self, db_path: str = "/tmp/prod_behavior.db", window_hours: int = 24):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.window = window_hours * 3600
        self._setup()

    def _setup(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY,
                session_id TEXT,
                prompt_hash TEXT,
                tool_sequence TEXT,
                turns INTEGER,
                latency_ms REAL,
                model TEXT,
                timestamp REAL
            )
        """)
        self.db.commit()

    def record(self, trace: ProductionTrace):
        self.db.execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (trace.trace_id, trace.session_id, trace.prompt_hash,
             json.dumps(trace.tool_sequence), trace.turns,
             trace.latency_ms, trace.model, trace.timestamp)
        )
        self.db.commit()

    def behavioral_stats(self, model: str, hours: int = 1) -> dict:
        since = time.time() - hours * 3600
        rows = self.db.execute("""
            SELECT tool_sequence, turns, latency_ms
            FROM traces
            WHERE model = ? AND timestamp > ?
        """, (model, since)).fetchall()

        if not rows:
            return {"error": "No data"}

        all_sequences = [json.loads(r[0]) for r in rows]
        all_turns = [r[1] for r in rows]
        all_latencies = [r[2] for r in rows]

        tool_freq = defaultdict(int)
        for seq in all_sequences:
            for tool in seq:
                tool_freq[tool] += 1

        total = len(rows)
        return {
            "sample_count": total,
            "avg_turns": round(sum(all_turns) / total, 2),
            "avg_latency_ms": round(sum(all_latencies) / total, 1),
            "tool_usage_rate": {tool: count / total for tool, count in tool_freq.items()},
            "avg_tool_calls": round(sum(len(s) for s in all_sequences) / total, 2),
        }

    def detect_drift(self, model: str, baseline_hours: int = 24, current_hours: int = 1) -> list[str]:
        """Compare recent behavior to historical baseline."""
        baseline = self.behavioral_stats(model, baseline_hours)
        current = self.behavioral_stats(model, current_hours)
        if "error" in baseline or "error" in current:
            return []

        alerts = []
        turn_delta = current["avg_turns"] - baseline["avg_turns"]
        if abs(turn_delta) > 1.5:
            alerts.append(f"Avg turns drifted: {baseline['avg_turns']} -> {current['avg_turns']} ({turn_delta:+.1f})")

        for tool, base_rate in baseline["tool_usage_rate"].items():
            curr_rate = current["tool_usage_rate"].get(tool, 0.0)
            if base_rate > 0.1 and abs(curr_rate - base_rate) > 0.2:
                alerts.append(f"Tool '{tool}' usage rate changed: {base_rate:.0%} -> {curr_rate:.0%}")

        return alerts

def run_production_monitor_demo():
    client = anthropic.Anthropic()
    monitor = ProductionBehaviorMonitor()
    model = "claude-haiku-4-5-20251001"

    tools = [
        {"name": "search", "description": "Search",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
        {"name": "verify", "description": "Verify",
         "input_schema": {"type": "object", "properties": {"f": {"type": "string"}}, "required": ["f"]}},
    ]

    prompts = [
        "What is machine learning?",
        "How does Python handle memory?",
        "What is REST API?",
    ]

    print("Simulating production traffic with behavioral monitoring:\n")
    for i, prompt in enumerate(prompts * 3):
        import hashlib
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
        start = time.time()
        messages = [{"role": "user", "content": prompt}]
        tool_seq = []
        turns = 0

        for turn in range(5):
            turns = turn + 1
            response = client.messages.create(
                model=model, max_tokens=256, tools=tools, messages=messages
            )
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_seq.append(block.name)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": "Result"})
            if response.stop_reason == "end_turn" or not results:
                break
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": results})

        trace = ProductionTrace(
            session_id=f"sess_{i}",
            prompt_hash=prompt_hash,
            tool_sequence=tool_seq,
            turns=turns,
            latency_ms=(time.time() - start) * 1000,
            model=model
        )
        monitor.record(trace)
        print(f"  [{i+1}] {prompt!r[:40]}: tools={tool_seq}, turns={turns}")

    stats = monitor.behavioral_stats(model, hours=24)
    print(f"\nBehavioral stats (last 24h):")
    print(f"  Samples: {stats['sample_count']}")
    print(f"  Avg turns: {stats['avg_turns']}")
    print(f"  Avg tool calls: {stats['avg_tool_calls']}")
    print(f"  Tool rates: {stats['tool_usage_rate']}")

run_production_monitor_demo()

# Expected Token Savings: ~0% monitoring overhead; detects behavioral drift before users notice
# Environment: Production sidecar; ship to Prometheus/Grafana for visualization and alerting
```

---

## Option 6: Regression Gate — Block Deployment on Behavioral Failure

```python
import anthropic
import json
import sys
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class RegressionGateConfig:
    max_turn_increase_pct: float = 0.25       # Allow up to 25% more turns
    max_new_tools_count: int = 1              # Allow at most 1 genuinely new tool
    required_tools_tolerance: float = 0.0    # Zero tolerance for missing required tools
    min_pass_rate: float = 0.90              # At least 90% of test cases must pass

@dataclass
class GateResult:
    passed: bool
    total_tests: int
    pass_count: int
    fail_count: int
    gate_failures: list[str]
    exit_code: int  # 0 = pass, 1 = fail

def evaluate_gate(
    client: anthropic.Anthropic,
    test_cases: list[dict],
    baseline_traces: list[dict],
    config: RegressionGateConfig,
    model: str
) -> GateResult:
    tools = [
        {"name": "search", "description": "Search",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
        {"name": "analyze", "description": "Analyze",
         "input_schema": {"type": "object", "properties": {"d": {"type": "string"}}, "required": ["d"]}},
        {"name": "verify", "description": "Verify",
         "input_schema": {"type": "object", "properties": {"f": {"type": "string"}}, "required": ["f"]}},
    ]

    pass_count = 0
    gate_failures = []

    for tc, baseline in zip(test_cases, baseline_traces):
        messages = [{"role": "user", "content": tc["prompt"]}]
        current_seq = []
        turns = 0

        for turn in range(8):
            turns = turn + 1
            response = client.messages.create(
                model=model, max_tokens=384, tools=tools, messages=messages
            )
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    current_seq.append(block.name)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": "ok"})
            if response.stop_reason == "end_turn" or not results:
                break
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": results})

        base_seq = baseline["tool_sequence"]
        base_turns = baseline["turns"]
        tc_passed = True

        # Check required tools (zero tolerance)
        required = baseline.get("required_tools", list(set(base_seq)))
        missing = [t for t in required if t not in current_seq]
        if missing:
            gate_failures.append(f"[{tc['id']}] Missing required tools: {missing}")
            tc_passed = False

        # Check turn regression
        allowed_turns = base_turns * (1 + config.max_turn_increase_pct)
        if turns > allowed_turns:
            gate_failures.append(f"[{tc['id']}] Turns {turns} > allowed {allowed_turns:.0f}")
            tc_passed = False

        # Check new tools
        new_tools = set(current_seq) - set(base_seq)
        if len(new_tools) > config.max_new_tools_count:
            gate_failures.append(f"[{tc['id']}] Too many new tools: {new_tools}")
            tc_passed = False

        if tc_passed:
            pass_count += 1
        print(f"  [{'PASS' if tc_passed else 'FAIL'}] {tc['id']}: {current_seq} ({turns} turns)")

    total = len(test_cases)
    pass_rate = pass_count / total
    gate_passed = pass_rate >= config.min_pass_rate and len(gate_failures) == 0

    return GateResult(
        passed=gate_passed,
        total_tests=total,
        pass_count=pass_count,
        fail_count=total - pass_count,
        gate_failures=gate_failures,
        exit_code=0 if gate_passed else 1
    )

def run_deployment_gate():
    client = anthropic.Anthropic()
    model = "claude-haiku-4-5-20251001"
    config = RegressionGateConfig(max_turn_increase_pct=0.3, min_pass_rate=0.80)

    # Baselines captured from previous passing run
    test_cases = [
        {"id": "tc_01", "prompt": "Search for Python news and verify one fact"},
        {"id": "tc_02", "prompt": "What is asyncio? Search if needed."},
        {"id": "tc_03", "prompt": "Analyze the impact of AI on software development"},
    ]
    baseline_traces = [
        {"tool_sequence": ["search", "verify"], "turns": 2, "required_tools": ["search"]},
        {"tool_sequence": ["search"], "turns": 2, "required_tools": []},
        {"tool_sequence": ["analyze"], "turns": 2, "required_tools": []},
    ]

    print(f"=== Deployment Gate: {model} ===\n")
    result = evaluate_gate(client, test_cases, baseline_traces, config, model)

    print(f"\n{'=' * 50}")
    print(f"Gate result: {'PASS ✓' if result.passed else 'FAIL ✗'}")
    print(f"Tests: {result.pass_count}/{result.total_tests} passed ({result.pass_count/result.total_tests:.0%})")
    if result.gate_failures:
        print(f"Failures:")
        for f in result.gate_failures:
            print(f"  -> {f}")
    print(f"Exit code: {result.exit_code}")
    # In CI/CD: sys.exit(result.exit_code)

run_deployment_gate()

# Expected Token Savings: ~0% on test runs; exit code 1 blocks bad deployments automatically
# Environment: GitHub Actions / GitLab CI gate step; run before canary or full rollout
```

---

## Comparison

| Option | What It Tests | Baseline Storage | CI Integration | Cross-Model | Best For |
|--------|--------------|-----------------|---------------|-------------|----------|
| Tool Sequence Capture | Tool call order | SQLite | Yes | No | Detecting missing/reordered tools |
| Decision Fingerprinting | Compact behavioral hash | In-memory | Yes | No | Fast regression check with minimal storage |
| Assertion DSL | Named behavioral rules | None (inline) | Yes | No | Explicit behavioral contracts per test case |
| Differential Testing | Model A vs Model B | None | Yes | Yes | Comparing behavior between model versions |
| Production Monitoring | Live traffic patterns | SQLite | No | No | Detecting drift after deployment |
| Deployment Gate | Pass/fail with exit code | JSON/SQLite | Yes | No | Blocking bad releases in CI/CD |

**Recommendation:** Use **Option 3** (Assertion DSL) to define explicit behavioral contracts for your agent's key workflows. Run **Option 1** (tool sequence capture) to establish baselines after each release. Add **Option 6** (deployment gate) to your CI pipeline to block releases that fail behavioral regression. Use **Option 4** (differential) when evaluating a new model version before upgrading.
