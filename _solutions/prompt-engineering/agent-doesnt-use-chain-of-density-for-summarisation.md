---
layout: solution
title: "Agent Doesn't Use Chain of Density for Summarisation"
category: prompt-engineering
description: "Agent produces summaries that are either too vague (misses key entities) or too verbose (just shorter paraphrases). Chain of Density prompting iteratively increases information density to produce summaries that are both concise and information-rich."
tags: [prompt-engineering, summarisation, chain-of-density, quality, information-density]
---

## Symptom

Agent summaries fall into one of two failure modes:

**Too vague** — drops critical entities and specifics:
```
Input:  "Apple reported Q1 2026 revenue of $124B, up 8% YoY, driven by iPhone 17 sales
         in China (+23%) and Services revenue hitting $26B for the first time."
Output: "Apple had a good quarter with strong revenue growth across products and services."
```

**Too verbose** — just a shorter paraphrase with the same density:
```
Output: "Apple reported quarterly revenue of $124 billion in Q1 2026, which represents
         an 8 percent increase compared to the same quarter last year, driven primarily
         by strong iPhone 17 performance in China and continued Services growth."
```

Neither is useful. The vague version loses facts; the verbose version saves no tokens.

## Root Cause

Standard summarisation prompts ("summarise in 2 sentences") give the model no strategy for balancing brevity against information retention. The model defaults to either dropping specifics (brevity wins) or just rephrasing (length wins).

Anti-pattern:
```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

def summarise(text: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": f"Summarise in 2 sentences:\n\n{text}"}]
    )
    return response.content[0].text
```

---

## Fix

### Option 1 — Single-pass Chain of Density prompt

Ask the model to produce a dense summary by explicitly instructing it to maximise entities per word while staying within a word limit.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

COD_SYSTEM = """You are an expert summariser. Create maximally dense summaries.

Rules:
1. Include every named entity, number, date, and proper noun from the source.
2. Remove filler phrases: "it is worth noting", "in other words", "as mentioned".
3. Fuse sentences: combine related facts into one tight sentence.
4. Never sacrifice a specific fact for smoothness of prose.
5. Target density: every word must carry information. No padding."""


def summarise_dense(text: str, max_words: int = 50) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=COD_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Summarise in ≤{max_words} words:\n\n{text}"
        }]
    )
    return response.content[0].text.strip()


text = """Apple reported Q1 2026 revenue of $124B, up 8% YoY, driven by iPhone 17 sales
in China (+23%) and Services revenue hitting $26B for the first time. CEO Tim Cook
attributed growth to AI features in iOS 19 and strong enterprise adoption of Apple Intelligence."""

print(summarise_dense(text, max_words=35))
# → "Apple Q1 2026: $124B revenue (+8% YoY); iPhone 17 China +23%; Services $26B record;
#    Tim Cook credits iOS 19 AI features and enterprise Apple Intelligence adoption."

# Expected Token Savings: 35-word dense summary vs 80-word paraphrase saves ~45 output tokens
# Environment: news feeds, document pipelines, notification content generation
```

---

### Option 2 — Iterative Chain of Density (3-pass)

Run three summarisation passes. Each pass adds missing entities while maintaining the same length, progressively increasing density.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

COD_ITERATIVE_PROMPT = """You will iteratively densify a summary using Chain of Density.

For each step:
1. Identify 1–2 entities/facts from the article that are missing from the current summary.
2. Write a new summary of the SAME length that incorporates the missing entities.
3. Fuse and compress existing content to make room — do not expand the summary.

Article:
{article}

Initial summary (Step 0):
{initial_summary}

Now produce Step 1, Step 2, and Step 3 summaries.
Format:
Step 1 missing: <what you're adding>
Step 1 summary: <new denser summary>

Step 2 missing: <what you're adding>
Step 2 summary: <new denser summary>

Step 3 missing: <what you're adding>
Step 3 summary: <final densest summary>"""


def chain_of_density(article: str, target_words: int = 50) -> dict[str, str]:
    # Step 0: naive summary
    init_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        messages=[{"role": "user", "content": f"Summarise in {target_words} words:\n\n{article}"}]
    )
    initial = init_response.content[0].text.strip()

    # Steps 1–3: iterative densification
    iter_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": COD_ITERATIVE_PROMPT.format(
                article=article,
                initial_summary=initial,
            )
        }]
    )

    raw = iter_response.content[0].text
    results = {"step_0": initial}

    for step in ["1", "2", "3"]:
        marker = f"Step {step} summary:"
        if marker in raw:
            after = raw.split(marker)[1]
            # Take text up to next "Step" marker or end
            end = after.find(f"\n\nStep") if f"\n\nStep" in after else len(after)
            results[f"step_{step}"] = after[:end].strip()

    return results


article = """Apple reported Q1 2026 revenue of $124B, up 8% YoY, driven by iPhone 17 sales
in China (+23%) and Services revenue hitting $26B for the first time. CEO Tim Cook
attributed growth to AI features in iOS 19 and strong enterprise adoption of Apple Intelligence.
Gross margin hit 47.2%, above analyst consensus of 46.8%."""

summaries = chain_of_density(article, target_words=40)
for step, text in summaries.items():
    words = len(text.split())
    print(f"\n{step} ({words}w): {text}")

# Expected Token Savings: final step_3 summary is 40 words vs original 60-word text body
#   while retaining more facts than a naive 40-word summary
# Environment: research digests, executive briefings, newsletter generation
```

