---
layout: solution
title: "Agent Doesn't Implement Regression Test Generation from Production Failures"
category: testing
description: "Automatically capture real production failures and convert them into permanent regression tests — so every bug fixed stays fixed."
tags: [testing, regression, production, failure-capture, eval, tdd, quality]
---

## Problem

Production failures are valuable learning artifacts. An agent mishandles a specific input in production; an engineer fixes it; six weeks later a prompt change causes the exact same failure to return. Without capturing production failures as regression tests, teams are perpetually surprised by the same bugs. Each incident is fixed in isolation with no structural memory of what broke.

```python
# Naive: fix the bug, move on — no test captures the failure
def handle_request(user_input: str) -> str:
    return agent.respond(user_input)  # if this fails, the failure disappears into logs
```

## Solution Options

### Option 1: Failure Capture Decorator with Automatic Test File Generation

Wrap any agent function with a decorator that captures failed calls and writes them to a test fixture file.

```python
import anthropic
import json
import os
import time
from dataclasses import dataclass, asdict
from functools import wraps
from pathlib import Path

@dataclass
class FailureRecord:
    captured_at: float
    function_name: str
    input_args: list
    input_kwargs: dict
    error_type: str
    error_message: str
    traceback_hint: str

FAILURE_LOG_PATH = Path("tests/fixtures/production_failures.jsonl")
FAILURE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

def capture_failures(func):
    """Decorator: logs any exception as a structured failure record."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            import traceback
            record = FailureRecord(
                captured_at=time.time(),
                function_name=func.__name__,
                input_args=[str(a) for a in args],
                input_kwargs={k: str(v) for k, v in kwargs.items()},
                error_type=type(e).__name__,
                error_message=str(e),
                traceback_hint=traceback.format_exc().splitlines()[-1],
            )
            with open(FAILURE_LOG_PATH, "a") as f:
                f.write(json.dumps(asdict(record)) + "\n")
            print(f"[FAILURE CAPTURED] {func.__name__}: {type(e).__name__}: {e}")
            raise
    return wrapper


def generate_regression_tests_from_log(log_path: Path = FAILURE_LOG_PATH) -> str:
    """Read the failure log and emit a pytest file with one test per failure."""
    client = anthropic.Anthropic()
    if not log_path.exists():
        return "# No failures captured yet"
    records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    if not records:
        return "# No failures captured yet"

    failures_text = json.dumps(records[:20], indent=2)  # cap at 20 for prompt size
    prompt = f"""Generate a pytest regression test file for these captured production failures.
For each failure, write a test that:
1. Reproduces the exact input that caused the failure
2. Asserts the function no longer raises that error
3. Includes a comment with the original error message
4. Uses descriptive test names

Captured failures:
{failures_text}

Write complete Python pytest code only. Use mock where necessary. Import the function from 'agent_core'."""

    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


# Example agent function with failure capture
client = anthropic.Anthropic()

@capture_failures
def process_user_query(query: str, user_id: str) -> str:
    if not query.strip():
        raise ValueError("Empty query not allowed")
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": query}],
    )
    return r.content[0].text


# Simulate a production failure
try:
    process_user_query("", user_id="user_123")   # will fail and be captured
except ValueError:
    pass

# Generate regression tests from captured failures
test_code = generate_regression_tests_from_log()
print("Generated regression test:\n")
print(test_code[:800])

# Expected Token Savings: Failure capture is zero tokens; test generation ~800 tokens per batch of failures
# Environment: ANTHROPIC_API_KEY
```

### Option 2: LLM-Powered Failure Analysis with Test Case Synthesis

When a production error occurs, use an LLM to analyze the root cause and generate a minimal reproduction test case plus a fix suggestion.

