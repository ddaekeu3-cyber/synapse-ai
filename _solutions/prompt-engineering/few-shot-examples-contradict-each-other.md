---
layout: solution
title: "Few-Shot Examples Contradict Each Other — Model Behaves Inconsistently"
category: prompt-engineering
description: "Agent is given 5 examples of how to respond. Two examples use different formats for the same case. One example uses markdown, another uses plain text. Model output is unpredictable because the examples don't agree."
tags: [few-shot, examples, inconsistency, format, prompt-engineering, contradiction, in-context-learning]
---

# Few-Shot Examples Contradict Each Other — Model Behaves Inconsistently

## Symptom
- Agent alternates between two output formats across different runs
- 3 out of 5 examples use JSON, 2 use plain text — output format varies unpredictably
- Some examples use first-person, others use third-person — tone is inconsistent
- Examples have different verbosity levels — sometimes agent is brief, sometimes verbose
- Model appears to average the contradictory examples, producing a format that matches none

## Root Cause
Few-shot examples act as the strongest signal the model has about desired behavior. When examples contradict each other, the model cannot determine which behavior to generalize. It may average them, favor the most recent example, or oscillate between interpretations. Contradictions in format, tone, length, or structure all cause this.

## Fix

### Option 1: Audit examples for consistency before deploying

```python
import json
from typing import Callable

def audit_few_shot_examples(
    examples: list[dict],
    checks: list[tuple[str, Callable]]
) -> list[str]:
    """
    Audit a set of few-shot examples for consistency.
    checks: list of (description, fn(response) -> bool) pairs
    """
    issues = []

    for check_name, check_fn in checks:
        results = [check_fn(ex["response"]) for ex in examples]
        if len(set(results)) > 1:
            failing = [i for i, r in enumerate(results) if not r]
            issues.append(
                f"Inconsistent '{check_name}': "
                f"examples {failing} fail while others pass"
            )

    return issues

EXAMPLES = [
    {"input": "Is this product available?", "response": '{"available": true, "sku": "A123"}'},
    {"input": "What is the price?", "response": "The price is $29.99"},
    {"input": "How long is shipping?", "response": '{"days": 3, "carrier": "UPS"}'},
    {"input": "Is it in stock?", "response": "Yes, we have 5 in stock"},
]

issues = audit_few_shot_examples(EXAMPLES, [
    ("is_json", lambda r: r.strip().startswith("{")),
    ("under_50_words", lambda r: len(r.split()) < 50),
    ("no_markdown", lambda r: "**" not in r and "```" not in r),
])

for issue in issues:
    print(f"AUDIT FAIL: {issue}")
# → AUDIT FAIL: Inconsistent 'is_json': examples [1, 3] fail while others pass
```

### Option 2: Enforce a canonical example template

```python
from dataclasses import dataclass

@dataclass
class FewShotExample:
    """Structured example with enforced format constraints"""
    input: str
    output: str
    output_format: str  # "json" | "plain_text" | "markdown"
    approximate_words: int

    def validate(self) -> list[str]:
        """Validate that this example matches its declared format"""
        errors = []

        if self.output_format == "json":
            try:
                json.loads(self.output)
            except json.JSONDecodeError as e:
                errors.append(f"Declared JSON but output is invalid: {e}")

        if self.output_format == "plain_text":
            if "```" in self.output or "**" in self.output or "##" in self.output:
                errors.append("Declared plain_text but output contains markdown")

        actual_words = len(self.output.split())
        if abs(actual_words - self.approximate_words) > self.approximate_words * 0.5:
            errors.append(
                f"Word count mismatch: declared ~{self.approximate_words}, "
                f"actual {actual_words}"
            )

        return errors

def validate_example_set(examples: list[FewShotExample]) -> None:
    """Ensure all examples use the same output format"""
    formats = set(ex.output_format for ex in examples)
    if len(formats) > 1:
        raise ValueError(
            f"Examples use conflicting output formats: {formats}. "
            f"All examples must use the same format."
        )

    for i, ex in enumerate(examples):
        errors = ex.validate()
        if errors:
            raise ValueError(f"Example {i} has errors:\n" + "\n".join(errors))
```

### Option 3: Select examples dynamically from a validated pool

```python
class ExamplePool:
    """
    Maintain a pool of validated examples.
    Select consistent examples for each query.
    """

    def __init__(self, examples: list[dict], required_format: str = "json"):
        self.required_format = required_format
        # Only admit examples that match the required format
        self.examples = [
            ex for ex in examples
            if self._matches_format(ex["response"], required_format)
        ]
        rejected = len(examples) - len(self.examples)
        if rejected:
            print(f"ExamplePool: rejected {rejected} inconsistent examples")

    def _matches_format(self, response: str, fmt: str) -> bool:
        if fmt == "json":
            try:
                json.loads(response.strip())
                return True
            except Exception:
                return False
        if fmt == "plain_text":
            return "```" not in response and "**" not in response
        return True

    def select(self, query: str, n: int = 3) -> list[dict]:
        """
        Select n examples most similar to the query.
        Simple: just take the first n (or use embedding similarity in production).
        """
        return self.examples[:n]

    def build_prompt_section(self, query: str, n: int = 3) -> str:
        selected = self.select(query, n)
        lines = ["Examples:"]
        for ex in selected:
            lines.append(f"Input: {ex['input']}")
            lines.append(f"Output: {ex['response']}")
            lines.append("")
        lines.append(f"Input: {query}")
        lines.append("Output:")
        return "\n".join(lines)
