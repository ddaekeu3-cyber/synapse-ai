---
layout: solution
title: "Agent confuses same-named technical concepts"
category: hallucination
description: "Agent conflates technical terms that share a name but mean different things in different contexts: 'context' in LLMs vs React vs databases, 'hook' in React vs Git vs fishing APIs, 'model' in ML vs MVC vs database schema. Wrong explanations propagate silently."
tags: [hallucination, disambiguation, context, terminology, prompt-engineering, domain]
---

## Symptom

A developer asks "how do I update the context?" and the agent explains React Context API when they meant the LLM conversation context. Or "how do I add a hook?" gets an explanation of React hooks when the codebase uses Git hooks. The answer is technically correct for the wrong interpretation and the user has to clarify — burning a turn and getting confused.

## Root Cause

Overloaded technical terms are extremely common: "context", "model", "hook", "schema", "middleware", "provider", "reducer", "state", "dispatch", "pipeline". The model assigns a meaning based on weak cues from nearby words. Without an explicit domain anchor in the system prompt or conversation, it defaults to the most statistically common usage in its training data — which may not match the user's context.

## Fix

Anchor the domain in the system prompt. For multi-domain products, inject the active domain, framework, and technology stack so the model interprets ambiguous terms correctly.

---

### Option 1 — Domain anchor in system prompt

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Domain-specific system prompts that anchor ambiguous terms
DOMAIN_SYSTEMS: dict[str, str] = {
    "llm_agent": (
        "You are an assistant helping build LLM-based agents with the Anthropic Python SDK. "
        "In this context:\n"
        "- 'context' means the conversation history / messages list sent to the API\n"
        "- 'model' means an Anthropic model (claude-sonnet-4-6, claude-haiku-4-5, etc.)\n"
        "- 'token' means a subword unit that models process, not an auth token\n"
        "- 'prompt' means the input text sent to the model\n"
        "- 'hook' means a callback/middleware function, not a React hook\n"
        "Always interpret these terms in the LLM agent context unless explicitly stated otherwise."
    ),
    "react_frontend": (
        "You are an assistant helping build React applications. "
        "In this context:\n"
        "- 'context' means React Context (createContext, useContext, Provider)\n"
        "- 'hook' means a React hook (useState, useEffect, custom hooks)\n"
        "- 'state' means React component state or global state (Redux, Zustand)\n"
        "- 'model' means a TypeScript type/interface, not an ML model\n"
        "- 'reducer' means a Redux/useReducer function\n"
        "Always interpret these terms in the React context unless explicitly stated otherwise."
    ),
    "django_backend": (
        "You are an assistant helping build Django applications. "
        "In this context:\n"
        "- 'model' means a Django ORM model (database table definition)\n"
        "- 'view' means a Django view function or class-based view\n"
        "- 'context' means the template context dictionary passed to render()\n"
        "- 'middleware' means Django middleware (MIDDLEWARE setting)\n"
        "- 'signal' means a Django signal (post_save, pre_delete, etc.)\n"
        "Always interpret these terms in the Django context unless explicitly stated otherwise."
    ),
}


def run_agent(user_message: str, domain: str = "llm_agent") -> str:
    system = DOMAIN_SYSTEMS.get(domain, "You are a helpful technical assistant.")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Same question, different domain anchors
question = "How do I pass context to my handler?"
print("=== LLM Agent context ===")
print(run_agent(question, domain="llm_agent"))
print("\n=== React context ===")
print(run_agent(question, domain="react_frontend"))
print("\n=== Django context ===")
print(run_agent(question, domain="django_backend"))
```

**Expected Token Savings:** Domain anchor adds ~80–150 tokens; prevents 1–2 clarification turns (each ~300–800 tokens) caused by wrong-domain answers.
**Environment:** Any domain-specific agent; inject the anchor once at session start and all subsequent questions benefit.

---

### Option 2 — Technology stack injection from project config

```python
import anthropic
from pathlib import Path
import json

client = anthropic.Anthropic(api_key="sk-live-...")

TERM_DISAMBIGUATION: dict[str, dict[str, str]] = {
    "context": {
        "react": "React Context API (createContext/useContext)",
        "django": "template context dict passed to render()",
        "anthropic": "conversation messages list (LLM context window)",
        "express": "Express.js request context object",
    },
    "model": {
        "django": "Django ORM model class (maps to a database table)",
        "react": "TypeScript type/interface representing data shape",
        "anthropic": "Anthropic language model (e.g. claude-sonnet-4-6)",
        "sqlalchemy": "SQLAlchemy model class",
    },
    "hook": {
        "react": "React hook function (must start with 'use')",
        "git": "Git lifecycle hook script (pre-commit, post-merge, etc.)",
        "django": "Django signal handler or middleware hook",
        "anthropic": "pre/post tool call interceptor",
    },
    "middleware": {
        "django": "Django MIDDLEWARE class processing requests/responses",
        "express": "Express.js middleware function (req, res, next)",
        "redux": "Redux middleware (thunk, saga, logger)",
    },
}


