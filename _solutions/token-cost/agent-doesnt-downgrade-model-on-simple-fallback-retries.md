---
layout: solution
title: "Agent Doesn't Downgrade Model on Simple Fallback Retries"
category: token-cost
description: "When the primary model fails or returns an unsatisfactory response, the agent retries with the same expensive model instead of routing simple follow-up tasks to a cheaper one."
tags: [token-cost, model-selection, retry, fallback, production]
---

## Symptom

The agent uses `claude-sonnet-4-6` for all requests. When a response is too long, too terse, or malformed, it retries the exact same prompt with the same model at full cost. For simple correction tasks — "output only valid JSON", "shorten to 50 words", "retry after rate limit" — paying sonnet prices is wasteful. A haiku-class model can handle these corrective tasks at ~20× lower cost.

## Root Cause

Retry logic is typically written as a loop that calls the same `client.messages.create()` invocation unchanged. The retry is meant to be identical to the original — but many retry scenarios are actually *different* tasks: format correction, length adjustment, rate-limit backoff, or output validation. These corrective calls are cheaper than the original inference and should use the cheapest capable model.

## Fix

### Option 1 — Tiered retry: sonnet first, haiku for format correction

```python
import anthropic
import json
import time

client = anthropic.Anthropic()

def ask_with_format_fallback(prompt: str) -> dict | None:
    """Try sonnet for content, haiku for JSON correction."""
    # Primary: expensive model for content quality
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    print(f"[sonnet] {resp.usage.input_tokens}+{resp.usage.output_tokens} tokens")

    # Try parsing
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Fallback: cheap model for format correction only
    print("[fallback] JSON parse failed → haiku correction")
    fix_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {"role": "user", "content": (
                "Fix the following text so it is valid JSON. "
                "Output only the corrected JSON, nothing else.\n\n"
                f"{text}"
            )},
        ],
    )
    print(f"[haiku]  {fix_resp.usage.input_tokens}+{fix_resp.usage.output_tokens} tokens")
    try:
        return json.loads(fix_resp.content[0].text.strip())
    except json.JSONDecodeError:
        return None

result = ask_with_format_fallback(
    'Return a JSON object with fields: name, language, year. Example: {"name":"Python","language":"Python","year":1991}'
)
print("Result:", result)
```

**Expected Token Savings:** Haiku costs ~1/20th of sonnet; if 20% of requests need format correction, blended cost falls by ~19% with no quality loss on the primary task.
**Environment:** Agents that generate structured output; any pipeline where format validation triggers retries.

---

### Option 2 — Rate-limit retry with model downgrade during backoff

```python
import anthropic
import time

client = anthropic.Anthropic()

MODEL_TIER = [
    "claude-sonnet-4-6",          # primary: best quality
    "claude-haiku-4-5-20251001",  # fallback: cheaper, faster, less likely rate-limited
]

def ask_with_rate_limit_downgrade(prompt: str, max_tokens: int = 256) -> str:
    for tier_idx, model in enumerate(MODEL_TIER):
        attempt = 0
        while attempt < 3:
            try:
                resp = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                if tier_idx > 0:
                    print(f"[downgrade] used {model} (tier {tier_idx})")
                print(f"[{model}] {resp.usage.input_tokens}+{resp.usage.output_tokens} tokens")
                return resp.content[0].text
            except anthropic.RateLimitError as exc:
                retry_after = int(exc.response.headers.get("retry-after", 5))
                attempt += 1
                if attempt < 3:
                    print(f"[rate_limit] {model} throttled, wait {retry_after}s (attempt {attempt}/3)")
                    time.sleep(retry_after)
                else:
                    print(f"[rate_limit] {model} exhausted — downgrading model tier")
                    break  # try next tier
            except anthropic.APIConnectionError:
                time.sleep(2 ** attempt)
                attempt += 1

    raise RuntimeError("All model tiers exhausted")

for i in range(5):
    result = ask_with_rate_limit_downgrade(f"Summarise the concept of entropy in one sentence.")
    print(f"Response {i}: {result[:80]}\n")
```

**Expected Token Savings:** Haiku has higher rate limits per tier; routing to haiku during sonnet rate limits avoids long waits while keeping requests flowing at lower cost.
**Environment:** Agents hitting rate limits on premium model tiers; batch processing that must complete within a time window despite rate limits.

