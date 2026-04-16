---
layout: solution
title: "Agent Doesn't Implement Evaluation Harness for Multi-Agent Workflows"
category: testing
description: "Test multi-agent pipelines end-to-end — verifying that orchestrator → subagent → tool → verifier chains produce correct outputs, not just that individual components work in isolation."
tags: [testing, evaluation, multi-agent, harness, e2e, orchestrator, pipeline, quality]
---

## Problem

Teams test individual LLM calls in isolation but never test the full multi-agent pipeline. An orchestrator agent, a research subagent, a code generation agent, and a verification agent each pass their unit tests — but when wired together, the orchestrator sends malformed handoffs, the verifier never triggers, and the final output contains hallucinations that any single-agent eval would have caught. Without an end-to-end evaluation harness, integration failures are discovered in production.

```python
# Naive: test each agent individually, never the pipeline
def test_summarizer():
    assert summarizer("text") is not None  # passes
def test_researcher():
    assert researcher("query") is not None  # passes
# Pipeline: summarizer → researcher → synthesizer — never tested together
```

## Solution Options

### Option 1: Sequential Pipeline Evaluator with Step Assertions

Define the pipeline as a sequence of steps. Run test cases through the full sequence and assert on intermediate and final outputs at each step.

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Callable, Any

@dataclass
class PipelineStep:
    name: str
    execute: Callable[[str, dict], str]   # (input, context) → output
    assertions: list[Callable[[str], bool]]  # list of checks on the step output
    assertion_labels: list[str]

@dataclass
class StepResult:
    step_name: str
    input_text: str
    output_text: str
    passed: list[str]
    failed: list[str]

@dataclass
class PipelineEvalResult:
    test_case: str
    step_results: list[StepResult]
    overall_pass: bool

client = anthropic.Anthropic()

def run_pipeline_eval(
    pipeline: list[PipelineStep],
    test_cases: list[dict],  # {"input": str, "expected_final": Callable[[str], bool]}
) -> list[PipelineEvalResult]:
    results = []
    for tc in test_cases:
        step_results = []
        current_input = tc["input"]
        context = {"original_input": current_input}
        all_passed = True
        for step in pipeline:
            output = step.execute(current_input, context)
            context[f"{step.name}_output"] = output
            passed = []
            failed = []
            for assertion, label in zip(step.assertions, step.assertion_labels):
                try:
                    if assertion(output):
                        passed.append(label)
                    else:
                        failed.append(label)
                        all_passed = False
                except Exception as e:
                    failed.append(f"{label} (error: {e})")
                    all_passed = False
            step_results.append(StepResult(step.name, current_input, output, passed, failed))
            current_input = output  # chain output → next input

        # Final assertion on last output
        if "expected_final" in tc:
            try:
                if not tc["expected_final"](current_input):
                    all_passed = False
                    step_results[-1].failed.append("final_assertion")
            except Exception:
                all_passed = False

        results.append(PipelineEvalResult(tc["input"][:40], step_results, all_passed))

    # Print summary
    passed = sum(1 for r in results if r.overall_pass)
    print(f"\n[EVAL] {passed}/{len(results)} test cases passed")
    for r in results:
        status = "PASS" if r.overall_pass else "FAIL"
        print(f"  [{status}] {r.test_case}")
        for sr in r.step_results:
            if sr.failed:
                print(f"    {sr.step_name}: FAILED {sr.failed}")
    return results


# Define a research → summarize pipeline
def research_step(query: str, context: dict) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are a research assistant. Find key facts.",
        messages=[{"role": "user", "content": f"Research: {query}"}],
    )
    return r.content[0].text

def summarize_step(research: str, context: dict) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="Summarize the research in 2-3 bullet points.",
        messages=[{"role": "user", "content": research}],
    )
    return r.content[0].text

pipeline = [
    PipelineStep(
        name="research",
        execute=research_step,
        assertions=[
            lambda o: len(o) > 50,
            lambda o: "error" not in o.lower(),
        ],
        assertion_labels=["min_length", "no_error"],
    ),
    PipelineStep(
        name="summarize",
        execute=summarize_step,
        assertions=[
            lambda o: "•" in o or "-" in o or "1." in o,
            lambda o: len(o.split()) < 100,
        ],
        assertion_labels=["has_bullets", "concise"],
    ),
]

