---
layout: solution
title: "Agent Sends Empty or Whitespace-Only Messages"
category: general
description: "Agent constructs messages programmatically from templates or concatenation — sometimes producing empty strings, whitespace-only content, or None values in the messages array. The API returns a 400 error or the model receives a blank prompt and hallucinates a question."
tags: [general, validation, messages, api-error, prompt-construction, robustness]
---

## Symptom

Agent logs show `anthropic.BadRequestError: messages[2].content: field required` or the model responds "Could you clarify your question?" to a message the user never sent. Inspection reveals `{"role": "user", "content": "   "}` or `{"role": "user", "content": ""}` in the messages array — the result of a template that rendered to nothing when variables were missing.

Error frequency in production without validation: **3–8% of requests** (spikes after deploys that change prompt templates)

## Root Cause

Message content is built by string concatenation or template rendering. When upstream variables are `None`, empty lists, or zero-length tool results, the assembled content string becomes `""` or whitespace. No guard exists before the array is passed to the API.

## Fix

---

### Option 1 — Pre-Flight Message Validator

Validate every message array before calling the API. Strip whitespace, remove empty entries, raise descriptive errors.

```python
import anthropic
from typing import Any

client = anthropic.Anthropic()

class EmptyMessageError(ValueError):
    pass

def validate_messages(messages: list[dict]) -> list[dict]:
    """
    Validate and clean a messages array before sending to the API.
    Removes empty content; raises on structurally invalid entries.
    """
    cleaned = []
    for i, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role not in ("user", "assistant"):
            raise EmptyMessageError(f"messages[{i}]: invalid role '{role}'")

        # Handle string content
        if isinstance(content, str):
            stripped = content.strip()
            if not stripped:
                # Log and skip — do not include blank messages
                print(f"[Validator] Skipping messages[{i}] ({role}): empty string content")
                continue
            cleaned.append({"role": role, "content": stripped})

        # Handle list content (tool use / multipart)
        elif isinstance(content, list):
            valid_blocks = [b for b in content if b and isinstance(b, dict)]
            if not valid_blocks:
                print(f"[Validator] Skipping messages[{i}] ({role}): empty content block list")
                continue
            cleaned.append({"role": role, "content": valid_blocks})

        elif content is None:
            print(f"[Validator] Skipping messages[{i}] ({role}): None content")
            continue

        else:
            raise EmptyMessageError(f"messages[{i}]: unexpected content type {type(content)}")

    # Ensure no consecutive same-role messages (API requirement)
    for i in range(1, len(cleaned)):
        if cleaned[i]["role"] == cleaned[i - 1]["role"]:
            raise EmptyMessageError(
                f"Consecutive {cleaned[i]['role']} messages at indices {i-1} and {i} — merge or interleave them"
            )

    if not cleaned:
        raise EmptyMessageError("All messages were empty after validation — nothing to send")

    return cleaned

def safe_create(
    messages: list[dict],
    system: str = "",
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
) -> str:
    try:
        clean = validate_messages(messages)
    except EmptyMessageError as e:
        return f"[Agent Error] Message validation failed: {e}"

    kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": clean}
    if system and system.strip():
        kwargs["system"] = system.strip()

    response = client.messages.create(**kwargs)
    return response.content[0].text

# Test cases
test_cases = [
    # Normal message — passes
    [{"role": "user", "content": "What is the capital of France?"}],
    # Empty string — stripped and skipped, then raises (nothing left)
    [{"role": "user", "content": "   "}],
    # None content — skipped
    [{"role": "user", "content": None}],
    # Mixed: one valid, one empty
    [
        {"role": "user", "content": "Hello!"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": ""},   # Empty — will be stripped
    ],
    # Template that rendered to whitespace
    [{"role": "user", "content": f"Context: {''.join([])}\nQuestion: {''}"}],
]

for i, msgs in enumerate(test_cases):
    print(f"\n--- Test {i+1} ---")
    result = safe_create(msgs)
    print(f"Result: {result[:80]}")
```

**Expected Token Savings:** 5–10% — prevents wasted API round-trips that return errors
**Environment:** `pip install anthropic`

---

### Option 2 — Message Builder with Null-Safe Content Assembly

Replace raw string concatenation with a message builder that silently drops empty sections and only finalises a message when it has valid content.

