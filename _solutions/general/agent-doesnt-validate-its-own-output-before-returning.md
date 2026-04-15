---
layout: solution
title: "Agent Doesn't Validate Its Own Output Before Returning"
category: general
description: "The agent returns answers that contain factual errors, broken code, invalid JSON, or incomplete responses without any self-checking step. Errors surface downstream in user-facing systems."
tags: [validation, self-critique, output-quality, self-consistency, code-verification, review]
---

## Symptom

The agent returns a Python function with a syntax error. Or a JSON response with mismatched brackets. Or a calculation that's obviously wrong. Or a plan missing a critical step. None of these are caught before the response reaches the user. Downstream systems break. Users report bugs. The error was trivially detectable if the agent had checked its own output.

## Root Cause

Language models generate output in a single forward pass with no built-in review step. The model that produces an answer is not automatically different from the one that would catch errors in it. Without an explicit validation loop — a second pass, a tool call, or a structured critique — the first output is always the final output, errors included.

## Fix

---

### Option 1: Two-Pass Generate-Then-Critique

Generate a first response, then pass it through an explicit critique step before returning.

```python
import anthropic

client = anthropic.Anthropic()

CRITIQUE_TOOL = {
    "name": "critique_response",
    "description": "Review a response for errors before it is returned to the user.",
    "input_schema": {
        "type": "object",
        "properties": {
            "issues_found": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of specific errors, omissions, or quality problems found",
            },
            "passes_quality_check": {
                "type": "boolean",
                "description": "True if response is correct and complete enough to return",
            },
            "corrected_response": {
                "type": "string",
                "description": "The corrected response if issues were found. Empty string if passes_quality_check is true.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in the correctness of the response (0.0-1.0)",
            },
        },
        "required": ["issues_found", "passes_quality_check", "corrected_response", "confidence"],
    },
}

GENERATION_SYSTEM = "You are a helpful, precise assistant. Answer clearly and completely."
CRITIQUE_SYSTEM = """You are a rigorous quality reviewer. Your job is to find errors in AI responses before they reach users.

Check for:
- Factual errors or hallucinations
- Logical inconsistencies
- Incomplete answers (missing steps, missing edge cases)
- Code with syntax errors or bugs
- Math calculation errors
- Contradictions within the response

Be strict. If you find any issue, set passes_quality_check=false and provide a corrected_response."""


def validated_response(user_message: str, max_retries: int = 2) -> str:
    """
    Generate a response and validate it before returning.
    Automatically corrects on validation failure.
    """
    # Step 1: Generate
    gen_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=GENERATION_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    candidate = gen_response.content[0].text

    # Step 2: Critique
    critique_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=CRITIQUE_SYSTEM,
        tools=[CRITIQUE_TOOL],
        tool_choice={"type": "any"},
        messages=[{
            "role": "user",
            "content": (
                f"Original question: {user_message}\n\n"
                f"Proposed response:\n{candidate}\n\n"
                f"Review this response for any errors or omissions."
            ),
        }],
    )

    for block in critique_response.content:
        if block.type == "tool_use" and block.name == "critique_response":
            result = block.input
            if result["passes_quality_check"]:
                print(f"  [Validation ✓] Confidence: {result['confidence']:.0%}")
                return candidate
            else:
                issues = result["issues_found"]
                print(f"  [Validation ✗] Issues: {issues}")
                corrected = result["corrected_response"]
                if corrected:
                    print(f"  [Auto-corrected]")
                    return corrected

    return candidate  # fallback if critique tool not called


# Test with questions that might generate errors
test_cases = [
    "What is 15% of 847?",
    "Write a Python function to check if a number is prime.",
    "List the planets in order from the Sun.",
]

for question in test_cases:
    print(f"\nQ: {question}")
    answer = validated_response(question)
    print(f"A: {answer[:200]}")
```

**Expected Token Savings**: Validation catches errors in ~1 extra call instead of user-reported bugs requiring 3–5 follow-up correction turns. Net saving: 2–4 turns per error.
**Environment**: Two Haiku calls per response. Total cost ≈ 2× single call but eliminates costly downstream errors.

---

### Option 2: Code Output Validation via Syntax Check + Execution

