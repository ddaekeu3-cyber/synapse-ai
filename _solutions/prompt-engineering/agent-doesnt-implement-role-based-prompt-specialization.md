---
title: "Agent Doesn't Implement Role-Based Prompt Specialization"
description: "Using one generic system prompt for all tasks makes the agent mediocre at everything. Role-based prompt specialization assigns purpose-built personas and instructions per task type, dramatically improving output quality for each domain."
difficulty: beginner
category: prompt-engineering
tags: [prompt-engineering, roles, personas, specialization, system-prompt, task-routing]
---

## Problem

A single generic system prompt — "You are a helpful AI assistant" — produces average-quality output for every task. A coding question gets the same prompt treatment as a legal review or a creative writing task. Role-based prompt specialization routes each request to a purpose-built system prompt tuned for that domain, matching tone, expertise, format expectations, and constraints to what the task actually needs.

```python
# BAD: one prompt for everything
async def handle_request(user_input: str) -> str:
    return await call_model(
        system="You are a helpful assistant.",
        user=user_input
    )
# Produces mediocre output regardless of task type
```

## Solution 1: Task Classifier + Role Router

Classify the task first, then select the matching specialist prompt.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

ROLE_PROMPTS = {
    "code": """You are an expert software engineer with deep knowledge of Python, system design, and best practices.
When answering:
- Provide working, production-ready code
- Include type hints and docstrings for non-trivial functions
- Call out potential edge cases or pitfalls
- Prefer clarity over cleverness
- Use standard library when possible before reaching for third-party packages""",

    "data_analysis": """You are a senior data scientist skilled in statistical analysis, Python (pandas, numpy, scipy), and data visualization.
When answering:
- Start with the analytical approach before code
- Explain what the numbers mean, not just how to compute them
- Flag data quality issues that could affect results
- Recommend appropriate statistical tests for the use case
- Include visualization suggestions where relevant""",

    "writing": """You are an experienced technical writer and editor.
When answering:
- Match tone and register to the intended audience
- Prioritize clarity and concision over length
- Use active voice and concrete examples
- Structure content with appropriate headings and hierarchy
- Avoid jargon unless writing for a specialist audience""",

    "legal": """You are a knowledgeable legal assistant. Important: you provide legal information, not legal advice.
When answering:
- Clearly distinguish between legal information (general) and legal advice (specific)
- Cite relevant legal concepts and their implications
- Flag jurisdiction-specific considerations
- Recommend consulting a licensed attorney for specific situations
- Use precise legal terminology with plain-English explanations""",

    "general": """You are a knowledgeable, thoughtful assistant.
When answering:
- Be direct and concise
- Acknowledge uncertainty where it exists
- Provide context when it adds value
- Ask clarifying questions if the request is ambiguous""",
}

CLASSIFIER_PROMPT = """Classify this user request into exactly one category. Output only the category name.

Categories:
- code: programming, debugging, software architecture, APIs, algorithms
- data_analysis: data processing, statistics, ML, analytics, charts
- writing: drafting, editing, summarizing, translating, creative writing
- legal: contracts, compliance, regulations, legal concepts
- general: everything else

Request: {request}

Category:"""

async def classify_task(user_input: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{
            "role": "user",
            "content": CLASSIFIER_PROMPT.format(request=user_input[:300])
        }]
    )
    category = response.content[0].text.strip().lower()
    return category if category in ROLE_PROMPTS else "general"

async def handle_with_role(user_input: str) -> tuple[str, str]:
    role = await classify_task(user_input)
    system_prompt = ROLE_PROMPTS[role]

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_input}]
    )
    return role, response.content[0].text if response.content else ""

async def main():
    test_cases = [
        "Write a Python function to parse JSON with error handling",
        "What's the difference between a t-test and ANOVA?",
        "Help me write a professional email declining a meeting",
    ]
    for query in test_cases:
        role, answer = await handle_with_role(query)
        print(f"\n[Role: {role}] {query[:50]}...")
        print(answer[:200])

