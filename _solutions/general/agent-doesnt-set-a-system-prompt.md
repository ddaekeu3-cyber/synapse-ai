---
layout: solution
title: "Agent Doesn't Set a System Prompt"
category: general
description: "Agent calls the API with no system prompt — getting default model behavior instead of task-specific instructions. The model adds unnecessary disclaimers, formats output inconsistently, refuses reasonable requests, and ignores domain conventions the agent relies on."
tags: [general, system-prompt, configuration, output-quality, consistency, persona]
---

## Symptom

Agent returns responses prefixed with "As an AI language model, I should mention that..." or adds unsolicited caveats to every answer. Output format changes between requests — sometimes markdown, sometimes plain text. The model declines edge-case requests that a properly-configured system prompt would handle correctly. Downstream parsers break because the format is unpredictable.

Response format consistency without system prompt: **~40%**
With task-specific system prompt: **~95%**

## Root Cause

The API call omits the `system` parameter entirely. The model falls back to default behavior designed for general-purpose use — not for the specific task the agent is performing. Default behavior includes safety hedges, format ambiguity, and general-purpose verbosity that is wrong for specialised agents.

## Fix

---

### Option 1 — Minimal Task-Specific System Prompt

Add a focused system prompt that defines role, output format, and tone. Three sentences are enough to eliminate most default-behavior problems.

```python
import anthropic

client = anthropic.Anthropic()

# Without system prompt — default model behavior
def call_without_system(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

# With task-specific system prompt — controlled behavior
def call_with_system(user_message: str, system: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

# Example 1: Sentiment classifier
SENTIMENT_SYSTEM = """You are a sentiment classifier.
Classify the input as exactly one of: positive, negative, neutral.
Output only the label — no explanation, no punctuation."""

review = "The shipping was fast but the product quality was disappointing."
print("=== Sentiment Classifier ===")
print(f"Without system: {call_without_system('Classify: ' + review)[:80]}")
print(f"With system:    {call_with_system(review, SENTIMENT_SYSTEM)}")

# Example 2: Code reviewer
CODE_REVIEW_SYSTEM = """You are a senior Python code reviewer.
Review the provided code and list issues in this format:
[SEVERITY] Description (line N if applicable)
Severities: CRITICAL | HIGH | MEDIUM | LOW | INFO
Be concise. No preamble."""

code = """
def divide(a, b):
    return a / b

result = divide(10, 0)
print(result)
"""
print("\n=== Code Reviewer ===")
print(f"With system:\n{call_with_system(code, CODE_REVIEW_SYSTEM)}")

# Example 3: JSON extractor
JSON_SYSTEM = """You are a data extraction API.
Extract the requested fields from the input and return valid JSON only.
No markdown, no explanation, no wrapper text."""

text = "Please contact John Smith at john.smith@company.com or call +1-555-0123."
print("\n=== JSON Extractor ===")
print(f"With system: {call_with_system('Extract name, email, phone from: ' + text, JSON_SYSTEM)}")
```

**Expected Token Savings:** 10–20% — removes default verbose caveats from every response
**Environment:** `pip install anthropic`

---

### Option 2 — System Prompt Registry with Validation

Maintain a validated registry of system prompts per task type. Enforce that every API call specifies a registered prompt — no bare calls allowed.

