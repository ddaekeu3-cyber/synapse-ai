---
layout: solution
title: "Agent Gets Stuck When Tool Returns Paginated Results"
category: loop-stuck
description: "Agent calls a paginated API tool, receives a partial first page and a cursor, but doesn't follow the cursor — either looping forever on the first page or treating partial results as complete and producing wrong answers."
tags: [loop-stuck, pagination, tool-calls, api, cursor]
---

## Symptom

The agent calls a list-items tool, gets 20 of 847 items, and then either:
- Calls the same tool again with the same parameters (infinite loop, same first page)
- Stops and answers "there are 20 items" when there are actually 847
- Asks the user what to do next, even though the response clearly has a `next_cursor`

```
Tool response: {
  "items": [...20 items...],
  "next_cursor": "eyJwYWdlIjogMn0=",
  "total_count": 847
}

Agent: "I found 20 items. Here is a summary..."  ← Wrong — missed 827 items
```

## Root Cause

The tool schema doesn't clearly communicate that a `next_cursor` must be followed, and the agent's system prompt has no pagination strategy:

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

tools = [{
    "name": "list_orders",
    "description": "List orders for a customer",  # No mention of pagination
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            # cursor parameter exists but is optional and undocumented
            "cursor": {"type": "string"},
        },
        "required": ["customer_id"]
    }
}]

# Agent never follows the cursor because neither the tool description
# nor the system prompt explains what next_cursor means
```

---

## Fix

### Option 1 — Explicit pagination instructions in tool description

Update the tool description to make the pagination contract unmistakable. The model follows `next_cursor` reliably when the description is explicit.

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

# Simulated paginated API
_ALL_ORDERS = [{"id": f"ord-{i}", "amount": i * 10} for i in range(1, 52)]

def list_orders_impl(customer_id: str, cursor: str | None = None, limit: int = 10) -> dict:
    start = int(cursor) if cursor else 0
    page = _ALL_ORDERS[start:start + limit]
    next_cursor = str(start + limit) if start + limit < len(_ALL_ORDERS) else None
    return {
        "items": page,
        "next_cursor": next_cursor,
        "total_count": len(_ALL_ORDERS),
        "page_info": f"items {start+1}–{start+len(page)} of {len(_ALL_ORDERS)}",
    }


TOOLS = [{
    "name": "list_orders",
    "description": (
        "List orders for a customer. Returns up to 10 orders per call. "
        "PAGINATION: If the response includes 'next_cursor', you MUST call this tool again "
        "with that cursor value to retrieve the next page. Keep calling until 'next_cursor' "
        "is null — only then do you have the complete list. "
        "Never summarise results while next_cursor is non-null."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "Customer ID"},
            "cursor": {
                "type": "string",
                "description": "Pagination cursor from previous response. Omit for first page."
            },
        },
        "required": ["customer_id"]
    }
}]

SYSTEM = """You are an order management assistant.
When listing items, always follow pagination cursors until next_cursor is null.
Only summarise the full dataset after you have retrieved all pages."""


def run_agent(customer_id: str) -> str:
    messages = [{"role": "user", "content": f"List all orders for customer {customer_id}"}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "list_orders":
                    result = list_orders_impl(
                        customer_id=block.input["customer_id"],
                        cursor=block.input.get("cursor"),
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

            messages.append({"role": "user", "content": tool_results})


print(run_agent("cust-123"))

# Expected Token Savings: correct pagination means one query per page instead of
#   looping forever or re-asking the user; correct answer prevents re-runs
# Environment: any agent using list/search tools that return cursor-based pagination
```

---

### Option 2 — Programmatic pagination wrapper (agent never sees cursors)

Wrap the paginated API in a tool that fetches all pages automatically before returning. The agent gets complete results in one tool call.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

_ALL_ORDERS = [{"id": f"ord-{i}", "amount": i * 10} for i in range(1, 52)]


def _fetch_page(customer_id: str, cursor: str | None, limit: int = 10) -> dict:
    start = int(cursor) if cursor else 0
    page = _ALL_ORDERS[start:start + limit]
    return {
        "items": page,
        "next_cursor": str(start + limit) if start + limit < len(_ALL_ORDERS) else None,
    }


def list_all_orders_impl(customer_id: str, max_items: int = 500) -> dict:
    """Fetch all pages and return complete list. Agent calls this once."""
    all_items = []
    cursor = None

    while len(all_items) < max_items:
        page = _fetch_page(customer_id, cursor)
        all_items.extend(page["items"])
        cursor = page.get("next_cursor")
        if cursor is None:
            break

    return {
        "items": all_items[:max_items],
        "total_fetched": len(all_items),
        "truncated": len(all_items) >= max_items,
    }