def detect_stack_from_files() -> list[str]:
    """Auto-detect the technology stack from project files."""
    stack = []
    checks = [
        ("package.json", "react", lambda c: "react" in c.lower()),
        ("requirements.txt", "django", lambda c: "django" in c.lower()),
        ("requirements.txt", "anthropic", lambda c: "anthropic" in c.lower()),
        ("package.json", "anthropic", lambda c: '"@anthropic-ai' in c or '"anthropic"' in c),
    ]
    for filename, tech, predicate in checks:
        p = Path(filename)
        if p.exists():
            try:
                if predicate(p.read_text()):
                    stack.append(tech)
            except Exception:
                pass
    return stack


def build_disambiguation_prompt(stack: list[str]) -> str:
    if not stack:
        return ""
    lines = ["Term disambiguation for this project's stack:"]
    for term, contexts in TERM_DISAMBIGUATION.items():
        for tech in stack:
            if tech in contexts:
                lines.append(f"  '{term}' = {contexts[tech]}")
                break
    return "\n".join(lines)


def run_agent(user_message: str, stack: list[str] | None = None) -> str:
    stack = stack or detect_stack_from_files() or ["anthropic"]
    disambiguation = build_disambiguation_prompt(stack)
    system = f"You are a coding assistant for a {', '.join(stack)} project.\n\n{disambiguation}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Auto-detection means no manual configuration; disambiguation block adds ~100 tokens but precisely eliminates the most common wrong-domain interpretations.
**Environment:** IDE assistant or developer tool where the project files are accessible; stack detection runs once at session start.

---

### Option 3 — Ambiguity detection with clarification request

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

# Overloaded terms and their possible domains
AMBIGUOUS_TERMS: dict[str, list[str]] = {
    "context": ["React Context", "LLM context window", "Django template context", "database connection context"],
    "hook": ["React hook", "Git hook", "webhook", "lifecycle hook"],
    "model": ["database model", "ML model", "MVC model", "TypeScript type"],
    "middleware": ["Express middleware", "Django middleware", "Redux middleware"],
    "reducer": ["Redux reducer", "React useReducer", "MapReduce reducer"],
    "provider": ["React Context Provider", "dependency injection provider", "OAuth provider"],
    "pipeline": ["ML pipeline", "CI/CD pipeline", "data pipeline", "processing pipeline"],
}


def detect_ambiguous_terms(message: str) -> list[tuple[str, list[str]]]:
    """Return list of (term, possible_meanings) for ambiguous terms in the message."""
    found = []
    lower = message.lower()
    for term, meanings in AMBIGUOUS_TERMS.items():
        if re.search(rf"\b{term}\b", lower):
            found.append((term, meanings))
    return found


CLASSIFIER_SYSTEM = (
    "Given a user message and a list of ambiguous terms with their possible meanings, "
    "determine if the question is clear enough to answer correctly OR if clarification is needed. "
    "If context in the message resolves the ambiguity (e.g., code snippets, imports, filenames), "
    "reply: CLEAR:<which meaning>\n"
    "If genuinely ambiguous, reply: AMBIGUOUS:<which terms need clarification>"
)


def resolve_ambiguity(user_message: str, ambiguous: list[tuple[str, list[str]]]) -> tuple[bool, str]:
    """Returns (is_clear, resolution_note)."""
    terms_desc = "; ".join(
        f"'{t}' could mean: {', '.join(m)}" for t, m in ambiguous
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": f"Message: {user_message}\nAmbiguous terms: {terms_desc}"}],
    )
    result = response.content[0].text.strip()
    if result.startswith("CLEAR:"):
        return True, result[6:].strip()
    return False, result[10:].strip() if result.startswith("AMBIGUOUS:") else result


def run_agent(user_message: str) -> str:
    ambiguous = detect_ambiguous_terms(user_message)

    if ambiguous:
        is_clear, note = resolve_ambiguity(user_message, ambiguous)
        if not is_clear:
            term_list = ", ".join(f"'{t}'" for t, _ in ambiguous)
            return (
                f"To answer correctly, I need to clarify what you mean by {term_list}. "
                f"Are you working with: {note}?"
            )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Haiku disambiguation costs ~50 tokens; prevents one full wrong-answer turn (~500 tokens) plus one correction turn.
**Environment:** General-purpose assistants serving developers across multiple stacks; ask once and avoid the wrong answer entirely.

---

