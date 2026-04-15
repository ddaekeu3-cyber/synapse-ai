---
layout: solution
title: "Agent Hallucinates Test Assertions That Pass Vacuously"
category: hallucination
description: "Agent generates unit tests with empty bodies, `assert True`, or assertions that always evaluate to True regardless of actual code behaviour — tests that provide false confidence and catch no real bugs."
tags: [hallucination, testing, assertions, quality, tdd]
---

## Symptom

The agent generates a test suite that looks complete but contains vacuous assertions:

```python
def test_calculate_discount():
    result = calculate_discount(100, 0.1)
    assert result  # Always True for non-zero floats — 0.0 would also be wrong!

def test_parse_date():
    parse_date("2024-01-15")  # No assertion — always passes

def test_user_creation():
    user = create_user("alice", "alice@example.com")
    assert user is not None  # True even if fields are all wrong
    assert True  # Literal no-op
```

All tests pass, CI is green, and a production bug ships that the tests were supposed to catch.

## Root Cause

The model generates syntactically valid test code but loses track of *what* the assertion should verify. Common failure modes:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=400,
    messages=[{"role": "user", "content": "Write tests for this function:\ndef add(a, b): return a + b"}]
)
# Model may produce:
# def test_add():
#     result = add(2, 3)
#     assert result  # ← Vacuous: True for any nonzero number
```

The model knows the *shape* of a test but doesn't always reason through what value is expected and what value would indicate failure.

---

## Fix

### Option 1 — System prompt requires concrete expected values

Instruct the model to always state the expected value explicitly and explain why that value is correct.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

TEST_QUALITY_SYSTEM = """You are a senior test engineer. When writing tests, follow these rules:

MANDATORY assertion rules:
1. Every test function MUST contain at least one assertEqual / == assertion against a CONCRETE expected value.
2. Never use `assert result` alone — always `assert result == <expected>`.
3. Never use `assert True` or `assert result is not None` as the only assertion.
4. Never write a test body that calls the function but makes no assertion.
5. For each assertion, add a comment explaining: what value is expected and what bug it would catch.
6. Include at least one negative test (what the function should NOT return or raise).

Bad:
    assert result          # Wrong — vacuous
    assert result is not None  # Wrong — doesn't verify correctness

Good:
    assert result == 90.0  # 10% discount on 100 → 90.0; catches off-by-one in percentage math
    assert result != 100.0  # Discount must reduce price"""


def generate_tests(function_code: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=TEST_QUALITY_SYSTEM,
        messages=[{"role": "user", "content": f"Write tests for:\n\n```python\n{function_code}\n```"}]
    )
    return response.content[0].text.strip()


code = """
def calculate_discount(price: float, discount_rate: float) -> float:
    \"\"\"Apply a discount rate (0.0–1.0) to a price. Returns discounted price.\"\"\"
    if not 0.0 <= discount_rate <= 1.0:
        raise ValueError("discount_rate must be between 0.0 and 1.0")
    return price * (1 - discount_rate)
"""

tests = generate_tests(code)
print(tests)

# Expected Token Savings: concrete assertions find real bugs → no wasted debug sessions chasing false-green CI
# Environment: code generation agents producing test suites for review or direct use
```

---

### Option 2 — Post-generation assertion linter

Scan generated test code for vacuous assertion patterns and reject or flag them.

