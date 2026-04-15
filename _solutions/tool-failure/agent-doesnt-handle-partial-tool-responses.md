---
layout: solution
title: "Agent Doesn't Handle Partial Tool Responses"
category: tool-failure
description: "Tool returns incomplete data — truncated JSON, missing required fields, or a partial list — and the agent treats it as success, producing answers based on wrong or missing information without detecting the problem."
tags: [tool-failure, validation, partial-response, error-handling, retry, schema]
---

## Symptom

The agent calls `search_documents` and gets back 3 results instead of the expected 10. It summarises those 3 results as if they were complete, misleading the user. Or the tool returns `{"status": "ok"}` but omits the `data` field — the agent reads `result["data"]` and raises a `KeyError`, crashing mid-conversation. In both cases, the agent never checked whether the response was complete.

Undetected partial response rate in production: **15–30%** (network timeouts, upstream truncation, pagination ignored)

## Root Cause

Tool results are passed directly to the agent as strings without validation. The agent has no mechanism to distinguish a complete response from a partial one — it just reads whatever the tool returns. Missing fields cause runtime errors; truncated lists cause silent data loss.

## Fix

---

### Option 1 — Schema Validation Wrapper on Every Tool Result

Wrap each tool in a validator that checks required fields before returning to the agent. On schema failure, return a structured error the agent can act on.

```python
import json
import anthropic
from typing import Any, Callable
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ToolResult:
    success: bool
    data: Any
    error: str = ""
    partial: bool = False
    missing_fields: list[str] = None

    def to_tool_response(self) -> str:
        if self.success:
            return json.dumps({"status": "ok", "data": self.data, "partial": self.partial})
        return json.dumps({
            "status": "error",
            "error": self.error,
            "missing_fields": self.missing_fields or [],
            "agent_instruction": "Do not use this result. Inform the user the data is unavailable.",
        })

# Schema: required fields per tool
TOOL_SCHEMAS: dict[str, dict] = {
    "search_documents": {
        "required": ["results", "total_count"],
        "results_min_length": 1,
    },
    "get_user_profile": {
        "required": ["user_id", "name", "email"],
    },
    "fetch_analytics": {
        "required": ["period", "metrics", "data_points"],
        "data_points_min_length": 1,
    },
}

def validate_tool_result(tool_name: str, raw_result: dict) -> ToolResult:
    schema = TOOL_SCHEMAS.get(tool_name, {})
    required = schema.get("required", [])
    missing = [f for f in required if f not in raw_result]

    if missing:
        return ToolResult(
            success=False,
            data=None,
            error=f"Tool '{tool_name}' returned partial response — missing fields: {missing}",
            partial=True,
            missing_fields=missing,
        )

    # Check minimum list lengths
    for key, min_len_key in [(k.replace("_min_length", ""), f"{k}_min_length") for k in schema if k.endswith("_min_length")]:
        value = raw_result.get(key)
        min_len = schema[min_len_key]
        if isinstance(value, list) and len(value) < min_len:
            return ToolResult(
                success=True,
                data=raw_result,
                partial=True,
                error=f"Field '{key}' has {len(value)} items, expected >= {min_len}",
            )

    return ToolResult(success=True, data=raw_result)

def make_validated_tool(tool_name: str, raw_fn: Callable) -> Callable:
    def wrapper(*args, **kwargs) -> str:
        try:
            raw = raw_fn(*args, **kwargs)
            result = validate_tool_result(tool_name, raw)
            return result.to_tool_response()
        except Exception as e:
            return json.dumps({
                "status": "error",
                "error": f"Tool execution failed: {str(e)}",
                "agent_instruction": "Report this error to the user.",
            })
    return wrapper

# Simulated tool implementations
def _search_documents_impl(query: str) -> dict:
    # Simulate a partial response (missing total_count)
    return {"results": [{"id": 1, "title": "Doc A"}]}

def _get_user_profile_impl(user_id: str) -> dict:
    return {"user_id": user_id, "name": "Alice Chen", "email": "alice@example.com"}

search_documents = make_validated_tool("search_documents", _search_documents_impl)
get_user_profile = make_validated_tool("get_user_profile", _get_user_profile_impl)

TOOLS = [
    {
        "name": "search_documents",
        "description": "Search documents by query. Returns results list and total_count.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_user_profile",
        "description": "Get user profile by user_id.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
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
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "search_documents":
                    result = search_documents(query=block.input["query"])
                elif block.name == "get_user_profile":
                    result = get_user_profile(user_id=block.input["user_id"])
                else:
                    result = json.dumps({"status": "error", "error": f"Unknown tool: {block.name}"})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

result = run_agent("Search for documents about machine learning.")
print(result)
```

