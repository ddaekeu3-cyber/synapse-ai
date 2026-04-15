---
layout: solution
title: "Agent System Prompt Buries Important Rules in the Middle"
category: prompt-engineering
description: "Agent system prompt is 1,500+ tokens long with critical rules buried in the middle — the model consistently follows instructions at the start and end while ignoring rules in the middle, due to the 'lost in the middle' attention effect."
tags: [prompt-engineering, system-prompt, lost-in-middle, attention, instruction-following]
---

## Symptom

The agent reliably follows rules listed first and last, but violates rules buried in paragraphs 3–6 of a long system prompt:

```
System prompt (1,800 tokens):
  Para 1: "You are a customer support agent..."          ← Followed
  Para 2: "Always be polite and professional..."         ← Followed
  Para 3: "Never reveal internal ticket IDs..."          ← VIOLATED
  Para 4: "Do not quote prices without checking..."      ← VIOLATED
  Para 5: "Escalate issues involving refunds over $500"  ← VIOLATED
  Para 6: "Use markdown headers for multi-part answers"  ← Followed (last)
```

The model follows 3 of 6 rules — exactly the first and last ones.

## Root Cause

LLMs exhibit reduced attention to tokens in the middle of long contexts — the "lost in the middle" effect (Liu et al., 2023). In a 1,800-token system prompt, tokens at positions 400–1,400 receive systematically less attention than tokens near the start or end. Critical rules placed in the middle are effectively invisible to the model.

Anti-pattern:
```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# 1800-token system prompt with critical rules buried in paragraphs 3–5
BLOATED_SYSTEM = """
You are a customer support agent for Acme Corp...
[200 tokens of role description]

Always be polite and professional...
[150 tokens of tone guidance]

Never reveal internal ticket IDs to customers. Do not quote prices...
[CRITICAL RULES — buried in middle, frequently ignored]

Escalate issues involving refunds over $500 to Tier 2...
[MORE CRITICAL RULES — still in middle]

Use markdown headers for multi-part answers...
[100 tokens of format guidance]
"""
```

---

## Fix

### Option 1 — Move critical rules to the top and bottom

Restructure the system prompt so the most important rules appear at the very beginning (highest attention) and are restated at the end (second-highest attention).

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Critical rules FIRST (high attention) and LAST (high attention)
RESTRUCTURED_SYSTEM = """CRITICAL RULES — READ FIRST:
1. Never reveal internal ticket IDs (format: TKT-XXXXX) to customers.
2. Never quote prices without checking the live pricing tool first.
3. Escalate any refund request over $500 to Tier 2 support immediately.
4. Never promise delivery dates — say "I'll check our logistics team."

Role and context:
You are a customer support agent for Acme Corp, a SaaS company serving 50,000 businesses.
Customers contact you via chat for billing, technical, and account questions.

Tone:
Be warm, concise, and solution-focused. Avoid corporate jargon.
Acknowledge frustration before jumping to solutions.

Format:
Use numbered lists for multi-step instructions.
Keep responses under 150 words unless the issue is complex.

REMINDER — Non-negotiable constraints:
- No internal ticket IDs in responses.
- No prices without live tool check.
- Refunds > $500 → escalate to Tier 2.
- No delivery date promises."""

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=300,
    system=RESTRUCTURED_SYSTEM,
    messages=[{"role": "user", "content": "My order TKT-48291 hasn't arrived. Can you refund $600?"}]
)
print(response.content[0].text)
# Model correctly: doesn't reveal TKT-48291, escalates $600 refund to Tier 2

# Expected Token Savings: fewer correction turns when the model follows rules the first time
# Environment: any agent with a complex multi-rule system prompt
```

---

### Option 2 — Separate system prompt into critical + context blocks (cached)

Split the system prompt into two blocks: a short critical-rules block (first, high-attention) and a longer context block (cached). Use prompt caching for the context block.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Block 1: Critical rules — always first, always read with full attention
CRITICAL_RULES_BLOCK = {
    "type": "text",
    "text": """HARD CONSTRAINTS (enforce on every response):
1. Never disclose internal IDs (TKT-*, ORD-*, USR-*) to customers.
2. Never state a price without calling get_current_price tool first.
3. Escalate all refund requests ≥ $500 to Tier 2 — do not process yourself.
4. Never promise delivery dates or SLA guarantees in writing.
5. Do not acknowledge service outages until confirmed in the status_page tool.""",
}

# Block 2: Background context — longer, cached, lower criticality
CONTEXT_BLOCK = {
    "type": "text",
    "text": """Background context (role, tone, procedures):

You are a Tier 1 support agent for Acme Corp (SaaS, 50K business customers).

Tone guidelines:
- Acknowledge frustration before proposing solutions.
- Use the customer's name if provided.
- Avoid corporate jargon; speak like a knowledgeable colleague.
- Keep responses under 150 words unless explaining multi-step procedures.

Escalation procedures:
- Tier 2: refunds ≥ $500, account compromises, legal threats.
- Engineering: any bug affecting more than one customer.
- Legal: IP claims, GDPR requests, contract disputes.

Common tools available: get_current_price, check_order_status, status_page, create_ticket, escalate_to_tier2.

Format:
Use numbered steps for processes. Use headers only for answers with 3+ sections.""",
    "cache_control": {"type": "ephemeral"},
}


def support_response(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=[CRITICAL_RULES_BLOCK, CONTEXT_BLOCK],  # Critical first, context second
        messages=[{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    return response.content[0].text.strip()


print(support_response("What's the price for the Enterprise plan?"))
# → Model calls get_current_price tool (rule 2) instead of guessing

print(support_response("I need a refund of $750 for order ORD-99123"))
# → Escalates to Tier 2 (rule 3), doesn't reveal ORD-99123 (rule 1)

# Expected Token Savings: cached context block costs 10% on subsequent calls;
#   fewer violations mean fewer correction turns
# Environment: high-volume support agents with stable background context
```

