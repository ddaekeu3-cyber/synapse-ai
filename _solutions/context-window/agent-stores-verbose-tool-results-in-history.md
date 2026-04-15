---
layout: solution
title: "Agent Stores Verbose Tool Results in Conversation History"
category: context-window
description: "Agent appends raw tool responses — 50 KB JSON blobs, full HTML pages, entire database query results — directly into the conversation history. After 10 tool calls the context window is full, the agent truncates its instructions, and token costs spike 5–10x."
tags: [context-window, token-cost, tool-results, summarization, compression, history]
---

## Symptom

After a few tool calls fetching large data (API responses, search results, database dumps), the agent starts losing track of the original goal. Logs show context windows at 180K+ tokens — mostly tool results. The final user turn costs $0.40 when it should cost $0.02. In worst cases, the agent truncates its system prompt to fit the history and starts ignoring core instructions.

Raw tool result size in typical REST API call: **2–200 KB**
After compression to agent-relevant facts: **50–500 bytes**

## Root Cause

Tool results are stored verbatim in `messages` as `tool_result` content blocks. There is no summarization, no field selection, no size cap. Each call to a search engine, database, or external API compounds the context bloat. The model never needed the full 200-record JSON array — it needed 3 fields from the first 5 records.

## Fix

---

### Option 1 — Extract Only Relevant Fields Before Storing

After each tool call, pass the result through a field extractor that keeps only the fields the agent will use. Store the compact version, not the raw response.

```python
import json
import anthropic
from typing import Any

client = anthropic.Anthropic()

# Field allowlists per tool — only these fields survive into history
FIELD_ALLOWLISTS: dict[str, list[str]] = {
    "search_web":        ["title", "url", "snippet"],
    "query_database":    ["id", "name", "status", "created_at"],
    "get_weather":       ["city", "temp_c", "condition", "humidity"],
    "fetch_user":        ["user_id", "name", "email", "plan"],
    "list_orders":       ["order_id", "status", "total", "date"],
}

# Maximum records to keep per list response
MAX_RECORDS: dict[str, int] = {
    "search_web":     5,
    "query_database": 10,
    "list_orders":    8,
}

def extract_fields(tool_name: str, raw_result: Any) -> Any:
    """Keep only allowlisted fields from tool results. Truncate long lists."""
    allowlist = FIELD_ALLOWLISTS.get(tool_name)
    max_records = MAX_RECORDS.get(tool_name)

    if allowlist is None:
        return raw_result  # No allowlist — pass through unchanged

    def filter_record(record: dict) -> dict:
        return {k: v for k, v in record.items() if k in allowlist}

    if isinstance(raw_result, dict):
        # Single record
        if "results" in raw_result or "items" in raw_result or "data" in raw_result:
            list_key = next(k for k in ("results", "items", "data") if k in raw_result)
            records = raw_result[list_key]
            if isinstance(records, list):
                filtered = [filter_record(r) for r in records[:max_records]]
                total = len(raw_result[list_key])
                result = {list_key: filtered}
                if total > (max_records or total):
                    result["_truncated"] = f"Showing {len(filtered)}/{total} records"
                # Preserve top-level non-list fields that are in allowlist
                for k, v in raw_result.items():
                    if k != list_key and k in allowlist:
                        result[k] = v
                return result
        return filter_record(raw_result)

    if isinstance(raw_result, list):
        filtered = [filter_record(r) if isinstance(r, dict) else r
                    for r in raw_result[:max_records]]
        if len(raw_result) > (max_records or len(raw_result)):
            return {"items": filtered, "_truncated": f"Showing {len(filtered)}/{len(raw_result)}"}
        return filtered

    return raw_result

def compress_tool_result(tool_name: str, raw_result_str: str) -> str:
    """Parse, filter, and re-serialize a tool result."""
    try:
        raw = json.loads(raw_result_str)
        compressed = extract_fields(tool_name, raw)
        result = json.dumps(compressed, separators=(",", ":"))
        original_size = len(raw_result_str)
        compressed_size = len(result)
        if original_size > 500:
            print(f"[Compress] {tool_name}: {original_size:,} → {compressed_size:,} bytes ({compressed_size/original_size:.0%})")
        return result
    except json.JSONDecodeError:
        return raw_result_str  # Not JSON — return as-is

# Simulated tools with verbose responses
def search_web(query: str) -> str:
    results = [
        {
            "title": f"Result {i}",
            "url": f"https://example.com/result-{i}",
            "snippet": f"Relevant snippet about {query}...",
            "full_content": "A" * 5000,   # Would be 5KB per result
            "metadata": {"crawled_at": "2025-04-14", "lang": "en", "domain_rank": i * 100},
            "related_queries": [f"related {j}" for j in range(20)],
        }
        for i in range(10)
    ]
    return json.dumps({"results": results, "total_results": 1_240_000, "search_time_ms": 234})

def query_database(table: str, limit: int = 50) -> str:
    records = [
        {
            "id": i,
            "name": f"Record {i}",
            "status": "active" if i % 3 else "inactive",
            "created_at": "2025-01-01",
            "updated_at": "2025-04-14",
            "internal_ref": f"REF-{i:06d}",
            "raw_payload": {"nested": "data" * 100},
        }
        for i in range(limit)
    ]
    return json.dumps({"data": records, "total": limit, "page": 1})

TOOLS = [
    {"name": "search_web", "description": "Search the web.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "query_database", "description": "Query a database table.",
     "input_schema": {"type": "object",
                      "properties": {"table": {"type": "string"}, "limit": {"type": "integer"}},
                      "required": ["table"]}},
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "search_web":
                    raw = search_web(**block.input)
                elif block.name == "query_database":
                    raw = query_database(**block.input)
                else:
                    raw = json.dumps({"error": "unknown tool"})

                # Compress before storing in history
                compressed = compress_tool_result(block.name, raw)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": compressed,   # Compact, not raw
                })

        messages.append({"role": "user", "content": tool_results})

print(run_agent("Search the web for 'Python async best practices' and query the users table."))
```

