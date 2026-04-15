---
layout: solution
title: "Agent Doesn't Implement Mutation Testing"
category: testing
description: "Unit tests that pass with a 100% coverage score can still miss critical logic bugs if the tests don't assert precise enough conditions. Mutation testing proves your tests actually catch errors."
tags: [testing, mutation, quality, pytest, mutmut, coverage]
---

# Agent Doesn't Implement Mutation Testing

Coverage metrics show whether lines of code were *executed* by tests — not whether the tests would *catch a bug* on those lines. Mutation testing introduces small deliberate faults (mutants) into your code and checks whether your test suite kills each one. A surviving mutant means your tests wouldn't catch that class of bug.

## Why This Happens

Most projects stop at `pytest --cov` once they hit 80%+ coverage. Mutation testing tools (`mutmut`, `cosmic-ray`) are slower and less familiar, so teams skip them even though coverage alone can't verify test adequacy.

---

## Option 1: mutmut on Core Agent Logic

Run `mutmut` against critical agent functions and fail CI if survival rate exceeds threshold.

```python
# agent_core.py — the module under test
import anthropic

client = anthropic.Anthropic()


def classify_intent(text: str) -> str:
    """Classify user intent into one of: question, command, feedback, other."""
    if not text or not text.strip():
        return "other"

    lowered = text.lower()
    if lowered.endswith("?") or lowered.startswith(("what", "how", "why", "when", "who")):
        return "question"
    if lowered.startswith(("do ", "run ", "create ", "delete ", "update ", "list ")):
        return "command"
    if any(w in lowered for w in ("good", "bad", "wrong", "great", "terrible", "thanks")):
        return "feedback"
    return "other"


def should_use_expensive_model(intent: str, token_estimate: int) -> bool:
    """Route to Opus only for complex intents with large context."""
    return intent == "command" and token_estimate > 2000


def truncate_to_budget(text: str, max_tokens: int) -> str:
    """Trim text to approximate token budget (4 chars ~ 1 token)."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars]
```

```python
# tests/test_agent_core.py — mutation-hardened tests
import pytest
from agent_core import classify_intent, should_use_expensive_model, truncate_to_budget


class TestClassifyIntent:
    def test_question_mark(self):
        assert classify_intent("Is this correct?") == "question"

    def test_what_prefix(self):
        assert classify_intent("What is the weather?") == "question"

    def test_how_prefix(self):
        assert classify_intent("How do I reset this?") == "question"

    def test_command_do(self):
        assert classify_intent("Do this task now") == "command"

    def test_command_run(self):
        assert classify_intent("Run the pipeline") == "command"

    def test_feedback_good(self):
        assert classify_intent("That was good work") == "feedback"

    def test_feedback_bad(self):
        assert classify_intent("That was bad") == "feedback"

    def test_other(self):
        assert classify_intent("Some random text here") == "other"

    def test_empty_string(self):
        assert classify_intent("") == "other"

    def test_whitespace_only(self):
        assert classify_intent("   ") == "other"

    # Mutation-killing: test exact return values, not just not-None
    def test_returns_string_not_bool(self):
        result = classify_intent("What?")
        assert isinstance(result, str)
        assert result == "question"  # exact match kills value-swap mutants

    def test_not_command_for_question(self):
        assert classify_intent("What time is it?") != "command"

    def test_not_other_for_known_intent(self):
        assert classify_intent("Do the thing") != "other"


class TestShouldUseExpensiveModel:
    def test_command_large_context(self):
        assert should_use_expensive_model("command", 2001) is True

    def test_command_exact_boundary(self):
        # Boundary value: exactly 2000 should NOT trigger expensive model
        assert should_use_expensive_model("command", 2000) is False

    def test_command_below_threshold(self):
        assert should_use_expensive_model("command", 1999) is False

    def test_question_large_context(self):
        # Questions never use expensive model regardless of size
        assert should_use_expensive_model("question", 5000) is False

    def test_feedback_large_context(self):
        assert should_use_expensive_model("feedback", 5000) is False

    def test_other_large_context(self):
        assert should_use_expensive_model("other", 99999) is False

    # Mutation-killing: test the exact boolean, not truthiness
    def test_returns_exact_true(self):
        result = should_use_expensive_model("command", 2001)
        assert result is True  # kills `return 1` mutants

    def test_returns_exact_false(self):
        result = should_use_expensive_model("command", 500)
        assert result is False  # kills `return 0` or `return None` mutants


class TestTruncateToBudget:
    def test_short_text_unchanged(self):
        text = "Hello"
        assert truncate_to_budget(text, 100) == text

    def test_exact_boundary(self):
        text = "a" * 400  # exactly 100 tokens * 4
        assert truncate_to_budget(text, 100) == text

    def test_over_budget_truncated(self):
        text = "a" * 401
        result = truncate_to_budget(text, 100)
        assert len(result) == 400  # exact length, not just "shorter"

    def test_truncation_preserves_start(self):
        text = "START" + "x" * 1000
        result = truncate_to_budget(text, 5)
        assert result.startswith("START")

    def test_zero_budget(self):
        result = truncate_to_budget("hello", 0)
        assert result == ""

    def test_empty_input(self):
        assert truncate_to_budget("", 100) == ""
```