test_cases = [
    {"input": "Python async programming", "expected_final": lambda o: "async" in o.lower()},
    {"input": "Machine learning basics", "expected_final": lambda o: len(o) > 30},
]

run_pipeline_eval(pipeline, test_cases)

# Expected Token Savings: Eval overhead is actual pipeline tokens; no extra LLM calls for pass/fail assertions
# Environment: ANTHROPIC_API_KEY
```

### Option 2: LLM-as-Judge Multi-Agent Eval with Rubric Scoring

Use a judge LLM to evaluate the final output of a multi-agent pipeline against a rubric. Score multiple dimensions and record a pass/fail verdict.

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass

@dataclass
class RubricDimension:
    name: str
    description: str
    weight: float  # sum of weights should be 1.0

@dataclass
class EvalVerdictItem:
    dimension: str
    score: float     # 0.0–1.0
    reasoning: str

@dataclass
class EvalVerdict:
    test_case: str
    pipeline_output: str
    dimension_scores: list[EvalVerdictItem]
    weighted_score: float
    passed: bool     # weighted_score >= threshold

client = anthropic.AsyncAnthropic()

JUDGE_PROMPT = """Evaluate this AI pipeline output against the following rubric.

Original task: {task}
Pipeline output: {output}

Rubric:
{rubric}

Score each dimension 0.0–1.0. Return JSON:
[{{"dimension": "<name>", "score": <float>, "reasoning": "<one sentence>"}}]"""

async def judge_output(
    task: str,
    output: str,
    rubric: list[RubricDimension],
    pass_threshold: float = 0.70,
) -> EvalVerdict:
    rubric_text = "\n".join(
        f"- {d.name} (weight={d.weight}): {d.description}" for d in rubric
    )
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(
            task=task, output=output[:800], rubric=rubric_text,
        )}],
    )
    try:
        items_raw = json.loads(r.content[0].text)
    except Exception:
        items_raw = []

    items = [EvalVerdictItem(**item) for item in items_raw]
    # Fill missing dimensions
    scored_dims = {item.dimension for item in items}
    for dim in rubric:
        if dim.name not in scored_dims:
            items.append(EvalVerdictItem(dim.name, 0.0, "not evaluated"))

    # Compute weighted score
    dim_map = {item.dimension: item.score for item in items}
    weighted = sum(dim_map.get(d.name, 0) * d.weight for d in rubric)

    return EvalVerdict(
        test_case=task[:50],
        pipeline_output=output[:200],
        dimension_scores=items,
        weighted_score=weighted,
        passed=weighted >= pass_threshold,
    )

async def run_multi_agent_pipeline(task: str) -> str:
    """Simulate a 2-agent pipeline: planner → executor."""
    plan_r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system="You are a planner. Create a brief plan.",
        messages=[{"role": "user", "content": f"Plan: {task}"}],
    )
    plan = plan_r.content[0].text
    exec_r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are an executor. Follow the plan and produce the output.",
        messages=[{"role": "user", "content": f"Plan:\n{plan}\n\nExecute the task: {task}"}],
    )
    return exec_r.content[0].text

async def eval_pipeline(test_cases: list[str], rubric: list[RubricDimension]) -> None:
    async def eval_one(task: str) -> EvalVerdict:
        output = await run_multi_agent_pipeline(task)
        return await judge_output(task, output, rubric)

    verdicts = await asyncio.gather(*[eval_one(t) for t in test_cases])
    passed = sum(1 for v in verdicts if v.passed)
    print(f"\n[EVAL] {passed}/{len(verdicts)} passed (threshold=0.70)")
    for v in verdicts:
        status = "PASS" if v.passed else "FAIL"
        print(f"  [{status}] score={v.weighted_score:.2f} | {v.test_case}")
        for item in v.dimension_scores:
            print(f"    {item.dimension}={item.score:.2f}: {item.reasoning}")

rubric = [
    RubricDimension("relevance", "Output addresses the original task", 0.40),
    RubricDimension("completeness", "Output covers all key aspects", 0.30),
    RubricDimension("clarity", "Output is clear and well-structured", 0.30),
]

asyncio.run(eval_pipeline([
    "Explain how to implement a REST API in Python",
    "Describe the differences between SQL and NoSQL databases",
], rubric))

# Expected Token Savings: Judge adds ~200 tokens per eval; evaluates full pipeline quality not just component health
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Deterministic Replay Eval with Mocked Subagents

Replace real subagent calls with deterministic mocks during evaluation. Run hundreds of test cases cheaply by controlling every agent's output and verifying orchestrator behavior.

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Callable
from unittest.mock import patch, MagicMock

@dataclass
class MockAgentResponse:
    content: str
    should_fail: bool = False
    error_message: str = ""

@dataclass
class ReplayTestCase:
    name: str
    initial_input: str
    mock_responses: dict[str, MockAgentResponse]  # agent_name → mock response
    final_assertions: list[Callable[[str], bool]]
    assertion_labels: list[str]

@dataclass
class ReplayResult:
    test_name: str
    passed: bool
    failed_assertions: list[str]
    final_output: str

client = anthropic.Anthropic()

class MultiAgentOrchestrator:
    """Example orchestrator that calls planner, researcher, and writer subagents."""

    def __init__(self):
        self.agent_calls: list[dict] = []

    def _call_agent(self, agent_name: str, input_text: str, system: str = "") -> str:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system or f"You are the {agent_name}.",
            messages=[{"role": "user", "content": input_text}],
        )
        result = r.content[0].text
        self.agent_calls.append({"agent": agent_name, "input": input_text[:50], "output": result[:50]})
        return result

    def run(self, task: str) -> str:
        plan = self._call_agent("planner", f"Create a plan for: {task}")
        research = self._call_agent("researcher", f"Research for: {plan}")
        output = self._call_agent("writer", f"Write based on:\nPlan: {plan}\nResearch: {research}")
        return output


def run_replay_eval(test_cases: list[ReplayTestCase]) -> list[ReplayResult]:
    results = []
    for tc in test_cases:
        orchestrator = MultiAgentOrchestrator()

        # Create mock for each subagent response
        response_sequence = list(tc.mock_responses.values())
        call_index = [0]

        def mock_create(**kwargs):
            idx = call_index[0] % len(response_sequence)
            mock_resp = response_sequence[idx]
            call_index[0] += 1
            if mock_resp.should_fail:
                raise Exception(mock_resp.error_message)
            m = MagicMock()
            m.content = [MagicMock(text=mock_resp.content)]
            m.usage = MagicMock(input_tokens=10, output_tokens=20)
            return m

        try:
            with patch.object(client.messages, "create", side_effect=mock_create):
                final_output = orchestrator.run(tc.initial_input)
        except Exception as e:
            final_output = f"ERROR: {e}"

        # Run assertions
        failed = []
        for assertion, label in zip(tc.final_assertions, tc.assertion_labels):
            try:
                if not assertion(final_output):
                    failed.append(label)
            except Exception as e:
                failed.append(f"{label}(error:{e})")

        results.append(ReplayResult(
            test_name=tc.name,
            passed=len(failed) == 0,
            failed_assertions=failed,
            final_output=final_output[:100],
        ))

    # Summary
    passed = sum(1 for r in results if r.passed)
    print(f"\n[REPLAY EVAL] {passed}/{len(results)} passed")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.test_name}")
        if r.failed_assertions:
            print(f"    Failed: {r.failed_assertions}")
    return results


test_cases = [
    ReplayTestCase(
        name="happy_path",
        initial_input="Write a blog post about Python",
        mock_responses={
            "planner": MockAgentResponse("1. Research Python\n2. Write intro\n3. Write body\n4. Conclude"),
            "researcher": MockAgentResponse("Python is a popular language created in 1991 by Guido van Rossum."),
            "writer": MockAgentResponse("# Python: A Powerful Language\n\nPython was created in 1991..."),
        },
        final_assertions=[
            lambda o: "python" in o.lower(),
            lambda o: len(o) > 50,
            lambda o: "ERROR" not in o,
        ],
        assertion_labels=["mentions_python", "min_length", "no_error"],
    ),
    ReplayTestCase(
        name="subagent_failure",
        initial_input="Analyze sales data",
        mock_responses={
            "planner": MockAgentResponse("", should_fail=True, error_message="Service unavailable"),
            "researcher": MockAgentResponse("Research data..."),
            "writer": MockAgentResponse("Analysis complete."),
        },
        final_assertions=[lambda o: "ERROR" in o],
        assertion_labels=["propagates_error"],
    ),
]

run_replay_eval(test_cases)

# Expected Token Savings: Mocked evals use 0 real LLM tokens; run 100s of test cases in seconds
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Golden Dataset Regression Suite for Pipeline Outputs

Maintain a golden dataset of (input, expected_output_properties) pairs. Run the pipeline against the golden set after every prompt change and report regressions.

```python
import anthropic
import json
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class GoldenCase:
    case_id: str
    input_text: str
    must_contain: list[str]     # substrings that must appear
    must_not_contain: list[str]  # substrings that must NOT appear
    min_length: int = 0
    max_length: int = 10000
    must_match_json_keys: list[str] = field(default_factory=list)  # if output should be JSON

