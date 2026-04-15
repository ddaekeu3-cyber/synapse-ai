---
layout: solution
title: "Agent Doesn't Use Chain-of-Thought for Math and Logic Problems"
category: prompt-engineering
description: "The agent asks Claude to solve multi-step math, logic, or reasoning problems directly. Without structured chain-of-thought prompting, the model skips intermediate steps and produces wrong answers confidently, especially for arithmetic, algebra, and logical deductions."
tags: [prompt-engineering, chain-of-thought, reasoning, math, logic, structured-output, anthropic]
---

# Agent Doesn't Use Chain-of-Thought for Math and Logic Problems

## Problem

A user asks the agent to calculate compound interest or solve a multi-step word problem. The prompt says "Answer concisely." Claude produces a number — sometimes right, often wrong — because it's forced to suppress the intermediate reasoning that leads to correct answers. Chain-of-thought prompting instructs the model to show its work, dramatically improving accuracy on multi-step problems.

## Solutions

### Option 1: Explicit Step-by-Step Instruction in System Prompt

```python
# prompts/cot_math.py
"""
Add explicit chain-of-thought instructions to the system prompt.
The model must show each step before stating the final answer.
"""
import anthropic
import re


MATH_SYSTEM_PROMPT = """You are a precise mathematical reasoning assistant.

For ALL math, logic, or multi-step reasoning problems:
1. Break the problem into numbered steps.
2. Show your calculation or reasoning at each step.
3. State the final answer clearly on a new line prefixed with "ANSWER:".

Example format:
Step 1: Identify what is being asked...
Step 2: [calculation]
Step 3: [calculation]
ANSWER: [final result with units]

Never skip steps. Never give the answer before showing the work."""


def solve_with_cot(problem: str) -> dict:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=MATH_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": problem}],
    )
    text = response.content[0].text

    # Extract the final answer
    answer_match = re.search(r"ANSWER:\s*(.+)", text, re.IGNORECASE)
    final_answer = answer_match.group(1).strip() if answer_match else ""

    # Count steps for quality check
    steps = re.findall(r"Step \d+", text, re.IGNORECASE)

    return {
        "full_reasoning": text,
        "final_answer": final_answer,
        "step_count": len(steps),
        "tokens_used": response.usage.input_tokens + response.usage.output_tokens,
    }


# ── Comparison: with vs without CoT ───────────────────────────────────────────

def solve_without_cot(problem: str) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system="Answer math questions briefly. Give only the final number.",
        messages=[{"role": "user", "content": problem}],
    )
    return response.content[0].text


if __name__ == "__main__":
    problem = (
        "A store sells apples for $1.20 each and oranges for $0.80 each. "
        "If Sarah buys 7 apples and 5 oranges, and pays with a $20 bill, "
        "how much change does she receive?"
    )
    result = solve_with_cot(problem)
    print(f"Steps shown: {result['step_count']}")
    print(f"Answer: {result['final_answer']}")
    print(f"Tokens: {result['tokens_used']}")
```

**Expected Token Savings:** -20 to -40% (more tokens used, but accuracy improvement justifies it)
**Environment:** `pip install anthropic`

---

### Option 2: Zero-Shot CoT with "Let's think step by step"

```python
# prompts/zero_shot_cot.py
"""
Zero-shot chain-of-thought: append "Let's think step by step" to the user message.
No examples needed. Effective for arithmetic, word problems, and logic puzzles.
"""
import re
import anthropic


def solve_zero_shot_cot(
    problem: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
) -> dict:
    """
    Use zero-shot CoT by appending the trigger phrase to the user message.
    Then parse the final answer from the reasoning output.
    """
    client = anthropic.Anthropic()

    # Step 1: Get the reasoning
    reasoning_response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": f"{problem}\n\nLet's think step by step.",
        }],
    )
    reasoning = reasoning_response.content[0].text

    # Step 2: Extract final answer (two-step CoT extraction)
    extraction_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[
            {
                "role": "user",
                "content": f"{problem}\n\nLet's think step by step.",
            },
            {
                "role": "assistant",
                "content": reasoning,
            },
            {
                "role": "user",
                "content": "Therefore, the final answer is:",
            },
        ],
    )
    final_answer = extraction_response.content[0].text.strip()

    return {
        "reasoning": reasoning,
        "final_answer": final_answer,
        "reasoning_tokens": reasoning_response.usage.output_tokens,
        "extraction_tokens": extraction_response.usage.output_tokens,
    }


def solve_math_problems(problems: list[str]) -> list[dict]:
    """Solve multiple math problems with zero-shot CoT."""
    return [solve_zero_shot_cot(p) for p in problems]


if __name__ == "__main__":
    result = solve_zero_shot_cot(
        "A train travels at 60 mph for 2.5 hours, then at 80 mph for 1.5 hours. "
        "What is the total distance traveled?"
    )
    print(f"Reasoning:\n{result['reasoning']}\n")
    print(f"Final answer: {result['final_answer']}")
```

