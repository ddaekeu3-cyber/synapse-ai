---
layout: solution
title: "Agent Doesn't Implement Tool Output Size Limits"
category: tool-failure
description: "Tool responses that return megabytes of data bloat the context window, trigger token limits, and inflate costs. Size-limited tool output with truncation, pagination, and summarization keeps agents reliable at scale."
tags: [tool-failure, output-size, truncation, pagination, context-window, token-cost, sqlite]
---

# Agent Doesn't Implement Tool Output Size Limits

## Problem

Tools that return raw database results, API payloads, or file contents can return hundreds of kilobytes in a single response. Injecting this into the context window causes three failures: exceeding the model's context limit, dramatically increasing token costs, and burying the relevant signal in noise.

Size-limited tool output with truncation, pagination, and summarization keeps the context window clean and costs predictable.

---

## Option 1: Hard Truncation with Size Guard

```python
import anthropic
import json

MAX_TOOL_OUTPUT_CHARS = 4000  # ~1000 tokens

def truncate_output(raw: str, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(raw) <= max_chars:
        return raw
    truncated = raw[:max_chars]
    omitted = len(raw) - max_chars
    return truncated + f"\n\n[TRUNCATED: {omitted} characters omitted. Request a smaller range or use pagination.]"


# Simulated tool that returns large output
def tool_read_file(path: str) -> str:
    # Simulates a large file return
    fake_content = f"# {path}\n\n" + "Line content here.\n" * 500
    return truncate_output(fake_content)


def tool_search_db(query: str) -> str:
    # Simulates large DB result
    rows = [{"id": i, "value": f"row_{i}_data"} for i in range(200)]
    raw = json.dumps(rows, indent=2)
    return truncate_output(raw)


def run_agent_with_size_limits(request: str):
    client = anthropic.Anthropic()

    tools = [
        {
            "name": "read_file",
            "description": "Read a file. Output is capped at 4000 characters.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "search_db",
            "description": "Search the database. Returns first 4000 chars of results.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    ]

    messages = [{"role": "user", "content": request}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "read_file":
                        output = tool_read_file(block.input["path"])
                    elif block.name == "search_db":
                        output = tool_search_db(block.input["query"])
                    else:
                        output = "Unknown tool"

                    chars = len(output)
                    print(f"[Tool] {block.name} → {chars} chars returned")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    run_agent_with_size_limits("Read the file /etc/hosts and search the DB for 'user'.")
# Expected Token Savings: 60-90% on large tool outputs — 200-row JSON → 4000 chars instead of 40000
# Environment: pip install anthropic; json is stdlib
```

---

## Option 2: Paginated Tool Output

```python
import anthropic
import json
from math import ceil

PAGE_SIZE = 20  # items per page

def paginate_results(items: list, page: int, page_size: int = PAGE_SIZE) -> dict:
    total = len(items)
    total_pages = max(1, ceil(total / page_size))
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "items": items[start:end],
        "page": page,
        "page_size": page_size,
        "total_items": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
    }


# Simulated data store
ALL_RECORDS = [
    {"id": i, "name": f"record_{i}", "value": i * 10}
    for i in range(1, 151)  # 150 records
]


def tool_list_records(page: int = 1) -> str:
    result = paginate_results(ALL_RECORDS, page)
    summary = (
        f"Page {result['page']}/{result['total_pages']} "
        f"({result['total_items']} total records, {result['page_size']} per page)\n"
        f"Has more: {result['has_next']}\n\n"
    )
    return summary + json.dumps(result["items"], indent=2)


def run_paginated_agent(request: str):
    client = anthropic.Anthropic()

    tools = [
        {
            "name": "list_records",
            "description": (
                "List records with pagination. Returns 20 records per page. "
                "Use 'page' parameter to navigate. Check 'Has more' in output to know if more pages exist."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "Page number (1-based)", "default": 1}
                },
                "required": [],
            },
        },
    ]

    messages = [{"role": "user", "content": request}]

    for _ in range(10):  # max turns
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    page = block.input.get("page", 1)
                    output = tool_list_records(page)
                    chars = len(output)
                    print(f"[Tool] list_records(page={page}) → {chars} chars")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    run_paginated_agent("How many records are there total? List the first 20 records.")
# Expected Token Savings: 80-95% vs. returning all 150 records at once (~1500 chars vs ~15000)
# Environment: pip install anthropic; json, math are stdlib
```

---

## Option 3: Size-Aware Summarization via Secondary Model Call