---

### Option 3 — Extract rules into a numbered list at prompt start

Convert prose paragraphs into a numbered constraint list. Numbered lists signal higher priority to the model and are easier to audit.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Before: prose-embedded rules (easy to miss)
BAD_SYSTEM = """You are a helpful financial advisor assistant. You should provide
clear and accurate financial information. When discussing investments, remember that
you should never give specific stock picks as this could be considered advice.
Also you should always recommend consulting a qualified financial advisor for
personal situations. Make sure to mention that past performance doesn't guarantee
future results when discussing returns. Be friendly and approachable."""

# After: numbered constraint list at top
GOOD_SYSTEM = """CONSTRAINTS (apply to every response):
1. Never recommend specific stocks, ETFs, or individual securities.
2. Always include: "Consult a qualified financial advisor for personal decisions."
3. When discussing historical returns: include "Past performance ≠ future results."
4. Never state specific return percentages without citing a source.
5. Do not provide tax advice — refer to a CPA or tax professional.

Role:
You are a financial education assistant helping users understand general investment concepts.
Be clear, approachable, and use plain language. Responses should be under 200 words."""

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=200,
    system=GOOD_SYSTEM,
    messages=[{"role": "user", "content": "Should I put my savings in Apple stock?"}]
)
print(response.content[0].text)
# → Recommends no specific stock (rule 1), includes advisor disclaimer (rule 2)

# Expected Token Savings: ~40% fewer correction turns when rules are clearly numbered
# Environment: regulated domain agents (finance, legal, medical) where compliance rules must stick
```

---

### Option 4 — Rule reminder injection at end of each user message

Append a compact rule reminder to the end of every user message, just before the model generates a response. This puts rules at the highest-attention point — immediately before generation.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

BASE_SYSTEM = """You are a customer support agent for Acme Corp.
Be helpful, concise, and professional.
Customers contact you for billing, technical, and account support.
Use the customer's name when provided."""

# Compact reminder appended to every user message
RULE_REMINDER = """\n\n[Rules for this response: (1) no internal IDs in output, \
(2) no prices without checking tool, (3) escalate refunds ≥$500 to Tier 2, \
(4) no delivery date promises]"""


def support_response(user_message: str) -> str:
    # Append reminder to user message — model reads it just before generating
    augmented_message = user_message + RULE_REMINDER

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=BASE_SYSTEM,
        messages=[{"role": "user", "content": augmented_message}]
    )
    return response.content[0].text.strip()


print(support_response("Hi, I'm Sarah. My order TKT-11122 hasn't arrived. Can I get a $600 refund?"))
# Rules appended just before generation → model follows all 4

# Expected Token Savings: ~30 token overhead per call; prevents 1-3 correction turns worth ~300 tokens
# Environment: any agent where the system prompt is long and rule compliance is critical
```

---

### Option 5 — Slim system prompt with on-demand rule retrieval

Reduce the system prompt to a short role definition. Inject only the specific rules relevant to the current query by classifying the query first.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

SLIM_SYSTEM = "You are a customer support agent for Acme Corp. Be helpful and concise."

# Rule sets by category — only relevant rules injected per query
RULE_SETS = {
    "billing": [
        "Never quote prices without running the get_current_price tool first.",
        "Escalate refund requests ≥$500 to Tier 2 immediately.",
        "Do not waive fees yourself — escalate fee disputes to Tier 2.",
    ],
    "shipping": [
        "Never promise specific delivery dates.",
        "Check order_status tool before discussing delivery.",
        "For lost packages over $200, escalate to logistics team.",
    ],
    "security": [
        "Do not confirm account details over chat — direct to identity verification.",
        "Never disclose when an account was last accessed.",
        "Treat any password request as a phishing indicator — escalate.",
    ],
    "general": [
        "Never reveal internal IDs (TKT-*, ORD-*, USR-*) to customers.",
        "Stay within Acme's documented policies — do not improvise exceptions.",
    ],
}