```python
import anthropic
import ast
import re

client = anthropic.Anthropic(api_key="sk-live-...")

VACUOUS_PATTERNS = [
    (r'\bassert\s+True\b', "assert True — literal no-op"),
    (r'\bassert\s+result\s*$', "assert result — truthy check only, no expected value"),
    (r'\bassert\s+\w+\s+is\s+not\s+None\s*$', "assert X is not None — presence only, no value check"),
    (r'def\s+test_\w+\([^)]*\)\s*:\s*\n\s+\w+\([^)]*\)\s*\n\s*$', "test body makes no assertion"),
]


def lint_assertions(test_code: str) -> list[str]:
    """Find vacuous assertion patterns in generated test code."""
    issues = []
    lines = test_code.splitlines()

    # Check line-level patterns
    for i, line in enumerate(lines, 1):
        for pattern, description in VACUOUS_PATTERNS[:3]:
            if re.search(pattern, line.strip()):
                issues.append(f"Line {i}: {description} → `{line.strip()}`")

    # Check for test functions with no assertions using AST
    try:
        tree = ast.parse(test_code)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                has_assertion = any(
                    isinstance(child, ast.Assert) or
                    (isinstance(child, ast.Expr) and
                     isinstance(child.value, ast.Call) and
                     isinstance(child.value.func, ast.Attribute) and
                     child.value.func.attr.startswith("assert"))
                    for child in ast.walk(node)
                )
                if not has_assertion:
                    issues.append(f"Function `{node.name}`: no assertion found — test always passes")
    except SyntaxError as e:
        issues.append(f"SyntaxError parsing test code: {e}")

    return issues


def generate_quality_tests(function_code: str) -> str:
    system = """Write pytest tests with concrete expected values.
Every test MUST assert an exact value using == or assertRaises.
Never use bare `assert result` or `assert True`."""

    for attempt in range(3):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": f"Write tests:\n```python\n{function_code}\n```"}]
        )
        code = response.content[0].text.strip()

        # Extract code block if wrapped in markdown
        if "```python" in code:
            code = code.split("```python")[1].split("```")[0].strip()

        issues = lint_assertions(code)

        if not issues:
            print(f"[lint] Passed on attempt {attempt + 1}")
            return code

        print(f"[lint] Attempt {attempt + 1} — {len(issues)} issue(s):")
        for issue in issues:
            print(f"  • {issue}")

        system += f"\n\nPrevious attempt had these assertion quality issues:\n" + "\n".join(issues)
        system += "\n\nFix ALL of them. Every assertion must compare against a specific expected value."

    return code  # Return best effort


func = "def multiply(a: int, b: int) -> int:\n    return a * b"
result = generate_quality_tests(func)
print(result)

# Expected Token Savings: linter catches vacuous tests before they enter CI; saves debugging time
# Environment: CI gate for AI-generated test suites; code review automation
```

---

### Option 3 — Provide input/output examples and require assertion matching

Supply concrete input/output pairs from the function's docstring or doctest, and instruct the model to derive assertions from them.

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def extract_doctests(function_code: str) -> list[tuple[str, str]]:
    """Extract >>> input and expected output pairs from docstrings."""
    pairs = []
    lines = function_code.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith(">>>"):
            call = line[3:].strip()
            if i + 1 < len(lines):
                expected = lines[i + 1].strip()
                if expected and not expected.startswith(">>>"):
                    pairs.append((call, expected))
        i += 1
    return pairs


def generate_tests_from_examples(function_code: str) -> str:
    examples = extract_doctests(function_code)

    example_block = ""
    if examples:
        example_block = "\n\nKnown input/output pairs to derive assertions from:\n"
        for call, expected in examples:
            example_block += f"  {call} → {expected}\n"
        example_block += "\nEach of these MUST appear as an explicit == assertion in the tests."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="""Write pytest tests. Derive concrete assertions from the documented examples.
If examples are given, each must appear as: assert function_call(args) == exact_expected_value
Add edge case tests: empty input, boundary values, invalid types (expect ValueError/TypeError).""",
        messages=[{
            "role": "user",
            "content": f"Write tests for:\n```python\n{function_code}\n```{example_block}"
        }]
    )
    return response.content[0].text.strip()


func_with_doctests = '''
def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert Celsius to Fahrenheit.

    >>> celsius_to_fahrenheit(0)
    32.0
    >>> celsius_to_fahrenheit(100)
    212.0
    >>> celsius_to_fahrenheit(-40)
    -40.0
    """
    return celsius * 9 / 5 + 32
'''

tests = generate_tests_from_examples(func_with_doctests)
print(tests)

# Expected Token Savings: doctest-derived assertions are provably correct → no hallucinated expected values
# Environment: agents generating tests for well-documented functions; TDD workflows
```

---

### Option 4 — Execute generated tests and verify they fail on a broken implementation

