---
layout: solution
title: "Agent Doesn't Implement Dynamic System Prompt Templates"
category: prompt-engineering
description: "Agents with hardcoded system prompts cannot adapt to different users, roles, or task contexts without code changes. Dynamic system prompt templates inject runtime variables — user name, role, current date, task-specific rules — producing context-aware prompts without duplicating prompt logic."
tags: [prompt-engineering, system-prompt, templates, dynamic, jinja2, personalization, multi-tenant]
---

## Problem

A single hardcoded system prompt fails users with different roles (admin vs. read-only), different domains (legal vs. medical), different tenants (different company names and policies), and different task contexts (coding vs. customer support). Maintaining separate prompt files for each variant creates duplication and drift. Dynamic templates compose a single prompt from reusable blocks, injecting runtime variables to produce the right system prompt for each context.

## Solutions

### Option 1: f-String Template with Context Dict

```python
import anthropic
from dataclasses import dataclass
from datetime import date

client = anthropic.Anthropic()

@dataclass
class AgentContext:
    user_name: str
    user_role: str  # "admin" | "viewer" | "editor"
    tenant_name: str
    domain: str     # "legal" | "medical" | "general"
    current_date: str = ""

    def __post_init__(self):
        if not self.current_date:
            self.current_date = date.today().isoformat()

ROLE_PERMISSIONS = {
    "admin": "You can read, write, and delete any data.",
    "editor": "You can read and write data, but cannot delete.",
    "viewer": "You can only read data. Refuse any modification requests.",
}

DOMAIN_GUIDELINES = {
    "legal": "Always add 'This is not legal advice.' to any legal interpretation.",
    "medical": "Always recommend consulting a licensed physician. Never diagnose.",
    "general": "Answer helpfully and concisely.",
}

def build_system_prompt(ctx: AgentContext) -> str:
    role_rules = ROLE_PERMISSIONS.get(ctx.user_role, ROLE_PERMISSIONS["viewer"])
    domain_rules = DOMAIN_GUIDELINES.get(ctx.domain, DOMAIN_GUIDELINES["general"])
    return f"""You are an AI assistant for {ctx.tenant_name}.

User: {ctx.user_name} (Role: {ctx.user_role})
Date: {ctx.current_date}
Domain: {ctx.domain}

Permissions:
{role_rules}

Domain guidelines:
{domain_rules}

Always address the user by their first name. Stay within your assigned role permissions."""

def chat(ctx: AgentContext, user_input: str) -> str:
    system = build_system_prompt(ctx)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_input}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    admin_ctx = AgentContext("Alice", "admin", "Acme Corp", "general")
    viewer_ctx = AgentContext("Bob", "viewer", "Acme Corp", "legal")

    print("=== Admin context ===")
    print(chat(admin_ctx, "Please delete the old report files."))

    print("\n=== Viewer + legal context ===")
    print(chat(viewer_ctx, "Can you interpret this contract clause for me?"))

# Expected Token Savings: single template vs N separate prompts; context dict adds ~50-100 tokens per call
# Environment: multi-tenant SaaS agents; role and domain injection prevents over-privileged responses
```

### Option 2: Block-Composition Template with Conditional Sections

```python
import anthropic
from typing import Any

client = anthropic.Anthropic()

# Prompt blocks — reusable fragments
BLOCKS = {
    "base_identity": "You are a helpful AI assistant named {agent_name} for {company}.",
    "date_awareness": "Today is {date}. Use this for any time-relative reasoning.",
    "tone_formal": "Maintain a formal, professional tone at all times.",
    "tone_casual": "Use a friendly, conversational tone.",
    "safety_medical": "IMPORTANT: Never diagnose or prescribe. Always recommend a licensed physician.",
    "safety_legal": "IMPORTANT: This is not legal advice. Recommend consulting a qualified lawyer.",
    "lang_instruction": "Always respond in {language}.",
    "output_format": "Format all responses as: 1) Direct answer, 2) Explanation, 3) Next steps.",
    "tool_restriction": "You have access to tools: {allowed_tools}. Do not reference other tools.",
}

def compose_prompt(
    base: dict[str, Any],
    include_blocks: list[str],
    **extra_vars,
) -> str:
    """
    Compose a system prompt from named blocks + variables.
    Blocks are filled with merged variables from base + extra_vars.
    """
    variables = {**base, **extra_vars}
    sections = []
    for block_name in include_blocks:
        template = BLOCKS.get(block_name, "")
        if template:
            try:
                sections.append(template.format(**variables))
            except KeyError as e:
                sections.append(f"[Block '{block_name}' missing variable: {e}]")
    return "\n\n".join(sections)

def chat(system: str, user_input: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_input}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    from datetime import date

    base_vars = {
        "agent_name": "HelperBot",
        "company": "MedCare Inc.",
        "date": date.today().isoformat(),
    }

    # Medical support agent
    medical_system = compose_prompt(
        base_vars,
        include_blocks=["base_identity", "date_awareness", "tone_formal", "safety_medical"],
    )
    print("=== Medical agent ===")
    print(chat(medical_system, "Do I have diabetes if my fasting glucose is 110 mg/dL?"))

    # Multilingual casual agent
    casual_system = compose_prompt(
        base_vars,
        include_blocks=["base_identity", "tone_casual", "lang_instruction"],
        language="Spanish",
    )
    print("\n=== Spanish casual agent ===")
    print(chat(casual_system, "Tell me a fun fact."))

# Expected Token Savings: blocks average 15-30 tokens each; compose only needed blocks vs 500-token monolith
# Environment: agents with many configuration axes; block composition avoids combinatorial prompt duplication
```