@dataclass
class GoldenResult:
    case_id: str
    passed: bool
    violations: list[str]
    output_preview: str

GOLDEN_DATASET: list[GoldenCase] = [
    GoldenCase(
        case_id="code_gen_001",
        input_text="Write a Python function that reverses a string",
        must_contain=["def ", "return", "[::-1]"],
        must_not_contain=["sorry", "i cannot", "as an ai"],
        min_length=50,
    ),
    GoldenCase(
        case_id="summary_001",
        input_text="Summarize in one sentence: Python is a programming language known for readability",
        must_contain=["python"],
        must_not_contain=["</s>", "###", "[INST]"],
        min_length=10,
        max_length=200,
    ),
    GoldenCase(
        case_id="json_extract_001",
        input_text='Extract name and age from: "John is 25 years old". Return JSON.',
        must_contain=['"name"', '"age"'],
        must_not_contain=["cannot", "unable"],
        must_match_json_keys=["name", "age"],
    ),
]

client = anthropic.Anthropic()

def run_pipeline(input_text: str, system_prompt: str) -> str:
    """The multi-agent pipeline under test."""
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system_prompt,
        messages=[{"role": "user", "content": input_text}],
    )
    return r.content[0].text

def evaluate_against_golden(
    system_prompt: str,
    dataset: list[GoldenCase] = GOLDEN_DATASET,
) -> list[GoldenResult]:
    results = []
    for case in dataset:
        output = run_pipeline(case.input_text, system_prompt)
        violations = []

        # Check must_contain
        for phrase in case.must_contain:
            if phrase.lower() not in output.lower():
                violations.append(f"Missing required phrase: {phrase!r}")

        # Check must_not_contain
        for phrase in case.must_not_contain:
            if phrase.lower() in output.lower():
                violations.append(f"Contains forbidden phrase: {phrase!r}")

        # Check length
        if len(output) < case.min_length:
            violations.append(f"Too short: {len(output)} < {case.min_length}")
        if len(output) > case.max_length:
            violations.append(f"Too long: {len(output)} > {case.max_length}")

        # Check JSON keys
        if case.must_match_json_keys:
            try:
                parsed = json.loads(output)
                for key in case.must_match_json_keys:
                    if key not in parsed:
                        violations.append(f"Missing JSON key: {key!r}")
            except json.JSONDecodeError:
                violations.append("Output is not valid JSON")

        results.append(GoldenResult(
            case_id=case.case_id,
            passed=len(violations) == 0,
            violations=violations,
            output_preview=output[:80],
        ))

    passed = sum(1 for r in results if r.passed)
    print(f"\n[GOLDEN EVAL] {passed}/{len(results)} cases passed")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.case_id}")
        for v in r.violations:
            print(f"    VIOLATION: {v}")
    return results


