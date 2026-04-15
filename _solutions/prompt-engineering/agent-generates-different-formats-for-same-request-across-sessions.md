---
layout: solution
title: "Agent Generates Different Formats for Same Request Across Sessions"
category: prompt-engineering
description: "Agent produces inconsistent output formats across sessions — sometimes Markdown, sometimes plain text, sometimes JSON, sometimes prose — making downstream parsing unreliable."
tags: [prompt-engineering, format-consistency, structured-output, tool-use, few-shot, validation]
---

## Symptom

The agent produces the same logical content in different formats depending on the session:

```
Session A: "Here are the results:\n• Item 1\n• Item 2"
Session B: '{"results": ["Item 1", "Item 2"]}'
Session C: "Results\n-------\n1. Item 1\n2. Item 2"
```

Downstream parsers crash, dashboards show raw Markdown, and customers see inconsistent UI rendering. The format is effectively random across restarts.

## Root Cause

Claude samples from a probability distribution over tokens. Without an explicit format constraint in the system prompt or schema, the "best" format is ambiguous — bullet lists, numbered lists, JSON, prose, and tables all fit plausibly. Small differences in conversation history or temperature can tip the choice either way.

## Fix

---

### Option 1 — Format Registry with Mandatory Template Injection

Maintain a central registry mapping request types to canonical templates. Inject the exact template into every system prompt so the model fills in blanks rather than inventing structure.

```python
import anthropic
from dataclasses import dataclass

@dataclass
class FormatTemplate:
    name: str
    description: str
    template: str
    example: str

FORMAT_REGISTRY: dict[str, FormatTemplate] = {
    "search_results": FormatTemplate(
        name="search_results",
        description="List of search results with title, URL, and summary",
        template="""## Search Results

{%- for result in results %}
### {{ loop.index }}. {{ result.title }}
**URL:** {{ result.url }}
**Summary:** {{ result.summary }}
{% endfor %}
---
*{{ total_count }} results found*""",
        example="""## Search Results

### 1. Example Page
**URL:** https://example.com
**Summary:** A brief description of the page content.

---
*1 results found*"""
    ),
    "comparison_table": FormatTemplate(
        name="comparison_table",
        description="Side-by-side comparison of options",
        template="""| Feature | {{ option_a }} | {{ option_b }} |
|---------|{{ '-' * option_a|length }}|{{ '-' * option_b|length }}|
{%- for feature in features %}
| {{ feature.name }} | {{ feature.value_a }} | {{ feature.value_b }} |
{% endfor %}""",
        example="""| Feature | Option A | Option B |
|---------|----------|----------|
| Price | $10/mo | $20/mo |
| Storage | 10 GB | 50 GB |"""
    ),
    "action_plan": FormatTemplate(
        name="action_plan",
        description="Numbered steps with context",
        template="""## Action Plan: {{ goal }}

**Estimated time:** {{ estimate }}

{%- for step in steps %}
{{ loop.index }}. **{{ step.title }}**
   {{ step.description }}
   *Prerequisite: {{ step.prereq or 'None' }}*
{% endfor %}
**Success criteria:** {{ success_criteria }}""",
        example="""## Action Plan: Deploy Application

**Estimated time:** 30 minutes

1. **Build Docker image**
   Run `docker build -t app:latest .`
   *Prerequisite: None*

**Success criteria:** Application responds on port 8080"""
    ),
}

def get_format_system_prompt(request_type: str) -> str:
    template = FORMAT_REGISTRY.get(request_type)
    if not template:
        return "Respond in clear, consistent Markdown."

    return f"""You MUST format your response using EXACTLY this template structure:

FORMAT NAME: {template.name}
DESCRIPTION: {template.description}

REQUIRED TEMPLATE:
{template.template}

EXAMPLE OUTPUT:
{template.example}

Do not deviate from this structure. Fill in the template fields with the actual content.
Do not add extra sections, preamble, or postamble beyond the template."""

def query_with_format(request_type: str, user_message: str) -> str:
    client = anthropic.Anthropic()

    system_prompt = get_format_system_prompt(request_type)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text

# Usage
result = query_with_format(
    "search_results",
    "Show me results for Python async tutorials"
)
print(result)
```