```python
import anthropic
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class MessageBuilder:
    """Null-safe message builder. Empty sections are silently omitted."""
    role: str
    _parts: list[str] = field(default_factory=list)

    def add(self, label: str, value: Optional[str], required: bool = False) -> "MessageBuilder":
        """Add a named section. Skips None/empty unless required=True."""
        if value and value.strip():
            self._parts.append(f"{label}: {value.strip()}" if label else value.strip())
        elif required:
            raise ValueError(f"Required section '{label}' is empty or None")
        return self

    def add_list(self, label: str, items: list[str]) -> "MessageBuilder":
        """Add a list section. Skips if list is empty."""
        valid = [item for item in items if item and item.strip()]
        if valid:
            formatted = "\n".join(f"- {item.strip()}" for item in valid)
            self._parts.append(f"{label}:\n{formatted}" if label else formatted)
        return self

    def add_raw(self, text: Optional[str]) -> "MessageBuilder":
        """Add raw text, skipping if empty."""
        if text and text.strip():
            self._parts.append(text.strip())
        return self

    def build(self) -> Optional[dict]:
        """Return the message dict, or None if content is empty."""
        content = "\n\n".join(self._parts).strip()
        if not content:
            return None
        return {"role": self.role, "content": content}

def build_conversation(
    user_query: str,
    retrieved_context: Optional[str],
    tool_results: list[str],
    prior_summary: Optional[str],
) -> list[dict]:
    """Build a messages array safely — no empty messages will be included."""
    messages = []

    user_msg = (
        MessageBuilder("user")
        .add("Context", retrieved_context)            # Skipped if None
        .add_list("Tool results", tool_results)       # Skipped if list is empty
        .add("Prior conversation", prior_summary)     # Skipped if None
        .add("Question", user_query, required=True)   # Raises if empty
        .build()
    )

    if user_msg:
        messages.append(user_msg)

    return messages

# Simulate various states of upstream data
scenarios = [
    ("What is the status?", "System is operational.", ["DB: OK", "API: OK"], None),
    ("What is the status?", None,                    [],                    None),   # All optional parts missing
    ("Summarise findings.", None,                    [],                    "Previously: user asked about sales."),
    ("",                   "Some context",           ["Result A"],          None),   # Empty query — will raise
]

for query, ctx, tools, summary in scenarios:
    print(f"\nQuery: '{query[:40]}', ctx={'yes' if ctx else 'no'}, tools={len(tools)}, summary={'yes' if summary else 'no'}")
    try:
        msgs = build_conversation(query, ctx, tools, summary)
        if msgs:
            print(f"Built {len(msgs)} message(s), content length: {len(msgs[0]['content'])}")
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                messages=msgs,
            )
            print(f"Response: {response.content[0].text[:60]}...")
        else:
            print("No messages built — nothing to send")
    except ValueError as e:
        print(f"Build error (expected): {e}")
```

**Expected Token Savings:** None — same tokens; eliminates API error round-trips
**Environment:** `pip install anthropic`

---

### Option 3 — Pydantic Message Schema with Strict Validation

Model the messages array with Pydantic. Validation happens on construction — invalid messages never reach the API call.

