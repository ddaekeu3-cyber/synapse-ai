---
layout: solution
title: "Agent Doesn't Implement Pagination for Large List Responses"
category: general
description: "Agent API endpoints return all results in a single response regardless of dataset size. Large lists exhaust memory, hit response size limits, and cause timeouts — while clients receive more data than they can display anyway."
tags: [pagination, api-design, cursor, fastapi, asyncpg, performance, scalability]
---

# Agent Doesn't Implement Pagination for Large List Responses

## Problem

`GET /api/agent/conversations` returns all 50,000 conversations for a user in one JSON blob. The response is 8MB, the database query takes 12 seconds, and the client crashes rendering the list. Adding pagination after the fact requires changing the API contract, breaking existing integrations. Pagination must be designed in from the start.

## Solutions

### Option 1: Cursor-Based Pagination (Stable Under Inserts)

```python
# api/pagination.py
"""
Cursor-based pagination using an opaque cursor (base64-encoded ID + timestamp).
Unlike offset pagination, cursors remain stable even when new records are inserted
between pages — critical for agent conversation history that grows during browsing.
"""
import base64
import json
from dataclasses import dataclass
from typing import Generic, TypeVar, Optional
import asyncpg
from fastapi import FastAPI, Query
from pydantic import BaseModel

T = TypeVar("T")


@dataclass
class Page(Generic[T]):
    items: list[T]
    next_cursor: Optional[str]
    has_more: bool
    total_count: Optional[int] = None  # Expensive to compute; omit if not needed


def encode_cursor(last_id: str, last_ts: float) -> str:
    """Create an opaque cursor from the last item's ID and timestamp."""
    payload = json.dumps({"id": last_id, "ts": last_ts})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, float]:
    """Decode cursor back to (id, timestamp)."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return payload["id"], float(payload["ts"])
    except Exception:
        raise ValueError(f"Invalid cursor: {cursor!r}")


async def paginate_conversations(
    pool: asyncpg.Pool,
    user_id: str,
    limit: int,
    cursor: Optional[str] = None,
) -> Page[dict]:
    """
    Fetch a page of conversations using cursor-based pagination.
    Consistent ordering: created_at DESC, id DESC (stable tiebreaker).
    """
    if cursor:
        last_id, last_ts = decode_cursor(cursor)
        rows = await pool.fetch(
            """
            SELECT id, user_id, title, created_at, updated_at
            FROM conversations
            WHERE user_id = $1
              AND (created_at, id) < ($2::timestamptz, $3)
            ORDER BY created_at DESC, id DESC
            LIMIT $4
            """,
            user_id, last_ts, last_id, limit + 1,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, user_id, title, created_at, updated_at
            FROM conversations
            WHERE user_id = $1
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            user_id, limit + 1,
        )

    items = [dict(r) for r in rows]
    has_more = len(items) > limit
    if has_more:
        items = items[:limit]

    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(str(last["id"]), last["created_at"].timestamp())

    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


# ── FastAPI endpoint ───────────────────────────────────────────────────────────

app = FastAPI()


class ConversationListResponse(BaseModel):
    items: list[dict]
    next_cursor: Optional[str]
    has_more: bool


@app.get("/api/conversations", response_model=ConversationListResponse)
async def list_conversations(
    user_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
):
    page = await paginate_conversations(app.state.db_pool, user_id, limit, cursor)
    return ConversationListResponse(
        items=page.items,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )
```

**Expected Token Savings:** Not applicable — API design
**Environment:** `pip install fastapi asyncpg pydantic`

---

### Option 2: Offset Pagination with Count (Simple Cases)