```python
import anthropic
import json

SIZE_THRESHOLD_CHARS = 2000   # Below this: pass raw
SUMMARIZE_THRESHOLD_CHARS = 8000  # Above this: summarize before injecting

def maybe_summarize(tool_name: str, raw_output: str) -> str:
    """
    If output is within threshold, return raw.
    If oversized, use a cheap model to summarize it first.
    """
    size = len(raw_output)

    if size <= SIZE_THRESHOLD_CHARS:
        return raw_output

    if size <= SUMMARIZE_THRESHOLD_CHARS:
        # Medium size: truncate with note
        truncated = raw_output[:SIZE_THRESHOLD_CHARS]
        return truncated + f"\n\n[Note: Output was {size} chars. Showing first {SIZE_THRESHOLD_CHARS}.]"

    # Large: summarize with haiku (cheap)
    print(f"[SizeLimiter] {tool_name} returned {size} chars — summarizing with haiku...")
    client = anthropic.Anthropic()
    summary_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"The following is output from tool '{tool_name}'. "
                f"Summarize it concisely in ≤300 words, preserving key data:\n\n"
                f"{raw_output[:12000]}"  # Feed first 12k chars to haiku
            ),
        }],
    )
    summary = summary_response.content[0].text
    return f"[Summarized from {size} chars]\n\n{summary}"


# Simulated tools
def tool_get_logs(service: str) -> str:
    # Simulate a large log dump
    lines = [f"2026-04-16 10:{i:02d}:00 INFO service={service} msg=request_processed latency={i*3}ms" for i in range(300)]
    return "\n".join(lines)

def tool_get_config(env: str) -> str:
    # Simulate a small config (no summarization needed)
    return json.dumps({"env": env, "debug": False, "timeout": 30, "workers": 4}, indent=2)


def run_agent_with_smart_limits(request: str):
    client = anthropic.Anthropic()

    TOOL_DISPATCH = {
        "get_logs": lambda inp: tool_get_logs(inp.get("service", "api")),
        "get_config": lambda inp: tool_get_config(inp.get("env", "prod")),
    }

    tools = [
        {
            "name": "get_logs",
            "description": "Fetch service logs. Large outputs are automatically summarized.",
            "input_schema": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        },
        {
            "name": "get_config",
            "description": "Fetch service configuration.",
            "input_schema": {
                "type": "object",
                "properties": {"env": {"type": "string"}},
                "required": ["env"],
            },
        },
    ]

    messages = [{"role": "user", "content": request}]

    for _ in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    fn = TOOL_DISPATCH.get(block.name)
                    raw = fn(block.input) if fn else "Unknown tool"
                    processed = maybe_summarize(block.name, raw)
                    print(f"[Tool] {block.name}: raw={len(raw)} → processed={len(processed)} chars")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": processed})
            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    run_agent_with_smart_limits("Get the logs for the 'api' service and get the prod config.")
# Expected Token Savings: 70-85% on large log outputs via haiku summarization
# Environment: pip install anthropic; json is stdlib
```

---

## Option 4: Per-Tool Size Budgets with SQLite Tracking