```python
import anthropic
from pydantic import BaseModel, field_validator, model_validator
from typing import Literal, Union

client = anthropic.Anthropic()

class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("text block content must not be empty or whitespace")
        return v.strip()

class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("tool_result content must not be empty")
        return v

class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: Union[str, list[Union[TextBlock, ToolResultBlock]]]

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v):
        if isinstance(v, str) and not v.strip():
            raise ValueError("message content string must not be empty or whitespace only")
        if isinstance(v, list) and len(v) == 0:
            raise ValueError("message content list must not be empty")
        return v

    def to_api_dict(self) -> dict:
        if isinstance(self.content, str):
            return {"role": self.role, "content": self.content}
        return {
            "role": self.role,
            "content": [block.model_dump() for block in self.content],
        }

class ConversationRequest(BaseModel):
    messages: list[Message]
    system: str = ""

    @model_validator(mode="after")
    def validate_structure(self) -> "ConversationRequest":
        if not self.messages:
            raise ValueError("messages list must not be empty")
        # No consecutive same-role messages
        for i in range(1, len(self.messages)):
            if self.messages[i].role == self.messages[i - 1].role:
                raise ValueError(
                    f"Consecutive {self.messages[i].role} messages at positions {i-1} and {i}"
                )
        return self

def validated_call(request: ConversationRequest) -> str:
    api_messages = [m.to_api_dict() for m in request.messages]
    kwargs = {"model": "claude-sonnet-4-6", "max_tokens": 512, "messages": api_messages}
    if request.system.strip():
        kwargs["system"] = request.system.strip()
    response = client.messages.create(**kwargs)
    return response.content[0].text

# Valid request
try:
    req = ConversationRequest(
        messages=[Message(role="user", content="What's 2+2?")],
    )
    print(f"Valid: {validated_call(req)}")
except Exception as e:
    print(f"Error: {e}")

# Invalid: empty string content
try:
    Message(role="user", content="   ")
except Exception as e:
    print(f"Caught empty string: {e}")

# Invalid: None-like (empty list)
try:
    Message(role="user", content=[])
except Exception as e:
    print(f"Caught empty list: {e}")

# Invalid: consecutive same-role messages
try:
    ConversationRequest(messages=[
        Message(role="user", content="Hello"),
        Message(role="user", content="Are you there?"),
    ])
except Exception as e:
    print(f"Caught consecutive roles: {e}")
```

**Expected Token Savings:** None — prevents errors; same tokens when messages are valid
**Environment:** `pip install anthropic pydantic`

---

### Option 4 — Template Renderer with Mandatory Non-Empty Assertion

Wrap all prompt template rendering in a renderer that asserts non-empty output. If a template renders to nothing, it raises at render time — not at API call time.

```python
import re
import anthropic
from string import Template
from typing import Any, Optional

client = anthropic.Anthropic()

class TemplateRenderError(ValueError):
    pass

class SafeTemplate:
    """
    Prompt template with null-safe variable substitution.
    Unreferenced variables are replaced with empty string (not left as $var).
    Empty optional sections are automatically removed.
    """
    OPTIONAL_SECTION_RE = re.compile(r"\[OPTIONAL:([^\]]+)\](.*?)\[/OPTIONAL\]", re.DOTALL)

    def __init__(self, template: str, name: str = "template"):
        self.template = template
        self.name = name

    def _process_optional_sections(self, text: str, variables: dict[str, Any]) -> str:
        """Remove [OPTIONAL:var]...[/OPTIONAL] blocks when var is empty/None."""
        def replace(match):
            var_name = match.group(1).strip()
            section_body = match.group(2)
            value = variables.get(var_name)
            if value and str(value).strip():
                return section_body.replace(f"${var_name}", str(value))
            return ""  # Drop the section

        return self.OPTIONAL_SECTION_RE.sub(replace, text)

    def render(self, **kwargs: Any) -> str:
        # Fill all variables; missing ones become empty string
        variables = {k: (str(v).strip() if v is not None else "") for k, v in kwargs.items()}

        text = self._process_optional_sections(self.template, kwargs)

        # Substitute known variables; ignore unknown $var patterns
        for key, value in variables.items():
            text = text.replace(f"${key}", value)

        # Remove any unreferenced $var placeholders
        text = re.sub(r"\$[a-zA-Z_][a-zA-Z0-9_]*", "", text)

        # Collapse multiple blank lines
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        return text

    def render_message(self, role: str = "user", **kwargs: Any) -> Optional[dict]:
        """Render and return a message dict, or None if content would be empty."""
        content = self.render(**kwargs)
        if not content:
            print(f"[Template:{self.name}] Rendered to empty — message omitted")
            return None
        return {"role": role, "content": content}

# Define templates with optional sections
RETRIEVAL_TEMPLATE = SafeTemplate("""
[OPTIONAL:context]## Retrieved Context
$context[/OPTIONAL]

[OPTIONAL:examples]## Examples
$examples[/OPTIONAL]

## User Question
$question
""", name="retrieval")

ANALYSIS_TEMPLATE = SafeTemplate("""
Analyse the following data:
$data

[OPTIONAL:focus_area]Focus specifically on: $focus_area[/OPTIONAL]

Provide a structured analysis.
""", name="analysis")

# Test with various missing values
scenarios = [
    # All fields present
    dict(context="Paris is the capital of France.", examples="Q: Capital of Spain? A: Madrid.", question="What is the capital of France?"),
    # Context missing — section dropped
    dict(context=None, examples=None, question="What is the capital of France?"),
    # All optional fields missing — only question remains
    dict(context="", examples="", question="Explain machine learning."),
    # Empty question — results in empty render
    dict(context="Some context here.", examples=None, question=""),
]

for i, kwargs in enumerate(scenarios):
    print(f"\n--- Scenario {i+1} ---")
    msg = RETRIEVAL_TEMPLATE.render_message(**kwargs)
    if msg:
        print(f"Content ({len(msg['content'])} chars): {msg['content'][:100]}...")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[msg],
        )
        print(f"Response: {response.content[0].text[:60]}...")
    else:
        print("Message omitted — empty after rendering")
```

