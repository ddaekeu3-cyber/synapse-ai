---
layout: solution
title: "Agent Truncates Long Tool Arguments Without Warning"
category: tool-failure
description: "Agent silently truncates SQL queries, file paths, or prompt strings when they exceed a character limit, producing corrupted tool calls with no error signal."
tags: [tool-failure, truncation, validation, reliability, production]
---

## Symptom

A tool call succeeds at the API level but returns wrong or partial results. SQL queries are cut mid-clause, file paths are truncated to invalid locations, or prompt strings end mid-sentence. The agent receives a tool result and continues reasoning as if the call succeeded, producing downstream errors or hallucinated completions that are hard to trace back to the truncation.

## Root Cause

LLM output is bounded by `max_tokens`. When a tool argument is assembled from template + dynamic content and the combined length exceeds the model's generation budget, the JSON output is silently cut short. The Anthropic SDK parses whatever tokens were produced — if the JSON happens to close cleanly, the truncated string is passed to the tool verbatim. No exception is raised; the tool receives a shorter-than-expected argument and either runs on corrupted input or returns an error that the agent misinterprets.

## Fix

### Option 1 — Length check before tool call

```python
import anthropic
import json

client = anthropic.Anthropic()

MAX_SQL_CHARS   = 4000   # safe limit for your database driver
MAX_PROMPT_CHARS = 2000

def safe_run_sql(query: str) -> dict:
    """Validate query length before execution."""
    if len(query) > MAX_SQL_CHARS:
        return {
            "error": "query_too_long",
            "message": (
                f"Query is {len(query)} characters; limit is {MAX_SQL_CHARS}. "
                "Split into smaller queries or use a subquery."
            ),
            "length": len(query),
            "limit": MAX_SQL_CHARS,
        }
    # -- execute query here --
    print(f"[sql] executing {len(query)}-char query")
    return {"rows": [], "status": "ok"}

tools = [
    {
        "name": "run_sql",
        "description": "Execute a SQL query. Maximum 4000 characters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SQL query to run. Must be under 4000 characters.",
                    "maxLength": MAX_SQL_CHARS,
                }
            },
            "required": ["query"],
        },
    }
]

def run_agent(user_request: str) -> None:
    messages = [{"role": "user", "content": user_request}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )
        if response.stop_reason != "tool_use":
            print(f"[agent] {response.content[0].text}")
            break
        for block in response.content:
            if block.type == "tool_use" and block.name == "run_sql":
                result = safe_run_sql(block.input["query"])
                messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": block.id,
                                 "content": json.dumps(result)}],
                })

run_agent("List all orders from the last 90 days grouped by customer region and product category with subtotals.")
```

**Expected Token Savings:** Catching truncation before execution avoids wasted retry cycles; the agent receives a structured error it can act on (split the query) rather than a partial result it has to re-explain.
**Environment:** Database query tools, shell command tools, file-write tools; any tool that accepts long string arguments.

---

### Option 2 — Split-and-batch strategy for oversized arguments

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

CHUNK_SIZE = 1500   # characters per chunk

def chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Split text into overlapping chunks to preserve context."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        # Try to break on a sentence boundary
        if end < len(text):
            boundary = text.rfind(". ", start, end)
            if boundary > start:
                end = boundary + 1
        chunks.append(text[start:end].strip())
        start = end
    return chunks

def process_large_document(document: str) -> list[dict]:
    """Process a document that may exceed single-call limits."""
    chunks = chunk_text(document)
    print(f"[batch] document split into {len(chunks)} chunks of ~{CHUNK_SIZE} chars")
    results = []
    for i, chunk in enumerate(chunks):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    f"Chunk {i+1}/{len(chunks)}. Extract key entities (JSON list):\n\n{chunk}"
                ),
            }],
        )
        try:
            chunk_result = json.loads(response.content[0].text)
        except json.JSONDecodeError:
            chunk_result = []
        results.extend(chunk_result)
        print(f"[batch] chunk {i+1}: {len(chunk_result)} entities")
    return results

tools = [
    {
        "name": "analyse_document",
        "description": "Analyse a document. Automatically batches if over 1500 characters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "document": {"type": "string", "description": "Document text to analyse."},
            },
            "required": ["document"],
        },
    }
]

def handle_tool(name: str, input_data: dict) -> Any:
    if name == "analyse_document":
        return process_large_document(input_data["document"])
    return {"error": "unknown_tool"}

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=512,
    tools=tools,
    messages=[{"role": "user", "content": "Analyse this: " + "alpha beta gamma. " * 300}],
)
for block in response.content:
    if block.type == "tool_use":
        result = handle_tool(block.name, block.input)
        print(f"[agent] total entities found: {len(result)}")
```

**Expected Token Savings:** Batching uses smaller, cheaper calls rather than one expensive call that risks truncation; intermediate results are stored so a failure in chunk N doesn't waste chunks 1 through N-1.
**Environment:** Document analysis pipelines, bulk summarisation, large file ingestion.

---

### Option 3 — Argument summariser: compress before calling

```python
import anthropic
import json

