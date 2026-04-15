---
layout: solution
title: "Agent Truncates Long Tool Outputs Without Warning"
category: general
description: "Tool results that exceed the model's context window are silently cut off, causing the agent to reason over incomplete data."
tags: [general, context-window, tool-failure, token-cost, data-integrity]
---

## Symptom

The agent calls a tool that returns a large result — a database query with thousands of rows, a full file read, a long API response. The result is silently truncated to fit within the context window. The agent either doesn't notice and produces an answer based on partial data, or it notices the truncation but has no way to retrieve the missing portion.

## Root Cause

Tool results are inserted into the conversation as text. If the result is larger than what the context window can accommodate alongside the system prompt and history, the API either returns an error or the developer's code pre-truncates the string. Without a pagination or chunking strategy, only the first N characters of the result are ever seen by the model.

## Fix

### Option 1 — Truncate with an explicit notice and row count

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_RESULT_CHARS = 3000

def execute_tool(name: str, inputs: dict) -> str:
    """Execute tool and truncate output with an explicit notice if too large."""
    if name == "query_database":
        # Simulate a large DB result
        rows = [{"id": i, "name": f"User {i}", "email": f"user{i}@example.com"} for i in range(500)]
        full_json = json.dumps(rows)

        if len(full_json) > MAX_RESULT_CHARS:
            # Count how many rows fit
            truncated_rows = []
            size = 0
            for row in rows:
                serialised = json.dumps(row) + ","
                if size + len(serialised) > MAX_RESULT_CHARS:
                    break
                truncated_rows.append(row)
                size += len(serialised)

            return json.dumps({
                "rows":         truncated_rows,
                "returned":     len(truncated_rows),
                "total":        len(rows),
                "truncated":    True,
                "notice":       f"Result truncated: showing {len(truncated_rows)}/{len(rows)} rows. "
                                "Use 'offset' and 'limit' parameters to page through results.",
            })

        return json.dumps({"rows": rows, "total": len(rows), "truncated": False})

    return json.dumps({"error": f"unknown tool: {name}"})

TOOLS = [
    {
        "name": "query_database",
        "description": "Query user records. Use 'limit' and 'offset' to paginate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit":  {"type": "integer", "default": 20},
                "offset": {"type": "integer", "default": 0},
            },
        },
    }
]

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": execute_tool(b.name, b.input)}
            for b in response.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(run_agent("How many users do we have and what are the first few?"))
```

**Expected Token Savings:** Explicit truncation notice prevents the model from reasoning as if the result were complete; avoids wrong answers that require a correction turn.
**Environment:** Any tool that returns variable-length results; minimum viable truncation strategy.

---

### Option 2 — Paginated tool with cursor-based navigation

```python
import json
import math
import anthropic

client = anthropic.Anthropic()

# Simulated database
ALL_RECORDS = [{"id": i, "product": f"Product {i}", "price": round(10 + i * 0.5, 2)} for i in range(200)]

PAGE_SIZE = 15

def get_records(page: int = 1) -> str:
    total_pages = math.ceil(len(ALL_RECORDS) / PAGE_SIZE)
    start       = (page - 1) * PAGE_SIZE
    end         = start + PAGE_SIZE
    records     = ALL_RECORDS[start:end]

    return json.dumps({
        "records":     records,
        "page":        page,
        "total_pages": total_pages,
        "total":       len(ALL_RECORDS),
        "has_more":    page < total_pages,
        "next_page":   page + 1 if page < total_pages else None,
    })

