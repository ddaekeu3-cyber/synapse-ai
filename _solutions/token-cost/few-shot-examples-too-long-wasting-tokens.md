---
layout: solution
title: "Few-Shot Examples in System Prompt Too Long — Thousands of Tokens Wasted Per Call"
category: token-cost
description: "Developer adds 10 detailed examples to the system prompt to improve output quality. Each example is 500 tokens. Every API call now costs an extra 5,000 tokens just for examples the model mostly ignores."
tags: [token-cost, few-shot, system-prompt, examples, prompt-engineering, optimization]
---

# Few-Shot Examples in System Prompt Too Long — Thousands of Tokens Wasted Per Call

## Symptom
- System prompt is 8,000 tokens but should be 500
- 6,000+ of those tokens are example input/output pairs
- API costs 10× higher than expected
- Examples were added to "improve quality" but cost kept growing
- Response quality with 2 examples is nearly identical to 10 examples

## Root Cause
Few-shot examples help but with diminishing returns. The first 2–3 examples provide most of the benefit. Additional examples add marginal quality improvement while adding substantial token cost to every API call. Long verbose examples compound this — a 50-word example is usually as effective as a 500-word example.

## Fix

### Option 1: Measure tokens in your system prompt

```python
import anthropic

client = anthropic.Anthropic()

def count_system_prompt_tokens(system_prompt: str) -> int:
    """Count tokens in system prompt using the API"""
    response = client.messages.count_tokens(
        model="claude-sonnet-4-6",
        system=system_prompt,
        messages=[{"role": "user", "content": "test"}]
    )
    return response.input_tokens

system_prompt = open("system_prompt.txt").read()
tokens = count_system_prompt_tokens(system_prompt)
print(f"System prompt: {tokens:,} tokens")

# Cost estimate at current pricing
cost_per_call = tokens * 0.000003  # $3 per 1M input tokens for Sonnet
print(f"System prompt cost per call: ${cost_per_call:.4f}")
print(f"Cost for 10,000 calls: ${cost_per_call * 10000:.2f}")
```

### Option 2: Reduce to 2-3 minimal examples

```python
# BEFORE — 5 verbose examples, ~2500 tokens
LONG_SYSTEM_PROMPT = """
You are a data extraction assistant.

Example 1:
Input: "The meeting is scheduled for Monday March 15th at 2pm in the main conference room with John Smith and Sarah Johnson to discuss Q1 budget review and departmental headcount planning for the upcoming fiscal year."
Output: {"date": "2024-03-15", "time": "14:00", "location": "main conference room", "attendees": ["John Smith", "Sarah Johnson"], "topics": ["Q1 budget review", "headcount planning"]}

Example 2:
[... 4 more equally verbose examples ...]
"""

# AFTER — 2 minimal examples, ~300 tokens
OPTIMIZED_SYSTEM_PROMPT = """
You are a data extraction assistant. Extract structured data from text.

Example:
Input: "Meeting: Mon 3/15 at 2pm, conf room A, with Jane and Bob, about Q1 budget"
Output: {"date": "2024-03-15", "time": "14:00", "location": "conf room A", "attendees": ["Jane", "Bob"], "topics": ["Q1 budget"]}

Return valid JSON only. No explanation.
"""
```

### Option 3: Use prompt caching for stable few-shot examples

```python
# If you need many examples, cache them so you only pay once per 5 minutes
import anthropic

client = anthropic.Anthropic()

MANY_EXAMPLES = """...(thousands of tokens of examples)..."""

def call_with_cached_examples(user_input: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": "You are a data extraction assistant.\n\n" + MANY_EXAMPLES,
                "cache_control": {"type": "ephemeral"}  # Cache examples — ~10% cost
            }
        ],
        messages=[{"role": "user", "content": user_input}]
    )
    return response.content[0].text
```

### Option 4: Dynamic few-shot — select relevant examples per query

```python
from sentence_transformers import SentenceTransformer
import numpy as np

class DynamicFewShot:
    """Select the most relevant examples for each query"""
    def __init__(self, examples: list[dict]):
        self.examples = examples
        self.model = SentenceTransformer("all-MiniLM-L6-v2")
        # Pre-embed all examples
        self.embeddings = self.model.encode([ex["input"] for ex in examples])

    def select(self, query: str, top_k: int = 2) -> list[dict]:
        """Return top_k most relevant examples for this query"""
        query_emb = self.model.encode([query])
        scores = np.dot(self.embeddings, query_emb.T).flatten()
        top_indices = scores.argsort()[-top_k:][::-1]
        return [self.examples[i] for i in top_indices]

    def format_for_prompt(self, selected: list[dict]) -> str:
        lines = []
        for ex in selected:
            lines.append(f"Input: {ex['input']}")
            lines.append(f"Output: {ex['output']}\n")
        return "\n".join(lines)

# Usage — only 2 relevant examples per call instead of 10 every time
few_shot = DynamicFewShot(all_examples)
relevant = few_shot.select(user_query, top_k=2)
examples_text = few_shot.format_for_prompt(relevant)
```

### Option 5: Benchmark quality vs. number of examples

```python
import anthropic, statistics

def benchmark_examples(test_cases: list[dict], max_examples: int = 10) -> dict:
    """Find the minimum examples needed for acceptable quality"""
    client = anthropic.Anthropic()
    results = {}

    for n_examples in range(0, max_examples + 1, 2):
        examples_text = format_examples(ALL_EXAMPLES[:n_examples])
        scores = []

        for test in test_cases[:20]:  # Sample 20 test cases
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=f"Task instructions.\n\n{examples_text}",
                messages=[{"role": "user", "content": test["input"]}]
            )
            score = evaluate(response.content[0].text, test["expected"])
            scores.append(score)

        avg_score = statistics.mean(scores)
        results[n_examples] = avg_score
        print(f"{n_examples} examples: {avg_score:.2%} accuracy")

    return results
# Often you'll find 2-3 examples = 90% of the benefit of 10
```

## Token Cost vs. Example Count

| Examples | Approx tokens | Cost delta vs. 0 examples |
|---|---|---|
| 0 | 0 | Baseline |
| 1 (minimal) | 100 | +$0.30 / 100K calls |
| 2 (minimal) | 200 | +$0.60 / 100K calls |
| 5 (verbose) | 2,500 | +$7.50 / 100K calls |
| 10 (verbose) | 5,000 | +$15.00 / 100K calls |

*At $3/M input tokens. Check current pricing.*

## When Each Approach Works Best

| Situation | Approach |
|---|---|
| < 5 examples needed | Inline in system prompt (minimal format) |
| 5–20 examples, stable | Prompt caching |
| 5–20 examples, varied queries | Dynamic selection |
| > 20 examples | Fine-tuning or RAG |

## Expected Token Savings
10 verbose examples per call × 1M calls: 5B tokens (~$15,000)
2 minimal examples per call: 200 tokens (~$600)
Savings: ~96%

## Environment
- Any agent with few-shot examples in system prompts; most impactful at scale
- Source: direct measurement, empirical quality/cost tradeoff analysis