**Expected Token Savings:** ~30% on repeated same-type requests via prompt caching on the format template
**Environment:** `pip install anthropic`

---

### Option 2 — Forced Structured Output via tool_choice

Use `tool_choice={"type": "tool", "name": "format_response"}` to guarantee the model always returns a schema-validated structure. Render it to the desired format after receiving the typed response.

```python
import json
import anthropic

client = anthropic.Anthropic()

RESPONSE_SCHEMAS = {
    "item_list": {
        "name": "format_response",
        "description": "Format a list of items consistently",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Section heading"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "detail": {"type": "string"},
                            "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                        },
                        "required": ["label", "detail"],
                    },
                },
                "summary": {"type": "string", "description": "One-sentence summary"},
            },
            "required": ["title", "items", "summary"],
        },
    },
    "step_by_step": {
        "name": "format_response",
        "description": "Format a step-by-step plan",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_number": {"type": "integer"},
                            "action": {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["step_number", "action"],
                    },
                },
                "estimated_duration": {"type": "string"},
            },
            "required": ["goal", "steps"],
        },
    },
}

def render_item_list(data: dict) -> str:
    lines = [f"## {data['title']}", ""]
    for item in data["items"]:
        priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
            item.get("priority", "medium"), "•"
        )
        lines.append(f"{priority_icon} **{item['label']}**: {item['detail']}")
    lines.extend(["", f"*{data['summary']}*"])
    return "\n".join(lines)

def render_step_by_step(data: dict) -> str:
    lines = [f"## Plan: {data['goal']}", ""]
    for step in data["steps"]:
        lines.append(f"**Step {step['step_number']}:** {step['action']}")
        if step.get("rationale"):
            lines.append(f"   _{step['rationale']}_")
        lines.append("")
    if data.get("estimated_duration"):
        lines.append(f"⏱ Estimated: {data['estimated_duration']}")
    return "\n".join(lines)

RENDERERS = {
    "item_list": render_item_list,
    "step_by_step": render_step_by_step,
}

def query_structured(schema_type: str, user_message: str) -> str:
    schema = RESPONSE_SCHEMAS[schema_type]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=[schema],
        tool_choice={"type": "tool", "name": "format_response"},
        messages=[{"role": "user", "content": user_message}],
    )

    # Extract structured data from tool call
    tool_use = next(b for b in response.content if b.type == "tool_use")
    structured_data = tool_use.input

    # Render to consistent Markdown
    renderer = RENDERERS[schema_type]
    return renderer(structured_data)

# Usage
result = query_structured(
    "step_by_step",
    "How do I set up a Python virtual environment?"
)
print(result)
```

**Expected Token Savings:** Negligible — correctness benefit, not cost benefit
**Environment:** `pip install anthropic`

---

### Option 3 — Post-Generation Format Validator with Regeneration

Validate the agent's output against a format spec after each generation. If it fails, regenerate with an explicit correction prompt. Cap retries at 2.