TOOLS = [
    {
        "name": "list_products",
        "description": (
            "List products with pagination. Returns up to 15 records per page. "
            "If has_more is true, call again with next_page to get more results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "description": "Page number (1-based)", "default": 1}
            },
        },
    }
]

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(20):  # allow more steps for pagination
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                page   = b.input.get("page", 1)
                result = get_records(page)
                print(f"[tool] page {page}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(run_agent("Find all products that cost more than $80 and count them."))
```

**Expected Token Savings:** Each page is small enough to fit comfortably; the model only reads pages it needs, not the entire dataset.
**Environment:** Database query tools, search results, list APIs; the paginated tool contract is the standard approach.

---

### Option 3 — Tool returns a summary with on-demand detail

```python
import json
import anthropic

client = anthropic.Anthropic()

# Simulated log file — very large
LOG_LINES = [
    f"2025-04-{(i % 30) + 1:02d} {i % 24:02d}:00 {'ERROR' if i % 7 == 0 else 'INFO'} "
    f"service={'auth' if i % 3 == 0 else 'api' if i % 3 == 1 else 'db'} "
    f"msg='Event {i}'"
    for i in range(1000)
]

def get_log_summary(service: str | None = None) -> str:
    lines = [l for l in LOG_LINES if service is None or f"service={service}" in l]
    errors = [l for l in lines if "ERROR" in l]
    return json.dumps({
        "total_lines":   len(lines),
        "error_count":   len(errors),
        "services":      list({l.split("service=")[1].split()[0] for l in lines}),
        "recent_errors": errors[-5:],    # only last 5 errors
        "hint":          "Call get_log_lines with filters for full detail.",
    })

def get_log_lines(service: str | None = None, level: str | None = None,
                  offset: int = 0, limit: int = 20) -> str:
    lines = LOG_LINES
    if service:
        lines = [l for l in lines if f"service={service}" in l]
    if level:
        lines = [l for l in lines if level.upper() in l]
    page = lines[offset:offset + limit]
    return json.dumps({
        "lines":   page,
        "offset":  offset,
        "limit":   limit,
        "total":   len(lines),
        "has_more": offset + limit < len(lines),
    })

TOOLS = [
    {
        "name": "get_log_summary",
        "description": "Get a high-level summary of logs. Call this first.",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string", "description": "Filter by service name (optional)"}},
        },
    },
    {
        "name": "get_log_lines",
        "description": "Get paginated log lines with optional filters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "level":   {"type": "string", "enum": ["ERROR", "INFO", "WARN"]},
                "offset":  {"type": "integer", "default": 0},
                "limit":   {"type": "integer", "default": 20},
            },
        },
    },
]

def execute(name: str, inputs: dict) -> str:
    if name == "get_log_summary": return get_log_summary(inputs.get("service"))
    if name == "get_log_lines":   return get_log_lines(**{k: inputs[k] for k in inputs if k in {"service","level","offset","limit"}})
    return json.dumps({"error": "unknown tool"})

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(10):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": execute(b.name, b.input)}
            for b in response.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(run_agent("How many errors are in the auth service logs, and what do the recent ones say?"))
```

**Expected Token Savings:** Summary call returns ~200 tokens instead of 50 000 log tokens; detail is fetched only for the specific lines the model needs.
**Environment:** Log analysis, large file inspection, any tool where a summary is sufficient for most queries.

---

### Option 4 — Chunked file reader with sliding window

```python
import anthropic

client = anthropic.Anthropic()

CHUNK_SIZE = 2000  # characters per chunk

def read_file_chunk(file_path: str, chunk_index: int = 0) -> dict:
    """Read a file in chunks to avoid context overflow."""
    try:
        with open(file_path) as f:
            content = f.read()
    except FileNotFoundError:
        return {"error": f"File not found: {file_path}"}

    total_chars  = len(content)
    total_chunks = (total_chars + CHUNK_SIZE - 1) // CHUNK_SIZE
    start        = chunk_index * CHUNK_SIZE
    end          = min(start + CHUNK_SIZE, total_chars)
    chunk        = content[start:end]

    return {
        "chunk":         chunk,
        "chunk_index":   chunk_index,
        "total_chunks":  total_chunks,
        "chars_in_chunk": len(chunk),
        "total_chars":   total_chars,
        "has_more":      chunk_index + 1 < total_chunks,
        "next_chunk":    chunk_index + 1 if chunk_index + 1 < total_chunks else None,
    }

TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read a file in chunks. Returns one chunk at a time (up to 2000 chars). "
            "If has_more is true, call again with next_chunk index."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path":   {"type": "string"},
                "chunk_index": {"type": "integer", "default": 0},
            },
            "required": ["file_path"],
        },
    }
]