### Option 3: Jinja2 Template Engine with Inheritance

```python
import anthropic
from datetime import date

try:
    from jinja2 import Environment, BaseLoader
    HAS_JINJA2 = True
except ImportError:
    HAS_JINJA2 = False
    print("Install jinja2: pip install jinja2")

client = anthropic.Anthropic()

# Base template (would normally be in a .j2 file)
BASE_TEMPLATE = """You are {{ agent_name }}, an AI assistant for {{ company }}.
{% if current_date %}Today's date: {{ current_date }}.{% endif %}

{% block identity %}{% endblock %}
{% block permissions %}{% endblock %}
{% block guidelines %}
Respond clearly and helpfully.
{% endblock %}
{% block format %}{% endblock %}"""

SUPPORT_TEMPLATE = """{% extends "base" %}
{% block identity %}
You are a customer support specialist. Your goal is to resolve issues efficiently.
{% endblock %}
{% block permissions %}
You can look up orders, issue refunds up to ${{ max_refund }}, and escalate to humans.
{% endblock %}
{% block guidelines %}
- Acknowledge frustration before solving
- Offer solutions before asking questions
- Always end with "Is there anything else I can help you with?"
{% endblock %}
{% block format %}
Response format: Empathy → Solution → Confirmation
{% endblock %}"""

def render_template(template_str: str, **variables) -> str:
    if not HAS_JINJA2:
        # Fallback to simple f-string rendering
        result = template_str
        for k, v in variables.items():
            result = result.replace("{{ " + k + " }}", str(v))
        return result

    env = Environment(loader=BaseLoader())
    # Register base template
    env.parse(BASE_TEMPLATE)
    template = env.from_string(template_str)
    return template.render(**variables)

def chat(template_str: str, variables: dict, user_input: str) -> str:
    system = render_template(template_str, **variables)
    # Clean up template artifacts for non-jinja2 fallback
    import re
    system = re.sub(r"\{%.*?%\}", "", system).strip()
    system = re.sub(r"\n{3,}", "\n\n", system)

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_input}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    # Use the flattened support template (no inheritance for fallback compatibility)
    FLAT_SUPPORT = """You are {{ agent_name }}, an AI assistant for {{ company }}.
Today's date: {{ current_date }}.

You are a customer support specialist. Your goal is to resolve issues efficiently.
You can look up orders, issue refunds up to ${{ max_refund }}, and escalate to humans.

Guidelines:
- Acknowledge frustration before solving
- Offer solutions before asking questions
- Always end with "Is there anything else I can help you with?"

Response format: Empathy → Solution → Confirmation"""

    variables = {
        "agent_name": "SupportBot",
        "company": "ShopEasy",
        "current_date": date.today().isoformat(),
        "max_refund": 50,
    }

    print(chat(FLAT_SUPPORT, variables, "My order arrived broken and I want a refund!"))

# Expected Token Savings: template reuse across calls; only variables differ per-request, not full prompt
# Environment: agents with complex prompt inheritance; Jinja2 supports loops, conditionals, macro reuse
```

### Option 4: Versioned Prompt Templates with A/B Selection