**Expected Token Savings:** 10–20% — agent avoids follow-up turns caused by acting on bad data
**Environment:** `pip install anthropic`

---

### Option 2 — Completeness Check Field in Every Tool Response

Embed a `complete: bool` and `expected_count` / `returned_count` in every tool response. The agent's system prompt instructs it to check these fields before using the data.

```python
import json
import random
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a data analysis assistant.

CRITICAL: Every tool response contains a `complete` field.
- If `complete` is false, do NOT use the data. Tell the user: "I received partial data from [tool_name] and cannot provide a reliable answer. Please retry."
- If `complete` is true but `returned_count` < `expected_count`, note the discrepancy in your response.
- Never present partial data as complete."""

def search_records(query: str, limit: int = 10) -> str:
    """Simulates a tool that sometimes returns partial results."""
    all_records = [{"id": i, "title": f"Record {i}", "query": query} for i in range(1, limit + 1)]

    # Simulate network truncation: 40% chance of partial response
    simulated_truncation = random.random() < 0.4
    if simulated_truncation:
        actual = all_records[:random.randint(1, limit - 1)]
        return json.dumps({
            "complete": False,
            "expected_count": limit,
            "returned_count": len(actual),
            "records": actual,
            "error": "Response truncated — upstream timeout at record fetch layer",
            "agent_instruction": "Do not summarise these records. The data is incomplete.",
        })

    return json.dumps({
        "complete": True,
        "expected_count": limit,
        "returned_count": len(all_records),
        "records": all_records,
    })

def aggregate_metrics(metric_type: str) -> str:
    """Returns aggregated metrics, sometimes with missing buckets."""
    buckets = ["2024-Q1", "2024-Q2", "2024-Q3", "2024-Q4"]
    simulated_gap = random.random() < 0.3

    if simulated_gap:
        available = buckets[:2]
        return json.dumps({
            "complete": False,
            "expected_count": len(buckets),
            "returned_count": len(available),
            "metric_type": metric_type,
            "data": {b: random.randint(100, 999) for b in available},
            "missing_periods": buckets[2:],
            "agent_instruction": "Inform user that Q3 and Q4 data is unavailable.",
        })

    return json.dumps({
        "complete": True,
        "expected_count": len(buckets),
        "returned_count": len(buckets),
        "metric_type": metric_type,
        "data": {b: random.randint(100, 999) for b in buckets},
    })

TOOLS = [
    {
        "name": "search_records",
        "description": "Search records by query. Always check `complete` field before using results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "aggregate_metrics",
        "description": "Get aggregated metrics. Always check `complete` field before using results.",
        "input_schema": {
            "type": "object",
            "properties": {"metric_type": {"type": "string"}},
            "required": ["metric_type"],
        },
    },
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "search_records":
                    result = search_records(**block.input)
                elif block.name == "aggregate_metrics":
                    result = aggregate_metrics(**block.input)
                else:
                    result = json.dumps({"complete": False, "error": "Unknown tool"})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

# Run several times to see both complete and partial behavior
for trial in range(3):
    print(f"\n--- Trial {trial + 1} ---")
    print(run_agent("Search for Q4 sales records and summarise the results."))
```

