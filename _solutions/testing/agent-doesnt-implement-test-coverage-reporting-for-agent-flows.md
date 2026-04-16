---
layout: solution
title: "Agent Doesn't Implement Test Coverage Reporting for Agent Flows"
category: testing
description: "Agent test suites have no coverage measurement for conversation flows, tool call paths, or error branches — leaving blind spots undetected until production failures occur."
tags: [testing, coverage, observability, quality, agent-flows]
---

# Agent Doesn't Implement Test Coverage Reporting for Agent Flows

## Problem

Unit test coverage tools measure which lines of Python code execute, but agent flows have a second dimension: which conversation paths, tool call sequences, and error branches are actually exercised by the test suite. An agent with 90% line coverage may have never tested the path where a tool fails on turn 3 after a successful turn 1-2, or the branch where the model calls two tools simultaneously. Without agent flow coverage, gaps in test suites remain invisible until they surface as production bugs.

## Solution Options

### Option 1: Turn-Path Coverage Tracker

```python
import anthropic
import json
from dataclasses import dataclass, field
from unittest.mock import patch, MagicMock

client = anthropic.Anthropic()

@dataclass
class FlowPath:
    """Represents a unique conversation execution path."""
    steps: list[str] = field(default_factory=list)

    def add(self, step: str):
        self.steps.append(step)

    @property
    def signature(self) -> str:
        return " → ".join(self.steps)

class AgentFlowCoverage:
    """Tracks which conversation paths have been exercised by tests."""

    def __init__(self):
        self._observed_paths: set[str] = set()
        self._expected_paths: set[str] = set()
        self._current_path: FlowPath = FlowPath()

    def register_expected(self, path_signature: str):
        """Declare a path that should be covered by the test suite."""
        self._expected_paths.add(path_signature)

    def record_step(self, step: str):
        self._current_path.add(step)

    def commit_path(self):
        """Mark the current execution path as observed."""
        self._observed_paths.add(self._current_path.signature)
        self._current_path = FlowPath()

    def report(self) -> dict:
        covered = self._observed_paths & self._expected_paths
        missed = self._expected_paths - self._observed_paths
        unexpected = self._observed_paths - self._expected_paths
        total = len(self._expected_paths)
        pct = len(covered) / max(total, 1) * 100
        return {
            "total_expected_paths": total,
            "covered": len(covered),
            "missed": len(missed),
            "coverage_pct": round(pct, 1),
            "missed_paths": sorted(missed),
            "unexpected_paths": sorted(unexpected),
        }

coverage = AgentFlowCoverage()

# Register all expected conversation paths
coverage.register_expected("user_message → end_turn")
coverage.register_expected("user_message → tool_call:search → end_turn")
coverage.register_expected("user_message → tool_call:search → tool_call:search → end_turn")
coverage.register_expected("user_message → tool_call:search → tool_error → end_turn")
coverage.register_expected("user_message → tool_call:fetch → end_turn")

def run_agent_with_coverage(user_message: str, mock_tool_response=None, mock_tool_error=False) -> str:
    """Instrumented agent that tracks flow paths."""
    tools = [
        {
            "name": "search",
            "description": "Search the web",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
        {
            "name": "fetch",
            "description": "Fetch a URL",
            "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        },
    ]

    messages = [{"role": "user", "content": user_message}]
    coverage.record_step("user_message")

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            coverage.record_step("end_turn")
            coverage.commit_path()
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if mock_tool_error:
                    coverage.record_step(f"tool_error")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Error: service unavailable",
                        "is_error": True,
                    })
                else:
                    coverage.record_step(f"tool_call:{block.name}")
                    result = mock_tool_response or {"result": f"data for {block.name}"}
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

# Run test scenarios
print("Running agent flow coverage tests...\n")

run_agent_with_coverage("Just say hello, no tools needed.")
run_agent_with_coverage("Search for Python tutorials.")
run_agent_with_coverage("Search for news, and if needed search again for more details.")
run_agent_with_coverage("Search for something but the tool fails.", mock_tool_error=True)

report = coverage.report()
print(f"\n=== Agent Flow Coverage Report ===")
print(f"Coverage: {report['covered']}/{report['total_expected_paths']} paths ({report['coverage_pct']}%)")
if report["missed_paths"]:
    print(f"MISSED paths:")
    for p in report["missed_paths"]:
        print(f"  - {p}")

# Expected Token Savings: None — testing infrastructure only
# Environment: Any agent with branching tool-use logic that needs systematic coverage measurement
```