**Expected Token Savings:** ~30% vs few-shot CoT (no examples needed)
**Environment:** `pip install anthropic`

---

### Option 3: Structured CoT with XML Tags

```python
# prompts/structured_cot.py
"""
Use XML tags to structure the chain-of-thought output.
The <thinking> section is separate from <answer>, making it easy to
parse the final answer programmatically while preserving full reasoning.
"""
import re
import anthropic
from typing import Optional


STRUCTURED_COT_PROMPT = """You are a logical reasoning assistant.

When solving problems, use this exact XML structure:

<thinking>
[Work through the problem step by step here]
[Show all calculations, logical deductions, and intermediate values]
[Check your work before concluding]
</thinking>

<answer>
[Final answer only — no reasoning here]
</answer>

Always complete both sections."""


def extract_structured_cot(text: str) -> tuple[str, str]:
    """Extract thinking and answer from structured CoT response."""
    thinking_match = re.search(r"<thinking>(.*?)</thinking>", text, re.DOTALL)
    answer_match = re.search(r"<answer>(.*?)</answer>", text, re.DOTALL)

    thinking = thinking_match.group(1).strip() if thinking_match else ""
    answer = answer_match.group(1).strip() if answer_match else text.strip()

    return thinking, answer


def solve_structured(problem: str, model: str = "claude-sonnet-4-6") -> dict:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=STRUCTURED_COT_PROMPT,
        messages=[{"role": "user", "content": problem}],
    )
    full_text = response.content[0].text
    thinking, answer = extract_structured_cot(full_text)

    return {
        "thinking": thinking,
        "answer": answer,
        "thinking_length": len(thinking),
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


# ── Batch solver with accuracy tracking ───────────────────────────────────────

def evaluate_accuracy(
    problems: list[dict],  # Each: {"problem": str, "expected_answer": str}
    model: str = "claude-sonnet-4-6",
) -> dict:
    """Run a set of problems and check how many answers match expected."""
    correct = 0
    results = []

    for item in problems:
        result = solve_structured(item["problem"], model)
        # Normalize: strip whitespace and lowercase for comparison
        got = result["answer"].strip().lower()
        expected = item["expected_answer"].strip().lower()
        is_correct = expected in got or got == expected
        if is_correct:
            correct += 1
        results.append({**result, "expected": item["expected_answer"], "correct": is_correct})

    return {
        "accuracy": correct / len(problems),
        "correct": correct,
        "total": len(problems),
        "results": results,
    }
```

**Expected Token Savings:** Thinking section can be hidden from downstream systems; only answer forwarded
**Environment:** `pip install anthropic`

---

### Option 4: Few-Shot CoT with Domain-Specific Examples

