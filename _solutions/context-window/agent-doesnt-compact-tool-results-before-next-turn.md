---
layout: solution
title: "Agent Doesn't Compact Tool Results Before Next Turn"
category: context-window
description: "Agent stores raw, verbose tool results in the message history, causing context to fill rapidly when tools return large payloads like API responses, file contents, or database rows."
tags: [context-window, tool-use, compaction, summarization, token-cost]
---

## Symptom

Context window fills after 5–10 tool calls even though only a few facts from each result are actually needed. A database query returning 500 rows bloats the history by 10,000+ tokens. Subsequent turns pay to re-read the full raw results even when the agent only needed the first 3 rows. Context overflow errors occur mid-task and earlier tool results are truncated from history.

## Root Cause

The Anthropic API requires tool_result messages to be present in the conversation history for the model to reference them. By default, agents store the complete raw output — the entire JSON blob, full file contents, or complete API response — as the tool_result content. This is correct for correctness but inefficient for cost: subsequent turns re-read every byte of every past tool result as input tokens, even when the relevant information is a small fraction of the full payload.

## Fix

### Option 1: Truncate tool results to a configurable max length

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_TOOL_RESULT_CHARS = 2000  # ~500 tokens — adjust per use case


def compact_tool_result(raw_result: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """
    Truncate a tool result to max_chars, appending a note about truncation.
    For JSON results, try to preserve structure.
    """
    if len(raw_result) <= max_chars:
        return raw_result

    # For JSON: truncate the parsed structure intelligently
    try:
        data = json.loads(raw_result)
        if isinstance(data, list):
            # Keep first N items and note how many were truncated
            serialized = ""
            kept = 0
            for i, item in enumerate(data):
                candidate = json.dumps(data[:i+1], indent=2)
                if len(candidate) > max_chars - 100:
                    break
                serialized = candidate
                kept = i + 1
            total = len(data)
            return f"{serialized}\n... [{total - kept} more items truncated. {total} total.]"
        elif isinstance(data, dict):
            truncated = json.dumps(data, indent=2)[:max_chars]
            return truncated + f"\n... [JSON truncated at {max_chars} chars]"
    except (json.JSONDecodeError, ValueError):
        pass

    # Plain text: hard truncate
    return raw_result[:max_chars] + f"\n... [truncated — {len(raw_result) - max_chars} chars omitted]"


TOOLS = [
    {
        "name": "query_database",
        "description": "Run a SQL query and return results as JSON array",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["sql"],
        },
    }
]


def simulate_db_query(sql: str) -> str:
    """Simulate a large database result."""
    rows = [{"id": i, "name": f"User_{i}", "email": f"user{i}@example.com", "score": i * 1.5}
            for i in range(200)]
    return json.dumps(rows)


messages = [{"role": "user", "content": "How many users have a score above 100?"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(response.content[0].text)
        break

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            raw = simulate_db_query(block.input.get("sql", ""))
            compacted = compact_tool_result(raw)
            print(f"Raw result: {len(raw):,} chars → Compacted: {len(compacted):,} chars")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": compacted,
            })

    messages.append({"role": "user", "content": tool_results})
```

**Expected Token Savings:** 80–95% reduction for large list results; a 200-row JSON response goes from ~8,000 to ~500 tokens.
**Environment:** Python 3.9+; max_chars tunable per tool type.

---

### Option 2: Haiku-powered result summarization before storing

```python
import json
import anthropic

client = anthropic.Anthropic()
haiku_client = anthropic.Anthropic()  # Same client, different call

SUMMARIZE_THRESHOLD_CHARS = 1500  # Summarize results larger than this


def summarize_tool_result(tool_name: str, tool_input: dict, raw_result: str) -> str:
    """
    Use Haiku to generate a compact summary of a large tool result.
    The summary replaces the raw result in conversation history.
    """
    if len(raw_result) <= SUMMARIZE_THRESHOLD_CHARS:
        return raw_result  # Small enough — use as-is

    # Build a targeted summarization prompt based on tool type
    prompt = (
        f"The tool '{tool_name}' was called with: {json.dumps(tool_input)}\n\n"
        f"It returned this result (may be truncated):\n{raw_result[:3000]}\n\n"
        "Summarize the key facts from this result in 3–5 bullet points. "
        "Include specific numbers, IDs, and values that will be needed for follow-up actions. "
        "Omit redundant or low-value details."
    )

    summary_response = haiku_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    summary = summary_response.content[0].text
    return f"[Summarized result — original was {len(raw_result):,} chars]\n{summary}"


