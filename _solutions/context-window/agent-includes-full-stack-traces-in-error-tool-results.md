---
layout: solution
title: "Agent Includes Full Stack Traces in Error Tool Results"
category: context-window
description: "When a tool call raises an exception, the agent includes the full Python traceback — often 30-60 lines — as the tool result. Over a multi-turn debugging session, accumulated stack traces consume thousands of tokens that add no value after the first occurrence."
tags: [context-window, token-cost, error-handling, tool-use, debugging]
---

## Symptom

A tool call fails and returns:

```
Traceback (most recent call last):
  File "/app/tools/database.py", line 147, in execute_query
    cursor.execute(sql, params)
  File "/usr/local/lib/python3.12/site-packages/psycopg2/extensions.py", line 217, in execute
    ...
  File "/usr/local/lib/python3.12/site-packages/psycopg2/__init__.py", line 122, in connect
    conn = _connect(dsn, connection_factory=connection_factory, **kwasync)
psycopg2.OperationalError: could not connect to server: Connection refused
	Is the server running on host "db.internal" (10.0.1.5) and accepting
	TCP/IP connections on port 5432?
```

That's 15 lines / ~400 tokens for a single error. Across 10 retries in a debugging session, that's 4,000 tokens of redundant traceback text the model doesn't need after the first occurrence.

## Root Cause

The exception handler captures and returns the full `traceback.format_exc()` output:

```python
import anthropic
import traceback

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: full traceback in tool result
def execute_tool(name: str, input_data: dict) -> str:
    try:
        return run_tool(name, input_data)
    except Exception:
        return traceback.format_exc()  # ← 30-60 lines every time
```

---

## Fix

### Option 1 — Return compact single-line error summaries

Extract only the error type and message, not the full traceback.

```python
import anthropic
import json
import traceback

client = anthropic.Anthropic(api_key="sk-live-...")


def compact_error(exc: Exception, include_location: bool = True) -> str:
    """
    Produce a compact single-line error summary.
    Format: ExceptionType: message (in module:line)
    """
    error_type = type(exc).__name__
    error_msg = str(exc).split("\n")[0][:200]  # First line, max 200 chars

    if include_location:
        tb = traceback.extract_tb(exc.__traceback__)
        if tb:
            frame = tb[-1]  # Innermost frame (where error occurred)
            location = f" (in {frame.filename.split('/')[-1]}:{frame.lineno})"
        else:
            location = ""
    else:
        location = ""

    return f"{error_type}: {error_msg}{location}"


def execute_tool_compact(name: str, input_data: dict) -> str:
    """Execute tool and return compact error summary on failure."""
    try:
        # Simulated tool execution
        if name == "query_database":
            raise ConnectionError("could not connect to server: Connection refused")
        return json.dumps({"result": "ok"})
    except Exception as e:
        summary = compact_error(e)
        print(f"[compact-error] {summary}")
        return json.dumps({
            "error": summary,
            "tool": name,
            "retryable": isinstance(e, (ConnectionError, TimeoutError))
        })


# Compare: full traceback vs compact
try:
    raise ConnectionError("could not connect to db.internal:5432")
except Exception as e:
    full = traceback.format_exc()
    compact = compact_error(e)
    print(f"Full traceback:  {len(full)} chars")
    print(f"Compact error:   {len(compact)} chars")
    print(f"Reduction: {100 * (1 - len(compact)/len(full)):.0f}%")
    print(f"\nCompact: {compact}")

# Expected Token Savings: 95% reduction in error token size; 10 retries = ~3,600 tokens saved
# Environment: any agent with tool error handling; debugging agents; production pipelines
```

---

### Option 2 — First-occurrence full trace, subsequent occurrences compact

Show the full traceback the first time an error type is seen, then switch to compact summaries for repeats.

