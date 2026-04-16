---
title: "Agent Doesn't Implement Evaluation Harness for Streaming Responses"
description: "How to evaluate streaming LLM responses for correctness, latency, completeness, and consistency—without waiting for the full response to complete."
categories: [testing]
difficulty: intermediate
---

Streaming responses present unique evaluation challenges: the final answer may be correct but arrive with high time-to-first-token, intermediate chunks may be inconsistent with the final answer, or the stream may terminate prematurely. A streaming evaluation harness captures all these dimensions.

## Solution 1: Stream Timing Harness

Measure time-to-first-token, inter-chunk latency, and total completion time.

```python
import asyncio
import time
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class StreamTimingResult:
    prompt: str
    time_to_first_token_ms: float | None = None
    avg_inter_chunk_ms: float | None = None
    total_time_ms: float | None = None
    chunk_count: int = 0
    total_chars: int = 0
    complete: bool = False
    chunk_timestamps: list[float] = field(default_factory=list)

    @property
    def chars_per_second(self) -> float:
        if not self.total_time_ms:
            return 0.0
        return (self.total_chars / self.total_time_ms) * 1000

    @property
    def avg_inter_chunk_latency(self) -> float:
        if len(self.chunk_timestamps) < 2:
            return 0.0
        gaps = [self.chunk_timestamps[i+1] - self.chunk_timestamps[i]
                for i in range(len(self.chunk_timestamps) - 1)]
        return (sum(gaps) / len(gaps)) * 1000  # ms


async def time_stream(prompt: str, model: str = "claude-haiku-4-5-20251001") -> StreamTimingResult:
    result = StreamTimingResult(prompt=prompt)
    start = time.monotonic()

    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            now = time.monotonic()
            if result.time_to_first_token_ms is None:
                result.time_to_first_token_ms = (now - start) * 1000
            result.chunk_timestamps.append(now)
            result.chunk_count += 1
            result.total_chars += len(text)

    result.total_time_ms = (time.monotonic() - start) * 1000
    result.complete = True
    return result


def assert_timing(result: StreamTimingResult, max_ttft_ms: float = 2000, max_total_ms: float = 30000):
    assert result.complete, "Stream did not complete"
    assert result.time_to_first_token_ms is not None, "No tokens received"
    assert result.time_to_first_token_ms <= max_ttft_ms, \
        f"TTFT {result.time_to_first_token_ms:.0f}ms exceeds {max_ttft_ms}ms"
    assert result.total_time_ms <= max_total_ms, \
        f"Total time {result.total_time_ms:.0f}ms exceeds {max_total_ms}ms"


async def main():
    prompts = [
        "What is 2+2?",
        "Explain the TCP handshake in 3 sentences.",
        "List 5 Python best practices.",
    ]

    results = await asyncio.gather(*[time_stream(p) for p in prompts])

    print(f"{'Prompt':<40} {'TTFT':>8} {'Total':>8} {'Chunks':>7} {'Chars/s':>8}")
    print("-" * 75)
    for r in results:
        try:
            assert_timing(r)
            status = "PASS"
        except AssertionError as e:
            status = f"FAIL: {e}"
        print(f"{r.prompt[:38]:<40} {r.time_to_first_token_ms:>7.0f}ms "
              f"{r.total_time_ms:>7.0f}ms {r.chunk_count:>7} {r.chars_per_second:>7.0f}  [{status}]")


asyncio.run(main())
```

## Solution 2: Incremental Content Validator

Validate stream chunks as they arrive, detecting format violations before the stream completes.

```python
import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class IncrementalValidationResult:
    prompt: str
    violations: list[str] = field(default_factory=list)
    full_text: str = ""
    complete: bool = False

    @property
    def passed(self) -> bool:
        return self.complete and not self.violations


IncrementalCheck = Callable[[str, str], str | None]  # (chunk, accumulated) -> error or None


def check_no_apology(chunk: str, accumulated: str) -> str | None:
    if len(accumulated) < 50 and re.search(r"i apologize|i'm sorry", accumulated.lower()):
        return "Response starts with an apology"
    return None


def check_no_placeholder(chunk: str, accumulated: str) -> str | None:
    if re.search(r"\[your\s|insert\s", accumulated.lower()):
        return "Response contains unfilled placeholders"
    return None


def check_markdown_balanced(chunk: str, accumulated: str) -> str | None:
    # Check triple backtick balance (simple heuristic)
    count = accumulated.count("```")
    if count > 0 and count % 2 != 0 and len(accumulated) > 500:
        # Might be unclosed code block (warn only after substantial content)
        return None  # Not a hard error mid-stream; check at end
    return None


