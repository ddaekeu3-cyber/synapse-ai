---
layout: solution
title: "Agent Doesn't Implement Trace-Based Testing"
category: testing
description: "How to record, replay, and assert on full agent execution traces including tool calls, intermediate states, and decision paths to catch behavioral regressions."
tags: [testing, tracing, replay, regression, observability, debugging]
---

# Agent Doesn't Implement Trace-Based Testing

Unit tests check functions; trace-based tests check agent behavior end-to-end. A trace captures the complete execution: input, model responses, tool calls, tool results, intermediate reasoning, and final output. Replaying traces lets you verify that refactors don't break behavior, that model upgrades preserve decisions, and that edge cases are reproducible without re-hitting expensive APIs.

## Option 1: Simple Trace Recorder and JSONL Replay

Record every agent interaction to JSONL during live runs. Replay recorded traces in tests without API calls.

```python
import anthropic
import json
import time
import os
from dataclasses import dataclass, asdict
from typing import Optional
from pathlib import Path

@dataclass
class TraceEvent:
    event_type: str        # "request", "response", "tool_call", "tool_result"
    timestamp: float
    data: dict

@dataclass
class AgentTrace:
    trace_id: str
    scenario: str
    events: list[TraceEvent]
    final_output: Optional[str] = None
    duration_seconds: float = 0.0


class TraceRecorder:
    def __init__(self, trace_dir: str = "traces"):
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(exist_ok=True)
        self.current_trace: Optional[AgentTrace] = None
        self._start_time: float = 0.0

    def start(self, scenario: str) -> str:
        trace_id = f"{scenario}-{int(time.time())}"
        self.current_trace = AgentTrace(
            trace_id=trace_id,
            scenario=scenario,
            events=[],
        )
        self._start_time = time.monotonic()
        return trace_id

    def record(self, event_type: str, data: dict):
        if self.current_trace:
            self.current_trace.events.append(TraceEvent(
                event_type=event_type,
                timestamp=time.time(),
                data=data,
            ))

    def finish(self, final_output: str):
        if self.current_trace:
            self.current_trace.final_output = final_output
            self.current_trace.duration_seconds = time.monotonic() - self._start_time
            self._save()

    def _save(self):
        if not self.current_trace:
            return
        path = self.trace_dir / f"{self.current_trace.trace_id}.jsonl"
        with open(path, "w") as f:
            # Header line
            f.write(json.dumps({
                "trace_id": self.current_trace.trace_id,
                "scenario": self.current_trace.scenario,
                "final_output": self.current_trace.final_output,
                "duration_seconds": self.current_trace.duration_seconds,
            }) + "\n")
            # Event lines
            for event in self.current_trace.events:
                f.write(json.dumps(asdict(event)) + "\n")
        print(f"[TRACE] Saved to {path}")
        return path


def load_trace(trace_path: str) -> AgentTrace:
    events = []
    header = None
    with open(trace_path) as f:
        for i, line in enumerate(f):
            data = json.loads(line.strip())
            if i == 0:
                header = data
            else:
                events.append(TraceEvent(**data))
    return AgentTrace(
        trace_id=header["trace_id"],
        scenario=header["scenario"],
        events=events,
        final_output=header["final_output"],
        duration_seconds=header["duration_seconds"],
    )


recorder = TraceRecorder()


def run_agent_with_recording(user_input: str, scenario: str = "default") -> str:
    client = anthropic.Anthropic()
    recorder.start(scenario)
    recorder.record("request", {"user_input": user_input})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": user_input}],
    )

    output = response.content[0].text
    recorder.record("response", {
        "output": output,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    })
    recorder.finish(output)
    return output


def assert_trace_behavior(trace: AgentTrace, assertions: list[dict]) -> list[str]:
    """Run assertions against a loaded trace. Returns list of failures."""
    failures = []

    for assertion in assertions:
        atype = assertion["type"]

        if atype == "output_contains":
            keyword = assertion["keyword"]
            if keyword.lower() not in (trace.final_output or "").lower():
                failures.append(f"Output missing keyword: '{keyword}'")

        elif atype == "tool_called":
            tool_name = assertion["tool_name"]
            tool_calls = [e for e in trace.events if e.event_type == "tool_call"
                          and e.data.get("tool_name") == tool_name]
            if not tool_calls:
                failures.append(f"Expected tool '{tool_name}' was never called")

        elif atype == "max_duration":
            if trace.duration_seconds > assertion["seconds"]:
                failures.append(f"Trace took {trace.duration_seconds:.1f}s > limit {assertion['seconds']}s")

        elif atype == "event_count":
            actual = len([e for e in trace.events if e.event_type == assertion["event_type"]])
            expected = assertion["expected"]
            if actual != expected:
                failures.append(f"Expected {expected} '{assertion['event_type']}' events, got {actual}")

    return failures


if __name__ == "__main__":
    # Record a trace
    output = run_agent_with_recording("What is the capital of France?", scenario="capital-query")
    print(f"Output: {output}")

    # Find and test the saved trace
    trace_files = list(Path("traces").glob("capital-query-*.jsonl"))
    if trace_files:
        trace = load_trace(str(trace_files[-1]))
        failures = assert_trace_behavior(trace, [
            {"type": "output_contains", "keyword": "Paris"},
            {"type": "max_duration", "seconds": 30.0},
            {"type": "event_count", "event_type": "response", "expected": 1},
        ])
        if failures:
            print(f"FAILURES: {failures}")
        else:
            print("All trace assertions passed!")

# Expected Token Savings: Zero API cost during test replay; baseline traces captured once, tested many times
# Environment: CI/CD pipelines, regression test suites for agent behavior
```