```python
import anthropic
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class SystemPromptSpec:
    name: str
    prompt: str
    max_tokens: int
    model: str = "claude-sonnet-4-6"
    description: str = ""

    def __post_init__(self):
        if not self.prompt.strip():
            raise ValueError(f"System prompt '{self.name}' is empty")
        if len(self.prompt) < 20:
            raise ValueError(f"System prompt '{self.name}' is too short (< 20 chars)")

class PromptRegistry:
    def __init__(self):
        self._registry: dict[str, SystemPromptSpec] = {}

    def register(self, spec: SystemPromptSpec) -> "PromptRegistry":
        self._registry[spec.name] = spec
        return self

    def get(self, name: str) -> SystemPromptSpec:
        if name not in self._registry:
            available = list(self._registry.keys())
            raise KeyError(f"Unknown prompt '{name}'. Available: {available}")
        return self._registry[name]

    def call(self, prompt_name: str, user_message: str, **override_kwargs) -> str:
        spec = self.get(prompt_name)
        kwargs = {
            "model": spec.model,
            "max_tokens": spec.max_tokens,
            "system": spec.prompt,
            "messages": [{"role": "user", "content": user_message}],
            **override_kwargs,
        }
        response = client.messages.create(**kwargs)
        return response.content[0].text

    def list_prompts(self) -> list[dict]:
        return [
            {"name": s.name, "description": s.description, "max_tokens": s.max_tokens}
            for s in self._registry.values()
        ]

# Build the registry at startup — fails fast on bad prompts
registry = PromptRegistry()

registry.register(SystemPromptSpec(
    name="classifier",
    prompt="You are a text classifier. Return only the category label — no explanation.",
    max_tokens=32,
    model="claude-haiku-4-5-20251001",
    description="Single-label text classification",
))

registry.register(SystemPromptSpec(
    name="summariser",
    prompt=(
        "You are a document summariser. "
        "Output a bullet-point summary with 3-5 points. "
        "Each bullet starts with '•'. No preamble or conclusion."
    ),
    max_tokens=512,
    description="Document summarisation to bullet points",
))

registry.register(SystemPromptSpec(
    name="sql_generator",
    prompt=(
        "You are a SQL generator for PostgreSQL. "
        "Output only the SQL query — no explanation, no markdown fences. "
        "Use lowercase keywords. Always include a LIMIT clause."
    ),
    max_tokens=256,
    description="Natural language to PostgreSQL",
))

registry.register(SystemPromptSpec(
    name="support_agent",
    prompt=(
        "You are a helpful customer support agent for TechCorp. "
        "Be empathetic and concise. Offer specific next steps. "
        "Never mention competitors. Escalate billing issues to billing@techcorp.com."
    ),
    max_tokens=1024,
    description="Customer support responses",
))

# Usage — always specifies a registered prompt
print("Available prompts:", registry.list_prompts())
print()

print("Classifier:", registry.call("classifier", "I love this product!"))
print("Summariser:", registry.call("summariser",
    "The quarterly results showed strong growth in APAC while EU markets softened due to regulatory headwinds. "
    "R&D investment increased 23% YoY. The board approved a $50M buyback program."))
print("SQL:", registry.call("sql_generator",
    "Get the top 5 customers by revenue in the last 30 days"))

# Trying an unregistered prompt raises immediately
try:
    registry.call("unknown_task", "Hello")
except KeyError as e:
    print(f"\nBlocked unregistered prompt: {e}")
```

**Expected Token Savings:** 10–30% — right-sized max_tokens and haiku for simple tasks
**Environment:** `pip install anthropic`

---

### Option 3 — Dynamic System Prompt Construction from Components

Build system prompts from reusable components — role, format, constraints, domain context. Mix and match without duplicating prompt text.

