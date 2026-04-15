---
layout: solution
title: "Agent Changes Numbers When Summarizing or Reformatting"
category: hallucination
description: "An agent is asked to summarize, reformat, or translate a document containing numbers — prices, counts, percentages, dates, IDs. The output contains different numbers than the input. $1,249 becomes $1,249.00 or $1,200. 47% becomes 45% or 'nearly half'. Order ID 8842031 becomes 8842013. These numeric mutations are hard to catch and can cause financial, compliance, or operational errors."
tags: [hallucination, numbers, summarization, reformatting, data-integrity, financial, verification, faithfulness]
---

# Agent Changes Numbers When Summarizing or Reformatting

## Symptom
- Summary says "revenue increased by 23%" but the source says 22.7%
- Agent rounds $1,249.99 to "$1,250" or "about $1,200" without being asked to
- Date "2024-03-15" becomes "March 2024" or "mid-March 2024" in reformatted output
- Customer ID 7749283 appears as 7749238 (transposed digits) in translated document
- "47 out of 312 users" becomes "15% of users" (correctly computed but not in the source)
- Agent writes "approximately 3 million" when source says "2,847,293"

## Root Cause
Numbers require exact reproduction, but LLMs are trained on data where summarization inherently involves paraphrasing — including approximating quantities. The model applies the same paraphrase tendency to exact values that must not be changed. Reformatting tasks (change language, change layout, extract to table) are especially prone because the model attends to structure, not to value preservation. The fix is to explicitly instruct exact numeric reproduction and verify numbers in output against source.

## Fix

### Option 1: Explicit numeric fidelity instruction + extraction-based approach

```python
import anthropic
import re
import json

client = anthropic.Anthropic()

NUMERIC_FIDELITY_SYSTEM = """You are a document reformatter.

## CRITICAL: Numeric Fidelity Rules

ALL numbers in the source document must appear UNCHANGED in your output:
- Dollar amounts: reproduce exactly as written ($1,249.99 stays $1,249.99)
- Percentages: reproduce exactly (22.7% stays 22.7%, not 23% or "about 23%")
- Counts: reproduce exactly (47 users stays 47 users, not "nearly 50")
- Dates: reproduce exactly (2024-03-15 stays 2024-03-15, do not reformat)
- IDs and codes: reproduce character-for-character (never transpose digits)
- Phone numbers, account numbers: reproduce exactly
- Measurements: preserve all decimal places as written

DO NOT:
- Round numbers unless explicitly asked
- Convert between units
- Replace exact numbers with approximations ("about", "nearly", "roughly", "approximately")
- Compute derived values not in the source (do not calculate percentages from counts)

If you are uncertain about a number, copy it verbatim from the source.
"""


def extract_numbers_from_text(text: str) -> list[str]:
    """Extract all numeric tokens from text for comparison."""
    patterns = [
        r'\$[\d,]+(?:\.\d{2})?',           # dollar amounts
        r'\d+(?:,\d{3})*(?:\.\d+)?%',      # percentages
        r'\b\d{4}-\d{2}-\d{2}\b',          # ISO dates
        r'\b\d+(?:,\d{3})+\b',             # large numbers with commas
        r'\b\d+\.\d+\b',                   # decimals
        r'\b\d{5,}\b',                     # long integers (IDs etc)
    ]
    numbers = []
    for pattern in patterns:
        numbers.extend(re.findall(pattern, text))
    return numbers


def verify_numeric_preservation(source: str, output: str) -> list[str]:
    """
    Check that all numbers from source appear in output.
    Returns list of numbers from source that are missing or changed in output.
    """
    source_numbers = set(extract_numbers_from_text(source))
    output_numbers = set(extract_numbers_from_text(output))
    missing = source_numbers - output_numbers
    return sorted(missing)


def reformat_with_numeric_fidelity(source_text: str, format_instruction: str) -> dict:
    """
    Reformat a document while verifying numeric preservation.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=NUMERIC_FIDELITY_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Reformat this document as follows: {format_instruction}\n\nDocument:\n{source_text}"
        }]
    )
    output = response.content[0].text
    missing = verify_numeric_preservation(source_text, output)

    return {
        "output": output,
        "numeric_violations": missing,
        "clean": len(missing) == 0
    }


# Example:
source = """
Q3 Financial Summary
Revenue: $2,847,293.50 (up 22.7% from Q2)
Customer count: 47,312 active users
Churn rate: 3.8%
Account ID: 7749283-A
Report date: 2024-09-30
"""

result = reformat_with_numeric_fidelity(
    source,
    "Convert to a JSON object with keys: revenue, growth_rate, customers, churn, account_id, date"
)
print(result["output"])
if result["numeric_violations"]:
    print(f"WARNING: These numbers changed: {result['numeric_violations']}")
```

### Option 2: Number-anchored extraction — extract numbers first, then format