```python
import anthropic
import json
import traceback
from dataclasses import dataclass
from pathlib import Path

@dataclass
class FailureAnalysis:
    root_cause: str
    minimal_reproduction: str     # Python code snippet
    suggested_fix: str
    test_case_code: str           # Complete pytest test

client = anthropic.Anthropic()

ANALYSIS_PROMPT = """A production agent failure occurred. Analyze it and generate a regression test.

Function name: {function_name}
Input: {input_repr}
Error type: {error_type}
Error message: {error_message}
Traceback:
{traceback_text}

Return JSON:
{{
  "root_cause": "<one-sentence explanation>",
  "minimal_reproduction": "<minimal Python code that reproduces this>",
  "suggested_fix": "<one-sentence fix recommendation>",
  "test_case_code": "<complete pytest test function that would catch this regression>"
}}"""

def analyze_failure(
    function_name: str,
    input_repr: str,
    error: Exception,
    tb_text: str,
) -> FailureAnalysis:
    prompt = ANALYSIS_PROMPT.format(
        function_name=function_name,
        input_repr=input_repr,
        error_type=type(error).__name__,
        error_message=str(error),
        traceback_text=tb_text,
    )
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    data = json.loads(r.content[0].text)
    return FailureAnalysis(**data)

def append_to_regression_suite(analysis: FailureAnalysis, output_path: str = "tests/test_regression.py") -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    separator = f"\n# --- Auto-generated regression test ---\n# Root cause: {analysis.root_cause}\n"
    with open(output_path, "a") as f:
        f.write(separator + analysis.test_case_code + "\n")
    print(f"[REGRESSION] Test appended to {output_path}")
    print(f"[REGRESSION] Root cause: {analysis.root_cause}")
    print(f"[REGRESSION] Fix suggestion: {analysis.suggested_fix}")


def agent_with_regression_capture(messages: list[dict], system: str = "") -> str:
    try:
        kwargs = dict(model="claude-haiku-4-5-20251001", max_tokens=512, messages=messages)
        if system:
            kwargs["system"] = system
        r = client.messages.create(**kwargs)
        return r.content[0].text
    except Exception as e:
        tb_text = traceback.format_exc()
        analysis = analyze_failure(
            function_name="agent_with_regression_capture",
            input_repr=repr(messages[:2]),
            error=e,
            tb_text=tb_text,
        )
        append_to_regression_suite(analysis)
        raise


# Simulate invoking the agent normally
try:
    result = agent_with_regression_capture(
        messages=[{"role": "user", "content": "What is 2+2?"}]
    )
    print(f"Success: {result[:100]}")
except Exception as e:
    print(f"Caught: {e}")

# Expected Token Savings: Analysis uses ~600 tokens per failure; worth it to avoid recurring prod incidents
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Traffic Mirroring with Divergence Detection

Mirror a sample of production traffic to a new model/prompt version. When outputs diverge significantly, record the input as a regression candidate.

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

@dataclass
class DivergenceRecord:
    input_text: str
    baseline_output: str
    candidate_output: str
    divergence_score: float
    divergence_reason: str

client = anthropic.AsyncAnthropic()
DIVERGENCE_LOG = Path("tests/fixtures/divergences.jsonl")
DIVERGENCE_LOG.parent.mkdir(parents=True, exist_ok=True)

def _word_overlap(a: str, b: str) -> float:
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

async def _judge_divergence(user_input: str, baseline: str, candidate: str) -> tuple[float, str]:
    prompt = (
        f"User input: {user_input}\n\n"
        f"Response A: {baseline}\n\nResponse B: {candidate}\n\n"
        "Rate divergence 0.0–1.0 and explain why they differ.\n"
        'Return JSON: {"divergence_score": <float>, "reason": "<one sentence>"}'
    )
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        data = json.loads(r.content[0].text)
        return float(data["divergence_score"]), data["reason"]
    except Exception:
        score = 1.0 - _word_overlap(baseline, candidate)
        return score, "heuristic: low word overlap"

async def mirror_and_detect(
    user_input: str,
    baseline_system: str,
    candidate_system: str,
    divergence_threshold: float = 0.4,
) -> str:
    """Run both versions, return baseline output, capture divergence if significant."""
    baseline_r, candidate_r = await asyncio.gather(
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=baseline_system,
            messages=[{"role": "user", "content": user_input}],
        ),
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=candidate_system,
            messages=[{"role": "user", "content": user_input}],
        ),
    )
    baseline_out = baseline_r.content[0].text
    candidate_out = candidate_r.content[0].text

    score, reason = await _judge_divergence(user_input, baseline_out, candidate_out)
    if score > divergence_threshold:
        record = DivergenceRecord(
            input_text=user_input,
            baseline_output=baseline_out,
            candidate_output=candidate_out,
            divergence_score=score,
            divergence_reason=reason,
        )
        with open(DIVERGENCE_LOG, "a") as f:
            f.write(json.dumps({
                "input": record.input_text,
                "baseline": record.baseline_output[:200],
                "candidate": record.candidate_output[:200],
                "score": record.divergence_score,
                "reason": record.divergence_reason,
            }) + "\n")
        print(f"[DIVERGENCE CAPTURED] score={score:.2f}: {reason}")

    return baseline_out  # always serve baseline in production

async def main():
    baseline_sys = "You are a helpful assistant. Always answer in 2-3 sentences."
    candidate_sys = "You are a concise assistant."

    queries = [
        "Explain what a neural network is",
        "What is the capital of France?",
        "How does garbage collection work in Python?",
    ]
    for q in queries:
        result = await mirror_and_detect(q, baseline_sys, candidate_sys)
        print(f"Q: {q}\nA: {result[:120]}\n")

asyncio.run(main())

# Expected Token Savings: 2× tokens per mirrored request (sample only, not all traffic); divergence log feeds test suite
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Property-Based Failure Recording with Auto-Minimization

When a production failure occurs, automatically minimize the input to find the smallest reproducing case, then save it as a golden test.

```python
import anthropic
import json
import time
from dataclasses import dataclass
from pathlib import Path