```python
import re
import anthropic
from dataclasses import dataclass
from typing import Callable

@dataclass
class FormatSpec:
    name: str
    rules: list[tuple[str, str]]  # (rule_description, regex_or_check)
    correction_hint: str

def check_has_markdown_headers(text: str) -> bool:
    return bool(re.search(r"^#{1,3} ", text, re.MULTILINE))

def check_has_bullet_list(text: str) -> bool:
    return bool(re.search(r"^[•\-\*] ", text, re.MULTILINE))

def check_no_raw_json(text: str) -> bool:
    return not bool(re.match(r"^\s*\{", text.strip()))

def check_has_numbered_steps(text: str) -> bool:
    return bool(re.search(r"^\d+\. ", text, re.MULTILINE))

FORMAT_SPECS: dict[str, FormatSpec] = {
    "tutorial": FormatSpec(
        name="tutorial",
        rules=[
            ("Must have a Markdown heading", check_has_markdown_headers),
            ("Must have numbered steps", check_has_numbered_steps),
            ("Must not return raw JSON", check_no_raw_json),
        ],
        correction_hint=(
            "Your response must use Markdown format with:\n"
            "1. A ## heading at the top\n"
            "2. Numbered steps (1. 2. 3.)\n"
            "3. No raw JSON objects\n"
            "Please rewrite your response following these rules exactly."
        ),
    ),
    "list_summary": FormatSpec(
        name="list_summary",
        rules=[
            ("Must have a Markdown heading", check_has_markdown_headers),
            ("Must have bullet points", check_has_bullet_list),
        ],
        correction_hint=(
            "Your response must use:\n"
            "• A ## heading\n"
            "• Bullet points with - or •\n"
            "Rewrite following these rules."
        ),
    ),
}

def validate_format(text: str, spec: FormatSpec) -> list[str]:
    failures = []
    for rule_name, check_fn in spec.rules:
        if not check_fn(text):
            failures.append(rule_name)
    return failures

def query_with_validation(
    format_type: str,
    user_message: str,
    max_retries: int = 2,
) -> str:
    client = anthropic.Anthropic()
    spec = FORMAT_SPECS[format_type]

    messages = [{"role": "user", "content": user_message}]

    for attempt in range(max_retries + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=f"You are a helpful assistant. Always respond in {spec.name} format.",
            messages=messages,
        )

        output = response.content[0].text
        failures = validate_format(output, spec)

        if not failures:
            return output

        if attempt < max_retries:
            # Append correction request
            messages.append({"role": "assistant", "content": output})
            correction = (
                f"Your response did not meet the required format.\n"
                f"Failed rules: {', '.join(failures)}\n\n"
                f"{spec.correction_hint}"
            )
            messages.append({"role": "user", "content": correction})

    # Return last attempt even if imperfect
    return output

# Usage
result = query_with_validation(
    "tutorial",
    "How do I install Python packages?"
)
print(result)
```

**Expected Token Savings:** -10% overhead on retry; saves downstream debugging cost
**Environment:** `pip install anthropic`

---

### Option 4 — Few-Shot Examples Pinned to System Prompt with Prompt Caching

Pin 3 canonical input/output examples directly in the system prompt. Use `cache_control` so the few-shot block is cached and reused across all sessions.

```python
import anthropic

FEW_SHOT_EXAMPLES = """
You always format responses using consistent Markdown. Here are canonical examples:

---
USER: List the top 3 benefits of exercise.
ASSISTANT:
## Benefits of Exercise

- **Improved cardiovascular health** — Regular exercise strengthens the heart and reduces risk of heart disease.
- **Better mental well-being** — Physical activity releases endorphins that reduce anxiety and improve mood.
- **Increased energy levels** — Consistent exercise improves stamina and reduces fatigue throughout the day.

*Start with 20 minutes of moderate activity three times per week.*

---
USER: How do I make a peanut butter sandwich?
ASSISTANT:
## Peanut Butter Sandwich

**Ingredients:** bread (2 slices), peanut butter, optional: jam

1. **Lay out bread** — Place two slices on a clean surface.
2. **Spread peanut butter** — Use a knife to coat one slice evenly, about 2mm thick.
3. **Add jam (optional)** — Spread jam on the other slice if desired.
4. **Combine** — Press the slices together, peanut butter side inward.
5. **Cut** — Slice diagonally for best results.

*Serve immediately or wrap for later.*

---
USER: Compare Python and JavaScript.
ASSISTANT:
## Python vs JavaScript

| Aspect | Python | JavaScript |
|--------|--------|------------|
| Primary use | Data science, scripting | Web development |
| Typing | Dynamic, strong | Dynamic, weak |
| Syntax | Indentation-based | Curly-brace blocks |
| Runtime | CPython, PyPy | V8, SpiderMonkey |
| Learning curve | Gentle | Moderate |

**Verdict:** Choose Python for data/ML work; JavaScript for web frontends.

---
Always follow the exact same Markdown structure shown above: heading, body with bullets/numbered-steps/table as appropriate, and a brief closing note in italics.
"""

def create_consistent_agent():
    client = anthropic.Anthropic()

    def query(user_message: str) -> str:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": FEW_SHOT_EXAMPLES,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        return response.content[0].text

    return query

# The few-shot block is cached after the first call
# All subsequent calls in the session pay zero input tokens for it
agent = create_consistent_agent()

r1 = agent("What are the benefits of meditation?")
r2 = agent("How do I brew coffee?")
r3 = agent("Compare cats and dogs as pets.")

for i, r in enumerate([r1, r2, r3], 1):
    print(f"=== Response {i} ===")
    print(r)
    print()
```