```python
# prompts/few_shot_cot.py
"""
Few-shot chain-of-thought: provide 2-3 worked examples before the actual problem.
Most effective for domain-specific problem types (finance, statistics, logic).
"""
import anthropic


# Financial calculation examples
FINANCE_COT_EXAMPLES = """
Example 1:
Q: What is the compound interest on $5,000 invested at 6% annually for 3 years?
A: Step 1: Identify the formula: A = P(1 + r)^t
   P = $5,000, r = 0.06, t = 3
   Step 2: Calculate: A = 5000 × (1.06)^3 = 5000 × 1.191016 = $5,955.08
   Step 3: Interest = $5,955.08 - $5,000 = $955.08
   ANSWER: $955.08

Example 2:
Q: A product costs $80 to make. At 40% markup, what is the selling price?
A: Step 1: Markup amount = 40% of $80 = 0.40 × 80 = $32
   Step 2: Selling price = Cost + Markup = $80 + $32 = $112
   ANSWER: $112
"""

# Logic puzzle examples
LOGIC_COT_EXAMPLES = """
Example 1:
Q: All A are B. Some B are C. Therefore, can we conclude all A are C?
A: Step 1: Premises: (All A → B) and (Some B → C)
   Step 2: From "All A are B", every A is also a B.
   Step 3: From "Some B are C", only SOME Bs are Cs — not necessarily the B that are also A.
   Step 4: We cannot guarantee the As (which are Bs) are among the "some Bs" that are Cs.
   ANSWER: No, we cannot conclude all A are C.

Example 2:
Q: If it rains, the ground gets wet. The ground is wet. Did it rain?
A: Step 1: P → Q (Rain → Wet ground)
   Step 2: We observe Q (ground is wet).
   Step 3: This is the fallacy of "affirming the consequent."
   Step 4: Other causes (sprinklers, spilled water) could also wet the ground.
   ANSWER: Not necessarily — wet ground doesn't prove it rained.
"""


def solve_with_examples(
    problem: str,
    domain: str = "general",  # "finance" | "logic" | "general"
    model: str = "claude-sonnet-4-6",
) -> str:
    examples = {
        "finance": FINANCE_COT_EXAMPLES,
        "logic": LOGIC_COT_EXAMPLES,
        "general": "",
    }.get(domain, "")

    user_content = (
        f"{examples}\nNow solve this problem using the same step-by-step approach:\nQ: {problem}\nA:"
        if examples
        else f"Q: {problem}\n\nSolve step by step, then give the final answer.\nA:"
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": user_content}],
    )
    return response.content[0].text


if __name__ == "__main__":
    finance_result = solve_with_examples(
        "If I invest $10,000 at 8% per year for 5 years, what is the total value?",
        domain="finance",
    )
    print("Finance result:", finance_result)

    logic_result = solve_with_examples(
        "No cats are dogs. Some pets are cats. Therefore, are some pets not dogs?",
        domain="logic",
    )
    print("Logic result:", logic_result)
```

**Expected Token Savings:** ~25% vs generic CoT (domain examples reduce exploration)
**Environment:** `pip install anthropic`

---

### Option 5: Extended Thinking for Hard Problems

```python
# prompts/extended_thinking_cot.py
"""
For very hard math or logic problems, use Claude's extended thinking feature.
Extended thinking lets Claude explore solution paths internally before committing
to an answer — analogous to a mathematician working on scratch paper.
"""
import anthropic
import os


def solve_hard_problem(
    problem: str,
    thinking_budget: int = 8000,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """
    Use extended thinking for complex multi-step problems.
    The thinking block contains internal reasoning not shown to users.
    """
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=thinking_budget + 1024,
        thinking={
            "type": "enabled",
            "budget_tokens": thinking_budget,
        },
        system=(
            "You are a precise mathematical and logical reasoning assistant. "
            "Take as much space as needed to work through the problem correctly. "
            "Your final answer must be clear and precisely stated."
        ),
        messages=[{"role": "user", "content": problem}],
    )

    thinking_text = ""
    answer_text = ""

    for block in response.content:
        if block.type == "thinking":
            thinking_text = block.thinking
        elif block.type == "text":
            answer_text = block.text

    return {
        "answer": answer_text,
        "thinking_tokens": len(thinking_text) // 4,  # Rough estimate
        "answer_tokens": response.usage.output_tokens,
        "input_tokens": response.usage.input_tokens,
        "thinking_length_chars": len(thinking_text),
    }


def adaptive_solver(problem: str) -> dict:
    """
    Adaptive: use standard CoT for simple problems, extended thinking for hard ones.
    Classifies problem difficulty before choosing strategy.
    """
    # Quick complexity estimate: long problems with multiple conditions are harder
    word_count = len(problem.split())
    has_multiple_conditions = problem.count("and") + problem.count("if") > 3
    is_complex = word_count > 50 or has_multiple_conditions

    if is_complex:
        return {**solve_hard_problem(problem), "strategy": "extended_thinking"}
    else:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{problem}\n\nLet's think step by step.",
            }],
        )
        return {
            "answer": response.content[0].text,
            "strategy": "zero_shot_cot",
            "input_tokens": response.usage.input_tokens,
        }


if __name__ == "__main__":
    # Hard problem: use extended thinking
    hard = (
        "A snail climbs 3 feet up a 10-foot pole each day but slides back 2 feet each night. "
        "How many days does it take to reach the top? "
        "Note: if the snail reaches the top during the day, it does not slide back."
    )
    result = adaptive_solver(hard)
    print(f"Strategy: {result['strategy']}")
    print(f"Answer: {result['answer']}")
```

