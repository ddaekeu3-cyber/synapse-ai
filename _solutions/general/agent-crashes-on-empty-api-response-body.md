---
layout: solution
title: "Agent Crashes on Empty API Response Body"
category: general
description: "Agent raises AttributeError or IndexError when the LLM returns an empty content list, a None stop_reason, or a null response body — often under rate limiting or network errors."
tags: [error-handling, robustness, api, null-safety, stop-reason]
---

## Symptom

The agent crashes mid-run with one of these tracebacks:

```
IndexError: list index out of range
AttributeError: 'NoneType' object has no attribute 'text'
KeyError: 'content'
```

It happens sporadically — normal runs succeed, but under load, after a 429 retry, or during transient network hiccups, the response body arrives empty or malformed and the agent explodes without a clean error message.

## Root Cause

The Anthropic API can return a valid HTTP 200 with an empty `content` list when the model stops due to `max_tokens`, a safety filter, or an internal timeout. The typical anti-pattern:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

def ask(prompt: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    # Crashes when content is [] or stop_reason is unexpected
    return response.content[0].text
```

`response.content` can be `[]` when `stop_reason == "max_tokens"` and the model produced nothing before hitting the limit, or when a content policy fires. Accessing `[0]` without checking length raises `IndexError`.

---

## Fix

### Option 1 — Defensive index with fallback string

Add a length check before indexing. Return a sentinel string so the caller can detect and retry.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

def ask(prompt: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )

    if not response.content:
        return ""  # caller treats empty string as a retryable signal

    block = response.content[0]
    if block.type != "text":
        return ""

    return block.text

# Expected Token Savings: none (no extra calls); prevents crash-and-burn restarts
# Environment: any synchronous agent using client.messages.create
```

---

### Option 2 — Stop-reason guard before content access

Check `stop_reason` first. `"end_turn"` is the only normal completion. Anything else (`"max_tokens"`, `"stop_sequence"`, `"tool_use"`, `None`) is handled explicitly.

```python
import anthropic
from typing import Optional

client = anthropic.Anthropic(api_key="sk-live-...")

RETRYABLE_STOP_REASONS = {"max_tokens"}
TERMINAL_STOP_REASONS = {"stop_sequence"}

def ask(prompt: str, max_retries: int = 2) -> Optional[str]:
    for attempt in range(max_retries + 1):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )

        stop_reason = response.stop_reason

        if stop_reason == "end_turn":
            if response.content and response.content[0].type == "text":
                return response.content[0].text
            return ""  # end_turn but empty content — treat as empty

        if stop_reason in RETRYABLE_STOP_REASONS and attempt < max_retries:
            # Widen the budget and retry
            prompt = prompt + "\n\nPlease continue your response."
            continue

        if stop_reason in TERMINAL_STOP_REASONS:
            # Content policy / stop sequence — not retryable
            return None

        # Unknown stop_reason — return whatever text exists
        texts = [b.text for b in response.content if b.type == "text"]
        return " ".join(texts) if texts else None

    return None

# Expected Token Savings: avoids wasted retries from crash-restart cycles
# Environment: agents that call create() in a loop with continuation logic
```

---

### Option 3 — Safe extractor utility function

Centralise all null-safety in one reusable helper. Every agent call goes through `extract_text()`.

```python
import anthropic
import logging
from typing import Optional

log = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key="sk-live-...")

def extract_text(response: anthropic.types.Message) -> Optional[str]:
    """Safely pull the first text block from a Message, or return None."""
    if response is None:
        log.warning("extract_text: received None response")
        return None

    if not hasattr(response, "content") or response.content is None:
        log.warning("extract_text: response has no content attribute")
        return None

    text_blocks = [
        block.text
        for block in response.content
        if hasattr(block, "type") and block.type == "text"
        and hasattr(block, "text") and block.text
    ]

    if not text_blocks:
        log.warning(
            "extract_text: no text blocks; stop_reason=%s content_len=%d",
            getattr(response, "stop_reason", "unknown"),
            len(response.content),
        )
        return None

    return text_blocks[0]


def ask(prompt: str) -> Optional[str]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    return extract_text(response)

# Expected Token Savings: zero extra tokens; eliminates crash-restart overhead
# Environment: any project with multiple call sites — centralise once, reuse everywhere
```

---

### Option 4 — Async-safe null guard with structured error events

For async agents, propagate errors as structured events rather than raising exceptions that kill the entire task group.

```python
import asyncio
import anthropic
from dataclasses import dataclass
from typing import Optional

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

@dataclass
class AgentResult:
    text: Optional[str]
    stop_reason: Optional[str]
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.text is not None


async def ask_async(prompt: str) -> AgentResult:
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
    except anthropic.APIError as exc:
        return AgentResult(text=None, stop_reason=None, error=str(exc))

    content = getattr(response, "content", None) or []
    text_blocks = [b.text for b in content if getattr(b, "type", None) == "text"]

    return AgentResult(
        text=text_blocks[0] if text_blocks else None,
        stop_reason=getattr(response, "stop_reason", None),
        error=None if text_blocks else f"empty_content:{response.stop_reason}",
    )


async def main():
    prompts = ["What is 2+2?", "Explain quantum entanglement.", ""]
    results = await asyncio.gather(*[ask_async(p) for p in prompts])

    for prompt, result in zip(prompts, results):
        if result.ok:
            print(f"OK: {result.text[:60]}")
        else:
            print(f"FAIL [{result.stop_reason}]: {result.error}")

asyncio.run(main())

# Expected Token Savings: parallel gather without crash propagation saves retry tokens
# Environment: async agents using asyncio.gather() or TaskGroup
```

---

### Option 5 — Streaming response null guard

Streaming responses have a different shape. Guard against empty stream events and `None` final message.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

def ask_streaming(prompt: str) -> str:
    collected: list[str] = []
    stop_reason: str | None = None

    try:
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for text_chunk in stream.text_stream:
                if text_chunk:  # skip empty/None chunks
                    collected.append(text_chunk)

            # get_final_message() can return None on stream abort
            final = stream.get_final_message()
            if final is not None:
                stop_reason = final.stop_reason

    except anthropic.APIStatusError as exc:
        # Stream interrupted mid-flight
        if collected:
            # Return partial text rather than crashing
            return "".join(collected) + " [STREAM INTERRUPTED]"
        raise

    result = "".join(collected)
    if not result and stop_reason != "end_turn":
        return f"[EMPTY RESPONSE: stop_reason={stop_reason}]"

    return result

# Expected Token Savings: partial text recovery avoids re-issuing full prompt
# Environment: agents using client.messages.stream() for real-time output
```

---

### Option 6 — Pydantic response wrapper with automatic validation

Wrap the raw API response in a Pydantic model that enforces non-empty text at parse time, surfacing errors as typed exceptions.

```python
import anthropic
from pydantic import BaseModel, field_validator, model_validator
from typing import Optional
import logging

log = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key="sk-live-...")


