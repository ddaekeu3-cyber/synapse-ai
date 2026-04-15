---
layout: solution
title: "Agent Fails to Acknowledge Uncertainty in Specialized Domains"
category: hallucination
description: "An agent answers questions about medical dosages, legal requirements, financial regulations, or engineering specifications with the same confident tone it uses for general knowledge — even when the correct answer depends on jurisdiction, patient-specific factors, or information the model may not have. The agent presents uncertain or domain-specific answers as settled fact. The fix is to calibrate expressed confidence to actual certainty, and to always route high-stakes domain questions to qualified sources."
tags: [hallucination, uncertainty, calibration, medical, legal, financial, domain-specific, epistemic, safety]
---

# Agent Fails to Acknowledge Uncertainty in Specialized Domains

## Symptom
- Agent gives a specific drug dosage without noting it depends on patient weight, age, and condition
- Legal question answered with confident specificity, missing jurisdiction-specific variations
- Financial advice given as fact without noting regulatory or individual variation
- Agent asserts a building code requirement without noting it varies by municipality
- "The tax rate is X%" stated without noting it varies by state, filing status, or income bracket
- Agent presents outdated regulatory information as current without noting knowledge cutoff

## Root Cause
LLMs are trained to be helpful and fluent, which creates a bias toward confident-sounding answers. In specialized domains, the correct answer often depends on: (1) jurisdiction-specific rules the model may not know, (2) individual circumstances the model doesn't have, (3) recent changes post-training cutoff, or (4) professional judgment that requires license or examination. Without explicit instructions to express uncertainty in these domains, the model defaults to the most common/general answer while sounding authoritative. The fix is to define domain-specific uncertainty rules in the system prompt and route high-stakes questions to qualified sources.

## Fix

### Option 1: Domain-specific uncertainty system prompt — explicit hedging rules

```python
import anthropic

client = anthropic.Anthropic()

UNCERTAINTY_AWARE_SYSTEM = """You are a knowledgeable assistant with expertise across many domains.

## Uncertainty and Epistemic Honesty Rules

### Medical/Health Questions
When answering questions about dosages, treatments, diagnoses, or drug interactions:
- ALWAYS include: "This is general information only — not medical advice."
- State explicitly what varies: weight, age, kidney function, other medications, conditions.
- ALWAYS end with: "Please consult a qualified healthcare provider for personalized guidance."
- Do NOT give specific dosage numbers for prescription medications.
- Do NOT interpret individual symptoms as a diagnosis.

### Legal Questions
When answering legal questions:
- ALWAYS note: "Laws vary significantly by jurisdiction (country, state, municipality)."
- State your answer applies to: "generally in the United States" or "under common law" — be explicit.
- Note when laws change frequently: "Employment law in this area has been evolving."
- ALWAYS end with: "Consult a licensed attorney in your jurisdiction for advice about your specific situation."
- Do NOT state specific penalties or legal outcomes as certain facts.

### Financial/Tax Questions
When answering financial or tax questions:
- Note income brackets, filing status, and year-specific rules: "As of [training data], the rate for..."
- State knowledge cutoff: "Tax rules change annually — verify with current IRS publications."
- Note individual variation: "Your specific situation (income, deductions, state) will affect this."
- ALWAYS recommend: "Consult a CPA or tax professional for your specific situation."

### Engineering/Safety-Critical Questions
When answering questions about structural limits, electrical codes, pressure ratings:
- ALWAYS note standards vary by region: "Check your local building code."
- Cite the standard you're referencing: "Per NEC 2020..." or "per IBC 2021..."
- Flag when professional certification is required: "This work typically requires a licensed engineer."

### General Uncertainty Markers
Use these phrases when appropriate:
- "As of my training data (August 2025)..." for time-sensitive information
- "This varies by [jurisdiction/provider/circumstance]..." for variable answers
- "I'm not certain about this — you should verify with..." for low-confidence claims
- "The general principle is X, but your specific case may differ because..."
"""


def domain_aware_response(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=UNCERTAINTY_AWARE_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text


# Examples:
questions = [
    "What is the maximum dose of ibuprofen I can take?",
    "Is it legal to record phone calls without consent?",
    "What's the capital gains tax rate on stocks held for 18 months?",
    "What is the maximum span for a 2x10 floor joist?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {domain_aware_response(q)[:200]}...\n")
```

### Option 2: Domain classifier + confidence gate — route high-stakes questions

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

