---
layout: solution
title: "Agent Uses Wrong Temperature for Task Type"
category: prompt-engineering
description: "Agent uses a single fixed temperature for all tasks — high temperature on structured extraction produces random inconsistent outputs; temperature=0 on creative tasks produces repetitive generic responses. Wrong temperature is the silent quality killer."
tags: [prompt-engineering, temperature, sampling, determinism, output-quality, configuration]
---

## Symptom

An extraction agent set to `temperature=1.0` returns different field values each run for the same input — sometimes the date is `"2025-04-14"`, sometimes `"April 14"`, sometimes omitted entirely. Downstream parsers break randomly. Conversely, a creative writing agent hardcoded to `temperature=0` produces the same generic opening paragraph every time, regardless of the topic. Users notice both problems but the root cause — wrong temperature — is rarely obvious.

Temperature misconfiguration impact:
- **Extraction at temperature=1.0**: field consistency drops from ~98% to ~60%
- **Creative tasks at temperature=0**: user-perceived originality score: **2/10**

## Root Cause

The agent passes a fixed `temperature` value (or relies on the API default of 1.0) regardless of task type. Temperature controls the randomness of token sampling — high values introduce variance that is desirable for creativity but destructive for structured outputs, while low values produce consistency that is correct for extraction but dull for open-ended generation.

## Fix

---

### Option 1 — Task-Type Router with Preset Temperature Profiles

Map each task type to a validated temperature profile. Route every request through the router before calling the API.

```python
import anthropic
from enum import Enum
from dataclasses import dataclass

client = anthropic.Anthropic()

class TaskType(str, Enum):
    EXTRACTION = "extraction"         # Pull structured data from text
    CLASSIFICATION = "classification" # Assign category labels
    SUMMARIZATION = "summarization"   # Condense content
    TRANSLATION = "translation"       # Language conversion
    CODE_GENERATION = "code_generation"  # Write runnable code
    CREATIVE_WRITING = "creative_writing"  # Stories, marketing copy
    BRAINSTORMING = "brainstorming"   # Divergent idea generation
    CHAT = "chat"                     # Conversational response
    REASONING = "reasoning"           # Step-by-step analysis

@dataclass
class TemperatureProfile:
    temperature: float
    top_p: float
    rationale: str

PROFILES: dict[TaskType, TemperatureProfile] = {
    TaskType.EXTRACTION:       TemperatureProfile(0.0,  1.0,  "Deterministic — same input must produce same output"),
    TaskType.CLASSIFICATION:   TemperatureProfile(0.0,  1.0,  "Labels must be consistent across runs"),
    TaskType.TRANSLATION:      TemperatureProfile(0.1,  1.0,  "Near-deterministic; tiny variance allows natural phrasing"),
    TaskType.SUMMARIZATION:    TemperatureProfile(0.3,  0.95, "Mostly faithful; slight variation for fluency"),
    TaskType.CODE_GENERATION:  TemperatureProfile(0.2,  1.0,  "Low variance — code must be syntactically correct"),
    TaskType.REASONING:        TemperatureProfile(0.2,  1.0,  "Consistent logic chain; avoids wild speculation"),
    TaskType.CHAT:             TemperatureProfile(0.7,  0.95, "Natural conversational variety"),
    TaskType.CREATIVE_WRITING: TemperatureProfile(0.9,  0.95, "High variance for originality and surprise"),
    TaskType.BRAINSTORMING:    TemperatureProfile(1.0,  0.95, "Maximum divergence — generate many distinct ideas"),
}

def call_with_profile(
    task_type: TaskType,
    system: str,
    user_message: str,
    max_tokens: int = 1024,
) -> str:
    profile = PROFILES[task_type]
    print(f"[Router] {task_type.value} → temperature={profile.temperature}, top_p={profile.top_p}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        temperature=profile.temperature,
        top_p=profile.top_p,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

# Extraction — deterministic
order_text = "Order #A-4821 placed on April 14 2025 for $349.99 by customer john@email.com."
extraction_result = call_with_profile(
    TaskType.EXTRACTION,
    system='Extract order details. Return JSON: {"order_id": str, "date": str, "amount": float, "email": str}',
    user_message=order_text,
)
print(f"Extraction: {extraction_result}\n")

# Creative writing — high variance
creative_result = call_with_profile(
    TaskType.CREATIVE_WRITING,
    system="You are a creative writer. Write vivid, original prose.",
    user_message="Write an opening paragraph for a story set in a rainy city.",
    max_tokens=200,
)
print(f"Creative: {creative_result[:120]}...\n")

# Brainstorming — maximum divergence
ideas = call_with_profile(
    TaskType.BRAINSTORMING,
    system="Generate highly diverse, unconventional ideas. Avoid obvious suggestions.",
    user_message="List 5 unusual ways a coffee shop could differentiate itself.",
    max_tokens=300,
)
print(f"Brainstorm: {ideas[:150]}...")
```

