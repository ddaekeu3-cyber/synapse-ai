---
layout: solution
title: "Agent Doesn't Implement Numerical Fact Validation"
category: hallucination
description: "Agents frequently hallucinate specific numbers — dates, prices, statistics, measurements, and counts. Validating numerical claims through range checks, cross-referencing, and re-sampling catches these errors before they reach users."
tags: [hallucination, numerical-validation, fact-checking, numbers, accuracy]
---

## Problem

LLMs are notoriously unreliable with specific numerical facts: they invent exact statistics, confuse units, miscalculate simple arithmetic, and state wrong years or prices with full confidence. A response claiming "Python 3.12 was released in 2019" or "the Eiffel Tower is 450 meters tall" sounds plausible but is incorrect. Without numerical validation, these errors propagate undetected.

## Solutions

### Option 1: Range-Based Plausibility Checks

```python
import anthropic
import re
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

# Domain-specific plausible ranges for common numeric claims
PLAUSIBILITY_RANGES = {
    # Software versions / years
    "python_release_year": (2008, 2026),
    "software_version_major": (1, 50),
    # Physical measurements
    "building_height_meters": (5, 830),
    "mountain_height_meters": (100, 8850),
    "human_height_cm": (50, 250),
    # Populations
    "country_population_millions": (0.01, 1500),
    "city_population_millions": (0.01, 40),
    # Temperatures
    "celsius_boiling_water": (99, 101),
    "celsius_body_temp": (35, 42),
    # Speed
    "speed_of_light_km_s": (299700, 299800),
    "sound_speed_m_s": (330, 350),
}

@dataclass
class NumberValidationResult:
    number: float
    context: str
    is_plausible: bool
    range_key: Optional[str]
    reason: str

def extract_numbers_with_context(text: str) -> list[tuple[float, str]]:
    """Extract numbers with surrounding context."""
    pattern = r'(\d+(?:[,\d]*)?(?:\.\d+)?)\s*(%|meters?|km|miles?|kg|lbs?|°[CF]|mph|km\/h)?'
    results = []
    for match in re.finditer(pattern, text):
        try:
            num_str = match.group(1).replace(',', '')
            num = float(num_str)
            # Get surrounding context (20 chars each side)
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + 20)
            context = text[start:end]
            results.append((num, context))
        except ValueError:
            continue
    return results

def validate_number_plausibility(number: float, context: str) -> NumberValidationResult:
    """Check if a number is plausible given its context."""
    context_lower = context.lower()

    for range_key, (min_val, max_val) in PLAUSIBILITY_RANGES.items():
        # Heuristic keyword matching for range selection
        keywords = range_key.replace("_", " ").split()
        if any(kw in context_lower for kw in keywords[:2]):
            is_plausible = min_val <= number <= max_val
            return NumberValidationResult(
                number=number,
                context=context,
                is_plausible=is_plausible,
                range_key=range_key,
                reason=f"Range [{min_val}, {max_val}]; value {'within' if is_plausible else 'outside'} bounds"
            )

    return NumberValidationResult(number=number, context=context, is_plausible=True,
        range_key=None, reason="No range rule applies")

def generate_and_validate(prompt: str, system: str = "") -> dict:
    """Generate a response and validate all extracted numbers."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system or "You are a factual assistant.",
        messages=[{"role": "user", "content": prompt}]
    )
    output = response.content[0].text

    numbers = extract_numbers_with_context(output)
    validations = [validate_number_plausibility(n, ctx) for n, ctx in numbers]
    implausible = [v for v in validations if not v.is_plausible]

    if implausible:
        print(f"[NumberCheck] {len(implausible)} implausible number(s) detected:")
        for v in implausible:
            print(f"  {v.number} in '{v.context.strip()}': {v.reason}")

    return {
        "output": output,
        "numbers_found": len(numbers),
        "implausible_count": len(implausible),
        "validations": validations
    }

# Test
result = generate_and_validate(
    "What is the height of the Eiffel Tower in meters, and what year was Python first released?"
)
print(f"Response: {result['output']}")
print(f"Numbers checked: {result['numbers_found']}, implausible: {result['implausible_count']}")

# Expected Token Savings: None — range checks are free (no extra LLM calls)
# Environment: ANTHROPIC_API_KEY required
```