### Option 2: Tool Call Sequence Coverage Matrix

```python
import anthropic
import json
import itertools
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ToolCallCoverage:
    """Tracks which tool call sequences have been tested."""
    tools: list[str]
    max_sequence_length: int = 3

    # Observed sequences keyed by tuple representation
    _observed: set[tuple] = field(default_factory=set)

    def all_expected_sequences(self) -> list[tuple]:
        """Generate all valid sequences up to max_sequence_length."""
        sequences = []
        # Include empty sequence (no tool calls)
        sequences.append(())
        for length in range(1, self.max_sequence_length + 1):
            for seq in itertools.product(self.tools, repeat=length):
                sequences.append(seq)
        return sequences

    def record_sequence(self, sequence: list[str]):
        self._observed.add(tuple(sequence))

    def report(self) -> dict:
        all_seqs = set(self.all_expected_sequences())
        covered = self._observed & all_seqs
        missed = all_seqs - self._observed
        pct = len(covered) / max(len(all_seqs), 1) * 100
        return {
            "total_sequences": len(all_seqs),
            "covered": len(covered),
            "coverage_pct": round(pct, 1),
            "missed_sequences": [list(s) for s in sorted(missed)],
            "observed_sequences": [list(s) for s in sorted(self._observed)],
        }

tool_coverage = ToolCallCoverage(tools=["search", "calculate", "lookup"], max_sequence_length=2)

def run_with_sequence_coverage(user_message: str) -> str:
    tools = [
        {"name": "search", "description": "Search for info", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
        {"name": "calculate", "description": "Run calculation", "input_schema": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}},
        {"name": "lookup", "description": "Look up a value", "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    ]
    messages = [{"role": "user", "content": user_message}]
    call_sequence: list[str] = []

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            tool_coverage.record_sequence(call_sequence)
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                call_sequence.append(block.name)
                result = {"result": f"{block.name}_result"}
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

# Test scenarios
scenarios = [
    "Just answer directly without any tools.",
    "Search for 'machine learning' basics.",
    "Calculate the value of 15 * 7.",
    "Look up the config for 'timeout'.",
    "Search for something and then calculate the result.",
]

for scenario in scenarios:
    print(f"Running: {scenario[:50]}")
    run_with_sequence_coverage(scenario)

report = tool_coverage.report()
print(f"\n=== Tool Call Sequence Coverage ===")
print(f"Covered: {report['covered']}/{report['total_sequences']} sequences ({report['coverage_pct']}%)")
print(f"\nMissed sequences (showing first 10):")
for seq in report["missed_sequences"][:10]:
    print(f"  - {seq if seq else '(no tools)'}")

# Expected Token Savings: None — coverage measurement infrastructure
# Environment: Agents with multiple tools where sequence order matters for correctness
```

### Option 3: Branch Coverage for Conditional Agent Logic