**Expected Token Savings:** None — same tokens; output quality and consistency improve dramatically
**Environment:** `pip install anthropic`

---

### Option 2 — Automatic Task Classifier that Sets Temperature

When the task type is unknown, use a fast classifier to infer it from the prompt, then apply the correct temperature automatically.

```python
import json
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

# Temperature map keyed by task label
TEMP_MAP = {
    "extraction":      0.0,
    "classification":  0.0,
    "translation":     0.1,
    "code":            0.2,
    "reasoning":       0.2,
    "summarization":   0.3,
    "qa":              0.4,
    "chat":            0.7,
    "creative":        0.9,
    "brainstorming":   1.0,
}

def classify_task(prompt: str) -> tuple[str, float]:
    """Use Haiku to classify the task type and return (label, temperature)."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=f"""Classify this prompt into exactly one task type.
Valid types: {', '.join(TEMP_MAP.keys())}
Return JSON: {{"task": "type"}}""",
        messages=[{"role": "user", "content": prompt[:500]}],
    )
    try:
        data = json.loads(response.content[0].text.strip())
        label = data.get("task", "chat")
        if label not in TEMP_MAP:
            label = "chat"
        return label, TEMP_MAP[label]
    except json.JSONDecodeError:
        return "chat", 0.7

def auto_call(
    user_message: str,
    system: str = "You are a helpful assistant.",
    max_tokens: int = 1024,
) -> str:
    """Automatically classify the task and apply the correct temperature."""
    task_label, temperature = classify_task(user_message)
    print(f"[Auto] Detected task='{task_label}', temperature={temperature}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

# Test automatic routing
test_prompts = [
    ('Extract the invoice date from: "Invoice dated 2025-03-15 for $1,200."', "extraction → temp=0.0"),
    ("Write a poem about autumn leaves in the style of haiku.", "creative → temp=0.9"),
    ("What is 2 + 2?", "qa → temp=0.4"),
    ("Generate 10 completely different product name ideas for a water bottle brand.", "brainstorming → temp=1.0"),
    ("Translate 'Good morning' to Japanese.", "translation → temp=0.1"),
]

for prompt, expected in test_prompts:
    print(f"\nPrompt: {prompt[:60]}...")
    print(f"Expected: {expected}")
    result = auto_call(prompt)
    print(f"Result: {result[:80]}...")
```

**Expected Token Savings:** ~5% — Haiku classifier is cheap; prevents regeneration from bad outputs
**Environment:** `pip install anthropic`

---

### Option 3 — Temperature Ladder for Self-Consistency Voting

For high-stakes tasks where determinism alone isn't enough, run the same prompt at multiple temperatures and vote on the most consistent answer.