```python
import sqlite3
import json
import anthropic
from datetime import datetime

# Per-tool character budgets
TOOL_BUDGETS: dict[str, int] = {
    "read_file":    5000,
    "search_db":    3000,
    "get_logs":     2000,
    "list_items":   4000,
    "default":      4000,
}

class SizeLimitTracker:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_size_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT,
                raw_chars INTEGER,
                returned_chars INTEGER,
                truncated INTEGER,
                called_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def apply_limit(self, tool_name: str, raw_output: str) -> str:
        budget = TOOL_BUDGETS.get(tool_name, TOOL_BUDGETS["default"])
        truncated = len(raw_output) > budget

        if truncated:
            output = (
                raw_output[:budget]
                + f"\n\n[SIZE LIMIT: tool={tool_name}, budget={budget}, "
                f"raw={len(raw_output)}, omitted={len(raw_output)-budget}]"
            )
        else:
            output = raw_output

        self.conn.execute(
            "INSERT INTO tool_size_log (tool_name, raw_chars, returned_chars, truncated) VALUES (?,?,?,?)",
            (tool_name, len(raw_output), len(output), int(truncated)),
        )
        self.conn.commit()
        return output

    def report(self) -> list[dict]:
        rows = self.conn.execute("""
            SELECT tool_name, COUNT(*) as calls,
                   AVG(raw_chars) as avg_raw, AVG(returned_chars) as avg_returned,
                   SUM(truncated) as truncations,
                   SUM(raw_chars - returned_chars) as chars_saved
            FROM tool_size_log GROUP BY tool_name
        """).fetchall()
        return [
            {
                "tool": r[0],
                "calls": r[1],
                "avg_raw": int(r[2] or 0),
                "avg_returned": int(r[3] or 0),
                "truncations": r[4],
                "chars_saved": int(r[5] or 0),
            }
            for r in rows
        ]


# Simulated tools
def _simulate_tool(name: str, size_chars: int) -> str:
    content = f"[{name} output]\n" + ("data_entry_here | " * (size_chars // 18))
    return content[:size_chars]


def run_budget_tracked_agent(request: str):
    tracker = SizeLimitTracker()
    client = anthropic.Anthropic()

    SIMULATED_SIZES = {
        "read_file": 12000,
        "search_db": 8000,
        "get_logs":  15000,
        "list_items": 2000,
    }

    tools = [
        {
            "name": name,
            "description": f"Tool: {name}",
            "input_schema": {"type": "object", "additionalProperties": True},
        }
        for name in SIMULATED_SIZES
    ]

    messages = [{"role": "user", "content": request}]

    for _ in range(8):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    sim_size = SIMULATED_SIZES.get(block.name, 3000)
                    raw = _simulate_tool(block.name, sim_size)
                    output = tracker.apply_limit(block.name, raw)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
            messages.append({"role": "user", "content": results})

    print("\nSize Budget Report:")
    for row in tracker.report():
        pct_saved = 100 * row["chars_saved"] / max(row["avg_raw"] * row["calls"], 1)
        print(f"  {row['tool']}: {row['calls']} calls, avg {row['avg_raw']}→{row['avg_returned']} chars, {pct_saved:.0f}% saved")


if __name__ == "__main__":
    run_budget_tracked_agent("Run all four tools and summarize what you found.")
# Expected Token Savings: 40-80% depending on tool verbosity vs. budget
# Environment: pip install anthropic; sqlite3, json are stdlib
```

---

## Option 5: Streaming Tool Output with Chunked Delivery

```python
import asyncio
import anthropic
from typing import AsyncIterator

CHUNK_SIZE = 1000  # chars per chunk delivered to agent
MAX_CHUNKS = 5     # max chunks per tool call

async def stream_tool_output(raw: str) -> AsyncIterator[str]:
    """Yield chunks of tool output, stopping after MAX_CHUNKS."""
    chunks_sent = 0
    for i in range(0, len(raw), CHUNK_SIZE):
        chunk = raw[i:i + CHUNK_SIZE]
        yield chunk
        chunks_sent += 1
        await asyncio.sleep(0)  # yield control
        if chunks_sent >= MAX_CHUNKS:
            remaining = len(raw) - (i + CHUNK_SIZE)
            if remaining > 0:
                yield f"\n\n[STREAM LIMIT: {remaining} more characters available. Request next chunk with offset={i + CHUNK_SIZE}.]"
            return


async def tool_read_large_file(path: str, offset: int = 0) -> str:
    """Read file with offset-based chunking."""
    fake_content = f"# {path}\n" + ("content line data here\n" * 300)
    segment = fake_content[offset:offset + CHUNK_SIZE * MAX_CHUNKS]

    if offset + len(segment) < len(fake_content):
        segment += f"\n\n[Showing chars {offset}–{offset+len(segment)} of {len(fake_content)}. Use offset={offset+len(segment)} for more.]"

    return segment


async def run_chunked_agent(request: str):
    client = anthropic.AsyncAnthropic()

    tools = [
        {
            "name": "read_file",
            "description": (
                "Read a file. Returns up to 5000 chars starting at 'offset'. "
                "If output says 'offset=N for more', call again with that offset."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["path"],
            },
        },
    ]

    messages = [{"role": "user", "content": request}]

    for turn in range(6):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    offset = block.input.get("offset", 0)
                    output = await tool_read_large_file(block.input["path"], offset)
                    print(f"[Tool] read_file(offset={offset}) → {len(output)} chars")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    asyncio.run(run_chunked_agent("Read the file /var/log/app.log and summarize the first section."))
# Expected Token Savings: 80% per call — only 5000 chars returned vs. full file
# Environment: pip install anthropic; asyncio is stdlib
```

---

## Option 6: Schema-Validated Output Projection