```

### Option 4: Sort examples from simple to complex (consistent progression)

```python
def order_examples_by_complexity(examples: list[dict]) -> list[dict]:
    """
    Order few-shot examples simple → complex.
    This helps the model understand the pattern progressively
    and reduces the impact of any individual inconsistency.
    """
    # Simple heuristic: sort by response length
    return sorted(examples, key=lambda ex: len(ex["response"]))

def build_few_shot_prompt(
    examples: list[dict],
    query: str,
    max_examples: int = 4
) -> str:
    """
    Build a consistent few-shot prompt with ordered examples.
    """
    ordered = order_examples_by_complexity(examples)
    selected = ordered[:max_examples]

    # Verify all examples share the same format
    formats_detected = set()
    for ex in selected:
        resp = ex["response"].strip()
        if resp.startswith("{") or resp.startswith("["):
            formats_detected.add("json")
        elif "```" in resp or "**" in resp:
            formats_detected.add("markdown")
        else:
            formats_detected.add("plain")

    if len(formats_detected) > 1:
        raise ValueError(
            f"Inconsistent formats in examples: {formats_detected}. "
            f"Homogenize examples before use."
        )

    lines = []
    for i, ex in enumerate(selected, 1):
        lines.append(f"Example {i}:")
        lines.append(f"Q: {ex['input']}")
        lines.append(f"A: {ex['response']}")
        lines.append("")
    lines.append(f"Now answer:")
    lines.append(f"Q: {query}")
    lines.append("A:")
    return "\n".join(lines)
```

### Option 5: One authoritative example + explicit format instruction

```python
# Instead of 5 inconsistent examples, use 1 perfect example + clear instructions
SYSTEM_WITH_ONE_EXAMPLE = """
You are a product catalog assistant. Always respond in JSON.

Response format (always follow this exactly):
{{
  "answer": "direct answer to the question",
  "confidence": "high" | "medium" | "low",
  "follow_up": "optional clarifying question or null"
}}

Example:
Input: "Is the red widget in stock?"
Output: {{"answer": "Yes, 12 units available", "confidence": "high", "follow_up": null}}

Rules:
- answer field: 1-2 sentences, factual
- confidence: based on certainty of information
- follow_up: only if clarification would help, otherwise null
- No other text outside the JSON object
"""

# One consistent example + explicit schema beats five contradictory examples
```

### Option 6: Test that examples produce consistent output

```python
import asyncio

async def test_few_shot_consistency(
    examples: list[dict],
    system_prompt: str,
    client,
    n_test_queries: int = 5
) -> dict:
    """
    Test whether the few-shot examples produce consistent output format
    across multiple different queries.
    """
    test_queries = [
        "What colors are available?",
        "How do I return a product?",
        "Is overnight shipping available?",
        "What payment methods do you accept?",
        "Can I cancel my order?",
    ][:n_test_queries]

    few_shot_context = "\n".join([
        f"Q: {ex['input']}\nA: {ex['response']}"
        for ex in examples
    ])

    results = []
    for query in test_queries:
        prompt = f"{few_shot_context}\n\nQ: {query}\nA:"
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0
        )
        results.append(response.content[0].text.strip())

    # Check format consistency
    formats = []
    for r in results:
        if r.startswith("{") or r.startswith("["):
            formats.append("json")
        elif "**" in r or "```" in r:
            formats.append("markdown")
        else:
            formats.append("plain")

    format_counts = {f: formats.count(f) for f in set(formats)}
    consistent = len(set(formats)) == 1

    return {
        "consistent": consistent,
        "format_distribution": format_counts,
        "sample_outputs": results[:2],
        "recommendation": (
            "Examples are consistent ✓" if consistent
            else f"Inconsistent formats detected: {format_counts}. Review examples."
        )
    }
```

## Common Example Contradictions

| Contradiction | Effect | Fix |
|---|---|---|
| Mix of JSON and plain text | Unpredictable format | Standardize all to one format |
| Different verbosity levels | Random response length | Normalize example lengths |
| Mix of tones (formal/casual) | Inconsistent tone | Pick one tone, rewrite all examples |
| Different field names | Missing/wrong fields | Enforce schema across all examples |
| Some use bullet points, some don't | Inconsistent structure | Decide on structure, rewrite |
| Edge case examples mixed with normal | Model over-applies edge handling | Separate edge cases from main examples |

## Expected Token Savings
Debugging inconsistent agent format + adding format enforcement: ~15,000 tokens
Consistent examples from the start: 0 wasted

## Environment
- All few-shot prompted agents; most critical for structured output and classification agents
- Source: direct experience; inconsistent examples are the most overlooked cause of format inconsistency