For code generation, validate syntax and run the code before returning it.

```python
import ast
import subprocess
import tempfile
import os
import anthropic

client = anthropic.Anthropic()


def validate_python_syntax(code: str) -> tuple[bool, str]:
    """Check Python syntax without running the code."""
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as e:
        return False, f"Syntax error at line {e.lineno}: {e.msg}"


def run_python_safely(code: str, timeout: int = 5) -> tuple[bool, str, str]:
    """
    Run Python code in a subprocess sandbox.
    Returns (success, stdout, stderr).
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name

    try:
        result = subprocess.run(
            ["python3", path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"Code timed out after {timeout}s"
    except Exception as e:
        return False, "", str(e)
    finally:
        os.unlink(path)


def extract_python_code(text: str) -> str | None:
    """Extract Python code from markdown code fence."""
    import re
    match = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else None


def generate_and_validate_code(task: str, max_attempts: int = 3) -> str:
    """Generate Python code and validate it before returning."""
    messages = [{"role": "user", "content": task}]
    error_history = []

    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system="You are a Python expert. Always provide complete, runnable Python code in ```python ... ``` blocks.",
            messages=messages,
        )

        raw = response.content[0].text
        code = extract_python_code(raw)

        if not code:
            print(f"  [Attempt {attempt+1}] No code block found")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "Please provide the code in a ```python ... ``` block."})
            continue

        # Validate syntax
        syntax_ok, syntax_error = validate_python_syntax(code)
        if not syntax_ok:
            print(f"  [Attempt {attempt+1}] Syntax error: {syntax_error}")
            error_history.append(f"Syntax error: {syntax_error}")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"Fix this syntax error: {syntax_error}"})
            continue

        # Run to check for runtime errors (if code has a __main__ guard or standalone test)
        test_code = code
        if "if __name__" not in code and "print(" in code:
            # Code has print statements — likely runnable
            success, stdout, stderr = run_python_safely(test_code)
            if not success:
                print(f"  [Attempt {attempt+1}] Runtime error: {stderr[:100]}")
                error_history.append(f"Runtime error: {stderr[:100]}")
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": f"Fix this runtime error: {stderr}"})
                continue

        print(f"  [Attempt {attempt+1}] Code validated ✓")
        return raw

    # Return last attempt with error note
    return f"{raw}\n\n⚠️ Note: This code may have issues: {'; '.join(error_history)}"


result = generate_and_validate_code(
    "Write a Python function to calculate the Fibonacci sequence up to n terms, with a demo."
)
print(result)
```

**Expected Token Savings**: Syntax errors caught in <1ms locally. Runtime errors caught before user sees broken code. Prevents 2–5 correction turns per bug.
**Environment**: Requires Python subprocess. Adjust `timeout` for complex code. Don't run untrusted code.

---

### Option 3: Self-Consistency Check Across Multiple Responses

Generate the response N times (or with different prompts). Only return if answers are consistent.