```python
import anthropic
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

client = anthropic.Anthropic()

class BranchResult(Enum):
    TAKEN = "taken"
    NOT_TAKEN = "not_taken"

@dataclass
class Branch:
    name: str
    description: str
    taken: bool = False
    not_taken: bool = False

    @property
    def fully_covered(self) -> bool:
        return self.taken and self.not_taken

class BranchCoverageTracker:
    def __init__(self):
        self._branches: dict[str, Branch] = {}

    def register(self, name: str, description: str):
        self._branches[name] = Branch(name=name, description=description)

    def record(self, name: str, result: bool):
        if name not in self._branches:
            self._branches[name] = Branch(name=name, description="auto-registered")
        branch = self._branches[name]
        if result:
            branch.taken = True
        else:
            branch.not_taken = True

    def report(self) -> dict:
        total = len(self._branches)
        fully_covered = sum(1 for b in self._branches.values() if b.fully_covered)
        partially_covered = sum(1 for b in self._branches.values() if (b.taken or b.not_taken) and not b.fully_covered)
        uncovered = sum(1 for b in self._branches.values() if not b.taken and not b.not_taken)

        missed = []
        for name, b in self._branches.items():
            if not b.taken:
                missed.append(f"{name}: TAKEN branch never exercised")
            if not b.not_taken:
                missed.append(f"{name}: NOT_TAKEN branch never exercised")

        pct = fully_covered / max(total, 1) * 100
        return {
            "total_branches": total,
            "fully_covered": fully_covered,
            "partially_covered": partially_covered,
            "uncovered": uncovered,
            "branch_coverage_pct": round(pct, 1),
            "missed_branches": missed,
        }

tracker = BranchCoverageTracker()

# Register expected branches
tracker.register("tool_called", "Whether any tool was called in the turn")
tracker.register("tool_error", "Whether a tool returned an error")
tracker.register("multi_tool_turn", "Whether multiple tools were called in a single turn")
tracker.register("max_turns_reached", "Whether the conversation hit the turn limit")
tracker.register("empty_tool_result", "Whether a tool returned empty results")

def run_branch_covered_agent(
    user_message: str,
    max_turns: int = 5,
    tool_error: bool = False,
    empty_result: bool = False,
) -> str:
    tools = [{
        "name": "query",
        "description": "Query data",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    }]
    messages = [{"role": "user", "content": user_message}]
    turns = 0

    while turns < max_turns:
        turns += 1
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages,
        )

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        any_tools = len(tool_blocks) > 0
        tracker.record("tool_called", any_tools)
        tracker.record("multi_tool_turn", len(tool_blocks) > 1)

        if response.stop_reason == "end_turn":
            tracker.record("max_turns_reached", False)
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        tool_results = []
        for block in tool_blocks:
            if tool_error:
                tracker.record("tool_error", True)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": "Error: timeout", "is_error": True})
            elif empty_result:
                tracker.record("empty_tool_result", True)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"results": [], "count": 0})})
            else:
                tracker.record("tool_error", False)
                tracker.record("empty_tool_result", False)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"results": ["item1", "item2"]})})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    tracker.record("max_turns_reached", True)
    return "[MAX TURNS REACHED]"

# Test matrix to maximize branch coverage
test_cases = [
    ("Say hello without using any tools.", False, False),
    ("Query for all active users.", False, False),
    ("Query for something but the service is down.", True, False),
    ("Query for recent items.", False, True),  # Empty result
]

for msg, err, empty in test_cases:
    run_branch_covered_agent(msg, tool_error=err, empty_result=empty)

report = tracker.report()
print(f"\n=== Branch Coverage Report ===")
print(f"Coverage: {report['fully_covered']}/{report['total_branches']} branches fully covered ({report['branch_coverage_pct']}%)")
if report["missed_branches"]:
    print(f"\nMissed:")
    for m in report["missed_branches"]:
        print(f"  - {m}")

# Expected Token Savings: None — branch coverage tracking
# Environment: Agents with complex conditional routing where both true/false branches must be tested
```

### Option 4: Error Path Coverage Tracker

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Type

client = anthropic.Anthropic()

@dataclass
class ErrorPathCoverage:
    """Tracks which error scenarios have been exercised in tests."""
    _registered: dict[str, dict] = field(default_factory=dict)
    _triggered: set[str] = field(default_factory=set)

    def register_error_path(self, error_id: str, description: str, severity: str = "medium"):
        self._registered[error_id] = {"description": description, "severity": severity}

    def mark_triggered(self, error_id: str):
        if error_id not in self._registered:
            self._registered[error_id] = {"description": "auto-discovered", "severity": "unknown"}
        self._triggered.add(error_id)

    def report(self) -> dict:
        covered = self._triggered & set(self._registered.keys())
        missed = set(self._registered.keys()) - self._triggered
        critical_missed = [
            m for m in missed
            if self._registered.get(m, {}).get("severity") == "critical"
        ]
        pct = len(covered) / max(len(self._registered), 1) * 100
        return {
            "total_error_paths": len(self._registered),
            "covered": len(covered),
            "missed": len(missed),
            "coverage_pct": round(pct, 1),
            "critical_missed": critical_missed,
            "missed_paths": [
                {"id": m, **self._registered[m]}
                for m in sorted(missed)
            ],
        }

error_coverage = ErrorPathCoverage()