**Expected Token Savings:** 60–95% per tool call — removes unused fields and truncates long lists
**Environment:** `pip install anthropic`

---

### Option 2 — Haiku-Based Result Summarizer

After each tool call, use claude-haiku to summarise the result into a compact, agent-relevant summary. Store the summary instead of the raw result.

```python
import json
import asyncio
import anthropic

async_client = anthropic.AsyncAnthropic()

# Summarization prompts per tool type
SUMMARY_PROMPTS: dict[str, str] = {
    "search_web": "Summarise these search results in 3-5 bullet points. Focus on key facts relevant to the original query. Be concise.",
    "fetch_document": "Extract the 3-5 most important facts from this document. One sentence each.",
    "run_sql": "Summarise the query results: total rows, key aggregates, notable patterns. Max 100 words.",
    "call_api": "Extract the key fields and values from this API response. Omit nulls, metadata, and pagination info.",
}

DEFAULT_SUMMARY_PROMPT = "Summarise this data in 2-4 sentences. Include only information relevant to answering the user's question."

async def summarize_result(
    tool_name: str,
    raw_result: str,
    user_context: str = "",
    max_summary_tokens: int = 200,
) -> str:
    """Summarise a tool result using Haiku. Much cheaper than storing raw data."""
    raw_size = len(raw_result)
    # Only summarize if result is large enough to matter
    if raw_size < 500:
        return raw_result

    prompt = SUMMARY_PROMPTS.get(tool_name, DEFAULT_SUMMARY_PROMPT)
    context_note = f"\nUser's goal: {user_context}" if user_context else ""

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_summary_tokens,
        system=f"{prompt}{context_note}",
        messages=[{"role": "user", "content": raw_result[:40_000]}],  # Cap input to Haiku
    )
    summary = response.content[0].text.strip()
    print(f"[Summarize] {tool_name}: {raw_size:,} bytes → {len(summary)} bytes ({len(summary)/raw_size:.1%})")
    return summary

# Simulated tools
async def fetch_document(url: str) -> str:
    return "<html><body>" + "Lorem ipsum dolor sit amet. " * 2000 + "</body></html>"

async def run_sql(query: str) -> str:
    rows = [{"id": i, "revenue": i * 1234.56, "region": "NA", "product": f"SKU-{i}"} for i in range(100)]
    return json.dumps({"rows": rows, "total_rows": 100, "query_time_ms": 45})

async def call_api(endpoint: str) -> str:
    return json.dumps({
        "data": [{"user_id": i, "score": i * 0.1, "metadata": {"v": "1", "ts": "2025-04-14"}} for i in range(50)],
        "pagination": {"page": 1, "total_pages": 20, "next_cursor": "abc123"},
        "api_version": "v3.1",
        "rate_limit": {"remaining": 95, "reset_at": "2025-04-14T12:00:00Z"},
    })

TOOLS = [
    {"name": "fetch_document", "description": "Fetch a web document.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "run_sql", "description": "Run a SQL query.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "call_api", "description": "Call an external API.",
     "input_schema": {"type": "object", "properties": {"endpoint": {"type": "string"}}, "required": ["endpoint"]}},
]

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "fetch_document":
                    raw = await fetch_document(**block.input)
                elif block.name == "run_sql":
                    raw = await run_sql(**block.input)
                elif block.name == "call_api":
                    raw = await call_api(**block.input)
                else:
                    raw = json.dumps({"error": "unknown tool"})

                # Summarise before storing
                summary = await summarize_result(block.name, raw, user_context=user_message)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": summary,
                })

        messages.append({"role": "user", "content": tool_results})

result = asyncio.run(run_agent(
    "Fetch the homepage at https://example.com, run a revenue query, and call the users API endpoint. Summarise findings."
))
print(f"\n{result}")
```