INCREMENTAL_CHECKS: list[IncrementalCheck] = [
    check_no_apology,
    check_no_placeholder,
    check_markdown_balanced,
]


def check_final(full_text: str) -> list[str]:
    errors = []
    if full_text.count("```") % 2 != 0:
        errors.append("Unclosed code block in response")
    if len(full_text.strip()) < 10:
        errors.append("Response too short (< 10 chars)")
    return errors


async def validate_stream(
    prompt: str,
    checks: list[IncrementalCheck] = INCREMENTAL_CHECKS,
    model: str = "claude-haiku-4-5-20251001",
) -> IncrementalValidationResult:
    result = IncrementalValidationResult(prompt=prompt)
    accumulated = ""

    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for chunk in stream.text_stream:
            accumulated += chunk
            for check in checks:
                error = check(chunk, accumulated)
                if error and error not in result.violations:
                    result.violations.append(error)

    result.full_text = accumulated
    result.violations.extend(check_final(accumulated))
    result.complete = True
    return result


async def main():
    test_cases = [
        "What is machine learning?",
        "Write a Python function to add two numbers",
        "List 3 benefits of exercise",
    ]

    results = await asyncio.gather(*[validate_stream(p) for p in test_cases])
    for r in results:
        status = "PASS" if r.passed else f"FAIL: {r.violations}"
        print(f"[{status}] {r.prompt}")


asyncio.run(main())
```

## Solution 3: Stream Completeness Checker

Verify that the stream actually completed fully (not truncated mid-sentence) and delivered the expected structure.

```python
import asyncio
import re
from dataclasses import dataclass
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class CompletenessResult:
    prompt: str
    full_text: str
    stop_reason: str
    complete_sentence: bool
    expected_sections_found: list[str]
    expected_sections_missing: list[str]

    @property
    def passed(self) -> bool:
        return (
            self.stop_reason == "end_turn"
            and self.complete_sentence
            and not self.expected_sections_missing
        )


def ends_complete_sentence(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return stripped[-1] in ".!?)]}"


def find_sections(text: str, expected: list[str]) -> tuple[list[str], list[str]]:
    found, missing = [], []
    for section in expected:
        if re.search(re.escape(section), text, re.IGNORECASE):
            found.append(section)
        else:
            missing.append(section)
    return found, missing


async def check_stream_completeness(
    prompt: str,
    expected_sections: list[str] | None = None,
    model: str = "claude-haiku-4-5-20251001",
) -> CompletenessResult:
    full_text = ""
    stop_reason = "unknown"

    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            full_text += text
        final = await stream.get_final_message()
        stop_reason = final.stop_reason

    found, missing = find_sections(full_text, expected_sections or [])
    return CompletenessResult(
        prompt=prompt,
        full_text=full_text,
        stop_reason=stop_reason,
        complete_sentence=ends_complete_sentence(full_text),
        expected_sections_found=found,
        expected_sections_missing=missing,
    )


async def main():
    test_cases = [
        (
            "Explain TCP in exactly three sections: Overview, Handshake, Use Cases",
            ["Overview", "Handshake", "Use Cases"],
        ),
        ("What is 2+2?", []),
        ("List the top 5 Python web frameworks with a brief description of each", []),
    ]

    results = await asyncio.gather(*[
        check_stream_completeness(p, sections) for p, sections in test_cases
    ])

    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.prompt[:60]}")
        print(f"  stop_reason={r.stop_reason} complete_sentence={r.complete_sentence}")
        if r.expected_sections_missing:
            print(f"  missing sections: {r.expected_sections_missing}")


