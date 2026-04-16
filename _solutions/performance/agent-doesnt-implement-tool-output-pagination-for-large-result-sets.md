---
title: "Agent Doesn't Implement Tool Output Pagination for Large Result Sets"
description: "Agents that retrieve full result sets from tools — all matching database records, complete search results, entire file listings — inject thousands of tokens of content that the LLM will never process meaningfully, consume the context window, and slow response times. Implement tool output pagination that retrieves results in pages, injects only the current page into context, and allows the agent to request additional pages when the task requires more data."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tool-output-pagination-for-large-result-sets
tags: [pagination, tool-output, context-efficiency, large-result-sets, page-cursor, token-reduction]
symptoms:
  - "Database query tool returns all 10,000 matching records — context window overflows"
  - "Search tool returns 100 results when the agent only uses the top 3"
  - "File listing tool returns thousands of paths regardless of what the agent needs"
  - "No cursor or page token mechanism for resuming large result retrieval"
  - "Context window consumed by tool output before the agent generates any analysis"
---

## Why This Happens

Tool implementations often mirror the underlying API or database capabilities — returning the full result set if no limit is applied. Without a pagination contract between the tool and the agent, the agent has no way to request partial results and the tool has no way to signal that more results are available. Pagination requires three components: the tool must accept page size and cursor parameters, the tool must return a continuation cursor when results are truncated, and the agent must have a mechanism to request the next page if the task requires more data.

## Solution 1: Paginated Result

```python
from dataclasses import dataclass, field
from typing import Any, Generic, List, Optional, TypeVar

T = TypeVar("T")


@dataclass
class PaginatedResult(Generic[T]):
    items: List[T]
    total_count: Optional[int]    # None if total is unknown (e.g., streaming cursor)
    page_size: int
    page_number: int              # 1-indexed, for offset-based pagination
    cursor: Optional[str]         # opaque cursor for cursor-based pagination
    has_more: bool
    next_cursor: Optional[str]    # cursor to pass for the next page
    tool_name: str = ""
    query_summary: str = ""       # brief description of what was queried

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def estimated_tokens(self) -> int:
        return int(sum(len(str(item)) for item in self.items) * 0.25)
```

## Solution 2: Page Request Descriptor

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class PageRequest:
    page_size: int = 20           # items per page
    page_number: int = 1          # for offset pagination
    cursor: Optional[str] = None  # for cursor pagination; takes precedence
    max_tokens_per_page: int = 2000  # abort pagination if a page exceeds this

    def to_offset(self) -> int:
        """Convert page number to offset for SQL-style queries."""
        return (self.page_number - 1) * self.page_size
```

## Solution 3: Pagination-Aware Tool Wrapper

```python
import asyncio
import base64
import json
from typing import Any, Callable, Dict, List, Optional