@dataclass
class MinimizedFailure:
    original_input: str
    minimized_input: str
    error_type: str
    error_message: str
    captured_at: float

MINIMIZED_FAILURES_PATH = Path("tests/fixtures/minimized_failures.json")

client = anthropic.Anthropic()

def _try_input(func, input_text: str) -> Exception | None:
    try:
        func(input_text)
        return None
    except Exception as e:
        return e

def minimize_failure(func, original_input: str, original_error: Exception) -> str:
    """Binary-search-style minimization: find shortest prefix that still fails."""
    minimized = original_input
    step = len(original_input) // 2
    while step > 10:
        candidate = minimized[:len(minimized) - step]
        err = _try_input(func, candidate)
        if err and type(err).__name__ == type(original_error).__name__:
            minimized = candidate
        step = step // 2
    # Try word-level removal
    words = minimized.split()
    for i in range(len(words) - 1, 0, -1):
        candidate = " ".join(words[:i])
        err = _try_input(func, candidate)
        if err and type(err).__name__ == type(original_error).__name__:
            minimized = candidate
            words = minimized.split()
    return minimized

def record_minimized_failure(
    func, original_input: str, error: Exception, storage_path: Path = MINIMIZED_FAILURES_PATH
) -> MinimizedFailure:
    print(f"[MINIMIZE] Finding minimal reproduction for {type(error).__name__}...")
    minimized = minimize_failure(func, original_input, error)
    record = MinimizedFailure(
        original_input=original_input,
        minimized_input=minimized,
        error_type=type(error).__name__,
        error_message=str(error),
        captured_at=time.time(),
    )
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(storage_path.read_text()) if storage_path.exists() else []
    existing.append({
        "original_input": record.original_input,
        "minimized_input": record.minimized_input,
        "error_type": record.error_type,
        "error_message": record.error_message,
        "captured_at": record.captured_at,
    })
    storage_path.write_text(json.dumps(existing, indent=2))
    print(f"[MINIMIZE] Original: {len(original_input)} chars → Minimized: {len(minimized)} chars")
    return record

def generate_tests_from_minimized(storage_path: Path = MINIMIZED_FAILURES_PATH) -> str:
    if not storage_path.exists():
        return ""
    records = json.loads(storage_path.read_text())
    lines = ["import pytest", "from agent_core import agent_respond", ""]
    for i, r in enumerate(records):
        safe_name = r["minimized_input"][:30].replace(" ", "_").replace('"', "").replace("'", "")
        lines.append(f"def test_regression_{i:03d}_{safe_name}():")
        lines.append(f'    # Original error: {r["error_type"]}: {r["error_message"][:60]}')
        lines.append(f'    # Minimized from {len(r["original_input"])} → {len(r["minimized_input"])} chars')
        lines.append(f'    result = agent_respond({r["minimized_input"]!r})')
        lines.append(f"    assert result is not None")
        lines.append("")
    return "\n".join(lines)


# Example agent function
def agent_respond(text: str) -> str:
    if len(text) > 500:
        raise ValueError(f"Input too long: {len(text)} chars (max 500)")
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": text}],
    )
    return r.content[0].text


# Simulate a production failure with a long input
long_input = "Please explain in detail " + "the theory of relativity " * 25
try:
    agent_respond(long_input)
except ValueError as e:
    record = record_minimized_failure(agent_respond, long_input, e)
    print(f"Minimized input: {record.minimized_input!r}")

print("\nGenerated tests:")
print(generate_tests_from_minimized()[:600])

