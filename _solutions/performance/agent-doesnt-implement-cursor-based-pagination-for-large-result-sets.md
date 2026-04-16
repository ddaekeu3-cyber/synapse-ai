---
title: "Agent Doesn't Implement Cursor-Based Pagination for Large Result Sets"
description: "Agents that fetch large tool result sets in a single query or use OFFSET pagination suffer from slow queries, excessive memory usage, and inconsistent results on concurrent writes. Implement keyset/cursor-based pagination to efficiently page through large datasets without performance degradation."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-cursor-based-pagination-for-large-result-sets
tags: [pagination, cursor, keyset-pagination, performance, database, large-datasets]
symptoms:
  - "Tool call fetching 10,000 rows causes memory spike and slow LLM injection"
  - "OFFSET 9000 LIMIT 100 query takes 10x longer than OFFSET 0 LIMIT 100"
  - "Paginated results skip or duplicate rows on concurrent inserts/deletes"
  - "Agent loads entire result set into context window causing token overflow"
  - "Each page request rescans the full table from the start"
---

## Why This Happens

OFFSET pagination tells the database to skip N rows before returning the next page. This requires scanning all N preceding rows on every request. At large offsets (page 100+), this becomes prohibitively slow. Keyset pagination instead uses the last-seen value of an indexed column as a cursor — the next page starts *after* that value, so the DB uses an index range scan regardless of how deep into the result set you are. This is O(log N + page_size) instead of O(offset + page_size).

## Solution 1: Keyset Paginator for Single Sorted Column

```python
from __future__ import annotations
import base64
import json
from dataclasses import dataclass
from typing import Any, Generic, List, Optional, TypeVar

T = TypeVar("T")

@dataclass
class Page(Generic[T]):
    items: List[T]
    next_cursor: Optional[str]   # None means no more pages
    has_more: bool
    page_size: int

class KeysetPaginator:
    """
    Keyset pagination on a single indexed column (typically created_at or id).
    Cursor encodes the last-seen value; next page fetches rows AFTER it.
    """

    def __init__(self, db, table: str, cursor_column: str = "id", order: str = "ASC"):
        self._db = db
        self._table = table
        self._cursor_col = cursor_column
        self._order = order

    def _encode_cursor(self, value: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()

    def _decode_cursor(self, cursor: str) -> Any:
        return json.loads(base64.urlsafe_b64decode(cursor).decode())

    async def fetch_page(
        self,
        page_size: int = 50,
        cursor: Optional[str] = None,
        filters: Optional[dict] = None,
    ) -> Page:
        params: list = []
        conditions: list = []

        if filters:
            for i, (col, val) in enumerate(filters.items(), start=1):
                conditions.append(f"{col} = ${i}")
                params.append(val)

        if cursor is not None:
            last_value = self._decode_cursor(cursor)
            op = ">" if self._order == "ASC" else "<"
            n = len(params) + 1
            conditions.append(f"{self._cursor_col} {op} ${n}")
            params.append(last_value)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_n = len(params) + 1
        params.append(page_size + 1)  # fetch one extra to detect has_more

        sql = (
            f"SELECT * FROM {self._table} "
            f"{where_clause} "
            f"ORDER BY {self._cursor_col} {self._order} "
            f"LIMIT ${limit_n}"
        )

        async with self._db.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        items = [dict(r) for r in rows[:page_size]]
        has_more = len(rows) > page_size
        next_cursor = (
            self._encode_cursor(items[-1][self._cursor_col]) if has_more and items else None
        )
        return Page(items=items, next_cursor=next_cursor, has_more=has_more, page_size=page_size)
```

## Solution 2: Compound Cursor Paginator (Sort by Multiple Columns)