asyncio.run(main())
```

## Solution 4: Multi-Run Consistency Evaluator

Run the same prompt multiple times streaming and measure consistency of the streamed outputs.

```python
import asyncio
from difflib import SequenceMatcher
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class ConsistencyResult:
    prompt: str
    runs: int
    responses: list[str] = field(default_factory=list)
    pairwise_similarities: list[float] = field(default_factory=list)

    @property
    def mean_similarity(self) -> float:
        if not self.pairwise_similarities:
            return 0.0
        return sum(self.pairwise_similarities) / len(self.pairwise_similarities)

    @property
    def is_consistent(self) -> bool:
        return self.mean_similarity >= 0.70


async def stream_collect(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    parts = []
    async with client.messages.stream(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            parts.append(text)
    return "".join(parts)


async def evaluate_consistency(prompt: str, runs: int = 3) -> ConsistencyResult:
    result = ConsistencyResult(prompt=prompt, runs=runs)
    result.responses = list(await asyncio.gather(*[stream_collect(prompt) for _ in range(runs)]))

    for i in range(len(result.responses)):
        for j in range(i + 1, len(result.responses)):
            sim = SequenceMatcher(None, result.responses[i], result.responses[j]).ratio()
            result.pairwise_similarities.append(sim)

    return result


async def main():
    prompts = [
        "What is the capital of France?",       # Highly deterministic
        "Write a haiku about programming.",     # Creative — lower consistency expected
        "What are the 4 pillars of OOP?",       # Factual — should be consistent
    ]

    results = await asyncio.gather(*[evaluate_consistency(p, runs=3) for p in prompts])
    for r in results:
        status = "CONSISTENT" if r.is_consistent else "INCONSISTENT"
        print(f"[{status}] {r.prompt}")
        print(f"  mean_similarity={r.mean_similarity:.2f} across {r.runs} runs")


asyncio.run(main())
```

## Solution 5: Structured Output Stream Validator

When expecting structured output (JSON, YAML, numbered list), validate that the streaming response builds toward the correct structure.

```python
import asyncio
import json
import re
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class StructuredStreamResult:
    prompt: str
    expected_format: str  # "json" | "numbered_list" | "yaml"
    raw_text: str = ""
    parsed_successfully: bool = False
    parse_error: str | None = None
    item_count: int | None = None


def extract_json(text: str) -> tuple[bool, str | None, dict | list | None]:
    # Try to find JSON block in the text
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    raw = match.group(1) if match else text.strip()
    try:
        data = json.loads(raw)
        return True, None, data
    except json.JSONDecodeError as e:
        return False, str(e), None


def extract_numbered_list(text: str) -> tuple[bool, str | None, list[str] | None]:
    items = re.findall(r"^\s*\d+[\.\)]\s+(.+)", text, re.MULTILINE)
    if items:
        return True, None, items
    return False, "No numbered list items found", None


async def validate_structured_stream(
    prompt: str,
    expected_format: str,
    model: str = "claude-haiku-4-5-20251001",
) -> StructuredStreamResult:
    result = StructuredStreamResult(prompt=prompt, expected_format=expected_format)
    parts = []

    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            parts.append(text)

    result.raw_text = "".join(parts)

    if expected_format == "json":
        ok, err, data = extract_json(result.raw_text)
        result.parsed_successfully = ok
        result.parse_error = err
        if ok and isinstance(data, list):
            result.item_count = len(data)
        elif ok and isinstance(data, dict):
            result.item_count = len(data)

    elif expected_format == "numbered_list":
        ok, err, items = extract_numbered_list(result.raw_text)
        result.parsed_successfully = ok
        result.parse_error = err
        result.item_count = len(items) if items else 0

    return result


async def main():
    test_cases = [
        (
            'Return a JSON array of 3 programming languages with "name" and "year" fields.',
            "json",
        ),
        (
            "List 5 best practices for writing clean code. Use numbered format.",
            "numbered_list",
        ),
    ]

    results = await asyncio.gather(*[
        validate_structured_stream(p, fmt) for p, fmt in test_cases
    ])

    for r in results:
        status = "PASS" if r.parsed_successfully else f"FAIL: {r.parse_error}"
        print(f"[{status}] {r.prompt[:60]}")
        if r.item_count is not None:
            print(f"  items found: {r.item_count}")


asyncio.run(main())
```

## Solution 6: Full Streaming Eval Harness

Combine timing, completeness, consistency, and content validation into a single composable harness.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class EvalCase:
    name: str
    prompt: str
    max_ttft_ms: float = 3000
    min_chars: int = 20
    required_keywords: list[str] = field(default_factory=list)
    runs: int = 1


@dataclass
class EvalReport:
    case_name: str
    passed: bool
    ttft_ms: float | None
    total_ms: float | None
    chars: int
    failures: list[str] = field(default_factory=list)


async def run_eval_case(case: EvalCase) -> EvalReport:
    failures = []
    all_texts = []

    for run in range(case.runs):
        parts = []
        ttft = None
        start = time.monotonic()

        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": case.prompt}],
        ) as stream:
            async for text in stream.text_stream:
                if ttft is None:
                    ttft = (time.monotonic() - start) * 1000
                parts.append(text)

        total_ms = (time.monotonic() - start) * 1000
        full = "".join(parts)
        all_texts.append(full)

    # Use first run for timing
    ttft = ttft
    chars = len(all_texts[0]) if all_texts else 0
    full = all_texts[0] if all_texts else ""

    if ttft and ttft > case.max_ttft_ms:
        failures.append(f"TTFT {ttft:.0f}ms > {case.max_ttft_ms}ms")
    if chars < case.min_chars:
        failures.append(f"Response too short ({chars} chars < {case.min_chars})")
    for kw in case.required_keywords:
        if kw.lower() not in full.lower():
            failures.append(f"Missing keyword: '{kw}'")

    return EvalReport(
        case_name=case.case_name if hasattr(case, "case_name") else case.name,
        passed=not failures,
        ttft_ms=ttft,
        total_ms=total_ms,
        chars=chars,
        failures=failures,
    )


async def run_harness(cases: list[EvalCase]) -> list[EvalReport]:
    return list(await asyncio.gather(*[run_eval_case(c) for c in cases]))


async def main():
    cases = [
        EvalCase(
            name="basic_factual",
            prompt="What is Python? Answer in 2 sentences.",
            min_chars=50,
            required_keywords=["programming", "language"],
        ),
        EvalCase(
            name="list_format",
            prompt="Name 3 sorting algorithms.",
            min_chars=30,
            required_keywords=["sort"],
        ),
        EvalCase(
            name="speed_check",
            prompt="Say hello.",
            max_ttft_ms=1500,
            min_chars=2,
        ),
    ]

    reports = await run_harness(cases)

    print(f"\n{'Case':<25} {'Status':<8} {'TTFT':>8} {'Chars':>7}")
    print("-" * 52)
    for r in reports:
        status = "PASS" if r.passed else "FAIL"
        ttft_str = f"{r.ttft_ms:.0f}ms" if r.ttft_ms else "N/A"
        print(f"{r.case_name:<25} {status:<8} {ttft_str:>8} {r.chars:>7}")
        for f in r.failures:
            print(f"  ↳ {f}")

    passed = sum(1 for r in reports if r.passed)
    print(f"\n{passed}/{len(reports)} cases passed")


asyncio.run(main())
```

## Comparison

| Solution | What it measures | LLM overhead | When to run | Best for |
|---|---|---|---|---|
| **Stream timing harness** | TTFT, throughput, latency | None | Every CI run | Latency SLO enforcement |
| **Incremental validator** | Format violations in-flight | None | Every CI run | Format regression detection |
| **Completeness checker** | Truncation, section presence | None | Every CI run | Structured output validation |
| **Multi-run consistency** | Output variance across runs | Nx inference | Weekly/nightly | Non-determinism detection |
| **Structured output validator** | JSON/list parseability | None | Every CI run | Constrained output formats |
| **Full harness** | All combined | None | Every CI run | Comprehensive regression suite |

Start with **stream timing harness** (Solution 1) and **completeness checker** (Solution 3) in CI — zero extra cost, immediate signal. Add **multi-run consistency** (Solution 4) on a nightly schedule to catch flakiness. Use **full harness** (Solution 6) as the single entry point once you have mature eval cases.