```bash
# Run mutation testing
pip install mutmut
mutmut run --paths-to-mutate agent_core.py --tests-dir tests/
mutmut results
# Show surviving mutants (tests that didn't catch the mutation)
mutmut show
```

**Expected Token Savings:** Kills weak tests before they ship; each surviving mutant represents an untested bug class.

**Environment:** Any Python project; `mutmut` works with pytest out of the box.

---

## Option 2: cosmic-ray for Exhaustive Mutation Operators

Use `cosmic-ray` for a broader set of mutation operators including boolean short-circuit and exception mutations.

```python
# tool_validator.py — agent tool input validator
from typing import Any


def validate_tool_input(tool_name: str, inputs: dict[str, Any]) -> tuple[bool, str]:
    """
    Validate tool inputs before calling external API.
    Returns (is_valid, error_message).
    """
    if not tool_name:
        return False, "tool_name cannot be empty"

    if not isinstance(inputs, dict):
        return False, "inputs must be a dict"

    if len(inputs) == 0:
        return False, "inputs cannot be empty"

    # Check for required string fields being non-empty
    for key, value in inputs.items():
        if isinstance(value, str) and len(value) == 0:
            return False, f"field '{key}' cannot be an empty string"

    return True, ""
```

```toml
# cosmic-ray.toml
[cosmic-ray]
module-path = "tool_validator.py"
timeout = 10.0
excluded-modules = []

[cosmic-ray.test-command]
command = "python -m pytest tests/test_tool_validator.py -x -q"

[[cosmic-ray.operators]]
name = "cosmic_ray.operators.boolean_replacer"

[[cosmic-ray.operators]]
name = "cosmic_ray.operators.comparison_operator_replacer"

[[cosmic-ray.operators]]
name = "cosmic_ray.operators.return_value_replacer"

[[cosmic-ray.operators]]
name = "cosmic_ray.operators.exception_replacer"
```

```python
# tests/test_tool_validator.py — mutation-hardened
import pytest
from tool_validator import validate_tool_input


@pytest.mark.parametrize("tool_name,inputs,expected_valid,error_contains", [
    ("get_weather", {"location": "NYC"}, True, ""),
    ("", {"location": "NYC"}, False, "empty"),
    ("get_weather", [], False, "dict"),
    ("get_weather", {}, False, "empty"),
    ("get_weather", {"query": ""}, False, "empty string"),
    ("search", {"q": "hello", "limit": 10}, True, ""),
])
def test_validate_tool_input(tool_name, inputs, expected_valid, error_contains):
    valid, error = validate_tool_input(tool_name, inputs)
    assert valid is expected_valid  # exact bool, kills value-swap mutants
    if error_contains:
        assert error_contains in error.lower()
    else:
        assert error == ""


def test_valid_returns_empty_error():
    valid, error = validate_tool_input("tool", {"key": "val"})
    assert valid is True
    assert error == ""  # not just falsy — exact empty string


def test_invalid_returns_nonempty_error():
    valid, error = validate_tool_input("", {"key": "val"})
    assert valid is False
    assert len(error) > 0  # error message must be non-empty


def test_numeric_values_allowed():
    valid, error = validate_tool_input("tool", {"count": 5, "flag": True})
    assert valid is True


def test_none_inputs():
    valid, error = validate_tool_input("tool", None)  # type: ignore
    assert valid is False
```