---

### Option 3 — Length-overshoot retry: truncate with haiku instead of re-running sonnet

```python
import anthropic

client = anthropic.Anthropic()

MAX_WORDS = 50

def count_words(text: str) -> int:
    return len(text.split())

def ask_with_length_correction(prompt: str) -> str:
    # Primary call: full model
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    words = count_words(text)
    print(f"[sonnet] {words} words, {resp.usage.input_tokens}+{resp.usage.output_tokens} tokens")

    if words <= MAX_WORDS:
        return text

    # Cheap correction: haiku trims the output
    print(f"[haiku] trimming {words} → ≤{MAX_WORDS} words")
    trim_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": (
                f"Shorten the following text to at most {MAX_WORDS} words. "
                "Preserve the key point. Output only the shortened text.\n\n"
                f"{text}"
            ),
        }],
    )
    result = trim_resp.content[0].text
    print(f"[haiku] {count_words(result)} words, {trim_resp.usage.input_tokens}+{trim_resp.usage.output_tokens} tokens")
    return result

answer = ask_with_length_correction(
    "Explain how neural networks learn, including backpropagation and gradient descent in detail."
)
print(f"\nFinal ({count_words(answer)} words):", answer)
```

**Expected Token Savings:** Trimming with haiku costs ~100 tokens; re-running the full sonnet prompt to get a shorter answer would cost 400–800 tokens — 4–8× more expensive.
**Environment:** Agents with strict output length constraints; content generation pipelines where the model consistently over-generates.

---

### Option 4 — Cascading model selector: try cheapest first, escalate only if needed

```python
import anthropic
import json

client = anthropic.Anthropic()

MODELS = [
    ("claude-haiku-4-5-20251001", 256),   # cheapest, try first
    ("claude-sonnet-4-6",          512),   # mid-tier, if haiku fails
    ("claude-opus-4-6",            1024),  # most capable, last resort
]

def validate_output(text: str, required_keys: list[str]) -> bool:
    """Check if output meets quality bar."""
    try:
        data = json.loads(text.strip())
        return all(k in data for k in required_keys)
    except json.JSONDecodeError:
        return False

def cascading_ask(prompt: str, required_keys: list[str]) -> tuple[str, str]:
    """Returns (result, model_used). Escalates only when cheaper model fails."""
    for model, max_tokens in MODELS:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
        tokens = resp.usage.input_tokens + resp.usage.output_tokens
        print(f"[{model}] {tokens} tokens, valid={validate_output(text, required_keys)}")

        if validate_output(text, required_keys):
            return text, model

        print(f"[escalate] {model} failed quality check → trying next tier")

    raise RuntimeError("All model tiers failed quality check")

prompt = (
    'Return a JSON object with these exact fields: title (string), year (integer), '
    'authors (list of strings), abstract (string, min 50 words). '
    'Make up a plausible academic paper.'
)
result, model_used = cascading_ask(prompt, ["title", "year", "authors", "abstract"])
print(f"\n[selected] {model_used}")
print(json.dumps(json.loads(result), indent=2)[:300])
```

**Expected Token Savings:** If haiku succeeds 60% of the time, blended cost is 0.60×haiku + 0.40×sonnet ≈ 48% of always-using-sonnet; escalation only pays premium when truly needed.
**Environment:** Quality-gated pipelines; batch processing where task complexity varies and not all items require the most capable model.

---

### Option 5 — Validation-retry with progressively more specific correction prompts

```python
import anthropic
import re

client = anthropic.Anthropic()

def extract_number(text: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None

def ask_for_number(question: str) -> float:
    """Get a numeric answer; use haiku for corrections, escalate prompt specificity."""
    # Step 1: cheap model, simple prompt
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": question}],
    )
    result = extract_number(resp.content[0].text)
    print(f"[haiku/v1] '{resp.content[0].text.strip()}' → {result}")
    if result is not None:
        return result

    # Step 2: haiku with stricter prompt
    resp2 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{"role": "user", "content": f"Answer with a single number only, no units, no words: {question}"}],
    )
    result2 = extract_number(resp2.content[0].text)
    print(f"[haiku/v2] '{resp2.content[0].text.strip()}' → {result2}")
    if result2 is not None:
        return result2

    # Step 3: sonnet as last resort
    resp3 = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16,
        messages=[{"role": "user", "content": f"Output only the numeric answer, nothing else: {question}"}],
    )
    result3 = extract_number(resp3.content[0].text)
    print(f"[sonnet/v3] '{resp3.content[0].text.strip()}' → {result3}")
    if result3 is not None:
        return result3

    raise ValueError(f"Could not extract number from: {question}")

questions = [
    "How many days are in a leap year?",
    "What is the boiling point of water in Celsius?",
    "How many planets are in the solar system?",
]
for q in questions:
    answer = ask_for_number(q)
    print(f"  Q: {q} → A: {answer}\n")
```