```python
import anthropic
import json
import traceback
from collections import defaultdict

client = anthropic.Anthropic(api_key="sk-live-...")

# Track which error types have been seen in this session
_seen_error_types: dict[str, int] = defaultdict(int)
FULL_TRACE_ON_FIRST = True


def smart_error_format(exc: Exception, tool_name: str) -> str:
    """
    Return full traceback on first occurrence of each error type.
    Return compact summary for subsequent occurrences.
    """
    error_key = f"{tool_name}:{type(exc).__name__}"
    occurrence = _seen_error_types[error_key]
    _seen_error_types[error_key] += 1

    error_type = type(exc).__name__
    error_msg = str(exc).split("\n")[0][:200]

    if occurrence == 0 and FULL_TRACE_ON_FIRST:
        # First occurrence — full detail
        tb_lines = traceback.format_exc().strip().split("\n")
        # Limit to 15 lines even on first occurrence
        if len(tb_lines) > 15:
            tb_lines = tb_lines[:6] + ["  ... (truncated) ..."] + tb_lines[-4:]
        return json.dumps({
            "error": f"{error_type}: {error_msg}",
            "traceback": "\n".join(tb_lines),
            "occurrence": 1,
            "tool": tool_name
        })
    else:
        # Repeat occurrence — compact only
        return json.dumps({
            "error": f"{error_type}: {error_msg}",
            "occurrence": occurrence + 1,
            "note": "Same error type seen before — traceback omitted. See first occurrence.",
            "tool": tool_name
        })


def execute_with_smart_errors(name: str, input_data: dict) -> str:
    try:
        if name == "query_database":
            raise ConnectionError("db.internal:5432 connection refused")
        return json.dumps({"ok": True})
    except Exception as e:
        return smart_error_format(e, name)


# First call — full trace
print("=== First error ===")
r1 = execute_with_smart_errors("query_database", {})
print(json.loads(r1).get("error"))
print(f"Size: {len(r1)} chars")

# Repeated error — compact
print("\n=== Repeated error ===")
r2 = execute_with_smart_errors("query_database", {})
print(json.loads(r2).get("error"))
print(f"Size: {len(r2)} chars")
print(f"Savings on repeat: {100 * (1 - len(r2)/len(r1)):.0f}%")

# Expected Token Savings: 90%+ savings on repeated errors; first occurrence retains full debug info
# Environment: retry loops; debugging sessions where the same error recurs multiple times
```

---

### Option 3 — Error fingerprinting: store traces in a sidecar, reference by ID

Store full tracebacks in an in-memory sidecar indexed by a short hash ID. Tool results carry only the ID.

```python
import anthropic
import json
import traceback
import hashlib
import time
from pathlib import Path

client = anthropic.Anthropic(api_key="sk-live-...")

ERROR_STORE: dict[str, dict] = {}  # error_id → full error details
ERROR_LOG = Path("/tmp/agent_errors.jsonl")


def store_error(exc: Exception, tool_name: str, input_data: dict) -> str:
    """Store full error details; return a short reference ID."""
    tb_text = traceback.format_exc()
    error_type = type(exc).__name__
    error_msg = str(exc)[:500]

    # Fingerprint: hash of error type + first traceback frame
    fingerprint_input = f"{error_type}:{error_msg[:100]}"
    error_id = hashlib.sha256(fingerprint_input.encode()).hexdigest()[:8]

    if error_id not in ERROR_STORE:
        ERROR_STORE[error_id] = {
            "id": error_id,
            "type": error_type,
            "message": error_msg,
            "traceback": tb_text,
            "tool": tool_name,
            "input_preview": str(input_data)[:100],
            "first_seen": time.time(),
            "count": 0
        }
        # Append to error log for external access
        with ERROR_LOG.open("a") as f:
            entry = {k: v for k, v in ERROR_STORE[error_id].items() if k != "traceback"}
            f.write(json.dumps(entry) + "\n")

    ERROR_STORE[error_id]["count"] += 1
    ERROR_STORE[error_id]["last_seen"] = time.time()

    return error_id


def lookup_error(error_id: str) -> dict | None:
    """Retrieve full error details by ID."""
    return ERROR_STORE.get(error_id)


def execute_with_error_ref(name: str, input_data: dict) -> str:
    """Execute tool; on error, return compact reference not full traceback."""
    try:
        if name == "parse_json":
            json.loads("{bad json")  # Trigger an error
        return json.dumps({"ok": True})
    except Exception as e:
        error_id = store_error(e, name, input_data)
        count = ERROR_STORE[error_id]["count"]
        return json.dumps({
            "error": f"{type(e).__name__}: {str(e)[:80]}",
            "error_id": error_id,
            "occurrence": count,
            "details": f"Full traceback stored as error #{error_id}"
        })


# Tool result is compact regardless of traceback size
result1 = execute_with_error_ref("parse_json", {"text": "{bad"})
result2 = execute_with_error_ref("parse_json", {"text": "{also bad"})  # Same error type

print(f"Result 1 ({len(result1)} chars): {result1}")
print(f"Result 2 ({len(result2)} chars): {result2}")

# Agent can look up details when needed
error_id = json.loads(result1)["error_id"]
details = lookup_error(error_id)
if details:
    print(f"\nFull traceback for #{error_id} (available on demand):")
    print(details["traceback"][:200] + "...")

# Expected Token Savings: tool results stay <100 chars; full traces available on demand
# Environment: long debugging sessions; agents with error analysis tools
```

---

### Option 4 — Structured error schema with actionable fields only