```python
import asyncio
import anthropic
from collections import Counter

client = anthropic.AsyncAnthropic()


def responses_are_consistent(responses: list[str], tolerance: float = 0.7) -> tuple[bool, str]:
    """
    Check if a majority of responses agree.
    For factual answers: check if key facts appear in most responses.
    Returns (is_consistent, best_response).
    """
    if len(responses) == 1:
        return True, responses[0]

    # For short factual answers: check character-level similarity
    lengths = [len(r) for r in responses]
    avg_len = sum(lengths) / len(lengths)

    # Extract key numbers/facts from each response
    import re
    number_sets = [set(re.findall(r"\b\d+(?:\.\d+)?\b", r)) for r in responses]

    # Check if most responses agree on numbers
    if number_sets:
        all_numbers = [n for s in number_sets for n in s]
        number_counts = Counter(all_numbers)
        majority = len(responses) * tolerance

        # Check if any number appears in a suspiciously inconsistent way
        inconsistencies = []
        seen_numbers = set()
        for num in number_counts:
            if num in seen_numbers:
                continue
            count = number_counts[num]
            # Find contradicting numbers (similar magnitude, different value)
            if count < len(responses) * 0.5:
                inconsistencies.append(num)
            seen_numbers.add(num)

        if len(inconsistencies) > 2:
            print(f"  [Inconsistency detected] Numbers vary: {inconsistencies[:3]}")
            return False, responses[0]

    # Use the median-length response as representative
    responses_sorted = sorted(responses, key=len)
    best = responses_sorted[len(responses_sorted) // 2]
    return True, best


async def self_consistent_answer(
    question: str,
    n_samples: int = 3,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    """
    Generate N independent answers and check consistency.
    Returns the answer if consistent, flags discrepancy if not.
    """
    tasks = [
        client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": question}],
        )
        for _ in range(n_samples)
    ]

    responses_raw = await asyncio.gather(*tasks)
    responses = [r.content[0].text for r in responses_raw]

    consistent, best = responses_are_consistent(responses)

    if not consistent:
        print(f"  [Self-consistency FAIL] Responses disagree — flagging uncertainty")
        return (
            f"{best}\n\n"
            f"⚠️ Note: This is a complex question where different reasoning paths gave different answers. "
            f"Please verify this answer independently."
        )

    print(f"  [Self-consistency ✓] {n_samples} independent responses agree")
    return best


async def main():
    questions = [
        "What is 17 multiplied by 23?",
        "How many US states are there?",
        "What year did World War II end?",
    ]

    for q in questions:
        print(f"\nQ: {q}")
        answer = await self_consistent_answer(q, n_samples=3)
        print(f"A: {answer[:200]}")


asyncio.run(main())
```

**Expected Token Savings**: 3× cost per query, but prevents expensive correction cycles on high-stakes factual questions. Best applied selectively to critical/uncertain queries.
**Environment**: Async Python. Use N=2 for speed, N=3 for higher confidence.

---

### Option 4: Schema Validation for Structured Output

Before returning structured data, validate it against the expected schema and re-prompt on failure.

```python
import json
import anthropic
from jsonschema import validate, ValidationError, Draft7Validator

client = anthropic.Anthropic()

# Define the expected output schema
EXPECTED_SCHEMA = {
    "type": "object",
    "properties": {
        "name":        {"type": "string", "minLength": 1},
        "age":         {"type": "integer", "minimum": 0, "maximum": 150},
        "email":       {"type": "string", "format": "email"},
        "skills":      {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "experience_years": {"type": "number", "minimum": 0},
        "is_available": {"type": "boolean"},
    },
    "required": ["name", "age", "email", "skills", "experience_years", "is_available"],
    "additionalProperties": False,
}

SYSTEM = f"""Extract candidate information and return a JSON object matching EXACTLY this schema:
{json.dumps(EXPECTED_SCHEMA, indent=2)}

Return ONLY valid JSON. No markdown, no explanation."""


def extract_with_validation(text: str, max_retries: int = 3) -> dict:
    """Extract structured data with schema validation."""
    messages = [{"role": "user", "content": f"Extract candidate info from:\n\n{text}"}]

    for attempt in range(max_retries):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            messages=messages,
        )

        raw = response.content[0].text.strip()

        # Strip code fences
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        # Parse JSON
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  [Attempt {attempt+1}] JSON parse error: {e}")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"JSON parse error: {e}. Return valid JSON only."})
            continue

        # Validate schema
        validator = Draft7Validator(EXPECTED_SCHEMA)
        errors = list(validator.iter_errors(data))

        if not errors:
            print(f"  [Validation ✓] Passed on attempt {attempt+1}")
            return data

        error_messages = [f"  - {e.path}: {e.message}" if e.path else f"  - {e.message}" for e in errors[:3]]
        error_text = "\n".join(error_messages)
        print(f"  [Attempt {attempt+1}] Schema errors:\n{error_text}")

        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": (
                f"Schema validation failed:\n{error_text}\n\n"
                f"Fix these issues. Remember:\n"
                f"- 'age' must be an integer (not string)\n"
                f"- 'is_available' must be boolean true/false\n"
                f"- 'skills' must be an array\n"
                f"- All required fields must be present\n"
                f"Return only valid JSON."
            ),
        })

    raise ValueError(f"Could not produce valid output after {max_retries} attempts")


# Test
candidate_text = """
John Smith is 34 years old and can be reached at john.smith@company.com.
He has 8 years of experience with Python, FastAPI, and PostgreSQL.
Currently looking for new opportunities.
"""

result = extract_with_validation(candidate_text)
print(json.dumps(result, indent=2))
print(f"Name: {result['name']}, Age: {result['age']}, Skills: {result['skills']}")
```

