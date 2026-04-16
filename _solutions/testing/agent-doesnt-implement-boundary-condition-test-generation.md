---
title: "Agent doesn't implement boundary condition test generation"
description: "Test suites only cover happy-path inputs. Boundary conditions—empty strings, null values, maximum lengths, minimum values, malformed data—are never tested, so edge-case bugs surface in production."
difficulty: intermediate
category: testing
tags: [boundary-testing, property-based-testing, fuzz-testing, hypothesis, test-generation]
---

## Problem

Most agent test suites are written against a single "typical" input: a medium-length user message, a well-formed tool response, a valid JSON config. The edges—empty input, one-character input, 100,000-character input, null, negative numbers, Unicode boundary characters, enum values outside the valid set—are never exercised. These are exactly where parsing bugs, silent truncation, off-by-one errors, and type coercion failures hide.

Boundary condition test generation systematically creates inputs at the extremes of every parameter's domain so that these bugs are caught in CI, not production.

```python
# BAD: one happy-path test covers nothing at the boundary
def test_summarize():
    result = agent.summarize("This is a normal message.")
    assert len(result) > 0
# Missing: empty string, single word, 100k chars, None, non-UTF8 bytes
```

## Solution 1: Parameterized boundary fixture library

Define reusable boundary fixtures for every common type. Inject them into tests via `pytest.mark.parametrize`.

```python
import pytest
import string


# ── String boundaries ────────────────────────────────────────────────
STRING_BOUNDARIES = [
    pytest.param("", id="empty"),
    pytest.param(" ", id="single_space"),
    pytest.param("\t\n\r", id="whitespace_only"),
    pytest.param("a", id="single_char"),
    pytest.param("a" * 100, id="100_chars"),
    pytest.param("a" * 4096, id="4096_chars"),
    pytest.param("a" * 100_000, id="100k_chars"),
    pytest.param("Hello 😀🔥🌍", id="emoji"),
    pytest.param("\u0000\u0001\u001f", id="control_chars"),
    pytest.param("<script>alert(1)</script>", id="xss_attempt"),
    pytest.param("'; DROP TABLE users; --", id="sql_injection"),
    pytest.param("\n".join(["line"] * 1000), id="1000_lines"),
    pytest.param("   leading and trailing   ", id="surrounding_whitespace"),
]

# ── Integer boundaries ───────────────────────────────────────────────
INT_BOUNDARIES = [
    pytest.param(0, id="zero"),
    pytest.param(1, id="one"),
    pytest.param(-1, id="negative_one"),
    pytest.param(2**31 - 1, id="int32_max"),
    pytest.param(-(2**31), id="int32_min"),
    pytest.param(2**63 - 1, id="int64_max"),
]

# ── Float boundaries ─────────────────────────────────────────────────
FLOAT_BOUNDARIES = [
    pytest.param(0.0, id="zero"),
    pytest.param(1.0, id="one"),
    pytest.param(-1.0, id="negative"),
    pytest.param(float("inf"), id="infinity"),
    pytest.param(float("-inf"), id="neg_infinity"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(1e-300, id="very_small"),
    pytest.param(1e300, id="very_large"),
]

# ── None / missing boundaries ─────────────────────────────────────────
NONE_BOUNDARIES = [
    pytest.param(None, id="none"),
    pytest.param({}, id="empty_dict"),
    pytest.param([], id="empty_list"),
    pytest.param("null", id="string_null"),
    pytest.param("None", id="string_None"),
]


# ── Example tests using fixtures ─────────────────────────────────────
def normalize_input(text: str) -> str:
    if not text or not text.strip():
        return ""
    return text.strip()[:10_000]


@pytest.mark.parametrize("text", STRING_BOUNDARIES)
def test_normalize_input_string_boundaries(text):
    """normalize_input must never raise and must return a string."""
    result = normalize_input(text)
    assert isinstance(result, str)
    assert len(result) <= 10_000


@pytest.mark.parametrize("value", INT_BOUNDARIES)
def test_token_budget_int_boundaries(value):
    """Token budget should clamp, not raise."""
    clamped = max(1, min(200_000, int(value) if value is not None else 1))
    assert 1 <= clamped <= 200_000
```

## Solution 2: Schema-derived boundary generator

Given a JSON Schema or Pydantic model, automatically generate boundary cases for each field type and constraint.