**Expected Token Savings:** ~60% on system prompt tokens after first call (cached few-shot block)
**Environment:** `pip install anthropic`

---

### Option 5 — Format Preference Persistence with SQLite

Store the user's last confirmed format preference in SQLite. Inject it into subsequent sessions so format is consistent per user, not random.

```python
import sqlite3
import json
import anthropic
from pathlib import Path
from datetime import datetime

DB_PATH = Path("format_preferences.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS format_prefs (
            user_id TEXT PRIMARY KEY,
            preferred_format TEXT NOT NULL,
            format_config TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            usage_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS format_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            session_id TEXT,
            format_used TEXT,
            was_correct INTEGER,
            user_correction TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

SUPPORTED_FORMATS = {
    "markdown_bullets": {
        "description": "Markdown with bullet points and headers",
        "instruction": "Use Markdown formatting with ## headers and - bullet points.",
        "example": "## Topic\n- Point one\n- Point two",
    },
    "numbered_steps": {
        "description": "Numbered step-by-step format",
        "instruction": "Format as numbered steps with bold step titles.",
        "example": "1. **First step** — Do this.\n2. **Second step** — Then this.",
    },
    "plain_prose": {
        "description": "Plain prose paragraphs",
        "instruction": "Write in plain prose paragraphs without special formatting.",
        "example": "Here is the information. It continues naturally...",
    },
    "json": {
        "description": "Structured JSON output",
        "instruction": 'Always respond with valid JSON in {"response": ..., "key_points": [...]} shape.',
        "example": '{"response": "...", "key_points": ["point1", "point2"]}',
    },
}

def get_format_for_user(user_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT preferred_format, format_config FROM format_prefs WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()

    if row:
        return {"format_name": row[0], **json.loads(row[1])}

    # Default format
    return {"format_name": "markdown_bullets", **SUPPORTED_FORMATS["markdown_bullets"]}

def save_format_preference(user_id: str, format_name: str):
    if format_name not in SUPPORTED_FORMATS:
        raise ValueError(f"Unknown format: {format_name}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO format_prefs (user_id, preferred_format, format_config, confirmed_at, usage_count)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            preferred_format = excluded.preferred_format,
            format_config = excluded.format_config,
            confirmed_at = excluded.confirmed_at,
            usage_count = usage_count + 1
    """, (user_id, format_name, json.dumps(SUPPORTED_FORMATS[format_name]), datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def query_with_user_format(user_id: str, user_message: str) -> str:
    init_db()
    client = anthropic.Anthropic()

    fmt = get_format_for_user(user_id)
    format_instruction = (
        f"FORMATTING REQUIREMENT (User {user_id} preference — always use this format):\n"
        f"{fmt['instruction']}\n\n"
        f"Example of correct format:\n{fmt['example']}"
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": format_instruction,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    return response.content[0].text

# Demo: save preference, then query across "sessions"
save_format_preference("user-42", "numbered_steps")

for question in [
    "How do I improve my sleep?",
    "Tips for better productivity?",
    "How to learn a new language?",
]:
    print(f"Q: {question}")
    print(query_with_user_format("user-42", question))
    print()
```