**Expected Token Savings:** 70–95% per large tool result — Haiku cost ~0.1% of Sonnet at 200-token summaries
**Environment:** `pip install anthropic`

---

### Option 3 — Size-Capped Tool Result Store with Overflow to External Cache

Store full tool results in an external cache (Redis / in-memory dict). Inject only a compact summary + retrieval reference into the conversation history. The agent can request the full result if needed via a `fetch_cached_result` tool.

```python
import json
import hashlib
import anthropic
from typing import Any

client = anthropic.Anthropic()

class ToolResultCache:
    MAX_INLINE_BYTES = 512   # Results larger than this go to cache

    def __init__(self):
        self._store: dict[str, str] = {}

    def _key(self, content: str) -> str:
        return "tr_" + hashlib.sha256(content.encode()).hexdigest()[:12]

    def store(self, tool_name: str, raw_result: str) -> tuple[str, bool]:
        """
        If result fits inline, return it directly.
        If too large, cache it and return a compact reference.
        Returns (content_for_history, was_cached).
        """
        if len(raw_result) <= self.MAX_INLINE_BYTES:
            return raw_result, False

        key = self._key(raw_result)
        self._store[key] = raw_result

        # Build a compact summary for the inline history
        try:
            parsed = json.loads(raw_result)
            if isinstance(parsed, dict):
                top_keys = list(parsed.keys())[:5]
                first_values = {k: parsed[k] for k in top_keys if not isinstance(parsed[k], (list, dict))}
                list_counts = {k: f"[{len(parsed[k])} items]" for k in top_keys if isinstance(parsed[k], list)}
                preview = {**first_values, **list_counts}
            elif isinstance(parsed, list):
                preview = {"type": "list", "count": len(parsed), "first_item_keys": list(parsed[0].keys()) if parsed and isinstance(parsed[0], dict) else []}
            else:
                preview = {"value": str(parsed)[:200]}
        except json.JSONDecodeError:
            preview = {"text_preview": raw_result[:200]}

        reference = json.dumps({
            "cache_key": key,
            "tool": tool_name,
            "size_bytes": len(raw_result),
            "preview": preview,
            "note": "Full result cached. Call fetch_cached_result to retrieve.",
        }, separators=(",", ":"))

        print(f"[Cache] {tool_name}: {len(raw_result):,} bytes → cached as {key} ({len(reference)} byte reference)")
        return reference, True

    def retrieve(self, cache_key: str) -> str:
        result = self._store.get(cache_key)
        if result is None:
            return json.dumps({"error": f"Cache key not found: {cache_key}"})
        return result

cache = ToolResultCache()

def get_large_dataset(name: str) -> str:
    rows = [{"id": i, "value": f"data_{i}", "score": i * 0.1} for i in range(500)]
    return json.dumps({"dataset": name, "rows": rows, "count": 500})

def get_small_result(key: str) -> str:
    return json.dumps({"key": key, "value": "found", "timestamp": "2025-04-14"})

TOOLS = [
    {"name": "get_large_dataset", "description": "Fetch a large dataset.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_small_result", "description": "Fetch a small key-value result.",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "fetch_cached_result",
     "description": "Retrieve a full tool result that was cached due to large size. Use when you need the complete data.",
     "input_schema": {"type": "object", "properties": {"cache_key": {"type": "string"}}, "required": ["cache_key"]}},
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "get_large_dataset":
                    raw = get_large_dataset(**block.input)
                    inline, _ = cache.store(block.name, raw)
                    result = inline
                elif block.name == "get_small_result":
                    raw = get_small_result(**block.input)
                    inline, _ = cache.store(block.name, raw)
                    result = inline
                elif block.name == "fetch_cached_result":
                    result = cache.retrieve(block.input["cache_key"])
                else:
                    result = json.dumps({"error": "unknown tool"})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

print(run_agent("Fetch the 'sales_2025' dataset and the config for key 'api_version'. Summarise what you found."))
```