# Expected Token Savings: Minimization is code-only (no API calls); test generation ~400 tokens per batch
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Structured Eval Dataset Builder from Production Logs

Parse structured production logs to extract input-output pairs, score them with an LLM judge, and build a curated eval dataset for regression testing.

```python
import anthropic
import json
import time
from dataclasses import dataclass
from pathlib import Path

@dataclass
class EvalCase:
    input_text: str
    expected_properties: list[str]   # e.g. ["mentions Python", "under 200 words"]
    failure_label: str               # what went wrong in production
    severity: str                    # "critical" | "major" | "minor"

client = anthropic.Anthropic()

EVAL_DATASET_PATH = Path("tests/evals/production_regression.jsonl")
EVAL_DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)

BUILD_EVAL_PROMPT = """A production failure was observed. Create an eval test case for it.

User input: {user_input}
Agent output (that was bad): {bad_output}
Failure description: {failure_description}

Return JSON:
{{
  "expected_properties": ["<property 1>", "<property 2>", "<property 3>"],
  "failure_label": "<short label for what went wrong>",
  "severity": "critical" | "major" | "minor"
}}

Properties should be concrete, checkable assertions about what a GOOD response should contain."""

def build_eval_case(user_input: str, bad_output: str, failure_description: str) -> EvalCase:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": BUILD_EVAL_PROMPT.format(
            user_input=user_input,
            bad_output=bad_output,
            failure_description=failure_description,
        )}],
    )
    data = json.loads(r.content[0].text)
    return EvalCase(
        input_text=user_input,
        expected_properties=data["expected_properties"],
        failure_label=data["failure_label"],
        severity=data["severity"],
    )

def save_eval_case(case: EvalCase) -> None:
    with open(EVAL_DATASET_PATH, "a") as f:
        f.write(json.dumps({
            "input": case.input_text,
            "expected_properties": case.expected_properties,
            "failure_label": case.failure_label,
            "severity": case.severity,
        }) + "\n")
    print(f"[EVAL] Saved: [{case.severity}] {case.failure_label}")

def run_regression_eval(agent_func, dataset_path: Path = EVAL_DATASET_PATH) -> dict:
    if not dataset_path.exists():
        return {"total": 0, "passed": 0, "failed": 0}
    cases = [json.loads(line) for line in dataset_path.read_text().splitlines() if line.strip()]
    results = {"total": len(cases), "passed": 0, "failed": 0, "failures": []}
    for case in cases:
        output = agent_func(case["input"])
        checks = []
        for prop in case["expected_properties"]:
            check_r = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content":
                    f'Does this text satisfy: "{prop}"?\nText: {output}\nReturn JSON: {{"satisfied": true/false}}'}],
            )
            satisfied = json.loads(check_r.content[0].text)["satisfied"]
            checks.append((prop, satisfied))
        if all(s for _, s in checks):
            results["passed"] += 1
        else:
            results["failed"] += 1
            failed_checks = [p for p, s in checks if not s]
            results["failures"].append({"label": case["failure_label"], "failed_checks": failed_checks})
    return results


# Example: capture a production failure as eval case
def my_agent(text: str) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": text}],
    )
    return r.content[0].text

# Simulate capturing a bad response observed in production
build_eval_case_result = build_eval_case(
    user_input="How do I sort a list in Python?",
    bad_output="You can sort things.",
    failure_description="Response too vague, did not show actual sort() syntax",
)
save_eval_case(build_eval_case_result)

# Run regression eval
results = run_regression_eval(my_agent)
print(f"\nEval results: {results['passed']}/{results['total']} passed")

# Expected Token Savings: Judge checks ~64 tokens each; curated evals catch regressions before prod deployment
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Async Failure Pipeline with Deduplication and Priority Ranking

High-volume production systems need an async pipeline that deduplicates similar failures, ranks by impact, and generates tests only for unique high-priority failures.

```python
import anthropic
import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class RawFailure:
    input_text: str
    error_type: str
    error_message: str
    timestamp: float
    count: int = 1

@dataclass
class RegressionTest:
    input_text: str
    error_type: str
    test_code: str
    priority: str   # "P0" | "P1" | "P2"
    dedup_key: str

client = anthropic.AsyncAnthropic()
REGRESSION_OUTPUT = Path("tests/test_production_regressions.py")

def _dedup_key(failure: RawFailure) -> str:
    """Failures with same error type + first 50 chars of message are duplicates."""
    return f"{failure.error_type}:{failure.error_message[:50]}"