```python
from pydantic import BaseModel, Field
from typing import Any, Iterator
import itertools


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10_000)
    max_tokens: int = Field(ge=1, le=200_000, default=4096)
    temperature: float = Field(ge=0.0, le=1.0, default=0.7)
    tags: list[str] = Field(default_factory=list, max_length=20)


def boundaries_for_field(name: str, field_info) -> list[tuple[str, Any]]:
    """Return (case_id, value) boundary pairs for a pydantic field."""
    cases = []
    meta = field_info.metadata  # contains Annotated constraints

    # Extract ge/le/min_length/max_length from field metadata
    constraints = {}
    for m in meta:
        for attr in ["ge", "le", "gt", "lt", "min_length", "max_length"]:
            if hasattr(m, attr):
                constraints[attr] = getattr(m, attr)

    annotation = field_info.annotation

    if annotation == str or (hasattr(annotation, "__origin__") and annotation == str):
        min_len = constraints.get("min_length", 0)
        max_len = constraints.get("max_length", 10_000)
        cases += [
            (f"{name}_min_length", "a" * min_len),
            (f"{name}_max_length", "a" * max_len),
            (f"{name}_empty", ""),
            (f"{name}_whitespace", "   "),
            (f"{name}_unicode", "café résumé"),
            (f"{name}_newlines", "\n" * 10),
        ]
    elif annotation == int:
        lo = constraints.get("ge", constraints.get("gt", 0))
        hi = constraints.get("le", constraints.get("lt", 2**31))
        cases += [
            (f"{name}_min", lo),
            (f"{name}_max", hi),
            (f"{name}_min_minus_1", lo - 1),
            (f"{name}_max_plus_1", hi + 1),
            (f"{name}_zero", 0),
            (f"{name}_negative", -1),
        ]
    elif annotation == float:
        lo = constraints.get("ge", 0.0)
        hi = constraints.get("le", 1.0)
        cases += [
            (f"{name}_min", lo),
            (f"{name}_max", hi),
            (f"{name}_below_min", lo - 0.001),
            (f"{name}_above_max", hi + 0.001),
            (f"{name}_nan", float("nan")),
        ]

    return cases


def generate_boundary_cases(model: type[BaseModel]) -> list[dict[str, Any]]:
    """Generate one boundary test case per field boundary."""
    base = {k: v.default for k, v in model.model_fields.items() if v.default is not None}
    cases = []
    for field_name, field_info in model.model_fields.items():
        for case_id, value in boundaries_for_field(field_name, field_info):
            case = dict(base)
            case[field_name] = value
            cases.append({"id": case_id, "input": case})
    return cases


# --- Usage in pytest ---
import pytest

boundary_cases = generate_boundary_cases(AgentRequest)


@pytest.mark.parametrize("case", boundary_cases, ids=[c["id"] for c in boundary_cases])
def test_agent_request_boundaries(case):
    """All boundary inputs must be handled without an unhandled exception."""
    try:
        req = AgentRequest(**case["input"])
        # At minimum: model constructs without crash
        assert req is not None
    except Exception as e:
        # Validation errors are acceptable; unhandled crashes are not
        assert "validation" in type(e).__name__.lower() or "value" in type(e).__name__.lower(), \
            f"Unexpected exception type {type(e).__name__}: {e}"
```

## Solution 3: Hypothesis-based property testing with custom strategies

Use the `hypothesis` library to generate thousands of random boundary inputs automatically, guided by custom strategies for your domain types.

```python
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st
import string


# ── Custom strategies ────────────────────────────────────────────────
user_message_strategy = st.one_of(
    st.just(""),                                          # empty
    st.text(min_size=1, max_size=1),                      # single char
    st.text(min_size=1, max_size=10_000),                 # normal range
    st.text(min_size=10_001, max_size=100_000),           # over limit
    st.text(alphabet=string.whitespace, min_size=1, max_size=100),  # whitespace only
    st.binary(min_size=1, max_size=100).map(             # random bytes decoded
        lambda b: b.decode("utf-8", errors="replace")
    ),
)

token_budget_strategy = st.one_of(
    st.integers(min_value=1, max_value=200_000),          # valid
    st.integers(min_value=-100, max_value=0),             # invalid negative
    st.integers(min_value=200_001, max_value=10_000_000), # over limit
    st.just(0),
)

temperature_strategy = st.one_of(
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False),   # valid
    st.just(float("nan")),
    st.just(float("inf")),
    st.floats(min_value=-10.0, max_value=-0.001),               # negative
    st.floats(min_value=1.001, max_value=100.0),                # over 1
)


# ── Component under test ─────────────────────────────────────────────
def build_request_payload(message: str, max_tokens: int, temperature: float) -> dict:
    """Build and sanitize an API request payload."""
    clean_message = (message or "").strip()[:10_000]
    safe_tokens = max(1, min(int(max_tokens), 200_000)) if isinstance(max_tokens, (int, float)) and not (isinstance(max_tokens, float) and (max_tokens != max_tokens)) else 4096
    safe_temp = max(0.0, min(float(temperature), 1.0)) if isinstance(temperature, float) and not (temperature != temperature) else 0.7

    if not clean_message:
        raise ValueError("Message cannot be empty after normalization")

    return {
        "messages": [{"role": "user", "content": clean_message}],
        "max_tokens": safe_tokens,
        "temperature": safe_temp,
    }


# ── Property tests ───────────────────────────────────────────────────
@given(
    message=user_message_strategy,
    max_tokens=token_budget_strategy,
    temperature=temperature_strategy,
)
@settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
def test_build_request_payload_never_crashes(message, max_tokens, temperature):
    """build_request_payload must never raise an unhandled exception."""
    try:
        payload = build_request_payload(message, max_tokens, temperature)
        # Postconditions: if it returns, the payload must be valid
        assert isinstance(payload["max_tokens"], int)
        assert 1 <= payload["max_tokens"] <= 200_000
        assert isinstance(payload["temperature"], float)
        assert 0.0 <= payload["temperature"] <= 1.0
        assert len(payload["messages"][0]["content"]) <= 10_000
    except ValueError:
        pass  # Acceptable: empty message after normalization
    except Exception as e:
        raise AssertionError(f"Unexpected exception: {type(e).__name__}: {e}")
```