## Option 2: Deterministic Replay with Mocked API Responses

Store full API responses in the trace and replay them as mocks — no network calls during test replay.

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch


@dataclass
class RecordedExchange:
    request_params: dict
    response_data: dict
    latency_ms: float


@dataclass
class ReplayTrace:
    scenario_id: str
    exchanges: list[RecordedExchange] = field(default_factory=list)
    replay_index: int = 0

    def next_response(self) -> Optional[dict]:
        if self.replay_index < len(self.exchanges):
            exchange = self.exchanges[self.replay_index]
            self.replay_index += 1
            return exchange.response_data
        return None

    def reset(self):
        self.replay_index = 0

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump({
                "scenario_id": self.scenario_id,
                "exchanges": [
                    {
                        "request_params": e.request_params,
                        "response_data": e.response_data,
                        "latency_ms": e.latency_ms,
                    }
                    for e in self.exchanges
                ],
            }, f, indent=2)

    @staticmethod
    def load(path: str) -> "ReplayTrace":
        with open(path) as f:
            data = json.load(f)
        trace = ReplayTrace(scenario_id=data["scenario_id"])
        for ex in data["exchanges"]:
            trace.exchanges.append(RecordedExchange(**ex))
        return trace


class RecordingClient:
    """Wraps anthropic.Anthropic to intercept and record all API calls."""

    def __init__(self, trace: ReplayTrace):
        self._client = anthropic.Anthropic()
        self._trace = trace

    def create_message(self, **kwargs) -> dict:
        start = time.monotonic()
        response = self._client.messages.create(**kwargs)
        latency = (time.monotonic() - start) * 1000

        # Serialize response
        response_data = {
            "id": response.id,
            "model": response.model,
            "stop_reason": response.stop_reason,
            "content": [{"type": c.type, "text": c.text if hasattr(c, "text") else None}
                        for c in response.content],
            "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
        }

        # Sanitize request params (remove non-serializable items)
        safe_params = {k: v for k, v in kwargs.items() if k != "stream"}

        self._trace.exchanges.append(RecordedExchange(
            request_params=safe_params,
            response_data=response_data,
            latency_ms=latency,
        ))

        return response_data