```python
# api/offset_pagination.py
"""
Classic offset/limit pagination. Simple to implement and understand.
Works well for stable datasets. Not suitable for fast-growing feeds
where offset drift causes duplicate or skipped items between pages.
"""
import math
from typing import Optional
import asyncpg
from fastapi import FastAPI, Query
from pydantic import BaseModel


class PaginatedResponse(BaseModel):
    items: list[dict]
    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next: bool
    has_prev: bool


async def get_paginated_results(
    pool: asyncpg.Pool,
    user_id: str,
    page: int,
    page_size: int,
) -> PaginatedResponse:
    """Offset-based pagination with total count."""
    offset = (page - 1) * page_size

    # Run count and data queries in parallel
    count_query = "SELECT COUNT(*) FROM conversations WHERE user_id = $1"
    data_query = """
        SELECT id, title, created_at, message_count
        FROM conversations
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
    """
    total, rows = await asyncio.gather(
        pool.fetchval(count_query, user_id),
        pool.fetch(data_query, user_id, page_size, offset),
    )

    total_pages = math.ceil(total / page_size) if total > 0 else 1
    return PaginatedResponse(
        items=[dict(r) for r in rows],
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_prev=page > 1,
    )


import asyncio
app = FastAPI()


@app.get("/api/conversations")
async def list_conversations(
    user_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return await get_paginated_results(app.state.db_pool, user_id, page, page_size)
```

**Expected Token Savings:** Not applicable — scalability
**Environment:** `pip install fastapi asyncpg pydantic`

---

### Option 3: Keyset Pagination for Agent Tool Results

```python
# api/keyset_pagination.py
"""
Keyset pagination for agent tool result history.
Uses the primary key as a stable page anchor — O(log n) with a btree index,
compared to O(n) for OFFSET pagination on large tables.
"""
from typing import Optional
import asyncpg
from fastapi import FastAPI, Query
from pydantic import BaseModel


class ToolResultPage(BaseModel):
    results: list[dict]
    next_id: Optional[str] = None
    prev_id: Optional[str] = None


async def get_tool_results_page(
    pool: asyncpg.Pool,
    session_id: str,
    limit: int,
    after_id: Optional[str] = None,
    before_id: Optional[str] = None,
) -> ToolResultPage:
    """
    Bi-directional keyset pagination: supports both forward (after_id)
    and backward (before_id) navigation through tool result history.
    """
    if after_id:
        # Forward: get results with id > after_id
        rows = await pool.fetch(
            """
            SELECT id, tool_name, input_summary, output_summary, created_at, success
            FROM tool_results
            WHERE session_id = $1 AND id > $2
            ORDER BY id ASC LIMIT $3
            """,
            session_id, after_id, limit + 1,
        )
        items = [dict(r) for r in rows[:limit]]
        next_id = str(rows[limit]["id"]) if len(rows) > limit else None
        prev_id = after_id  # Caller knows where they came from

    elif before_id:
        # Backward: get results with id < before_id, reverse order
        rows = await pool.fetch(
            """
            SELECT id, tool_name, input_summary, output_summary, created_at, success
            FROM tool_results
            WHERE session_id = $1 AND id < $2
            ORDER BY id DESC LIMIT $3
            """,
            session_id, before_id, limit,
        )
        items = list(reversed([dict(r) for r in rows]))
        next_id = before_id
        prev_id = str(rows[-1]["id"]) if rows else None

    else:
        # First page
        rows = await pool.fetch(
            """
            SELECT id, tool_name, input_summary, output_summary, created_at, success
            FROM tool_results
            WHERE session_id = $1
            ORDER BY id ASC LIMIT $2
            """,
            session_id, limit + 1,
        )
        items = [dict(r) for r in rows[:limit]]
        next_id = str(rows[limit]["id"]) if len(rows) > limit else None
        prev_id = None

    return ToolResultPage(results=items, next_id=next_id, prev_id=prev_id)


app = FastAPI()


@app.get("/api/sessions/{session_id}/tool-results")
async def list_tool_results(
    session_id: str,
    limit: int = Query(default=25, ge=1, le=100),
    after: Optional[str] = Query(default=None),
    before: Optional[str] = Query(default=None),
):
    return await get_tool_results_page(
        app.state.db_pool, session_id, limit, after_id=after, before_id=before
    )
```