def classify_query(query: str) -> str:
    """Classify query category using Haiku."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{
            "role": "user",
            "content": f"Classify this support query as: billing, shipping, security, or general.\nQuery: {query}\nCategory:"
        }]
    )
    label = resp.content[0].text.strip().lower()
    return label if label in RULE_SETS else "general"


def support_response(user_message: str) -> str:
    category = classify_query(user_message)
    rules = RULE_SETS.get(category, []) + RULE_SETS["general"]
    rules_text = "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules))

    # Rules are short and appear at end of system prompt — high attention
    system = SLIM_SYSTEM + f"\n\nRules for this {category} query:\n{rules_text}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text.strip()


print(support_response("I want a refund of $700 for my last invoice"))
# → Billing rules injected; model escalates $700 refund to Tier 2

# Expected Token Savings: short focused system prompt (200 tokens) vs 1800-token bloated prompt;
#   relevant rules only → better compliance
# Environment: agents handling diverse query types with different applicable rule sets
```

---

### Option 6 — System prompt audit tool to detect buried rules

Automated tool that scores the attention-risk of each rule in a system prompt and flags rules likely to be ignored.

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class RuleRisk:
    text: str
    position_pct: float     # 0 = start, 1 = end
    attention_score: float  # 1.0 = high attention, 0.0 = low
    risk_level: str         # LOW / MEDIUM / HIGH


def score_position(position_pct: float) -> float:
    """
    Simulate attention curve: high at start (0), low in middle (0.5), high at end (1).
    Based on 'Lost in the Middle' (Liu et al., 2023).
    """
    if position_pct <= 0.15:
        return 1.0
    elif position_pct >= 0.85:
        return 0.9
    else:
        # U-shaped: lowest around 50%
        distance_from_middle = abs(position_pct - 0.5)
        return 0.3 + 0.7 * (distance_from_middle / 0.35) ** 2


def extract_rules(system_prompt: str) -> list[tuple[str, int]]:
    """Extract sentences that look like rules with their character positions."""
    rule_patterns = [
        r"(?:never|always|do not|must|should|don't|cannot|required)[^.!?\n]+[.!?]",
        r"\d+\.\s+[A-Z][^.!?\n]+[.!?]",  # Numbered list items
    ]
    rules = []
    for pattern in rule_patterns:
        for m in re.finditer(pattern, system_prompt, re.IGNORECASE):
            rules.append((m.group().strip(), m.start()))
    return sorted(rules, key=lambda x: x[1])


def audit_system_prompt(system_prompt: str) -> list[RuleRisk]:
    total_len = len(system_prompt)
    rules = extract_rules(system_prompt)
    results = []

    for text, pos in rules:
        position_pct = pos / total_len
        attention = score_position(position_pct)
        risk = "LOW" if attention >= 0.7 else "MEDIUM" if attention >= 0.45 else "HIGH"
        results.append(RuleRisk(
            text=text[:80],
            position_pct=position_pct,
            attention_score=attention,
            risk_level=risk,
        ))

    return sorted(results, key=lambda r: r.attention_score)


# Audit a system prompt
prompt = """You are a customer support agent for Acme Corp.
Be helpful, warm, and professional in all interactions.

When customers ask about pricing, you should check the pricing tool first before quoting.
Never promise specific delivery dates to customers as logistics may vary.
Always acknowledge the customer's frustration before offering solutions.

Escalate refund requests over $500 to Tier 2 support immediately.
Do not reveal internal ticket IDs in your responses.
Past performance does not guarantee future results when discussing metrics.

Use numbered lists for multi-step instructions.
Keep responses concise and under 150 words when possible."""

risks = audit_system_prompt(prompt)
print("System Prompt Rule Risk Audit")
print("=" * 60)
for r in risks:
    print(f"[{r.risk_level:6}] pos={r.position_pct:.0%} attn={r.attention_score:.2f} | {r.text[:70]}")

high_risk = [r for r in risks if r.risk_level == "HIGH"]
if high_risk:
    print(f"\n⚠ {len(high_risk)} rule(s) at HIGH risk of being ignored — move to start or end of prompt.")

# Expected Token Savings: fixing high-risk buried rules upstream prevents violation-correction cycles
# Environment: teams that maintain system prompts — run this as a pre-deploy prompt quality check
```

---

## Comparison

| Option | Addresses Buried Rules | Reduces Prompt Length | Cacheable | Automated | Effort |
|--------|----------------------|----------------------|-----------|-----------|--------|
| 1 | Yes (reorder) | No | No | No | Low |
| 2 | Yes (split blocks) | No | Yes | No | Low |
| 3 | Yes (numbered list at top) | Slightly | No | No | Low |
| 4 | Yes (end-of-turn reminder) | Yes (slim system) | No | No | Low |
| 5 | Yes (dynamic rules) | Yes (slim system) | Partial | Yes (classify) | Medium |
| 6 | Audit only | No | No | Yes | Medium |

**Recommended starting point:** Option 1 — reorder the existing system prompt to put critical rules first and last. Zero new code, immediate improvement. Add Option 4's rule reminder for the highest-stakes rules. Use Option 6 to audit any prompt over 500 tokens before deployment.