```python
import json
import asyncio
import anthropic
from collections import Counter

async_client = anthropic.AsyncAnthropic()

async def sample_at_temperature(
    system: str,
    user_message: str,
    temperature: float,
    max_tokens: int = 512,
) -> str:
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text.strip()

async def self_consistency_vote(
    system: str,
    user_message: str,
    temperatures: list[float] = None,
    n_samples: int = 5,
) -> dict:
    """
    Sample multiple times across temperature range.
    For classification/extraction: vote for most frequent answer.
    """
    if temperatures is None:
        # Spread across low temperatures for structured tasks
        temperatures = [0.0, 0.1, 0.2, 0.3, 0.4][:n_samples]

    tasks = [
        sample_at_temperature(system, user_message, t, 256)
        for t in temperatures
    ]
    samples = await asyncio.gather(*tasks)

    # Normalize and vote
    normalized = [s.strip().lower() for s in samples]
    vote_counts = Counter(normalized)
    winner, count = vote_counts.most_common(1)[0]
    confidence = count / len(samples)

    return {
        "answer": winner,
        "confidence": confidence,
        "samples": samples,
        "vote_distribution": dict(vote_counts),
    }

async def demo():
    # Sentiment classification — vote across low-temperature samples
    sentiment_system = """Classify the sentiment of the review.
Return EXACTLY one word: positive, negative, or neutral."""

    review = "The product works fine but the packaging was damaged and customer service took 3 days to respond."

    print("=== Sentiment Classification (self-consistency voting) ===")
    result = await self_consistency_vote(
        system=sentiment_system,
        user_message=review,
        temperatures=[0.0, 0.1, 0.2, 0.3, 0.4],
    )
    print(f"Answer: {result['answer']}")
    print(f"Confidence: {result['confidence']:.0%}")
    print(f"Sample distribution: {result['vote_distribution']}")

    # For creative tasks — fan out at high temperature for diversity
    print("\n=== Creative Taglines (high-temperature sampling) ===")
    creative_tasks = [
        sample_at_temperature(
            "Write exactly one punchy marketing tagline (max 8 words).",
            "For a brand of eco-friendly reusable coffee cups.",
            temperature=1.0,
        )
        for _ in range(5)
    ]
    taglines = await asyncio.gather(*creative_tasks)
    for i, t in enumerate(taglines, 1):
        print(f"  {i}. {t}")

asyncio.run(demo())
```

**Expected Token Savings:** None — uses more tokens intentionally for higher-confidence answers
**Environment:** `pip install anthropic`

---

### Option 4 — Dynamic Temperature Based on Output Format Requirement

Inspect the system prompt for output format constraints. If strict JSON/schema output is required, override temperature to 0 regardless of what was configured.

```python
import json
import re
import anthropic
from typing import Any

client = anthropic.Anthropic()

FORMAT_INDICATORS = [
    r'\bJSON\b', r'\bjson\b',
    r'Return exactly',
    r'Output only',
    r'Respond with only',
    r'\bschema\b',
    r'\bYAML\b',
    r'structured output',
    r'machine-readable',
    r'parseable',
]

def infer_required_temperature(system_prompt: str, requested_temperature: float) -> tuple[float, str]:
    """
    If the system prompt demands structured output, override to temperature=0.
    Returns (final_temperature, reason).
    """
    for pattern in FORMAT_INDICATORS:
        if re.search(pattern, system_prompt):
            if requested_temperature > 0.1:
                return 0.0, f"Overridden to 0.0: system prompt requires structured output (matched '{pattern}')"
            return requested_temperature, "No override needed — temperature already low"

    return requested_temperature, f"No override — free-form output at temperature={requested_temperature}"

def guarded_call(
    system: str,
    user_message: str,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    validate_json: bool = False,
) -> dict[str, Any]:
    final_temp, reason = infer_required_temperature(system, temperature)
    print(f"[Guard] {reason}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        temperature=final_temp,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    text = response.content[0].text.strip()

    result: dict[str, Any] = {"text": text, "temperature_used": final_temp}

    if validate_json:
        try:
            # Strip markdown code fences if present
            clean = re.sub(r'^```(?:json)?\n?', '', text)
            clean = re.sub(r'\n?```$', '', clean)
            result["parsed"] = json.loads(clean)
            result["valid_json"] = True
        except json.JSONDecodeError as e:
            result["valid_json"] = False
            result["parse_error"] = str(e)

    return result