import json

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(15):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                result = read_file_chunk(b.input["file_path"], b.input.get("chunk_index", 0))
                print(f"[tool] chunk {result.get('chunk_index')}/{result.get('total_chunks', '?')}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

# Create a test file
import tempfile, os
with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
    f.write("This is a test document.\n" * 200)
    tmp_path = f.name

result = run_agent(f"Read {tmp_path} and tell me how many lines it has.")
print(result)
os.unlink(tmp_path)
```

**Expected Token Savings:** Each chunk is bounded to 2 000 chars; agent reads only as many chunks as needed for the query.
**Environment:** File-reading agents; code review assistants; document analysis pipelines.

---

### Option 5 — Tool result compression via Haiku summarizer

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_RAW_CHARS = 2000

def compress_if_needed(tool_name: str, raw_result: str) -> str:
    """If result is too large, summarise it before passing to the main model."""
    if len(raw_result) <= MAX_RAW_CHARS:
        return raw_result

    print(f"[compress] {tool_name} result {len(raw_result)} chars → compressing")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                f"This is the output of tool '{tool_name}'. "
                "Summarise it concisely, preserving all important data points, "
                "numbers, errors, and actionable information.\n\n"
                + raw_result[:8000]   # cap input to Haiku too
            ),
        }],
    )
    summary = response.content[0].text
    return json.dumps({
        "compressed": True,
        "original_chars": len(raw_result),
        "summary": summary,
    })

# Simulated large tool output
LARGE_OUTPUT = json.dumps({
    "metrics": [{"timestamp": f"2025-04-{i:02d}", "requests": 10000 + i * 50, "errors": i * 2, "p99_ms": 200 + i} for i in range(30)],
    "alerts":  [{"level": "critical", "message": "Error rate exceeded 5% on 2025-04-15"}],
    "summary": "Monthly API health report",
})

def execute_tool(name: str, inputs: dict) -> str:
    raw = LARGE_OUTPUT
    return compress_if_needed(name, raw)

TOOLS = [
    {
        "name": "get_metrics",
        "description": "Get API health metrics report.",
        "input_schema": {"type": "object", "properties": {}},
    }
]

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": execute_tool(b.name, b.input)}
            for b in response.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(run_agent("What is the trend in API errors over the past month?"))
```

**Expected Token Savings:** A 10 000-char result compressed to 400 chars saves ~2 400 tokens on every subsequent turn that includes it in history.
**Environment:** Monitoring agents, analytics pipelines, any tool that returns verbose structured data.

---

### Option 6 — Streaming tool output with partial result processing

```python
import json
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def stream_large_result(total_records: int = 500, chunk_size: int = 50):
    """Simulate streaming tool results in chunks rather than all at once."""
    for start in range(0, total_records, chunk_size):
        end   = min(start + chunk_size, total_records)
        chunk = {
            "records": [{"id": i, "value": i * 2} for i in range(start, end)],
            "start":   start,
            "end":     end,
            "total":   total_records,
            "done":    end >= total_records,
        }
        yield json.dumps(chunk)
        await asyncio.sleep(0)  # yield control

async def process_with_streaming(query: str) -> str:
    """
    Process a large result by accumulating statistics as chunks arrive
    rather than loading everything into the context window.
    """
    total   = 0
    max_val = 0
    count   = 0

    async for chunk_str in stream_large_result():
        chunk    = json.loads(chunk_str)
        records  = chunk["records"]
        total   += sum(r["value"] for r in records)
        max_val  = max(max_val, max((r["value"] for r in records), default=0))
        count   += len(records)
        print(f"[stream] processed {chunk['end']}/{chunk['total']} records")

    # Now ask the model with just the aggregated stats, not the raw data
    stats_summary = json.dumps({"count": count, "total": total, "max": max_val, "avg": round(total/count, 2)})
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"{query}\n\nPre-computed statistics: {stats_summary}",
        }],
    )
    return response.content[0].text

async def main():
    result = await process_with_streaming("Summarise the dataset and flag anything unusual.")
    print(result)

asyncio.run(main())
```

**Expected Token Savings:** Streaming + pre-aggregation sends only a small stats object to the model instead of thousands of records; scales to arbitrarily large datasets.
**Environment:** ETL pipelines, large dataset analysis; when you can compute statistics outside the LLM, do it there.

---

## Comparison

| Option | Output Handling | Pagination | Async | Best For |
|---|---|---|---|---|
| 1. Truncate with notice | Explicit warning + count | No | No | Quick retrofit; minimum viable |
| 2. Paginated tool | Cursor-based pages | Yes | No | Database/list APIs |
| 3. Summary + detail | Two-tier tools | Partial | No | Logs, large structured responses |
| 4. Chunked file reader | Chunk index | Yes | No | File-reading agents |
| 5. Haiku compressor | LLM summarization | No | No | Verbose third-party API responses |
| 6. Streaming aggregation | Pre-compute stats | N/A | Yes | Very large datasets, ETL pipelines |