### Option 2: Arithmetic Re-Verification

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ArithmeticCheck:
    expression: str
    claimed_result: float
    computed_result: float
    error: float
    is_correct: bool

def extract_arithmetic_claims(text: str) -> list[tuple[str, float]]:
    """
    Find arithmetic expressions and their claimed results.
    e.g., "3 * 4 = 12" or "15% of 200 is 30"
    """
    claims = []

    # Pattern: A op B = C
    op_pattern = re.compile(
        r'(\d+(?:\.\d+)?)\s*([×x\*÷/\+\-])\s*(\d+(?:\.\d+)?)\s*(?:=|equals?|is)\s*(\d+(?:\.\d+)?)',
        re.IGNORECASE
    )
    for m in op_pattern.finditer(text):
        a, op, b, result = m.group(1), m.group(2), m.group(3), m.group(4)
        expr = f"{a} {op} {b}"
        claims.append((expr, float(result)))

    # Pattern: X% of Y is Z
    pct_pattern = re.compile(
        r'(\d+(?:\.\d+)?)\s*%\s+of\s+(\d+(?:\.\d+)?)\s+(?:is|=|equals?)\s+(\d+(?:\.\d+)?)',
        re.IGNORECASE
    )
    for m in pct_pattern.finditer(text):
        pct, base, result = float(m.group(1)), float(m.group(2)), float(m.group(3))
        claims.append((f"{pct}% of {base}", float(result)))

    return claims

def verify_arithmetic(expr: str, claimed: float) -> ArithmeticCheck:
    """Compute the correct result and compare."""
    try:
        # Safe eval for simple arithmetic
        safe_expr = re.sub(r'[×x]', '*', expr)
        safe_expr = re.sub(r'÷', '/', safe_expr)
        # Handle percentage
        if "% of" in safe_expr:
            parts = re.match(r'([\d.]+)% of ([\d.]+)', safe_expr)
            if parts:
                computed = float(parts.group(1)) / 100 * float(parts.group(2))
            else:
                computed = claimed
        else:
            computed = eval(safe_expr, {"__builtins__": {}})

        error = abs(computed - claimed)
        is_correct = error < max(0.01 * abs(computed), 0.001)  # 1% relative or 0.001 absolute

        return ArithmeticCheck(expr, claimed, computed, error, is_correct)
    except Exception:
        return ArithmeticCheck(expr, claimed, claimed, 0.0, True)

def validated_response(prompt: str) -> dict:
    """Generate response, extract and verify all arithmetic claims."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    output = response.content[0].text

    claims = extract_arithmetic_claims(output)
    checks = [verify_arithmetic(expr, claimed) for expr, claimed in claims]
    errors = [c for c in checks if not c.is_correct]

    if errors:
        print(f"[ArithCheck] {len(errors)} arithmetic error(s) found:")
        for c in errors:
            print(f"  '{c.expression}' = {c.claimed_result} (correct: {c.computed_result:.4f})")

        # Request correction
        correction_prompt = f"""The following response contains arithmetic errors:
{output}

Errors found:
{chr(10).join(f"- {c.expression} should equal {c.computed_result:.4f}, not {c.claimed_result}" for c in errors)}