client = anthropic.Anthropic()

HARD_LIMIT = 2000  # characters

def compress_argument(arg: str, target_type: str, limit: int = HARD_LIMIT) -> str:
    """
    If arg exceeds limit, ask Claude to compress it while preserving semantics.
    Returns the (possibly compressed) argument and logs a warning.
    """
    if len(arg) <= limit:
        return arg

    print(f"[compress] argument is {len(arg)} chars — compressing to ~{limit}")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=limit // 4,  # tokens < chars
        messages=[{
            "role": "user",
            "content": (
                f"Compress this {target_type} to under {limit} characters while preserving "
                f"all critical information. Return ONLY the compressed version:\n\n{arg[:4000]}"
            ),
        }],
    )
    compressed = response.content[0].text.strip()
    print(f"[compress] compressed to {len(compressed)} chars")
    return compressed

def call_search_tool(query: str) -> dict:
    safe_query = compress_argument(query, "search query")
    print(f"[search] querying: {safe_query[:80]}...")
    # -- call external search API here --
    return {"results": [], "query_used": safe_query}

tools = [
    {
        "name": "web_search",
        "description": "Search the web. Query is compressed automatically if too long.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    }
]

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=1024,
    tools=tools,
    messages=[{
        "role": "user",
        "content": "Search for: " + "machine learning neural networks deep learning " * 100,
    }],
)
for block in response.content:
    if block.type == "tool_use" and block.name == "web_search":
        result = call_search_tool(block.input["query"])
        print(f"[result] {result}")
```

**Expected Token Savings:** One compression call is cheaper than a failed tool execution plus a retry loop; the agent uses the compressed argument transparently without needing to restructure its plan.
**Environment:** Search tools, prompt injection into external APIs, situations where the original argument cannot be split.

---

### Option 4 — Chunked tool calls: return a continuation token

```python
import anthropic
import json
import hashlib

client = anthropic.Anthropic()

# In-memory store mapping token → remaining content
_pending: dict[str, str] = {}

CHUNK_SIZE = 1000

def write_file_chunked(path: str, content: str, continuation_token: str | None = None) -> dict:
    """
    Write file content in chunks. If content is large, write the first chunk
    and return a continuation_token the agent must pass on the next call.
    """
    if continuation_token:
        content = _pending.pop(continuation_token, "")
        if not content:
            return {"status": "error", "message": "Invalid or expired continuation token."}

    if len(content) <= CHUNK_SIZE:
        # Final (or only) chunk — write to disk
        print(f"[file] writing {len(content)} chars to {path}")
        # open(path, "a").write(content)  # append mode for chunks
        return {"status": "done", "path": path, "chars_written": len(content)}

    # More content remains — store tail and return token
    chunk = content[:CHUNK_SIZE]
    remainder = content[CHUNK_SIZE:]
    token = hashlib.md5(remainder[:64].encode()).hexdigest()[:12]
    _pending[token] = remainder
    print(f"[file] wrote chunk ({len(chunk)} chars), {len(remainder)} chars remaining")
    return {
        "status": "partial",
        "chars_written": len(chunk),
        "continuation_token": token,
        "message": f"Call write_file again with continuation_token='{token}' to write remaining {len(remainder)} chars.",
    }

tools = [
    {
        "name": "write_file",
        "description": (
            "Write content to a file. If content exceeds 1000 characters, "
            "a continuation_token is returned — call again with that token to continue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":               {"type": "string"},
                "content":            {"type": "string"},
                "continuation_token": {"type": "string", "description": "Token from previous partial write."},
            },
            "required": ["path", "content"],
        },
    }
]

# Demo: write 3000 characters in chunks
large_content = "x" * 3000
result = write_file_chunked("/tmp/out.txt", large_content)
print(json.dumps(result, indent=2))
while result.get("status") == "partial":
    result = write_file_chunked("/tmp/out.txt", "", result["continuation_token"])
    print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Enables writing arbitrarily large content through a bounded-size tool interface; continuation pattern is idiomatic and lets the agent resume after interruption.
**Environment:** File-write tools, code generation tools, any tool where content must be streamed incrementally.

---

### Option 5 — Schema maxLength enforcement with early rejection