```python
import anthropic
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class PromptComponent:
    content: str
    priority: int = 0   # Higher priority components appear first

@dataclass
class DynamicSystemPrompt:
    _components: list[PromptComponent] = field(default_factory=list)

    def add_role(self, role: str) -> "DynamicSystemPrompt":
        self._components.append(PromptComponent(f"You are {role}.", priority=100))
        return self

    def add_output_format(self, format_spec: str) -> "DynamicSystemPrompt":
        self._components.append(PromptComponent(f"Output format: {format_spec}", priority=90))
        return self

    def add_constraint(self, constraint: str) -> "DynamicSystemPrompt":
        self._components.append(PromptComponent(constraint, priority=80))
        return self

    def add_domain_context(self, context: str) -> "DynamicSystemPrompt":
        self._components.append(PromptComponent(context, priority=70))
        return self

    def add_examples(self, examples: str) -> "DynamicSystemPrompt":
        self._components.append(PromptComponent(f"Examples:\n{examples}", priority=60))
        return self

    def build(self) -> str:
        if not self._components:
            raise ValueError("System prompt has no components — at minimum add_role() is required")
        sorted_comps = sorted(self._components, key=lambda c: c.priority, reverse=True)
        return "\n".join(c.content for c in sorted_comps)

def create(system_prompt: DynamicSystemPrompt, user_message: str,
           model: str = "claude-sonnet-4-6", max_tokens: int = 512) -> str:
    system = system_prompt.build()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

# Shared components
BASE_CONSTRAINTS = [
    "Never fabricate information you are not confident about.",
    "Be concise — omit filler words and unnecessary preamble.",
]

# Assemble different agent configurations from shared components
email_agent = (
    DynamicSystemPrompt()
    .add_role("a professional business email writer")
    .add_output_format("Return only the email body — no subject line, no explanations.")
    .add_constraint(BASE_CONSTRAINTS[1])
    .add_constraint("Use formal but friendly language.")
)

qa_agent = (
    DynamicSystemPrompt()
    .add_role("a factual question-answering assistant")
    .add_output_format("Answer in 1-3 sentences. If unsure, say so explicitly.")
    .add_constraint(BASE_CONSTRAINTS[0])
)

api_agent = (
    DynamicSystemPrompt()
    .add_role("a REST API response generator")
    .add_output_format("Return only valid JSON. No markdown, no explanations.")
    .add_constraint("All string values must be valid UTF-8.")
    .add_domain_context("API version: v2. Date format: ISO 8601.")
)

print("=== Email Agent ===")
print(create(email_agent, "Write an apology email to a client about a delayed shipment."))

print("\n=== QA Agent ===")
print(create(qa_agent, "What is the difference between TCP and UDP?"))

print("\n=== API Agent ===")
print(create(api_agent, "Return a user object with id=42, name='Alice', role='admin'.", max_tokens=128))

# Print assembled system prompts for debugging
print("\n=== Assembled System Prompts ===")
for name, agent in [("email", email_agent), ("qa", qa_agent), ("api", api_agent)]:
    print(f"\n[{name}]\n{agent.build()}")
```

**Expected Token Savings:** 5–15% — reusable components prevent bloated copy-pasted prompts
**Environment:** `pip install anthropic`

---

### Option 4 — System Prompt with Cached Static Context

For agents with large domain context (product catalogues, policy documents, knowledge bases), put static content in the system prompt with cache_control. Dynamic task instructions go in the user turn.

```python
import anthropic

client = anthropic.Anthropic()

# Large static knowledge base (simulated — in production this could be 50K+ tokens)
PRODUCT_CATALOGUE = """
## Product Catalogue

### Electronics
- SKU-E001: Wireless Headphones Pro ($349) — noise cancellation, 30hr battery
- SKU-E002: USB-C Hub 7-in-1 ($79) — 4K HDMI, 100W PD, SD card
- SKU-E003: Mechanical Keyboard TKL ($199) — Cherry MX Brown, RGB

### Software
- LIC-S001: Analytics Suite Annual ($599/user) — dashboards, exports, API access
- LIC-S002: Team Collaboration ($15/user/month) — unlimited users, SSO

### Support Tiers
- Standard: email only, 48hr SLA
- Professional: email + chat, 8hr SLA
- Enterprise: 24/7 phone, 1hr SLA, dedicated CSM

### Return Policy
- Electronics: 30 days unopened, 15 days opened
- Software licenses: non-refundable after activation
- Damaged items: replace within 7 days of receipt
"""

COMPANY_POLICIES = """
## Company Policies

### Pricing Rules
- Discounts require manager approval for >20%
- Government/education: 25% standard discount
- Bundles: electronics + software = 15% off software

### Escalation
- Billing disputes > $500: escalate to finance@company.com
- Technical issues unresolved 48hr: escalate to engineering@company.com
- Legal/compliance: legal@company.com only
"""

def build_support_system(agent_name: str = "Support Agent") -> list[dict]:
    """
    Build a system prompt with cached static sections.
    Static sections are cached — tokens charged once, reused across turns.
    """
    return [
        {
            "type": "text",
            "text": f"You are {agent_name} for TechCorp. Be professional, empathetic, and solution-focused. "
                    f"Use the product catalogue and policies below to answer accurately.",
        },
        {
            "type": "text",
            "text": PRODUCT_CATALOGUE + COMPANY_POLICIES,
            "cache_control": {"type": "ephemeral"},  # Cached — not re-billed each turn
        },
    ]

def support_call(user_message: str, conversation_history: list[dict] = None) -> tuple[str, list[dict]]:
    history = conversation_history or []
    history.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=build_support_system(),
        messages=history,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})

    # Show cache usage
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_created = getattr(usage, "cache_creation_input_tokens", 0)
    if cache_read:
        print(f"[Cache] {cache_read} tokens read from cache (saved ${cache_read * 0.000003:.4f})")
    if cache_created:
        print(f"[Cache] {cache_created} tokens cached for future use")

    return reply, history

# Multi-turn support conversation
history = []
questions = [
    "What headphones do you sell and what's the price?",
    "Do you offer discounts for universities?",
    "I bought the headphones 20 days ago unopened — can I return them?",
]

for q in questions:
    print(f"\nUser: {q}")
    reply, history = support_call(q, history)
    print(f"Agent: {reply[:150]}...")
```

