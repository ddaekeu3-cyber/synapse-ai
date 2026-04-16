---
title: "Agent Doesn't Implement Tool Result Pagination to Avoid Context Overflow"
description: "Agents that inject full tool results into the context overflow the context window when tools return large datasets: a database query returning 500 rows, a file listing with 1,000 entries, or a search result with 50 documents. The agent either truncates silently, fails with a token limit error, or wastes tokens on low-priority results. Implement tool result pagination that limits result size per injection, tracks pagination state, and fetches the next page on demand."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tool-result-pagination-to-avoid-context-overflow
tags: [pagination, tool-results, context-overflow, result-size, large-datasets, incremental-retrieval]
symptoms:
  - "Database query tool returns 500 rows and overflows the context window"
  - "File listing tool returns all 1,000 files, most of which are irrelevant"
  - "Tool result truncation silently drops data the agent needed"
  - "No mechanism for the agent to request the next page of a large result set"
  - "Context token limit errors caused by single large tool results"
---

## Why This Happens

Tools return what they have: a database query returns all matching rows, a search returns all hits. The tool does not know how many tokens the agent can absorb. Without pagination at the tool dispatcher level, large results are injected in full. Pagination requires the dispatcher to slice results into pages, return the first page, and maintain a cursor so the agent can request subsequent pages using a `get_next_page` call. The agent sees a manageable result plus a pagination header indicating whether more results exist.

## Solution 1: Paginated Result

```python
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class PaginatedResult:
    items: List[Any]
    page: int
    page_size: int
    total_items: Optional[int]    # None if unknown
    has_next_page: bool
    cursor: Optional[str] = None  # opaque cursor for next-page fetch
    tool_name: str = ""
    query_summary: str = ""       # brief summary of the original query

    @property
    def is_last_page(self) -> bool:
        return not self.has_next_page

    def pagination_header(self) -> str:
        total_str = f"/{self.total_items}" if self.total_items is not None else ""
        return (
            f"[Page {self.page} | Showing {len(self.items)}{total_str} results"
            + (f" | More available — cursor: {self.cursor}" if self.has_next_page else " | End of results")
            + "]"
        )
```

## Solution 2: Result Paginator

```python
import hashlib
import json
import time
from threading import Lock
from typing import Any, Dict, List, Optional


class ToolResultPaginator:
    """
    Slices large result lists into pages and stores cursor state
    so subsequent pages can be retrieved by cursor token.
    """

    def __init__(
        self,
        default_page_size: int = 20,
        max_page_size: int = 100,
        cursor_ttl_seconds: float = 600.0,
    ):
        self._default_size = default_page_size
        self._max_size = max_page_size
        self._cursor_ttl = cursor_ttl_seconds
        self._cursors: Dict[str, dict] = {}
        self._lock = Lock()

    def _make_cursor(self, items: List[Any], offset: int, tool_name: str) -> str:
        payload = json.dumps({"tool": tool_name, "offset": offset, "ts": time.time()})
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def paginate(
        self,
        items: List[Any],
        tool_name: str,
        page_size: Optional[int] = None,
        query_summary: str = "",
    ) -> PaginatedResult:
        size = min(page_size or self._default_size, self._max_size)
        page_items = items[:size]
        has_next = len(items) > size

        cursor = None
        if has_next:
            cursor = self._make_cursor(items, size, tool_name)
            with self._lock:
                self._cursors[cursor] = {
                    "items": items,
                    "offset": size,
                    "tool_name": tool_name,
                    "created_at": time.time(),
                    "query_summary": query_summary,
                }

        return PaginatedResult(
            items=page_items,
            page=1,
            page_size=size,
            total_items=len(items),
            has_next_page=has_next,
            cursor=cursor,
            tool_name=tool_name,
            query_summary=query_summary,
        )

    def next_page(self, cursor: str, page_size: Optional[int] = None) -> Optional[PaginatedResult]:
        with self._lock:
            state = self._cursors.get(cursor)

        if state is None:
            return None

        # Check TTL
        if time.time() - state["created_at"] > self._cursor_ttl:
            with self._lock:
                self._cursors.pop(cursor, None)
            return None

        size = min(page_size or self._default_size, self._max_size)
        offset = state["offset"]
        items = state["items"]
        page_items = items[offset:offset + size]
        has_next = offset + size < len(items)
        page_num = offset // size + 1

        new_cursor = None
        if has_next:
            new_cursor = self._make_cursor(items, offset + size, state["tool_name"])
            with self._lock:
                self._cursors[new_cursor] = {
                    **state,
                    "offset": offset + size,
                    "created_at": time.time(),
                }
            with self._lock:
                self._cursors.pop(cursor, None)

        return PaginatedResult(
            items=page_items,
            page=page_num,
            page_size=size,
            total_items=len(items),
            has_next_page=has_next,
            cursor=new_cursor,
            tool_name=state["tool_name"],
            query_summary=state.get("query_summary", ""),
        )

    def evict_expired(self) -> int:
        cutoff = time.time() - self._cursor_ttl
        with self._lock:
            expired = [k for k, v in self._cursors.items() if v["created_at"] < cutoff]
            for k in expired:
                del self._cursors[k]
        return len(expired)
```

## Solution 3: Token-Aware Page Sizer