**Expected Token Savings:** None — same tokens; prevents hallucination from incomplete data
**Environment:** `pip install anthropic`

---

### Option 3 — Automatic Retry with Pagination Detection

Detect pagination signals in tool responses and automatically continue fetching until the result is complete, then return the merged complete result to the agent.

```python
import json
import asyncio
import anthropic

async_client = anthropic.AsyncAnthropic()

class PaginatedFetcher:
    MAX_PAGES = 10
    PAGE_SIZE = 5

    async def fetch_page(self, tool_name: str, params: dict, cursor: str = None) -> dict:
        """Simulates a paginated API — replace with real HTTP calls."""
        await asyncio.sleep(0.02)  # Simulated latency
        total = 13  # Simulated total records
        offset = int(cursor or 0)
        records = [
            {"id": offset + i + 1, "title": f"Item {offset + i + 1}"}
            for i in range(min(self.PAGE_SIZE, total - offset))
        ]
        next_cursor = str(offset + len(records)) if offset + len(records) < total else None
        return {
            "records": records,
            "next_cursor": next_cursor,
            "total": total,
            "returned": len(records),
        }

    async def fetch_all(self, tool_name: str, params: dict) -> str:
        """Fetches all pages and merges results. Returns complete tool result."""
        all_records = []
        cursor = None
        pages_fetched = 0

        while pages_fetched < self.MAX_PAGES:
            page = await self.fetch_page(tool_name, params, cursor)
            all_records.extend(page.get("records", []))
            pages_fetched += 1

            next_cursor = page.get("next_cursor")
            if not next_cursor:
                break
            cursor = next_cursor
            print(f"[Fetcher] Page {pages_fetched}: fetched {len(page['records'])} records, cursor={next_cursor}")

        total = page.get("total", len(all_records))
        complete = len(all_records) >= total

        return json.dumps({
            "complete": complete,
            "total": total,
            "returned": len(all_records),
            "pages_fetched": pages_fetched,
            "records": all_records,
        })

fetcher = PaginatedFetcher()

TOOLS = [
    {
        "name": "list_items",
        "description": "List all items matching a filter. Pagination is handled automatically — always returns complete results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string"},
                "category": {"type": "string"},
            },
            "required": ["filter"],
        },
    },
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
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                print(f"[Agent] Calling {block.name}({block.input})")
                # Pagination handled transparently — agent receives merged complete result
                result = await fetcher.fetch_all(block.name, block.input)
                parsed = json.loads(result)
                print(f"[Fetcher] Complete: {parsed['complete']}, Total: {parsed['total']}, Returned: {parsed['returned']}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

result = asyncio.run(run_agent("List all items in category 'electronics' and count them."))
print(f"\n{result}")
```

**Expected Token Savings:** None — more tokens used (full data), but answers are correct
**Environment:** `pip install anthropic`

---

### Option 4 — Structural Diff Against Expected Response Shape

Before passing tool results to the agent, diff the actual response structure against the expected schema and annotate missing or null fields so the agent knows what is and isn't present.