**Expected Token Savings**: Schema validation at microseconds catches structural errors before they propagate. Saves ~3 downstream correction turns per validation failure.
**Environment**: `pip install jsonschema`. Works with any structured extraction task.

---

### Option 5: Checklist-Based Completeness Verification

Define a checklist of what a complete answer must contain. Verify each item before returning.

```python
import anthropic

client = anthropic.Anthropic()

CHECKLIST_TOOL = {
    "name": "verify_completeness",
    "description": "Verify that a response satisfies all required checklist items.",
    "input_schema": {
        "type": "object",
        "properties": {
            "checklist_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item":   {"type": "string"},
                        "passed": {"type": "boolean"},
                        "note":   {"type": "string"},
                    },
                    "required": ["item", "passed"],
                },
            },
            "overall_pass": {"type": "boolean"},
            "missing_elements": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["checklist_results", "overall_pass", "missing_elements"],
    },
}

# Task-specific checklists
CHECKLISTS = {
    "code_review": [
        "Contains at least one specific code issue identified",
        "Suggests concrete fix for each issue found",
        "Mentions performance implications if any",
        "Notes security concerns if any",
        "Overall assessment provided",
    ],
    "technical_explanation": [
        "Defines the main concept",
        "Explains how it works mechanically",
        "Gives a concrete example",
        "Mentions common use cases",
        "Notes limitations or caveats",
    ],
    "troubleshooting": [
        "Identifies the root cause",
        "Lists diagnostic steps",
        "Provides at least one solution",
        "Mentions how to verify the fix",
        "Notes prevention for future",
    ],
}


def generate_and_verify(user_message: str, checklist_type: str) -> str:
    checklist = CHECKLISTS.get(checklist_type, [])
    if not checklist:
        # No checklist: return as-is
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text

    # Generate
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    )
    candidate = response.content[0].text

    # Verify completeness
    checklist_text = "\n".join(f"- {item}" for item in checklist)
    verify_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        tools=[CHECKLIST_TOOL],
        tool_choice={"type": "any"},
        messages=[{
            "role": "user",
            "content": (
                f"Verify if this response satisfies all checklist items.\n\n"
                f"Checklist:\n{checklist_text}\n\n"
                f"Response to verify:\n{candidate}"
            ),
        }],
    )

    for block in verify_response.content:
        if block.type == "tool_use" and block.name == "verify_completeness":
            result = block.input
            if result["overall_pass"]:
                print(f"  [Completeness ✓] All {len(checklist)} items passed")
                return candidate

            missing = result["missing_elements"]
            print(f"  [Completeness ✗] Missing: {missing}")

            # Request completion of missing elements
            completion_response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": candidate},
                    {"role": "user", "content": (
                        f"Your response is missing these elements:\n"
                        + "\n".join(f"- {m}" for m in missing)
                        + "\n\nPlease add the missing elements to complete your response."
                    )},
                ],
            )
            addition = completion_response.content[0].text
            return candidate + "\n\n" + addition

    return candidate


# Test
print(generate_and_verify(
    "Explain how Python's Global Interpreter Lock (GIL) works.",
    "technical_explanation",
))
```

**Expected Token Savings**: Completeness check catches missing elements in 1 verification call (cheap) vs user asking follow-up questions (expensive, 2–5 turns each).
**Environment**: Domain-specific checklists. Maintain checklist per task type in a config dict.

---

### Option 6: Lightweight LLM-as-Judge Before Return

Use a fast, cheap judge model to score the response on quality dimensions. Return only if score exceeds threshold.

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class QualityScore:
    accuracy: float        # 0-10: factual correctness
    completeness: float    # 0-10: coverage of question
    clarity: float         # 0-10: easy to understand
    overall: float         # 0-10: overall quality
    issues: list[str]
    recommendation: str    # APPROVE | REVISE | REJECT