**Expected Token Savings:** Not applicable — O(log n) query cost vs O(n) for OFFSET
**Environment:** `pip install fastapi asyncpg pydantic`

---

### Option 4: Streaming Large Agent Outputs with Chunked Transfer

```python
# api/streaming_list.py
"""
For very large result sets (exports, bulk retrieval), stream the response
as newline-delimited JSON (NDJSON) rather than paginating.
The client receives items as they're produced without waiting for all of them.
"""
import json
import asyncio
import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


app = FastAPI()


async def stream_conversations(
    pool: asyncpg.Pool,
    user_id: str,
    batch_size: int = 100,
):
    """
    Async generator: yields conversations as NDJSON lines.
    Memory usage is O(batch_size), not O(total records).
    """
    last_id = None
    while True:
        if last_id is None:
            rows = await pool.fetch(
                "SELECT id, title, created_at FROM conversations "
                "WHERE user_id = $1 ORDER BY id ASC LIMIT $2",
                user_id, batch_size,
            )
        else:
            rows = await pool.fetch(
                "SELECT id, title, created_at FROM conversations "
                "WHERE user_id = $1 AND id > $2 ORDER BY id ASC LIMIT $3",
                user_id, last_id, batch_size,
            )

        if not rows:
            break

        for row in rows:
            item = {"id": str(row["id"]), "title": row["title"],
                    "created_at": row["created_at"].isoformat()}
            yield json.dumps(item) + "\n"
            last_id = row["id"]

        # Yield control between batches
        await asyncio.sleep(0)


@app.get("/api/conversations/export")
async def export_conversations(user_id: str):
    """
    Stream all conversations as NDJSON.
    Client reads line by line:
        async for line in response.aiter_lines():
            item = json.loads(line)
    """
    return StreamingResponse(
        stream_conversations(app.state.db_pool, user_id),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f"attachment; filename=conversations.ndjson",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )
```

**Expected Token Savings:** Not applicable — memory efficiency
**Environment:** `pip install fastapi asyncpg`

---

### Option 5: Agent Memory Store with Paginated Search

```python
# memory/paginated_search.py
"""
When the agent searches its memory store, return paginated results
rather than injecting all matching memories into the context window.
Combine pagination with relevance scoring to return only the most useful memories.
"""
import math
import anthropic
from dataclasses import dataclass
from typing import Optional


@dataclass
class MemorySearchResult:
    memories: list[dict]
    total_found: int
    page: int
    total_pages: int
    tokens_estimated: int


def _estimate_tokens(memories: list[dict]) -> int:
    """Rough token estimate: 1 token ≈ 4 chars."""
    total_chars = sum(len(str(m.get("content", ""))) for m in memories)
    return total_chars // 4


class PaginatedMemoryStore:
    def __init__(self, max_context_tokens: int = 4000):
        self._memories: list[dict] = []
        self.max_context_tokens = max_context_tokens

    def add(self, key: str, content: str, tags: list[str] = None):
        self._memories.append({
            "key": key,
            "content": content,
            "tags": tags or [],
            "id": len(self._memories),
        })

    def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 10,
        tags: Optional[list[str]] = None,
    ) -> MemorySearchResult:
        """
        Search memories with pagination.
        Returns only enough memories to stay within token budget.
        """
        query_words = set(query.lower().split())

        def _score(memory: dict) -> float:
            content_words = set(memory["content"].lower().split())
            tag_words = set(t.lower() for t in memory.get("tags", []))
            return len(query_words & content_words) + len(query_words & tag_words) * 2

        # Filter by tags if specified
        candidates = self._memories
        if tags:
            tag_set = set(t.lower() for t in tags)
            candidates = [m for m in candidates if any(t.lower() in tag_set for t in m.get("tags", []))]

        # Score and sort
        scored = sorted(candidates, key=_score, reverse=True)
        scored = [m for m in scored if _score(m) > 0]

        total = len(scored)
        total_pages = math.ceil(total / page_size) if total > 0 else 1
        offset = (page - 1) * page_size
        page_items = scored[offset:offset + page_size]

        # Further limit to token budget
        budget_items = []
        tokens_used = 0
        for item in page_items:
            item_tokens = _estimate_tokens([item])
            if tokens_used + item_tokens > self.max_context_tokens:
                break
            budget_items.append(item)
            tokens_used += item_tokens

        return MemorySearchResult(
            memories=budget_items,
            total_found=total,
            page=page,
            total_pages=total_pages,
            tokens_estimated=tokens_used,
        )

    def inject_into_context(self, query: str, max_tokens: int = 2000) -> str:
        """Retrieve the most relevant memories within a token budget."""
        result = self.search(query, page=1, page_size=20)
        lines = [f"Relevant memories (page 1/{result.total_pages}, ~{result.tokens_estimated} tokens):"]
        for m in result.memories:
            lines.append(f"- [{m['key']}] {m['content']}")
        if result.total_pages > 1:
            lines.append(f"  ({result.total_found - len(result.memories)} more memories available — ask to see page 2+)")
        return "\n".join(lines)
```

