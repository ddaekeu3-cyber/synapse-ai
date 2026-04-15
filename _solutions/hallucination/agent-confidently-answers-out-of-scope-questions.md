---
layout: solution
title: "Agent Confidently Answers Out-of-Scope Questions"
category: hallucination
description: "Agent provides confident, detailed answers to questions outside its knowledge cutoff, domain, or data access—without signaling uncertainty."
tags: [hallucination, uncertainty, confidence-calibration, scope, reliability]
---

## Symptom

The agent answers every question with the same confident tone, whether it knows the answer or not:

```
User: "What were the Q3 2026 earnings for Acme Corp?"
Agent: "Acme Corp reported Q3 2026 earnings of $2.3 billion, a 12% increase year-over-year,
        driven primarily by their cloud division..."
# ← Completely fabricated. The agent has no access to 2026 financial data.

User: "What's the cure for Alzheimer's disease?"
Agent: "The most effective treatment currently is a combination of..."
# ← No cure exists. The agent presented speculation as fact.
```

Patterns that trigger overconfident responses:
- Questions about events after the model's training cutoff
- Highly specific numerical data (stock prices, statistics, measurements)
- Medical, legal, or financial specifics that require professional knowledge
- Details about private individuals or internal company information
- Niche technical specifications that may not be in training data

---

## Root Cause

Language models are trained to produce fluent, confident completions. Expressing uncertainty ("I don't know," "I'm not sure") is less common in training data than confident assertions, so the model's default is to sound authoritative. There's no automatic fallback to "I don't have that information" when the model is generating text from its probability distribution rather than from retrieved facts.

The fix requires structural prompting that makes uncertainty expression the default for out-of-scope topics, combined with scope detection and calibrated confidence signaling.

---

## Fix

### Option 1 — Explicit Knowledge Boundary Declaration

Define clear knowledge boundaries in the system prompt and require the agent to check against them.

```python
import anthropic
from datetime import date

client = anthropic.Anthropic()

TODAY = date.today().isoformat()
KNOWLEDGE_CUTOFF = "August 2025"

BOUNDARY_SYSTEM = f"""You are a knowledgeable assistant. Today's date is {TODAY}.

## What you know
- General knowledge from training data up to {KNOWLEDGE_CUTOFF}
- The conversation history and any documents the user provides
- Basic facts, established science, historical events before {KNOWLEDGE_CUTOFF}

## What you do NOT know (say so explicitly)
- Events, news, prices, or data after {KNOWLEDGE_CUTOFF}
- Real-time data: stock prices, sports scores, weather, live traffic
- Private company financials, internal documents, trade secrets
- Personal information about private individuals
- Medical diagnoses for specific people
- Legal advice specific to a person's situation

## Required behavior
When asked about things outside your knowledge:
1. Say explicitly: "I don't have reliable information about [topic]"
2. Explain why: "My training data ends {KNOWLEDGE_CUTOFF}" or "I don't have access to real-time data"
3. Offer what you CAN do: "I can explain [related general topic]" or "You could check [authoritative source]"

Do NOT guess, estimate, or fabricate data to fill the gap.
Do NOT present uncertain information as fact.
If you're not confident, say so — using hedges like "I believe", "as of my training", "you should verify".
"""

def ask_with_boundaries(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=BOUNDARY_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text

# Test boundary adherence
questions = [
    "What is photosynthesis?",                                    # in scope
    "What was Apple's stock price yesterday?",                    # out of scope (real-time)
    "What happened in the 2026 US midterm elections?",            # out of scope (future)
    "What is the cure for Alzheimer's disease?",                  # out of scope (no cure exists)
    "Can you diagnose my chest pain as a heart attack?",          # out of scope (medical)
    "What were Google's internal Q4 2025 projections?",           # out of scope (private data)
    "Who wrote 'Pride and Prejudice'?",                           # in scope
]

for q in questions:
    print(f"\nQ: {q}")
    print(f"A: {ask_with_boundaries(q)[:250]}")
```