```python
import base64
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

@dataclass
class CompoundCursor:
    values: Dict[str, Any]  # {column: last_seen_value}

class CompoundKeysetPaginator:
    """
    Keyset pagination with compound sort key (e.g., created_at + id).
    Handles ties in the primary sort column by using id as tiebreaker.
    """

    def __init__(self, db, table: str, sort_columns: List[Tuple[str, str]]):
        """
        sort_columns: [(column_name, 'ASC'|'DESC'), ...]
        Example: [('created_at', 'DESC'), ('id', 'ASC')]
        """
        self._db = db
        self._table = table
        self._sort_columns = sort_columns

    def encode_cursor(self, row: dict) -> str:
        values = {col: row[col] for col, _ in self._sort_columns}
        return base64.urlsafe_b64encode(json.dumps(values, default=str).encode()).decode()

    def decode_cursor(self, cursor: str) -> CompoundCursor:
        values = json.loads(base64.urlsafe_b64decode(cursor).decode())
        return CompoundCursor(values=values)

    def _build_cursor_condition(self, cursor: CompoundCursor, params: list) -> str:
        """
        Generates the compound WHERE clause for keyset pagination.
        For (a ASC, b ASC), after (a0, b0):
          (a > a0) OR (a = a0 AND b > b0)
        """
        cols = self._sort_columns
        clauses = []
        for i in range(len(cols)):
            parts = []
            for j in range(i):
                col, direction = cols[j]
                n = len(params) + 1
                params.append(cursor.values[col])
                parts.append(f"{col} = ${n}")
            col, direction = cols[i]
            op = ">" if direction == "ASC" else "<"
            n = len(params) + 1
            params.append(cursor.values[col])
            parts.append(f"{col} {op} ${n}")
            clauses.append("(" + " AND ".join(parts) + ")")
        return "(" + " OR ".join(clauses) + ")"

    async def fetch_page(
        self,
        page_size: int = 50,
        cursor: Optional[str] = None,
        extra_filters: Optional[dict] = None,
    ) -> Page:
        params: list = []
        conditions: list = []

        if extra_filters:
            for col, val in extra_filters.items():
                n = len(params) + 1
                conditions.append(f"{col} = ${n}")
                params.append(val)

        if cursor is not None:
            decoded = self.decode_cursor(cursor)
            conditions.append(self._build_cursor_condition(decoded, params))

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        order_clause = ", ".join(f"{col} {direction}" for col, direction in self._sort_columns)
        limit_n = len(params) + 1
        params.append(page_size + 1)

        sql = (
            f"SELECT * FROM {self._table} "
            f"{where_clause} "
            f"ORDER BY {order_clause} "
            f"LIMIT ${limit_n}"
        )
        async with self._db.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        items = [dict(r) for r in rows[:page_size]]
        has_more = len(rows) > page_size
        next_cursor = self.encode_cursor(items[-1]) if has_more and items else None
        return Page(items=items, next_cursor=next_cursor, has_more=has_more, page_size=page_size)
```

## Solution 3: Async Generator for Full Dataset Iteration

```python
import asyncio
from typing import AsyncIterator, Optional

class CursorPageIterator:
    """
    AsyncIterator that automatically follows cursor chains.
    Lets agents iterate over arbitrarily large result sets
    without loading everything into memory.
    """

    def __init__(
        self,
        paginator: KeysetPaginator,
        page_size: int = 100,
        cursor: Optional[str] = None,
        **filter_kwargs,
    ):
        self._paginator = paginator
        self._page_size = page_size
        self._cursor = cursor
        self._filters = filter_kwargs
        self._buffer: list = []
        self._exhausted = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._buffer:
            if self._exhausted:
                raise StopAsyncIteration
            page = await self._paginator.fetch_page(
                page_size=self._page_size,
                cursor=self._cursor,
                filters=self._filters or None,
            )
            self._buffer = page.items
            self._cursor = page.next_cursor
            if not page.has_more:
                self._exhausted = True
            if not self._buffer:
                raise StopAsyncIteration
        return self._buffer.pop(0)


# Usage: agent iterates through all messages without loading all into memory
async def summarize_all_messages(paginator: KeysetPaginator, user_id: str) -> str:
    summaries = []
    async for message in CursorPageIterator(paginator, page_size=100, user_id=user_id):
        summaries.append(message["content"][:200])
    return "\n".join(summaries)
```

## Solution 4: Bidirectional Cursor Paginator (Forward + Backward)

```python
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

class CursorDirection(Enum):
    FORWARD = "forward"
    BACKWARD = "backward"

@dataclass
class BidirectionalPage:
    items: List[dict]
    start_cursor: Optional[str]   # cursor to go backward from first item
    end_cursor: Optional[str]     # cursor to go forward from last item
    has_previous: bool
    has_next: bool

class BidirectionalPaginator:
    """
    Supports both forward (next page) and backward (previous page) navigation.
    Uses the same keyset approach in both directions.
    """

    def __init__(self, db, table: str, cursor_column: str = "id"):
        self._db = db
        self._table = table
        self._col = cursor_column

    async def fetch(
        self,
        page_size: int = 50,
        after: Optional[str] = None,   # forward cursor
        before: Optional[str] = None,  # backward cursor
    ) -> BidirectionalPage:
        import base64, json

        def decode(c): return json.loads(base64.urlsafe_b64decode(c).decode())
        def encode(v): return base64.urlsafe_b64encode(json.dumps(v).encode()).decode()

        params = [page_size + 1]
        if after:
            last_id = decode(after)
            sql = (
                f"SELECT * FROM {self._table} WHERE {self._col} > $2 "
                f"ORDER BY {self._col} ASC LIMIT $1"
            )
            params.append(last_id)
        elif before:
            first_id = decode(before)
            # Fetch in reverse, then flip the result
            sql = (
                f"SELECT * FROM (SELECT * FROM {self._table} WHERE {self._col} < $2 "
                f"ORDER BY {self._col} DESC LIMIT $1) sub ORDER BY {self._col} ASC"
            )
            params.append(first_id)
        else:
            sql = f"SELECT * FROM {self._table} ORDER BY {self._col} ASC LIMIT $1"

        async with self._db.acquire() as conn:
            rows = [dict(r) for r in await conn.fetch(sql, *params)]

        has_more = len(rows) > page_size
        items = rows[:page_size]

        return BidirectionalPage(
            items=items,
            start_cursor=encode(items[0][self._col]) if items else None,
            end_cursor=encode(items[-1][self._col]) if items else None,
            has_previous=bool(before) or False,
            has_next=has_more,
        )
```

