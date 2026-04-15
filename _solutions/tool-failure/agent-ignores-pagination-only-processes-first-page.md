---
layout: solution
title: "Agent Only Processes First Page of Results — Silently Misses Remaining Pages"
category: tool-failure
description: "Agent calls an API that returns paginated results. It reads the first page (100 items) and considers the task done. The remaining 9 pages (900 items) are never fetched. Agent reports success on incomplete data."
tags: [pagination, api, incomplete-data, cursor, next-page, silent-failure, batch]
---

# Agent Only Processes First Page of Results — Silently Misses Remaining Pages

## Symptom
- Agent reports "processed 100 users" when there are 5,000 users in the system
- API response has `"has_more": true` but agent doesn't check it
- `next_cursor` in the response is ignored — only first page fetched
- Report covers only 10% of data because agent stopped after page 1
- Agent doesn't error — silently returns partial results as if complete
- Link header with `rel="next"` URL is present but not followed

## Root Cause
APIs paginate results to limit response size. The first response contains partial data plus a signal that more exists: `has_more`, `next_cursor`, `next_page_token`, or an HTTP `Link` header. Agents that don't check for these signals treat the first page as the complete result set. No error is raised — the agent just silently discards the remaining data.

## Fix

### Option 1: Generic paginator for cursor-based APIs

```python
import httpx
from typing import AsyncIterator

async def paginate_cursor(
    client: httpx.AsyncClient,
    url: str,
    params: dict = None,
    cursor_field: str = "next_cursor",
    items_field: str = "items",
    has_more_field: str = "has_more",
    page_limit: int = None
) -> AsyncIterator[dict]:
    """
    Yield all items from a cursor-paginated API.
    Handles: has_more, next_cursor, next_page_token, etc.
    """
    params = dict(params or {})
    pages_fetched = 0

    while True:
        response = await client.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        items = data.get(items_field, [])
        for item in items:
            yield item

        pages_fetched += 1
        if page_limit and pages_fetched >= page_limit:
            print(f"Page limit {page_limit} reached — stopping pagination")
            break

        # Check for next cursor (various field names)
        next_cursor = (
            data.get(cursor_field) or
            data.get("next_cursor") or
            data.get("cursor") or
            data.get("next_page_token") or
            data.get("pagination", {}).get("next_cursor")
        )

        has_more = data.get(has_more_field, bool(next_cursor))

        if not has_more or not next_cursor:
            print(f"Pagination complete: {pages_fetched} pages fetched")
            break

        params["cursor"] = next_cursor
        print(f"Fetching page {pages_fetched + 1} (cursor: {next_cursor[:20]}...)")

# Usage — all items, not just page 1:
async with httpx.AsyncClient() as client:
    all_users = []
    async for user in paginate_cursor(client, "/api/users", {"limit": 100}):
        all_users.append(user)
    print(f"Total users: {len(all_users)}")  # → 5,000, not 100
```

### Option 2: Page-number based pagination

```python
async def paginate_by_page(
    client: httpx.AsyncClient,
    url: str,
    params: dict = None,
    items_field: str = "results",
    total_field: str = "total",
    page_size: int = 100
) -> list:
    """
    Fetch all pages from a page-number-based API.
    Reads total count from first response to determine how many pages to fetch.
    """
    params = dict(params or {})
    params["page"] = 1
    params["per_page"] = page_size

    # Fetch first page to get total count
    response = await client.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    items = data.get(items_field, [])
    total = data.get(total_field, len(items))

    if total <= page_size:
        return items  # Only one page

    # Calculate remaining pages
    import math
    total_pages = math.ceil(total / page_size)
    print(f"Fetching {total_pages} pages ({total} total items)")

    # Fetch remaining pages
    import asyncio
    async def fetch_page(page_num: int) -> list:
        p = dict(params)
        p["page"] = page_num
        resp = await client.get(url, params=p, timeout=30)
        resp.raise_for_status()
        return resp.json().get(items_field, [])

    # Parallel fetch of remaining pages
    remaining_pages = await asyncio.gather(*[
        fetch_page(page) for page in range(2, total_pages + 1)
    ])

    for page_items in remaining_pages:
        items.extend(page_items)

    print(f"All pages fetched: {len(items)} items (expected {total})")
    return items
```

### Option 3: Link header pagination (GitHub, REST standards)

```python
import re
import httpx
from typing import AsyncIterator

def parse_link_header(header: str) -> dict[str, str]:
    """
    Parse HTTP Link header for pagination URLs.
    Format: <url>; rel="next", <url>; rel="last"
    """
    links = {}
    if not header:
        return links
    for part in header.split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', part.strip())
        if match:
            links[match.group(2)] = match.group(1)
    return links

async def paginate_link_header(
    client: httpx.AsyncClient,
    url: str,
    items_field: str = None
) -> AsyncIterator:
    """
    Follow Link: <next> headers until no next page exists.
    Used by GitHub API, many REST APIs.
    """
    current_url = url
    page = 1

    while current_url:
        response = await client.get(current_url, timeout=30)
        response.raise_for_status()
        data = response.json()

        # Yield items (or entire response if no items_field)
        if items_field:
            for item in data.get(items_field, []):
                yield item
        elif isinstance(data, list):
            for item in data:
                yield item
        else:
            yield data

        # Follow next page
        links = parse_link_header(response.headers.get("link", ""))
        current_url = links.get("next")
        if current_url:
            page += 1
            print(f"Following link to page {page}")

# GitHub API example:
async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
    async for repo in paginate_link_header(client, "https://api.github.com/user/repos?per_page=100"):
        process_repo(repo)
```