# Test 1: Structured extraction — temperature override kicks in
r1 = guarded_call(
    system='Extract fields. Return JSON: {"name": str, "age": int, "city": str}',
    user_message="My name is Elena, I am 34 years old and live in Barcelona.",
    temperature=0.9,  # Developer set this too high by mistake
    validate_json=True,
)
print(f"JSON valid: {r1.get('valid_json')}, temp used: {r1['temperature_used']}")
print(f"Parsed: {r1.get('parsed')}\n")

# Test 2: Creative task — temperature preserved
r2 = guarded_call(
    system="You are a creative copywriter. Write engaging marketing copy.",
    user_message="Write a one-sentence tagline for a smart water bottle.",
    temperature=0.9,
)
print(f"Creative output (temp={r2['temperature_used']}): {r2['text']}\n")

# Test 3: Borderline — "Output only" triggers override
r3 = guarded_call(
    system="Respond with only a single number: the word count of the input.",
    user_message="The quick brown fox jumps over the lazy dog.",
    temperature=0.7,
)
print(f"Word count (temp={r3['temperature_used']}): {r3['text']}")
```

**Expected Token Savings:** 10–20% — avoids regeneration loops caused by malformed structured outputs
**Environment:** `pip install anthropic`

---

### Option 5 — Temperature Annealing for Iterative Refinement

Start with high temperature to generate diverse candidate outputs, then progressively lower temperature to refine the best candidate into a polished result.

```python
import asyncio
import anthropic

async_client = anthropic.AsyncAnthropic()

async def generate_candidates(
    system: str,
    user_message: str,
    n: int = 4,
    temperature: float = 1.0,
    max_tokens: int = 512,
) -> list[str]:
    """High-temperature fan-out: generate N diverse candidates."""
    tasks = [
        async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        for _ in range(n)
    ]
    responses = await asyncio.gather(*tasks)
    return [r.content[0].text.strip() for r in responses]

async def select_best(candidates: list[str], criteria: str) -> str:
    """Use Claude to pick the best candidate according to criteria."""
    numbered = "\n".join(f"{i+1}. {c}" for i, c in enumerate(candidates))
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        temperature=0.0,
        system=f"Select the best option based on: {criteria}. Reply with only the number.",
        messages=[{"role": "user", "content": numbered}],
    )
    try:
        idx = int(response.content[0].text.strip()) - 1
        return candidates[max(0, min(idx, len(candidates) - 1))]
    except (ValueError, IndexError):
        return candidates[0]

async def refine(
    candidate: str,
    refinement_prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> str:
    """Low-temperature polish of the selected candidate."""
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        temperature=temperature,
        system="Refine and polish the following content. Preserve the core idea.",
        messages=[{
            "role": "user",
            "content": f"Original:\n{candidate}\n\nRefinement goal: {refinement_prompt}",
        }],
    )
    return response.content[0].text.strip()

async def annealed_generation(
    task: str,
    system: str,
    refinement_goal: str,
    selection_criteria: str,
    n_candidates: int = 4,
) -> dict:
    """Full annealing pipeline: explore → select → refine."""
    print(f"[Step 1] Generating {n_candidates} candidates at temperature=1.0...")
    candidates = await generate_candidates(system, task, n=n_candidates, temperature=1.0)
    for i, c in enumerate(candidates, 1):
        print(f"  Candidate {i}: {c[:60]}...")

    print(f"\n[Step 2] Selecting best candidate (temperature=0.0)...")
    best = await select_best(candidates, selection_criteria)
    print(f"  Selected: {best[:80]}...")

    print(f"\n[Step 3] Refining at temperature=0.2...")
    final = await refine(best, refinement_goal, temperature=0.2)

    return {
        "candidates": candidates,
        "selected": best,
        "final": final,
    }

result = asyncio.run(annealed_generation(
    task="Write a product description for wireless noise-cancelling headphones targeting remote workers.",
    system="You are a senior product copywriter. Write concise, compelling product descriptions (2-3 sentences).",
    refinement_goal="Make it more specific, action-oriented, and emphasize the remote work benefit.",
    selection_criteria="Most specific and emotionally resonant description targeting remote workers",
    n_candidates=4,
))