**Expected Token Savings:** 80–98% for large results — reference in history is ~100 bytes vs 50 KB raw
**Environment:** `pip install anthropic`

---

### Option 4 — Progressive Disclosure: Store Summary, Fetch Detail on Demand

Automatically generate a two-tier result: a compact summary always stored in history, and a full detail blob retrievable on demand. The agent decides whether it needs the detail.

```python
import json
import asyncio
import anthropic
from dataclasses import dataclass
from typing import Any

async_client = anthropic.AsyncAnthropic()

@dataclass
class TieredResult:
    summary: str          # Always stored in history (~100–200 chars)
    detail: str           # Available on demand
    result_id: str

    def to_history_content(self) -> str:
        return json.dumps({
            "summary": self.summary,
            "result_id": self.result_id,
            "detail_available": True,
            "instruction": "Use summary for quick answers. Call get_detail(result_id) if you need specifics.",
        })

class ProgressiveResultStore:
    def __init__(self):
        self._details: dict[str, str] = {}
        self._counter = 0

    async def create(self, tool_name: str, raw_result: str) -> TieredResult:
        self._counter += 1
        result_id = f"{tool_name}_{self._counter}"
        self._details[result_id] = raw_result

        if len(raw_result) < 300:
            return TieredResult(summary=raw_result, detail=raw_result, result_id=result_id)

        # Use Haiku to generate a compact summary
        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system="Summarise this data in 1-2 sentences (max 150 chars). Be specific about counts and key values.",
            messages=[{"role": "user", "content": raw_result[:20_000]}],
        )
        summary = response.content[0].text.strip()
        return TieredResult(summary=summary, detail=raw_result, result_id=result_id)

    def get_detail(self, result_id: str) -> str:
        return self._details.get(result_id, json.dumps({"error": f"No detail for {result_id}"}))

store = ProgressiveResultStore()

async def search_products(category: str) -> str:
    products = [
        {"id": i, "name": f"Product {i}", "price": 9.99 + i, "stock": 100 - i, "rating": 4.2}
        for i in range(50)
    ]
    return json.dumps({"category": category, "products": products, "total": 50, "page": 1})

async def get_analytics(metric: str) -> str:
    return json.dumps({
        "metric": metric,
        "value": 42_157,
        "change_pct": +8.3,
        "period": "2025-Q1",
        "breakdown": {"NA": 18000, "EU": 15000, "APAC": 9157},
    })

TOOLS = [
    {"name": "search_products", "description": "Search products by category.",
     "input_schema": {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]}},
    {"name": "get_analytics", "description": "Get analytics for a metric.",
     "input_schema": {"type": "object", "properties": {"metric": {"type": "string"}}, "required": ["metric"]}},
    {"name": "get_detail",
     "description": "Retrieve full detail for a previous tool result by result_id.",
     "input_schema": {"type": "object", "properties": {"result_id": {"type": "string"}}, "required": ["result_id"]}},
]

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "search_products":
                    raw = await search_products(**block.input)
                    tiered = await store.create(block.name, raw)
                    result = tiered.to_history_content()
                    print(f"[Tier] {block.name}: summary='{tiered.summary[:60]}...'")
                elif block.name == "get_analytics":
                    raw = await get_analytics(**block.input)
                    tiered = await store.create(block.name, raw)
                    result = tiered.to_history_content()
                elif block.name == "get_detail":
                    result = store.get_detail(block.input["result_id"])
                else:
                    result = json.dumps({"error": "unknown tool"})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

result = asyncio.run(run_agent("Search electronics products and get revenue analytics. Give me a summary."))
print(f"\n{result}")
```