asyncio.run(main())
```

## Solution 2: Composable Role Mixins

Build system prompts by combining reusable prompt fragments.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Reusable prompt fragments
MIXINS = {
    "concise": "Be concise. Answer in as few words as necessary without sacrificing accuracy.",
    "cite_sources": "When making factual claims, indicate whether they are well-established or uncertain.",
    "code_quality": "All code must be correct, handle errors gracefully, and follow PEP 8.",
    "no_markdown": "Do not use markdown formatting. Plain text only.",
    "structured_output": "Structure your response with clear sections and headers.",
    "step_by_step": "Break down your reasoning step by step before giving the final answer.",
    "expert_tone": "Assume the reader is a domain expert. Skip basic explanations.",
    "beginner_tone": "Assume the reader is a beginner. Define technical terms when first used.",
    "conservative": "When uncertain, err on the side of caution and recommend expert consultation.",
    "creative": "Be creative and explore unconventional approaches before the obvious one.",
}

ROLE_BASES = {
    "senior_engineer": (
        "You are a senior software engineer with 15 years of experience in distributed systems, "
        "Python, and cloud infrastructure.",
        ["code_quality", "expert_tone", "step_by_step"]
    ),
    "junior_tutor": (
        "You are a patient programming tutor helping beginners learn to code.",
        ["code_quality", "beginner_tone", "step_by_step", "structured_output"]
    ),
    "analyst": (
        "You are a business analyst who translates technical concepts into business value.",
        ["concise", "structured_output", "cite_sources"]
    ),
    "reviewer": (
        "You are a rigorous code reviewer focused on correctness, security, and maintainability.",
        ["code_quality", "expert_tone", "conservative"]
    ),
}

def build_system_prompt(role: str, extra_mixins: list[str] | None = None) -> str:
    base_description, default_mixins = ROLE_BASES.get(role, ("You are a helpful assistant.", []))
    all_mixins = list(dict.fromkeys(default_mixins + (extra_mixins or [])))  # deduplicate, preserve order
    mixin_text = "\n".join(f"- {MIXINS[m]}" for m in all_mixins if m in MIXINS)
    return f"{base_description}\n\nGuidelines:\n{mixin_text}"

async def call_with_role_mixins(
    role: str,
    user_input: str,
    extra_mixins: list[str] | None = None
) -> str:
    system = build_system_prompt(role, extra_mixins)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_input}]
    )
    return response.content[0].text if response.content else ""

async def main():
    # Senior engineer with extra conciseness
    result = await call_with_role_mixins(
        "senior_engineer",
        "Review this code: def divide(a, b): return a/b",
        extra_mixins=["concise"]
    )
    print(f"[Senior Engineer + Concise]\n{result[:300]}\n")

    # Beginner tutor explaining the same concept
    result = await call_with_role_mixins(
        "junior_tutor",
        "Explain what happens when you divide by zero in Python"
    )
    print(f"[Junior Tutor]\n{result[:300]}")

asyncio.run(main())
```

## Solution 3: Dynamic Role Injection from Context

Infer the appropriate role from conversation context rather than explicit classification.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

ROLES = {
    "debugger": {
        "system": (
            "You are an expert debugger. Your job is to find root causes, not just symptoms. "
            "For every bug: (1) state your hypothesis, (2) explain the evidence, (3) provide a fix, "
            "(4) explain how to prevent recurrence."
        ),
        "signals": ["error", "bug", "exception", "crash", "not working", "broken", "fix", "traceback"]
    },
    "architect": {
        "system": (
            "You are a software architect. You think in systems, trade-offs, and long-term maintainability. "
            "For design questions: present 2-3 options, state trade-offs clearly, give a recommendation with rationale."
        ),
        "signals": ["design", "architecture", "structure", "pattern", "system", "scale", "approach", "best way"]
    },
    "optimizer": {
        "system": (
            "You are a performance optimization expert. You think in algorithmic complexity, "
            "memory usage, and profiling. Always ask: what is the bottleneck? Measure before optimizing."
        ),
        "signals": ["slow", "performance", "optimize", "faster", "latency", "throughput", "bottleneck", "cache"]
    },
    "explainer": {
        "system": (
            "You are a gifted teacher who makes complex concepts simple without losing accuracy. "
            "Use analogies, concrete examples, and build from simple to complex."
        ),
        "signals": ["explain", "what is", "how does", "understand", "confused", "help me", "tutorial", "learn"]
    },
}

