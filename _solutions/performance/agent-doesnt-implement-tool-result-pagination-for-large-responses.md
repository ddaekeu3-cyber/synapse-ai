---
title: "Agent Doesn't Implement Tool Result Pagination for Large Responses"
description: "Agents that inject full tool results into context regardless of size will overflow the context window when a tool returns thousands of rows, a full file listing, or a large JSON payload. Implement tool result pagination that truncates results to a configurable page size, returns a continuation cursor, and allows the agent to fetch subsequent pages on demand — keeping each context injection bounded while preserving access to the full result set."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tool-result-pagination-for-large-responses
tags: [pagination, tool-results, context-window, cursor-based, large-responses, token-efficiency]
symptoms:
  - "Tool returns 5,000 rows and the entire result is injected into context, hitting the token limit"
  - "Agent receives a truncated JSON payload mid-object because the result was cut off by the context ceiling"
  - "No way for the agent to request the next page of a large result set"
  - "Every tool call re-fetches and re-injects the full dataset even when only the first few items are needed"
  - "Context window fills with tool output before the agent has had a chance to reason"
---

## Why This Happens

Tools are typically designed to return complete result sets. The calling agent does not enforce any size contract on what a tool can return, so a database query tool that returns ten thousand rows, a file-listing tool that traverses a deep directory tree, or an API tool that returns a full paginated resource without pagination parameters will inject arbitrarily large payloads into the context. Without a pagination layer between tool execution and context injection, there is no bound on how much of the context window a single tool call can consume. Pagination requires a result store, a cursor scheme, and a protocol that lets the agent call `get_next_page(cursor)` when it needs more.

## Solution 1: Paginated Result Store

```python
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PaginatedResultSet:
    result_id: str
    items: List[Any]
    page_size: int
    total_items: int
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 300.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_pages(self) -> int:
        return max(1, (self.total_items + self.page_size - 1) // self.page_size)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def get_page(self, page_number: int) -> List[Any]:
        start = page_number * self.page_size
        end = start + self.page_size
        return self.items[start:end]


class PaginatedResultStore:
    """
    Holds full tool result sets in memory. Each set is addressable by a
    result_id and supports cursor-based page retrieval.
    """

    def __init__(self, max_stored_results: int = 100):
        self._store: Dict[str, PaginatedResultSet] = {}
        self._max = max_stored_results

    def store(
        self,
        items: List[Any],
        page_size: int,
        ttl_seconds: float = 300.0,
        metadata: Dict[str, Any] = None,
    ) -> PaginatedResultSet:
        self._evict_expired()
        if len(self._store) >= self._max:
            # Evict oldest
            oldest_id = min(self._store, key=lambda k: self._store[k].created_at)
            del self._store[oldest_id]

        result_id = str(uuid.uuid4())
        result_set = PaginatedResultSet(
            result_id=result_id,
            items=items,
            page_size=page_size,
            total_items=len(items),
            ttl_seconds=ttl_seconds,
            metadata=metadata or {},
        )
        self._store[result_id] = result_set
        return result_set

    def get(self, result_id: str) -> Optional[PaginatedResultSet]:
        result = self._store.get(result_id)
        if result and result.is_expired():
            del self._store[result_id]
            return None
        return result

    def _evict_expired(self) -> None:
        expired = [k for k, v in self._store.items() if v.is_expired()]
        for k in expired:
            del self._store[k]
```

## Solution 2: Page Cursor

```python
import base64
import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class PageCursor:
    result_id: str
    page_number: int
    total_pages: int
    total_items: int
    page_size: int

    def encode(self) -> str:
        payload = {
            "r": self.result_id,
            "p": self.page_number,
            "tp": self.total_pages,
            "ti": self.total_items,
            "ps": self.page_size,
        }
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

    @classmethod
    def decode(cls, token: str) -> "PageCursor":
        payload = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
        return cls(
            result_id=payload["r"],
            page_number=payload["p"],
            total_pages=payload["tp"],
            total_items=payload["ti"],
            page_size=payload["ps"],
        )

    @property
    def has_next(self) -> bool:
        return self.page_number + 1 < self.total_pages

    def next_cursor(self) -> Optional["PageCursor"]:
        if not self.has_next:
            return None
        return PageCursor(
            result_id=self.result_id,
            page_number=self.page_number + 1,
            total_pages=self.total_pages,
            total_items=self.total_items,
            page_size=self.page_size,
        )
```

## Solution 3: Paginating Tool Wrapper

```python
from typing import Any, Callable, Dict, List, Optional


class PaginatingToolWrapper:
    """
    Wraps a raw tool call. Stores the full result in the PaginatedResultStore
    and returns only the first page plus a cursor for subsequent pages.
    """

    def __init__(
        self,
        store: PaginatedResultStore,
        default_page_size: int = 20,
        result_extractor: Optional[Callable[[Any], List[Any]]] = None,
    ):
        self._store = store
        self._page_size = default_page_size
        self._extractor = result_extractor or (lambda r: r if isinstance(r, list) else [r])

    async def call(
        self,
        tool_fn: Callable,
        *args: Any,
        page_size: Optional[int] = None,
        **kwargs: Any,
    ) -> dict:
        raw_result = await tool_fn(*args, **kwargs)
        items = self._extractor(raw_result)
        size = page_size or self._page_size

        result_set = self._store.store(items=items, page_size=size)
        first_page = result_set.get_page(0)

        cursor = PageCursor(
            result_id=result_set.result_id,
            page_number=0,
            total_pages=result_set.total_pages,
            total_items=result_set.total_items,
            page_size=size,
        )

        response = {
            "items": first_page,
            "page": 0,
            "page_size": size,
            "total_items": result_set.total_items,
            "total_pages": result_set.total_pages,
            "has_more": cursor.has_next,
        }
        if cursor.has_next:
            response["next_cursor"] = cursor.next_cursor().encode()

        return response
```