```python
import json
import anthropic
from typing import Any

client = anthropic.Anthropic()

# Expected response shapes per tool
EXPECTED_SHAPES: dict[str, Any] = {
    "get_order": {
        "order_id": str,
        "status": str,
        "items": list,
        "total_amount": float,
        "shipping_address": {
            "street": str,
            "city": str,
            "country": str,
        },
        "estimated_delivery": str,
    },
    "get_inventory": {
        "product_id": str,
        "quantity": int,
        "warehouse_location": str,
        "last_updated": str,
    },
}

def diff_shape(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Recursively find missing or null fields."""
    issues = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            issues.append(f"{path}: expected object, got {type(actual).__name__}")
            return issues
        for key, expected_type in expected.items():
            field_path = f"{path}.{key}" if path else key
            if key not in actual:
                issues.append(f"MISSING: {field_path}")
            elif actual[key] is None:
                issues.append(f"NULL: {field_path}")
            elif isinstance(expected_type, dict):
                issues.extend(diff_shape(expected_type, actual[key], field_path))
            elif not isinstance(actual[key], expected_type):
                issues.append(f"WRONG_TYPE: {field_path} (expected {expected_type.__name__}, got {type(actual[key]).__name__})")
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            issues.append(f"{path}: expected list, got {type(actual).__name__}")
        elif len(actual) == 0:
            issues.append(f"EMPTY_LIST: {path}")
    return issues

def annotated_tool_result(tool_name: str, raw_result: dict) -> str:
    """Annotate the tool result with completeness information."""
    expected = EXPECTED_SHAPES.get(tool_name)
    if not expected:
        return json.dumps(raw_result)

    issues = diff_shape(expected, raw_result)
    if not issues:
        return json.dumps({"complete": True, "data": raw_result})

    return json.dumps({
        "complete": False,
        "data": raw_result,
        "missing_or_null_fields": issues,
        "agent_instruction": (
            f"This response is incomplete. Missing: {', '.join(issues[:5])}. "
            "Do not invent values for missing fields. Tell the user which information is unavailable."
        ),
    })

# Simulated tools returning incomplete data
def get_order(order_id: str) -> dict:
    # Simulates a real-world partial API response
    return {
        "order_id": order_id,
        "status": "shipped",
        "items": [{"sku": "A100", "qty": 2}],
        "total_amount": None,       # NULL field
        # "shipping_address" missing entirely
        # "estimated_delivery" missing
    }

def get_inventory(product_id: str) -> dict:
    return {
        "product_id": product_id,
        "quantity": 45,
        "warehouse_location": "WH-3B",
        "last_updated": "2025-04-14T09:00:00Z",
    }

TOOLS = [
    {
        "name": "get_order",
        "description": "Get order details by order_id.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "get_inventory",
        "description": "Get inventory level for a product.",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
    },
]

SYSTEM = """You are an order management assistant.
When tool results contain `complete: false` and `missing_or_null_fields`, acknowledge what information is unavailable.
Never fabricate values for missing fields. Always tell the user exactly what is and isn't available."""

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "get_order":
                    raw = get_order(block.input["order_id"])
                elif block.name == "get_inventory":
                    raw = get_inventory(block.input["product_id"])
                else:
                    raw = {"error": "unknown tool"}

                result = annotated_tool_result(block.name, raw)
                print(f"[Tool] {block.name} → complete={json.loads(result).get('complete')}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

print(run_agent("What are the details for order ORD-12345? Include shipping address and delivery estimate."))
```

**Expected Token Savings:** 5–15% — one correct response vs multiple confused follow-ups
**Environment:** `pip install anthropic`

---

### Option 5 — Tool-Level Retry with Idempotency on Partial Response

When a tool returns a partial result, automatically retry up to N times with an idempotency key. Return the best (most complete) result across all attempts.