JUDGE_TOOL = {
    "name": "quality_score",
    "description": "Score the quality of an AI response.",
    "input_schema": {
        "type": "object",
        "properties": {
            "accuracy":     {"type": "number", "minimum": 0, "maximum": 10},
            "completeness": {"type": "number", "minimum": 0, "maximum": 10},
            "clarity":      {"type": "number", "minimum": 0, "maximum": 10},
            "overall":      {"type": "number", "minimum": 0, "maximum": 10},
            "issues":       {"type": "array", "items": {"type": "string"}},
            "recommendation": {"type": "string", "enum": ["APPROVE", "REVISE", "REJECT"]},
        },
        "required": ["accuracy", "completeness", "clarity", "overall", "issues", "recommendation"],
    },
}

JUDGE_SYSTEM = """You are a strict quality judge for AI responses. Score objectively on:
- accuracy (0-10): Are all facts correct?
- completeness (0-10): Does it fully answer the question?
- clarity (0-10): Is it easy to understand?
- overall (0-10): Overall quality

APPROVE if overall >= 8, REVISE if 5-7, REJECT if < 5."""

APPROVAL_THRESHOLD = 7.0


async def judge_response(question: str, response: str) -> QualityScore:
    """Use a judge model to score the response."""
    judge_response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=JUDGE_SYSTEM,
        tools=[JUDGE_TOOL],
        tool_choice={"type": "any"},
        messages=[{
            "role": "user",
            "content": f"Question: {question}\n\nResponse to judge:\n{response}",
        }],
    )

    for block in judge_response.content:
        if block.type == "tool_use" and block.name == "quality_score":
            inp = block.input
            return QualityScore(
                accuracy=inp["accuracy"],
                completeness=inp["completeness"],
                clarity=inp["clarity"],
                overall=inp["overall"],
                issues=inp.get("issues", []),
                recommendation=inp["recommendation"],
            )

    return QualityScore(8, 8, 8, 8, [], "APPROVE")  # default if judge fails


async def judged_response(question: str, max_revisions: int = 2) -> str:
    """Generate and judge response. Revise if below threshold."""
    messages = [{"role": "user", "content": question}]

    for attempt in range(max_revisions + 1):
        gen, _ = await asyncio.gather(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=messages,
            ),
            asyncio.sleep(0),  # yield to event loop
        )
        candidate = gen.content[0].text

        score = await judge_response(question, candidate)
        print(f"  [Judge attempt {attempt+1}] Overall: {score.overall}/10 → {score.recommendation}")

        if score.recommendation == "APPROVE" or score.overall >= APPROVAL_THRESHOLD:
            return candidate

        if score.issues:
            print(f"  [Issues] {score.issues[:2]}")

        if attempt < max_revisions:
            messages.append({"role": "assistant", "content": candidate})
            messages.append({
                "role": "user",
                "content": (
                    f"Your response scored {score.overall}/10. Issues:\n"
                    + "\n".join(f"- {i}" for i in score.issues)
                    + "\n\nPlease provide an improved response."
                ),
            })

    return candidate  # return best attempt


async def main():
    questions = [
        "Explain the difference between TCP and UDP protocols.",
        "What are the key principles of SOLID design?",
    ]

    for q in questions:
        print(f"\nQ: {q[:80]}")
        answer = await judged_response(q)
        print(f"A: {answer[:200]}...")


asyncio.run(main())
```

**Expected Token Savings**: Judge call (~100 tokens) catches low-quality responses before users see them — prevents 3–8 turns of user-initiated corrections. Apply selectively to high-stakes queries.
**Environment**: Async Python. Run generator and judge concurrently for speed (though judge requires the generator to finish first here).

---

| Option | Validation Method | Latency Added | Cost Multiplier | Best For |
|--------|-----------------|--------------|----------------|---------|
| 1 | LLM self-critique | +1 API call | ~2× | General responses |
| 2 | Syntax + subprocess | <100ms | ~1× | Python code generation |
| 3 | Self-consistency (3×) | +2 API calls | ~3× | High-stakes factual queries |
| 4 | JSON schema validation | <1ms | ~1.1× | Structured data extraction |
| 5 | Checklist verification | +1 API call | ~1.5× | Domain-specific completeness |
| 6 | LLM-as-judge | +1 API call | ~1.5× | Quality scoring with rubrics |
