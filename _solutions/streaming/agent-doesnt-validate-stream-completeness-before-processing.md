---
layout: solution
title: "Agent Doesn't Validate Stream Completeness Before Processing"
category: streaming
description: "The agent processes partial streamed output as if it were complete, producing truncated JSON, cut-off code, or half-finished summaries that downstream consumers silently misinterpret."
tags: [streaming, validation, reliability, production, json]
---

## Symptom

The agent streams a response and passes chunks to a downstream parser as they arrive. When the stream ends early — due to a network drop, `max_tokens` being hit, or a server-side stop — the consumer receives a partial JSON object, a truncated function call, or an incomplete sentence. The parser either crashes, silently accepts corrupted data, or produces wrong output that propagates downstream.

## Root Cause

The Anthropic streaming API signals completion via `stop_reason`. When `stop_reason == "max_tokens"`, the model was cut off; when `stop_reason == "end_turn"`, it finished naturally. Without checking this field, the agent treats every stream as complete. Similarly, network-level disconnections raise exceptions that, if swallowed, leave the accumulated buffer in an indeterminate state.

## Fix

### Option 1 — Check stop_reason before using streamed output

```python
import anthropic

client = anthropic.Anthropic()

def stream_validated(prompt: str, max_tokens: int = 256) -> str | None:
    accumulated = ""
    stop_reason = None

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for delta in stream.text_stream:
            accumulated += delta
            print(delta, end="", flush=True)
        # Access final message after stream closes
        final = stream.get_final_message()
        stop_reason = final.stop_reason

    print()

    if stop_reason != "end_turn":
        print(f"[warn] stream stopped with reason={stop_reason!r} — output may be incomplete")
        return None  # Caller decides whether to retry or use partial output

    return accumulated

result = stream_validated("Write a Python function to parse ISO 8601 dates.", max_tokens=512)
if result:
    print(f"[ok] {len(result)} chars, complete output")
else:
    print("[retry] output was truncated — increase max_tokens or retry")
```

**Expected Token Savings:** Prevents downstream processing of truncated output that would produce wrong results, saving the tokens of any follow-up correction call.
**Environment:** Any streaming agent; essential when output is parsed (JSON, code, structured data) rather than displayed directly to a user.

---

### Option 2 — JSON-aware stream validator: detect completeness from bracket balance

```python
import anthropic
import json

client = anthropic.Anthropic()

def is_json_complete(text: str) -> bool:
    """Returns True if text is valid, complete JSON."""
    try:
        json.loads(text.strip())
        return True
    except json.JSONDecodeError:
        return False

def stream_json(prompt: str, max_retries: int = 2) -> dict | None:
    system = (
        "Respond with valid JSON only. No markdown, no explanation. "
        'Example: {"key": "value"}'
    )
    for attempt in range(max_retries + 1):
        accumulated = ""
        stop_reason = None

        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for delta in stream.text_stream:
                accumulated += delta
            stop_reason = stream.get_final_message().stop_reason

        if stop_reason != "end_turn":
            print(f"[attempt {attempt+1}] truncated (stop_reason={stop_reason}), retrying")
            continue

        if not is_json_complete(accumulated):
            print(f"[attempt {attempt+1}] JSON incomplete despite end_turn, retrying")
            continue

        return json.loads(accumulated.strip())

    print("[error] failed to get complete JSON after retries")
    return None

result = stream_json(
    'Return a JSON object with fields: name (string), age (integer), skills (list of strings). '
    'Make up a fictional software engineer.'
)
if result:
    print("Parsed:", result)
```

**Expected Token Savings:** Catching truncated JSON before it reaches the parser avoids silent data corruption and costly downstream error handling.
**Environment:** Agents that stream structured JSON responses; tool-call argument parsing; any pipeline where output feeds a JSON parser.

---

### Option 3 — asyncio streaming with completeness assertion and typed result

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class StreamResult:
    text:        str
    stop_reason: str
    input_tokens:  int
    output_tokens: int

    @property
    def is_complete(self) -> bool:
        return self.stop_reason == "end_turn"

    @property
    def was_truncated(self) -> bool:
        return self.stop_reason == "max_tokens"

async def stream_with_result(prompt: str, max_tokens: int = 512) -> StreamResult:
    accumulated = ""
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for delta in stream.text_stream:
            accumulated += delta
        final = await stream.get_final_message()

    return StreamResult(
        text=accumulated,
        stop_reason=final.stop_reason,
        input_tokens=final.usage.input_tokens,
        output_tokens=final.usage.output_tokens,
    )

async def safe_process(prompt: str) -> str | None:
    result = await stream_with_result(prompt)

    if result.was_truncated:
        # Retry with higher limit
        print(f"[truncated] retrying with 2× max_tokens")
        result = await stream_with_result(prompt, max_tokens=1024)

    if not result.is_complete:
        print(f"[error] unexpected stop_reason={result.stop_reason!r}")
        return None

    print(f"[ok] {result.output_tokens} output tokens, stop={result.stop_reason}")
    return result.text

async def main():
    prompts = [
        "List 10 software design patterns with one-sentence descriptions.",
        "Explain the CAP theorem in detail.",
        "Write a Python class for a binary search tree.",
    ]
    results = await asyncio.gather(*[safe_process(p) for p in prompts])
    for p, r in zip(prompts, results):
        status = "ok" if r else "failed"
        print(f"[{status}] {p[:50]}")

asyncio.run(main())
```

**Expected Token Savings:** Typed `StreamResult.was_truncated` makes retry logic explicit; automatic 2× retry only fires when genuinely needed rather than on every call.
**Environment:** Async agents processing multiple parallel streams; pipelines where each stream result feeds a downstream stage.

---

### Option 4 — Sentinel pattern: model confirms completion in output

```python
import anthropic