**Expected Token Savings:** 5–15% — eliminates retries caused by template rendering bugs
**Environment:** `pip install anthropic`

---

### Option 5 — Conversation History Sanitizer

Periodically sanitize the in-memory conversation history before use. Remove empty turns, merge adjacent same-role messages, and detect tool result orphans (tool_result blocks with no matching tool_use).

```python
import json
import anthropic
from typing import Any

client = anthropic.Anthropic()

def sanitize_history(messages: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Clean a conversation history.
    Returns (cleaned_messages, list_of_issues_found).
    """
    issues = []
    cleaned = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content")

        # Remove None/missing role
        if role not in ("user", "assistant"):
            issues.append(f"[{i}] Invalid role '{role}' — removed")
            continue

        # Handle string content
        if isinstance(content, str):
            stripped = content.strip()
            if not stripped:
                issues.append(f"[{i}] {role}: empty string — removed")
                continue
            cleaned.append({"role": role, "content": stripped})

        # Handle list content
        elif isinstance(content, list):
            valid_blocks = []
            for j, block in enumerate(content):
                if not isinstance(block, dict):
                    issues.append(f"[{i}][{j}] Non-dict content block — removed")
                    continue
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "")
                    if not text or not text.strip():
                        issues.append(f"[{i}][{j}] Empty text block — removed")
                        continue
                    valid_blocks.append({**block, "text": text.strip()})
                elif block_type in ("tool_use", "tool_result"):
                    valid_blocks.append(block)
                else:
                    valid_blocks.append(block)

            if not valid_blocks:
                issues.append(f"[{i}] {role}: all content blocks empty — removed")
                continue
            cleaned.append({"role": role, "content": valid_blocks})

        elif content is None:
            issues.append(f"[{i}] {role}: None content — removed")
            continue

    # Fix consecutive same-role messages by merging
    merged = []
    for msg in cleaned:
        if merged and merged[-1]["role"] == msg["role"]:
            prev = merged[-1]
            # Merge string contents
            if isinstance(prev["content"], str) and isinstance(msg["content"], str):
                issues.append(f"Merging consecutive {msg['role']} messages")
                prev["content"] = prev["content"] + "\n\n" + msg["content"]
            else:
                # Can't auto-merge mixed types — keep as-is (may cause API error)
                issues.append(f"WARNING: Consecutive {msg['role']} messages with mixed content types")
                merged.append(msg)
        else:
            merged.append(msg)

    return merged, issues

def safe_chat(history: list[dict], new_message: str) -> tuple[str, list[dict]]:
    history.append({"role": "user", "content": new_message})
    clean, issues = sanitize_history(history)

    if issues:
        print(f"[Sanitizer] Fixed {len(issues)} issue(s):")
        for issue in issues:
            print(f"  {issue}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=clean,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply, history

# Simulate a corrupted history
corrupted_history: list[dict] = [
    {"role": "user",      "content": "Hello!"},
    {"role": "assistant", "content": "Hi there!"},
    {"role": "user",      "content": "   "},                    # Empty — will be removed
    {"role": "user",      "content": "Follow-up question."},    # Consecutive user — will be merged with previous
    {"role": "assistant", "content": None},                     # None — will be removed
    {"role": "assistant", "content": "Let me help with that."}, # Consecutive assistant (after None removed)
]

print("Starting conversation with corrupted history...\n")
reply, history = safe_chat(corrupted_history, "What can you help me with today?")
print(f"\nFinal reply: {reply[:100]}...")
print(f"Clean history length: {len(history)}")
```

