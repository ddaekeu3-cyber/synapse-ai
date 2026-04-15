---
layout: solution
title: "Agent Fabricates Statistics and Numbers"
category: hallucination
description: "Agent confidently cites specific percentages, counts, prices, and research findings that are plausible but invented, leading users to make decisions based on false data."
tags: [hallucination, statistics, numbers, grounding, retrieval, reliability]
---

## Symptom

The agent states "studies show that 73% of users prefer..." — a number that sounds precise and credible but was never in any study. It quotes a market size of "$4.2 billion" that is off by an order of magnitude, or cites a "2023 report by McKinsey" that does not exist. The problem is worst for: market size figures, scientific statistics, prices, historical counts, and research citations. The model generates plausible-looking numbers because numeric tokens are predicted the same way as all other tokens — by likelihood, not by lookup.

## Root Cause

LLMs do not store facts in a database. Numeric facts are encoded in weights with low precision — the model knows that a number is "around that magnitude" but will generate a specific, confident-sounding value even when the exact figure was never in training data or was updated since the cutoff. High-temperature sampling makes this worse. The model has no built-in signal that distinguishes "I know this precisely" from "I am guessing a plausible number."

## Fix

### Option 1 — Explicit uncertainty instruction: forbid invented statistics

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a factual research assistant with strict honesty rules about numbers and statistics.

Rules for numeric claims:
1. If you are not certain of an exact figure, say "approximately" or give a range.
2. Never cite a specific percentage, dollar amount, or count unless you are certain it is accurate.
3. If a statistic requires a source, say "according to [source]" and only cite sources you are confident exist.
4. When you are uncertain, say so explicitly: "I don't have reliable data on the exact figure."
5. Never invent research papers, report names, or study citations.

It is better to say "I don't know the exact number" than to state a made-up statistic with confidence."""

def ask(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

# Questions that typically trigger fabricated statistics
questions = [
    "What percentage of startups fail in the first year?",
    "What is the global market size for AI in 2024?",
    "How many lines of code are in the Linux kernel?",
    "What is the average salary for a senior software engineer in San Francisco?",
    "What percentage of emails are spam?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask(q)[:250]}\n")
```

**Expected Token Savings:** Explicit uncertainty instruction prevents fabricated statistics; eliminates correction turns when users fact-check and find wrong numbers (typically 3-5 turns).
**Environment:** Research assistants, market analysis agents, or any agent that handles quantitative questions; this instruction is the mandatory baseline.

---

### Option 2 — Numeric claim detector: flag and audit statistics before returning

```python
import re
import anthropic

client = anthropic.Anthropic()

# Patterns that suggest a numeric claim that could be fabricated
NUMERIC_CLAIM_PATTERNS = [
    re.compile(r"\d+\.?\d*\s*%",                        re.IGNORECASE),  # percentages
    re.compile(r"\$\s*\d[\d,.]*\s*(billion|million|trillion|thousand)?", re.IGNORECASE),  # dollar amounts
    re.compile(r"\d+\.?\d*\s*(billion|million|trillion)\s*(people|users|dollars|records)", re.IGNORECASE),
    re.compile(r"(study|research|report|survey)\s+shows?\s+that", re.IGNORECASE),
    re.compile(r"according to\s+(?:a|the|an)?\s*(?:recent|new|2\d{3})", re.IGNORECASE),
    re.compile(r"\d{4}\s+(?:study|report|survey|research|analysis)", re.IGNORECASE),
]

VERIFIER_SYSTEM = """You are a fact-checker. Review this text for numeric claims (percentages, dollar amounts, counts, statistics).
For each numeric claim, assess: is this likely to be accurate, or could it be a hallucinated/approximate figure?