client = anthropic.Anthropic()

SENTINEL = "[[COMPLETE]]"

def stream_with_sentinel(prompt: str) -> str | None:
    system = (
        f"Complete your response fully, then output exactly '{SENTINEL}' on the last line. "
        "This signals to the system that your response is complete."
    )
    accumulated = ""
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=768,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for delta in stream.text_stream:
            accumulated += delta

    if SENTINEL in accumulated:
        clean = accumulated.replace(SENTINEL, "").rstrip()
        print(f"[sentinel] confirmed complete ({len(clean)} chars)")
        return clean
    else:
        print(f"[sentinel] MISSING — output is incomplete. Got {len(accumulated)} chars")
        return None

result = stream_with_sentinel(
    "List all the steps to deploy a Python web app to AWS ECS, from Dockerfile to load balancer."
)
if result:
    print(result[:200])
```

**Expected Token Savings:** Sentinel adds ~5 tokens overhead but eliminates the cost of processing and correcting silently truncated output; works even when `stop_reason` is ambiguous.
**Environment:** Long-form generation tasks where the model may naturally stop mid-list or mid-sentence; batch jobs where output completeness must be verified before writing to storage.

---

### Option 5 — Stream length budget: warn when output approaches max_tokens

```python
import anthropic

client = anthropic.Anthropic()

def stream_with_budget_warning(
    prompt: str,
    max_tokens: int = 512,
    warn_threshold: float = 0.85,
) -> tuple[str, bool]:
    """
    Returns (text, is_complete).
    Emits a warning when output is approaching max_tokens.
    """
    accumulated = ""
    output_token_estimate = 0  # rough: 1 token ≈ 4 chars
    warned = False

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for delta in stream.text_stream:
            accumulated += delta
            output_token_estimate = len(accumulated) // 4

            if not warned and output_token_estimate > max_tokens * warn_threshold:
                print(
                    f"\n[budget] ~{output_token_estimate} tokens used "
                    f"({output_token_estimate/max_tokens:.0%} of {max_tokens} limit)"
                )
                warned = True

        final = stream.get_final_message()
        stop_reason = final.stop_reason
        actual_output = final.usage.output_tokens

    is_complete = stop_reason == "end_turn"
    if not is_complete:
        print(
            f"[budget] TRUNCATED — used {actual_output}/{max_tokens} tokens "
            f"(stop_reason={stop_reason!r})"
        )
    return accumulated, is_complete

text, ok = stream_with_budget_warning(
    "Write a comprehensive guide to Python async/await with examples.",
    max_tokens=300,  # intentionally low to trigger truncation
)
print(f"\n[result] complete={ok}, length={len(text)}")
```

**Expected Token Savings:** Budget warnings let operators catch undersized `max_tokens` settings before they silently corrupt output in production.
**Environment:** Monitoring and alerting pipelines; dev/staging environments where you want to tune max_tokens before going to production.

---

### Option 6 — Stream aggregator with schema validation post-stream

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

def validate_schema(data: dict, required_keys: list[str]) -> list[str]:
    """Returns list of missing required keys."""
    return [k for k in required_keys if k not in data or data[k] is None]

def stream_structured(
    prompt: str,
    required_keys: list[str],
    max_tokens: int = 512,
    max_retries: int = 2,
) -> dict | None:
    system = (
        f"Respond with a JSON object containing these fields: {', '.join(required_keys)}. "
        "Output valid JSON only. No markdown fences."
    )
    for attempt in range(max_retries + 1):
        accumulated = ""
        stop_reason = None

        try:
            with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for delta in stream.text_stream:
                    accumulated += delta
                stop_reason = stream.get_final_message().stop_reason
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            print(f"[attempt {attempt+1}] network error: {exc}")
            continue

        if stop_reason != "end_turn":
            print(f"[attempt {attempt+1}] truncated (stop={stop_reason!r})")
            continue

        try:
            data: dict[str, Any] = json.loads(accumulated.strip())
        except json.JSONDecodeError as e:
            print(f"[attempt {attempt+1}] JSON parse error: {e}")
            continue

        missing = validate_schema(data, required_keys)
        if missing:
            print(f"[attempt {attempt+1}] schema missing keys: {missing}")
            continue

        print(f"[ok] attempt={attempt+1}, keys={list(data.keys())}")
        return data

    return None

result = stream_structured(
    "Describe the Python programming language.",
    required_keys=["name", "year_created", "creator", "primary_use_cases", "current_version"],
)
if result:
    print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Schema validation catches structurally incomplete responses (missing keys) that `stop_reason == "end_turn"` alone cannot detect; retry only fires on genuine failures.
**Environment:** Data extraction pipelines; agents producing structured records for databases; ETL agents where every required field must be present.

---

## Comparison

| Option | Completeness Check | Handles Truncation | Handles Bad JSON | Async Safe | Best For |
|---|---|---|---|---|---|
| 1. stop_reason check | API field | Retry / return None | No | No | Simple baseline; any streaming agent |
| 2. JSON bracket balance | Syntax validation | Retry | Yes | No | JSON-output agents |
| 3. Typed StreamResult | API field + typed | Auto 2× retry | No | Yes | Async parallel streams |
| 4. Sentinel token | Model-confirmed | Detect from output | No | No | Long-form; ambiguous stop signals |
| 5. Budget warning | Token estimate | Warns, doesn't retry | No | No | Monitoring; max_tokens tuning |
| 6. Schema validation | API field + keys | Retry | Yes | No | Structured extraction; ETL pipelines |