**Expected Token Savings:** Use extended thinking only for genuinely hard problems; ~60% savings vs always using it
**Environment:** `pip install anthropic`

---

### Option 6: CoT Accuracy Evaluator

```python
# tests/prompts/test_cot_accuracy.py
"""
Automated accuracy evaluation for chain-of-thought prompting strategies.
Compares accuracy and cost across different CoT approaches.
"""
import pytest
import anthropic
from prompts.cot_math import solve_with_cot, solve_without_cot
from prompts.structured_cot import solve_structured


# Ground truth problem set for benchmarking
MATH_BENCHMARK = [
    {
        "problem": "What is 15% of 240?",
        "answer": "36",
    },
    {
        "problem": "A rectangle has length 12cm and width 8cm. What is its area?",
        "answer": "96",
    },
    {
        "problem": "If a car travels 180 miles in 3 hours, what is its average speed in mph?",
        "answer": "60",
    },
    {
        "problem": "What is 2 to the power of 8?",
        "answer": "256",
    },
    {
        "problem": "A shirt costs $45 after a 25% discount. What was the original price?",
        "answer": "60",
    },
]


def _normalize(text: str) -> str:
    """Extract the first number from a string for comparison."""
    import re
    numbers = re.findall(r'\d+(?:\.\d+)?', text.replace(",", ""))
    return numbers[0] if numbers else text.strip().lower()


@pytest.mark.parametrize("item", MATH_BENCHMARK)
def test_cot_gets_correct_answer(item):
    """Chain-of-thought must answer the benchmark correctly."""
    result = solve_with_cot(item["problem"])
    actual = _normalize(result["final_answer"])
    expected = _normalize(item["answer"])
    assert actual == expected, (
        f"Problem: {item['problem']!r}\n"
        f"Expected: {item['answer']!r}\n"
        f"Got: {result['final_answer']!r}\n"
        f"Reasoning:\n{result['full_reasoning']}"
    )


@pytest.mark.parametrize("item", MATH_BENCHMARK)
def test_cot_shows_work(item):
    """CoT responses must contain at least 2 reasoning steps."""
    result = solve_with_cot(item["problem"])
    assert result["step_count"] >= 2, (
        f"Insufficient reasoning for: {item['problem']!r}\n"
        f"Only {result['step_count']} steps found."
    )


def test_cot_vs_direct_accuracy(capsys):
    """Compare accuracy: CoT should equal or exceed direct answering."""
    cot_correct = 0
    direct_correct = 0

    for item in MATH_BENCHMARK[:3]:  # Test on first 3 to save API calls
        cot_result = solve_with_cot(item["problem"])
        cot_answer = _normalize(cot_result["final_answer"])

        direct_answer = _normalize(solve_without_cot(item["problem"]))
        expected = _normalize(item["answer"])

        if cot_answer == expected:
            cot_correct += 1
        if direct_answer == expected:
            direct_correct += 1

    print(f"\nCoT accuracy: {cot_correct}/3, Direct accuracy: {direct_correct}/3")
    assert cot_correct >= direct_correct, (
        f"CoT ({cot_correct}/3) should not be less accurate than direct ({direct_correct}/3)"
    )
```

**Expected Token Savings:** Accuracy benchmark shows when CoT cost overhead is justified
**Environment:** `pip install anthropic pytest`

---

## Comparison Table

| Option | Technique | Examples Needed | Accuracy Boost | Token Cost | Best For |
|--------|-----------|----------------|----------------|------------|----------|
| 1: Explicit steps | System prompt instruction | No | High | +30% | General math |
| 2: Zero-shot CoT | "Let's think step by step" | No | High | +40% | Word problems |
| 3: Structured XML | Tagged <thinking>/<answer> | No | High | +35% | Parseable output |
| 4: Few-shot CoT | Domain examples | 2–3 per domain | Highest | +50% | Specialized domains |
| 5: Extended thinking | Sonnet thinking mode | No | Highest | +100–300% | Very hard problems |
| 6: Accuracy evaluator | N/A (benchmark) | Ground truth | N/A (measures) | Per run | CI accuracy gate |