DEFAULT_SYSTEM = "You are a knowledgeable and helpful assistant."

def infer_role(user_input: str) -> str | None:
    lower = user_input.lower()
    scores = {}
    for role, config in ROLES.items():
        score = sum(1 for signal in config["signals"] if signal in lower)
        if score > 0:
            scores[role] = score
    if not scores:
        return None
    return max(scores, key=lambda r: scores[r])

async def smart_role_dispatch(user_input: str, conversation_history: list[dict] | None = None) -> tuple[str, str]:
    role = infer_role(user_input)
    system = ROLES[role]["system"] if role else DEFAULT_SYSTEM

    messages = list(conversation_history or [])
    messages.append({"role": "user", "content": user_input})

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        messages=messages
    )
    output = response.content[0].text if response.content else ""
    return role or "default", output

async def main():
    queries = [
        "My Python script crashes with AttributeError: 'NoneType' object has no attribute 'split'",
        "What's the best way to design a rate limiter for a REST API?",
        "My database queries are really slow on large tables",
        "Can you explain how async/await works in Python?",
    ]
    for query in queries:
        role, answer = await smart_role_dispatch(query)
        print(f"\n[Auto-role: {role}] {query[:60]}...")
        print(answer[:250])

asyncio.run(main())
```

## Solution 4: Multi-Role Panel for Ambiguous Requests

For complex tasks, invoke multiple specialist roles and synthesize their perspectives.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class PanelResponse:
    role: str
    perspective: str
    key_point: str

PANEL_ROLES = {
    "security": (
        "You are a security engineer. Review this from a security and risk perspective. "
        "What could go wrong? What are the attack vectors or vulnerabilities? Be specific."
    ),
    "performance": (
        "You are a performance engineer. Review this from a performance perspective. "
        "What are the bottlenecks, scalability concerns, or efficiency improvements?"
    ),
    "maintainability": (
        "You are a senior developer focused on code quality. Review this from a maintainability perspective. "
        "What makes this hard to maintain, test, or evolve?"
    ),
    "ux": (
        "You are a UX-focused engineer. Review this from a user experience perspective. "
        "How does this affect the end user? What friction points exist?"
    ),
}

async def get_panel_perspective(role: str, system: str, task: str) -> PanelResponse:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system + "\n\nBe concise. Lead with your single most important point.",
        messages=[{"role": "user", "content": task}]
    )
    text = response.content[0].text if response.content else ""
    first_sentence = text.split(".")[0] + "." if "." in text else text[:100]
    return PanelResponse(role=role, perspective=text, key_point=first_sentence)

async def synthesize_panel(task: str, panel_responses: list[PanelResponse]) -> str:
    perspectives = "\n\n".join(
        f"[{r.role.upper()}]: {r.key_point}" for r in panel_responses
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a technical lead synthesizing multiple expert perspectives into a balanced recommendation.",
        messages=[{
            "role": "user",
            "content": (
                f"Task: {task}\n\n"
                f"Expert perspectives:\n{perspectives}\n\n"
                f"Synthesize these into a prioritized recommendation."
            )
        }]
    )
    return response.content[0].text if response.content else ""

async def multi_role_panel(task: str, roles: list[str] | None = None) -> dict:
    selected_roles = roles or list(PANEL_ROLES.keys())

    # All specialist calls in parallel
    panel_responses = await asyncio.gather(*[
        get_panel_perspective(role, PANEL_ROLES[role], task)
        for role in selected_roles
        if role in PANEL_ROLES
    ])

    synthesis = await synthesize_panel(task, list(panel_responses))

    return {
        "task": task,
        "perspectives": {r.role: r.key_point for r in panel_responses},
        "synthesis": synthesis
    }

async def main():
    result = await multi_role_panel(
        "We're considering storing user session tokens as plain text in localStorage for simplicity."
    )
    print("Perspectives:")
    for role, point in result["perspectives"].items():
        print(f"  [{role}] {point}")
    print(f"\nSynthesis:\n{result['synthesis'][:400]}")

asyncio.run(main())
```

