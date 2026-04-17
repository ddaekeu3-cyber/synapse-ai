---
title: "Agent Doesn't Implement Query Result Pagination for Large Tool Responses"
description: "Agents that fetch complete result sets from database or search tools consume excess tokens and memory when results far exceed what the LLM can use: a tool returning 10,000 records when the agent needs only the top 20 wastes bandwidth, fills the context window with irrelevant data, and delays the response. Implement query result pagination that fetches only the needed page, supports continuation tokens for multi-turn retrieval, and tracks pagination state across turns."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-query-result-pagination-for-large-tool-responses
tags: [pagination, result-limiting, cursor-based-pagination, context-efficiency, tool-response-size, lazy-fetching]
symptoms:
  - "Tool responses contain thousands of records when the agent only needs the first 10"
  - "Context window fills up with tool result data that the LLM ignores"
  - "No way to retrieve the next page of results in a follow-up turn"
  - "Database tools use LIMIT 10000 as a safety cap instead of actual pagination"
  - "Memory spikes when large result sets are serialized into tool response strings"
---

## Why This Happens

Tools that expose database queries or search APIs often return all matching results because the caller — the agent — did not specify a limit. The agent loop then injects the full result set into the LLM context, consuming thousands of tokens for data the model will not use. Pagination requires two changes: the tool must accept `page_size` and `cursor` parameters, and the agent must track pagination state across turns so it can request the next page when needed.

## Solution 1: Pagination Parameters and State

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Generic, List, Optional, TypeVar

T = TypeVar("T")


@dataclass
class PaginationParams:
    page_size: int = 20
    cursor: Optional[str] = None   # opaque continuation token
    page_number: Optional[int] = None   # offset-based alternative


@dataclass
class PaginatedResult(Generic[T]):
    items: List[T]
    total_count: Optional[int]     # None if count is expensive
    page_size: int
    next_cursor: Optional[str]     # None if no more pages
    has_more: bool
    page_number: int = 1
    tool_name: str = ""
    query_id: str = field(default_factory=lambda: str(uuid.uuid4())[:16])
    fetched_at: float = field(default_factory=time.time)

    def to_context_dict(self) -> dict:
        """Serializes for injection into LLM context — omits raw items for brevity."""
        return {
            "item_count": len(self.items),
            "total_count": self.total_count,
            "has_more": self.has_more,
            "next_cursor": self.next_cursor,
            "page_number": self.page_number,
            "query_id": self.query_id,
        }
```

## Solution 2: Cursor Manager

```python
import base64
import json
import time
from threading import Lock
from typing import Any, Dict, Optional


class PaginationCursorManager:
    """
    Creates and resolves opaque cursor tokens that encode pagination state.
    Cursors are base64-encoded JSON blobs with TTL for safety.
    """

    def __init__(self, cursor_ttl_seconds: float = 3600.0):
        self._ttl = cursor_ttl_seconds
        self._cursors: Dict[str, dict] = {}
        self._lock = Lock()

    def create(
        self,
        tool_name: str,
        query_params: Dict[str, Any],
        offset: int,
        total: Optional[int] = None,
    ) -> str:
        state = {
            "tool": tool_name,
            "query": query_params,
            "offset": offset,
            "total": total,
            "created_at": time.time(),
        }
        cursor_id = base64.urlsafe_b64encode(
            json.dumps(state).encode()
        ).decode().rstrip("=")
        with self._lock:
            self._cursors[cursor_id] = state
        return cursor_id

    def resolve(self, cursor: str) -> Optional[dict]:
        with self._lock:
            # Try in-memory first
            if cursor in self._cursors:
                state = self._cursors[cursor]
                if time.time() - state["created_at"] < self._ttl:
                    return state
                del self._cursors[cursor]
                return None

        # Fall back to decoding the cursor itself
        try:
            padded = cursor + "=" * (4 - len(cursor) % 4)
            state = json.loads(base64.urlsafe_b64decode(padded))
            if time.time() - state.get("created_at", 0) < self._ttl:
                return state
            return None
        except Exception:
            return None

    def evict_expired(self) -> int:
        now = time.time()
        with self._lock:
            expired = [c for c, s in self._cursors.items() if now - s["created_at"] > self._ttl]
            for c in expired:
                del self._cursors[c]
            return len(expired)
```

## Solution 3: Paginated Tool Wrapper

```python
from typing import Any, Callable, Dict, List, Optional