Generate tests, then run them against a deliberately broken version of the function. If all tests still pass, the tests are vacuous.

```python
import anthropic
import subprocess
import tempfile
import textwrap
import os

client = anthropic.Anthropic(api_key="sk-live-...")


def make_broken_version(function_code: str) -> str:
    """Create a broken version of the function for mutation testing."""
    # Simple mutations: negate returns, change operators
    broken = function_code
    # Replace arithmetic operators to introduce bugs
    replacements = [
        (" + ", " - "),
        (" * ", " / "),
        (" - ", " + "),
        ("return True", "return False"),
        ("return False", "return True"),
    ]
    for original, mutant in replacements:
        if original in broken:
            broken = broken.replace(original, mutant, 1)
            break
    return broken


def run_tests(function_code: str, test_code: str) -> tuple[bool, str]:
    """Run test_code against function_code. Returns (passed, output)."""
    full_code = textwrap.dedent(f"""
{function_code}

{test_code}
""")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(full_code)
        path = f.name

    try:
        result = subprocess.run(
            ["python", "-m", "pytest", path, "-v", "--tb=short"],
            capture_output=True, text=True, timeout=30
        )
        passed = result.returncode == 0
        return passed, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "Test run timed out"
    finally:
        os.unlink(path)


def generate_mutation_validated_tests(function_code: str) -> str:
    system = """Write pytest tests with exact == assertions. Tests must be specific enough to fail if the implementation is wrong."""

    for attempt in range(3):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": f"Write tests:\n```python\n{function_code}\n```"}]
        )
        raw = response.content[0].text.strip()
        test_code = raw.split("```python")[-1].split("```")[0].strip() if "```" in raw else raw

        # Step 1: Tests must pass on correct implementation
        correct_pass, correct_out = run_tests(function_code, test_code)
        if not correct_pass:
            print(f"[mutation] Attempt {attempt+1}: Tests fail on correct code — regenerating")
            system += "\n\nThe generated tests failed even on the correct implementation. Fix them."
            continue

        # Step 2: Tests must FAIL on broken implementation
        broken_code = make_broken_version(function_code)
        broken_pass, broken_out = run_tests(broken_code, test_code)
        if broken_pass:
            print(f"[mutation] Attempt {attempt+1}: Tests pass on BROKEN code — assertions are vacuous")
            system += "\n\nTests passed on a deliberately broken implementation. Make assertions more specific."
            continue

        print(f"[mutation] Attempt {attempt+1}: Tests correctly pass on correct and fail on broken code")
        return test_code

    return test_code  # Best effort


func = "def clamp(value: float, lo: float, hi: float) -> float:\n    return max(lo, min(value, hi))"
tests = generate_mutation_validated_tests(func)
print(tests)

# Expected Token Savings: mutation testing proves test quality before CI; catches vacuous suites early
# Environment: high-stakes code generation; teams requiring verified test quality gates
```

---

### Option 5 — Structured output: model returns assertions as data, not code

Ask the model to return test cases as structured JSON (inputs + expected outputs), then generate the test code deterministically.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def generate_test_cases_json(function_code: str) -> list[dict]:
    """Ask model to return test cases as structured data, not code."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system="""Return test cases as a JSON array. Each element must have:
{
  "name": "descriptive test name",
  "args": [positional, args],
  "kwargs": {"key": "value"},
  "expected": <exact return value>,
  "raises": null or "ExceptionClassName",
  "reason": "what bug this catches"
}

Return ONLY valid JSON. No markdown, no explanation.""",
        messages=[{
            "role": "user",
            "content": f"Generate test cases for:\n```python\n{function_code}\n```"
        }]
    )

    raw = response.content[0].text.strip()
    # Strip markdown if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


def render_pytest(function_name: str, test_cases: list[dict]) -> str:
    """Deterministically render test cases as pytest code — no hallucination possible."""
    lines = ["import pytest", ""]
    for tc in test_cases:
        safe_name = tc["name"].replace(" ", "_").replace("-", "_").lower()
        lines.append(f"def test_{safe_name}():")
        lines.append(f"    # {tc['reason']}")

        args_repr = ", ".join(repr(a) for a in tc.get("args", []))
        kwargs_repr = ", ".join(f"{k}={repr(v)}" for k, v in tc.get("kwargs", {}).items())
        call_args = ", ".join(filter(None, [args_repr, kwargs_repr]))
        call = f"{function_name}({call_args})"

        if tc.get("raises"):
            lines.append(f"    with pytest.raises({tc['raises']}):")
            lines.append(f"        {call}")
        else:
            lines.append(f"    assert {call} == {repr(tc['expected'])}")
        lines.append("")

    return "\n".join(lines)


func_code = """
def truncate(text: str, max_length: int, suffix: str = "...") -> str:
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix
"""

test_cases = generate_test_cases_json(func_code)
print("Test cases JSON:")
print(json.dumps(test_cases, indent=2))

test_code = render_pytest("truncate", test_cases)
print("\nGenerated pytest code:")
print(test_code)

# Expected Token Savings: structured data separates "what to test" from "how to code it" → no vacuous assertions possible
# Environment: automated test generation pipelines; agents producing tests for code review
```