**Expected Token Savings:** 70–90% — summaries are ~100 bytes; details only fetched when truly needed
**Environment:** `pip install anthropic`

---

### Option 5 — Rolling Tool Result Compaction

After N tool calls, compact all historical tool results in the messages array into a single merged summary. Keep the live conversation window small regardless of how many tools were called.

```python
import json
import asyncio
import anthropic

async_client = anthropic.AsyncAnthropic()

COMPACT_EVERY_N = 4   # Compact after every 4 tool result turns
MAX_SUMMARY_TOKENS = 300

async def compact_tool_history(tool_turns: list[dict]) -> dict:
    """Merge multiple tool result turns into one compact summary message."""
    all_results = []
    for turn in tool_turns:
        content = turn.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    all_results.append(block.get("content", ""))
        elif isinstance(content, str):
            all_results.append(content)

    combined = "\n\n---\n\n".join(all_results)

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=MAX_SUMMARY_TOKENS,
        system="Summarise these tool results into a compact fact sheet. Preserve all key numbers, names, and statuses. Omit verbose metadata.",
        messages=[{"role": "user", "content": combined[:30_000]}],
    )
    summary = response.content[0].text.strip()
    print(f"[Compact] {len(tool_turns)} tool turns → {len(combined):,} bytes → {len(summary)} byte summary")

    # Return as a single user message with a synthetic tool result
    return {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "compacted",
            "content": json.dumps({"_compacted_results": summary, "_turns_merged": len(tool_turns)}),
        }],
    }

class CompactingConversation:
    def __init__(self):
        self.messages: list[dict] = []
        self._tool_turn_count = 0
        self._tool_turns_since_compact: list[dict] = []

    def add(self, message: dict):
        self.messages.append(message)
        if message.get("role") == "user":
            content = message.get("content", [])
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            ):
                self._tool_turns_since_compact.append(message)
                self._tool_turn_count += 1

    async def maybe_compact(self) -> bool:
        if len(self._tool_turns_since_compact) < COMPACT_EVERY_N:
            return False

        print(f"\n[Compact] Compacting {len(self._tool_turns_since_compact)} tool turns...")
        compacted = await compact_tool_history(self._tool_turns_since_compact)

        # Replace the tool turns in messages with the compacted version
        for turn in self._tool_turns_since_compact:
            self.messages.remove(turn)
        self.messages.append(compacted)
        self._tool_turns_since_compact = []
        return True

    def get_messages(self) -> list[dict]:
        return self.messages

conversation = CompactingConversation()

async def mock_tool_call(name: str, args: dict) -> str:
    data = {
        "search": {"results": [{"title": f"Result {i}", "score": 0.9 - i*0.1} for i in range(20)]},
        "fetch":  {"content": "Page content " * 500},
        "query":  {"rows": [{"id": i, "val": i * 100} for i in range(30)]},
        "stats":  {"mean": 42.3, "std": 5.1, "n": 1000, "p95": 61.2},
    }
    tool_type = name.split("_")[0]
    return json.dumps(data.get(tool_type, {"result": "ok"}))

TOOLS = [{"name": t, "description": f"Tool {t}.",
           "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}
         for t in ["search_docs", "fetch_page", "query_db", "stats_api", "search_code", "fetch_config"]]

async def run_agent(user_message: str) -> str:
    conversation.add({"role": "user", "content": user_message})

    while True:
        await conversation.maybe_compact()

        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=conversation.get_messages(),
        )

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            conversation.add({"role": "assistant", "content": text})
            return text

        conversation.add({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                raw = await mock_tool_call(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": raw})

        tool_turn = {"role": "user", "content": tool_results}
        conversation.add(tool_turn)

result = asyncio.run(run_agent(
    "Search docs, fetch the homepage, query the DB, get stats, search code examples, and fetch config. Then summarise all findings."
))
print(f"\n{result}")
```

**Expected Token Savings:** 60–80% after compaction — long tool chains stay within budget
**Environment:** `pip install anthropic`

---

### Option 6 — Prompt-Cached Tool Result Reference