Please provide a corrected response."""

        correction = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": output},
                {"role": "user", "content": correction_prompt}
            ]
        )
        corrected_output = correction.content[0].text
    else:
        corrected_output = output

    return {
        "original": output,
        "corrected": corrected_output,
        "arithmetic_errors": len(errors),
        "checks": checks
    }

# Test
result = validated_response(
    "If a product costs $85 and has a 15% discount, what is the final price? Also, what is 7 * 8?"
)
print(f"Original: {result['original'][:200]}")
print(f"Arithmetic errors found: {result['arithmetic_errors']}")
if result['arithmetic_errors'] > 0:
    print(f"Corrected: {result['corrected'][:200]}")

# Expected Token Savings: Correction call only on error detection; catches ~90% of arithmetic errors
# Environment: ANTHROPIC_API_KEY required
```

### Option 3: Multi-Sample Numerical Consensus

```python
import anthropic
import re
import asyncio
from collections import Counter
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class NumericalConsensus:
    question: str
    answers: list[str]
    extracted_numbers: list[float]
    consensus_number: float | None
    agreement_rate: float
    is_consensus: bool

def extract_primary_number(text: str) -> float | None:
    """Extract the most prominent number from a response."""
    # Look for numbers that appear to be the main answer
    patterns = [
        r'(?:answer|result|is|equals?|=)\s*:?\s*([\d,]+(?:\.\d+)?)',
        r'^([\d,]+(?:\.\d+)?)',   # Number at start of response
        r'([\d,]+(?:\.\d+)?)\s*(?:meters?|km|miles?|years?|dollars?|\$|kg)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            try:
                return float(match.group(1).replace(',', ''))
            except ValueError:
                continue

    # Fallback: first number in text
    match = re.search(r'(\d+(?:,\d+)*(?:\.\d+)?)', text)
    if match:
        try:
            return float(match.group(1).replace(',', ''))
        except ValueError:
            pass
    return None

async def sample_numerical_answer(question: str, sample_id: int) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{"role": "user", "content": f"Answer with a number only: {question}"}]
    )
    return response.content[0].text.strip()

def find_consensus(numbers: list[float], tolerance_pct: float = 0.02) -> tuple[float | None, float]:
    """
    Find consensus number (within tolerance_pct of each other).
    Returns (consensus_value, agreement_rate).
    """
    if not numbers:
        return None, 0.0

    # Round to significant figures for grouping
    def round_sig(x: float, sig: int = 4) -> float:
        from math import log10, floor
        if x == 0:
            return 0.0
        d = ceil(sig - 1 - log10(abs(x)))
        return round(x, d)

    from math import ceil, log10
    rounded = [round_sig(n) for n in numbers]
    counts = Counter(rounded)
    top_value, top_count = counts.most_common(1)[0]

    # Check for near-matches within tolerance
    cluster = [n for n in numbers if abs(n - top_value) / max(abs(top_value), 1e-10) <= tolerance_pct]
    agreement = len(cluster) / len(numbers)

    if agreement >= 0.6:
        consensus = sum(cluster) / len(cluster)
        return consensus, agreement

    return None, agreement

async def numerical_consensus_answer(question: str, n_samples: int = 5) -> NumericalConsensus:
    """Sample multiple answers and find numerical consensus."""
    raw_answers = await asyncio.gather(*[
        sample_numerical_answer(question, i) for i in range(n_samples)
    ])

    numbers = [extract_primary_number(a) for a in raw_answers]
    valid_numbers = [n for n in numbers if n is not None]

    consensus_val, agreement = find_consensus(valid_numbers)

    return NumericalConsensus(
        question=question,
        answers=list(raw_answers),
        extracted_numbers=valid_numbers,
        consensus_number=consensus_val,
        agreement_rate=agreement,
        is_consensus=consensus_val is not None
    )

async def main():
    questions = [
        "How many bones are in the adult human body?",
        "What is the boiling point of water in Celsius at sea level?",
        "In what year was the first iPhone released?",
        "What is the approximate speed of light in km/s?",
    ]

    for q in questions:
        result = await numerical_consensus_answer(q, n_samples=5)
        status = "CONSENSUS" if result.is_consensus else "NO CONSENSUS"
        print(f"[{status}] {q}")
        print(f"  Numbers sampled: {result.extracted_numbers}")
        print(f"  Consensus: {result.consensus_number} (agreement: {result.agreement_rate:.0%})")
        print()

asyncio.run(main())

# Expected Token Savings: 5x tokens vs single, but catches ~60% of numerical hallucinations
# Environment: ANTHROPIC_API_KEY required, uses asyncio
```

### Option 4: LLM-Based Numerical Fact Checker

```python
import anthropic
import re
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class FactCheckResult:
    claim: str
    verdict: str   # "correct" | "incorrect" | "uncertain" | "unverifiable"
    confidence: float
    correction: str | None
    source_hint: str

def extract_numerical_claims(text: str) -> list[str]:
    """Extract sentences containing numerical claims."""
    sentences = re.split(r'[.!?]+', text)
    numerical_pattern = re.compile(r'\d+(?:\.\d+)?(?:\s*%|\s*(?:million|billion|thousand|km|meters?|years?|dollars?))?')
    return [s.strip() for s in sentences if numerical_pattern.search(s) and len(s.strip()) > 10]

def fact_check_claim(claim: str) -> FactCheckResult:
    """Use a second LLM call to fact-check a numerical claim."""
    prompt = f"""You are a fact-checker. Evaluate this numerical claim for accuracy:

Claim: "{claim}"

Respond in JSON:
{{
  "verdict": "correct" | "incorrect" | "uncertain" | "unverifiable",
  "confidence": 0.0-1.0,
  "correction": "<corrected version if incorrect, null otherwise>",
  "source_hint": "<what knowledge base this comes from>"
}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        match = re.search(r'\{.*\}', response.content[0].text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return FactCheckResult(
                claim=claim,
                verdict=data.get("verdict", "uncertain"),
                confidence=float(data.get("confidence", 0.5)),
                correction=data.get("correction"),
                source_hint=data.get("source_hint", "")
            )
    except (json.JSONDecodeError, ValueError, KeyError):
        pass

    return FactCheckResult(claim, "uncertain", 0.5, None, "")

def generate_and_fact_check(prompt: str) -> dict:
    """Generate response, extract numerical claims, fact-check each."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    output = response.content[0].text

    claims = extract_numerical_claims(output)
    print(f"[FactCheck] Checking {len(claims)} numerical claim(s)...")

    results = [fact_check_claim(c) for c in claims]
    incorrect = [r for r in results if r.verdict == "incorrect" and r.confidence > 0.7]

    if incorrect:
        corrections_text = "\n".join(
            f"- {r.claim} → {r.correction}" for r in incorrect if r.correction
        )
        print(f"[FactCheck] {len(incorrect)} incorrect claim(s) found:\n{corrections_text}")

    return {
        "output": output,
        "claims_checked": len(claims),
        "incorrect": len(incorrect),
        "uncertain": sum(1 for r in results if r.verdict == "uncertain"),
        "fact_check_results": results
    }

# Test
result = generate_and_fact_check(
    "Tell me about the Eiffel Tower: its height, construction year, and approximate weight."
)
print(f"\nResponse: {result['output'][:300]}")
print(f"\nClaims checked: {result['claims_checked']}, incorrect: {result['incorrect']}")
for r in result['fact_check_results']:
    icon = {"correct": "✓", "incorrect": "✗", "uncertain": "?", "unverifiable": "~"}.get(r.verdict, "?")
    print(f"  [{icon}] {r.verdict} ({r.confidence:.0%}): {r.claim[:60]}")

# Expected Token Savings: Checker adds ~200 tokens/claim; prevents costly re-prompts from user corrections
# Environment: ANTHROPIC_API_KEY required
```

### Option 5: Unit and Dimensional Consistency Checker

```python
import anthropic
import re
from dataclasses import dataclass
from enum import Enum

client = anthropic.Anthropic()

class UnitCategory(str, Enum):
    LENGTH = "length"
    WEIGHT = "weight"
    TEMPERATURE = "temperature"
    SPEED = "speed"
    TIME = "time"
    CURRENCY = "currency"
    PERCENTAGE = "percentage"

# Unit normalization to SI base
UNIT_CONVERSIONS: dict[str, tuple[UnitCategory, float]] = {
    # Length → meters
    "m": (UnitCategory.LENGTH, 1.0),
    "meters": (UnitCategory.LENGTH, 1.0),
    "meter": (UnitCategory.LENGTH, 1.0),
    "km": (UnitCategory.LENGTH, 1000.0),
    "kilometers": (UnitCategory.LENGTH, 1000.0),
    "ft": (UnitCategory.LENGTH, 0.3048),
    "feet": (UnitCategory.LENGTH, 0.3048),
    "miles": (UnitCategory.LENGTH, 1609.34),
    "mile": (UnitCategory.LENGTH, 1609.34),
    # Weight → kg
    "kg": (UnitCategory.WEIGHT, 1.0),
    "kilograms": (UnitCategory.WEIGHT, 1.0),
    "lbs": (UnitCategory.WEIGHT, 0.4536),
    "pounds": (UnitCategory.WEIGHT, 0.4536),
    "g": (UnitCategory.WEIGHT, 0.001),
    "grams": (UnitCategory.WEIGHT, 0.001),
    # Speed → m/s
    "m/s": (UnitCategory.SPEED, 1.0),
    "km/h": (UnitCategory.SPEED, 1/3.6),
    "mph": (UnitCategory.SPEED, 0.447),
}

@dataclass
class UnitClaim:
    value: float
    unit: str
    category: UnitCategory
    normalized_value: float
    context: str

@dataclass
class ConsistencyIssue:
    claim_a: UnitClaim
    claim_b: UnitClaim
    ratio: float
    description: str

def extract_unit_claims(text: str) -> list[UnitClaim]:
    """Extract all number+unit pairs from text."""
    claims = []
    pattern = re.compile(
        r'([\d,]+(?:\.\d+)?)\s*(m/s|km/h|mph|km|meters?|feet?|ft|miles?|kg|kilograms?|lbs?|pounds?|grams?|g)\b',
        re.IGNORECASE
    )
    for match in pattern.finditer(text):
        try:
            value = float(match.group(1).replace(',', ''))
            unit = match.group(2).lower().rstrip('s') if match.group(2).lower().rstrip('s') in UNIT_CONVERSIONS else match.group(2).lower()
            unit_key = match.group(2).lower()
            if unit_key in UNIT_CONVERSIONS:
                category, factor = UNIT_CONVERSIONS[unit_key]
            elif unit_key.rstrip('s') in UNIT_CONVERSIONS:
                category, factor = UNIT_CONVERSIONS[unit_key.rstrip('s')]
            else:
                continue

            start = max(0, match.start() - 30)
            end = min(len(text), match.end() + 30)
            context = text[start:end]

            claims.append(UnitClaim(
                value=value, unit=match.group(2),
                category=category,
                normalized_value=value * factor,
                context=context
            ))
        except ValueError:
            continue
    return claims

def check_unit_consistency(claims: list[UnitClaim]) -> list[ConsistencyIssue]:
    """Find dimensional inconsistencies."""
    issues = []
    by_category: dict[UnitCategory, list[UnitClaim]] = {}
    for c in claims:
        by_category.setdefault(c.category, []).append(c)

    # Known plausible ranges in SI units
    si_ranges = {
        UnitCategory.LENGTH: (0.001, 50_000_000),  # 1mm to Earth circumference
        UnitCategory.WEIGHT: (0.0001, 600_000_000),  # 0.1g to blue whale
        UnitCategory.SPEED: (0.0001, 300_000_000),  # still to speed of light
    }

    for category, cat_claims in by_category.items():
        if category not in si_ranges:
            continue
        min_si, max_si = si_ranges[category]
        for claim in cat_claims:
            if not (min_si <= claim.normalized_value <= max_si):
                issues.append(ConsistencyIssue(
                    claim_a=claim, claim_b=claim,
                    ratio=claim.normalized_value,
                    description=f"{claim.value} {claim.unit} = {claim.normalized_value:.2f} SI units — outside plausible range [{min_si}, {max_si}]"
                ))

    return issues

def validate_units(prompt: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    output = response.content[0].text

    claims = extract_unit_claims(output)
    issues = check_unit_consistency(claims)

    print(f"[UnitCheck] Found {len(claims)} unit claims, {len(issues)} issues")
    for issue in issues:
        print(f"  ISSUE: {issue.description}")

    return {"output": output, "claims": len(claims), "issues": len(issues)}

# Test
result = validate_units("How fast does light travel? Also, how tall is Mount Everest?")
print(f"Response: {result['output'][:300]}")

# Expected Token Savings: None — unit checking is pure computation, no extra LLM calls
# Environment: ANTHROPIC_API_KEY required
```

### Option 6: Grounded Numerical Response with Source Citation

```python
import anthropic
import re
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

# Known verified facts database
VERIFIED_FACTS: dict[str, dict] = {
    "eiffel_tower_height": {"value": 330, "unit": "meters", "note": "including antenna"},
    "speed_of_light_km_s": {"value": 299792, "unit": "km/s"},
    "human_body_bones": {"value": 206, "unit": "bones", "note": "adult human"},
    "water_boiling_celsius": {"value": 100, "unit": "°C", "note": "at sea level"},
    "earth_radius_km": {"value": 6371, "unit": "km", "note": "mean radius"},
    "python_first_release_year": {"value": 1991, "unit": "year"},
    "iphone_first_release_year": {"value": 2007, "unit": "year"},
    "mount_everest_height_m": {"value": 8849, "unit": "meters", "note": "2020 survey"},
}

@dataclass
class GroundedFact:
    key: str
    value: float
    unit: str
    note: str
    grounded: bool

def ground_numerical_response(
    prompt: str,
    verified_facts: dict[str, dict] = None
) -> dict:
    """
    Generate response with verified facts injected as context.
    Model is given ground truth numbers to anchor its response.
    """
    facts = verified_facts or VERIFIED_FACTS

    # Build verified facts context
    facts_text = "\n".join(
        f"- {key}: {data['value']} {data['unit']}" + (f" ({data['note']})" if data.get('note') else "")
        for key, data in facts.items()
    )

    grounded_system = f"""You are a factual assistant. Use ONLY the following verified facts when answering about these topics. Do not deviate from these numbers.

Verified facts:
{facts_text}

If asked about something not in this list, say so explicitly rather than guessing."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=grounded_system,
        messages=[{"role": "user", "content": prompt}]
    )
    output = response.content[0].text

    # Verify the response uses grounded numbers
    grounded_hits = []
    for key, data in facts.items():
        val_str = str(int(data['value'])) if data['value'] == int(data['value']) else str(data['value'])
        if val_str in output:
            grounded_hits.append(key)

    print(f"[Grounded] Response uses {len(grounded_hits)} verified facts: {grounded_hits}")
    return {
        "output": output,
        "grounded_facts_used": grounded_hits,
        "total_verified_facts": len(facts)
    }

# Test
result = ground_numerical_response(
    "How tall is the Eiffel Tower? When was Python first released? How many bones does an adult human have?"
)
print(f"Response: {result['output']}")

# Expected Token Savings: Grounding prevents hallucination without re-sampling; adds ~200 tokens to system prompt
# Environment: ANTHROPIC_API_KEY required
```

## Comparison

| Option | Detection Method | Extra Cost | Catches | Best Use Case |
|--------|----------------|------------|---------|---------------|
| Range-Based Plausibility | Domain rules | None | Out-of-range values | Well-understood numeric domains |
| Arithmetic Re-Verification | Python eval | +1 call on error | Math errors | Responses with calculations |
| Multi-Sample Consensus | 5x sampling | 5x tokens | Statistical outliers | Critical single-number answers |
| LLM Fact Checker | Classifier call | +N LLM calls | Wrong known facts | General factual claims |
| Unit Consistency Checker | Dimensional analysis | None | Unit/scale errors | Technical/scientific responses |
| Grounded Numerical Response | Fact injection | +200 sys tokens | Anchors to truth | Known-fact domains (heights, dates) |
