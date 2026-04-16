---
title: "Agent Doesn't Implement Negative Space Prompting"
description: "Guide model behavior by explicitly stating what NOT to do—reducing unwanted outputs, hallucinations, and off-topic responses through deliberate negative constraints."
difficulty: intermediate
category: prompt-engineering
tags: [negative-space, constraints, prompt-engineering, hallucination, output-quality]
---

## Problem

Prompts focus entirely on what the model should do, leaving implicit all the behaviors to avoid. Models then fill that negative space with default patterns: verbose disclaimers, unsolicited advice, hedging language, or hallucinated details. Explicit negative constraints dramatically reduce these behaviors without requiring complex output parsers.

## Solutions

### Option 1: Explicit Do-Not List

Add a structured "do not" section to the system prompt that mirrors the positive instructions.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

SYSTEM_PROMPT = """You are a concise technical assistant.

DO:
- Answer the specific question asked
- Use code examples when relevant
- Give the shortest complete answer

DO NOT:
- Add disclaimers like "I should note..." or "It's worth mentioning..."
- Repeat the question back to the user
- Add closing phrases like "I hope this helps!" or "Let me know if..."
- Suggest consulting a professional unless safety is at risk
- Pad answers with background context that wasn't asked for
- Use hedging phrases like "generally speaking" or "in most cases" unless variance is specifically relevant
"""

async def ask(question: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text

async def compare():
    question = "How do I reverse a list in Python?"

    # Without negative constraints
    plain_response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="You are a helpful technical assistant.",
        messages=[{"role": "user", "content": question}]
    )

    # With negative constraints
    constrained_response = await ask(question)

    print("=== Without negative constraints ===")
    print(plain_response.content[0].text)
    print("\n=== With negative constraints ===")
    print(constrained_response)

asyncio.run(compare())
```

### Option 2: Negative Persona Anchoring

Define who the agent is NOT as a way to calibrate tone, depth, and style.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def build_system_prompt(domain: str, audience: str) -> str:
    return f"""You are a {domain} expert writing for {audience}.

You are NOT:
- A customer service rep (don't apologize or over-explain)
- A professor (don't give lectures; skip foundational theory unless asked)
- A lawyer (don't caveat every statement with liability hedges)
- A cheerleader (don't validate questions or praise them as "great question")
- A search engine (don't list 10 options when 2 are clearly better)

When you see ambiguity, pick the most likely interpretation and answer it.
If the question has one clear best answer, give that—don't artificially balance perspectives.
"""

async def expert_answer(domain: str, audience: str, question: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=build_system_prompt(domain, audience),
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text

async def demo():
    scenarios = [
        ("database engineering", "senior backend engineers",
         "Should I use UUIDs or sequential IDs for primary keys?"),
        ("financial modeling", "startup founders",
         "Should I raise a seed round or bootstrap?"),
        ("DevOps", "mid-level engineers",
         "Kubernetes or Docker Compose for a team of 5?"),
    ]

    for domain, audience, question in scenarios:
        print(f"\nQ ({domain} for {audience}): {question}")
        answer = await expert_answer(domain, audience, question)
        print(f"A: {answer[:300]}...")

asyncio.run(demo())
```

### Option 3: Output-Shape Negative Constraints

Constrain the format and structure of outputs by explicitly banning unwanted patterns.

```python
import asyncio
import re
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

BANNED_PATTERNS = [
    r"(?i)i hope this helps",
    r"(?i)let me know if you have",
    r"(?i)feel free to ask",
    r"(?i)great question",
    r"(?i)certainly!",
    r"(?i)of course!",
    r"(?i)absolutely!",
    r"(?i)as an AI",
    r"(?i)i should note that",
    r"(?i)it's worth (noting|mentioning)",
    r"(?i)please note that",
    r"(?i)keep in mind that",
]

FORMAT_CONSTRAINTS = """
Output format rules — violations will be flagged:
- Do NOT start with affirmations ("Sure!", "Great!", "Certainly!")
- Do NOT end with offers to help further
- Do NOT use bullet points with more than 5 items unless specifically asked for a list
- Do NOT include markdown headers (###) for responses under 200 words
- Do NOT repeat any word or phrase used in the user's question as your opening word
"""

def check_violations(text: str) -> list[str]:
    violations = []
    for pattern in BANNED_PATTERNS:
        if re.search(pattern, text):
            violations.append(f"Banned pattern: {pattern}")
    return violations

async def constrained_response(user_message: str, system_extra: str = "") -> tuple[str, list[str]]:
    system = FORMAT_CONSTRAINTS + "\n" + system_extra
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": user_message}]
    )
    text = response.content[0].text
    violations = check_violations(text)
    return text, violations

async def run_output_shape_test():
    test_messages = [
        "Explain what an API is.",
        "What's the difference between TCP and UDP?",
        "How should I handle errors in Python?",
    ]

    total_violations = 0
    for msg in test_messages:
        text, violations = await constrained_response(msg)
        total_violations += len(violations)
        status = "PASS" if not violations else f"FAIL ({len(violations)} violations)"
        print(f"\n[{status}] {msg[:50]}")
        if violations:
            for v in violations:
                print(f"  - {v}")

    print(f"\nTotal violations: {total_violations}/{len(test_messages)} responses")

asyncio.run(run_output_shape_test())
```