```python
import anthropic
import hashlib
import json
import sqlite3
import time
from pathlib import Path

DB = Path("/tmp/prompt_versions.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS prompt_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            template TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            active INTEGER DEFAULT 1,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS prompt_usage (
            call_id TEXT PRIMARY KEY,
            template_id TEXT,
            user_id TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            used_at REAL
        );
    """)
    con.commit()
    con.close()

def register_template(name: str, version: str, template: str, weight: float = 1.0) -> str:
    template_id = hashlib.md5(f"{name}:{version}".encode()).hexdigest()[:8]
    con = sqlite3.connect(DB)
    con.execute("""
        INSERT OR REPLACE INTO prompt_templates (id, name, version, template, weight, created_at)
        VALUES (?,?,?,?,?,?)
    """, (template_id, name, version, template, weight, time.time()))
    con.commit()
    con.close()
    return template_id

def select_template(name: str) -> tuple[str, str] | None:
    """Weighted random selection among active versions of a template."""
    import random
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, template, weight FROM prompt_templates WHERE name=? AND active=1",
        (name,),
    ).fetchall()
    con.close()
    if not rows:
        return None
    total = sum(r[2] for r in rows)
    r = random.uniform(0, total)
    cumulative = 0
    for tid, template, weight in rows:
        cumulative += weight
        if r <= cumulative:
            return tid, template
    return rows[-1][0], rows[-1][1]

def chat_with_versioned_prompt(
    template_name: str,
    variables: dict,
    user_input: str,
    user_id: str = "anon",
) -> str:
    result = select_template(template_name)
    if not result:
        raise ValueError(f"No active template: {template_name}")
    template_id, template = result
    system = template.format(**variables)

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_input}],
    )

    import uuid
    con = sqlite3.connect(DB)
    con.execute("""
        INSERT INTO prompt_usage VALUES (?,?,?,?,?,?)
    """, (str(uuid.uuid4()), template_id, user_id,
          resp.usage.input_tokens, resp.usage.output_tokens, time.time()))
    con.commit()
    con.close()
    return resp.content[0].text

if __name__ == "__main__":
    init_db()

    # Register two versions with different weights (80/20 A/B split)
    register_template(
        "assistant", "v1",
        "You are a helpful assistant for {company}. Be concise.",
        weight=0.8,
    )
    register_template(
        "assistant", "v2",
        "You are an expert AI assistant for {company}. Think step by step and be thorough.",
        weight=0.2,
    )

    for i in range(4):
        r = chat_with_versioned_prompt(
            "assistant",
            {"company": "TechCorp"},
            f"What is {i+1} + {i+1}?",
            user_id=f"user_{i}",
        )
        print(f"[{i+1}] {r.strip()[:50]}")

# Expected Token Savings: usage table reveals token cost per version; prune expensive v2 if it costs 2x more
# Environment: prompt optimization; A/B weight selection gradually rolls out improved prompts
```

### Option 5: Role-Based Prompt Matrix with Runtime Assembly

```python
import anthropic
from dataclasses import dataclass
from typing import Literal

client = anthropic.Anthropic()

Role = Literal["analyst", "writer", "coder", "reviewer"]
Tone = Literal["formal", "casual", "technical"]
OutputFormat = Literal["bullet", "prose", "json", "markdown"]

ROLE_CORE = {
    "analyst": "You are a data analyst. Focus on patterns, insights, and evidence-based conclusions.",
    "writer": "You are a technical writer. Prioritize clarity, structure, and readability.",
    "coder": "You are a software engineer. Write clean, correct, well-commented code.",
    "reviewer": "You are a critical reviewer. Identify flaws, gaps, and improvements.",
}

TONE_MODIFIER = {
    "formal": "Use formal language. Avoid contractions and colloquialisms.",
    "casual": "Use friendly, approachable language. Short sentences are fine.",
    "technical": "Use precise technical terminology without over-explaining basics.",
}

FORMAT_INSTRUCTION = {
    "bullet": "Structure your response as bullet points.",
    "prose": "Write in flowing paragraphs.",
    "json": "Return valid JSON only. No prose.",
    "markdown": "Use Markdown formatting with headers and code blocks where appropriate.",
}

@dataclass
class PromptMatrix:
    role: Role
    tone: Tone
    output_format: OutputFormat
    user_name: str = "User"
    task_context: str = ""
    constraints: list[str] = None

    def build(self) -> str:
        parts = [
            ROLE_CORE[self.role],
            TONE_MODIFIER[self.tone],
            FORMAT_INSTRUCTION[self.output_format],
        ]
        if self.task_context:
            parts.append(f"Task context: {self.task_context}")
        if self.constraints:
            constraint_block = "Constraints:\n" + "\n".join(f"- {c}" for c in self.constraints)
            parts.append(constraint_block)
        parts.append(f"Address the user as '{self.user_name}'.")
        return "\n\n".join(parts)

def chat(matrix: PromptMatrix, question: str) -> str:
    system = matrix.build()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    # Coder, technical, markdown
    coder_matrix = PromptMatrix(
        role="coder",
        tone="technical",
        output_format="markdown",
        user_name="Alex",
        constraints=["Python 3.11+ only", "No external dependencies"],
    )
    print("=== Coder ===")
    print(chat(coder_matrix, "Write a function to flatten a nested list."))

    # Analyst, formal, bullet
    analyst_matrix = PromptMatrix(
        role="analyst",
        tone="formal",
        output_format="bullet",
        user_name="Dr. Chen",
        task_context="Q4 sales performance review",
    )
    print("\n=== Analyst ===")
    print(chat(analyst_matrix, "What are three key factors to analyze in sales data?"))

# Expected Token Savings: matrix avoids prompt duplication; 4 roles × 3 tones × 4 formats = 48 combos from 11 blocks
# Environment: multi-persona agents; matrix pattern prevents combinatorial prompt file explosion
```