---

### Option 6 — Behaviour-driven test generation with Given/When/Then structure

Force the model to articulate expected behaviour before writing assertions, reducing vacuous outputs.

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

BDD_SYSTEM = """You are a BDD test engineer. For each test scenario:

1. Write a docstring with Given/When/Then:
   - Given: the input state
   - When: the function is called
   - Then: the exact expected output (must be a concrete value)

2. Derive the assertion directly from the Then clause.
3. The Then clause must specify an exact value, not a vague description.

Template:
    def test_<scenario>():
        \"\"\"Given <setup>, When <action>, Then <exact value>.\"\"\"
        # Given
        <setup code>
        # When
        result = <function call>
        # Then
        assert result == <exact value from Then clause>  # <reason>

Never write `assert result` or `assert True`. Always compare to a concrete expected value."""


def generate_bdd_tests(function_code: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=BDD_SYSTEM,
        messages=[{"role": "user", "content": f"Write BDD tests:\n```python\n{function_code}\n```"}]
    )
    raw = response.content[0].text.strip()

    # Validate: look for concrete == assertions
    assertions = re.findall(r'assert\s+\w[^=\n]*==\s*\S+', raw)
    vacuous = re.findall(r'assert\s+(?:True|result\s*$|\w+\s+is\s+not\s+None\s*$)', raw, re.MULTILINE)

    print(f"[bdd] Found {len(assertions)} concrete assertions, {len(vacuous)} vacuous assertions")
    if vacuous:
        print(f"[bdd] WARNING: vacuous assertions: {vacuous}")

    return raw


func = """
def word_count(text: str) -> dict[str, int]:
    \"\"\"Count occurrences of each word (case-insensitive).\"\"\"
    counts = {}
    for word in text.lower().split():
        counts[word] = counts.get(word, 0) + 1
    return counts
"""

tests = generate_bdd_tests(func)
print(tests)

# Expected Token Savings: GWT structure anchors expected values to behaviour description → fewer vacuous assertions
# Environment: teams practising BDD; agents generating tests from user story acceptance criteria
```

---

## Comparison

| Option | Prevents Vacuous | Catches Missing Assertions | Auto-Validates | Complexity |
|--------|-----------------|--------------------------|----------------|------------|
| 1 | Prompt rules | Partially | No | Low |
| 2 | Regex + AST lint | Yes | Yes (retry) | Low |
| 3 | Doctest anchoring | Yes | No | Low |
| 4 | Mutation testing | Yes | Yes (execute) | High |
| 5 | Structured JSON | Yes (design) | No | Medium |
| 6 | BDD Given/When/Then | Yes | Partial | Low |

**Recommended starting point:** Option 2 (assertion linter) as a CI gate — it catches the most common vacuous patterns with a simple regex + AST scan and costs nothing to run. Combine with Option 1's system prompt to reduce regeneration rate. Use Option 4 (mutation testing) for high-stakes functions where test quality is critical.
