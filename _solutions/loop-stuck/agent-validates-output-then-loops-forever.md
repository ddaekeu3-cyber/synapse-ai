---
layout: solution
title: "Agent Validates Its Own Output and Loops Forever — Self-Validation Loop"
category: loop-stuck
description: "Agent generates output, then validates it, finds a minor issue, regenerates, validates again, finds another issue, regenerates. Loop continues because the validator always finds something to improve."
tags: [loop, self-validation, output, quality, stopping-criteria, perfectionism]
---

# Agent Validates Its Own Output and Loops Forever — Self-Validation Loop

## Symptom
- Agent generates a response, runs a self-check, makes small improvements, self-checks again
- Loop: generate → validate → "could be better" → regenerate → validate → "could be better"
- Each iteration produces marginally different output, not definitively better
- Token usage climbs unboundedly
- Agent never reaches a "good enough" threshold to stop

## Root Cause
Self-validation without explicit stopping criteria. The validator LLM call will always find something to suggest ("this could be more concise", "this could be more detailed"). Without a binary pass/fail criterion or a hard iteration cap, the improvement loop continues indefinitely.

## Fix

### Option 1: Hard cap on validation iterations

```python
MAX_VALIDATION_ITERATIONS = 2

async def generate_with_validation(prompt: str, agent) -> str:
    output = await agent.generate(prompt)

    for iteration in range(MAX_VALIDATION_ITERATIONS):
        issues = await agent.validate(output)

        if not issues or issues.get("pass"):
            print(f"Validation passed on iteration {iteration + 1}")
            return output

        print(f"Iteration {iteration + 1}: issues found, regenerating...")
        output = await agent.regenerate(prompt, output, issues)

    print(f"Max iterations ({MAX_VALIDATION_ITERATIONS}) reached — using current output")
    return output  # Use best output regardless
```

### Option 2: Binary pass/fail validator instead of open-ended critique

```python
# WRONG — open-ended validation always finds improvements
validation_prompt = "Review this output and suggest improvements:"
# Validator: "The third sentence could be clearer..."
# → Agent regenerates → Validator: "The second paragraph is slightly verbose..."
# → Infinite loop

# RIGHT — binary validation with specific acceptance criteria
validation_prompt = """Review this output against these BINARY criteria:
1. Does it answer the user's question? YES/NO
2. Is it factually accurate? YES/NO
3. Is it under 500 words? YES/NO
4. Does it have any code syntax errors? YES/NO

If ALL answers are YES (or N/A), respond: PASS
If any answer is NO, respond: FAIL: [specific issue]

Do NOT suggest stylistic improvements. ONLY check the binary criteria."""

# Now validation either passes or fails — no gray area
```

### Option 3: Improvement threshold — stop if change is small

```python
from difflib import SequenceMatcher

def improvement_is_significant(old: str, new: str, threshold: float = 0.1) -> bool:
    """Returns True if new version is meaningfully different from old"""
    similarity = SequenceMatcher(None, old, new).ratio()
    change = 1 - similarity  # 0 = identical, 1 = completely different
    return change > threshold  # Only continue if >10% changed

async def generate_with_improvement_threshold(prompt: str, agent) -> str:
    output = await agent.generate(prompt)

    for iteration in range(5):
        issues = await agent.validate(output)
        if not issues:
            break

        new_output = await agent.improve(output, issues)

        if not improvement_is_significant(output, new_output, threshold=0.05):
            print(f"Change too small (< 5%) at iteration {iteration+1} — stopping")
            output = new_output
            break

        output = new_output

    return output
```

### Option 4: Separate generator and validator models

```python
import anthropic

generator_client = anthropic.Anthropic()
validator_client = anthropic.Anthropic()

VALIDATOR_SYSTEM = """You are a strict output validator.
Your job is to enforce hard requirements, not suggest improvements.
Respond ONLY with:
- "PASS" if all requirements are met
- "FAIL: [specific requirement not met]" if any requirement fails
Do not suggest improvements. Do not comment on style. Binary judgment only."""

async def validated_generation(prompt: str, requirements: list[str], max_tries: int = 3) -> str:
    requirements_text = "\n".join(f"- {r}" for r in requirements)

    for attempt in range(max_tries):
        # Generate
        gen_response = generator_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        )
        output = gen_response.content[0].text

        # Validate
        val_response = validator_client.messages.create(
            model="claude-haiku-4-5-20251001",  # Fast model for validation
            max_tokens=256,
            system=VALIDATOR_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Requirements:\n{requirements_text}\n\nOutput to validate:\n{output}"
            }]
        )
        verdict = val_response.content[0].text.strip()

        if verdict.startswith("PASS"):
            return output

        failure_reason = verdict.replace("FAIL:", "").strip()
        print(f"Attempt {attempt + 1} failed: {failure_reason}")
        prompt += f"\n\nPrevious attempt failed because: {failure_reason}. Fix this specific issue."

    print(f"All {max_tries} attempts failed validation. Returning best attempt.")
    return output
```

### Option 5: Time-boxed validation

```python
import asyncio, time

async def time_boxed_validation(prompt: str, agent, max_seconds: float = 30.0) -> str:
    """Stop validation loop after time budget expires"""
    start = time.time()
    output = await agent.generate(prompt)

    while time.time() - start < max_seconds:
        remaining = max_seconds - (time.time() - start)
        if remaining < 5:  # Less than 5s left — stop
            print(f"Time budget nearly exhausted ({remaining:.1f}s left) — using current output")
            return output

        try:
            issues = await asyncio.wait_for(agent.validate(output), timeout=remaining)
        except asyncio.TimeoutError:
            print("Validation timed out — using current output")
            return output

        if not issues:
            return output

        output = await agent.fix(output, issues)

    print(f"Time budget ({max_seconds}s) exhausted — returning current output")
    return output
```

## Validation Loop Stopping Criteria

| Criterion | How to implement | When to use |
|---|---|---|
| Iteration cap | `for i in range(N)` | Always — safety net |
| Binary pass/fail | Specific YES/NO questions | Quality gates |
| Change threshold | `difflib.SequenceMatcher` | Diminishing returns |
| Time budget | `asyncio.wait_for` | Latency-sensitive |
| Score threshold | `score >= 0.9` | When scoring exists |
| Explicit "done" | Model outputs `[DONE]` | Structured output |

## Expected Token Savings
Open-ended validation loop (10 iterations): ~50,000 tokens
2-iteration hard cap: ~10,000 tokens (80% reduction)

## Environment
- Any agent with self-review, quality checking, or iterative improvement loops
- Source: direct experience; validation loops are a common cause of runaway token usage