## Solution 5: Agent Tool Wrapper with Automatic Chunked Injection

```python
import asyncio
from typing import Any, Callable, List, Optional

class PaginatedToolWrapper:
    """
    Wraps any tool that returns large result sets. Fetches pages
    incrementally and injects summaries into the agent context window
    rather than dumping the entire result.
    """

    def __init__(
        self,
        paginator: KeysetPaginator,
        max_items_per_context: int = 50,
        summarize_fn: Optional[Callable[[List[dict]], str]] = None,
    ):
        self._paginator = paginator
        self._max_items = max_items_per_context
        self._summarize = summarize_fn or self._default_summary

    async def fetch_for_context(
        self,
        cursor: Optional[str] = None,
        filters: Optional[dict] = None,
    ) -> dict:
        """
        Returns a context-friendly dict with items + navigation info.
        Agent includes next_cursor in its state for subsequent calls.
        """
        page = await self._paginator.fetch_page(
            page_size=self._max_items,
            cursor=cursor,
            filters=filters,
        )
        return {
            "items": page.items,
            "summary": self._summarize(page.items),
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
            "count": len(page.items),
            "instruction": (
                "Call this tool again with next_cursor to retrieve the next page."
                if page.has_more else "All results have been returned."
            ),
        }

    @staticmethod
    def _default_summary(items: List[dict]) -> str:
        if not items:
            return "No items."
        keys = list(items[0].keys())[:4]
        lines = [", ".join(f"{k}={row.get(k,'?')}" for k in keys) for row in items[:5]]
        suffix = f" ... and {len(items)-5} more" if len(items) > 5 else ""
        return "\n".join(lines) + suffix
```

## Solution 6: Cursor Stability Guarantees for Concurrent Writes

```python
from dataclasses import dataclass
from typing import Any, List, Optional

@dataclass
class StableCursorPage:
    items: List[dict]
    next_cursor: Optional[str]
    snapshot_xid: Optional[int]   # PostgreSQL transaction snapshot for stability

class SnapshotIsolatedPaginator:
    """
    Uses PostgreSQL REPEATABLE READ transaction snapshot to ensure
    that all pages of a result set see a consistent view, even if
    rows are inserted or deleted between page fetches.
    """

    def __init__(self, db):
        self._db = db

    async def start_session(self) -> int:
        """Begin a snapshot and return its transaction ID."""
        async with self._db.acquire() as conn:
            await conn.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            xid = await conn.fetchval("SELECT txid_current_snapshot()")
            return xid

    async def fetch_page_in_snapshot(
        self,
        conn,
        table: str,
        cursor_col: str,
        after_value: Optional[Any],
        page_size: int = 50,
    ) -> StableCursorPage:
        import base64, json

        def encode(v): return base64.urlsafe_b64encode(json.dumps(v, default=str).encode()).decode()

        if after_value is not None:
            rows = await conn.fetch(
                f"SELECT * FROM {table} WHERE {cursor_col} > $1 ORDER BY {cursor_col} LIMIT $2",
                after_value, page_size + 1,
            )
        else:
            rows = await conn.fetch(
                f"SELECT * FROM {table} ORDER BY {cursor_col} LIMIT $1",
                page_size + 1,
            )

        items = [dict(r) for r in rows[:page_size]]
        has_more = len(rows) > page_size
        xid = await conn.fetchval("SELECT txid_current()")
        return StableCursorPage(
            items=items,
            next_cursor=encode(items[-1][cursor_col]) if has_more and items else None,
            snapshot_xid=xid,
        )
```

## Comparison

| Approach | Performance | Stable Under Writes | Bidirectional | Memory Usage |
|---|---|---|---|---|
| KeysetPaginator | O(log N + page) | Partial (no snapshot) | No | O(page_size) |
| CompoundKeysetPaginator | O(log N + page) | Partial | No | O(page_size) |
| CursorPageIterator | O(log N + page) × pages | Partial | No | O(page_size) |
| BidirectionalPaginator | O(log N + page) | Partial | Yes | O(page_size) |
| PaginatedToolWrapper | O(log N + page) | Partial | No | O(max_items) |
| SnapshotIsolatedPaginator | O(log N + page) | Full (REPEATABLE READ) | No | O(page_size) |

**Best for production**: Use `KeysetPaginator` or `CompoundKeysetPaginator` for all agent tool result sets. Use `CursorPageIterator` when the agent needs to process all results sequentially. Use `SnapshotIsolatedPaginator` when data consistency across pages is critical (e.g., financial reports, compliance exports). Always encode cursors as opaque base64 tokens — never expose raw DB IDs in the cursor.