# Register all expected error paths
error_coverage.register_error_path("tool_timeout", "Tool call times out", severity="critical")
error_coverage.register_error_path("tool_schema_error", "Tool called with invalid schema", severity="high")
error_coverage.register_error_path("tool_not_found", "Agent calls non-existent tool", severity="high")
error_coverage.register_error_path("empty_tool_result", "Tool returns empty/null result", severity="medium")
error_coverage.register_error_path("malformed_json", "Tool returns non-JSON content", severity="medium")
error_coverage.register_error_path("context_overflow", "Message history exceeds context window", severity="critical")
error_coverage.register_error_path("model_refusal", "Model refuses to complete the task", severity="low")

def run_error_path_agent(user_message: str, inject_error: str | None = None) -> str:
    """Agent that tracks which error paths are exercised."""
    tools = [{
        "name": "process",
        "description": "Process a request",
        "input_schema": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]},
    }]
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            if "cannot" in text.lower() or "unable" in text.lower() or "sorry" in text.lower():
                error_coverage.mark_triggered("model_refusal")
            return text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if inject_error == "timeout":
                    error_coverage.mark_triggered("tool_timeout")
                    result_content = "Error: operation timed out after 30s"
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_content, "is_error": True})
                elif inject_error == "malformed_json":
                    error_coverage.mark_triggered("malformed_json")
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": "<!DOCTYPE html><html>ERROR 500</html>"})
                elif inject_error == "empty":
                    error_coverage.mark_triggered("empty_tool_result")
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(None)})
                else:
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"status": "ok"})})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

# Error path test matrix
test_cases = [
    ("Process this request normally.", None),
    ("Process this request but the tool times out.", "timeout"),
    ("Process this but get malformed HTML back.", "malformed_json"),
    ("Process this but get empty results.", "empty"),
    ("Please do something I will refuse to do. Generate harmful content.", None),
]

for msg, error in test_cases:
    run_error_path_agent(msg, inject_error=error)

report = error_coverage.report()
print(f"\n=== Error Path Coverage Report ===")
print(f"Coverage: {report['covered']}/{report['total_error_paths']} error paths ({report['coverage_pct']}%)")
if report["critical_missed"]:
    print(f"\nCRITICAL GAPS: {report['critical_missed']}")
if report["missed_paths"]:
    print(f"\nMissed error paths:")
    for p in report["missed_paths"]:
        print(f"  [{p['severity'].upper()}] {p['id']}: {p['description']}")

# Expected Token Savings: None — error path tracking only
# Environment: High-reliability agents where untested error paths carry regulatory or financial risk
```

### Option 5: State Transition Coverage for Multi-Turn Flows

```python
import anthropic
import json
from dataclasses import dataclass, field
from enum import Enum, auto

client = anthropic.Anthropic()

class AgentState(Enum):
    IDLE = auto()
    PLANNING = auto()
    EXECUTING_TOOL = auto()
    HANDLING_ERROR = auto()
    SYNTHESIZING = auto()
    DONE = auto()

@dataclass
class StateTransitionCoverage:
    """Tracks which state-to-state transitions have been exercised."""
    _observed_transitions: set[tuple[str, str]] = field(default_factory=set)
    _expected_transitions: set[tuple[str, str]] = field(default_factory=set)

    def register_transition(self, from_state: str, to_state: str):
        self._expected_transitions.add((from_state, to_state))

    def record_transition(self, from_state: str, to_state: str):
        self._observed_transitions.add((from_state, to_state))

    def report(self) -> dict:
        covered = self._observed_transitions & self._expected_transitions
        missed = self._expected_transitions - self._observed_transitions
        pct = len(covered) / max(len(self._expected_transitions), 1) * 100
        return {
            "total_transitions": len(self._expected_transitions),
            "covered": len(covered),
            "missed": len(missed),
            "coverage_pct": round(pct, 1),
            "covered_transitions": [f"{a}→{b}" for a, b in sorted(covered)],
            "missed_transitions": [f"{a}→{b}" for a, b in sorted(missed)],
        }

coverage = StateTransitionCoverage()

