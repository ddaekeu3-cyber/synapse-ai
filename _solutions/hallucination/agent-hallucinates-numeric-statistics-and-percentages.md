---
layout: solution
title: "Agent Hallucinates Numeric Statistics and Percentages"
category: hallucination
description: "Agent confidently cites specific numbers, percentages, growth rates, and statistics that it invented, presenting training-data-era figures as current facts or fabricating plausible-sounding values entirely."
tags: [hallucination, statistics, numbers, grounding, tool-use, verification]
---

## Symptom

The agent states "65% of enterprises use AI agents in production" or "Python grew 42% last year" without citing a source. Users later discover the numbers are wrong or unverifiable. Financial reports, research summaries, or dashboards built on agent output contain fabricated statistics. When asked for a source, the agent either invents one or admits it was estimating.

## Root Cause

Claude's training data contains millions of statistics, but the model cannot reliably distinguish which numbers are current, authoritative, or even real. Numeric values are high-confidence outputs — the model has no internal signal that says "I'm less certain about this specific percentage than about this word's meaning." Numbers that sound plausible are generated fluently without any uncertainty marker. Unlike factual claims that can be cross-referenced by the model internally, stale or invented statistics can't be self-detected.

## Fix

### Option 1: Explicit no-numbers-without-sources instruction

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a research assistant that provides accurate, well-sourced information.

<instructions>
CRITICAL RULE: Never state a specific number, percentage, statistic, or quantitative claim without:
1. Citing the source (organization, report name, year)
2. Noting if the data may be outdated

If you do not have a verifiable source for a statistic, you MUST:
- Describe the trend qualitatively instead ("adoption has grown significantly")
- Or explicitly say "I don't have a verified figure for this"
- Or suggest where the user could find authoritative data

Examples of FORBIDDEN output:
  ✗ "Python is used by 48% of developers"
  ✗ "The AI market grew 35% last year"
  ✗ "65% of Fortune 500 companies use cloud services"

Examples of CORRECT output:
  ✓ "According to Stack Overflow's 2023 Developer Survey, Python was used by 49% of respondents"
  ✓ "AI adoption has grown significantly in recent years (for current figures, see Gartner or IDC reports)"
  ✓ "I don't have a verified market size figure — McKinsey and IDC publish annual AI market reports"
</instructions>"""


def ask_research_question(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


# Test with statistic-heavy questions
questions = [
    "What percentage of companies have adopted AI?",
    "How much has Python's popularity grown in the last 5 years?",
    "What is the current size of the cloud computing market?",
]

for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask_research_question(q)[:300]}\n")
```

**Expected Token Savings:** Prompt instruction prevents hallucination-recovery turns where the user challenges a fabricated statistic and the agent must backtrack.
**Environment:** Python 3.9+; instruction approach requires no additional tools; effective for conversational agents.

---

### Option 2: Statistics tool that requires real lookups

```python
import anthropic

client = anthropic.Anthropic()

# Verified statistics database (in production: connect to authoritative data sources)
VERIFIED_STATS = {
    "python_developer_usage_2023": {
        "value": "49%",
        "source": "Stack Overflow Developer Survey 2023",
        "url": "https://survey.stackoverflow.co/2023/",
        "notes": "Among all respondents who code",
    },
    "global_cloud_market_2023": {
        "value": "$591 billion",
        "source": "Gartner (2023)",
        "notes": "Worldwide public cloud services revenue",
    },
    "remote_work_us_2023": {
        "value": "12.7%",
        "source": "Bureau of Labor Statistics, 2023",
        "notes": "Full-time remote workers in US",
    },
}


def lookup_statistic(topic: str, year: int | None = None) -> str:
    """
    Look up a statistic from the verified database.
    Returns a structured result with source attribution.
    """
    # Simple keyword match (in production: semantic search)
    topic_lower = topic.lower()
    for key, stat in VERIFIED_STATS.items():
        key_words = key.replace("_", " ")
        if any(word in topic_lower for word in key_words.split()):
            source_info = f"{stat['source']}"
            if "url" in stat:
                source_info += f" ({stat['url']})"
            return (
                f"VERIFIED STATISTIC:\n"
                f"  Value: {stat['value']}\n"
                f"  Source: {source_info}\n"
                f"  Notes: {stat.get('notes', '')}\n"
            )
    return (
        f"NO VERIFIED DATA FOUND for '{topic}'.\n"
        f"Do not estimate or guess. Tell the user to consult authoritative sources "
        f"such as Gartner, IDC, Statista, or government statistical agencies."
    )


TOOLS = [
    {
        "name": "lookup_statistic",
        "description": (
            "Look up a verified statistic or data point. "
            "ALWAYS call this tool before stating any number, percentage, or quantitative claim. "
            "If this tool returns no data, do not guess — say so explicitly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "The statistical topic to look up"},
                "year": {"type": "integer", "description": "Specific year if relevant"},
            },
            "required": ["topic"],
        },
    }
]

SYSTEM = """You are a data-driven research assistant.