def build_mock_client(trace: ReplayTrace):
    """Build a mock anthropic client that replays recorded responses."""
    mock_client = MagicMock()

    def mock_create(**kwargs):
        recorded = trace.next_response()
        if not recorded:
            raise ValueError(f"No more recorded exchanges in trace {trace.scenario_id}")

        # Build mock response
        mock_response = MagicMock()
        mock_response.id = recorded["id"]
        mock_response.model = recorded["model"]
        mock_response.stop_reason = recorded["stop_reason"]
        mock_response.usage.input_tokens = recorded["usage"]["input_tokens"]
        mock_response.usage.output_tokens = recorded["usage"]["output_tokens"]

        mock_content = []
        for c in recorded["content"]:
            mc = MagicMock()
            mc.type = c["type"]
            mc.text = c.get("text", "")
            mock_content.append(mc)
        mock_response.content = mock_content

        return mock_response

    mock_client.messages.create.side_effect = mock_create
    return mock_client


def my_agent_logic(client, user_input: str) -> str:
    """Agent logic that uses the provided client."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": user_input}],
    )
    return response.content[0].text


def record_scenario(scenario_id: str, user_input: str, save_path: str) -> str:
    trace = ReplayTrace(scenario_id=scenario_id)
    recording_client = RecordingClient(trace)

    output = my_agent_logic(recording_client, user_input)
    trace.save(save_path)
    print(f"[RECORD] Saved {len(trace.exchanges)} exchanges to {save_path}")
    return output


def replay_and_test(trace_path: str, user_input: str) -> tuple[str, bool]:
    trace = ReplayTrace.load(trace_path)
    mock_client = build_mock_client(trace)

    output = my_agent_logic(mock_client, user_input)

    # Verify all exchanges were consumed
    all_consumed = trace.replay_index == len(trace.exchanges)
    return output, all_consumed


if __name__ == "__main__":
    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        trace_path = f.name

    try:
        # Record
        user_input = "Explain what REST API means in one sentence."
        live_output = record_scenario("rest-api-explanation", user_input, trace_path)
        print(f"Live output: {live_output[:100]}")

        # Replay (no API call)
        replayed_output, consumed = replay_and_test(trace_path, user_input)
        print(f"Replayed output: {replayed_output[:100]}")
        print(f"Outputs match: {live_output == replayed_output}")
        print(f"All exchanges consumed: {consumed}")
    finally:
        os.unlink(trace_path)

# Expected Token Savings: 100% savings on replay — zero API calls during test suite runs after initial recording
# Environment: Unit testing, CI pipelines where deterministic replay is required without network access
```

## Option 3: Tool Call Sequence Assertions

Record the exact sequence of tool calls (name, arguments, result) and assert that refactored agents make the same calls in the same order.

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCallRecord:
    step: int
    tool_name: str
    arguments: dict
    result: Any
    model_reasoning: Optional[str] = None


@dataclass
class ToolSequenceTrace:
    scenario: str
    user_input: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    final_output: str = ""

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump({
                "scenario": self.scenario,
                "user_input": self.user_input,
                "tool_calls": [
                    {
                        "step": tc.step,
                        "tool_name": tc.tool_name,
                        "arguments": tc.arguments,
                        "result": tc.result,
                        "model_reasoning": tc.model_reasoning,
                    }
                    for tc in self.tool_calls
                ],
                "final_output": self.final_output,
            }, f, indent=2)

    @staticmethod
    def load(path: str) -> "ToolSequenceTrace":
        with open(path) as f:
            data = json.load(f)
        trace = ToolSequenceTrace(
            scenario=data["scenario"],
            user_input=data["user_input"],
            final_output=data["final_output"],
        )
        for tc in data["tool_calls"]:
            trace.tool_calls.append(ToolCallRecord(**tc))
        return trace


@dataclass
class SequenceAssertion:
    description: str
    expected_tools: list[str]          # Ordered tool names
    allow_extra_calls: bool = False    # Whether extra tool calls are OK
    check_args: bool = True            # Whether to check argument keys


def assert_tool_sequence(
    trace: ToolSequenceTrace,
    assertion: SequenceAssertion,
) -> list[str]:
    failures = []
    actual_tools = [tc.tool_name for tc in trace.tool_calls]

    if not assertion.allow_extra_calls:
        if actual_tools != assertion.expected_tools:
            failures.append(
                f"Tool sequence mismatch.\n"
                f"  Expected: {assertion.expected_tools}\n"
                f"  Actual:   {actual_tools}"
            )
    else:
        # Subsequence check
        expected_idx = 0
        for tool in actual_tools:
            if expected_idx < len(assertion.expected_tools) and tool == assertion.expected_tools[expected_idx]:
                expected_idx += 1
        if expected_idx < len(assertion.expected_tools):
            failures.append(
                f"Expected tool sequence not found as subsequence.\n"
                f"  Expected: {assertion.expected_tools}\n"
                f"  Actual:   {actual_tools}"
            )

    if assertion.check_args:
        for tc in trace.tool_calls:
            if tc.arguments is None or not isinstance(tc.arguments, dict):
                failures.append(f"Tool '{tc.tool_name}' (step {tc.step}) has no valid arguments dict")

    return failures


def run_agent_with_tool_recording(
    user_input: str,
    scenario: str,
) -> ToolSequenceTrace:
    client = anthropic.Anthropic()
    trace = ToolSequenceTrace(scenario=scenario, user_input=user_input)

    tools = [
        {
            "name": "search",
            "description": "Search for information",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
        {
            "name": "calculate",
            "description": "Perform mathematical calculations",
            "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
        },
    ]

    messages = [{"role": "user", "content": user_input}]
    step = 0

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            tools=tools,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                step += 1
                # Simulate tool execution
                result = f"[Result for {block.name}({json.dumps(block.input)[:40]})]"
                trace.tool_calls.append(ToolCallRecord(
                    step=step,
                    tool_name=block.name,
                    arguments=block.input,
                    result=result,
                ))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})
        else:
            # No more tool calls — final response
            final = next((b.text for b in response.content if hasattr(b, "text")), "")
            trace.final_output = final
            break

    return trace


if __name__ == "__main__":
    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        trace_path = f.name

    try:
        trace = run_agent_with_tool_recording(
            "Search for the GDP of France and calculate 10% of it.",
            scenario="gdp-calculation",
        )
        trace.save(trace_path)

        loaded = ToolSequenceTrace.load(trace_path)
        failures = assert_tool_sequence(loaded, SequenceAssertion(
            description="GDP calculation requires search then calculate",
            expected_tools=["search", "calculate"],
            allow_extra_calls=False,
        ))

        if failures:
            print(f"FAILURES:\n" + "\n".join(failures))
        else:
            print(f"Tool sequence assertion passed: {[tc.tool_name for tc in loaded.tool_calls]}")
    finally:
        os.unlink(trace_path)

# Expected Token Savings: Catches tool-call order regressions without repeated live runs; gold traces recorded once
# Environment: Multi-step agents where tool execution order matters for correctness
```

## Option 4: Trace Diffing for Model Upgrade Validation

Compare traces from two model versions to detect behavioral drift during upgrades.

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Optional
import difflib


@dataclass
class TraceDiff:
    scenario: str
    model_a: str
    model_b: str
    output_similarity: float       # 0.0–1.0
    tool_sequence_match: bool
    semantic_drift_detected: bool
    diff_summary: list[str]


def run_agent(prompt: str, model: str) -> dict:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return {
        "model": model,
        "output": response.content[0].text,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


def compute_text_similarity(a: str, b: str) -> float:
    """Character-level similarity ratio."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def detect_semantic_drift(output_a: str, output_b: str) -> tuple[bool, list[str]]:
    """Use Claude to judge whether two outputs are semantically equivalent."""
    client = anthropic.Anthropic()

    judge_prompt = f"""Compare these two AI responses for semantic equivalence.

Response A:
{output_a[:500]}

Response B:
{output_b[:500]}

Are these responses semantically equivalent (same facts, same intent, same conclusions)?
Respond with JSON: {{"equivalent": true/false, "key_differences": ["diff1", "diff2"]}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": judge_prompt}],
    )

    try:
        text = response.content[0].text
        import re
        json_match = re.search(r"\{[^}]+\}", text, re.DOTALL)
        data = json.loads(json_match.group()) if json_match else {}
        diffs = data.get("key_differences", [])
        is_equivalent = data.get("equivalent", True)
        return not is_equivalent, diffs
    except Exception:
        return False, []


def diff_model_outputs(
    scenarios: list[dict],
    model_a: str = "claude-haiku-4-5-20251001",
    model_b: str = "claude-sonnet-4-6",
) -> list[TraceDiff]:
    diffs = []

    for scenario in scenarios:
        prompt = scenario["prompt"]
        name = scenario["name"]

        print(f"\nRunning scenario: {name}")
        result_a = run_agent(prompt, model_a)
        result_b = run_agent(prompt, model_b)

        similarity = compute_text_similarity(result_a["output"], result_b["output"])
        drift, differences = detect_semantic_drift(result_a["output"], result_b["output"])

        summary = []
        if similarity < 0.3:
            summary.append(f"Low text similarity ({similarity:.0%}) — responses are very different in phrasing")
        if drift:
            summary.extend(differences)
        if not summary:
            summary.append("No significant differences detected")

        diffs.append(TraceDiff(
            scenario=name,
            model_a=model_a,
            model_b=model_b,
            output_similarity=similarity,
            tool_sequence_match=True,  # Would compare tool calls in full implementation
            semantic_drift_detected=drift,
            diff_summary=summary,
        ))

        print(f"  Similarity: {similarity:.0%} | Drift: {drift}")
        for d in summary:
            print(f"  - {d}")

    return diffs


if __name__ == "__main__":
    scenarios = [
        {"name": "capital-query", "prompt": "What is the capital of Japan?"},
        {"name": "math-reasoning", "prompt": "If a train travels 120km at 60km/h, how long does it take?"},
        {"name": "code-generation", "prompt": "Write a Python function to check if a string is a palindrome."},
    ]

    results = diff_model_outputs(scenarios)

    print("\n=== UPGRADE VALIDATION REPORT ===")
    for diff in results:
        status = "DRIFT DETECTED" if diff.semantic_drift_detected else "OK"
        print(f"{diff.scenario}: {status} (similarity={diff.output_similarity:.0%})")

# Expected Token Savings: Validates model upgrades without manual review; catches regressions automatically
# Environment: Model upgrade testing, A/B validation, canary deployments of new model versions
```

## Option 5: Trace Coverage Analysis — Which Scenarios Remain Untested

Analyze which user intent categories are covered by existing traces and surface gaps.

```python
import anthropic
import json
import os
from dataclasses import dataclass
from pathlib import Path
from collections import defaultdict


@dataclass
class CoverageReport:
    total_traces: int
    categories: dict[str, int]       # category -> count
    uncovered_categories: list[str]
    coverage_percentage: float


INTENT_CATEGORIES = [
    "factual_lookup",
    "math_calculation",
    "code_generation",
    "creative_writing",
    "summarization",
    "data_extraction",
    "comparison",
    "step_by_step_instructions",
    "question_answering",
    "error_analysis",
]


def classify_trace_scenario(user_input: str) -> str:
    """Use Claude to classify what intent category a trace covers."""
    client = anthropic.Anthropic()

    categories_str = "\n".join(f"- {c}" for c in INTENT_CATEGORIES)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": f"""Classify this user input into exactly one category:

{categories_str}

User input: "{user_input}"

Reply with only the category name, nothing else."""}],
    )
    label = response.content[0].text.strip().lower().replace(" ", "_")
    # Normalize to known categories
    for cat in INTENT_CATEGORIES:
        if cat in label or label in cat:
            return cat
    return "question_answering"  # default


def analyze_trace_coverage(trace_dir: str) -> CoverageReport:
    trace_files = list(Path(trace_dir).glob("*.json")) + list(Path(trace_dir).glob("*.jsonl"))

    category_counts: dict[str, int] = defaultdict(int)
    total = 0

    for path in trace_files:
        try:
            with open(path) as f:
                # Try to read as JSONL (first line is header)
                first_line = f.readline()
                data = json.loads(first_line)
                user_input = data.get("user_input", data.get("scenario", ""))

            if user_input:
                category = classify_trace_scenario(user_input)
                category_counts[category] += 1
                total += 1
                print(f"  {path.name}: {category}")
        except Exception as e:
            print(f"  Skipping {path.name}: {e}")

    covered = set(category_counts.keys())
    uncovered = [c for c in INTENT_CATEGORIES if c not in covered]
    coverage_pct = len(covered) / len(INTENT_CATEGORIES) * 100

    return CoverageReport(
        total_traces=total,
        categories=dict(category_counts),
        uncovered_categories=uncovered,
        coverage_percentage=coverage_pct,
    )


def suggest_missing_test_cases(uncovered: list[str]) -> list[str]:
    """Generate example prompts for uncovered categories."""
    examples = {
        "factual_lookup": "What is the boiling point of water in Celsius?",
        "math_calculation": "Calculate the compound interest on $1000 at 5% for 3 years.",
        "code_generation": "Write a Python function to merge two sorted arrays.",
        "creative_writing": "Write a haiku about autumn leaves.",
        "summarization": "Summarize the key points of this paragraph in 2 sentences.",
        "data_extraction": "Extract all email addresses from the following text.",
        "comparison": "Compare Python and JavaScript for backend development.",
        "step_by_step_instructions": "How do I set up a Python virtual environment?",
        "question_answering": "Why is the sky blue?",
        "error_analysis": "Why does this Python code raise a KeyError?",
    }
    return [f"ADD: [{cat}] Example: '{examples.get(cat, 'Add test for ' + cat)}'" for cat in uncovered]


if __name__ == "__main__":
    import tempfile

    # Create a temp trace dir with sample traces
    with tempfile.TemporaryDirectory() as trace_dir:
        # Write sample traces
        sample_traces = [
            {"user_input": "What is 2+2?", "final_output": "4"},
            {"user_input": "Write a function to reverse a string", "final_output": "def rev(s): return s[::-1]"},
            {"user_input": "What is the capital of France?", "final_output": "Paris"},
        ]
        for i, trace in enumerate(sample_traces):
            with open(f"{trace_dir}/trace_{i}.json", "w") as f:
                json.dump(trace, f)

        print("Analyzing trace coverage...")
        report = analyze_trace_coverage(trace_dir)

        print(f"\n=== COVERAGE REPORT ===")
        print(f"Total traces: {report.total_traces}")
        print(f"Coverage: {report.coverage_percentage:.0f}% ({len(report.categories)}/{len(INTENT_CATEGORIES)} categories)")
        print(f"\nCovered: {list(report.categories.keys())}")
        print(f"\nUncovered ({len(report.uncovered_categories)}):")
        for suggestion in suggest_missing_test_cases(report.uncovered_categories):
            print(f"  {suggestion}")

# Expected Token Savings: Prevents redundant trace coverage; focuses new test recording on uncovered scenarios
# Environment: Mature agent test suites where coverage gaps need systematic identification
```

## Option 6: Trace-as-Contract — Behavioral Specification Enforcement

Define behavioral contracts as expected trace patterns and fail CI if any contract is violated.

```python
import anthropic
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class TraceContract:
    name: str
    description: str
    user_input_pattern: str       # Regex pattern to match applicable inputs
    checks: list[Callable]        # List of check functions: (trace_data) -> Optional[str]

    def applies_to(self, user_input: str) -> bool:
        return bool(re.search(self.user_input_pattern, user_input, re.IGNORECASE))

    def validate(self, trace_data: dict) -> list[str]:
        failures = []
        for check in self.checks:
            result = check(trace_data)
            if result:
                failures.append(result)
        return failures


def output_not_empty(trace: dict) -> Optional[str]:
    if not trace.get("final_output", "").strip():
        return "Contract violation: output is empty"
    return None


def output_under_500_words(trace: dict) -> Optional[str]:
    output = trace.get("final_output", "")
    word_count = len(output.split())
    if word_count > 500:
        return f"Contract violation: output has {word_count} words, max is 500"
    return None


def no_apologies_for_capability(trace: dict) -> Optional[str]:
    output = (trace.get("final_output") or "").lower()
    apology_patterns = ["i cannot", "i'm unable to", "i am not able", "i don't have the ability"]
    for pat in apology_patterns:
        if pat in output:
            return f"Contract violation: agent refused capability — found '{pat}'"
    return None


def contains_code_block(trace: dict) -> Optional[str]:
    output = trace.get("final_output", "")
    if "```" not in output and "def " not in output and "function " not in output:
        return "Contract violation: code generation request produced no code block"
    return None


def no_pii_in_output(trace: dict) -> Optional[str]:
    output = trace.get("final_output", "")
    pii_patterns = [
        r"\b\d{3}-\d{2}-\d{4}\b",        # SSN
        r"\b4[0-9]{12}(?:[0-9]{3})?\b",  # Visa card
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",  # Email
    ]
    for pat in pii_patterns:
        if re.search(pat, output):
            return f"Contract violation: PII pattern detected in output"
    return None


# Define behavioral contracts
CONTRACTS = [
    TraceContract(
        name="code-generation-contract",
        description="Code generation requests must produce actual code",
        user_input_pattern=r"(write|implement|create|generate).*(function|class|code|script)",
        checks=[output_not_empty, contains_code_block, output_under_500_words],
    ),
    TraceContract(
        name="factual-query-contract",
        description="Factual questions must be answered (not refused)",
        user_input_pattern=r"^(what|who|when|where|why|how).+\?$",
        checks=[output_not_empty, no_apologies_for_capability],
    ),
    TraceContract(
        name="pii-safety-contract",
        description="No PII must appear in any response",
        user_input_pattern=r".*",  # Applies to all inputs
        checks=[no_pii_in_output],
    ),
]


def run_trace_and_validate_contracts(user_input: str) -> tuple[dict, list[str]]:
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": user_input}],
    )

    trace_data = {
        "user_input": user_input,
        "final_output": response.content[0].text,
        "model": response.model,
    }

    all_failures = []
    for contract in CONTRACTS:
        if contract.applies_to(user_input):
            failures = contract.validate(trace_data)
            if failures:
                all_failures.extend([f"[{contract.name}] {f}" for f in failures])
                print(f"CONTRACT VIOLATED: {contract.name}")
            else:
                print(f"Contract OK: {contract.name}")

    return trace_data, all_failures


if __name__ == "__main__":
    test_cases = [
        "Write a Python function to sort a list of dictionaries by key.",
        "What is the speed of light?",
        "What is the capital of Germany?",
    ]

    all_failures = []
    for inp in test_cases:
        print(f"\nInput: {inp}")
        trace, failures = run_trace_and_validate_contracts(inp)
        all_failures.extend(failures)
        print(f"Output preview: {trace['final_output'][:80]}...")

    print(f"\n{'='*60}")
    if all_failures:
        print(f"CONTRACT FAILURES ({len(all_failures)}):")
        for f in all_failures:
            print(f"  {f}")
        exit(1)
    else:
        print("All behavioral contracts satisfied.")

# Expected Token Savings: Catches behavioral regressions in CI before deployment; prevents costly rollback cycles
# Environment: Production agents with defined behavioral SLAs, regulated industries with compliance requirements
```

## Comparison

| Option | Recording | Replay | API Calls in Test | Best For |
|--------|-----------|--------|-------------------|----------|
| 1 JSONL Recorder | Full trace to JSONL | Assertion on loaded trace | Yes (recording phase) | Simple regression tracking |
| 2 Deterministic Mock Replay | Full API response stored | Zero API calls | None | Fast CI, offline testing |
| 3 Tool Sequence Assertions | Tool calls recorded | Order/args assertion | Yes (recording phase) | Multi-step tool-use agents |
| 4 Model Diff | Live runs per model | Semantic diff with judge | Yes (both models) | Model upgrade validation |
| 5 Coverage Analysis | Existing traces classified | Gap report generated | Yes (classification) | Test suite completeness audit |
| 6 Trace-as-Contract | Live run with validation | Real-time contract check | Yes | CI behavioral compliance gating |