class PaginatedToolWrapper:
    """
    Wraps a tool function that returns a list to add pagination support.
    The tool still fetches all results internally; pagination is applied
    at the response layer. For true DB-level pagination, use native LIMIT/OFFSET.
    """

    def __init__(
        self,
        cursor_manager: PaginationCursorManager,
        default_page_size: int = 20,
        max_page_size: int = 100,
    ):
        self._cursors = cursor_manager
        self._default_page_size = default_page_size
        self._max_page_size = max_page_size

    async def paginate(
        self,
        tool_name: str,
        fn: Callable,
        args: Dict[str, Any],
        params: PaginationParams,
    ) -> PaginatedResult:
        page_size = min(
            params.page_size or self._default_page_size,
            self._max_page_size,
        )

        # Resolve cursor to get offset
        offset = 0
        if params.cursor:
            state = self._cursors.resolve(params.cursor)
            if state and state["tool"] == tool_name:
                offset = state.get("offset", 0)

        # Fetch results — ideally tool supports limit/offset natively
        paginated_args = {**args, "limit": page_size + 1, "offset": offset}
        try:
            items = await fn(**paginated_args)
        except TypeError:
            # Tool doesn't support limit/offset — fetch all and slice
            all_items = await fn(**args)
            items = all_items[offset:offset + page_size + 1]

        has_more = len(items) > page_size
        page_items = items[:page_size]

        next_cursor = None
        if has_more:
            next_cursor = self._cursors.create(
                tool_name=tool_name,
                query_params=args,
                offset=offset + page_size,
            )

        return PaginatedResult(
            items=page_items,
            total_count=None,
            page_size=page_size,
            next_cursor=next_cursor,
            has_more=has_more,
            page_number=(offset // page_size) + 1,
            tool_name=tool_name,
        )
```

## Solution 4: Multi-Turn Pagination State Manager

```python
from typing import Dict, Optional


class MultiTurnPaginationStateManager:
    """
    Tracks active pagination cursors per conversation so the agent
    can continue fetching the next page in follow-up turns.
    """

    def __init__(self):
        self._state: Dict[str, Dict[str, str]] = {}
        # conversation_id -> {tool_name -> cursor}

    def store_cursor(self, conversation_id: str, tool_name: str, cursor: Optional[str]) -> None:
        if conversation_id not in self._state:
            self._state[conversation_id] = {}
        if cursor:
            self._state[conversation_id][tool_name] = cursor
        else:
            self._state[conversation_id].pop(tool_name, None)

    def get_cursor(self, conversation_id: str, tool_name: str) -> Optional[str]:
        return self._state.get(conversation_id, {}).get(tool_name)

    def has_more(self, conversation_id: str, tool_name: str) -> bool:
        return self.get_cursor(conversation_id, tool_name) is not None

    def clear_conversation(self, conversation_id: str) -> None:
        self._state.pop(conversation_id, None)

    def active_paginations(self, conversation_id: str) -> list:
        return list(self._state.get(conversation_id, {}).keys())
```

## Solution 5: Result Size Enforcer

```python
import json
from typing import Any


class ToolResultSizeEnforcer:
    """
    Enforces maximum result size before tool output is injected into context.
    Truncates item lists and adds a pagination notice when size is exceeded.
    """

    def __init__(
        self,
        max_items_per_response: int = 50,
        max_chars_per_response: int = 10000,
    ):
        self._max_items = max_items_per_response
        self._max_chars = max_chars_per_response

    def enforce(self, result: PaginatedResult) -> dict:
        items = result.items
        truncated_items = False

        if len(items) > self._max_items:
            items = items[:self._max_items]
            truncated_items = True

        serialized = json.dumps(items, default=str)
        truncated_chars = False

        if len(serialized) > self._max_chars:
            # Reduce items until under limit
            while items and len(json.dumps(items, default=str)) > self._max_chars:
                items = items[:-1]
            truncated_chars = True

        return {
            "items": items,
            "item_count": len(items),
            "total_available": result.total_count,
            "has_more": result.has_more or truncated_items or truncated_chars,
            "next_cursor": result.next_cursor,
            "page_number": result.page_number,
            "truncated": truncated_items or truncated_chars,
            "query_id": result.query_id,
        }
```

## Solution 6: Pagination Dashboard

```python
import time
from typing import List


class PaginationDashboard:
    """
    Tracks pagination usage patterns to identify tools that are
    commonly paginated and optimize their default page sizes.
    """

    def __init__(self):
        self._fetches: List[dict] = []

    def record(self, result: PaginatedResult) -> None:
        self._fetches.append({
            "ts": time.time(),
            "tool_name": result.tool_name,
            "page_number": result.page_number,
            "item_count": len(result.items),
            "has_more": result.has_more,
        })
        if len(self._fetches) > 50_000:
            self._fetches.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [f for f in self._fetches if f["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "fetches": 0}

        multi_page = sum(1 for f in recent if f["page_number"] > 1)
        has_more = sum(1 for f in recent if f["has_more"])
        by_tool: dict = {}
        for f in recent:
            name = f["tool_name"]
            by_tool[name] = by_tool.get(name, 0) + 1

        return {
            "window_seconds": window_seconds,
            "total_fetches": len(recent),
            "multi_page_fetches": multi_page,
            "fetches_with_more_available": has_more,
            "fetches_by_tool": by_tool,
        }
```

## Comparison

| Approach | Cursor-Based | Offset-Based | Multi-Turn State | Size Enforcement | Dashboard |
|---|---|---|---|---|---|
| PaginationCursorManager | Yes (encoded) | Via offset in cursor | No | No | No |
| PaginatedToolWrapper | Yes | Yes | No | No | No |
| MultiTurnPaginationStateManager | Via cursors | No | Yes | No | No |
| ToolResultSizeEnforcer | No | No | No | Yes | No |
| PaginationDashboard | No | No | No | No | Yes |

**Best for production**: Use cursor-based pagination over offset-based for tools backed by databases — offsets are unstable when new records are inserted between pages. Set `default_page_size=20` as a conservative default that fits comfortably in LLM context; increase to 50 only for simple structured data. Use `MultiTurnPaginationStateManager` to persist `next_cursor` between turns so the agent can naturally respond to "show me more" by using the stored cursor rather than re-running the query. Monitor `fetches_with_more_available` in the dashboard: a high fraction means users consistently need more than the first page, suggesting the default page size is too small or the initial query is too broad.