### Option 4 — Few-shot examples with domain-correct terminology

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Few-shot examples that demonstrate correct term interpretation for this domain
LLM_AGENT_EXAMPLES = """
Q: How do I add context to my requests?
A: To add context in an LLM agent, extend the messages list before calling client.messages.create():
```python
messages.append({"role": "user", "content": "Additional context: ..."})
```

Q: What's the best model to use?
A: For most tasks, use claude-sonnet-4-6. For quick/cheap tasks, use claude-haiku-4-5-20251001.
   For complex reasoning, use claude-opus-4-6.

Q: How do I add a hook?
A: To add a hook (callback) in an Anthropic agent, intercept the API call:
```python
def pre_call_hook(messages, **kwargs):
    # modify messages or kwargs before the API call
    return messages, kwargs
```
"""


def run_agent_with_examples(user_message: str) -> str:
    system = (
        "You are an expert in building LLM agents with the Anthropic SDK. "
        "Use the terminology and concepts from these examples as your reference:\n\n"
        f"{LLM_AGENT_EXAMPLES}\n\n"
        "Match the technical domain from these examples when interpreting questions."
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Examples add ~200–400 tokens but anchor the model to the correct domain interpretation across all subsequent turns.
**Environment:** Domain-specific assistants with a narrow technical scope; few-shot examples are particularly effective for jargon-heavy domains.

---

### Option 5 — Glossary injection: define ambiguous terms upfront

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class DomainGlossary:
    domain_name: str
    terms: dict[str, str]   # term → definition in this domain

    def to_prompt_block(self) -> str:
        lines = [f"## Glossary for {self.domain_name}:"]
        for term, definition in self.terms.items():
            lines.append(f"  {term}: {definition}")
        return "\n".join(lines)


ANTHROPIC_SDK_GLOSSARY = DomainGlossary(
    domain_name="Anthropic Python SDK",
    terms={
        "context": "The messages list representing conversation history, passed as the 'messages' parameter",
        "model": "An Anthropic language model identifier string (e.g. 'claude-sonnet-4-6')",
        "token": "A subword unit; models process ~4 chars per token on average",
        "prompt": "The full input to the model, including system prompt and messages",
        "completion": "The model's generated response text",
        "tool": "A function the model can invoke, defined in the 'tools' parameter",
        "tool_use": "A response block indicating the model wants to call a tool",
        "tool_result": "A message containing the output of a tool call",
        "streaming": "Receiving response tokens incrementally via the stream API",
        "usage": "The input_tokens and output_tokens fields in the API response",
        "cache": "Anthropic's prompt caching feature for reusing processed tokens",
    },
)


def run_agent(user_message: str, glossary: DomainGlossary = ANTHROPIC_SDK_GLOSSARY) -> str:
    system = (
        f"You are a coding assistant specializing in {glossary.domain_name}.\n\n"
        f"{glossary.to_prompt_block()}\n\n"
        "Use these definitions when interpreting technical terms in questions."
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Glossary adds ~200 tokens; prevents every overloaded-term question from going to the wrong domain.
**Environment:** Narrow-domain assistants (SDK docs bot, framework helper); the glossary is authored once and reused across all sessions.

---

### Option 6 — Active clarification: detect and ask before answering

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

OVERLOADED_TERMS = {
    "context", "model", "hook", "middleware", "provider",
    "reducer", "state", "schema", "pipeline", "agent", "session",
}

DISAMBIGUATION_SYSTEM = (
    "You are a technical assistant. Before answering questions that use overloaded terms, "
    "check if the meaning is clear from the question context (code snippets, imports, "
    "file names, framework names). "
    "If the meaning is clear, answer directly. "
    "If genuinely ambiguous, ask ONE clarifying question about the specific overloaded term. "
    "Never ask multiple clarifying questions at once."
)


def run_agent_with_active_disambiguation(user_message: str) -> str:
    # Check if message contains overloaded terms
    lower = user_message.lower()
    has_overloaded = any(f" {term} " in f" {lower} " or lower.startswith(term) for term in OVERLOADED_TERMS)

    if not has_overloaded:
        # No ambiguity risk — answer directly
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text

    # Potentially ambiguous — use the disambiguation system
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=DISAMBIGUATION_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Comparison table
# | Option | Disambiguation Method | Requires Config | Extra Cost |
# |--------|----------------------|----------------|------------|
# | 1 Domain anchor | Static system prompt | Manual | ~100 tok |
# | 2 Stack injection | File detection | None | ~100 tok |
# | 3 Ambiguity detector | Haiku classifier | None | ~50 tok |
# | 4 Few-shot examples | Example-based | Manual | ~300 tok |
# | 5 Glossary injection | Explicit glossary | Manual | ~200 tok |
# | 6 Active clarification | Model judgment | None | ~0 or 1 turn |
```

**Expected Token Savings:** Active clarification adds zero tokens when the model infers the domain from context; costs one short clarification question only when genuinely ambiguous; prevents a full wrong-domain answer.
**Environment:** General-purpose technical assistants without a fixed domain; the model's judgment handles the long tail of ambiguous queries without a manually maintained glossary.