## Solution 5: Role-Aware Few-Shot Example Selection

Attach role-specific few-shot examples that demonstrate the expected output style.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

FEW_SHOT_EXAMPLES = {
    "code_reviewer": [
        {
            "user": "Review: def get_user(id): return db.query(f'SELECT * FROM users WHERE id={id}')",
            "assistant": (
                "**Critical: SQL Injection vulnerability**\n"
                "String interpolation in SQL queries allows attackers to inject arbitrary SQL.\n\n"
                "**Fix:**\n```python\ndef get_user(user_id: int) -> dict | None:\n"
                "    return db.query('SELECT * FROM users WHERE id = %s', (user_id,))\n```\n"
                "Use parameterized queries. Also: rename `id` param to `user_id` to avoid shadowing built-in."
            )
        }
    ],
    "data_analyst": [
        {
            "user": "I have sales data for 12 months. How do I check if there's a trend?",
            "assistant": (
                "For trend detection on 12 months of data, I'd use two approaches:\n\n"
                "1. **Visual**: Plot with a rolling 3-month average to smooth noise\n"
                "2. **Statistical**: Linear regression on time index → p-value on slope coefficient\n\n"
                "```python\nimport numpy as np\nfrom scipy import stats\n"
                "slope, intercept, r, p, se = stats.linregress(range(12), sales)\n"
                "print(f'Trend: {slope:.2f}/month, p={p:.3f}')\n```\n"
                "If p < 0.05, the trend is statistically significant. "
                "Watch out for seasonality — 12 months may not be enough to distinguish trend from cycle."
            )
        }
    ],
}

def build_few_shot_messages(role: str, user_input: str) -> list[dict]:
    examples = FEW_SHOT_EXAMPLES.get(role, [])
    messages = []
    for ex in examples:
        messages.append({"role": "user", "content": ex["user"]})
        messages.append({"role": "assistant", "content": ex["assistant"]})
    messages.append({"role": "user", "content": user_input})
    return messages

ROLE_SYSTEMS = {
    "code_reviewer": (
        "You are a rigorous code reviewer. Focus on correctness, security, performance, and readability. "
        "Always provide a fixed version when pointing out issues."
    ),
    "data_analyst": (
        "You are a senior data analyst. Combine statistical rigor with practical business context. "
        "Always include runnable code examples."
    ),
}

async def call_with_few_shot_role(role: str, user_input: str) -> str:
    system = ROLE_SYSTEMS.get(role, "You are a helpful assistant.")
    messages = build_few_shot_messages(role, user_input)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        messages=messages
    )
    return response.content[0].text if response.content else ""

async def main():
    result = await call_with_few_shot_role(
        "code_reviewer",
        "Review: def login(username, password): user = db.get(username); return user.password == password"
    )
    print(f"[Code Reviewer]\n{result[:400]}")