<instructions>
- Before stating ANY numeric claim, call lookup_statistic.
- If lookup_statistic returns no verified data, describe the topic qualitatively and suggest where to find authoritative data.
- Never state a number without a verified source.
- Always include the source when presenting statistics.
</instructions>"""

messages = [{"role": "user", "content": "What percentage of developers use Python?"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            result = lookup_statistic(
                block.input.get("topic", ""),
                block.input.get("year"),
            )
            print(f"[lookup_statistic] {result[:200]}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Mandatory lookup tool prevents hallucinated statistics that trigger user correction turns; verification pass costs ~200 tokens vs. 1–3 correction turns.
**Environment:** Python 3.9+; replace `VERIFIED_STATS` dict with Statista API, internal data warehouse, or web search.

---

### Option 3: Numeric claim detector and post-generation auditor

```python
import re
import anthropic

client = anthropic.Anthropic()

# Patterns that indicate quantitative claims
NUMERIC_CLAIM_PATTERNS = [
    re.compile(r"\b\d+\.?\d*\s*%"),                           # Percentages: 42%, 3.5%
    re.compile(r"\$\s*\d+[\d.,]*\s*(?:billion|million|trillion|M|B|T)\b", re.I),  # Dollar amounts
    re.compile(r"\b\d+[\d.,]*\s*(?:billion|million|trillion)\b", re.I),  # Large numbers
    re.compile(r"\b(?:grew|increased|decreased|rose|fell|dropped)\s+(?:by\s+)?\d+", re.I),  # Growth claims
    re.compile(r"\b(?:doubled|tripled|quadrupled)\b", re.I),  # Multiplier claims
    re.compile(r"\b\d+\s*(?:out of|in)\s*\d+\b"),            # Ratios
    re.compile(r"\b(?:most|majority|minority|half|quarter|third)\s+of\b", re.I),  # Fraction claims
]

CITATION_PATTERNS = [
    re.compile(r"according to", re.I),
    re.compile(r"(?:source|report|study|survey|data):", re.I),
    re.compile(r"\[\d+\]"),          # Reference markers
    re.compile(r"\((?:20\d\d|19\d\d)\)"),  # Year citations
    re.compile(r"per\s+(?:the\s+)?[A-Z][a-z]+ [A-Z]"),  # "per the Gartner Report"
]


def detect_uncited_claims(text: str) -> list[str]:
    """Find numeric claims in text that lack citations."""
    uncited = []
    sentences = re.split(r"[.!?]+", text)

    for sentence in sentences:
        has_number = any(p.search(sentence) for p in NUMERIC_CLAIM_PATTERNS)
        has_citation = any(p.search(sentence) for p in CITATION_PATTERNS)

        if has_number and not has_citation:
            uncited.append(sentence.strip())

    return uncited


def audit_and_revise(user_question: str) -> str:
    """
    Generate a response, audit it for uncited statistics, then revise if needed.
    """
    # First pass: generate response
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="You are a research assistant. Provide detailed, informative answers.",
        messages=[{"role": "user", "content": user_question}],
    )
    initial_response = response.content[0].text

    # Audit for uncited claims
    uncited = detect_uncited_claims(initial_response)

    if not uncited:
        return initial_response

    # Second pass: revise with explicit flagging of uncited claims
    print(f"[Audit] Found {len(uncited)} uncited numeric claim(s):")
    for claim in uncited:
        print(f"  ⚠ {claim[:100]}")

    revision_prompt = f"""You wrote this response:

{initial_response}

The following statements contain numeric claims without citations:
{chr(10).join(f'- "{c}"' for c in uncited)}

Revise your response to either:
1. Add a source for each numeric claim (organization, report, year), or
2. Replace the specific number with a qualitative description ("has grown significantly"), or
3. Explicitly note uncertainty ("exact figures vary by source")

