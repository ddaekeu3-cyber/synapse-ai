---
title: "Agent Doesn't Implement Tool Result Pagination for Large Datasets"
description: "Agents that inject full tool results for large dataset queries — 'list all orders', 'fetch all users', 'get full transaction history' — overwhelm the LLM context with thousands of records, consuming most of the token budget on data the model will never use. Implement tool result pagination that fetches data in pages, injects only the relevant page, and provides navigation context so the agent can request additional pages if needed."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tool-result-pagination-for-large-datasets
tags: [pagination, large-datasets, context-efficiency, tool-results, page-navigation, token-budget]
symptoms:
  - "A 'list all records' tool returns 10,000 items that fill the entire context window"
  - "Agent uses only the first 20 records but all 10,000 are injected into context"
  - "No page size limit — tool results grow with dataset size regardless of what the agent needs"
  - "Large tool results push earlier context (system prompt, conversation) out of the window"
  - "No pagination metadata — agent cannot request the next page even if it needs more results"
---

## Why This Happens

Tools that query databases or APIs often return all matching records. When injected into the LLM context, large result sets consume most of the token budget on data the model statistically processes only the first few hundred tokens of anyway. Pagination constrains injected data to a window the model can actually use, provides navigation metadata so the agent can request additional pages when needed, and preserves token budget for system prompt, conversation history, and response generation.

## Solution 1: Paginated Tool Result

```python
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class PaginatedToolResult:
    items: List[Any]
    total_count: int
    page: int                    # 1-indexed
    page_size: int
    has_next_page: bool
    has_prev_page: bool
    cursor: Optional[str] = None  # opaque cursor for cursor-based pagination
    query_summary: str = ""       # human-readable description of the query

    @property
    def total_pages(self) -> int:
        if self.page_size == 0:
            return 0
        import math
        return math.ceil(self.total_count / self.page_size)

    def navigation_hint(self) -> str:
        parts = [
            f"Showing {len(self.items)} of {self.total_count} results",
            f"(page {self.page}/{self.total_pages})",
        ]
        if self.has_next_page:
            parts.append("— call with page=" + str(self.page + 1) + " for more")
        return " ".join(parts)
```

## Solution 2: Page Size Policy

```python
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class PageSizePolicy:
    tool_name: str
    default_page_size: int = 20
    max_page_size: int = 100
    token_budget_per_item: int = 50    # estimated tokens per result item


DEFAULT_PAGE_POLICIES: Dict[str, PageSizePolicy] = {
    "list_orders": PageSizePolicy("list_orders", default_page_size=10, max_page_size=50),
    "search_users": PageSizePolicy("search_users", default_page_size=20, max_page_size=100),
    "get_transactions": PageSizePolicy("get_transactions", default_page_size=15, max_page_size=50),
    "list_files": PageSizePolicy("list_files", default_page_size=25, max_page_size=100),
}


class PageSizePolicyRegistry:
    def __init__(self, policies: Dict[str, PageSizePolicy]):
        self._policies = policies

    def get(self, tool_name: str) -> PageSizePolicy:
        return self._policies.get(
            tool_name,
            PageSizePolicy(tool_name=tool_name),
        )

    def effective_page_size(
        self, tool_name: str, requested: Optional[int], token_budget: Optional[int] = None
    ) -> int:
        policy = self.get(tool_name)
        if requested is not None:
            size = min(requested, policy.max_page_size)
        else:
            size = policy.default_page_size
        # Further constrain by token budget if provided
        if token_budget and policy.token_budget_per_item > 0:
            budget_limit = token_budget // policy.token_budget_per_item
            size = min(size, max(1, budget_limit))
        return size
```

## Solution 3: Pagination-Aware Tool Wrapper

```python
from typing import Any, Callable, List, Optional


class PaginationAwareToolWrapper:
    """
    Wraps a data-fetching tool function to enforce page size limits.
    Adds navigation metadata to every result so the LLM knows
    whether more data is available and how to request it.
    """

    def __init__(self, policy_registry: PageSizePolicyRegistry):
        self._registry = policy_registry

    async def fetch_page(
        self,
        tool_name: str,
        fetch_fn: Callable,
        page: int = 1,
        page_size: Optional[int] = None,
        token_budget: Optional[int] = None,
        **kwargs: Any,
    ) -> PaginatedToolResult:
        effective_size = self._registry.effective_page_size(
            tool_name, page_size, token_budget
        )
        offset = (page - 1) * effective_size

        # Fetch one extra item to detect if there's a next page
        raw_items = await fetch_fn(
            offset=offset,
            limit=effective_size + 1,
            **kwargs,
        )

        has_next = len(raw_items) > effective_size
        items = raw_items[:effective_size]

        total_count = kwargs.get("total_count_hint", len(items) + offset + (1 if has_next else 0))

        return PaginatedToolResult(
            items=items,
            total_count=total_count,
            page=page,
            page_size=effective_size,
            has_next_page=has_next,
            has_prev_page=page > 1,
        )
```