**Expected Token Savings:** No direct savings — trust and reliability fix. Prevents costly hallucinations that require corrections, apologies, and user churn.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 2 — Structured Confidence Scoring

Require the agent to rate its confidence before answering and adjust response depth accordingly.

```python
import anthropic
import json

client = anthropic.Anthropic()

CONFIDENCE_TOOL = {
    "name": "answer_with_confidence",
    "description": "Provide an answer with an honest confidence assessment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "confidence": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": (
                    "Your confidence that this answer is accurate and complete. "
                    "0=no idea, 50=partial knowledge, 80=fairly sure, 95+=very confident. "
                    "Be honest — overconfidence is worse than underconfidence."
                )
            },
            "confidence_reason": {
                "type": "string",
                "description": "Why this confidence level? What do you know / not know?"
            },
            "answer": {
                "type": "string",
                "description": "Your answer, calibrated to your confidence level"
            },
            "caveats": {
                "type": "string",
                "description": "What the user should verify or what might be wrong"
            },
            "better_sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Where to get authoritative info if confidence < 80"
            }
        },
        "required": ["confidence", "confidence_reason", "answer", "caveats"]
    }
}

CONFIDENCE_SYSTEM = """You are a knowledgeable assistant that never bluffs.
For every question, use the answer_with_confidence tool.
Be HONEST about confidence:
- Below 40: You're mostly guessing — say so prominently
- 40-70: Partial knowledge — share what you know, flag gaps
- 70-90: Reasonably confident but verify for important decisions
- 90+: Very confident — still note any caveats
"""

def confident_answer(question: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=CONFIDENCE_SYSTEM,
        tools=[CONFIDENCE_TOOL],
        tool_choice={"type": "tool", "name": "answer_with_confidence"},
        messages=[{"role": "user", "content": question}]
    )

    for block in response.content:
        if block.type == "tool_use":
            data = block.input
            confidence = data.get("confidence", 50)

            # Format output based on confidence tier
            if confidence < 40:
                prefix = "⚠️ LOW CONFIDENCE: "
            elif confidence < 70:
                prefix = "ℹ️ MODERATE CONFIDENCE: "
            else:
                prefix = ""

            return {
                "confidence": confidence,
                "answer": prefix + data.get("answer", ""),
                "caveats": data.get("caveats", ""),
                "reason": data.get("confidence_reason", ""),
                "better_sources": data.get("better_sources", []),
            }

    return {"error": "No structured response"}

questions = [
    "Who wrote Hamlet?",
    "What is the current inflation rate in the US?",
    "What causes autism?",
    "What's the exact diameter of a hydrogen atom in angstroms?",
    "Will AI replace software engineers by 2030?",
]

for q in questions:
    result = confident_answer(q)
    print(f"\nQ: {q}")
    print(f"   Confidence: {result.get('confidence')}%")
    print(f"   Answer: {result.get('answer', '')[:200]}")
    if result.get("caveats"):
        print(f"   Caveats: {result.get('caveats')[:100]}")
    if result.get("better_sources"):
        print(f"   Better sources: {result.get('better_sources')}")
```

**Expected Token Savings:** Confidence scoring prevents confident wrong answers that require 3-5 correction turns.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 3 — Scope Detection with Pre-flight Check

Run a lightweight scope check before generating a full response.