```bash
# Run cosmic-ray
pip install cosmic-ray
cosmic-ray init cosmic-ray.toml session.sqlite
cosmic-ray exec session.sqlite
cosmic-ray report session.sqlite
# Check mutation score
cosmic-ray survival-rate session.sqlite
```

**Expected Token Savings:** Exhaustive mutation operators catch conditional boundary bugs and exception-handling holes before production.

**Environment:** Python; CI pipelines; most effective on pure logic modules with no I/O.

---

## Option 3: Manual Mutation Testing for Critical Paths

Write explicit "anti-tests" that verify the implementation rejects wrong-but-close behavior — without relying on a mutation tool.

```python
# rate_limiter.py
import time
from collections import deque


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        # Remove timestamps outside window
        while self._timestamps and self._timestamps[0] <= now - self._window:
            self._timestamps.popleft()

        if len(self._timestamps) >= self._max:
            return False

        self._timestamps.append(now)
        return True

    @property
    def current_count(self) -> int:
        return len(self._timestamps)
```

```python
# tests/test_rate_limiter_mutations.py
import time
import pytest
from rate_limiter import SlidingWindowRateLimiter


class TestRateLimiterMutationResistant:
    """
    Each test is designed to catch a specific class of mutation:
    - Off-by-one in limit check (>= vs >)
    - Wrong comparison in window cleanup (<= vs <)
    - Missing timestamp append
    - Inverted return value
    """

    def test_allows_exactly_max(self):
        limiter = SlidingWindowRateLimiter(3, 1.0)
        results = [limiter.allow() for _ in range(3)]
        # Kills `> max` mutant (would allow max+1)
        assert results == [True, True, True]

    def test_blocks_at_max_plus_one(self):
        limiter = SlidingWindowRateLimiter(3, 1.0)
        for _ in range(3):
            limiter.allow()
        # Kills `> max` mutant (would allow the 4th)
        assert limiter.allow() is False

    def test_count_increments(self):
        limiter = SlidingWindowRateLimiter(10, 1.0)
        limiter.allow()
        # Kills missing-append mutant
        assert limiter.current_count == 1

    def test_allow_returns_true_not_truthy(self):
        limiter = SlidingWindowRateLimiter(5, 1.0)
        result = limiter.allow()
        # Kills `return 1` mutant
        assert result is True

    def test_deny_returns_false_not_falsy(self):
        limiter = SlidingWindowRateLimiter(1, 10.0)
        limiter.allow()
        result = limiter.allow()
        # Kills `return 0` mutant
        assert result is False

    def test_window_expiry_resets(self):
        limiter = SlidingWindowRateLimiter(2, 0.1)
        limiter.allow()
        limiter.allow()
        assert limiter.allow() is False  # at limit

        time.sleep(0.15)
        # Kills off-by-one in window comparison (<= vs <)
        assert limiter.allow() is True

    def test_one_request_limit(self):
        limiter = SlidingWindowRateLimiter(1, 10.0)
        assert limiter.allow() is True
        assert limiter.allow() is False
        # Third call — still blocked
        assert limiter.allow() is False

    def test_zero_allowed_blocks_immediately(self):
        limiter = SlidingWindowRateLimiter(0, 1.0)
        # Kills `>= max` -> `> max` mutant
        assert limiter.allow() is False
```

**Expected Token Savings:** Manual mutation tests are free to run and catch the most common mutation classes without tool overhead.

**Environment:** Pure Python logic; useful when mutation tools are too slow for CI.

---

## Option 4: Property-Based Mutation Detection with Hypothesis

Use Hypothesis strategies to generate inputs that will expose mutant behavior in boundary conditions.

```python
# token_counter.py
def estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token per 4 chars, minimum 1."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def fits_in_budget(text: str, budget: int) -> bool:
    """Check if text fits within token budget."""
    return estimate_tokens(text) <= budget


def split_to_chunks(text: str, chunk_tokens: int) -> list[str]:
    """Split text into chunks of approximately chunk_tokens each."""
    if not text or chunk_tokens <= 0:
        return []
    chunk_size = chunk_tokens * 4
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
```