### Option 4: Domain-Specific Hallucination Guards

Use negative constraints to block the specific hallucination categories most common in your domain.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class DomainGuards:
    domain: str
    hallucination_risks: list[str]
    required_uncertainty_phrases: list[str]

DOMAIN_GUARDS = {
    "medical": DomainGuards(
        domain="medical information",
        hallucination_risks=[
            "Do NOT cite specific dosages, drug interaction rates, or clinical trial numbers unless you are 100% certain",
            "Do NOT name specific medications as treatments for conditions",
            "Do NOT give prognosis percentages or survival rates",
            "Do NOT reference specific medical guidelines by name (e.g., 'AHA guidelines state...')",
        ],
        required_uncertainty_phrases=["consult a physician", "not medical advice"]
    ),
    "legal": DomainGuards(
        domain="legal information",
        hallucination_risks=[
            "Do NOT cite specific case names, docket numbers, or statute numbers you are not certain of",
            "Do NOT state what courts have 'ruled' without certainty",
            "Do NOT give jurisdiction-specific advice unless jurisdiction is specified",
            "Do NOT state deadlines or filing windows as facts",
        ],
        required_uncertainty_phrases=["consult an attorney", "not legal advice"]
    ),
    "financial": DomainGuards(
        domain="financial information",
        hallucination_risks=[
            "Do NOT cite specific return rates, yields, or historical performance figures",
            "Do NOT name specific securities as investments",
            "Do NOT state tax rules as universally applicable",
            "Do NOT give specific price targets or valuations",
        ],
        required_uncertainty_phrases=["consult a financial advisor", "not financial advice"]
    ),
}

def build_guarded_system(guard: DomainGuards) -> str:
    risks = "\n".join(f"- {r}" for r in guard.hallucination_risks)
    return f"""You provide general {guard.domain} information.

HALLUCINATION GUARDS — strictly enforced:
{risks}

When uncertain, say "I'm not certain" or "you should verify this" rather than stating it as fact.
Always include: {' or '.join(f'"{p}"' for p in guard.required_uncertainty_phrases)}.
"""

async def guarded_answer(domain_key: str, question: str) -> str:
    guard = DOMAIN_GUARDS[domain_key]
    system = build_guarded_system(guard)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text

async def demo_domain_guards():
    test_cases = [
        ("medical", "What dose of ibuprofen should I take for back pain?"),
        ("legal", "Can my landlord enter my apartment without notice?"),
        ("financial", "Should I invest in tech stocks right now?"),
    ]

    for domain, question in test_cases:
        print(f"\n[{domain.upper()}] {question}")
        answer = await guarded_answer(domain, question)
        print(answer[:400])

asyncio.run(demo_domain_guards())
```

### Option 5: Competitive Negative Prompting

Show the model examples of bad outputs to avoid, alongside good ones.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

FEW_SHOT_NEGATIVE = """You write crisp, direct responses. Study these examples:

QUESTION: What is recursion?
BAD RESPONSE: "Great question! Recursion is a fascinating concept in computer science that has been around for many decades. I'll explain it to you step by step. Essentially, recursion occurs when... [200 more words] ...I hope this explanation was helpful! Let me know if you need clarification."
GOOD RESPONSE: "A function that calls itself. Each call solves a smaller version of the problem until hitting a base case that stops the recursion.

```python
def factorial(n):
    if n <= 1: return 1       # base case
    return n * factorial(n-1) # recursive call
```"

QUESTION: How do I center a div in CSS?
BAD RESPONSE: "Centering elements in CSS has historically been a challenge for many developers, and there are multiple approaches depending on your use case, browser requirements, and layout context. Let me walk through the main options..."
GOOD RESPONSE: "```css
/* Modern: flexbox */
.parent { display: flex; justify-content: center; align-items: center; }