TOOLS = [
    {
        "name": "fetch_api_response",
        "description": "Fetch data from an external API",
        "input_schema": {
            "type": "object",
            "properties": {"endpoint": {"type": "string"}},
            "required": ["endpoint"],
        },
    },
    {
        "name": "read_log_file",
        "description": "Read a log file and return its contents",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "lines": {"type": "integer", "default": 100}},
            "required": ["path"],
        },
    },
]


def simulate_tool(name: str, inputs: dict) -> str:
    if name == "fetch_api_response":
        # Simulate large API response
        items = [{"id": i, "status": "active", "data": f"value_{i}" * 5} for i in range(100)]
        return json.dumps({"items": items, "total": 100, "page": 1})
    if name == "read_log_file":
        # Simulate large log file
        lines = [f"2026-04-15 10:{i:02d}:00 INFO Request processed user_id={i} latency={i*10}ms" for i in range(200)]
        return "\n".join(lines)
    return "Unknown tool"


messages = [{"role": "user", "content": "Check the API response and log file, then tell me if there are any anomalies."}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=768,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(response.content[0].text)
        break

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            raw = simulate_tool(block.name, block.input)
            compacted = summarize_tool_result(block.name, block.input, raw)
            print(f"[{block.name}] {len(raw):,} → {len(compacted):,} chars")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": compacted,
            })

    messages.append({"role": "user", "content": tool_results})
```

**Expected Token Savings:** Haiku summary costs ~500 tokens; saves 2,000–20,000 tokens per large result stored in history. Break-even at ~3 turns.
**Environment:** Python 3.9+; Haiku summarization adds ~100ms latency; disable for small results to avoid overhead.

---

### Option 3: Per-tool result size budget with auto-compaction

```python
import json
import anthropic

client = anthropic.Anthropic()

# Per-tool token budgets (chars → ~tokens at 4:1 ratio)
TOOL_BUDGETS: dict[str, int] = {
    "query_database": 800,       # DB results: keep top rows
    "read_file": 1200,            # Files: keep beginning + end
    "web_search": 600,            # Search: keep top result only
    "fetch_api": 1000,            # API: keep key fields
    "__default__": 1500,          # Unknown tools
}


def compact_by_type(tool_name: str, raw: str) -> str:
    budget = TOOL_BUDGETS.get(tool_name, TOOL_BUDGETS["__default__"])

    if len(raw) <= budget:
        return raw

    # JSON list: keep top N items
    try:
        data = json.loads(raw)
        if isinstance(data, list) and len(data) > 0:
            compact_list = []
            total_len = 0
            for item in data:
                item_str = json.dumps(item)
                if total_len + len(item_str) > budget - 100:
                    break
                compact_list.append(item)
                total_len += len(item_str)
            omitted = len(data) - len(compact_list)
            result = json.dumps(compact_list, indent=2)
            if omitted > 0:
                result += f"\n[...{omitted} more items omitted. Total: {len(data)}]"
            return result

        # JSON dict: drop large string values
        if isinstance(data, dict):
            compacted = {}
            for k, v in data.items():
                if isinstance(v, str) and len(v) > 200:
                    compacted[k] = v[:200] + f"...[{len(v)-200} chars omitted]"
                elif isinstance(v, list) and len(v) > 10:
                    compacted[k] = v[:10] + [f"...{len(v)-10} more items"]
                else:
                    compacted[k] = v
            return json.dumps(compacted, indent=2)[:budget]
    except (json.JSONDecodeError, ValueError):
        pass

    # Plain text: keep beginning + tail
    if tool_name == "read_file":
        half = budget // 2
        return raw[:half] + f"\n...[{len(raw) - budget} chars omitted]...\n" + raw[-half:]

    return raw[:budget] + f"\n...[{len(raw) - budget} chars omitted]"


def run_agent(user_message: str) -> str:
    tools = [
        {
            "name": "query_database",
            "description": "Query database and return JSON rows",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        },
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    ]

    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                # Simulate large result
                raw = json.dumps([{"id": i, "val": f"data_{i}"} for i in range(300)])
                compacted = compact_by_type(block.name, raw)
                saving_pct = (1 - len(compacted) / len(raw)) * 100
                print(f"[{block.name}] {len(raw):,} → {len(compacted):,} chars ({saving_pct:.0f}% saved)")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": compacted})

        messages.append({"role": "user", "content": results})


print(run_agent("Query the top users from the database"))
```

**Expected Token Savings:** Per-tool budgets target compaction where it matters most (large DB results) while preserving small results intact.
**Environment:** Python 3.9+; budget values tunable per deployment; JSON-aware compaction preserves structure.

---

### Option 4: Extract-only relevant fields from structured results

```python
import json
from typing import Any
import anthropic

client = anthropic.Anthropic()