```python
# tests/test_token_counter_property.py
import pytest
from hypothesis import given, assume, settings
from hypothesis import strategies as st
from token_counter import estimate_tokens, fits_in_budget, split_to_chunks


@given(st.text())
def test_empty_returns_zero(text):
    if text == "":
        assert estimate_tokens(text) == 0


@given(st.text(min_size=1))
def test_nonempty_returns_at_least_one(text):
    assert estimate_tokens(text) >= 1


@given(st.text(min_size=1))
def test_longer_text_more_tokens(text):
    # Kills mutants that drop the max(1, ...) or use wrong divisor
    assume(len(text) >= 4)
    short = text[:len(text) // 2]
    long = text
    assert estimate_tokens(long) >= estimate_tokens(short)


@given(st.text(), st.integers(min_value=0, max_value=10000))
def test_fits_in_budget_consistent(text, budget):
    tokens = estimate_tokens(text)
    fits = fits_in_budget(text, budget)
    # Kills `<` vs `<=` mutant
    assert fits == (tokens <= budget)


@given(st.text(), st.integers(min_value=1, max_value=100))
def test_chunks_cover_full_text(text, chunk_tokens):
    assume(len(text) > 0)
    chunks = split_to_chunks(text, chunk_tokens)
    # All text is covered — kills any off-by-one in range()
    assert "".join(chunks) == text


@given(st.text(min_size=1), st.integers(min_value=1, max_value=50))
def test_each_chunk_size_bounded(text, chunk_tokens):
    chunks = split_to_chunks(text, chunk_tokens)
    max_chars = chunk_tokens * 4
    for chunk in chunks[:-1]:  # last chunk may be shorter
        assert len(chunk) == max_chars


@given(st.integers(min_value=0), st.integers(min_value=0))
def test_zero_budget_never_fits_nonempty(budget, _):
    assume(budget < 1)
    assert fits_in_budget("x", budget) is False
```

**Expected Token Savings:** Hypothesis finds minimal failing examples that manual tests miss; kills arithmetic-operator mutants effectively.

**Environment:** Any Python project; `pip install hypothesis`.

---

## Option 5: CI Mutation Score Gate

Add a mutation score check to CI that fails the build if the score drops below a threshold.

```python
# scripts/check_mutation_score.py
"""
Run mutmut and fail CI if mutation score < MIN_SCORE.
Usage: python scripts/check_mutation_score.py --min-score 0.80
"""
import subprocess
import sys
import re
import argparse


def run_mutmut(paths: list[str]) -> dict:
    """Run mutmut and parse results."""
    print("Running mutation tests...")
    result = subprocess.run(
        ["mutmut", "run", "--paths-to-mutate", ",".join(paths)],
        capture_output=True,
        text=True,
    )

    # Get results
    results_output = subprocess.run(
        ["mutmut", "results"],
        capture_output=True,
        text=True,
    ).stdout

    killed = len(re.findall(r"Killed", results_output))
    survived = len(re.findall(r"Survived", results_output))
    total = killed + survived

    return {
        "killed": killed,
        "survived": survived,
        "total": total,
        "score": killed / total if total > 0 else 0.0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-score", type=float, default=0.80)
    parser.add_argument("--paths", nargs="+", default=["src/"])
    args = parser.parse_args()

    stats = run_mutmut(args.paths)

    print(f"\nMutation Testing Results:")
    print(f"  Killed:   {stats['killed']}")
    print(f"  Survived: {stats['survived']}")
    print(f"  Total:    {stats['total']}")
    print(f"  Score:    {stats['score']:.1%}")
    print(f"  Required: {args.min_score:.1%}")

    if stats["score"] < args.min_score:
        print(f"\nFAIL: Mutation score {stats['score']:.1%} < {args.min_score:.1%}")
        print("Surviving mutants represent untested bug classes. Improve your tests.")
        sys.exit(1)
    else:
        print(f"\nPASS: Mutation score {stats['score']:.1%} >= {args.min_score:.1%}")
        sys.exit(0)


if __name__ == "__main__":
    main()
```

```yaml
# .github/workflows/mutation-test.yml
name: Mutation Tests
on:
  pull_request:
    paths:
      - "src/**/*.py"
      - "tests/**/*.py"

jobs:
  mutation:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install mutmut pytest
      - run: python scripts/check_mutation_score.py --min-score 0.80 --paths src/agent_core.py src/tool_validator.py
```

**Expected Token Savings:** Enforces test quality gate in CI; prevents weak tests from shipping.