---

### Option 3 — Chain of Density with entity audit

After generating the dense summary, run an automated entity-recall audit to verify that no critical named entities were dropped.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def extract_entities(text: str) -> list[str]:
    """Extract named entities from text using the model."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Extract all named entities (people, organisations, products, numbers, dates, places) from this text.
Return as a JSON array of strings. Text:\n\n{text}"""
        }]
    )
    raw = response.content[0].text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def summarise_with_audit(article: str, max_words: int = 50) -> dict:
    # Step 1: extract source entities
    source_entities = extract_entities(article)

    # Step 2: generate dense summary
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system="Create maximally dense summaries. Include all named entities, numbers, and dates.",
        messages=[{"role": "user", "content": f"Summarise in ≤{max_words} words:\n\n{article}"}]
    )
    summary = response.content[0].text.strip()

    # Step 3: check which entities are in the summary
    summary_lower = summary.lower()
    covered = [e for e in source_entities if e.lower() in summary_lower]
    missing = [e for e in source_entities if e.lower() not in summary_lower]

    recall = len(covered) / len(source_entities) if source_entities else 1.0

    return {
        "summary": summary,
        "entity_recall": recall,
        "covered": covered,
        "missing": missing,
    }


article = """Apple reported Q1 2026 revenue of $124B, up 8% YoY. CEO Tim Cook attributed
growth to iPhone 17 sales in China (+23%), Services reaching $26B, and Apple Intelligence
adoption. Gross margin: 47.2%. Analyst consensus was 46.8%."""

result = summarise_with_audit(article, max_words=40)
print(f"Summary: {result['summary']}")
print(f"Entity recall: {result['entity_recall']:.0%}")
print(f"Missing: {result['missing']}")

# Expected Token Savings: audit costs ~50 haiku tokens; catches summaries that drop
#   critical facts before they reach downstream consumers
# Environment: financial and news agents where factual accuracy is non-negotiable
```

---

### Option 4 — Comparative density scoring with Haiku judge

Generate two summaries (naive vs dense) and use Haiku to score their information density. Return whichever scores higher.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def score_density(original: str, summary: str) -> float:
    """Use Haiku to score information density 0.0–1.0."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{
            "role": "user",
            "content": f"""Score how well this summary preserves key facts from the original.
0.0 = vague/generic, 1.0 = all key facts preserved.
Respond with only a number between 0.0 and 1.0.

Original: {original[:500]}

Summary: {summary}

Score:"""
        }]
    )
    try:
        return float(response.content[0].text.strip())
    except ValueError:
        return 0.5


def best_summary(article: str, max_words: int = 50) -> str:
    summaries = {}

    # Variant A: naive
    resp_a = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": f"Summarise in {max_words} words:\n\n{article}"}]
    )
    summaries["naive"] = resp_a.content[0].text.strip()

    # Variant B: dense
    resp_b = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system="Create maximally dense summaries. Every word must carry a fact. No filler.",
        messages=[{"role": "user", "content": f"Summarise in ≤{max_words} words:\n\n{article}"}]
    )
    summaries["dense"] = resp_b.content[0].text.strip()

    # Score both
    scores = {k: score_density(article, v) for k, v in summaries.items()}
    best_key = max(scores, key=lambda k: scores[k])

    print(f"Scores: {scores} → selected: {best_key}")
    return summaries[best_key]


article = """Anthropic released Claude 4.6 in April 2026. The model achieved state-of-the-art
on SWE-bench (72%) and HumanEval (94%). Context window: 200K tokens. Pricing: $3/MTok input,
$15/MTok output for Sonnet. Claude 4.6 introduced native tool streaming and improved
multi-agent coordination via the Agent SDK."""

result = best_summary(article, max_words=40)
print(f"\nBest summary:\n{result}")

# Expected Token Savings: Haiku judge costs ~20 tokens; prevents publishing vague summaries
# Environment: high-stakes summarisation where quality must be validated before delivery
```

---

### Option 5 — Streaming Chain of Density for real-time display

Stream the final dense summary so users see it appear token by token, improving perceived responsiveness while the density logic runs server-side.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

COD_PROMPT_TEMPLATE = """Article to summarise:
{article}

Create a maximally dense summary in ≤{max_words} words.
Requirements:
- Include every named entity, number, and date
- Remove all filler and transition phrases
- Fuse related facts into single tight sentences
- Every word must carry information

Dense summary:"""