**Expected Token Savings:** Haiku handles most numeric extractions in 1–2 attempts; sonnet is only invoked when haiku genuinely fails, saving ~80% of correction call costs.
**Environment:** Extraction agents parsing structured values (numbers, dates, codes) from free-form text; agents with simple output validation that doesn't require complex reasoning.

---

### Option 6 — Cost-aware retry budget: track spend and downgrade automatically

```python
import anthropic
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

# Approximate costs per 1M tokens (input/output) in USD
COSTS = {
    "claude-opus-4-6":          (15.00, 75.00),
    "claude-sonnet-4-6":        (3.00,  15.00),
    "claude-haiku-4-5-20251001":(0.25,  1.25),
}

@dataclass
class RetryBudget:
    max_usd: float
    spent:   float = 0.0

    def charge(self, model: str, input_tokens: int, output_tokens: int) -> float:
        in_cost, out_cost = COSTS.get(model, (3.0, 15.0))
        cost = (input_tokens * in_cost + output_tokens * out_cost) / 1_000_000
        self.spent += cost
        return cost

    def remaining(self) -> float:
        return self.max_usd - self.spent

    def cheapest_affordable(self) -> str | None:
        for model in ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"]:
            in_cost, out_cost = COSTS[model]
            # Estimate cost of a typical call (200 in + 200 out tokens)
            estimated = (200 * in_cost + 200 * out_cost) / 1_000_000
            if estimated <= self.remaining():
                return model
        return None

def ask_within_budget(prompt: str, budget: RetryBudget, preferred: str = "claude-sonnet-4-6") -> str | None:
    model = preferred if (budget.remaining() > 0.01) else budget.cheapest_affordable()
    if model is None:
        print(f"[budget] exhausted (spent ${budget.spent:.4f})")
        return None

    resp = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    cost = budget.charge(model, resp.usage.input_tokens, resp.usage.output_tokens)
    print(f"[{model}] cost=${cost:.4f}, remaining=${budget.remaining():.4f}")
    return resp.content[0].text

budget = RetryBudget(max_usd=0.05)  # 5 cent budget for this request batch
prompts = [
    "Translate 'hello' to French.",
    "What is 12 × 13?",
    "Name three Python web frameworks.",
    "What does HTTP stand for?",
    "Define API in one sentence.",
]
for p in prompts:
    result = ask_within_budget(p, budget)
    if result:
        print(f"  → {result[:60]}\n")
    else:
        print("  → budget exhausted, skipping\n")

print(f"Total spent: ${budget.spent:.4f}")
```

**Expected Token Savings:** Real-time spend tracking forces automatic downgrade when budget runs low; prevents a runaway retry loop from multiplying costs 10× in a batch job.
**Environment:** Batch agents with a fixed per-job cost ceiling; multi-tenant systems where per-request spend must be bounded.

---

## Comparison

| Option | Downgrade Trigger | Models Used | Escalation | Best For |
|---|---|---|---|---|
| 1. Format correction | JSON parse failure | sonnet → haiku | No (haiku fixes only) | Structured output generation |
| 2. Rate limit downgrade | 429 exhausted on primary | sonnet → haiku | No | High-traffic agents hitting rate limits |
| 3. Length correction | Word count exceeds limit | sonnet → haiku | No | Content with strict length constraints |
| 4. Cascading selector | Quality validation fails | haiku → sonnet → opus | Yes (upward) | Variable-complexity tasks |
| 5. Prompt escalation | Extraction failure | haiku → haiku+ → sonnet | Yes (upward) | Simple extraction with format issues |
| 6. Cost budget | Remaining budget check | preferred → cheapest | No (downward only) | Fixed-budget batch jobs |