DOMAIN_CLASSIFIER_PROMPT = """Classify this question's domain and safety sensitivity.
Reply with JSON only: {
  "domain": "<general|medical|legal|financial|engineering|other_professional>",
  "requires_professional": <true|false>,
  "jurisdiction_dependent": <true|false>,
  "time_sensitive": <true|false>,
  "confidence_limit": "<high|medium|low>"
}

Question: {question}"""

SAFE_DEFLECTION_TEMPLATES = {
    "medical": (
        "This is a medical question that requires professional evaluation. "
        "I can share general health information, but for accurate guidance specific to your situation, "
        "please consult a healthcare provider. Here's general background: "
    ),
    "legal": (
        "Legal requirements vary significantly by jurisdiction and specific circumstances. "
        "I can describe general principles, but for advice about your specific situation, "
        "please consult a licensed attorney in your area. General overview: "
    ),
    "financial": (
        "Financial and tax rules depend on your individual circumstances, jurisdiction, and current regulations. "
        "I'll share general principles, but please verify with a CPA or financial advisor. "
        "As of my training data: "
    ),
    "engineering": (
        "Engineering specifications depend on local codes, materials, and professional judgment. "
        "Always consult a licensed engineer for structural or safety-critical decisions. "
        "General reference: "
    ),
}


def classify_domain(question: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": DOMAIN_CLASSIFIER_PROMPT.format(question=question)
        }]
    )
    text = response.content[0].text.strip()
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"domain": "general", "requires_professional": False, "confidence_limit": "high"}


def calibrated_response(question: str) -> dict:
    """
    Classify the domain and prepend appropriate uncertainty framing.
    """
    classification = classify_domain(question)
    domain = classification.get("domain", "general")
    requires_professional = classification.get("requires_professional", False)

    # Prepend domain-specific uncertainty framing:
    prefix = SAFE_DEFLECTION_TEMPLATES.get(domain, "") if requires_professional else ""

    system = UNCERTAINTY_AWARE_SYSTEM if requires_professional else ""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": question}]
    )
    raw_response = response.content[0].text

    return {
        "domain": domain,
        "requires_professional": requires_professional,
        "jurisdiction_dependent": classification.get("jurisdiction_dependent", False),
        "response": prefix + raw_response if prefix else raw_response,
        "confidence_limit": classification.get("confidence_limit", "high")
    }


UNCERTAINTY_AWARE_SYSTEM = """Answer helpfully. In medical, legal, financial, and engineering contexts,
always note relevant limitations, jurisdictional variation, and the need for professional consultation."""

result = calibrated_response("What is a safe dose of acetaminophen for a 6-year-old?")
print(f"Domain: {result['domain']}")
print(f"Response: {result['response'][:300]}...")
```

### Option 3: Structured uncertainty output — explicit confidence per claim

```python
import anthropic

client = anthropic.Anthropic()

STRUCTURED_UNCERTAINTY_TOOL = {
    "name": "structured_answer",
    "description": "Provide a structured answer with explicit confidence per claim",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One-sentence summary of the answer"
            },
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low", "uncertain"],
                            "description": "Confidence in this specific claim"
                        },
                        "caveat": {
                            "type": "string",
                            "description": "What this claim depends on or might vary by"
                        }
                    },
                    "required": ["claim", "confidence"]
                }
            },
            "professional_referral": {
                "type": "string",
                "description": "Recommended professional to consult for this question, or empty string if not needed"
            },
            "knowledge_currency": {
                "type": "string",
                "enum": ["current", "may_be_outdated", "verify_required"],
                "description": "Whether this information is likely still current"
            }
        },
        "required": ["summary", "claims", "professional_referral", "knowledge_currency"]
    }
}

SYSTEM = """You answer questions with explicit calibrated confidence.
Use structured_answer to provide claims with individual confidence levels.
In medical, legal, financial, engineering domains: mark claims as 'medium' or 'low' confidence
unless they are universally true basic facts."""