def extract_fields(data: Any, fields: list[str]) -> Any:
    """
    Extract only the specified fields from a dict or list of dicts.
    Supports dot-notation for nested fields: "user.name"
    """
    def get_nested(obj: dict, path: str) -> Any:
        parts = path.split(".")
        for part in parts:
            if isinstance(obj, dict):
                obj = obj.get(part)
            else:
                return None
        return obj

    if isinstance(data, list):
        return [
            {f.split(".")[-1]: get_nested(item, f) for f in fields}
            for item in data
            if isinstance(item, dict)
        ]
    if isinstance(data, dict):
        return {f.split(".")[-1]: get_nested(data, f) for f in fields}
    return data


# Tool-specific field extractors — only keep what the agent needs
TOOL_FIELD_EXTRACTORS: dict[str, list[str]] = {
    "list_users": ["id", "email", "status", "created_at"],
    "get_orders": ["order_id", "amount", "status", "user_id"],
    "fetch_products": ["sku", "name", "price", "stock"],
    "search_logs": ["timestamp", "level", "message"],
}


def compact_structured_result(tool_name: str, raw_result: str) -> str:
    """Extract only relevant fields from a structured tool result."""
    fields = TOOL_FIELD_EXTRACTORS.get(tool_name)
    if not fields:
        return raw_result  # No extractor — return as-is

    try:
        data = json.loads(raw_result)
        extracted = extract_fields(data, fields)
        compacted = json.dumps(extracted, indent=2)
        reduction = (1 - len(compacted) / max(len(raw_result), 1)) * 100
        header = f"[Fields extracted: {', '.join(fields)} — {reduction:.0f}% reduction]\n"
        return header + compacted
    except (json.JSONDecodeError, ValueError):
        return raw_result


TOOLS = [
    {
        "name": "list_users",
        "description": "List all users from the database",
        "input_schema": {
            "type": "object",
            "properties": {"active_only": {"type": "boolean", "default": True}},
        },
    },
    {
        "name": "get_orders",
        "description": "Get recent orders",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 30}},
        },
    },
]


def simulate_rich_tool(name: str) -> str:
    """Simulate tools that return many fields, most of which are not needed."""
    if name == "list_users":
        users = [{
            "id": f"u_{i}", "email": f"user{i}@ex.com", "status": "active",
            "created_at": "2026-01-01", "password_hash": "xxx", "internal_flags": [1, 2, 3],
            "metadata": {"long_field": "x" * 200}, "last_login": "2026-04-01",
            "preferences": {"theme": "dark", "notifications": True, "data": "y" * 100},
        } for i in range(50)]
        return json.dumps(users)
    if name == "get_orders":
        orders = [{
            "order_id": f"ord_{i}", "user_id": f"u_{i}", "amount": i * 9.99,
            "status": "completed", "line_items": [{"sku": f"p{j}", "qty": j} for j in range(10)],
            "shipping_address": {"street": "123 Main", "city": "NY", "zip": "10001"},
            "internal_notes": "x" * 500,
        } for i in range(30)]
        return json.dumps(orders)
    return "{}"