## Solution 4: LLM-generated adversarial boundary cases

Ask a judge model to generate creative boundary inputs that a human tester would miss — focusing on domain-specific edge cases unique to your agent's purpose.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from typing import Any

client = AsyncAnthropic()

BOUNDARY_GEN_PROMPT = """You are an expert software tester specializing in boundary condition analysis.

Given the following function signature and description, generate 10 adversarial boundary test cases.
Focus on inputs that are:
1. At or just beyond valid limits (min-1, max+1, empty, single element)
2. Semantically tricky (looks valid but causes subtle bugs)
3. Encoding edge cases (Unicode, emoji, right-to-left text, zero-width chars)
4. Injection attempts relevant to the domain
5. Type confusion (string "null", "true", "0" that might be coerced)

Function: {function_name}
Description: {description}
Parameters: {parameters}

Respond ONLY with a JSON array of test cases:
[
  {{"id": "case_name", "input": {{"param": "value"}}, "expected_behavior": "should raise ValueError / should return empty string / etc."}}
]"""


async def generate_boundary_cases(
    function_name: str,
    description: str,
    parameters: dict[str, str],
) -> list[dict[str, Any]]:
    message = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": BOUNDARY_GEN_PROMPT.format(
                function_name=function_name,
                description=description,
                parameters=json.dumps(parameters, indent=2),
            ),
        }],
    )
    text = message.content[0].text.strip()
    # Strip markdown if present
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


async def main():
    cases = await generate_boundary_cases(
        function_name="summarize_document",
        description="Accepts a document string and returns a 1–3 sentence summary. Raises ValueError for empty input.",
        parameters={
            "document": "str — the document to summarize, max 50000 chars",
        },
    )
    for case in cases:
        print(f"[{case['id']}] input={case['input']} → {case['expected_behavior']}")


asyncio.run(main())
```

## Solution 5: Production-log fuzz corpus harvesting

Collect real inputs from production logs that caused errors, slow responses, or unexpected outputs. Use them as a fuzz corpus for regression testing.

```python
import json
import re
import hashlib
import os
from dataclasses import dataclass, asdict
from typing import Iterator


@dataclass
class FuzzCorpusEntry:
    corpus_id: str
    input_text: str
    error_type: str | None
    latency_ms: float | None
    source: str  # "production_error" | "slow_request" | "unexpected_output"


class ProductionFuzzCorpus:
    def __init__(self, corpus_dir: str = ".fuzz_corpus"):
        self.corpus_dir = corpus_dir
        os.makedirs(corpus_dir, exist_ok=True)

    def _entry_path(self, corpus_id: str) -> str:
        return os.path.join(self.corpus_dir, f"{corpus_id}.json")

    def add(self, entry: FuzzCorpusEntry):
        with open(self._entry_path(entry.corpus_id), "w") as f:
            json.dump(asdict(entry), f, indent=2)

    def __iter__(self) -> Iterator[FuzzCorpusEntry]:
        for fname in os.listdir(self.corpus_dir):
            if fname.endswith(".json"):
                with open(os.path.join(self.corpus_dir, fname)) as f:
                    yield FuzzCorpusEntry(**json.load(f))