## Solution 4: Cursor-Based Page Fetcher

```python
from typing import Any, Dict, List


class CursorBasedPageFetcher:
    """
    Fetches subsequent pages from a stored result set using a cursor token.
    Intended to be exposed as a tool the agent can call when it needs more results.
    """

    def __init__(self, store: PaginatedResultStore):
        self._store = store

    def fetch_page(self, cursor_token: str) -> dict:
        try:
            cursor = PageCursor.decode(cursor_token)
        except Exception:
            return {"error": "invalid_cursor", "items": []}

        result_set = self._store.get(cursor.result_id)
        if result_set is None:
            return {"error": "result_expired_or_not_found", "items": []}

        page_items = result_set.get_page(cursor.page_number)
        next_cursor = cursor.next_cursor()

        response = {
            "items": page_items,
            "page": cursor.page_number,
            "page_size": cursor.page_size,
            "total_items": cursor.total_items,
            "total_pages": cursor.total_pages,
            "has_more": next_cursor is not None,
        }
        if next_cursor:
            response["next_cursor"] = next_cursor.encode()
        return response
```

## Solution 5: Token-Budget-Aware Page Sizer

```python
import json
from typing import Any, List


class TokenBudgetAwarePageSizer:
    """
    Dynamically computes a page size so that the serialized page fits
    within a token budget, using a characters-per-token estimate.
    """

    def __init__(
        self,
        token_budget: int = 2000,
        chars_per_token: float = 4.0,
        min_page_size: int = 1,
        max_page_size: int = 200,
    ):
        self._budget = token_budget
        self._chars_per_token = chars_per_token
        self._min = min_page_size
        self._max = max_page_size

    def compute_page_size(self, sample_items: List[Any]) -> int:
        if not sample_items:
            return self._max

        sample_size = min(5, len(sample_items))
        sample = sample_items[:sample_size]
        avg_chars = sum(len(json.dumps(item)) for item in sample) / sample_size
        avg_tokens = avg_chars / self._chars_per_token

        if avg_tokens <= 0:
            return self._max

        computed = int(self._budget / avg_tokens)
        return max(self._min, min(self._max, computed))

    def fits_in_budget(self, items: List[Any]) -> bool:
        total_chars = sum(len(json.dumps(item)) for item in items)
        return (total_chars / self._chars_per_token) <= self._budget
```

## Solution 6: Pagination Usage Monitor

```python
import time
from typing import List


class PaginationUsageMonitor:
    """
    Records pagination events to identify which tools produce the
    largest result sets and how often agents fetch beyond the first page.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record_first_page(
        self,
        tool_name: str,
        total_items: int,
        page_size: int,
        has_more: bool,
    ) -> None:
        self._append({
            "event": "first_page",
            "tool_name": tool_name,
            "total_items": total_items,
            "page_size": page_size,
            "has_more": has_more,
        })

    def record_continuation(self, tool_name: str, page_number: int) -> None:
        self._append({
            "event": "continuation",
            "tool_name": tool_name,
            "page_number": page_number,
        })

    def _append(self, record: dict) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        record["ts"] = time.time()
        self._records.append(record)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]

        first_pages = [r for r in recent if r["event"] == "first_page"]
        continuations = [r for r in recent if r["event"] == "continuation"]

        tool_totals: dict = {}
        for r in first_pages:
            name = r["tool_name"]
            if name not in tool_totals:
                tool_totals[name] = []
            tool_totals[name].append(r["total_items"])

        return {
            "window_seconds": window_seconds,
            "first_page_calls": len(first_pages),
            "continuation_calls": len(continuations),
            "continuation_rate": round(
                len(continuations) / max(len(first_pages), 1), 3
            ),
            "largest_result_sets": {
                tool: max(sizes)
                for tool, sizes in sorted(
                    tool_totals.items(), key=lambda kv: max(kv[1]), reverse=True
                )[:5]
            },
        }
```

## Comparison

| Approach | Result Storage | Cursor Encoding | Dynamic Page Size | Multi-Page Fetch | Usage Monitoring |
|---|---|---|---|---|---|
| PaginatedResultStore | Yes (TTL + eviction) | No | No | No | No |
| PageCursor | No | Yes (base64 JSON) | No | No | No |
| PaginatingToolWrapper | Via store | Via cursor | No | No | No |
| CursorBasedPageFetcher | Via store | Via cursor | No | Yes | No |
| TokenBudgetAwarePageSizer | No | No | Yes (char estimate) | No | No |
| PaginationUsageMonitor | No | No | No | No | Yes |

**Best for production**: Set `default_page_size` via `TokenBudgetAwarePageSizer` rather than a hardcoded constant — as tool result schemas change, the computed page size adapts automatically. Use `ttl_seconds=300` on stored result sets: most agent sessions complete within five minutes, so holding full result sets longer than that wastes memory. Expose `CursorBasedPageFetcher.fetch_page` as a dedicated agent tool named `get_next_page` with a single `cursor` parameter — this gives the agent a natural way to request more results without re-executing the original tool call. Monitor `continuation_rate` via `PaginationUsageMonitor`: a rate above 0.5 means agents are routinely paging through results and the default page size may be too small for the workload.