messages = [{"role": "user", "content": "Show me the active users and their recent orders"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(response.content[0].text[:400])
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            raw = simulate_rich_tool(block.name)
            compacted = compact_structured_result(block.name, raw)
            print(f"[{block.name}] {len(raw):,} → {len(compacted):,} chars")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": compacted})

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Field extraction reduces result size by 60–90% for APIs returning many unused fields; no information loss for needed fields.
**Environment:** Python 3.9+; field extractor registry is a 5-line config; supports dot-notation for nested JSON.

---

### Option 5: Rolling compaction of old tool results in history

```python
import json
import anthropic

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()

CONTEXT_BUDGET_CHARS = 40_000   # ~10,000 tokens — adjust to your limit
COMPACT_THRESHOLD = 0.75        # Compact when history reaches 75% of budget


def estimate_history_size(messages: list[dict]) -> int:
    """Estimate character count of the full message history."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(json.dumps(block))
    return total


def compact_old_tool_results(messages: list[dict]) -> list[dict]:
    """
    Replace verbose tool_result content in older messages with one-line summaries.
    Preserves the most recent N tool results at full fidelity.
    """
    KEEP_RECENT_RESULTS = 3  # Keep last 3 tool results at full size
    tool_result_indices = []

    # Find all tool_result blocks in message history
    for i, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_result_indices.append(i)

    # Compact all but the most recent N
    to_compact = tool_result_indices[:-KEEP_RECENT_RESULTS] if len(tool_result_indices) > KEEP_RECENT_RESULTS else []

    if not to_compact:
        return messages

    compacted = []
    compact_set = set(to_compact)

    for i, msg in enumerate(messages):
        if i not in compact_set:
            compacted.append(msg)
            continue

        # Compact tool results in this message
        new_content = []
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                original = block.get("content", "")
                if len(original) > 200:
                    summary_resp = haiku.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=80,
                        messages=[{"role": "user", "content": f"Summarize in one sentence: {original[:1000]}"}],
                    )
                    summary = summary_resp.content[0].text
                    new_content.append({**block, "content": f"[Compacted] {summary}"})
                else:
                    new_content.append(block)
            else:
                new_content.append(block)

        compacted.append({**msg, "content": new_content})

    print(f"[Compaction] Compressed {len(to_compact)} old tool results")
    return compacted


def run_long_conversation(turns: list[str]) -> None:
    messages: list[dict] = []

    for user_msg in turns:
        # Check if compaction is needed
        size = estimate_history_size(messages)
        if size > CONTEXT_BUDGET_CHARS * COMPACT_THRESHOLD:
            print(f"[Compaction triggered] History size: {size:,} chars")
            messages = compact_old_tool_results(messages)
            print(f"[After compaction] History size: {estimate_history_size(messages):,} chars")

        messages.append({"role": "user", "content": user_msg})
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})
        print(f"Turn: {user_msg[:50]} → {reply[:80]}\n")


run_long_conversation([
    "What is prompt caching?",
    "How does tool use work in Claude?",
    "What are the context window limits?",
    "How should I handle rate limits?",
    "What models are available?",
])
```

**Expected Token Savings:** Rolling compaction keeps history size bounded; prevents context overflow on conversations with 10+ tool calls.
**Environment:** Python 3.9+; Haiku compaction costs ~50 tokens per result; activates only when history approaches the budget.

---

### Option 6: Tool result size tracking with per-turn budget enforcement

```python
import json
from dataclasses import dataclass, field
import anthropic

client = anthropic.Anthropic()


@dataclass
class ToolResultBudgetTracker:
    """Tracks cumulative tool result size and enforces per-turn budgets."""
    per_turn_budget_chars: int = 3000
    cumulative_limit_chars: int = 20000
    _cumulative_size: int = 0
    _turn_sizes: list[int] = field(default_factory=list)

    def compact(self, tool_name: str, raw: str) -> str:
        turn_used = sum(self._turn_sizes[-1:]) if self._turn_sizes else 0

        # If this result would exceed per-turn budget, truncate it
        remaining_budget = self.per_turn_budget_chars - turn_used
        if len(raw) > remaining_budget and remaining_budget > 200:
            compacted = raw[:remaining_budget] + f"\n...[{len(raw) - remaining_budget} chars over budget — truncated]"
        elif remaining_budget <= 200:
            compacted = f"[Result from {tool_name} omitted — turn budget exhausted. Raw size: {len(raw):,} chars]"
        else:
            compacted = raw

        self._cumulative_size += len(compacted)
        if not self._turn_sizes:
            self._turn_sizes.append(len(compacted))
        else:
            self._turn_sizes[-1] += len(compacted)

        return compacted

    def next_turn(self) -> None:
        self._turn_sizes.append(0)

    def report(self) -> str:
        return (
            f"Tool result budget: cumulative={self._cumulative_size:,}/{self.cumulative_limit_chars:,} chars, "
            f"turns={len(self._turn_sizes)}, "
            f"avg_per_turn={self._cumulative_size // max(len(self._turn_sizes), 1):,}"
        )


TOOLS = [
    {
        "name": "search",
        "description": "Search for information",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    },
    {
        "name": "fetch",
        "description": "Fetch a URL",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
]

tracker = ToolResultBudgetTracker(per_turn_budget_chars=3000, cumulative_limit_chars=20000)
messages = [{"role": "user", "content": "Search for Python and fetch the top result"}]

for turn in range(5):
    tracker.next_turn()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            # Simulate variable-size tool results
            raw = "x" * (3000 + turn * 2000)  # Gets bigger each turn
            compacted = tracker.compact(block.name, raw)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": compacted})

    messages.append({"role": "user", "content": results})
    print(f"Turn {turn}: {tracker.report()}")
```

**Expected Token Savings:** Hard per-turn budget prevents runaway tool result growth; cumulative tracking enables proactive compaction before context overflow.
**Environment:** Python 3.9+; budget values tunable per model's context limit; zero dependencies.

---

| Option | Approach | Compaction Trigger | Best For |
|--------|----------|-------------------|----------|
| 1 | Hard truncation | Result size > max_chars | Simple agents, any tool type |
| 2 | Haiku summarization | Size > threshold | Complex results needing semantic compression |
| 3 | Per-tool size budgets | Tool-specific thresholds | Mixed tool libraries |
| 4 | Field extraction | Structured JSON results | APIs with many unused fields |
| 5 | Rolling history compaction | History size > 75% budget | Long multi-turn conversations |
| 6 | Budget tracker with enforcement | Per-turn + cumulative | Production agents with strict limits |