### Option 6: Prompt Template Registry with Hot Reload

```python
import anthropic
import json
import threading
import time
from pathlib import Path
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class TemplateRecord:
    name: str
    template: str
    version: int
    loaded_at: float

class PromptRegistry:
    """
    In-memory registry of prompt templates.
    Templates can be reloaded at runtime without restarting the agent.
    """
    def __init__(self):
        self._templates: dict[str, TemplateRecord] = {}
        self._lock = threading.RLock()

    def register(self, name: str, template: str, version: int = 1):
        with self._lock:
            existing = self._templates.get(name)
            if existing and existing.version >= version:
                return  # Skip stale update
            self._templates[name] = TemplateRecord(name, template, version, time.time())
            print(f"  [Registry] Registered '{name}' v{version}")

    def load_from_dict(self, templates: dict[str, dict]):
        """Bulk-load templates from a config dict."""
        for name, cfg in templates.items():
            self.register(name, cfg["template"], cfg.get("version", 1))

    def render(self, name: str, **variables) -> str:
        with self._lock:
            record = self._templates.get(name)
        if not record:
            raise KeyError(f"Template '{name}' not found in registry")
        try:
            return record.template.format(**variables)
        except KeyError as e:
            raise ValueError(f"Template '{name}' missing variable: {e}")

    def list_templates(self) -> list[str]:
        with self._lock:
            return list(self._templates.keys())

registry = PromptRegistry()

# Initial templates
registry.load_from_dict({
    "support": {
        "version": 1,
        "template": "You are a support agent for {company}. Today: {date}. Help users resolve issues quickly.",
    },
    "onboarding": {
        "version": 1,
        "template": "You are an onboarding assistant for {company}. Welcome {user_name} warmly and guide them step by step.",
    },
})

def chat(template_name: str, variables: dict, question: str) -> str:
    system = registry.render(template_name, **variables)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text

def simulate_hot_reload():
    """Simulate a config push updating a template at runtime."""
    time.sleep(1)
    registry.register(
        "support",
        "You are a premium support agent for {company}. Today: {date}. You have authority to offer 20% discounts.",
        version=2,
    )
    print("  [HotReload] support template updated to v2")

if __name__ == "__main__":
    from datetime import date
    vars_support = {"company": "Acme", "date": date.today().isoformat()}
    vars_onboard = {"company": "Acme", "user_name": "Sam"}

    reload_thread = threading.Thread(target=simulate_hot_reload, daemon=True)
    reload_thread.start()

    print("[v1] Support:", chat("support", vars_support, "My order is late.")[:60])
    time.sleep(1.5)
    print("[v2] Support:", chat("support", vars_support, "My order is late.")[:60])
    print("Onboarding:", chat("onboarding", vars_onboard, "Where do I start?")[:60])
    print("\nRegistered templates:", registry.list_templates())

# Expected Token Savings: hot reload avoids restart overhead; correct template = fewer correction retries
# Environment: long-running agents where prompt updates should apply immediately without downtime
```

## Comparison

| Option | Templating | Variables | Versioning | Runtime Update |
|--------|-----------|----------|-----------|---------------|
| 1 — f-string + context dict | f-string | Dataclass | No | No |
| 2 — Block composition | f-string | Dict | No | Add blocks |
| 3 — Jinja2 inheritance | Jinja2 | Dict | No | No |
| 4 — A/B versioned | f-string | Dict | Yes + weighted A/B | DB update |
| 5 — Role-tone-format matrix | f-string | Dataclass | No | Code change |
| 6 — Hot-reload registry | f-string | Dict | Version numbers | Thread-safe at runtime |