### Option 4: Tool wrapper that auto-paginates

```python
def auto_paginating_tool(
    fn,
    items_extractor=lambda r: r.get("items", []),
    has_more_check=lambda r: r.get("has_more", False),
    next_page_fn=lambda r, params: {**params, "cursor": r.get("next_cursor")}
):
    """
    Wrap a tool function to automatically fetch all pages.
    The agent calls the tool once — it internally paginates.
    """
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, params: dict = None, **kwargs):
        params = params or {}
        all_items = []
        page = 1

        while True:
            result = await fn(*args, params=params, **kwargs)
            items = items_extractor(result)
            all_items.extend(items)

            print(f"Page {page}: {len(items)} items (total so far: {len(all_items)})")

            if not has_more_check(result) or not items:
                break

            params = next_page_fn(result, params)
            page += 1

        return {"items": all_items, "total": len(all_items), "pages": page}

    return wrapper

# Apply to a tool:
@auto_paginating_tool(
    items_extractor=lambda r: r.get("users", []),
    has_more_check=lambda r: bool(r.get("next_cursor")),
    next_page_fn=lambda r, p: {**p, "cursor": r.get("next_cursor")}
)
async def list_users(params: dict) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get("/api/users", params=params)
        return resp.json()

# Agent calls once, gets all pages:
result = await list_users(params={"limit": 100})
print(f"Total users: {result['total']}")  # All users, not just 100
```

### Option 5: Detect pagination signals and warn agent

```python
def check_response_for_pagination(response_data: dict, response_headers: dict = None) -> dict:
    """
    Inspect an API response and detect if pagination was ignored.
    Returns warning info if there are more pages to fetch.
    """
    signals = []

    # Check common pagination fields
    if response_data.get("has_more") is True:
        signals.append(f"has_more=true (next_cursor: {response_data.get('next_cursor', 'present')})")

    if response_data.get("next_cursor"):
        signals.append(f"next_cursor present: {response_data['next_cursor'][:20]}...")

    if response_data.get("next_page_token"):
        signals.append(f"next_page_token present")

    if "pagination" in response_data:
        pg = response_data["pagination"]
        if pg.get("next_page") or pg.get("has_next"):
            signals.append(f"pagination.next_page present")

    # Check Link header
    if response_headers:
        link = response_headers.get("link", "") or response_headers.get("Link", "")
        if "rel=\"next\"" in link:
            signals.append("Link header contains rel='next'")

    if signals:
        return {
            "has_more_pages": True,
            "signals": signals,
            "warning": (
                f"API response indicates more pages exist: {signals}. "
                f"Fetch remaining pages before processing results."
            )
        }

    return {"has_more_pages": False}

# Inject into tool execution:
def tool_response_guard(tool_result: dict, headers: dict = None) -> dict:
    check = check_response_for_pagination(tool_result, headers)
    if check["has_more_pages"]:
        print(f"PAGINATION WARNING: {check['warning']}")
        tool_result["_pagination_warning"] = check["warning"]
    return tool_result
```

### Option 6: System prompt for pagination awareness

```
System prompt:
"Pagination rules:

When a tool returns results from an API:

1. ALWAYS check the response for pagination signals:
   - has_more: true
   - next_cursor or cursor fields
   - next_page_token
   - total > len(results)
   - HTTP Link header with rel='next'

2. If any pagination signal is present, fetch the next page before processing.
   Continue until has_more is false or no next cursor exists.

3. NEVER report 'processed X items' if you've only seen page 1 of multiple pages.

4. If the tool supports a 'cursor' or 'page' parameter, use it to fetch subsequent pages.

5. If you are uncertain whether you have all results, ask the user or check
   the total count field against the count of items received."
```

## Pagination Signal Reference

| Signal | Field / header | Meaning |
|---|---|---|
| `has_more: true` | Response body | More pages exist — fetch next |
| `next_cursor: "abc"` | Response body | Pass as `cursor=abc` in next request |
| `next_page_token` | Response body | Pass as `page_token=` in next request |
| `total: 5000, count: 100` | Response body | 4,900 more items remain |
| `Link: <url>; rel="next"` | HTTP header | Fetch the URL in the header |
| `X-Next-Page: 3` | HTTP header | Request `page=3` next |

## Expected Token Savings
Processing 10% of data → wrong results → rerun with full data: ~40,000 tokens
Auto-pagination fetches all data on first run: 0 wasted

## Environment
- Any agent that calls list/search API endpoints; critical for reporting, migration, and batch processing agents
- Source: direct experience; missing pagination is the most common source of silent data incompleteness in agent pipelines