# Tool exposes a non-paginated interface — agent just calls it once
TOOLS = [{
    "name": "list_all_orders",
    "description": "Fetch ALL orders for a customer. Returns complete list (up to 500 items).",
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "max_items": {
                "type": "integer",
                "description": "Maximum items to return. Default 500.",
                "default": 500,
            },
        },
        "required": ["customer_id"]
    }
}]


def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = list_all_orders_impl(**block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
        messages.append({"role": "user", "content": tool_results})


print(run_agent("How many orders does customer cust-456 have?"))

# Expected Token Savings: 1 tool call instead of N; no pagination logic in the agent loop
# Environment: agents where result sets are bounded and can be fetched eagerly
```

---

### Option 3 — Streaming pagination with summarisation per page

For very large result sets, stream one page at a time and have the model summarise incrementally rather than loading everything into context.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

_ALL_ORDERS = [{"id": f"ord-{i}", "amount": (i % 10) * 100, "status": "completed"} for i in range(1, 201)]


def fetch_page(cursor: str | None, page_size: int = 20) -> dict:
    start = int(cursor) if cursor else 0
    items = _ALL_ORDERS[start:start + page_size]
    return {
        "items": items,
        "next_cursor": str(start + page_size) if start + page_size < len(_ALL_ORDERS) else None,
        "page_number": (start // page_size) + 1,
        "total_pages": -(-len(_ALL_ORDERS) // page_size),  # ceil division
    }


def summarise_page(page: dict, running_summary: str) -> str:
    """Use Haiku to merge page stats into running summary."""
    items = page["items"]
    page_stats = {
        "count": len(items),
        "total_amount": sum(i["amount"] for i in items),
        "by_status": {s: sum(1 for i in items if i["status"] == s)
                      for s in set(i["status"] for i in items)},
    }

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Update running summary with new page data.
Running summary: {running_summary or 'None yet'}
New page ({page['page_number']}/{page['total_pages']}): {json.dumps(page_stats)}
Updated summary (one paragraph, cumulative totals):"""
        }]
    )
    return response.content[0].text.strip()


def paginate_and_summarise(customer_id: str) -> str:
    cursor = None
    summary = ""

    while True:
        page = fetch_page(cursor)
        summary = summarise_page(page, summary)
        print(f"Page {page['page_number']}/{page['total_pages']} summarised")

        cursor = page.get("next_cursor")
        if cursor is None:
            break

    return summary


result = paginate_and_summarise("cust-789")
print(f"\nFinal summary:\n{result}")

# Expected Token Savings: per-page summaries are O(1) context instead of O(N items)
# Environment: agents processing large datasets where full results exceed context window
```

---

### Option 4 — Max-page guard to prevent infinite loops

Add a hard page limit so the agent never loops more than N times regardless of what the API returns. Log a warning when the limit is hit.

```python
import anthropic
import json
import logging

log = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key="sk-live-...")

_ALL_ITEMS = list(range(1, 1001))  # 1000 items

MAX_PAGES = 20  # Safety limit


def fetch_page_impl(resource: str, cursor: str | None, page_size: int = 10) -> dict:
    start = int(cursor) if cursor else 0
    items = _ALL_ITEMS[start:start + page_size]
    return {
        "items": items,
        "next_cursor": str(start + page_size) if start + page_size < len(_ALL_ITEMS) else None,
        "total_count": len(_ALL_ITEMS),
    }


TOOLS = [{
    "name": "list_items",
    "description": (
        "List items with pagination. If next_cursor is present, call again with that cursor. "
        "If next_cursor is null, all items have been retrieved."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "resource": {"type": "string"},
            "cursor": {"type": "string"},
            "page_size": {"type": "integer", "default": 10},
        },
        "required": ["resource"]
    }
}]

SYSTEM = """Always follow pagination: call list_items with next_cursor until it is null.
If you reach 20 pages without completing, stop and report what you found so far."""


def run_with_page_guard(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    page_counts: dict[str, int] = {}  # Track pages per resource

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            resource = block.input.get("resource", "default")
            page_counts[resource] = page_counts.get(resource, 0) + 1

            if page_counts[resource] > MAX_PAGES:
                log.warning("Page limit hit for resource=%s after %d pages", resource, MAX_PAGES)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({
                        "error": "PAGE_LIMIT_REACHED",
                        "message": f"Stopped after {MAX_PAGES} pages. Results are partial.",
                        "next_cursor": None,
                        "items": [],
                    }),
                    "is_error": True,
                })
                continue

            result = fetch_page_impl(
                resource=resource,
                cursor=block.input.get("cursor"),
                page_size=block.input.get("page_size", 10),
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
            })

        messages.append({"role": "user", "content": tool_results})


print(run_with_page_guard("List all items in the inventory resource"))

# Expected Token Savings: prevents infinite loops from consuming unbounded tokens
# Environment: production agents where page count must be capped regardless of API behaviour
```

---

### Option 5 — Async parallel pagination with page merging

When the API supports offset-based pagination (not cursor-based), fetch all pages concurrently.

```python
import asyncio
import anthropic
import json

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

_ALL_ITEMS = [{"id": i, "name": f"Item {i}"} for i in range(1, 101)]
PAGE_SIZE = 10


async def fetch_page_async(offset: int) -> list[dict]:
    """Simulate async page fetch."""
    await asyncio.sleep(0.01)  # Network latency
    return _ALL_ITEMS[offset:offset + PAGE_SIZE]


async def fetch_total_count() -> int:
    await asyncio.sleep(0.01)
    return len(_ALL_ITEMS)


async def fetch_all_pages_parallel() -> list[dict]:
    """Fetch first page + count, then fetch all remaining pages in parallel."""
    total = await fetch_total_count()
    num_pages = -(-total // PAGE_SIZE)  # ceil

    # Fetch all pages concurrently
    pages = await asyncio.gather(*[
        fetch_page_async(i * PAGE_SIZE) for i in range(num_pages)
    ])

    all_items = []
    for page in pages:
        all_items.extend(page)

    return all_items


TOOLS = [{
    "name": "list_all_items",
    "description": "Fetch all items using parallel pagination. Returns complete list.",
    "input_schema": {"type": "object", "properties": {}, "required": []}
}]


async def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        tool_results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "list_all_items":
                items = await fetch_all_pages_parallel()
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"items": items, "total": len(items)}),
                })

        messages.append({"role": "user", "content": tool_results})


result = asyncio.run(run_agent("How many items are in the catalog?"))
print(result)

# Expected Token Savings: parallel fetch completes in 1 round-trip vs N sequential;
#   faster result → fewer timeout retries
# Environment: offset-based APIs where total count is known upfront
```

---

### Option 6 — Pagination state machine injected as context

Inject the pagination state into the system prompt dynamically, so the model always knows which page it's on and exactly what to do next.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

_ALL_ORDERS = [{"id": f"ord-{i}", "amount": i * 5} for i in range(1, 31)]


def fetch_page(cursor: str | None, page_size: int = 5) -> dict:
    start = int(cursor) if cursor else 0
    items = _ALL_ORDERS[start:start + page_size]
    next_cursor = str(start + page_size) if start + page_size < len(_ALL_ORDERS) else None
    return {
        "items": items,
        "next_cursor": next_cursor,
        "fetched_so_far": start + len(items),
        "total": len(_ALL_ORDERS),
    }


TOOLS = [{
    "name": "get_next_page",
    "description": "Get the next page of orders. Pass the cursor from the previous response.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cursor": {"type": "string", "description": "Cursor from previous page, or omit for first page."}
        }
    }
}]