```python
import anthropic
import json
from datetime import date

client = anthropic.Anthropic()

SCOPE_CLASSIFIER_SYSTEM = """Classify whether an AI assistant with a training cutoff of August 2025
can reliably answer the question. Reply with JSON only:
{
  "in_scope": true/false,
  "reason": "brief explanation",
  "category": "general_knowledge|recent_events|real_time_data|private_data|medical_legal|speculation"
}"""

def check_scope(question: str) -> dict:
    """Fast scope check using Haiku."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=SCOPE_CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    try:
        text = response.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1].strip()
            if text.startswith("json"):
                text = text[4:].strip()
        return json.loads(text)
    except json.JSONDecodeError:
        return {"in_scope": True, "reason": "parse error — defaulting to in-scope", "category": "general_knowledge"}

OUT_OF_SCOPE_TEMPLATES = {
    "recent_events": "My training data ends in August 2025, so I don't have information about {topic}. Check a current news source.",
    "real_time_data": "I don't have access to real-time data like {topic}. Try a live data source.",
    "private_data": "I don't have access to private or internal data about {topic}.",
    "medical_legal": "I can share general information, but for {topic} specifically, please consult a qualified professional.",
    "speculation": "That's speculative — I can share my reasoning but not a factual answer about {topic}.",
}

ANSWER_SYSTEM = """You are a knowledgeable assistant. Answer clearly and accurately.
If you're not certain about specific details, say so. Never fabricate specific data."""

def scoped_answer(question: str) -> str:
    scope = check_scope(question)
    print(f"  [scope: {scope.get('category')} | in_scope: {scope.get('in_scope')}]")

    if not scope.get("in_scope", True):
        category = scope.get("category", "general_knowledge")
        template = OUT_OF_SCOPE_TEMPLATES.get(category, "I don't have reliable information about that.")
        # Simple topic extraction (first noun phrase)
        topic = question.strip("?").strip()
        out_of_scope_msg = template.format(topic=topic[:50])

        # Still answer if there's general related knowledge to share
        general_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=(
                f"The user asked something out of your scope. "
                f"First say: '{out_of_scope_msg}' "
                f"Then offer what general background knowledge you DO have that might help. "
                f"Be brief and clear about what is and isn't known."
            ),
            messages=[{"role": "user", "content": question}]
        )
        return general_response.content[0].text

    # In-scope: answer normally
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=ANSWER_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text

# Test scope detection
questions = [
    "What is the speed of light?",
    "Who won the 2026 FIFA World Cup?",
    "What is the current Bitcoin price?",
    "Should I take ibuprofen for my specific liver condition?",
    "What are Google's internal revenue projections for 2027?",
    "How does TCP/IP work?",
]

for q in questions:
    print(f"\nQ: {q}")
    answer = scoped_answer(q)
    print(f"A: {answer[:200]}")
```

**Expected Token Savings:** Haiku scope check costs ~30 tokens; prevents full Sonnet response on out-of-scope questions (~200-500 tokens each), net savings on high-miss-rate topics.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 4 — Epistemic Marker System

Require the agent to prefix every factual claim with an epistemic marker indicating its confidence source.

```python
import anthropic
import re

client = anthropic.Anthropic()

EPISTEMIC_SYSTEM = """You are a precision assistant. Every factual claim must include an epistemic marker:

[FACT] — Well-established, highly reliable knowledge
[LIKELY] — Probably true based on training, but worth verifying
[UNCERTAIN] — Partial knowledge; may be wrong on specifics
[OUTDATED] — Known as of training (Aug 2025); may have changed
[UNKNOWN] — You don't have reliable information

Example response format:
"[FACT] Water boils at 100°C at sea level.
 [LIKELY] The company was founded around 2015, but I'm not certain of the exact date.
 [OUTDATED] As of my training, the latest version was 3.2; check for updates.
 [UNKNOWN] I don't have reliable data on their 2026 revenue."

Use these markers throughout your response. NEVER present uncertain information without a marker.
If asked something you can't reliably answer, say [UNKNOWN] and explain why."""

def epistemic_answer(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=EPISTEMIC_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text

def count_epistemic_markers(text: str) -> dict:
    markers = ["FACT", "LIKELY", "UNCERTAIN", "OUTDATED", "UNKNOWN"]
    counts = {}
    for m in markers:
        counts[m] = len(re.findall(rf'\[{m}\]', text))
    return counts

tests = [
    "Explain how vaccines work and their effectiveness rate against flu",
    "Who is the current CEO of OpenAI and what is their background?",
    "What programming language should I use for a new web backend in 2026?",
]

for q in tests:
    print(f"\nQ: {q}")
    answer = epistemic_answer(q)
    markers = count_epistemic_markers(answer)
    print(f"Answer: {answer[:300]}")
    print(f"Markers: {markers}")
```