**Expected Token Savings:** 85–95% on cached context after first call — large knowledge bases cost once
**Environment:** `pip install anthropic`

---

### Option 5 — Environment-Aware System Prompt Switcher

Use different system prompts for dev/staging/production. Dev prompts are verbose with debug info; prod prompts are lean and focused.

```python
import anthropic
import os
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class EnvironmentPrompt:
    prod: str
    dev: str
    staging: Optional[str] = None

    def for_env(self, env: str) -> str:
        env = env.lower()
        if env == "production":
            return self.prod
        if env == "staging":
            return self.staging or self.prod
        return self.dev  # development / test / default

PROMPTS = {
    "data_analyst": EnvironmentPrompt(
        prod=(
            "You are a data analysis assistant. "
            "Answer questions about data concisely and accurately. "
            "Return numbers with 2 decimal places. Use tables for comparisons."
        ),
        dev=(
            "You are a data analysis assistant (DEVELOPMENT MODE). "
            "Answer questions about data concisely and accurately. "
            "Return numbers with 2 decimal places. Use tables for comparisons.\n\n"
            "DEBUG: Explain your reasoning step-by-step before answering. "
            "Flag any assumptions you make with [ASSUMPTION]. "
            "Note confidence level: HIGH/MEDIUM/LOW."
        ),
    ),
    "code_assistant": EnvironmentPrompt(
        prod=(
            "You are a Python code assistant. "
            "Output only runnable Python code. No explanations unless asked. "
            "Include type hints. Prefer stdlib over third-party libraries."
        ),
        dev=(
            "You are a Python code assistant (DEVELOPMENT MODE). "
            "Output runnable Python code with inline comments explaining non-obvious decisions. "
            "Include type hints. Flag potential issues with # FIXME or # NOTE. "
            "Prefer stdlib. Show alternative approaches if relevant."
        ),
    ),
}

def env_aware_call(
    task: str,
    user_message: str,
    max_tokens: int = 512,
    env: str = None,
) -> str:
    env = env or os.environ.get("AGENT_ENV", "development")
    prompt_spec = PROMPTS.get(task)

    if not prompt_spec:
        raise ValueError(f"Unknown task '{task}'. Available: {list(PROMPTS.keys())}")

    system = prompt_spec.for_env(env)
    print(f"[Env:{env}] Using {'verbose dev' if env != 'production' else 'lean prod'} prompt for '{task}'")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

# Same question, different environments
question = "What's the average of 84, 91, 76, and 88?"

print("=== Production ===")
print(env_aware_call("data_analyst", question, env="production"))

print("\n=== Development (verbose) ===")
print(env_aware_call("data_analyst", question, env="development"))
```

**Expected Token Savings:** 20–40% in production — dev prompts are intentionally verbose; prod prompts are lean
**Environment:** `pip install anthropic`

---

### Option 6 — System Prompt Linter and Coverage Checker

At startup, lint system prompts for common missing elements: no role defined, no output format, no constraint on uncertainty. Warn developers before the prompt reaches production.