Return errors as structured JSON with only fields the model can act on: error type, message, retryable flag, and suggested fix.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# Map exception types to agent-actionable metadata
ERROR_METADATA = {
    "ConnectionError":    {"retryable": True,  "category": "network",   "suggestion": "Wait 5s and retry; check service health"},
    "TimeoutError":       {"retryable": True,  "category": "network",   "suggestion": "Increase timeout or retry with smaller payload"},
    "PermissionError":    {"retryable": False, "category": "auth",      "suggestion": "Check API key permissions and scopes"},
    "ValueError":         {"retryable": False, "category": "input",     "suggestion": "Fix input data format; see 'message' for details"},
    "KeyError":           {"retryable": False, "category": "schema",    "suggestion": "Check that required fields exist in response"},
    "json.JSONDecodeError": {"retryable": False, "category": "parsing", "suggestion": "Response was not valid JSON; check API response format"},
}


def structured_error(exc: Exception, tool_name: str, input_preview: str = "") -> str:
    """Return a minimal, actionable error structure without stack trace."""
    error_type = type(exc).__name__
    meta = ERROR_METADATA.get(error_type, {
        "retryable": False,
        "category": "unknown",
        "suggestion": "Inspect error message and adjust approach"
    })

    return json.dumps({
        "ok": False,
        "error_type": error_type,
        "message": str(exc).split("\n")[0][:150],
        "tool": tool_name,
        "retryable": meta["retryable"],
        "category": meta["category"],
        "suggestion": meta["suggestion"]
    })


def execute_structured(name: str, input_data: dict) -> str:
    try:
        if name == "fetch_api":
            raise TimeoutError("Request to api.example.com timed out after 30s")
        if name == "validate":
            raise ValueError("Field 'amount' must be positive; got -5")
        return json.dumps({"ok": True})
    except Exception as e:
        return structured_error(e, name, str(input_data)[:50])


# Show how compact and actionable this is
for tool in ["fetch_api", "validate"]:
    result = execute_structured(tool, {"input": "test"})
    print(f"{tool}: {result}")
    print(f"  Size: {len(result)} chars\n")

# Expected Token Savings: 50-100 chars vs 500-2000 for full tracebacks; model gets actionable metadata
# Environment: production agents; any tool with known failure modes and prescribed remediation steps
```

---

### Option 5 — Tool result size budget: auto-truncate oversized errors

Enforce a maximum size for any tool result. Errors exceeding the budget are automatically truncated to the most informative lines.

```python
import anthropic
import json
import traceback
import re

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_TOOL_RESULT_CHARS = 500  # Hard budget per tool result


def smart_truncate_traceback(tb_text: str, max_chars: int) -> str:
    """
    Intelligently truncate a traceback:
    1. Keep the exception line (last line) — always
    2. Keep the innermost frame — most relevant
    3. Fill remaining budget with top frames
    """
    lines = tb_text.strip().split("\n")
    if not lines:
        return tb_text[:max_chars]

    # Last line is the exception
    exception_line = lines[-1]
    # Find innermost frame (last "File ..." line)
    frame_lines = [l for l in lines if l.strip().startswith("File ")]
    innermost = frame_lines[-1] if frame_lines else ""

    # Build compacted version
    core = f"{exception_line}\n  {innermost}" if innermost else exception_line

    if len(core) >= max_chars:
        return core[:max_chars]

    # Fill remaining with first frames
    remaining = max_chars - len(core) - 20  # 20 char buffer for ellipsis
    prefix_lines = lines[:4]
    prefix = "\n".join(prefix_lines)
    if len(prefix) <= remaining:
        return f"{prefix}\n  ... (truncated) ...\n{core}"

    return f"... (truncated) ...\n{core}"