```python
import anthropic
import re
import json

client = anthropic.Anthropic()

def extract_numeric_inventory(text: str) -> dict:
    """
    Step 1: Extract all numbers from the source before reformatting.
    This inventory is used to verify the output.
    """
    inventory = {}
    # Match label: value patterns:
    patterns = [
        r'([\w\s]+):\s*(\$?[\d,]+(?:\.\d+)?%?)',
        r'([\w\s]+)\s+(?:of|is|was|=)\s*(\$?[\d,]+(?:\.\d+)?%?)',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            label = match.group(1).strip().lower()
            value = match.group(2).strip()
            inventory[label] = value
    return inventory


def two_step_reformat(source: str, target_format: str) -> dict:
    """
    Two-step approach:
    1. Extract numeric values (grounding step)
    2. Reformat with extracted values anchored in the prompt
    """
    # Step 1: extract numbers as ground truth
    inventory = extract_numeric_inventory(source)
    inventory_str = json.dumps(inventory, indent=2)

    # Step 2: reformat with numbers locked in
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Reformat the document below as: {target_format}

LOCKED VALUES (use these exact values — do not change any of them):
{inventory_str}

Document to reformat:
{source}"""
        }]
    )
    return {
        "output": response.content[0].text,
        "locked_values": inventory
    }


source = """
Monthly Report — October 2024
Total sales: $184,250.00
Units sold: 1,293
Return rate: 4.2%
Net margin: 18.7%
Reference: RPT-2024-10-001
"""

result = two_step_reformat(source, "a markdown table with columns: Metric, Value")
print(result["output"])
print("\nLocked values used:", result["locked_values"])
```

### Option 3: Structured output with numeric schema — force exact field types

```python
import anthropic
import json
from decimal import Decimal

client = anthropic.Anthropic()

# When reformatting to structured output, define exact numeric types
# so the model can't paraphrase (it must fit the schema).

EXTRACT_TOOL = {
    "name": "extract_financial_data",
    "description": "Extract financial metrics from a report",
    "input_schema": {
        "type": "object",
        "properties": {
            "revenue_dollars": {
                "type": "number",
                "description": "Revenue in dollars, exact value as stated (e.g., 2847293.50)"
            },
            "revenue_growth_percent": {
                "type": "number",
                "description": "Revenue growth percentage, exact value as stated (e.g., 22.7)"
            },
            "customer_count": {
                "type": "integer",
                "description": "Number of active customers, exact integer as stated"
            },
            "churn_rate_percent": {
                "type": "number",
                "description": "Churn rate percentage, exact value as stated (e.g., 3.8)"
            },
            "report_date": {
                "type": "string",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                "description": "Report date in YYYY-MM-DD format exactly as stated"
            },
            "report_id": {
                "type": "string",
                "description": "Report ID or reference number, exact string as stated"
            }
        },
        "required": ["revenue_dollars", "customer_count", "report_date"]
    }
}


def extract_with_schema(report_text: str) -> dict:
    """
    Use tool_choice="any" to force structured extraction.
    Numbers go into typed schema fields — can't be paraphrased.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "any"},
        messages=[{
            "role": "user",
            "content": f"Extract all financial metrics from this report. Use exact values only:\n\n{report_text}"
        }]
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "extract_financial_data":
            return block.input

    raise ValueError("Model did not call extraction tool")


report = """
Q4 2024 Financial Report
Revenue reached $3,142,891.75, a 31.4% increase over Q3.
We served 52,847 active customers with a churn rate of 2.9%.
Date: 2024-12-31
Reference: FY2024-Q4-FINAL
"""

data = extract_with_schema(report)
print(json.dumps(data, indent=2))
# {
#   "revenue_dollars": 3142891.75,       ← exact
#   "revenue_growth_percent": 31.4,      ← exact
#   "customer_count": 52847,             ← exact integer
#   "churn_rate_percent": 2.9,           ← exact
#   "report_date": "2024-12-31",         ← exact format
#   "report_id": "FY2024-Q4-FINAL"       ← exact string
# }
```

### Option 4: Numeric diff — automated regression test for summaries

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class NumericMutation:
    original: str
    mutated: str
    context_original: str   # surrounding text in source
    context_mutated: str    # surrounding text in output

def find_numeric_mutations(
    source: str,
    output: str,
    context_window: int = 30
) -> list[NumericMutation]:
    """
    Find numbers that appear in the source but are absent or changed in the output.
    Returns detailed mutation records.
    """
    number_pattern = re.compile(r'\b[\d,]+(?:\.\d+)?%?\b|\$[\d,]+(?:\.\d+)?')
    source_numbers = {}

    for match in number_pattern.finditer(source):
        num = match.group()
        start, end = match.start(), match.end()
        ctx = source[max(0, start - context_window):end + context_window]
        source_numbers[num] = ctx

    output_numbers = set(match.group() for match in number_pattern.finditer(output))
    mutations = []

    for num, ctx in source_numbers.items():
        if num not in output_numbers:
            # Find what the output has in a similar context
            mutations.append(NumericMutation(
                original=num,
                mutated="[missing]",
                context_original=ctx.strip(),
                context_mutated=""
            ))

    return mutations