**Environment:** GitHub Actions or any CI system; `mutmut` + pytest.

---

## Option 6: Snapshot + Mutation Combo for Prompt Logic

For LLM prompt construction functions, combine snapshot testing with manual mutation to verify prompt assembly logic.

```python
# prompt_builder.py
from dataclasses import dataclass


@dataclass
class AgentContext:
    user_name: str
    task: str
    tools: list[str]
    max_steps: int = 5


def build_system_prompt(ctx: AgentContext) -> str:
    tool_list = "\n".join(f"- {t}" for t in ctx.tools)
    return (
        f"You are a helpful assistant for {ctx.user_name}.\n\n"
        f"Your task: {ctx.task}\n\n"
        f"Available tools:\n{tool_list}\n\n"
        f"Complete the task in at most {ctx.max_steps} steps."
    )


def build_user_message(task: str, context: str = "") -> str:
    if context:
        return f"Context:\n{context}\n\nTask: {task}"
    return f"Task: {task}"
```

```python
# tests/test_prompt_builder_mutations.py
import pytest
from prompt_builder import AgentContext, build_system_prompt, build_user_message


CTX = AgentContext(
    user_name="Alice",
    task="Summarize the document",
    tools=["read_file", "search", "write_file"],
    max_steps=3,
)


class TestBuildSystemPromptMutations:
    def test_contains_user_name(self):
        prompt = build_system_prompt(CTX)
        assert "Alice" in prompt

    def test_contains_task(self):
        assert "Summarize the document" in build_system_prompt(CTX)

    def test_contains_all_tools(self):
        prompt = build_system_prompt(CTX)
        for tool in CTX.tools:
            assert f"- {tool}" in prompt  # exact format, kills join-separator mutants

    def test_contains_max_steps(self):
        prompt = build_system_prompt(CTX)
        assert "3" in prompt  # kills max_steps value mutants

    def test_different_max_steps_reflected(self):
        ctx2 = AgentContext("Bob", "task", ["tool"], max_steps=7)
        assert "7" in build_system_prompt(ctx2)
        assert "3" not in build_system_prompt(ctx2)  # kills value not changing

    def test_tool_order_preserved(self):
        prompt = build_system_prompt(CTX)
        idx_read = prompt.index("read_file")
        idx_search = prompt.index("search")
        idx_write = prompt.index("write_file")
        assert idx_read < idx_search < idx_write

    def test_no_tools_produces_empty_list(self):
        ctx = AgentContext("X", "task", [])
        prompt = build_system_prompt(ctx)
        assert "Available tools:\n\n" in prompt  # empty list section present


class TestBuildUserMessageMutations:
    def test_without_context(self):
        result = build_user_message("Do something")
        assert result == "Task: Do something"
        assert "Context" not in result  # kills context-always-included mutant

    def test_with_context_includes_both(self):
        result = build_user_message("Do something", "Background info")
        assert "Background info" in result
        assert "Do something" in result

    def test_context_before_task(self):
        result = build_user_message("Task text", "Context text")
        assert result.index("Context text") < result.index("Task text")

    def test_empty_context_treated_as_no_context(self):
        result = build_user_message("Do something", "")
        assert "Context" not in result  # kills `if context:` -> `if True:` mutant
```

**Expected Token Savings:** Catches prompt assembly bugs before they cause model behavior regressions; mutation-kills verify format-critical logic.

**Environment:** Any agent with non-trivial prompt construction logic; zero external dependencies.

---

## Comparison

| Option | Tool | Speed | CI Integration | Mutation Operators | Best For |
|--------|------|-------|---------------|-------------------|----------|
| 1. mutmut basic | mutmut | Medium | Yes | Arithmetic, boolean | Core logic modules |
| 2. cosmic-ray exhaustive | cosmic-ray | Slow | Yes | All operators | Critical path exhaustive check |
| 3. Manual anti-tests | None | Fast | Yes | Custom | Logic without tool overhead |
| 4. Hypothesis property | Hypothesis | Fast | Yes | Boundary/arithmetic | Numeric/string functions |
| 5. CI score gate | mutmut + script | Medium | Yes | All | Enforcing quality threshold |
| 6. Snapshot + mutation | None | Fast | Yes | Value/format | Prompt construction logic |