def bounded_tool_result(result: str | dict, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Ensure any tool result stays within the size budget."""
    if isinstance(result, dict):
        text = json.dumps(result)
    else:
        text = str(result)

    if len(text) <= max_chars:
        return text

    # Check if it looks like a traceback
    if "Traceback (most recent call last)" in text:
        truncated = smart_truncate_traceback(text, max_chars)
        return truncated

    # Generic truncation with marker
    return text[:max_chars - 20] + "\n... [truncated]"


def execute_with_budget(name: str, input_data: dict) -> str:
    try:
        if name == "run_script":
            # Simulate a deeply nested exception
            raise RuntimeError(
                "Database connection pool exhausted after 30s\n"
                "Additional context: all 20 connections busy\n"
                "Last query: SELECT * FROM very_large_table"
            )
        return json.dumps({"ok": True})
    except Exception:
        raw_tb = traceback.format_exc()
        # Apply budget
        bounded = bounded_tool_result(raw_tb)
        return json.dumps({"error": bounded, "tool": name})


result = execute_with_budget("run_script", {})
parsed = json.loads(result)
print(f"Error ({len(parsed['error'])} chars):\n{parsed['error']}")

# Expected Token Savings: hard budget guarantees no single error > 500 chars; loop of 10 errors < 5K tokens
# Environment: agents with strict context budgets; production error reporting pipelines
```

---

### Option 6 — Post-turn error summarisation: compress historical errors before next model call

After accumulating several error tool results in the conversation, summarise them into a compact error digest before the next model turn.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_ERROR_RESULTS_BEFORE_COMPRESS = 3   # Compress after 3 error tool results
ERROR_RESULT_SIZE_THRESHOLD = 300        # Compress results larger than this


def is_error_result(tool_result: dict) -> bool:
    """Detect if a tool result contains an error."""
    content = tool_result.get("content", "")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return "error" in parsed or "traceback" in parsed.get("error", "").lower()
        except (json.JSONDecodeError, AttributeError):
            return "error" in content.lower() or "traceback" in content.lower()
    return False


def compress_error_results(messages: list[dict]) -> tuple[list[dict], int]:
    """
    Scan message history for large error tool results and compress them.
    Returns (updated_messages, tokens_saved_estimate).
    """
    error_results = []
    total_error_chars = 0

    for msg in messages:
        if msg["role"] == "user" and isinstance(msg["content"], list):
            for item in msg["content"]:
                if item.get("type") == "tool_result" and is_error_result(item):
                    content_len = len(str(item.get("content", "")))
                    if content_len > ERROR_RESULT_SIZE_THRESHOLD:
                        error_results.append(item)
                        total_error_chars += content_len

    if len(error_results) < MAX_ERROR_RESULTS_BEFORE_COMPRESS:
        return messages, 0

    # Ask Claude to summarise the error pattern
    error_texts = [str(r.get("content", "")) for r in error_results]
    summary_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system="Summarise the following tool errors in 2-3 lines. Focus on root cause and pattern.",
        messages=[{
            "role": "user",
            "content": f"Errors to summarise:\n\n" + "\n\n---\n\n".join(error_texts[:5])
        }]
    )
    summary = summary_response.content[0].text.strip()

    # Replace the first error result with the summary; remove subsequent ones
    compressed = []
    replaced = False
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg["content"], list):
            new_content = []
            for item in msg["content"]:
                if item.get("type") == "tool_result" and item in error_results:
                    if not replaced:
                        new_content.append({
                            **item,
                            "content": json.dumps({
                                "error_summary": summary,
                                "original_errors_compressed": len(error_results)
                            })
                        })
                        replaced = True
                    # Skip subsequent error results (compressed into summary)
                else:
                    new_content.append(item)
            compressed.append({**msg, "content": new_content})
        else:
            compressed.append(msg)

    tokens_saved = total_error_chars // 4  # Rough token estimate
    return compressed, tokens_saved


# Demo: show compression on a message list with repeated errors
FAKE_TRACEBACK = "Traceback (most recent call last):\n" + ("  File 'app.py', line 42, in handler\n" * 8) + "ConnectionError: db.internal:5432 refused\n"

messages = [
    {"role": "user", "content": "Debug the database connection issue"},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "query_db", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"error": FAKE_TRACEBACK})}]},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "query_db", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": json.dumps({"error": FAKE_TRACEBACK})}]},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t3", "name": "query_db", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "content": json.dumps({"error": FAKE_TRACEBACK})}]},
]

before_chars = sum(len(json.dumps(m)) for m in messages)
compressed_msgs, saved_tokens = compress_error_results(messages)
after_chars = sum(len(json.dumps(m)) for m in compressed_msgs)

print(f"Before: {before_chars:,} chars | After: {after_chars:,} chars")
print(f"Reduction: {100 * (1 - after_chars/before_chars):.0f}% ({saved_tokens} tokens saved)")

# Expected Token Savings: compressing 3+ repeated tracebacks saves 2,000-6,000 tokens per session
# Environment: debugging agents; pipelines prone to repeated errors before root cause is fixed
```

---

## Comparison

| Option | Token Size | Loses Debug Info | Handles Repeats | Complexity |
|--------|-----------|-----------------|-----------------|------------|
| 1 | ~30 chars | Some | No (always compact) | Low |
| 2 | First: ~400, repeat: ~60 | No | Yes | Low |
| 3 | ~80 chars (ID ref) | No (sidecar) | Yes | Medium |
| 4 | ~200 chars | Some (structured) | No | Low |
| 5 | ≤500 chars | Partial | No | Low |
| 6 | Compressed digest | Partial | Yes (batches) | Medium |

**Recommended starting point:** Option 1 (compact single-line errors) for all tools — a 3-line change to the exception handler that reduces error result size by 95%. Combine with Option 2's first-occurrence logic to retain full detail when it's genuinely useful. Use Option 3 (error ID sidecar) in long debugging sessions where the full traceback may be needed later without re-consuming context.