Return JSON:
{
  "claims": [
    {
      "claim": "the exact quoted statistic",
      "risk": "high|medium|low",
      "reason": "why it might be wrong"
    }
  ],
  "overall_risk": "high|medium|low"
}"""

def detect_numeric_claims(text: str) -> list[str]:
    claims = []
    for pattern in NUMERIC_CLAIM_PATTERNS:
        matches = pattern.findall(text)
        claims.extend(matches)
    return list(set(claims))

def audit_response(text: str) -> dict:
    import json
    detected = detect_numeric_claims(text)
    if not detected:
        return {"claims": [], "overall_risk": "low"}

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=VERIFIER_SYSTEM,
        messages=[{"role": "user", "content": f"Text to audit:\n{text}"}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"claims": [{"claim": c, "risk": "medium", "reason": "could not verify"} for c in detected], "overall_risk": "medium"}

def safe_ask(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    text = response.content[0].text

    audit = audit_response(text)
    if audit["overall_risk"] == "high":
        high_risk = [c for c in audit["claims"] if c.get("risk") == "high"]
        print(f"[audit] HIGH RISK numeric claims detected:")
        for c in high_risk:
            print(f"  ⚠ {c['claim']!r} — {c['reason']}")
        # Append a disclaimer
        text += "\n\n⚠ Note: Some specific figures in this response may be approximate or unverified. Please verify exact numbers from primary sources."

    return text

questions = [
    "What is the market share of iOS vs Android globally?",
    "How many active Python developers are there worldwide?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {safe_ask(q)[:300]}\n")
```

**Expected Token Savings:** Audit pass adds ~100 tokens but catches fabricated statistics before they reach users; each caught fabrication prevents a trust-destroying correction sequence.
**Environment:** High-stakes agents (market research, investment analysis, scientific writing) where numeric accuracy is critical.

---

### Option 3 — RAG grounding: only cite statistics from retrieved documents

```python
import anthropic

client = anthropic.Anthropic()

# In production, this would be a vector database search
# Simulated knowledge base with sourced statistics
KNOWLEDGE_BASE = [
    {
        "source": "Statista, 2024",
        "content": "As of 2024, Android holds approximately 72% of the global smartphone market share, while iOS holds around 27%.",
    },
    {
        "source": "Stack Overflow Developer Survey 2023",
        "content": "Python is the most-used programming language for the fourth year in a row, used by 49.3% of respondents.",
    },
    {
        "source": "Cybersecurity Ventures, 2023",
        "content": "Cybercrime is expected to cost the world $8 trillion annually in 2023.",
    },
    {
        "source": "IDC Worldwide Quarterly Cloud IT Infrastructure Tracker, Q1 2024",
        "content": "Global cloud infrastructure spending reached $79.4 billion in Q1 2024.",
    },
]

def retrieve(query: str, top_k: int = 2) -> list[dict]:
    """Keyword-based retrieval — replace with embedding search in production."""
    query_words = set(query.lower().split())
    scored = []
    for doc in KNOWLEDGE_BASE:
        doc_words = set(doc["content"].lower().split())
        score = len(query_words & doc_words)
        scored.append((score, doc))
    scored.sort(key=lambda x: -x[0])
    return [doc for _, doc in scored[:top_k] if _ > 0]

def grounded_ask(question: str) -> str:
    docs = retrieve(question)

    if docs:
        context = "\n\n".join(
            f"[Source: {d['source']}]\n{d['content']}"
            for d in docs
        )
        system = f"""You are a factual assistant. Answer using ONLY the provided sources.
If the sources do not contain the answer, say "I don't have sourced data on that."
Never invent statistics. Always cite the source when quoting a number.

Available sources:
{context}"""
    else:
        system = """You are a factual assistant. You have no sourced data available for this question.
Say "I don't have sourced data on this topic" and explain what kind of source the user should consult."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