# Evaluate with baseline system prompt
print("=== Baseline system prompt ===")
evaluate_against_golden("You are a helpful assistant.")

# Evaluate with modified system prompt — check for regressions
print("\n=== Modified system prompt ===")
evaluate_against_golden("You are a helpful assistant. Be very brief, one line max.")

# Expected Token Savings: Golden evals run actual pipeline tokens; zero judge LLM overhead for deterministic checks
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Chaos Injection Testing for Agent Handoff Resilience

Inject failures, delays, and malformed outputs at each pipeline stage to verify the orchestrator handles degraded subagents gracefully.

```python
import anthropic
import asyncio
import json
import random
from dataclasses import dataclass
from enum import Enum
from typing import Callable

class ChaosType(Enum):
    NONE = "none"
    SLOW = "slow"           # 2s delay
    EMPTY = "empty"         # empty string
    MALFORMED = "malformed" # invalid JSON when JSON expected
    ERROR = "error"         # raises exception
    TRUNCATED = "truncated" # cuts output at 10 chars

@dataclass
class ChaosInjectionSpec:
    stage_name: str
    chaos_type: ChaosType
    probability: float = 1.0

@dataclass
class ChaosTestResult:
    spec: ChaosInjectionSpec
    pipeline_completed: bool
    final_output: str
    graceful: bool     # True if error was handled, not propagated as raw exception

client = anthropic.AsyncAnthropic()

async def _chaotic_llm_call(prompt: str, chaos: ChaosType) -> str:
    if chaos == ChaosType.SLOW:
        await asyncio.sleep(2.0)
    if chaos == ChaosType.EMPTY:
        return ""
    if chaos == ChaosType.MALFORMED:
        return '{broken json: "missing quote}'
    if chaos == ChaosType.ERROR:
        raise ConnectionError("Simulated subagent failure")
    if chaos == ChaosType.TRUNCATED:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return r.content[0].text[:10]

    r = await client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text

async def resilient_pipeline(task: str, chaos_specs: dict[str, ChaosType]) -> tuple[bool, str]:
    """Pipeline with graceful degradation at each stage."""
    try:
        # Stage 1: Planner
        plan = await asyncio.wait_for(
            _chaotic_llm_call(f"Plan: {task}", chaos_specs.get("planner", ChaosType.NONE)),
            timeout=5.0,
        )
        if not plan or len(plan) < 5:
            plan = f"Default plan: directly complete the task: {task}"

        # Stage 2: Executor
        result = await asyncio.wait_for(
            _chaotic_llm_call(f"Execute based on plan:\n{plan}\nTask: {task}",
                              chaos_specs.get("executor", ChaosType.NONE)),
            timeout=5.0,
        )
        if not result:
            result = f"[DEGRADED] Could not execute fully. Task: {task}"

        return True, result

    except (asyncio.TimeoutError, ConnectionError, Exception) as e:
        return False, f"[GRACEFUL FAILURE] {type(e).__name__}: {e}"

async def run_chaos_suite(task: str, chaos_specs_list: list[dict[str, ChaosType]]) -> list[ChaosTestResult]:
    results = []
    for chaos_specs in chaos_specs_list:
        desc = {k: v.value for k, v in chaos_specs.items() if v != ChaosType.NONE} or {"all": "none"}
        try:
            completed, output = await asyncio.wait_for(
                resilient_pipeline(task, chaos_specs), timeout=10.0
            )
            graceful = completed or output.startswith("[GRACEFUL")
        except Exception as e:
            completed = False
            output = f"UNHANDLED: {e}"
            graceful = False

        spec = ChaosInjectionSpec(
            stage_name=str(desc),
            chaos_type=list(chaos_specs.values())[0] if chaos_specs else ChaosType.NONE,
        )
        result = ChaosTestResult(spec=spec, pipeline_completed=completed,
                                 final_output=output[:80], graceful=graceful)
        status = "GRACEFUL" if graceful else "CRASH"
        print(f"[CHAOS:{status}] {desc} → {output[:60]}")
        results.append(result)

    graceful_count = sum(1 for r in results if r.graceful)
    print(f"\n[CHAOS SUMMARY] {graceful_count}/{len(results)} handled gracefully")
    return results

async def main():
    chaos_test_suite = [
        {},  # baseline — no chaos
        {"planner": ChaosType.SLOW},
        {"planner": ChaosType.EMPTY},
        {"executor": ChaosType.ERROR},
        {"planner": ChaosType.MALFORMED, "executor": ChaosType.EMPTY},
    ]
    await run_chaos_suite("Write a summary of Python features", chaos_test_suite)

asyncio.run(main())

# Expected Token Savings: Chaos injection validates pipeline resilience without full prod traffic; 1 token per chaos branch
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Continuous Eval Pipeline with Drift Detection

Run the eval suite automatically against a sliding window of recent production outputs. Alert when the pass rate drops below a threshold, indicating prompt drift or model behavior change.

```python
import anthropic
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class EvalCase:
    input_text: str
    check: callable  # (output: str) -> bool