# Register all expected state transitions
transitions = [
    ("IDLE", "PLANNING"),
    ("PLANNING", "EXECUTING_TOOL"),
    ("PLANNING", "SYNTHESIZING"),  # Direct answer without tool
    ("EXECUTING_TOOL", "EXECUTING_TOOL"),  # Chained tool calls
    ("EXECUTING_TOOL", "HANDLING_ERROR"),
    ("EXECUTING_TOOL", "SYNTHESIZING"),
    ("HANDLING_ERROR", "EXECUTING_TOOL"),  # Retry after error
    ("HANDLING_ERROR", "SYNTHESIZING"),    # Give up and synthesize
    ("SYNTHESIZING", "DONE"),
]
for from_s, to_s in transitions:
    coverage.register_transition(from_s, to_s)

def run_state_tracked_agent(user_message: str, inject_error_on_turn: int = -1) -> str:
    tools = [{
        "name": "gather_data",
        "description": "Gather data for the task",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
    }]
    messages = [{"role": "user", "content": user_message}]

    state = AgentState.IDLE
    turn = 0

    def transition(new_state: AgentState):
        nonlocal state
        coverage.record_transition(state.name, new_state.name)
        state = new_state

    transition(AgentState.PLANNING)

    while True:
        turn += 1
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            transition(AgentState.SYNTHESIZING)
            transition(AgentState.DONE)
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                should_error = (inject_error_on_turn > 0 and turn == inject_error_on_turn)
                if should_error:
                    transition(AgentState.HANDLING_ERROR)
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": "Error: service unavailable", "is_error": True,
                    })
                else:
                    transition(AgentState.EXECUTING_TOOL)
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": json.dumps({"data": f"results for {block.input.get('topic', '')}"})
                    })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

# Run scenarios to cover different state transition paths
test_cases = [
    ("Answer this directly without any tools: what is 2+2?", -1),
    ("Gather data on Python async patterns.", -1),
    ("Gather data on multiple topics: ML, databases, and networking.", -1),
    ("Gather data on something but the service fails.", 1),
]

for msg, error_turn in test_cases:
    run_state_tracked_agent(msg, inject_error_on_turn=error_turn)

report = coverage.report()
print(f"\n=== State Transition Coverage ===")
print(f"Coverage: {report['covered']}/{report['total_transitions']} transitions ({report['coverage_pct']}%)")
print(f"\nCovered: {report['covered_transitions']}")
if report["missed_transitions"]:
    print(f"Missed: {report['missed_transitions']}")