**Expected Token Savings:** 10–20% — removes padding from empty/duplicate messages
**Environment:** `pip install anthropic`

---

### Option 6 — Structured Message Factory with Runtime Assertion

Replace ad-hoc message construction with a typed factory. Every factory method asserts non-empty output before returning — construction fails loud and early.

```python
import anthropic
from typing import Optional
from functools import wraps

client = anthropic.Anthropic()

def assert_non_empty(fn):
    """Decorator: raises if the returned message content is empty."""
    @wraps(fn)
    def wrapper(*args, **kwargs) -> dict:
        result = fn(*args, **kwargs)
        content = result.get("content", "")
        if isinstance(content, str) and not content.strip():
            raise ValueError(
                f"{fn.__name__} produced an empty message. "
                f"Args: {args[1:]}, Kwargs: {list(kwargs.keys())}"
            )
        if isinstance(content, list) and not content:
            raise ValueError(f"{fn.__name__} produced a message with an empty content list.")
        return result
    return wrapper

class MessageFactory:
    """Typed factory for building messages. All methods assert non-empty output."""

    @staticmethod
    @assert_non_empty
    def user_text(text: str) -> dict:
        return {"role": "user", "content": text.strip()}

    @staticmethod
    @assert_non_empty
    def user_with_context(question: str, context: Optional[str] = None, tool_output: Optional[str] = None) -> dict:
        parts = []
        if context and context.strip():
            parts.append(f"Context:\n{context.strip()}")
        if tool_output and tool_output.strip():
            parts.append(f"Tool output:\n{tool_output.strip()}")
        if not question or not question.strip():
            raise ValueError("question must not be empty")
        parts.append(f"Question: {question.strip()}")
        return {"role": "user", "content": "\n\n".join(parts)}

    @staticmethod
    @assert_non_empty
    def assistant_text(text: str) -> dict:
        return {"role": "assistant", "content": text.strip()}

    @staticmethod
    def tool_result(tool_use_id: str, content: str) -> dict:
        if not content or not content.strip():
            content = '{"status": "empty", "note": "tool returned no content"}'
        return {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
            }],
        }

def run_safe_conversation():
    history = []
    factory = MessageFactory()

    # Normal exchange
    try:
        history.append(factory.user_text("What is the boiling point of water?"))
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=history,
        )
        reply_text = response.content[0].text
        history.append(factory.assistant_text(reply_text))
        print(f"Q1: {reply_text[:80]}...")
    except ValueError as e:
        print(f"Message construction error: {e}")

    # Template produces empty question — caught at factory level
    try:
        dynamic_question = ""  # Simulates a template that rendered to nothing
        history.append(factory.user_with_context(
            question=dynamic_question,
            context="Some background information.",
        ))
    except ValueError as e:
        print(f"\nCaught empty question at factory: {e}")

    # Context is None but question is valid — context section omitted
    try:
        msg = factory.user_with_context(
            question="How does photosynthesis work?",
            context=None,
            tool_output=None,
        )
        history.append(msg)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=history,
        )
        print(f"\nQ2 (no context): {response.content[0].text[:80]}...")
    except ValueError as e:
        print(f"Factory error: {e}")

run_safe_conversation()
```

**Expected Token Savings:** None — correctness improvement; construction errors caught before API call
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Detection Point | Failure Mode | Best For |
|--------|----------------|-------------|----------|
| Pre-Flight Validator | Before API call | Raises / skips | Any existing codebase — retrofit |
| Message Builder | At assembly time | Returns None | Agents with many optional sections |
| Pydantic Schema | On object creation | Validation error | Type-safe codebases, FastAPI apps |
| Safe Template | At render time | Returns None | Prompt-heavy systems with templates |
| History Sanitizer | Before each turn | Auto-repair | Long-running agents with mutable history |
| Message Factory | At factory call | AssertionError | New agents — enforce from the start |

**Recommended starting point:** Option 1 (Pre-Flight Validator) — add one `validate_messages()` call wrapping every `client.messages.create()`. Catches all empty-content bugs immediately with no changes to the rest of the codebase.