@dataclass
class EvalWindow:
    timestamp: float
    total: int
    passed: int

    @property
    def pass_rate(self) -> float:
        return self.passed / max(self.total, 1)

class ContinuousEvalMonitor:
    def __init__(
        self,
        cases: list[EvalCase],
        window_size: int = 20,
        alert_threshold: float = 0.80,
        eval_every_n_calls: int = 5,
    ):
        self.cases = cases
        self.window: deque[EvalWindow] = deque(maxlen=window_size)
        self.alert_threshold = alert_threshold
        self.eval_every_n = eval_every_n_calls
        self.call_count = 0
        self.client = anthropic.Anthropic()

    def _run_eval_round(self, system_prompt: str) -> EvalWindow:
        passed = 0
        for case in self.cases:
            r = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=system_prompt,
                messages=[{"role": "user", "content": case.input_text}],
            )
            output = r.content[0].text
            if case.check(output):
                passed += 1
        window = EvalWindow(timestamp=time.time(), total=len(self.cases), passed=passed)
        self.window.append(window)
        return window

    def _rolling_pass_rate(self) -> float:
        if not self.window:
            return 1.0
        total = sum(w.total for w in self.window)
        passed = sum(w.passed for w in self.window)
        return passed / max(total, 1)

    def on_call(self, system_prompt: str) -> bool:
        """Call after each production request. Returns True if eval triggered and passed."""
        self.call_count += 1
        if self.call_count % self.eval_every_n != 0:
            return True
        window = self._run_eval_round(system_prompt)
        rolling = self._rolling_pass_rate()
        print(
            f"[CONTINUOUS EVAL] round pass={window.pass_rate:.0%} "
            f"rolling={rolling:.0%} calls={self.call_count}"
        )
        if rolling < self.alert_threshold:
            print(
                f"[DRIFT ALERT] Rolling pass rate {rolling:.0%} < "
                f"threshold {self.alert_threshold:.0%} — prompt may have drifted"
            )
            return False
        return True