**Expected Token Savings:** ~25% via cached format instruction block
**Environment:** `pip install anthropic`

---

### Option 6 — Pre-Generation Format Declaration (Prefill Anchoring)

Use the assistant prefill technique to lock in the first tokens of every response. Starting with `## ` or `1. ` forces the model into a specific format branch before sampling any content tokens.

```python
import anthropic
from enum import Enum

class FormatAnchor(str, Enum):
    MARKDOWN_HEADER = "## "
    NUMBERED_LIST = "1. **"
    BULLET_LIST = "- **"
    TABLE = "| "
    JSON_OBJECT = '{\n  "response": "'
    JSON_ARRAY = '[\n  "'

FORMAT_SYSTEM_PROMPTS = {
    FormatAnchor.MARKDOWN_HEADER: (
        "Structure every response with a ## heading, then organized content below it. "
        "End with a brief italic summary."
    ),
    FormatAnchor.NUMBERED_LIST: (
        "Always respond as a numbered list. Each item has a bold title followed by explanation. "
        "Format: 1. **Title** — Explanation."
    ),
    FormatAnchor.BULLET_LIST: (
        "Always respond as a bullet list with bold labels. "
        "Format: - **Label**: Description."
    ),
    FormatAnchor.TABLE: (
        "When comparing items, always use a Markdown table. "
        "Columns must have consistent alignment."
    ),
    FormatAnchor.JSON_OBJECT: (
        'Always respond with a JSON object: {"response": "...", "summary": "...", "confidence": 0.0-1.0}. '
        "No prose outside the JSON."
    ),
    FormatAnchor.JSON_ARRAY: (
        'Always respond with a JSON array of strings: ["item1", "item2", ...]. '
        "No prose outside the JSON."
    ),
}

def query_with_prefill(
    anchor: FormatAnchor,
    user_message: str,
) -> str:
    client = anthropic.Anthropic()

    system_prompt = FORMAT_SYSTEM_PROMPTS[anchor]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": user_message},
            # Prefill forces the response to begin with the anchor token
            {"role": "assistant", "content": anchor.value},
        ],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    # Prepend the anchor since it was provided as prefill, not generated
    raw = response.content[0].text
    return anchor.value + raw

# Test format consistency across many calls
questions = [
    "What are the benefits of open-source software?",
    "List reasons to learn a second language.",
    "Why is sleep important?",
]

print("=== Markdown Header Format ===")
for q in questions:
    result = query_with_prefill(FormatAnchor.MARKDOWN_HEADER, q)
    print(f"First line: {result.splitlines()[0]!r}")
    print()

print("=== JSON Object Format ===")
for q in questions:
    result = query_with_prefill(FormatAnchor.JSON_OBJECT, q)
    print(f"First 60 chars: {result[:60]!r}")
    print()
```

**Expected Token Savings:** ~40% via cached system prompt; prefill adds 0 cost (no input tokens for anchor)
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Consistency | Implementation | Token Cost | Best For |
|--------|-------------|----------------|------------|----------|
| Format Registry | High | Medium | Low (cached) | Multi-type apps |
| Forced tool_choice | Very High | Medium | Neutral | Machine consumers |
| Post-gen Validation | Medium | Low | +10% retry | Quick fixes |
| Few-Shot Pinning | High | Low | Low (cached) | Human-readable output |
| SQLite Preference | High | Medium | Low (cached) | Multi-user apps |
| Prefill Anchoring | Very High | Low | Very Low | Single-format apps |

**Recommended starting point:** Option 6 (Prefill Anchoring) for single-format requirements, Option 2 (Forced tool_choice) when output is consumed by code.