**Expected Token Savings:** 50–80% by injecting only the top K most relevant memories
**Environment:** `pip install anthropic`

---

### Option 6: Pagination Client Helper

```python
# client/paginator.py
"""
Client-side helper that transparently iterates through all pages
of a paginated API. Useful for batch processing, exports, and tests.
"""
import asyncio
from typing import AsyncIterator, Optional, Callable, TypeVar
import httpx

T = TypeVar("T")


async def iter_pages(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
    page_size: int = 50,
    cursor_field: str = "next_cursor",
    items_field: str = "items",
    max_pages: int = 1000,
) -> AsyncIterator[dict]:
    """
    Async generator that automatically follows cursor pagination.
    Yields individual items (not pages) from all pages.

    Usage:
        async for item in iter_pages(client, "/api/conversations", {"user_id": "u1"}):
            process(item)
    """
    params = {**(params or {}), "limit": page_size}
    cursor = None
    pages_fetched = 0

    while pages_fetched < max_pages:
        if cursor:
            params["cursor"] = cursor

        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        items = data.get(items_field, [])
        for item in items:
            yield item

        cursor = data.get(cursor_field)
        has_more = data.get("has_more", bool(cursor))
        pages_fetched += 1

        if not has_more or not cursor:
            break

        await asyncio.sleep(0)  # Yield control between pages


async def collect_all_pages(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
    **kwargs,
) -> list[dict]:
    """Collect all items from all pages into a list."""
    return [item async for item in iter_pages(client, url, params, **kwargs)]


# ── Usage example ─────────────────────────────────────────────────────────────

async def export_all_conversations(user_id: str) -> list[dict]:
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        return await collect_all_pages(
            client,
            "/api/conversations",
            params={"user_id": user_id},
            page_size=100,
        )


async def process_stream(user_id: str):
    """Process conversations one at a time without loading all into memory."""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        count = 0
        async for conversation in iter_pages(
            client,
            "/api/conversations",
            params={"user_id": user_id},
            page_size=50,
        ):
            # Process one at a time — O(1) memory
            print(f"Processing: {conversation['title']}")
            count += 1
        print(f"Processed {count} conversations")
```

**Expected Token Savings:** Not applicable — client efficiency
**Environment:** `pip install httpx`

---

## Comparison Table

| Option | Pagination Style | Stable Under Inserts | Bi-directional | Memory Efficient | Total Count |
|--------|-----------------|---------------------|----------------|------------------|-------------|
| 1: Cursor-based | Cursor (opaque) | Yes | No | Yes | Optional |
| 2: Offset/limit | Page number | No (drift possible) | Yes | No | Yes |
| 3: Keyset | Primary key | Yes | Yes | Yes | No |
| 4: NDJSON streaming | Streaming | Yes | No | Yes (O(batch)) | No |
| 5: Memory search | Relevance + page | N/A | No | Yes (token budget) | Yes |
| 6: Client iterator | Cursor follower | N/A (client) | No | Yes (streaming) | No |