eval_cases = [
    EvalCase("What is 2+2?", lambda o: "4" in o),
    EvalCase("Say only the word 'hello'", lambda o: "hello" in o.lower() and len(o) < 30),
    EvalCase("List exactly 3 colors", lambda o: len([w for w in ["red", "blue", "green",
             "yellow", "purple", "orange"] if w in o.lower()]) >= 2),
]

monitor = ContinuousEvalMonitor(eval_cases, eval_every_n_calls=3, alert_threshold=0.70)
client = anthropic.Anthropic()

# Simulate production calls with monitoring
for i in range(15):
    # Production call
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system="You are a helpful assistant.",
        messages=[{"role": "user", "content": f"Production query {i}"}],
    )
    # Monitor eval (every 3rd call)
    ok = monitor.on_call("You are a helpful assistant.")

# Expected Token Savings: Eval runs every N calls, not every call; rolling window catches drift without eval storms
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Test Type | LLM Overhead | Coverage | Best For |
|--------|----------|-------------|----------|----------|
| 1. Sequential Assertions | Integration + step checks | None (assertions) | Per-step | Step-by-step pipeline validation |
| 2. LLM-as-Judge | Quality eval | ~200 tok/case | Holistic | Subjective quality measurement |
| 3. Deterministic Replay | Unit with mocks | 0 (mocked) | Orchestrator logic | Rapid iteration, CI/CD |
| 4. Golden Dataset | Regression | None (rule-based) | Output format/content | Preventing known regressions |
| 5. Chaos Injection | Resilience | Minimal | Error handling paths | Fault tolerance validation |
| 6. Continuous Monitor | Production drift | Every N calls | Long-term quality | Production health monitoring |

**Recommended**: Option 3 (replay/mocks) in CI for speed, Option 4 (golden dataset) for regression prevention, and Option 6 (continuous) in production for drift detection.