```python
import json
import asyncio
import random
import hashlib
import time
import anthropic
from dataclasses import dataclass
from typing import Any

async_client = anthropic.AsyncAnthropic()

@dataclass
class AttemptResult:
    data: dict
    completeness: float   # 0.0–1.0 — fraction of expected fields present
    attempt: int
    latency_ms: float

def measure_completeness(data: dict, required_fields: list[str]) -> float:
    if not required_fields:
        return 1.0
    present = sum(1 for f in required_fields if data.get(f) is not None)
    return present / len(required_fields)

REQUIRED_FIELDS: dict[str, list[str]] = {
    "fetch_report": ["report_id", "title", "sections", "generated_at", "author"],
    "get_metrics": ["period", "values", "unit", "source"],
}

async def fetch_report(report_id: str, idempotency_key: str) -> dict:
    """Simulates unreliable report endpoint — sometimes returns partial data."""
    await asyncio.sleep(random.uniform(0.05, 0.15))
    if random.random() < 0.5:
        # Partial response
        return {
            "report_id": report_id,
            "title": f"Report {report_id}",
            # "sections" missing
            "generated_at": "2025-04-14T10:00:00Z",
            # "author" missing
        }
    return {
        "report_id": report_id,
        "title": f"Report {report_id}",
        "sections": ["Executive Summary", "Analysis", "Recommendations"],
        "generated_at": "2025-04-14T10:00:00Z",
        "author": "Data Team",
    }

async def get_metrics(period: str, idempotency_key: str) -> dict:
    await asyncio.sleep(random.uniform(0.05, 0.10))
    return {
        "period": period,
        "values": [42.1, 38.5, 55.0],
        "unit": "thousands",
        "source": "analytics_db",
    }

async def resilient_tool_call(
    tool_name: str,
    params: dict,
    max_attempts: int = 3,
    completeness_threshold: float = 0.8,
) -> str:
    """Retry tool until completeness threshold met. Return best result."""
    base_key = f"{tool_name}:{json.dumps(params, sort_keys=True)}"
    idempotency_key = hashlib.sha256(base_key.encode()).hexdigest()[:16]

    required = REQUIRED_FIELDS.get(tool_name, [])
    attempts: list[AttemptResult] = []

    for attempt in range(1, max_attempts + 1):
        start = time.monotonic()
        try:
            if tool_name == "fetch_report":
                raw = await fetch_report(params.get("report_id"), idempotency_key)
            elif tool_name == "get_metrics":
                raw = await get_metrics(params.get("period"), idempotency_key)
            else:
                raw = {"error": f"Unknown tool: {tool_name}"}

            latency = (time.monotonic() - start) * 1000
            completeness = measure_completeness(raw, required)
            result = AttemptResult(raw, completeness, attempt, latency)
            attempts.append(result)

            print(f"[Retry] {tool_name} attempt {attempt}/{max_attempts}: completeness={completeness:.0%}, latency={latency:.0f}ms")

            if completeness >= completeness_threshold:
                break

        except Exception as e:
            print(f"[Retry] {tool_name} attempt {attempt} failed: {e}")
            await asyncio.sleep(0.1 * attempt)

    if not attempts:
        return json.dumps({"complete": False, "error": "All attempts failed"})

    # Return the most complete attempt
    best = max(attempts, key=lambda a: a.completeness)
    complete = best.completeness >= completeness_threshold

    missing = [f for f in required if best.data.get(f) is None]
    return json.dumps({
        "complete": complete,
        "completeness": round(best.completeness, 2),
        "data": best.data,
        "attempts": len(attempts),
        "missing_fields": missing if not complete else [],
        "agent_instruction": (
            "" if complete
            else f"Best available data after {len(attempts)} attempts. Missing: {missing}. Inform the user."
        ),
    })

TOOLS = [
    {
        "name": "fetch_report",
        "description": "Fetch a report by ID. Retries automatically for completeness.",
        "input_schema": {
            "type": "object",
            "properties": {"report_id": {"type": "string"}},
            "required": ["report_id"],
        },
    },
    {
        "name": "get_metrics",
        "description": "Get metrics for a time period.",
        "input_schema": {
            "type": "object",
            "properties": {"period": {"type": "string"}},
            "required": ["period"],
        },
    },
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
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                result = await resilient_tool_call(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

result = asyncio.run(run_agent("Fetch report RPT-2025-04 and summarise its sections."))
print(f"\n{result}")
```

**Expected Token Savings:** None — correctness focus; prevents costly correction loops downstream
**Environment:** `pip install anthropic`

---

### Option 6 — Partial Response Aggregator with Merge Strategy

When a tool consistently returns partial results (different fields each time), run multiple calls in parallel and merge the best values from each response into one complete result.