def build_system(page_state: dict) -> str:
    if not page_state:
        return "Retrieve all orders using get_next_page. Follow the cursor until next_cursor is null."

    return f"""Pagination state:
- Pages fetched: {page_state['pages_fetched']}
- Items collected: {page_state['fetched_so_far']} / {page_state['total']}
- Next cursor: {page_state['next_cursor'] or 'NONE — all pages retrieved'}

{'Call get_next_page with cursor=' + repr(page_state['next_cursor']) + ' to continue.' if page_state['next_cursor'] else 'All pages are retrieved. Summarise the results now.'}"""


def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    all_items = []
    page_state: dict = {}

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=build_system(page_state),
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        tool_results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "get_next_page":
                page = fetch_page(block.input.get("cursor"))
                all_items.extend(page["items"])
                page_state = {
                    "pages_fetched": page_state.get("pages_fetched", 0) + 1,
                    "next_cursor": page["next_cursor"],
                    "fetched_so_far": page["fetched_so_far"],
                    "total": page["total"],
                }
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(page),
                })

        messages.append({"role": "user", "content": tool_results})


print(run_agent("Fetch and summarise all orders"))

# Expected Token Savings: dynamic system prompt keeps agent on track without long reasoning traces
# Environment: stateful multi-turn agents where pagination progress must be reliably tracked
```

---

## Comparison

| Option | Agent Sees Cursors | Loop-Safe | Large Datasets | Async | Context Overhead |
|--------|--------------------|-----------|----------------|-------|-----------------|
| 1 | Yes (with instructions) | Partial | Partial | No | Low |
| 2 | No (wrapper) | Yes | Bounded | No | Low |
| 3 | No (summarise/page) | Yes | Yes | No | Low |
| 4 | Yes + guard | Yes (hard cap) | Partial | No | Medium |
| 5 | No (parallel) | Yes | Bounded | Yes | Low |
| 6 | Yes + state | Yes | Partial | No | Medium |

**Recommended starting point:** Option 2 (programmatic wrapper) for datasets under 10,000 items — the agent never needs to know about pagination. Use Option 3 (per-page summarisation) for very large result sets that exceed the context window. Add Option 4's page guard to any approach as a safety net.