/* Or: grid */
.parent { display: grid; place-items: center; }
```"

Now respond with the same quality to the user's question.
"""

async def competitive_negative_response(question: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=FEW_SHOT_NEGATIVE,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text

async def demo_competitive():
    questions = [
        "What is a closure in JavaScript?",
        "How does HTTP caching work?",
        "What's the difference between == and === in Python?",
    ]

    for q in questions:
        print(f"\nQ: {q}")
        answer = await competitive_negative_response(q)
        print(f"A: {answer}")

asyncio.run(demo_competitive())
```

### Option 6: Adaptive Negative Constraint Learning

Track which unwanted patterns appear in outputs and dynamically tighten constraints.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from collections import defaultdict
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class PatternTracker:
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    threshold: int = 3  # Add to negative prompt after N occurrences

    DETECTABLE_PATTERNS = {
        "affirmation_opener": r"^(Sure|Certainly|Of course|Absolutely|Great|Happy to)",
        "hope_this_helps": r"(?i)hope this helps",
        "follow_up_offer": r"(?i)(let me know|feel free|don't hesitate)",
        "ai_disclosure": r"(?i)as an AI",
        "hedging": r"(?i)(generally speaking|in most cases|typically|usually) (it|this|you)",
        "question_repeat": r"(?i)you('ve| have) asked (about|how|what|why)",
    }

    def scan(self, text: str) -> list[str]:
        found = []
        for name, pattern in self.DETECTABLE_PATTERNS.items():
            if re.search(pattern, text):
                self.counts[name] += 1
                found.append(name)
        return found

    def build_negative_constraints(self) -> str:
        active = [
            name for name, count in self.counts.items()
            if count >= self.threshold
        ]
        if not active:
            return ""

        constraint_map = {
            "affirmation_opener": "Do not start responses with affirmations (Sure, Certainly, etc.)",
            "hope_this_helps": 'Do not use "I hope this helps" or similar phrases',
            "follow_up_offer": "Do not offer further help at the end of responses",
            "ai_disclosure": 'Do not say "as an AI"',
            "hedging": "Avoid hedging language like 'generally speaking' or 'typically'",
            "question_repeat": "Do not paraphrase or repeat the user's question",
        }

        lines = [constraint_map[p] for p in active if p in constraint_map]
        return "\nAdditional constraints (auto-learned):\n" + "\n".join(f"- {l}" for l in lines)

class AdaptiveAgent:
    def __init__(self):
        self.tracker = PatternTracker()
        self.base_system = "You are a precise technical assistant. Answer concisely."

    async def respond(self, message: str) -> tuple[str, list[str]]:
        dynamic_constraints = self.tracker.build_negative_constraints()
        system = self.base_system + dynamic_constraints

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": message}]
        )
        text = response.content[0].text
        violations = self.tracker.scan(text)
        return text, violations

async def demo_adaptive():
    agent = AdaptiveAgent()
    questions = [
        "What is dependency injection?",
        "Explain the singleton pattern.",
        "What is event-driven architecture?",
        "How does memoization work?",
        "What is a message queue?",
    ]

    for i, q in enumerate(questions):
        text, violations = await agent.respond(q)
        constraint_count = len(agent.tracker.build_negative_constraints().split("\n"))
        print(f"\n[Turn {i+1}] Violations: {violations or 'none'}, Active constraints: {constraint_count}")
        print(f"Response preview: {text[:150]}...")

    print(f"\nFinal pattern counts: {dict(agent.tracker.counts)}")

asyncio.run(demo_adaptive())
```

## Comparison

| Approach | Constraint Type | Learning | Maintenance | Best For |
|---|---|---|---|---|
| Explicit Do-Not List | Static list | None | Manual | General-purpose agents |
| Negative Persona Anchoring | Tone/style | None | Low | Role-based agents |
| Output-Shape Constraints | Format/structure | None (auto-check) | Low | Structured output agents |
| Domain Hallucination Guards | Factual safety | None | Per-domain | High-stakes domains |
| Competitive Negative Prompting | Behavioral via examples | None | Moderate | Fine-grained style control |
| Adaptive Constraint Learning | All types | Automatic | Low | Production agents with feedback loops |

**Choose Explicit Do-Not List** as a first step—it's the highest ROI per line of prompt. **Choose Domain Hallucination Guards** for medical, legal, or financial agents where hallucinations have real consequences. **Choose Adaptive Constraint Learning** when you have production traffic to learn from and want constraints to tighten automatically over time.