Do not remove factual content — only add attribution or soften unsupported claims."""

    revised = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[
            {"role": "user", "content": user_question},
            {"role": "assistant", "content": initial_response},
            {"role": "user", "content": revision_prompt},
        ],
    )
    return revised.content[0].text


result = audit_and_revise("How has AI adoption grown among enterprises in recent years?")
print(f"\nFinal response:\n{result[:500]}")
```

**Expected Token Savings:** Audit catches hallucinated statistics before they reach the user, preventing downstream correction turns that cost 2–5× the original turn.
**Environment:** Python 3.9+; regex audit adds <1ms; revision pass costs ~300 extra tokens but prevents user-reported errors.

---

### Option 4: Step-back prompting to derive qualitative trends before quantifying

```python
import anthropic

client = anthropic.Anthropic()

STEP_BACK_SYSTEM = """You are a careful research analyst who separates known trends from specific statistics.

When asked about quantitative topics:
1. First state what you know with HIGH confidence (trends, directions, relative comparisons)
2. Then explicitly flag any specific numbers: say "I believe X but am not certain of the exact figure"
3. Recommend where to verify any numbers you do cite

Never present a statistic as verified fact unless you have explicit training data supporting it."""


def answer_with_step_back(question: str) -> str:
    """
    Two-turn approach:
    Turn 1: Extract what is qualitatively known (Haiku — cheap)
    Turn 2: Answer the question grounded by Turn 1 (Sonnet — accurate)
    """
    # Step back: what do we know qualitatively?
    stepback_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            "You identify general trends and principles related to a question, "
            "WITHOUT citing any specific numbers or statistics. "
            "Focus only on directions, comparisons, and widely-known qualitative facts."
        ),
        messages=[{
            "role": "user",
            "content": f"What are the general trends and principles related to: {question}\n\nDo NOT include any specific numbers or percentages.",
        }],
    )
    qualitative_context = stepback_response.content[0].text

    # Answer grounded in qualitative context
    grounded_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=STEP_BACK_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Established qualitative context (treat as ground truth):\n{qualitative_context}\n\n"
                    "Answer the question. For any specific number you include, explicitly note your confidence level "
                    "and recommend a source for verification."
                ),
            }
        ],
    )
    return grounded_response.content[0].text


questions = [
    "What percentage of software bugs are caused by null pointer exceptions?",
    "How much faster is Rust than Python for CPU-bound tasks?",
    "What share of web traffic comes from mobile devices?",
]

for q in questions:
    print(f"Q: {q}")
    answer = answer_with_step_back(q)
    print(f"A: {answer[:400]}\n")
```

**Expected Token Savings:** Haiku step-back costs ~150 tokens; prevents fabricated statistics that trigger 1–3 correction turns costing 500–2,000 tokens each.
**Environment:** Python 3.9+; two-model step-back pattern; Haiku step-back costs $0.0004 vs. potential correction cost of $0.01+.

---

### Option 5: Numeric claim confidence scoring with Haiku

```python
import re
import anthropic

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()

NUMERIC_PATTERN = re.compile(
    r"(\b\d+\.?\d*\s*%|\$[\d.,]+\s*(?:billion|million|trillion|M|B)?|\b\d+[\d.,]*\s*(?:billion|million|trillion)\b)",
    re.I,
)


def extract_numeric_claims(text: str) -> list[str]:
    """Extract sentences containing numeric claims."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s for s in sentences if NUMERIC_PATTERN.search(s)]


def score_claim_confidence(claim: str) -> tuple[str, float]:
    """Use Haiku to assess confidence in a numeric claim. Returns (assessment, score 0-1)."""
    response = haiku.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        system=(
            "Rate the reliability of a numeric claim on a scale of 0–10. "
            "10 = well-established fact with authoritative sources. "
            "5 = plausible estimate but uncertain. "
            "0 = likely fabricated or unverifiable. "
            "Reply with: SCORE: N | REASON: brief explanation"
        ),
        messages=[{"role": "user", "content": f"Claim: {claim}"}],
    )
    text = response.content[0].text
    score_match = re.search(r"SCORE:\s*(\d+(?:\.\d+)?)", text)
    score = float(score_match.group(1)) / 10 if score_match else 0.5
    return text, score