## Solution 4: Context-Budget-Adaptive Paginator

```python
from typing import Any, Callable, List, Optional


class ContextBudgetAdaptivePaginator:
    """
    Dynamically adjusts page size based on remaining context budget.
    Injects a compact summary when the budget is too small for a full page.
    """

    def __init__(
        self,
        wrapper: PaginationAwareToolWrapper,
        chars_per_token: int = 4,
    ):
        self._wrapper = wrapper
        self._chars_per_token = chars_per_token

    async def fetch(
        self,
        tool_name: str,
        fetch_fn: Callable,
        available_tokens: int,
        page: int = 1,
        **kwargs: Any,
    ) -> dict:
        result = await self._wrapper.fetch_page(
            tool_name=tool_name,
            fetch_fn=fetch_fn,
            page=page,
            token_budget=available_tokens,
            **kwargs,
        )

        return {
            "items": result.items,
            "navigation": result.navigation_hint(),
            "page": result.page,
            "total_pages": result.total_pages,
            "has_next": result.has_next_page,
            "has_prev": result.has_prev_page,
            "item_count": len(result.items),
            "total_count": result.total_count,
        }
```

## Solution 5: Multi-Page Aggregator

```python
from typing import Any, Callable, List, Optional


class MultiPageAggregator:
    """
    Fetches multiple pages and aggregates results for cases where
    the agent explicitly needs a larger result set but still within limits.
    Stops when max_pages is reached or no more pages exist.
    """

    def __init__(
        self,
        wrapper: PaginationAwareToolWrapper,
        max_pages: int = 5,
    ):
        self._wrapper = wrapper
        self._max_pages = max_pages

    async def fetch_all(
        self,
        tool_name: str,
        fetch_fn: Callable,
        page_size: int = 20,
        **kwargs: Any,
    ) -> dict:
        all_items: List[Any] = []
        page = 1

        while page <= self._max_pages:
            result = await self._wrapper.fetch_page(
                tool_name=tool_name,
                fetch_fn=fetch_fn,
                page=page,
                page_size=page_size,
                **kwargs,
            )
            all_items.extend(result.items)
            if not result.has_next_page:
                break
            page += 1

        return {
            "items": all_items,
            "pages_fetched": page,
            "truncated": page >= self._max_pages,
            "total_items_fetched": len(all_items),
        }
```

## Solution 6: Pagination Usage Monitor

```python
import time
from typing import List


class PaginationUsageMonitor:
    """
    Records pagination events to surface which tools return large result sets
    and how often multi-page fetches are needed.
    """

    def __init__(self):
        self._events: List[dict] = []

    def record(self, tool_name: str, result: PaginatedToolResult) -> None:
        self._events.append({
            "ts": time.time(),
            "tool": tool_name,
            "page": result.page,
            "page_size": result.page_size,
            "total_count": result.total_count,
            "has_next": result.has_next_page,
            "utilization": round(len(result.items) / max(result.total_count, 1), 4),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "events": 0}
        multi_page = [e for e in recent if e["has_next"]]
        tool_counts: dict = {}
        for e in recent:
            t = e["tool"]
            tool_counts[t] = tool_counts.get(t, 0) + 1
        return {
            "window_seconds": window_seconds,
            "total_paginated_calls": len(recent),
            "multi_page_rate": round(len(multi_page) / len(recent), 4),
            "avg_utilization": round(sum(e["utilization"] for e in recent) / len(recent), 4),
            "calls_by_tool": tool_counts,
        }
```

## Comparison

| Approach | Page Size Enforcement | Token-Budget Adaptation | Navigation Metadata | Multi-Page Fetch | Usage Monitoring |
|---|---|---|---|---|---|
| PaginationAwareToolWrapper | Yes | Via policy | Yes | No | No |
| PageSizePolicyRegistry | Yes (per tool) | Yes (budget param) | No | No | No |
| ContextBudgetAdaptivePaginator | Via wrapper | Yes (auto-adapt) | Yes | No | No |
| MultiPageAggregator | Via wrapper | No | No | Yes (capped) | No |
| PaginationUsageMonitor | No | No | No | No | Yes |

**Best for production**: Default page size should be calibrated so the full page fits in 20% of the available context budget — this leaves 80% for system prompt, conversation history, and response. Include `navigation_hint()` in every injected tool result so the LLM sees "Showing 20 of 1,847 results (page 1/93) — call with page=2 for more" and can request additional pages when the current page does not contain what it needs. Cap `MultiPageAggregator.max_pages=3` by default — fetching more than 3 pages in one turn usually indicates the query is too broad and the tool call arguments need refinement rather than more pages. Monitor `avg_utilization`: consistently below 0.10 (agent only uses 10% of the page) suggests page size is still too large.