```python
from typing import Any, List


class TokenAwarePageSizer:
    """
    Determines how many items to include in a page based on
    estimated token cost per item and available token budget.
    """

    CHARS_PER_TOKEN = 4.0

    def __init__(self, default_item_tokens: int = 50):
        self._default_item_tokens = default_item_tokens

    def _estimate_item_tokens(self, item: Any) -> int:
        text = str(item)
        return max(1, int(len(text) / self.CHARS_PER_TOKEN))

    def optimal_page_size(
        self,
        items: List[Any],
        token_budget: int,
        sample_size: int = 5,
    ) -> int:
        if not items:
            return 1
        sample = items[:sample_size]
        avg_tokens = sum(self._estimate_item_tokens(item) for item in sample) / len(sample)
        overhead = 100   # header, separators, pagination marker
        usable = max(token_budget - overhead, 1)
        size = max(1, int(usable / avg_tokens))
        return size
```

## Solution 4: Paginated Tool Dispatcher

```python
from typing import Any, Callable, List, Optional


class PaginatedToolDispatcher:
    """
    Wraps tool calls and automatically paginates large results.
    Tools that return lists are paged; scalar results are returned as-is.
    """

    def __init__(
        self,
        paginator: ToolResultPaginator,
        sizer: TokenAwarePageSizer,
        token_budget_per_result: int = 2000,
    ):
        self._paginator = paginator
        self._sizer = sizer
        self._token_budget = token_budget_per_result
        self._paginated_tools: set = set()

    def register_paged_tool(self, tool_name: str) -> None:
        self._paginated_tools.add(tool_name)

    async def dispatch(
        self,
        tool_name: str,
        fn: Callable,
        query_summary: str = "",
    ) -> Any:
        result = await fn()

        if tool_name not in self._paginated_tools or not isinstance(result, list):
            return result

        page_size = self._sizer.optimal_page_size(result, self._token_budget)
        paged = self._paginator.paginate(
            result,
            tool_name=tool_name,
            page_size=page_size,
            query_summary=query_summary,
        )
        return paged

    def get_next_page(self, cursor: str) -> Optional[PaginatedResult]:
        return self._paginator.next_page(cursor)
```

## Solution 5: Pagination Usage Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class PaginationUsageTracker:
    """
    Tracks pagination patterns to identify tools that consistently
    require multiple pages — candidates for default page size tuning.
    """

    def __init__(self):
        self._records: Deque[dict] = deque(maxlen=10_000)
        self._lock = Lock()

    def record(self, result: PaginatedResult, pages_fetched: int = 1) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "tool_name": result.tool_name,
                "total_items": result.total_items,
                "page_size": result.page_size,
                "pages_fetched": pages_fetched,
                "had_next_page": result.has_next_page,
            })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"requests": 0}

        multi_page = [r for r in recent if r["pages_fetched"] > 1]
        by_tool: dict = {}
        for r in recent:
            t = r["tool_name"]
            if t not in by_tool:
                by_tool[t] = {"count": 0, "multi_page": 0}
            by_tool[t]["count"] += 1
            if r["pages_fetched"] > 1:
                by_tool[t]["multi_page"] += 1

        return {
            "requests": len(recent),
            "multi_page_rate": round(len(multi_page) / len(recent), 4),
            "by_tool": {
                t: {
                    "count": v["count"],
                    "multi_page_rate": round(v["multi_page"] / v["count"], 4),
                }
                for t, v in by_tool.items()
            },
        }
```

## Solution 6: Pagination Dashboard

```python
import time


class ToolResultPaginationDashboard:
    """
    Renders pagination policy, active cursor count, and usage patterns.
    """

    def __init__(
        self,
        paginator: ToolResultPaginator,
        dispatcher: PaginatedToolDispatcher,
        tracker: PaginationUsageTracker,
    ):
        self._paginator = paginator
        self._dispatcher = dispatcher
        self._tracker = tracker

    def render(self) -> dict:
        with self._paginator._lock:
            active_cursors = len(self._paginator._cursors)
        return {
            "generated_at": time.time(),
            "config": {
                "default_page_size": self._paginator._default_size,
                "max_page_size": self._paginator._max_size,
                "cursor_ttl_seconds": self._paginator._cursor_ttl,
                "token_budget_per_result": self._dispatcher._token_budget,
            },
            "active_cursors": active_cursors,
            "paged_tools": sorted(self._dispatcher._paginated_tools),
            "usage_1h": self._tracker.summary(3600.0),
        }
```

## Comparison

| Approach | Result Slicing | Cursor State | Token-Aware Sizing | Next-Page Fetch | Usage Tracking |
|---|---|---|---|---|---|
| ToolResultPaginator | Yes | Yes (TTL cursor) | No | Yes | No |
| TokenAwarePageSizer | No | No | Yes (avg tokens) | No | No |
| PaginatedToolDispatcher | Via paginator | Via paginator | Via sizer | Yes | No |
| PaginationUsageTracker | No | No | No | No | Yes |
| ToolResultPaginationDashboard | No | No | No | No | Via tracker |

**Best for production**: Set `default_page_size=20` and let `TokenAwarePageSizer` increase or decrease it based on actual item size — a page of 20 short database rows is very different from 20 large document chunks. Expose `get_next_page` as a tool the agent can call with the cursor token — this makes pagination agent-directed rather than automatic, which is important when the agent may not need all pages. Set `cursor_ttl_seconds=600` so cursors expire if the agent abandons a multi-page retrieval mid-session. Monitor `multi_page_rate` per tool — consistently above 0.30 means the default page size is too small for that tool's typical result set.