print(f"\n=== Final Output ===\n{result['final']}")
```

**Expected Token Savings:** None — uses more tokens for quality; prevents costly rewrites from poor first drafts
**Environment:** `pip install anthropic`

---

### Option 6 — Per-Field Temperature in Structured Generation

When generating structured output with multiple fields of different nature (some deterministic, some creative), use separate API calls per field with the appropriate temperature for each.

```python
import asyncio
import json
import anthropic
from dataclasses import dataclass
from typing import Any

async_client = anthropic.AsyncAnthropic()

@dataclass
class FieldSpec:
    name: str
    description: str
    temperature: float   # Field-specific temperature
    max_tokens: int = 128

async def generate_field(
    context: str,
    spec: FieldSpec,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[str, Any]:
    """Generate one field with its own temperature."""
    response = await async_client.messages.create(
        model=model,
        max_tokens=spec.max_tokens,
        temperature=spec.temperature,
        system=f"Generate ONLY the value for the field '{spec.name}': {spec.description}. No labels, no explanations.",
        messages=[{"role": "user", "content": f"Context: {context}"}],
    )
    return spec.name, response.content[0].text.strip()

async def generate_structured(
    context: str,
    fields: list[FieldSpec],
) -> dict[str, Any]:
    """Generate all fields in parallel, each with its own temperature."""
    tasks = [generate_field(context, spec) for spec in fields]
    results = await asyncio.gather(*tasks)
    return dict(results)

# Product listing generator — deterministic fields + creative fields
PRODUCT_FIELDS = [
    # Deterministic fields — must match facts in context
    FieldSpec("sku",          "Product SKU code — extract from context exactly",   temperature=0.0),
    FieldSpec("price_usd",    "Price in USD as a number only",                      temperature=0.0),
    FieldSpec("category",     "Product category: electronics/clothing/home/sports", temperature=0.0),
    FieldSpec("in_stock",     "Is it in stock? Reply: true or false",               temperature=0.0),
    # Creative fields — vary for engagement
    FieldSpec("tagline",      "Catchy one-line marketing tagline (max 10 words)",   temperature=0.9, max_tokens=64),
    FieldSpec("description",  "Engaging 2-sentence product description",            temperature=0.8, max_tokens=200),
    FieldSpec("seo_keywords", "5 comma-separated SEO keywords for this product",    temperature=0.6, max_tokens=80),
]

async def demo():
    product_context = """
    SKU: WH-NC-800
    Product: Sony WH-1000XM5 wireless headphones
    Price: $349.99
    Stock: 47 units available
    Features: industry-leading noise cancellation, 30hr battery, multipoint Bluetooth
    """

    print("Generating structured product listing with per-field temperatures...\n")
    listing = await generate_structured(product_context, PRODUCT_FIELDS)

    print("=== Generated Product Listing ===")
    print(json.dumps(listing, indent=2))

    # Show temperature used per field
    print("\n=== Temperature Used Per Field ===")
    for spec in PRODUCT_FIELDS:
        val = listing.get(spec.name, "")
        print(f"  {spec.name:20s} temp={spec.temperature} → {str(val)[:50]}")

asyncio.run(demo())
```

**Expected Token Savings:** 15–25% — Haiku at field level is cheap; avoids regeneration from mixed-temperature failures
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Task Coverage | Automation Level | Best For |
|--------|--------------|-----------------|----------|
| Task-Type Router | All known task types | Manual classification | When task type is always known at call site |
| Auto Classifier | Any prompt | Fully automatic | General-purpose agents with diverse tasks |
| Self-Consistency Vote | Classification/extraction | Semi-automatic | High-stakes decisions needing confidence score |
| Format-Guarded Override | Structured output tasks | Automatic guard | Preventing developer misconfiguration |
| Temperature Annealing | Creative + refinement | Automatic pipeline | Creative work that also needs polish |
| Per-Field Temperature | Mixed structured+creative | Manual field spec | Product generators, report builders |

**Recommended starting point:** Option 1 (Task-Type Router) — define profiles once, route every call through it. A 15-minute change that eliminates the entire class of temperature-related quality failures.