```python
import json
import asyncio
import random
import anthropic
from typing import Any

async_client = anthropic.AsyncAnthropic()

def merge_partial_responses(responses: list[dict], required_fields: list[str]) -> dict[str, Any]:
    """Merge multiple partial responses, taking the first non-None value per field."""
    merged: dict[str, Any] = {}
    for field in required_fields:
        for resp in responses:
            value = resp.get(field)
            if value is not None:
                merged[field] = value
                break
    # Include any extra fields present in any response
    for resp in responses:
        for key, val in resp.items():
            if key not in merged and val is not None:
                merged[key] = val
    return merged

async def fetch_device_status_shard(device_id: str, shard: int) -> dict:
    """Simulates a sharded API — each shard returns different fields."""
    await asyncio.sleep(random.uniform(0.03, 0.08))
    shards = {
        0: {"device_id": device_id, "name": f"Device-{device_id}", "status": "online"},
        1: {"device_id": device_id, "firmware_version": "2.4.1", "last_ping": "2025-04-14T11:00:00Z"},
        2: {"device_id": device_id, "location": "Building B, Floor 3", "owner": "ops-team"},
    }
    return shards.get(shard, {})

REQUIRED_FIELDS = ["device_id", "name", "status", "firmware_version", "last_ping", "location", "owner"]

async def aggregated_device_status(device_id: str) -> str:
    """Fetch all shards in parallel and merge into one complete response."""
    tasks = [fetch_device_status_shard(device_id, shard) for shard in range(3)]
    shards = await asyncio.gather(*tasks, return_exceptions=True)

    valid_shards = [s for s in shards if isinstance(s, dict)]
    print(f"[Aggregator] Collected {len(valid_shards)}/3 shards for {device_id}")

    merged = merge_partial_responses(valid_shards, REQUIRED_FIELDS)
    missing = [f for f in REQUIRED_FIELDS if f not in merged]
    complete = len(missing) == 0

    return json.dumps({
        "complete": complete,
        "data": merged,
        "missing_fields": missing,
        "shards_used": len(valid_shards),
        "agent_instruction": "" if complete else f"Still missing: {missing}. Report unavailable fields to user.",
    })

TOOLS = [
    {
        "name": "get_device_status",
        "description": "Get complete device status. Aggregates from multiple shards automatically.",
        "input_schema": {
            "type": "object",
            "properties": {"device_id": {"type": "string"}},
            "required": ["device_id"],
        },
    },
]

SYSTEM = """You are a device management assistant.
When tool results include `missing_fields`, explicitly tell the user which information could not be retrieved.
Never invent device data — report only what is confirmed in the tool response."""

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                result = await aggregated_device_status(block.input["device_id"])
                parsed = json.loads(result)
                print(f"[Agent] complete={parsed['complete']}, fields={list(parsed['data'].keys())}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

result = asyncio.run(run_agent("Give me a full status report for device DEV-4892."))
print(f"\n{result}")
```

**Expected Token Savings:** None — parallel fetches ensure completeness in a single agent turn
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Detection Method | Recovery Strategy | Best For |
|--------|-----------------|------------------|----------|
| Schema Validation Wrapper | Required field check | Return error to agent | Known tool response shapes |
| Completeness Flag | Embedded `complete` field | Agent reads flag and reports | All tools you control |
| Pagination Fetcher | `next_cursor` detection | Auto-fetch all pages | Paginated APIs |
| Structural Diff | Shape diff vs expected | Annotate missing fields | Complex nested responses |
| Retry with Best-Pick | Completeness score | Retry up to N, keep best | Flaky/unreliable endpoints |
| Parallel Aggregator | Merge multiple shards | Fan-out + merge | Sharded APIs, read replicas |

**Recommended starting point:** Option 2 (Completeness Flag) — add `complete: bool` to every tool you control, then add the system prompt instruction. Takes 15 minutes, catches the most common partial-response failure modes with zero extra API calls.