```python
import anthropic
import json
from typing import Any

# Field allowlists — only return these fields from tool output
TOOL_PROJECTIONS: dict[str, list[str] | None] = {
    "get_user":    ["id", "name", "email", "role"],          # omit: password_hash, tokens, pii
    "get_order":   ["id", "status", "total", "created_at"],  # omit: raw payment data
    "get_metrics": ["p50_ms", "p99_ms", "error_rate"],       # omit: raw histogram buckets
    "get_config":  None,  # None = return everything
}

MAX_ARRAY_ITEMS = 10  # Limit list fields to N items

def project_output(tool_name: str, raw: Any) -> str:
    """Apply field projection and array limits to tool output."""
    allowlist = TOOL_PROJECTIONS.get(tool_name)

    if isinstance(raw, list):
        # Limit list length
        truncated = raw[:MAX_ARRAY_ITEMS]
        result: Any = truncated
        if len(raw) > MAX_ARRAY_ITEMS:
            result = truncated + [{"_note": f"{len(raw) - MAX_ARRAY_ITEMS} more items omitted"}]
    elif isinstance(raw, dict) and allowlist is not None:
        result = {k: v for k, v in raw.items() if k in allowlist}
        omitted = set(raw.keys()) - set(allowlist)
        if omitted:
            result["_omitted_fields"] = sorted(omitted)
    else:
        result = raw

    output = json.dumps(result, indent=2, default=str)

    # Final hard cap
    if len(output) > 6000:
        output = output[:6000] + "\n[truncated at 6000 chars]"

    return output


# Simulated tool responses (with sensitive/noisy fields)
def tool_get_user(user_id: str) -> dict:
    return {
        "id": user_id,
        "name": "Alice Smith",
        "email": "alice@example.com",
        "role": "admin",
        "password_hash": "$2b$12$abcdefghijklmnopqrstuv",  # should be omitted
        "session_tokens": ["tok_abc", "tok_def"],            # should be omitted
        "login_attempts": 0,
        "created_at": "2025-01-15",
    }

def tool_get_order(order_id: str) -> dict:
    return {
        "id": order_id,
        "status": "shipped",
        "total": 149.99,
        "created_at": "2026-04-10",
        "raw_payment_data": {"card": "4111111111111111", "cvv": "123"},  # omitted
        "internal_trace_id": "tr_xyzabc",                                 # omitted
    }

def tool_get_metrics(service: str) -> dict:
    return {
        "service": service,
        "p50_ms": 42,
        "p99_ms": 380,
        "error_rate": 0.002,
        "histogram_buckets": list(range(1000)),  # noisy — omitted
        "raw_samples": [{"t": i, "v": i * 0.1} for i in range(500)],  # omitted
    }


TOOL_FNS = {
    "get_user":    lambda inp: tool_get_user(inp.get("user_id", "u1")),
    "get_order":   lambda inp: tool_get_order(inp.get("order_id", "o1")),
    "get_metrics": lambda inp: tool_get_metrics(inp.get("service", "api")),
}


def run_projected_agent(request: str):
    client = anthropic.Anthropic()

    tools = [
        {
            "name": name,
            "description": f"Tool: {name}",
            "input_schema": {"type": "object", "additionalProperties": True},
        }
        for name in TOOL_FNS
    ]

    messages = [{"role": "user", "content": request}]

    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    fn = TOOL_FNS.get(block.name)
                    raw = fn(block.input) if fn else {}
                    raw_str = json.dumps(raw)
                    projected = project_output(block.name, raw)
                    print(f"[Tool] {block.name}: {len(raw_str)} raw → {len(projected)} projected chars")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": projected})
            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    run_projected_agent("Get user u-99, order o-123, and metrics for the 'api' service.")
# Expected Token Savings: 50-90% by omitting noisy/sensitive fields before context injection
# Environment: pip install anthropic; json is stdlib
```

---

## Comparison

| Option | Mechanism | Size Control | Preserves All Data | SQLite | Best For |
|--------|-----------|-------------|-------------------|--------|----------|
| 1 | Hard character truncation | Fixed cap | No (tail lost) | No | Quick protection, simple tools |
| 2 | Pagination | Page-sized slices | Yes (across pages) | No | List/table results |
| 3 | Smart summarization | Haiku rewrite | Semantic summary | No | Log/text output with meaning |
| 4 | Per-tool budgets + tracking | Per-tool limits | No (tail lost) | Yes | Multi-tool audit and reporting |
| 5 | Offset-based chunked reads | Chunk limits | Yes (across calls) | No | Large file/document access |
| 6 | Field projection allowlist | Schema-level | Key fields only | No | APIs with noisy/sensitive fields |