asyncio.run(main())
```

## Solution 6: Role Registry with Capability Declarations

Declare what each role can and cannot do, enabling automated capability checks before routing.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class Role:
    name: str
    system_prompt: str
    can_handle: list[str]       # task type keywords
    cannot_handle: list[str]    # things explicitly out of scope
    max_tokens: int = 1024
    model: str = "claude-haiku-4-5-20251001"
    temperature_hint: str = "balanced"  # "precise" | "balanced" | "creative"

ROLE_REGISTRY: dict[str, Role] = {
    "medical_informer": Role(
        name="medical_informer",
        system_prompt=(
            "You are a medical information assistant. You provide general health information "
            "based on established medical literature. You do NOT diagnose, prescribe, or replace "
            "professional medical advice. Always recommend consulting a qualified healthcare provider "
            "for personal medical decisions."
        ),
        can_handle=["symptoms", "medication", "health", "disease", "nutrition", "anatomy"],
        cannot_handle=["diagnosis", "prescription", "dosage recommendation", "treatment plan"],
        model="claude-haiku-4-5-20251001",
        temperature_hint="precise"
    ),
    "creative_writer": Role(
        name="creative_writer",
        system_prompt=(
            "You are a creative writer with a distinctive voice. You craft engaging narratives, "
            "vivid descriptions, and compelling characters. Embrace unexpected angles, subvert clichés, "
            "and prioritize emotional resonance over literal accuracy."
        ),
        can_handle=["story", "poem", "creative", "fiction", "narrative", "character", "dialogue"],
        cannot_handle=["factual analysis", "code", "legal advice", "medical information"],
        max_tokens=2048,
        temperature_hint="creative"
    ),
    "technical_docs": Role(
        name="technical_docs",
        system_prompt=(
            "You are a technical documentation specialist. You write clear, accurate, and complete "
            "documentation for software systems. Use consistent terminology, include examples, "
            "and structure content for both quick reference and deep reading."
        ),
        can_handle=["documentation", "readme", "api docs", "how-to", "reference", "guide"],
        cannot_handle=["opinion pieces", "creative writing", "medical or legal advice"],
        temperature_hint="precise"
    ),
}

def check_capability(role: Role, user_input: str) -> tuple[bool, str | None]:
    lower = user_input.lower()
    for blocked in role.cannot_handle:
        if blocked in lower:
            return False, f"Role '{role.name}' cannot handle: {blocked}"
    return True, None

def select_role(user_input: str) -> Role | None:
    lower = user_input.lower()
    best_role = None
    best_score = 0
    for role in ROLE_REGISTRY.values():
        score = sum(1 for kw in role.can_handle if kw in lower)
        capable, _ = check_capability(role, user_input)
        if score > best_score and capable:
            best_score = score
            best_role = role
    return best_role

async def dispatch(user_input: str) -> tuple[str, str]:
    role = select_role(user_input)
    if not role:
        system = "You are a helpful assistant."
        role_name = "default"
    else:
        system = role.system_prompt
        role_name = role.name

        capable, reason = check_capability(role, user_input)
        if not capable:
            return role_name, f"This request is outside my scope: {reason}"

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_input}]
    )
    return role_name, response.content[0].text if response.content else ""

async def main():
    queries = [
        "What are common symptoms of iron deficiency?",
        "Write a short poem about debugging code at 3am",
        "Write API documentation for a user authentication endpoint",
    ]
    for query in queries:
        role_name, answer = await dispatch(query)
        print(f"\n[Role: {role_name}] {query[:55]}...")
        print(answer[:250])

asyncio.run(main())
```

## Comparison

| Approach | Routing Method | Flexibility | Latency | Best For |
|---|---|---|---|---|
| Classifier + Router | LLM classification | High | +1 call | General-purpose agents |
| Composable Mixins | Static keyword match | Very High | None | Configurable role systems |
| Context Inference | Keyword scoring | Medium | None | Chat-style interfaces |
| Multi-Role Panel | All specialists | Very High | N parallel calls | High-stakes decisions |
| Few-Shot Role | Examples in context | High | +tokens | Consistent output style |
| Role Registry | Capability-declared | High | None | Governed multi-agent systems |

**Rule of thumb**: Start with composable mixins for low-overhead specialization. Add a classifier when task types are unclear. Use the panel pattern only for high-stakes decisions where multiple expert perspectives add value.
