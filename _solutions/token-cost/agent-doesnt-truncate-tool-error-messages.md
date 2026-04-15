---
layout: solution
title: "Agent doesn't truncate tool error messages"
category: token-cost
description: "Agent passes full stack traces, verbose HTTP error bodies, and multi-KB exception dumps as tool_result content. These fill the context window with noise the model cannot act on, inflating token costs on every subsequent turn."
tags: [token-cost, tool-failure, error-handling, context-window, truncation]
---

## Symptom

A tool call fails and the raw exception — including a full Python traceback, a 10 KB HTTP response body, or a verbose database error with internal query details — is stored verbatim as the `tool_result` content. The model then includes this on every subsequent API call, paying the full token cost repeatedly even though the important information is the first two lines.

## Root Cause

Tool result handlers return `str(exception)` or the full response text without any length cap. A `requests.exceptions.ConnectionError` can produce 2 KB of traceback. A failed SQL query can return the entire query plan. A 500 response from an API can return a full HTML error page. None of this extra content helps the model decide what to do next — it only burns tokens.

## Fix

Normalize error messages before returning them as tool results. Keep only the actionable signal: error type, message, and optionally the first relevant frame. Strip everything else.

---

### Option 1 — Hard character cap on tool result content

```python
import anthropic
import traceback

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_TOOL_RESULT_CHARS = 500   # ~125 tokens


def truncate_result(content: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(content) <= max_chars:
        return content
    truncated = content[:max_chars]
    removed = len(content) - max_chars
    return f"{truncated}\n... [{removed} characters truncated]"


def run_tool(tool_name: str, tool_input: dict) -> str:
    """Dispatch a tool call and return a bounded result string."""
    try:
        if tool_name == "query_database":
            # Simulate a failing DB call
            raise RuntimeError(
                "psycopg2.errors.UndefinedTable: relation \"users_v2\" does not exist\n"
                "LINE 1: SELECT * FROM users_v2 WHERE id = $1\n"
                "                      ^\n"
                + "Stack trace:\n" + "  File ... \n" * 20
            )
        return f"Tool {tool_name} succeeded."

    except Exception as exc:
        # Only keep the first line of the exception message
        short_msg = str(exc).splitlines()[0]
        error_content = f"ERROR [{type(exc).__name__}]: {short_msg}"
        return truncate_result(error_content)


TOOLS = [
    {
        "name": "query_database",
        "description": "Run a SQL query.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    }
]


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for _ in range(5):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    content = run_tool(block.name, block.input)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    })
            messages.append({"role": "user", "content": results})

    return ""
```

**Expected Token Savings:** 80–95 % per error tool result; a 2 KB traceback becomes a 50-character summary; savings compound on every subsequent turn.
**Environment:** Any agent with tools that can raise exceptions; the simplest fix with no dependencies.

---

### Option 2 — Structured error normalizer with type classification

```python
import anthropic
import re
from dataclasses import dataclass
from typing import Callable

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class NormalizedError:
    error_type: str      # Python exception class name
    message: str         # First meaningful line
    hint: str = ""       # Optional: extracted actionable hint
    retryable: bool = False

    def to_tool_result(self) -> str:
        parts = [f"ERROR [{self.error_type}]: {self.message}"]
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        parts.append(f"Retryable: {self.retryable}")
        return "\n".join(parts)


def normalize_exception(exc: Exception) -> NormalizedError:
    cls_name = type(exc).__name__
    full_msg = str(exc)
    first_line = full_msg.splitlines()[0].strip()[:200]

    # Classify and extract hints
    if "connection" in cls_name.lower() or "timeout" in cls_name.lower():
        return NormalizedError(cls_name, first_line, "Check network connectivity.", retryable=True)

    if "does not exist" in full_msg.lower() or "undefined" in full_msg.lower():
        # Extract the object name from DB errors
        match = re.search(r'relation "([^"]+)" does not exist', full_msg)
        hint = f"Table '{match.group(1)}' not found." if match else "Object not found."
        return NormalizedError(cls_name, first_line, hint, retryable=False)

    if "permission" in full_msg.lower() or "403" in full_msg:
        return NormalizedError(cls_name, first_line, "Insufficient permissions.", retryable=False)

    if "rate limit" in full_msg.lower() or "429" in full_msg:
        return NormalizedError(cls_name, first_line, "Rate limited — wait before retrying.", retryable=True)

    return NormalizedError(cls_name, first_line, retryable=False)


def safe_run_tool(tool_fn: Callable, **kwargs: object) -> str:
    try:
        return str(tool_fn(**kwargs))
    except Exception as exc:
        return normalize_exception(exc).to_tool_result()
```

**Expected Token Savings:** Structured errors are ~50–80 tokens vs 500–2000 for raw exceptions; the `retryable` flag also prevents unnecessary retry loops.
**Environment:** Production agents where error taxonomy matters for downstream routing (retry vs escalate vs abort).