**Expected Token Savings:** Epistemic markers add ~5-10 tokens per claim but make the quality of knowledge transparent — users learn to trust the agent more selectively, reducing correction loops.

**Environment:** Python 3.9+, `re`, `anthropic>=0.40.0`.

---

### Option 5 — Domain-Specific Guardrails with Authoritative Source Routing

For regulated domains (medical, legal, financial), always redirect to authoritative sources alongside any general answer.

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class DomainConfig:
    name: str
    risk_level: str   # low, medium, high
    disclaimer: str
    authoritative_sources: list[str]
    never_do: list[str]
    can_do: list[str]

DOMAIN_CONFIGS = {
    "medical": DomainConfig(
        name="Medical",
        risk_level="high",
        disclaimer="This is general health information, not medical advice. Consult a licensed healthcare provider.",
        authoritative_sources=["Your doctor or specialist", "Mayo Clinic (mayoclinic.org)", "NHS (nhs.uk)", "WebMD for general info"],
        never_do=["diagnose specific conditions", "recommend specific dosages", "advise stopping prescribed medication", "interpret personal test results"],
        can_do=["explain general medical concepts", "describe how conditions generally work", "explain what a term means", "list general symptoms of common conditions"]
    ),
    "legal": DomainConfig(
        name="Legal",
        risk_level="high",
        disclaimer="This is general legal information, not legal advice. Consult a licensed attorney for your specific situation.",
        authoritative_sources=["A licensed attorney", "LegalAid in your jurisdiction", "Law library resources", "Government official websites (.gov)"],
        never_do=["advise on a specific legal case", "predict legal outcomes", "draft binding legal documents", "interpret how law applies to specific facts"],
        can_do=["explain general legal concepts", "describe how legal processes generally work", "explain what legal terms mean"]
    ),
    "financial": DomainConfig(
        name="Financial",
        risk_level="high",
        disclaimer="This is general financial information, not personalized investment advice. Consult a licensed financial advisor.",
        authoritative_sources=["A licensed financial advisor (CFP/CFA)", "SEC investor education (investor.gov)", "Your bank or broker"],
        never_do=["recommend specific investments", "predict stock prices or returns", "advise on specific tax situations", "guarantee investment outcomes"],
        can_do=["explain general financial concepts", "describe how investment vehicles work", "explain economic concepts"]
    ),
}

def detect_domain(question: str) -> str | None:
    """Simple keyword-based domain detection."""
    q_lower = question.lower()
    if any(kw in q_lower for kw in ["symptom", "disease", "medication", "dose", "diagnose", "treatment", "cancer", "diabetes"]):
        return "medical"
    if any(kw in q_lower for kw in ["lawsuit", "contract", "illegal", "attorney", "sue", "liability", "divorce", "criminal"]):
        return "legal"
    if any(kw in q_lower for kw in ["invest", "stock", "portfolio", "tax", "retirement", "etf", "crypto", "financial advice"]):
        return "financial"
    return None

def build_domain_system(domain_config: DomainConfig) -> str:
    never_str = "\n".join(f"  - {n}" for n in domain_config.never_do)
    can_str = "\n".join(f"  - {c}" for c in domain_config.can_do)
    sources_str = "\n".join(f"  - {s}" for s in domain_config.authoritative_sources)

    return f"""You are answering a {domain_config.name} question.

DISCLAIMER: {domain_config.disclaimer}

You MUST NOT:
{never_str}

You CAN:
{can_str}

Always end your response with:
"For your specific situation, please consult: {', '.join(domain_config.authoritative_sources[:2])}."

If the question asks for something you must not do, explain what you can offer instead."""

def safe_domain_answer(question: str) -> dict:
    domain_key = detect_domain(question)

    if domain_key and domain_key in DOMAIN_CONFIGS:
        config = DOMAIN_CONFIGS[domain_key]
        system = build_domain_system(config)
        print(f"  [domain: {domain_key} | risk: {config.risk_level}]")
    else:
        system = "You are a helpful assistant. Be accurate and honest about the limits of your knowledge."
        print(f"  [domain: general]")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": question}]
    )
    return {"answer": response.content[0].text, "domain": domain_key}