```python
import anthropic
import json
import jsonschema

client = anthropic.Anthropic()

# Tool schemas with explicit maxLength constraints
TOOL_SCHEMAS = {
    "run_bash": {
        "name": "run_bash",
        "description": "Run a bash command. Max 500 characters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "maxLength": 500,
                    "description": "Bash command. Must be under 500 characters.",
                },
            },
            "required": ["command"],
        },
    },
    "send_email": {
        "name": "send_email",
        "description": "Send an email. Subject max 200 chars, body max 5000 chars.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "maxLength": 200},
                "body":    {"type": "string", "maxLength": 5000},
                "to":      {"type": "string", "format": "email"},
            },
            "required": ["subject", "body", "to"],
        },
    },
}

def validate_tool_input(tool_name: str, input_data: dict) -> list[str]:
    """Return list of validation errors (empty list = valid)."""
    schema = TOOL_SCHEMAS.get(tool_name, {}).get("input_schema", {})
    errors = []
    validator = jsonschema.Draft7Validator(schema)
    for error in validator.iter_errors(input_data):
        errors.append(f"{' → '.join(str(p) for p in error.path)}: {error.message}")
    return errors

def dispatch_tool(tool_name: str, tool_id: str, input_data: dict) -> dict:
    errors = validate_tool_input(tool_name, input_data)
    if errors:
        return {
            "error": "validation_failed",
            "violations": errors,
            "hint": "Shorten the argument and retry.",
        }
    print(f"[tool] {tool_name} validated OK — dispatching")
    return {"status": "ok"}

# Simulate agent calling run_bash with a too-long command
oversized_input = {"command": "echo " + "hello " * 200}
result = dispatch_tool("run_bash", "toolu_01", oversized_input)
print(json.dumps(result, indent=2))

# Valid call
valid_input = {"command": "ls -la /tmp"}
result = dispatch_tool("run_bash", "toolu_02", valid_input)
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Schema validation rejects bad calls before they reach the backend; the structured error message tells the agent exactly which field to fix, reducing multi-turn debugging.
**Environment:** Tool dispatchers with many tools; useful as a middleware layer before any tool execution.

---

### Option 6 — Pre-call length validator decorator

```python
import functools
import inspect
import anthropic
import json

client = anthropic.Anthropic()

def enforce_arg_limits(**limits: int):
    """
    Decorator that raises ValueError if any string argument exceeds its character limit.
    Usage: @enforce_arg_limits(query=4000, context=2000)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Map positional args to parameter names
            sig = inspect.signature(func)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            violations = []
            for param, limit in limits.items():
                value = bound.arguments.get(param, "")
                if isinstance(value, str) and len(value) > limit:
                    violations.append(
                        f"'{param}' is {len(value)} chars (limit: {limit}). "
                        f"Truncate or split before calling."
                    )
            if violations:
                raise ValueError("Argument length violations:\n" + "\n".join(violations))
            return func(*args, **kwargs)
        return wrapper
    return decorator

@enforce_arg_limits(query=2000, context=1000)
def search_knowledge_base(query: str, context: str = "") -> list[dict]:
    """Search internal KB. query ≤ 2000 chars, context ≤ 1000 chars."""
    print(f"[kb] searching: {query[:60]}...")
    return [{"id": 1, "text": "result"}]

@enforce_arg_limits(code=8000)
def execute_python(code: str) -> dict:
    """Execute Python snippet. code ≤ 8000 chars."""
    print(f"[exec] running {len(code)}-char snippet")
    return {"stdout": "", "returncode": 0}

# Valid call
try:
    result = search_knowledge_base("What is the capital of France?")
    print(f"[ok] {result}")
except ValueError as e:
    print(f"[error] {e}")

# Oversized call — caught by decorator
try:
    long_query = "find articles about " + "machine learning " * 200
    result = search_knowledge_base(long_query)
except ValueError as e:
    print(f"[error] {e}")

# Demonstrate in an agentic loop
def run_agent_with_tools(user_msg: str) -> None:
    tools = [{
        "name": "search_knowledge_base",
        "description": "Search KB. query max 2000 chars.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":   {"type": "string", "maxLength": 2000},
                "context": {"type": "string", "maxLength": 1000},
            },
            "required": ["query"],
        },
    }]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        tools=tools,
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in response.content:
        if block.type == "tool_use":
            try:
                result = search_knowledge_base(**block.input)
                print(f"[agent] tool ok: {result}")
            except ValueError as e:
                print(f"[agent] tool rejected: {e}")

run_agent_with_tools("Search for info about Python asyncio event loops.")
```

**Expected Token Savings:** Decorator catches violations in development and production; zero overhead for valid calls; eliminates the class of bugs where truncated arguments silently corrupt tool state.
**Environment:** Any Python tool implementation layer; particularly useful during development to catch issues before they reach production.

---

## Comparison

| Option | Detection Point | Extra API Calls | Handles Split | Handles Compress | Best For |
|---|---|---|---|---|---|
| 1. Length check | Before dispatch | 0 | No | No | Simple guard; returns actionable error |
| 2. Split-and-batch | Inside tool | 0 | Yes | No | Document/text processing pipelines |
| 3. Argument summariser | Before dispatch | 1 (compress) | No | Yes | Search/prompt tools where split isn't valid |
| 4. Continuation token | Inside tool | 0 | Yes (sequential) | No | File writes, streamed content |
| 5. Schema maxLength | Dispatch middleware | 0 | No | No | Multi-tool dispatcher; centralised validation |
| 6. Decorator | Function boundary | 0 | No | No | Per-function enforcement; dev + prod parity |