# Expected Token Savings: None — state machine coverage tracking
# Environment: Agents modeled as state machines where all valid transitions must be tested
```

### Option 6: Coverage-Aware Test Generator

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class CoverageGap:
    gap_id: str
    description: str
    suggested_test: str
    priority: str  # "critical", "high", "medium", "low"

@dataclass
class AgentCoverageSuite:
    """
    Tracks coverage across multiple dimensions and generates
    test case suggestions for uncovered scenarios.
    """
    tool_names: list[str]
    _executed_scenarios: list[dict] = field(default_factory=list)

    def record_scenario(
        self,
        scenario_name: str,
        tools_called: list[str],
        had_error: bool,
        turns: int,
        ended_naturally: bool,
    ):
        self._executed_scenarios.append({
            "name": scenario_name,
            "tools_called": tools_called,
            "tool_call_count": len(tools_called),
            "unique_tools": list(set(tools_called)),
            "had_error": had_error,
            "turns": turns,
            "ended_naturally": ended_naturally,
        })

    def analyze_gaps(self) -> list[CoverageGap]:
        gaps = []
        all_tools = set(self.tool_names)
        tools_ever_called = set(t for s in self._executed_scenarios for t in s["tools_called"])
        tools_never_called = all_tools - tools_ever_called

        # Gap: tools never called
        for tool in tools_never_called:
            gaps.append(CoverageGap(
                gap_id=f"tool_never_called:{tool}",
                description=f"Tool '{tool}' has never been invoked in any test scenario",
                suggested_test=f"Add a scenario that specifically triggers the '{tool}' tool",
                priority="high",
            ))

        # Gap: no error scenarios
        if not any(s["had_error"] for s in self._executed_scenarios):
            gaps.append(CoverageGap(
                gap_id="no_error_scenarios",
                description="No test scenarios exercise the error handling path",
                suggested_test="Add a scenario with mock_tool_error=True to test error recovery",
                priority="critical",
            ))

        # Gap: no multi-tool scenarios
        if not any(s["tool_call_count"] > 1 for s in self._executed_scenarios):
            gaps.append(CoverageGap(
                gap_id="no_multi_tool",
                description="No scenarios exercise multi-tool conversations",
                suggested_test="Add a complex task that requires calling 2+ tools",
                priority="high",
            ))

        # Gap: no zero-tool scenarios
        if not any(s["tool_call_count"] == 0 for s in self._executed_scenarios):
            gaps.append(CoverageGap(
                gap_id="no_direct_answer",
                description="No scenarios exercise direct answers without tool use",
                suggested_test="Add a factual question the model can answer without tools",
                priority="medium",
            ))

        # Gap: max turns never reached
        if not any(not s["ended_naturally"] for s in self._executed_scenarios):
            gaps.append(CoverageGap(
                gap_id="max_turns_never_hit",
                description="The max-turns termination condition has never been tested",
                suggested_test="Add a scenario with max_turns=1 on a multi-step task",
                priority="medium",
            ))

        return gaps

    def coverage_summary(self) -> dict:
        gaps = self.analyze_gaps()
        total_checks = 5 + len(self.tool_names)
        critical = sum(1 for g in gaps if g.priority == "critical")
        return {
            "scenarios_run": len(self._executed_scenarios),
            "gaps_found": len(gaps),
            "critical_gaps": critical,
            "coverage_pct": round((1 - len(gaps) / max(total_checks, 1)) * 100, 1),
            "gaps": [{"id": g.gap_id, "priority": g.priority, "suggested": g.suggested_test} for g in gaps],
        }

suite = AgentCoverageSuite(tool_names=["search", "calculate", "lookup", "write"])

def instrumented_agent(user_message: str, scenario_name: str, max_turns: int = 5, inject_error: bool = False) -> str:
    tools = [
        {"name": "search", "description": "Search", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
        {"name": "calculate", "description": "Calculate", "input_schema": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}},
    ]
    messages = [{"role": "user", "content": user_message}]
    tools_called: list[str] = []
    had_error = False
    turns = 0
    ended_naturally = False

    while turns < max_turns:
        turns += 1
        response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=256, tools=tools, messages=messages)

        if response.stop_reason == "end_turn":
            ended_naturally = True
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tools_called.append(block.name)
                if inject_error:
                    had_error = True
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": "Error", "is_error": True})
                else:
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"result": "ok"})})
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    suite.record_scenario(scenario_name, tools_called, had_error, turns, ended_naturally)
    return "done"

# Run a subset of tests — deliberately leave gaps
instrumented_agent("Search for Python docs.", "search_basic")
instrumented_agent("Calculate 15 * 7.", "calculate_basic")
instrumented_agent("Search for ML papers and calculate their average citation count.", "multi_tool")

summary = suite.coverage_summary()
print(f"\n=== Coverage-Aware Test Suite Analysis ===")
print(f"Scenarios run: {summary['scenarios_run']}")
print(f"Gaps found: {summary['gaps_found']} ({summary['critical_gaps']} critical)")
print(f"Estimated coverage: {summary['coverage_pct']}%\n")
print("Gap remediation suggestions:")
for gap in summary["gaps"]:
    print(f"  [{gap['priority'].upper()}] {gap['id']}")
    print(f"    → {gap['suggested']}")

# Expected Token Savings: None — coverage gap analysis infrastructure
# Environment: Teams building test suites for agents and wanting systematic gap discovery
```

## Comparison

| Option | Coverage Dimension | Auto-Suggests Fixes | Persistence | CI-Ready | Best For |
|--------|------------------|--------------------|-----------|---------|----|
| 1. Turn-Path Tracker | Conversation flow paths | No | Memory | Yes | Agents with branching turn sequences |
| 2. Tool Sequence Matrix | Tool call ordering | No | Memory | Yes | Tools where call order matters |
| 3. Branch Coverage | True/false branches | No | Memory | Yes | Agents with conditional routing logic |
| 4. Error Path Coverage | Error scenario coverage | No | Memory | Yes | High-reliability production agents |
| 5. State Transitions | State machine transitions | No | Memory | Yes | FSM-based agent architectures |
| 6. Coverage Gap Generator | Multi-dimensional | Yes | Memory | Yes | Teams building test suites from scratch |