class PaginationAwareToolWrapper:
    """
    Wraps a tool function that returns a list of results.
    Applies page_size limiting and generates opaque cursors
    for tools that do not natively support pagination.
    """

    def __init__(
        self,
        tool_name: str,
        tool_fn: Callable,            # async fn(**kwargs) -> List[Any]
        default_page_size: int = 20,
        max_page_size: int = 100,
    ):
        self._name = tool_name
        self._fn = tool_fn
        self._default_page_size = default_page_size
        self._max_page_size = max_page_size

    def _encode_cursor(self, offset: int) -> str:
        return base64.b64encode(json.dumps({"offset": offset}).encode()).decode()

    def _decode_cursor(self, cursor: str) -> int:
        try:
            return json.loads(base64.b64decode(cursor)).get("offset", 0)
        except Exception:
            return 0

    async def call_paged(
        self,
        args: Dict[str, Any],
        request: PageRequest,
    ) -> PaginatedResult:
        page_size = min(request.page_size or self._default_page_size, self._max_page_size)

        # Resolve offset from cursor or page number
        if request.cursor:
            offset = self._decode_cursor(request.cursor)
        else:
            offset = request.to_offset()

        # Fetch one extra to detect if there are more results
        all_results: List[Any] = await self._fn(**args)
        total = len(all_results)
        page_items = all_results[offset: offset + page_size]
        has_more = (offset + page_size) < total
        next_cursor = self._encode_cursor(offset + page_size) if has_more else None

        return PaginatedResult(
            items=page_items,
            total_count=total,
            page_size=page_size,
            page_number=(offset // page_size) + 1,
            cursor=request.cursor,
            has_more=has_more,
            next_cursor=next_cursor,
            tool_name=self._name,
        )
```

## Solution 4: Agent Page Navigator

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class AgentPageNavigator:
    """
    Gives the agent a structured interface for multi-page tool result retrieval.
    The agent can request the next page only if its task requires more data,
    preventing unnecessary context consumption.
    """

    def __init__(
        self,
        wrapper: PaginationAwareToolWrapper,
        max_pages_per_query: int = 5,
    ):
        self._wrapper = wrapper
        self._max_pages = max_pages_per_query

    async def get_first_page(
        self,
        args: Dict[str, Any],
        page_size: int = 20,
    ) -> PaginatedResult:
        request = PageRequest(page_size=page_size, page_number=1)
        return await self._wrapper.call_paged(args, request)

    async def get_next_page(
        self,
        args: Dict[str, Any],
        previous_result: PaginatedResult,
    ) -> Optional[PaginatedResult]:
        if not previous_result.has_more or not previous_result.next_cursor:
            return None
        request = PageRequest(
            page_size=previous_result.page_size,
            cursor=previous_result.next_cursor,
        )
        return await self._wrapper.call_paged(args, request)

    async def collect_pages(
        self,
        args: Dict[str, Any],
        page_size: int = 20,
        stop_condition: Optional[Callable[[PaginatedResult], bool]] = None,
    ) -> List[Any]:
        """
        Collects results across pages up to max_pages_per_query.
        Stops early if stop_condition returns True.
        """
        all_items: List[Any] = []
        result = await self.get_first_page(args, page_size)
        all_items.extend(result.items)

        pages_fetched = 1
        while result.has_more and pages_fetched < self._max_pages:
            if stop_condition and stop_condition(result):
                break
            result = await self.get_next_page(args, result)
            if result is None:
                break
            all_items.extend(result.items)
            pages_fetched += 1

        return all_items
```

## Solution 5: Context-Budget-Aware Page Selector

```python
from typing import Any, List, Optional


class ContextBudgetAwarePageSelector:
    """
    Selects a page size that fits within the remaining context budget.
    Prevents pagination from overrunning the LLM's context window
    when individual items vary widely in size.
    """

    def __init__(
        self,
        tokens_per_char: float = 0.25,
        min_page_size: int = 5,
        max_page_size: int = 100,
    ):
        self._tpc = tokens_per_char
        self._min = min_page_size
        self._max = max_page_size

    def select_page_size(
        self,
        available_tokens: int,
        sample_items: Optional[List[Any]] = None,
        avg_item_chars: int = 200,
    ) -> int:
        if sample_items:
            avg_item_chars = max(
                1, int(sum(len(str(i)) for i in sample_items) / len(sample_items))
            )
        tokens_per_item = int(avg_item_chars * self._tpc)
        computed = available_tokens // max(tokens_per_item, 1)
        return max(self._min, min(computed, self._max))

    def items_fit_in_budget(self, items: List[Any], available_tokens: int) -> bool:
        total_chars = sum(len(str(item)) for item in items)
        return int(total_chars * self._tpc) <= available_tokens
```

## Solution 6: Pagination Efficiency Dashboard

```python
import time
from typing import List


class PaginationEfficiencyDashboard:
    """
    Tracks how many pages are fetched per query, items-per-page utilization,
    and token savings from pagination vs. full result injection.
    """

    def __init__(self, tokens_per_char: float = 0.25):
        self._tpc = tokens_per_char
        self._queries: List[dict] = []
        self._recorded_at: List[float] = []

    def record_query(
        self,
        pages_fetched: int,
        items_returned: int,
        total_available: Optional[int],
        page_size: int,
    ) -> None:
        fraction_fetched = (
            round(items_returned / max(total_available, 1), 3)
            if total_available else None
        )
        self._queries.append({
            "pages": pages_fetched,
            "items_returned": items_returned,
            "total_available": total_available,
            "fraction_fetched": fraction_fetched,
            "page_size": page_size,
        })
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            q for q, ts in zip(self._queries, self._recorded_at) if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "queries": 0}

        avg_pages = sum(q["pages"] for q in recent) / len(recent)
        avg_fraction = [
            q["fraction_fetched"] for q in recent if q["fraction_fetched"] is not None
        ]
        return {
            "window_seconds": window_seconds,
            "queries": len(recent),
            "avg_pages_per_query": round(avg_pages, 2),
            "avg_fraction_of_results_fetched": round(
                sum(avg_fraction) / len(avg_fraction), 3
            ) if avg_fraction else None,
            "single_page_queries_pct": round(
                sum(1 for q in recent if q["pages"] == 1) / len(recent) * 100, 1
            ),
        }
```

## Comparison

| Approach | Page Size Control | Cursor Support | Context Budget Awareness | Multi-Page Collect | Efficiency Tracking |
|---|---|---|---|---|---|
| PaginationAwareToolWrapper | Yes | Yes (opaque) | No | No | No |
| AgentPageNavigator | Via wrapper | Via wrapper | No | Yes (bounded) | No |
| ContextBudgetAwarePageSelector | Yes (dynamic) | No | Yes | No | No |
| PaginationEfficiencyDashboard | No | No | No | No | Yes |

**Best for production**: Default `page_size=20` for text-heavy results and `page_size=50` for structured data (short records) — these fit within a 2,000-token page budget for most item sizes. Give the agent explicit pagination awareness in its system prompt: "If a tool returns `has_more: true`, you may request the next page by calling the tool with the provided `next_cursor` parameter." Without this instruction the LLM will not know to continue pagination. Set `max_pages_per_query=3` as the default — most agent tasks need only the most relevant first page or two, and allowing unlimited pagination creates a path for runaway context consumption. Monitor `avg_fraction_of_results_fetched` in `PaginationEfficiencyDashboard`: a value below 0.1 (10%) across most queries means the default page size is too large and can be reduced, saving tokens without losing coverage.