async def _generate_test(failure: RawFailure, priority: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content":
            f"Write a single pytest test that would catch this regression:\n"
            f"Input: {failure.input_text!r}\n"
            f"Error: {failure.error_type}: {failure.error_message}\n"
            f"Priority: {priority}\n"
            f"The function under test is called 'agent_respond' from module 'agent_core'.\n"
            f"Return only the test function code."}],
    )
    return r.content[0].text

async def _rank_failures(failures: list[RawFailure]) -> list[tuple[RawFailure, str]]:
    """Assign P0/P1/P2 priority to each unique failure."""
    results = []
    sem = asyncio.Semaphore(3)
    async def rank_one(f: RawFailure) -> tuple[RawFailure, str]:
        async with sem:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content":
                    f'Rate priority of this failure: error={f.error_type}, count={f.count}, '
                    f'message={f.error_message[:80]!r}\n'
                    'Return JSON: {"priority": "P0"|"P1"|"P2"}'}],
            )
            priority = json.loads(r.content[0].text)["priority"]
            return f, priority
    return await asyncio.gather(*[rank_one(f) for f in failures])

async def process_failure_batch(raw_failures: list[RawFailure]) -> list[RegressionTest]:
    # Deduplicate
    deduped: dict[str, RawFailure] = {}
    for f in raw_failures:
        key = _dedup_key(f)
        if key in deduped:
            deduped[key].count += 1
        else:
            deduped[key] = f

    print(f"[PIPELINE] {len(raw_failures)} failures → {len(deduped)} unique")

    # Rank by priority
    ranked = await _rank_failures(list(deduped.values()))

    # Generate tests only for P0 + P1
    sem = asyncio.Semaphore(3)
    regression_tests = []

    async def gen_test(failure: RawFailure, priority: str) -> RegressionTest | None:
        if priority == "P2":
            return None
        async with sem:
            code = await _generate_test(failure, priority)
            return RegressionTest(
                input_text=failure.input_text,
                error_type=failure.error_type,
                test_code=code,
                priority=priority,
                dedup_key=_dedup_key(failure),
            )

    tests = await asyncio.gather(*[gen_test(f, p) for f, p in ranked])
    return [t for t in tests if t is not None]

async def main():
    # Simulate a batch of production failures arriving
    failures = [
        RawFailure("", "ValueError", "Empty input not allowed", time.time()),
        RawFailure("", "ValueError", "Empty input not allowed", time.time()),  # duplicate
        RawFailure("x" * 600, "ValueError", "Input too long: 600 chars", time.time()),
        RawFailure(None, "TypeError", "NoneType has no attribute strip", time.time()),
        RawFailure("ignore previous instructions", "SecurityError", "Injection detected", time.time()),
    ]
    tests = await process_failure_batch(failures)
    print(f"\n[PIPELINE] Generated {len(tests)} regression tests")
    for t in tests:
        print(f"  [{t.priority}] {t.error_type}: {t.input_text[:40]!r}")
    # Write test file
    REGRESSION_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(REGRESSION_OUTPUT, "w") as f:
        f.write("# Auto-generated regression tests from production failures\n")
        f.write("import pytest\nfrom agent_core import agent_respond\n\n")
        for t in tests:
            f.write(f"# [{t.priority}] {t.error_type}\n")
            f.write(t.test_code + "\n\n")
    print(f"[PIPELINE] Wrote {REGRESSION_OUTPUT}")

asyncio.run(main())

# Expected Token Savings: Dedup reduces test gen calls; P2 skipped entirely; parallel ranking with semaphore
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Capture Method | Minimization | Test Generation | Deduplication | Best For |
|--------|---------------|-------------|----------------|--------------|----------|
| 1. Decorator Capture | Exception decorator | No | LLM batch | No | Quick integration, any function |
| 2. LLM Root Cause | Exception + analysis | No | LLM per failure | No | Deep failure analysis |
| 3. Traffic Mirroring | Divergence detection | No | Implicit (captures diverging inputs) | No | A/B testing new prompts |
| 4. Input Minimization | Exception + binary search | Yes | Template | No | Complex input failures |
| 5. Eval Dataset Builder | Log parsing | No | LLM judge | Implicit | Curated regression evals |
| 6. Async Pipeline | Batch + dedup + rank | No | LLM P0+P1 only | Yes | High-volume production |

**Recommended**: Option 1 + 6 together — Option 1 as always-on capture decorator, Option 6 as nightly batch processor that deduplicates and ranks captured failures.