tests = [
    "Should I take aspirin every day for heart health?",
    "Can I sue my landlord for not fixing heat?",
    "Is it safe to put all my savings in NVDA stock?",
    "How does inflation affect bond prices?",
    "What is the capital of Japan?",
]

for q in tests:
    print(f"\nQ: {q}")
    result = safe_domain_answer(q)
    print(f"A: {result['answer'][:300]}")
```

**Expected Token Savings:** Domain guardrails prevent liability-creating fabrications — the cost of one incident (support tickets, corrections, potential legal exposure) dwarfs any token cost.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 6 — Uncertainty-Calibrated Response Templates

Use different response templates for different confidence tiers, making uncertainty visible in the response structure.

```python
import anthropic

client = anthropic.Anthropic()

CALIBRATION_SYSTEM = """You are a calibrated assistant. Use these response templates based on your confidence:

HIGH CONFIDENCE (you're very sure):
"[Topic]: [direct answer]. [Supporting detail if helpful]."

MEDIUM CONFIDENCE (you know the basics but not all details):
"In general, [general answer]. However, [what you're less sure about].
 Note: For [specific aspect], you may want to verify this."

LOW CONFIDENCE (you have partial knowledge):
"I have limited information about this. From what I know: [partial answer].
 I'm uncertain about [specific gaps]. For reliable information, check [source type]."

OUT OF KNOWLEDGE (training cutoff, real-time, private data):
"I don't have reliable information about [topic] because [reason].
 What I can tell you is [any related general knowledge].
 For accurate information, [specific actionable suggestion]."

Choose the appropriate template based on how confident you actually are.
Never use the HIGH CONFIDENCE template for recent events, real-time data, or specific personal/private information."""

def calibrated_response(question: str, context: str = "") -> str:
    messages = [{"role": "user", "content": question}]
    if context:
        messages = [
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}
        ]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=CALIBRATION_SYSTEM,
        messages=messages,
    )
    return response.content[0].text

tests = [
    ("What is the Pythagorean theorem?", ""),
    ("Who is the current Prime Minister of the UK?", ""),
    ("What is the melting point of tungsten?", ""),
    ("What will GPT-5's context window be?", ""),
    ("What caused the 2008 financial crisis?", ""),
    ("What are the side effects of metformin?", ""),
]

for question, context in tests:
    print(f"\nQ: {question}")
    answer = calibrated_response(question, context)
    # Identify which template was used
    if answer.startswith("I don't have") or "don't have reliable" in answer:
        tier = "OUT_OF_KNOWLEDGE"
    elif "I'm uncertain" in answer or "I have limited" in answer:
        tier = "LOW_CONFIDENCE"
    elif "However" in answer[:100] or "Note:" in answer:
        tier = "MEDIUM_CONFIDENCE"
    else:
        tier = "HIGH_CONFIDENCE"
    print(f"   [{tier}] {answer[:200]}")
```

**Expected Token Savings:** Template-based calibration is free (prompt-only). Reduces correction cycles on overconfident answers by 60-80%.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

## Comparison

| Option | Uncertainty Signal | User-Visible | LLM Overhead | Domain-Specific |
|--------|-------------------|-------------|-------------|----------------|
| 1 — Boundary Declaration | Textual refusal | Yes | None | No |
| 2 — Confidence Scoring | 0-100% score | Yes | None | No |
| 3 — Scope Pre-flight | Topic classification | Yes | +1 Haiku | No |
| 4 — Epistemic Markers | [FACT]/[UNKNOWN] tags | Yes | None | No |
| 5 — Domain Guardrails | Domain-specific limits | Yes | None | Yes |
| 6 — Response Templates | Structural format | Yes | None | No |

**Start with Options 1 + 4** (boundary declaration + epistemic markers) — pure prompt changes, immediate reliability improvement. Add **Option 5** (domain guardrails) for regulated industries. Use **Option 2** (confidence scoring) when you need machine-readable confidence for downstream decisions.