def summarize_with_verification(
    source: str,
    summary_instruction: str = "Summarize this document"
) -> dict:
    """Summarize and verify numeric preservation."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="When summarizing, preserve all numbers exactly as they appear in the source. Never approximate or round.",
        messages=[{"role": "user", "content": f"{summary_instruction}:\n\n{source}"}]
    )
    summary = response.content[0].text
    mutations = find_numeric_mutations(source, summary)

    if mutations:
        print(f"[numeric-diff] {len(mutations)} mutation(s) detected:")
        for m in mutations:
            print(f"  Original: {m.original!r} in context: ...{m.context_original}...")
            print(f"  Mutated to: {m.mutated!r}")

    return {
        "summary": summary,
        "mutations": [{"original": m.original, "mutated": m.mutated} for m in mutations],
        "clean": len(mutations) == 0
    }
```

### Option 5: Number-preservation chain — summarize numbers separately

```python
import anthropic
import re
import json

client = anthropic.Anthropic()

def extract_exact_numbers(text: str) -> list[dict]:
    """Extract numbers with their labels/context for anchoring."""
    results = []
    # Match "label: number" or "number label" patterns
    label_value = re.findall(
        r'([A-Za-z][\w\s]{2,30}):\s*([\$\d][,\d\.%\$]+)',
        text
    )
    for label, value in label_value:
        results.append({"label": label.strip(), "value": value.strip()})
    return results


def number_preserving_summary(source: str) -> str:
    """
    Summarize in two passes:
    1. Extract exact numbers (non-creative, deterministic)
    2. Write narrative with numbers locked in
    """
    # Pass 1: extract all numbers
    numbers = extract_exact_numbers(source)
    number_anchor = json.dumps(numbers)

    # Pass 2: write summary with anchored numbers
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Write a one-paragraph summary of this document.
You MUST use these exact numbers in your summary (do not change any):
{number_anchor}

Document:
{source}"""
        }]
    )
    summary = response.content[0].text

    # Verify anchors appear in summary:
    for item in numbers:
        if item["value"] not in summary:
            print(f"WARNING: {item['label']}: {item['value']} not found in summary")

    return summary
```

### Option 6: Translation/language-change numeric guard

```python
import anthropic
import re

client = anthropic.Anthropic()

# Translation is especially prone to numeric mutation because
# number formatting varies by locale (1,000.00 vs 1.000,00).
# Anchor numbers in the translation prompt.

def translate_preserving_numbers(
    source: str,
    target_language: str
) -> str:
    """
    Translate text while preserving all numbers in their original format.
    """
    # Extract all numeric tokens before translation:
    number_pattern = re.compile(
        r'\b[\d]{1,3}(?:,\d{3})*(?:\.\d+)?%?\b'  # formatted numbers
        r'|\$[\d,]+(?:\.\d{2})?'                   # dollar amounts
        r'|\b\d{4}-\d{2}-\d{2}\b'                  # dates
        r'|\b\d{10,}\b'                             # long IDs
    )
    numbers_in_source = number_pattern.findall(source)
    numbers_list = ", ".join(f'"{n}"' for n in set(numbers_in_source))

    system = f"""You are a translator.
Translate to {target_language}.

CRITICAL: These numbers must appear UNCHANGED in your translation (same digits, same format):
{numbers_list}

Do NOT convert number formats for the target locale.
Do NOT translate or reformat dates, IDs, prices, or percentages."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": f"Translate:\n{source}"}]
    )
    translated = response.content[0].text

    # Verify all source numbers appear in translation:
    missing = [n for n in set(numbers_in_source) if n not in translated]
    if missing:
        print(f"WARNING: Numbers changed in translation: {missing}")

    return translated


source_en = """
Contract #C-20241115-8847
Amount: $142,500.00
Due date: 2025-03-31
Late fee: 2.5% per month
Customer ID: 4429183
"""

translated = translate_preserving_numbers(source_en, "Spanish")
print(translated)
# Numbers remain: $142,500.00, 2025-03-31, 2.5%, 4429183, C-20241115-8847
```

## Numeric Mutation Risk by Operation Type

| Operation | Mutation Risk | Most Common Mutation |
|---|---|---|
| Summarization | High | Rounding, approximation ("about X") |
| Translation | High | Locale reformatting (1,000 → 1.000) |
| Reformatting to table | Medium | Digit transposition, missing decimals |
| Format conversion (MD→JSON) | Medium | Type coercion loses decimal places |
| Language simplification | High | Converting to approximations |
| Extraction to structured output | Low (with schema) | Eliminated by typed schema |

## Expected Token Savings
Verification passes (Options 1, 4) add ~100–300 tokens. The cost of undetected numeric mutations in financial, legal, or medical contexts far exceeds this overhead. Use structured extraction (Option 3) when possible — it eliminates numeric mutations entirely at zero extra cost.

## Environment
- Any agent that summarizes, reformats, translates, or extracts from documents containing numbers; critical for financial reports, contracts, medical records, and compliance documents; structured extraction via tool schemas (Option 3) is the most reliable fix for structured outputs; the numeric fidelity system prompt (Option 1) is the minimum viable fix for free-form summarization; combine Options 1 + verification (Option 4) for production use