questions = [
    "What is Android's market share?",
    "How popular is Python?",
    "What is the average developer salary in Japan?",   # not in KB
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {grounded_ask(q)[:250]}\n")
```

**Expected Token Savings:** Grounding eliminates all fabricated statistics by restricting the model to sourced documents; trust in numeric claims is structurally guaranteed rather than prompt-instructed.
**Environment:** Research agents, market intelligence tools, and any agent queried for quantitative data.

---

### Option 4 — Structured output with explicit source fields

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a research assistant that only cites verifiable statistics.

For every numeric claim, respond in JSON:
{
  "answer": "prose answer using only verifiable numbers",
  "statistics": [
    {
      "figure": "the exact number or percentage",
      "context": "what it measures",
      "source": "name of source if known, or null",
      "year": "year of data if known, or null",
      "confidence": "high (I know this well) | medium (approximate) | low (uncertain)"
    }
  ],
  "disclaimer": "any important caveats about data freshness or uncertainty"
}

If you cannot identify a reliable source for a statistic, set source to null and confidence to "low"."""

def ask_structured(question: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=384,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"answer": raw, "statistics": [], "disclaimer": "Parse error"}

def format_result(result: dict) -> str:
    lines = [result.get("answer", "")]
    stats = result.get("statistics", [])
    if stats:
        lines.append("\nStatistics cited:")
        for s in stats:
            source = s.get("source") or "source unknown"
            year   = f", {s['year']}" if s.get("year") else ""
            conf   = s.get("confidence", "unknown")
            lines.append(f"  • {s['figure']} — {s['context']} [{source}{year}] ({conf} confidence)")
    if result.get("disclaimer"):
        lines.append(f"\nNote: {result['disclaimer']}")
    return "\n".join(lines)

questions = [
    "What percentage of the world population uses social media?",
    "What is the global e-commerce market size?",
    "How many programming languages exist?",
]
for q in questions:
    result = ask_structured(q)
    print(f"Q: {q}")
    print(format_result(result)[:400])
    print()
```

**Expected Token Savings:** Structured output with mandatory source fields forces the model to acknowledge uncertainty for each statistic; low-confidence claims are flagged before reaching the user.
**Environment:** Market analysis, academic writing assistants, and compliance-sensitive agents where each statistic must be attributable.

---

### Option 5 — Two-pass verification: generate then cross-check statistics

```python
import re
import anthropic

client = anthropic.Anthropic()

NUMBER_PATTERN = re.compile(
    r"(?:\d+\.?\d*\s*%"
    r"|\$\s*\d[\d,.]*(?:\s*(?:billion|million|trillion))?"
    r"|\d+\.?\d*\s*(?:billion|million|trillion)\s+\w+"
    r")",
    re.IGNORECASE,
)

CROSS_CHECK_SYSTEM = """You are a statistics fact-checker.
Given a claim containing a specific number, assess whether it is:
- ACCURATE: You are confident this figure is correct.
- APPROXIMATE: The figure is in the right ballpark but may not be exact.
- UNCERTAIN: You cannot verify this; the figure could be wrong.
- FABRICATED: This figure seems invented or implausible.

Return JSON: {"verdict": "accurate|approximate|uncertain|fabricated", "note": "brief explanation"}"""

def cross_check_claim(claim: str) -> dict:
    import json
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=96,
        system=CROSS_CHECK_SYSTEM,
        messages=[{"role": "user", "content": f"Claim: {claim}"}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"verdict": "uncertain", "note": "could not verify"}

def ask_verified(question: str) -> str:
    # Pass 1: generate answer
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    text = response.content[0].text

    # Pass 2: extract and cross-check every numeric claim
    matches = NUMBER_PATTERN.findall(text)
    problems = []
    for match in set(matches):
        # Find context around the match
        idx = text.find(match)
        context = text[max(0, idx-40):idx+len(match)+40]
        result = cross_check_claim(context)
        verdict = result.get("verdict", "uncertain")
        print(f"  [check] {match!r} → {verdict}: {result.get('note', '')[:60]}")
        if verdict in {"fabricated", "uncertain"}:
            problems.append(match)

    if problems:
        # Regenerate with explicit caution instruction
        response2 = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system="When you are not certain of an exact figure, say 'approximately' or give a range. Never invent specific statistics.",
            messages=[{"role": "user", "content": question}],
        )
        return response2.content[0].text

    return text

questions = [
    "How many websites are there on the internet?",
    "What fraction of global CO2 emissions come from aviation?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask_verified(q)[:250]}\n")
```

**Expected Token Savings:** Two-pass verification costs ~N × 60 tokens per numeric claim but prevents trust-destroying fabrications; net savings when correction rate exceeds 1 in (verification_cost / correction_cost).
**Environment:** High-stakes factual agents; verification pass runs asynchronously and adds minimal latency when parallelised.

---

### Option 6 — Safe numeric response templates

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a factual assistant that handles numeric questions carefully.

For each numeric question, choose the appropriate response template:

TEMPLATE A — Known figure with source:
"According to [source], [figure] [context]. This figure is from [year]."

TEMPLATE B — Known approximate range:
"Estimates vary, but [figure] typically ranges from [low] to [high], depending on [factors]."

TEMPLATE C — Order-of-magnitude known:
"The exact figure varies, but it is in the range of [rough order of magnitude]."

TEMPLATE D — Unknown:
"I don't have reliable sourced data on the exact figure. For accurate statistics, I'd recommend checking [type of authoritative source]."

Use the most conservative template that is accurate. Never use Template A unless you are certain of both the figure and the source."""

def ask(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

questions = [
    "How many stars are in the Milky Way?",
    "What is the poverty rate in the United States?",
    "How many active volcanoes are on Earth?",
    "What is the exact number of neurons in the human brain?",
    "What percentage of the ocean has been explored?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask(q)[:250]}\n")
```

**Expected Token Savings:** Response templates constrain the model to acknowledge uncertainty in a structured way; prevents the most common failure mode (precise-sounding invented figures) at zero extra API calls.
**Environment:** Science communication agents, educational tools, and any agent where numeric precision matters; templates establish a clear contract between agent and user about certainty levels.

---

## Comparison

| Option | Prevention vs Detection | Extra API Calls | Source Attribution | Best For |
|---|---|---|---|---|
| 1. Explicit uncertainty instruction | Prevention (prompt) | None | No | Baseline — always use this |
| 2. Numeric claim detector | Detection | 1 audit call | No | High-stakes numeric Q&A |
| 3. RAG grounding | Prevention (structural) | None (retrieval) | Yes | Agents with a verifiable knowledge base |
| 4. Structured output with source fields | Prevention + transparency | None | Yes | Research and compliance agents |
| 5. Two-pass verification | Detection + correction | N claim checks | No | Critical numeric accuracy requirements |
| 6. Response templates | Prevention (prompt) | None | Partial | Science, education, general factual agents |