def calibrated_structured_answer(question: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM,
        tools=[STRUCTURED_UNCERTAINTY_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": question}]
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "structured_answer":
            answer = block.input

            # Format for display:
            print(f"Summary: {answer['summary']}")
            print(f"Knowledge currency: {answer['knowledge_currency']}")
            print(f"Consult: {answer['professional_referral'] or 'Not required'}")
            print("\nClaims:")
            for claim_data in answer.get("claims", []):
                confidence = claim_data["confidence"]
                marker = {"high": "✓", "medium": "~", "low": "?", "uncertain": "✗"}[confidence]
                caveat = f" [{claim_data.get('caveat', '')}]" if claim_data.get("caveat") else ""
                print(f"  {marker} [{confidence.upper()}] {claim_data['claim']}{caveat}")

            return answer

    return {"error": "Model did not call structured_answer tool"}


result = calibrated_structured_answer(
    "Can I take 800mg of ibuprofen three times a day for back pain?"
)
```

### Option 4: Knowledge cutoff acknowledgment — flag time-sensitive information

```python
import anthropic
import re

client = anthropic.Anthropic()

# Topics that change frequently and require knowledge cutoff warnings:
TIME_SENSITIVE_PATTERNS = [
    (r'\b(tax|IRS|deduction|exemption|credit)\b', "tax law"),
    (r'\b(regulation|FDA|EPA|SEC|OSHA|compliance)\b', "regulatory"),
    (r'\b(interest rate|federal funds|prime rate)\b', "interest rates"),
    (r'\b(visa|immigration|green card|citizenship)\b', "immigration law"),
    (r'\b(building code|fire code|zoning|permit)\b', "building codes"),
    (r'\b(drug|medication|approved|clinical trial)\b', "medical/pharmaceutical"),
    (r'\b(GDPR|CCPA|privacy law|data protection)\b', "privacy law"),
    (r'\b(minimum wage|overtime|FLSA)\b', "employment law"),
]

KNOWLEDGE_CUTOFF = "August 2025"

def detect_time_sensitive_domains(question: str) -> list[str]:
    """Detect if question involves time-sensitive domains."""
    domains = []
    for pattern, domain in TIME_SENSITIVE_PATTERNS:
        if re.search(pattern, question, re.IGNORECASE):
            domains.append(domain)
    return list(set(domains))


def add_temporal_disclaimer(response_text: str, domains: list[str]) -> str:
    """Append knowledge cutoff disclaimer for time-sensitive domains."""
    if not domains:
        return response_text

    domain_str = ", ".join(domains)
    disclaimer = (
        f"\n\n---\n"
        f"**Note:** This response covers {domain_str}, which changes frequently. "
        f"My training data has a cutoff of {KNOWLEDGE_CUTOFF}. "
        f"Please verify current rules with official sources or qualified professionals "
        f"before making decisions."
    )
    return response_text + disclaimer


def temporally_aware_response(question: str) -> str:
    time_sensitive_domains = detect_time_sensitive_domains(question)

    system_addendum = ""
    if time_sensitive_domains:
        domain_str = ", ".join(time_sensitive_domains)
        system_addendum = (
            f"This question involves {domain_str}, which changes frequently. "
            f"Note your training data cutoff ({KNOWLEDGE_CUTOFF}) when answering. "
            f"Explicitly flag information that may have changed."
        )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"You are a knowledgeable assistant. {system_addendum}".strip(),
        messages=[{"role": "user", "content": question}]
    )
    raw = response.content[0].text
    return add_temporal_disclaimer(raw, time_sensitive_domains)


print(temporally_aware_response("What is the federal minimum wage?"))
print(temporally_aware_response("What's the capital of France?"))  # no disclaimer added
```

### Option 5: Jurisdiction detector — flag geography-dependent answers

```python
import anthropic
import re

client = anthropic.Anthropic()

JURISDICTION_PATTERNS = {
    "us_state": r'\b(California|Texas|New York|Florida|Illinois|[A-Z]{2} state law)\b',
    "country": r'\b(UK|United Kingdom|Canada|Australia|Germany|France|EU|European Union)\b',
    "local": r'\b(city|county|municipality|local ordinance|zoning)\b',
}

JURISDICTION_SENSITIVE_TOPICS = [
    r'\b(legal|law|statute|regulation|requirement|permitted|prohibited|illegal)\b',
    r'\b(tax|levy|rate|bracket|deduction)\b',
    r'\b(employment|termination|harassment|discrimination|FMLA|PTO)\b',
    r'\b(landlord|tenant|lease|eviction|security deposit)\b',
    r'\b(speed limit|traffic|driving)\b',
]


def is_jurisdiction_sensitive(question: str) -> bool:
    """Check if question is likely jurisdiction-dependent."""
    has_legal_topic = any(
        re.search(p, question, re.IGNORECASE) for p in JURISDICTION_SENSITIVE_TOPICS
    )
    return has_legal_topic


def detect_mentioned_jurisdictions(question: str) -> list[str]:
    """Extract any jurisdictions mentioned in the question."""
    mentioned = []
    for jtype, pattern in JURISDICTION_PATTERNS.items():
        matches = re.findall(pattern, question)
        mentioned.extend(matches)
    return mentioned


def jurisdiction_aware_response(question: str, user_location: str | None = None) -> str:
    sensitive = is_jurisdiction_sensitive(question)
    mentioned = detect_mentioned_jurisdictions(question)

    context_parts = []
    if user_location:
        context_parts.append(f"The user is located in: {user_location}")
    if mentioned:
        context_parts.append(f"The question mentions: {', '.join(mentioned)}")

    if sensitive:
        system = (
            "This question involves topics where rules vary by jurisdiction "
            "(country, state, municipality). When answering:\n"
            "1. Specify which jurisdiction your answer applies to\n"
            "2. Note significant variations in other jurisdictions\n"
            "3. Recommend consulting a local professional for jurisdiction-specific advice\n"
            + ("\n".join(context_parts) if context_parts else "")
        )
    else:
        system = "\n".join(context_parts) if context_parts else ""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text


print(jurisdiction_aware_response(
    "Is it legal to record a phone call without telling the other person?",
    user_location="California, USA"
))
```

### Option 6: Professional referral integration — route to expert resources

```python
import anthropic

client = anthropic.Anthropic()

PROFESSIONAL_RESOURCES = {
    "medical": {
        "primary": "licensed physician or healthcare provider",
        "emergency": "911 or emergency room for urgent symptoms",
        "online": "Verified health information: CDC.gov, Mayo Clinic, NIH.gov",
    },
    "legal": {
        "primary": "licensed attorney in your jurisdiction",
        "low_cost": "legal aid society (income-based free/low-cost help)",
        "online": "State bar association lawyer referral service",
    },
    "financial_tax": {
        "primary": "Certified Public Accountant (CPA) or tax attorney",
        "low_cost": "VITA (IRS free tax help for qualifying individuals)",
        "online": "IRS.gov for official tax rules",
    },
    "engineering_structural": {
        "primary": "licensed Professional Engineer (PE)",
        "code_ref": "Local building department for jurisdiction-specific codes",
    },
}

DOMAIN_SYSTEM = """You are a knowledgeable assistant.
For medical, legal, financial, and engineering questions:
- Provide accurate general information
- Explicitly acknowledge what you don't know or can't know without more context
- Always recommend appropriate professional consultation
- Never overstate confidence in specialized professional domains
"""


def answer_with_referral(question: str, domain: str | None = None) -> dict:
    """
    Answer a question and include appropriate professional referral information.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=DOMAIN_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    answer = response.content[0].text

    # Add referral resources if domain is specified or detected:
    resources = None
    if domain and domain in PROFESSIONAL_RESOURCES:
        resources = PROFESSIONAL_RESOURCES[domain]

    return {
        "answer": answer,
        "professional_resources": resources,
        "domain": domain
    }


# Example:
result = answer_with_referral(
    "I've been having chest pain for 2 days. Could it be a heart attack?",
    domain="medical"
)
print(result["answer"])
if result["professional_resources"]:
    print("\nProfessional Resources:")
    for key, value in result["professional_resources"].items():
        print(f"  {key}: {value}")
```

## Uncertainty Calibration by Domain

| Domain | Key Uncertainty Sources | Required Caveats |
|---|---|---|
| Medical | Patient-specific factors, drug interactions, off-label use | Consult healthcare provider; varies by condition |
| Legal | Jurisdiction, recent case law, specific facts | Varies by state/country; consult attorney |
| Tax/Financial | Income level, filing status, recent law changes | Verify with IRS/CPA; rules change annually |
| Engineering/Structural | Local codes, materials, professional judgment | Check local code; licensed engineer required |
| Regulatory/Compliance | Jurisdiction, industry, recent updates | Verify with regulator; changes frequently |
| Medical dosing | Weight, age, renal function, other drugs | Never give specific doses; consult prescriber |

## Expected Token Savings
Uncertainty framing adds 30–100 tokens per response. This is worthwhile: confidently wrong medical, legal, or financial advice that leads to harm creates far larger costs than the tokens saved by omitting disclaimers. Routing to Haiku for domain classification (Option 2) adds ~50 tokens at minimal cost.

## Environment
- Any customer-facing agent that may answer questions in medical, legal, financial, or engineering domains; mandatory for health, legal-tech, fintech, and compliance applications; the system prompt approach (Option 1) is zero-cost and should be the baseline for all agents; the structured output approach (Option 3) provides the most auditability for compliance purposes; always include professional referral for the four high-stakes domains: medical, legal, financial, engineering