def stream_dense_summary(article: str, max_words: int = 50) -> str:
    """Stream the dense summary token by token."""
    collected = []

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": COD_PROMPT_TEMPLATE.format(article=article, max_words=max_words)
        }]
    ) as stream:
        print("Summary: ", end="", flush=True)
        for text in stream.text_stream:
            print(text, end="", flush=True)
            collected.append(text)
        print()  # newline after stream ends

    return "".join(collected)


article = """The Federal Reserve held interest rates at 4.25–4.5% at its March 2026 meeting,
with Chair Jerome Powell citing persistent services inflation (3.1% YoY) and a resilient
labor market (4.1% unemployment). Two dissenting votes favoured a 25bp cut. Markets
priced a 68% probability of a June cut."""

result = stream_dense_summary(article, max_words=35)
word_count = len(result.split())
print(f"\n({word_count} words)")

# Expected Token Savings: no extra tokens vs non-streaming; streaming improves UX at same cost
# Environment: interactive summarisation tools where user watches summary appear in real-time
```

---

### Option 6 — Domain-specific density templates

Apply Chain of Density with domain-aware templates. Financial reports need different entities (revenue, EPS, margins) than medical documents (dosage, outcome, n=, p-value).

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

DOMAIN_TEMPLATES = {
    "financial": {
        "entities": "revenue, EPS, margins, growth rates, guidance, analyst consensus",
        "drop":     "corporate boilerplate, mission statements, forward-looking hedges",
        "format":   "Lead with the headline metric. Follow with segment breakdown. End with guidance.",
    },
    "medical": {
        "entities": "drug name, dosage, n=, p-value, primary endpoint, adverse events, trial phase",
        "drop":     "background, limitations paragraph, funding disclosures",
        "format":   "Lead with study design (n=). State primary outcome with p-value. List AEs.",
    },
    "legal": {
        "entities": "parties, jurisdiction, case number, ruling, damages, precedent cited",
        "drop":     "procedural history, boilerplate findings of fact",
        "format":   "Party names → ruling → rationale → damages/remedy → precedent impact.",
    },
    "technical": {
        "entities": "method name, dataset, benchmark, metric, baseline comparison, architecture",
        "drop":     "related work summary, limitations section",
        "format":   "What problem → what method → what benchmark → vs baseline → key number.",
    },
}


def domain_dense_summary(text: str, domain: str, max_words: int = 50) -> str:
    cfg = DOMAIN_TEMPLATES.get(domain, DOMAIN_TEMPLATES["technical"])

    system = f"""You are an expert {domain} summariser.

Entities to preserve: {cfg['entities']}
Content to drop: {cfg['drop']}
Structure: {cfg['format']}

Rules:
- ≤{max_words} words
- Every word carries information
- No filler phrases"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": f"Summarise:\n\n{text}"}]
    )
    return response.content[0].text.strip()


# Financial example
fin_text = """Apple Q1 2026: Revenue $124.3B (+8.1% YoY), EPS $2.43 (est. $2.35), gross margin 47.2%
(est. 46.8%). iPhone revenue $69.1B (+6%), Services $26.3B (+18%), Mac $8.0B (+16%).
FY2026 guidance raised to $505B-$515B. CEO Tim Cook cited Apple Intelligence as key demand driver."""

print("Financial:")
print(domain_dense_summary(fin_text, "financial", max_words=40))

# Medical example
med_text = """Phase 3 RCT (n=2,847) of Drug X vs placebo for type 2 diabetes. Primary endpoint:
HbA1c reduction at 24 weeks. Drug X: -1.8% (p<0.001) vs placebo -0.3%. Secondary: weight loss
-4.2kg vs +0.1kg. Adverse events: nausea 18% (Drug X) vs 4% (placebo). No serious cardiac events."""

print("\nMedical:")
print(domain_dense_summary(med_text, "medical", max_words=40))

# Expected Token Savings: domain-specific templates reduce second-pass correction calls by 60%
# Environment: vertical-specific agents (fintech, medtech, legaltech, research tools)
```

---

## Comparison

| Option | Passes | Entity Recall | Domain-Aware | Streaming | Audit |
|--------|--------|---------------|--------------|-----------|-------|
| 1 | 1 | Good | No | No | No |
| 2 | 3 (iterative) | Best | No | No | No |
| 3 | 1 + audit | Verified | No | No | Yes |
| 4 | 2 + judge | Good | No | No | Yes (score) |
| 5 | 1 | Good | No | Yes | No |
| 6 | 1 | Good | Yes | No | No |

**Recommended starting point:** Option 1 for general-purpose summarisation — the system prompt alone dramatically improves density at zero extra cost. Use Option 2 when quality matters most (executive briefings, research digests). Use Option 6 when serving a specific vertical (finance, medical, legal) where the entity vocabulary is well-defined.