---

### Option 3 — HTTP response error truncator

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_BODY_CHARS = 300


def truncate_http_error(status_code: int, response_body: str, url: str = "") -> str:
    """
    Produce a compact error message from an HTTP error response.
    Strips HTML, truncates JSON, removes boilerplate.
    """
    body = response_body.strip()

    # Strip HTML error pages entirely — keep only the title if present
    if body.startswith("<!") or body.startswith("<html"):
        import re
        title_match = re.search(r"<title[^>]*>([^<]+)</title>", body, re.IGNORECASE)
        body = title_match.group(1).strip() if title_match else f"HTML error page ({len(body)} bytes)"

    # For JSON, keep only top-level error fields
    elif body.startswith("{"):
        try:
            data = json.loads(body)
            error_fields = {k: v for k, v in data.items() if "error" in k.lower() or "message" in k.lower() or "detail" in k.lower()}
            body = json.dumps(error_fields) if error_fields else json.dumps(data)[:MAX_BODY_CHARS]
        except json.JSONDecodeError:
            pass

    # Truncate whatever remains
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f"... [{len(response_body) - MAX_BODY_CHARS} chars omitted]"

    url_part = f" ({url})" if url else ""
    return f"HTTP {status_code}{url_part}: {body}"


def call_api_tool(endpoint: str, params: dict) -> str:
    """Simulated API tool that might return verbose errors."""
    import urllib.request
    import urllib.error

    try:
        # Simulate the call
        raise urllib.error.HTTPError(
            endpoint, 500,
            "Internal Server Error",
            {},  # type: ignore
            None,
        )
    except urllib.error.HTTPError as exc:
        # Simulate a verbose HTML error body
        html_body = "<html><head><title>500 Internal Server Error</title></head><body>" + "x" * 5000 + "</body></html>"
        return truncate_http_error(exc.code, html_body, endpoint)
    except Exception as exc:
        return f"ERROR: {str(exc)[:200]}"
```

**Expected Token Savings:** HTML error pages of 5–50 KB reduced to under 100 characters; JSON error bodies trimmed to the relevant fields only.
**Environment:** Agents that call external REST APIs; particularly valuable for third-party APIs that return verbose HTML 500 pages.

---

### Option 4 — Async tool runner with per-tool result budget

```python
import anthropic
import asyncio
from typing import Callable, Awaitable

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# Per-tool token budgets (in approximate characters)
TOOL_RESULT_BUDGETS: dict[str, int] = {
    "search_web": 2000,      # Search results can be long
    "query_database": 400,   # DB results should be compact
    "read_file": 3000,       # File content can be larger
    "call_api": 500,         # API responses: keep small
    "run_code": 1000,        # Code output: moderate
}
DEFAULT_BUDGET = 600


def apply_budget(tool_name: str, content: str) -> str:
    budget = TOOL_RESULT_BUDGETS.get(tool_name, DEFAULT_BUDGET)
    if len(content) <= budget:
        return content
    omitted = len(content) - budget
    return content[:budget] + f"\n[... {omitted} chars omitted — result truncated to budget]"


async def run_tool_async(tool_name: str, tool_input: dict) -> str:
    try:
        # Simulate slow async tool execution
        await asyncio.sleep(0.01)
        if tool_name == "query_database":
            # Simulate large result
            return "id,name,email\n" + "\n".join(
                f"{i},user{i},user{i}@example.com" for i in range(500)
            )
        return f"Result from {tool_name}: ok"

    except Exception as exc:
        error = f"ERROR [{type(exc).__name__}]: {str(exc).splitlines()[0][:150]}"
        return apply_budget(tool_name, error)

    # Apply budget to successful results too
    # (handled below in the caller)


async def run_agent_async(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]
    tools = [
        {
            "name": "query_database",
            "description": "Run a SQL query.",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        }
    ]

    for _ in range(5):
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_tasks = [
                run_tool_async(block.name, block.input)
                for block in response.content
                if block.type == "tool_use"
            ]
            raw_results = await asyncio.gather(*tool_tasks)

            results = []
            for block, raw in zip(
                [b for b in response.content if b.type == "tool_use"],
                raw_results,
            ):
                bounded = apply_budget(block.name, raw)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": bounded,
                })
            messages.append({"role": "user", "content": results})

    return ""
```

**Expected Token Savings:** Per-tool budgets prevent any single noisy tool from dominating the context; database results with 500 rows become a ~400-char summary.
**Environment:** Async multi-tool agents; the budget table lets you tune per-tool aggressiveness based on observed result sizes.

---

### Option 5 — Error deduplication: collapse repeated identical errors

```python
import anthropic
import hashlib
from collections import Counter