def harvest_from_log(log_path: str, corpus: ProductionFuzzCorpus):
    """
    Parse a structured log file and extract inputs that caused errors or slow responses.
    Expected log format: one JSON object per line with fields: message, error, latency_ms.
    """
    with open(log_path) as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            message = record.get("message", "")
            error = record.get("error")
            latency = record.get("latency_ms", 0)

            # Harvest errors and slow requests (>5s) as corpus entries
            if error or latency > 5000:
                corpus_id = hashlib.sha256(message.encode()).hexdigest()[:12]
                source = "production_error" if error else "slow_request"
                corpus.add(FuzzCorpusEntry(
                    corpus_id=corpus_id,
                    input_text=message,
                    error_type=error,
                    latency_ms=latency,
                    source=source,
                ))


# ── Use the corpus in pytest ─────────────────────────────────────────
import pytest

corpus = ProductionFuzzCorpus()
corpus_entries = list(corpus)


@pytest.mark.parametrize(
    "entry",
    corpus_entries,
    ids=[e.corpus_id for e in corpus_entries],
)
def test_production_corpus_no_regression(entry):
    """
    Every input from the production fuzz corpus must be handled without
    an unhandled exception. The original error (if any) should be
    raised as a controlled exception type, not an unexpected crash.
    """
    from your_agent import process_message  # replace with actual import

    try:
        result = process_message(entry.input_text)
        assert result is not None
    except (ValueError, TypeError, RuntimeError):
        pass  # Controlled exceptions are acceptable
    except Exception as e:
        pytest.fail(
            f"Unhandled exception for corpus entry {entry.corpus_id} "
            f"(originally: {entry.error_type}): {type(e).__name__}: {e}"
        )
```

## Solution 6: Combinatorial boundary explosion runner with coverage tracking

When a function has multiple parameters, exhaustive combinations of boundaries grow exponentially. This solution samples the boundary space intelligently using pairwise (t-way) combinatorics.

```python
import itertools
import pytest
from typing import Any


def pairwise_combinations(param_boundaries: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """
    Generate pairwise (2-way) combinations of parameter boundaries.
    Covers all pairs without combinatorial explosion.
    Produces O(N*M) cases instead of O(N^M).
    """
    params = list(param_boundaries.keys())
    all_values = list(param_boundaries.values())

    cases = []
    seen = set()

    # Start with the first param × all others (pairwise)
    for i, j in itertools.combinations(range(len(params)), 2):
        for vi in all_values[i]:
            for vj in all_values[j]:
                # Build a case: use first boundary value for all other params
                case = {p: vals[0] for p, vals in param_boundaries.items()}
                case[params[i]] = vi
                case[params[j]] = vj
                key = json.dumps(case, sort_keys=True, default=str)
                if key not in seen:
                    seen.add(key)
                    cases.append(case)

    return cases


import json

PARAM_BOUNDARIES = {
    "message": ["", "a", "a" * 10_000, "a" * 100_001, None],
    "max_tokens": [0, 1, 4096, 200_000, 200_001],
    "temperature": [-0.1, 0.0, 0.7, 1.0, 1.1, float("nan")],
    "stream": [True, False, None],
}


def agent_validate_params(message, max_tokens, temperature, stream) -> bool:
    """Returns True if params are valid, raises ValueError if not."""
    if not message or not isinstance(message, str):
        raise ValueError("message must be a non-empty string")
    if not (1 <= max_tokens <= 200_000):
        raise ValueError("max_tokens out of range")
    if not (0.0 <= temperature <= 1.0) or temperature != temperature:  # nan check
        raise ValueError("temperature out of range")
    return True


pairwise_cases = pairwise_combinations(PARAM_BOUNDARIES)


@pytest.mark.parametrize("case", pairwise_cases)
def test_pairwise_param_boundaries(case):
    try:
        result = agent_validate_params(**case)
        assert result is True
    except ValueError:
        pass  # Controlled rejection is expected for boundary inputs
    except Exception as e:
        pytest.fail(f"Unexpected exception {type(e).__name__}: {e} for input {case}")
```

## Comparison

| Approach | Coverage breadth | Maintenance cost | Finds unknown bugs | CI speed | LLM required |
|---|---|---|---|---|---|
| Parameterized fixtures | Manual boundaries | Low | No | Fast | No |
| Schema-derived generator | All fields auto | Medium | Partial | Fast | No |
| Hypothesis property testing | Thousands of cases | Low | Yes | Medium | No |
| LLM adversarial generation | Domain-specific | Low | Yes | Medium | Yes |
| Production fuzz corpus | Real-world inputs | Low | Yes | Fast | No |
| Pairwise combinatorial | Cross-param pairs | Low | Partial | Fast | No |

**Recommendation**: Start with **parameterized fixtures** (Solution 1) for immediate coverage, add **Hypothesis** (Solution 3) for automated exploration, and feed **production corpus** (Solution 5) back into CI to prevent regressions from real-world inputs. Use **LLM-generated cases** (Solution 4) when your domain has complex semantic constraints that are hard to express as simple type boundaries.