```python
import re
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class LintResult:
    prompt_name: str
    warnings: list[str]
    errors: list[str]
    score: int   # 0-100

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

ROLE_INDICATORS   = ["you are", "your role", "act as", "serve as"]
FORMAT_INDICATORS = ["output", "format", "return", "respond with", "reply with", "structure"]
UNCERTAINTY_INDICATORS = ["unsure", "uncertain", "don't know", "cannot confirm", "if unsure"]
VERBOSITY_INDICATORS = ["concise", "brief", "short", "one sentence", "do not explain", "no preamble"]

def lint_system_prompt(name: str, prompt: str) -> LintResult:
    errors = []
    warnings = []
    score = 100
    p = prompt.lower()

    # Required: role definition
    if not any(ind in p for ind in ROLE_INDICATORS):
        errors.append("No role defined. Add 'You are [role].' at the start.")
        score -= 30

    # Recommended: output format specification
    if not any(ind in p for ind in FORMAT_INDICATORS):
        warnings.append("No output format specified. Model will choose format unpredictably.")
        score -= 15

    # Recommended: uncertainty handling
    if not any(ind in p for ind in UNCERTAINTY_INDICATORS):
        warnings.append("No uncertainty policy. Model may confabulate when unsure.")
        score -= 10

    # Check: prompt is not too short
    if len(prompt.split()) < 10:
        errors.append(f"Prompt too short ({len(prompt.split())} words). Minimum 10 words recommended.")
        score -= 25

    # Check: prompt is not uselessly vague
    vague_phrases = ["be helpful", "help the user", "answer questions"]
    if all(phrase in p for phrase in ["be helpful"]) and len(prompt.split()) < 20:
        warnings.append("Prompt may be too vague. Add specific domain or task constraints.")
        score -= 10

    # Check verbosity control
    if not any(ind in p for ind in VERBOSITY_INDICATORS):
        warnings.append("No verbosity control. Model may give overly long responses.")
        score -= 5

    # Check for placeholder text
    if re.search(r"\[YOUR.*?\]|\{YOUR.*?\}", prompt, re.IGNORECASE):
        errors.append("Prompt contains unfilled placeholder text like [YOUR_ROLE].")
        score -= 20

    score = max(0, score)
    return LintResult(name, warnings, errors, score)

def lint_and_create(
    prompt_name: str,
    prompt: str,
    user_message: str,
    block_on_error: bool = True,
    max_tokens: int = 512,
) -> str:
    result = lint_system_prompt(prompt_name, prompt)

    if result.errors or result.warnings:
        print(f"[Lint:{prompt_name}] Score={result.score}/100")
    for err in result.errors:
        print(f"  ERROR: {err}")
    for warn in result.warnings:
        print(f"  WARN:  {warn}")

    if result.errors and block_on_error:
        raise ValueError(f"System prompt '{prompt_name}' failed linting. Fix errors before deploying.")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

# Test various prompt quality levels
prompts = [
    ("empty",    ""),
    ("vague",    "Be helpful and answer the user's questions."),
    ("ok",       "You are a helpful assistant. Answer concisely. If unsure, say so."),
    ("placeholder", "You are [YOUR_ROLE]. Help users with [DOMAIN]. Output format: [FORMAT]."),
    ("excellent", (
        "You are a technical documentation writer. "
        "Output clear, structured explanations in plain English. "
        "Use bullet points for lists. If unsure about a technical detail, say 'I am not certain, but...' "
        "Be concise — avoid filler words and unnecessary preamble."
    )),
]

for name, prompt in prompts:
    print(f"\n--- {name} ---")
    if not prompt:
        result = lint_system_prompt(name, prompt)
        print(f"Score: {result.score} | Errors: {result.errors}")
        continue
    try:
        reply = lint_and_create(name, prompt, "What is recursion?", block_on_error=(name in ("empty", "placeholder")))
        print(f"Reply: {reply[:80]}...")
    except ValueError as e:
        print(f"Blocked: {e}")
```

**Expected Token Savings:** None — pre-deployment quality gate; prevents poor prompts from reaching production
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Approach | Enforcement | Best For |
|--------|----------|-------------|----------|
| Minimal Task Prompt | Inline system string | Manual | Quick fix — any existing agent |
| Prompt Registry | Validated named prompts | At registration | Teams managing multiple agents |
| Component Builder | Composable parts | At build time | Complex agents with shared constraints |
| Cached Static Context | Prompt caching | At call time | Agents with large knowledge bases |
| Environment-Aware | Dev/prod variants | Via ENV variable | Teams needing debug-friendly dev mode |
| Prompt Linter | Automated quality check | At startup/CI | CI/CD pipelines, code review |

**Recommended starting point:** Option 1 (Minimal Task-Specific System Prompt) — add a 3-sentence system prompt to every bare `messages.create()` call. Specify: role, output format, and what to do when uncertain. This single change eliminates 80% of default-behavior quality issues immediately.