client = anthropic.Anthropic(api_key="sk-live-...")


class ErrorDeduplicator:
    """
    Track repeated tool errors across turns.
    After N identical errors, replace subsequent ones with a short reference.
    """

    def __init__(self, dedup_after: int = 2) -> None:
        self._counts: Counter[str] = Counter()
        self._dedup_after = dedup_after

    def _key(self, tool_name: str, error_msg: str) -> str:
        return hashlib.md5(f"{tool_name}:{error_msg[:100]}".encode()).hexdigest()[:8]

    def process(self, tool_name: str, result: str) -> str:
        if not result.startswith("ERROR"):
            return result

        key = self._key(tool_name, result)
        self._counts[key] += 1
        count = self._counts[key]

        if count == 1:
            return result   # First occurrence: pass through in full

        short = result.splitlines()[0][:120]
        return f"[Repeated error #{count}] {short}"


_dedup = ErrorDeduplicator(dedup_after=2)


def run_tool(tool_name: str, tool_input: dict) -> str:
    try:
        raise ConnectionError("Failed to connect to database: connection refused at 10.0.0.5:5432")
    except Exception as exc:
        short = f"ERROR [{type(exc).__name__}]: {str(exc).splitlines()[0][:150]}"
        return _dedup.process(tool_name, short)
```

**Expected Token Savings:** Repeated errors in multi-turn retry loops are collapsed from N×200 chars to N×40 chars; particularly valuable in loops that retry the same broken tool repeatedly.
**Environment:** Agents that retry failed tools multiple times; the deduplicator prevents context bloat from accumulating identical error messages.

---

### Option 6 — Global tool result middleware with configurable policies

```python
import anthropic
from dataclasses import dataclass
from enum import Enum
from typing import Callable

client = anthropic.Anthropic(api_key="sk-live-...")


class TruncationPolicy(Enum):
    HARD_CAP = "hard_cap"
    FIRST_LINE = "first_line"
    STRUCTURED = "structured"
    PASSTHROUGH = "passthrough"


@dataclass
class ToolPolicy:
    success_max_chars: int = 2000
    error_policy: TruncationPolicy = TruncationPolicy.FIRST_LINE
    error_max_chars: int = 300
    include_traceback_lines: int = 0   # 0 = no traceback


TOOL_POLICIES: dict[str, ToolPolicy] = {
    "search_web": ToolPolicy(success_max_chars=3000, error_max_chars=200),
    "query_database": ToolPolicy(success_max_chars=800, error_max_chars=200),
    "run_code": ToolPolicy(success_max_chars=2000, error_max_chars=500, include_traceback_lines=3),
    "call_api": ToolPolicy(success_max_chars=600, error_max_chars=200),
}
DEFAULT_POLICY = ToolPolicy()


def apply_policy(tool_name: str, result: str, is_error: bool) -> str:
    policy = TOOL_POLICIES.get(tool_name, DEFAULT_POLICY)

    if is_error:
        lines = result.splitlines()
        if policy.error_policy == TruncationPolicy.FIRST_LINE:
            kept = lines[:1 + policy.include_traceback_lines]
            result = "\n".join(kept)
        max_chars = policy.error_max_chars
    else:
        max_chars = policy.success_max_chars

    if len(result) > max_chars:
        omitted = len(result) - max_chars
        result = result[:max_chars] + f"\n[{omitted} chars omitted]"

    return result


def tool_middleware(
    tool_fn: Callable[..., str],
    tool_name: str,
) -> Callable[..., str]:
    """Wrap any tool function with truncation middleware."""
    def wrapper(**kwargs: object) -> str:
        try:
            raw = tool_fn(**kwargs)
            return apply_policy(tool_name, raw, is_error=False)
        except Exception as exc:
            error = f"ERROR [{type(exc).__name__}]: {str(exc)}"
            return apply_policy(tool_name, error, is_error=True)
    return wrapper


# Comparison table
# | Option | Technique | Handles Errors | Handles Success | Complexity |
# |--------|-----------|----------------|-----------------|------------|
# | 1 Hard cap | char limit | Yes | Yes | Minimal |
# | 2 Structured normalizer | type classification | Yes | No | Low |
# | 3 HTTP truncator | HTML/JSON parse | Yes | No | Medium |
# | 4 Async per-tool budget | budget dict | Yes | Yes | Medium |
# | 5 Error deduplicator | hash + counter | Yes | No | Low |
# | 6 Middleware + policy | dataclass config | Yes | Yes | Medium |
```

**Expected Token Savings:** Policies applied to all tools; a noisy tool that previously emitted 5 KB per call becomes 300 chars; across 10 turns with 3 tool calls each, savings can exceed 100K characters.
**Environment:** Any production agent with multiple tools; the policy table makes tuning per-tool without touching tool implementation code.