class SafeResponse(BaseModel):
    text: Optional[str]
    stop_reason: Optional[str]
    input_tokens: int
    output_tokens: int
    is_complete: bool

    @field_validator("text")
    @classmethod
    def text_or_none(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def check_complete_has_text(self) -> "SafeResponse":
        if self.is_complete and self.text is None:
            log.warning(
                "Response marked complete but text is None; stop_reason=%s",
                self.stop_reason,
            )
        return self


def wrap_response(response: anthropic.types.Message) -> SafeResponse:
    content = response.content or []
    text_blocks = [b.text for b in content if getattr(b, "type", None) == "text"]
    text = text_blocks[0] if text_blocks else None

    return SafeResponse(
        text=text,
        stop_reason=response.stop_reason,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        is_complete=(response.stop_reason == "end_turn"),
    )


def ask(prompt: str) -> SafeResponse:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    return wrap_response(response)


# Usage
result = ask("Summarize the water cycle in one sentence.")
if result.text:
    print(result.text)
else:
    print(f"No text produced. stop_reason={result.stop_reason}")

# Expected Token Savings: zero extra tokens; type-safe responses prevent downstream crashes
# Environment: production agents requiring typed contracts between components
```

---

## Comparison

| Option | Approach | Crash-Safe | Retryable | Streaming | Type-Safe |
|--------|----------|-----------|-----------|-----------|-----------|
| 1 | Defensive index + fallback | Yes | No | No | No |
| 2 | Stop-reason guard + retry | Yes | Yes | No | No |
| 3 | Central `extract_text()` helper | Yes | No | No | No |
| 4 | Async structured error events | Yes | No | No | Partial |
| 5 | Streaming null guard | Yes | Partial | Yes | No |
| 6 | Pydantic response wrapper | Yes | No | No | Yes |

**Recommended starting point:** Option 3 for synchronous agents (zero dependencies, easy to add to any existing code), Option 4 for async multi-task agents where a single crash must not kill sibling tasks.