def generate_and_score_response(question: str, confidence_threshold: float = 0.6) -> str:
    """Generate a response and flag low-confidence numeric claims."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    initial = response.content[0].text

    claims = extract_numeric_claims(initial)
    low_confidence_claims = []

    for claim in claims:
        assessment, score = score_claim_confidence(claim)
        print(f"[Score {score:.1f}] {claim[:80]}")
        if score < confidence_threshold:
            low_confidence_claims.append((claim, score, assessment))

    if not low_confidence_claims:
        return initial

    # Flag low-confidence claims in the response
    flagged_response = initial
    for claim, score, assessment in low_confidence_claims:
        note = f" ⚠️[Confidence {score:.0%} — verify this figure]"
        # Simple insertion after the claim sentence
        flagged_response = flagged_response.replace(
            claim,
            claim.rstrip(".") + note + ("." if claim.endswith(".") else ""),
            1,
        )

    return flagged_response


result = generate_and_score_response(
    "What are some key statistics about AI adoption in enterprise software?",
    confidence_threshold=0.6,
)
print(f"\nFlagged response:\n{result[:600]}")
```

**Expected Token Savings:** Haiku scoring costs ~50 tokens per claim; identifies hallucinated statistics before user sees them, avoiding correction cycles.
**Environment:** Python 3.9+; confidence threshold tunable; high-threshold mode for financial/medical contexts.

---

### Option 6: Constrain output to qualitative-only when no data source is available

```python
import anthropic

client = anthropic.Anthropic()

QUALITATIVE_SYSTEM = """You are a research analyst. You have strict output rules:

<rules>
1. You may ONLY cite a specific number if you can name the exact source (organization + year + report name).
2. If you cannot name the source, use ONLY qualitative language:
   - Instead of "42% of companies..." → "Many companies..." or "A significant portion of companies..."
   - Instead of "grew 35% year over year" → "grew substantially year over year"
   - Instead of "$500 billion market" → "a large and growing market"
3. When asked directly for a number you cannot verify: say "I don't have a verified figure for this. For current data, consult [specific authoritative source]."
4. You may describe TRENDS confidently without citing specific values.
</rules>

<qualitative_vocabulary>
Instead of percentages, use: most, many, a majority, a minority, a growing share, few, nearly all, roughly half
Instead of growth rates, use: rapidly, significantly, modestly, substantially, marginally
Instead of market sizes, use: large, significant, substantial, niche, dominant
</qualitative_vocabulary>"""


def constrained_research_response(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=QUALITATIVE_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


def hybrid_response(question: str, verified_data: dict | None = None) -> str:
    """
    Use verified data when available, qualitative language otherwise.
    verified_data: {topic: {value, source}} from authoritative APIs
    """
    context = ""
    if verified_data:
        lines = [f"- {k}: {v['value']} (Source: {v['source']})" for k, v in verified_data.items()]
        context = f"\n\n<verified_data>\n" + "\n".join(lines) + "\n</verified_data>\n\nYou may cite these verified figures with attribution."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=QUALITATIVE_SYSTEM + context,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


# Without verified data: qualitative only
print("=== Qualitative-only mode ===")
print(constrained_research_response("How widely adopted is Kubernetes in production?"))

# With some verified data: can cite specific numbers
print("\n=== Hybrid mode with verified data ===")
verified = {
    "Kubernetes production adoption": {
        "value": "96% of surveyed organizations",
        "source": "CNCF Annual Survey 2023",
    }
}
print(hybrid_response("How widely adopted is Kubernetes in production?", verified_data=verified))
```

**Expected Token Savings:** Qualitative-only mode eliminates all hallucinated statistics; hybrid mode cites only verified numbers, preventing user fact-checking cycles worth 500–2,000 tokens each.
**Environment:** Python 3.9+; vocabulary constraints are prompt-engineering only, zero overhead; hybrid mode requires integration with a statistics API or internal data warehouse.

---

| Option | Approach | Prevention Mechanism | Best For |
|--------|----------|---------------------|----------|
| 1 | Explicit no-numbers instruction | Prompt rule enforcement | General conversational agents |
| 2 | Mandatory statistics lookup tool | Tool-gated citation | Research/data agents |
| 3 | Post-generation audit + revision | Regex detection + rewrite | High-stakes content pipelines |
| 4 | Step-back qualitative grounding | Two-turn separation | Complex analytical questions |
| 5 | Haiku confidence scoring | Per-claim reliability rating | Mixed-certainty reports |
| 6 | Qualitative vocabulary constraints | Output mode restriction | Compliance/regulated contexts |