Store large tool results as prompt-cached system context. Tool results appear in history only as a reference ID. The cache is reused across turns without re-sending the data.

```python
import json
import anthropic

client = anthropic.Anthropic()

class CachedResultLibrary:
    """
    Stores large tool results as cached system blocks.
    The agent sees only a reference; the full data is in the cache.
    """
    def __init__(self):
        self._library: dict[str, str] = {}

    def store(self, result_id: str, content: str):
        self._library[result_id] = content

    def build_system_with_cache(self, base_system: str) -> list[dict]:
        """
        Build a system prompt with all stored results as cached blocks.
        Returns a list of content blocks for the system parameter.
        """
        blocks = [{"type": "text", "text": base_system}]
        if self._library:
            data_section = "## Tool Results Reference Library\n\n"
            for rid, content in self._library.items():
                data_section += f"### {rid}\n{content}\n\n"
            blocks.append({
                "type": "text",
                "text": data_section,
                "cache_control": {"type": "ephemeral"},
            })
        return blocks

    def make_reference(self, result_id: str, tool_name: str, size: int) -> str:
        return json.dumps({
            "result_id": result_id,
            "tool": tool_name,
            "size_bytes": size,
            "location": "system_cache",
            "instruction": f"Full data available in system context under '### {result_id}'",
        })

result_library = CachedResultLibrary()

def fetch_large_report(report_id: str) -> str:
    rows = [{"month": f"2025-{i:02d}", "revenue": 100_000 + i * 5_000, "units": 1000 + i * 50} for i in range(1, 13)]
    return json.dumps({"report_id": report_id, "annual_data": rows, "currency": "USD", "generated": "2025-04-14"})

def get_config(service: str) -> str:
    return json.dumps({"service": service, "max_connections": 100, "timeout_ms": 5000, "region": "us-east-1"})

BASE_SYSTEM = """You are a business analytics assistant.
Tool results are stored in the system context reference library above.
When a tool result references location 'system_cache', look for its data under '### result_id' in the system context."""

TOOLS = [
    {"name": "fetch_large_report", "description": "Fetch an annual business report.",
     "input_schema": {"type": "object", "properties": {"report_id": {"type": "string"}}, "required": ["report_id"]}},
    {"name": "get_config", "description": "Get service configuration.",
     "input_schema": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]}},
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    turn = 0

    while True:
        turn += 1
        system_blocks = result_library.build_system_with_cache(BASE_SYSTEM)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_blocks,
            tools=TOOLS,
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        cache_info = getattr(response.usage, "cache_read_input_tokens", 0)
        if cache_info:
            print(f"[Cache] Turn {turn}: {cache_info} tokens served from cache")

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "fetch_large_report":
                    raw = fetch_large_report(**block.input)
                elif block.name == "get_config":
                    raw = get_config(**block.input)
                else:
                    raw = json.dumps({"error": "unknown"})

                result_id = f"{block.name}_{block.id[:6]}"
                result_library.store(result_id, raw)
                reference = result_library.make_reference(result_id, block.name, len(raw))
                print(f"[Cache] Stored {result_id}: {len(raw):,} bytes → {len(reference)} byte reference in history")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": reference,
                })

        messages.append({"role": "user", "content": tool_results})

result = run_agent("Fetch the 2025-annual report and the analytics service config. Summarise revenue trends.")
print(f"\n{result}")
```

**Expected Token Savings:** 85–95% in history; cached system tokens reused at ~10% cost across turns
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Compression Method | Retrieval | Best For |
|--------|------------------|-----------|----------|
| Field Allowlist | Remove unused keys | Immediate | Known API schemas |
| Haiku Summarizer | LLM compression | Immediate | Unstructured / HTML results |
| External Cache + Reference | Cache + fetch tool | On-demand | Very large results (>100 KB) |
| Progressive Disclosure | Two-tier (summary + detail) | On-demand | Agent-driven retrieval |
| Rolling Compaction | Periodic merge + summarize | Merged | Long multi-tool conversations |
| Prompt Cache Reference | System cache block | In-context | Repeated access to same data |

**Recommended starting point:** Option 1 (Field Allowlist) — define field allowlists for each tool you control. A 20-minute change that immediately cuts tool result tokens by 70–95% with no quality loss and no extra API calls.
