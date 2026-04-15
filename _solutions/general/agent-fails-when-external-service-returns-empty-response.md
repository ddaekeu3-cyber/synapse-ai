---
layout: solution
title: "Agent Fails When External Service Returns Empty or Null Response"
category: general
description: "Agent calls an API that returns 200 OK with an empty body, empty array, or null. Agent tries to access .data.items and crashes with AttributeError or KeyError. No graceful handling of legitimate empty results."
tags: [empty-response, null, none, error-handling, defensive, api, graceful]
---

# Agent Fails When External Service Returns Empty or Null Response

## Symptom
- `KeyError: 'data'` when API returns `{}` instead of `{"data": [...]}`
- `AttributeError: 'NoneType' object has no attribute 'items'` on null response
- `IndexError: list index out of range` when accessing `results[0]` on empty array
- Agent crashes on a legitimate "no results found" response
- 200 OK with empty body causes `json.JSONDecodeError` from `response.json()`
- Agent treats "empty result set" as an error instead of a valid state

## Root Cause
Agent code assumes a specific response shape and doesn't handle valid empty/null states. APIs return empty responses legitimately: no search results, no records in date range, new account with no history. Treating these as errors causes unnecessary failures for valid states that should be communicated as "found nothing" rather than crashing.

## Fix

### Option 1: Defensive response unwrapping with defaults

```python
from typing import Any

def safe_get(data: Any, *keys, default=None) -> Any:
    """
    Safely traverse a nested dict/list structure.
    Returns default if any key is missing or value is None.
    """
    current = data
    for key in keys:
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list):
            try:
                current = current[key]
            except (IndexError, TypeError):
                return default
        else:
            return default
    return current if current is not None else default

# FRAGILE — crashes on empty/missing:
def fragile_parse(response: dict) -> list:
    return response["data"]["items"]  # KeyError if missing

# SAFE — returns empty list on any empty/null state:
def safe_parse(response: dict) -> list:
    return safe_get(response, "data", "items", default=[]) or []

# Examples:
safe_parse({})                          # → []
safe_parse({"data": None})              # → []
safe_parse({"data": {}})                # → []
safe_parse({"data": {"items": None}})   # → []
safe_parse({"data": {"items": [1, 2]}}) # → [1, 2]
```

### Option 2: Handle empty HTTP body before JSON parsing

```python
import httpx
import json

async def safe_api_get(url: str, client: httpx.AsyncClient) -> dict | None:
    """
    GET request with full handling of empty/null responses.
    Returns None for genuinely empty responses.
    """
    try:
        response = await client.get(url, timeout=30)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"API error {e.response.status_code}: {e.response.text[:200]}")

    # Handle empty body
    if not response.content:
        print(f"Empty response body from {url}")
        return None

    # Handle non-JSON content type
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        print(f"Unexpected content-type: {content_type}. Body: {response.text[:100]}")
        return None

    # Parse JSON safely
    try:
        data = response.json()
    except json.JSONDecodeError as e:
        print(f"Invalid JSON from {url}: {e}. Body: {response.text[:200]}")
        return None

    return data

async def get_user_orders(user_id: str, client) -> list[dict]:
    """Get orders — returns empty list if user has no orders"""
    data = await safe_api_get(f"/users/{user_id}/orders", client)
    if data is None:
        return []

    # Handle multiple valid empty shapes
    orders = (
        data.get("orders") or
        data.get("data") or
        data.get("items") or
        (data if isinstance(data, list) else [])
    )
    return orders if isinstance(orders, list) else []
```

### Option 3: Distinguish "empty result" from "error"

```python
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

@dataclass
class QueryResult(Generic[T]):
    """
    Structured result that distinguishes:
    - Success with data
    - Success with no data (valid empty)
    - Error (request failed)
    """
    items: list[T]
    total: int | None
    error: str | None
    is_empty: bool

    @classmethod
    def empty(cls) -> "QueryResult":
        return cls(items=[], total=0, error=None, is_empty=True)

    @classmethod
    def error(cls, message: str) -> "QueryResult":
        return cls(items=[], total=None, error=message, is_empty=False)

    @classmethod
    def success(cls, items: list, total: int = None) -> "QueryResult":
        return cls(items=items, total=total or len(items), error=None, is_empty=len(items) == 0)

async def search_products(query: str, client) -> QueryResult:
    try:
        data = await safe_api_get(f"/products/search?q={query}", client)
    except Exception as e:
        return QueryResult.error(str(e))

    if data is None or not data:
        return QueryResult.empty()

    items = data.get("products", [])
    total = data.get("total", len(items))
    return QueryResult.success(items, total)

# Usage — agent handles all states explicitly:
result = await search_products("blue widget", client)

if result.error:
    print(f"Search failed: {result.error}")
elif result.is_empty:
    print("No products found matching your query")
else:
    print(f"Found {result.total} products:")
    for item in result.items:
        print(f"  - {item['name']}")
```

### Option 4: Null-safe chaining for nested access

```python
class Maybe:
    """
    Monad-style wrapper for safe chaining through potentially-null structures.
    """

    def __init__(self, value):
        self._value = value

    def get(self, key, default=None) -> "Maybe":
        if self._value is None:
            return Maybe(default)
        if isinstance(self._value, dict):
            return Maybe(self._value.get(key, default))
        return Maybe(default)

    def index(self, i: int, default=None) -> "Maybe":
        if self._value is None or not isinstance(self._value, (list, tuple)):
            return Maybe(default)
        try:
            return Maybe(self._value[i])
        except IndexError:
            return Maybe(default)

    def unwrap(self, default=None):
        return self._value if self._value is not None else default

    def __bool__(self):
        return self._value is not None and self._value != [] and self._value != {}

# Usage — chain without crashing on None at any level:
response = {"data": None}
items = Maybe(response).get("data").get("items").unwrap(default=[])
# → [] (no crash)

first_item = Maybe(response).get("data").get("items").index(0).get("name").unwrap(default="Unknown")
# → "Unknown" (no crash)
```

### Option 5: System prompt for graceful empty handling

```
System prompt:
"Empty and null response rules:

1. When a tool returns an empty list [], treat it as 'no results found' — not an error.
   Respond to the user: 'No [items] were found matching your criteria.'

2. When a tool returns null or None, check if this means:
   a. The resource doesn't exist → tell the user
   b. The field is optional → proceed with the default value

3. NEVER access list[0] without checking len(list) > 0 first.

4. When results are empty:
   - Do NOT retry the same query expecting different results
   - Do NOT assume the service is broken
   - DO inform the user and offer to search with different criteria

5. Distinguish:
   - 'No results' → valid state, not an error
   - 'Request failed' → actual error, may warrant retry
   - 'Null field' → missing optional data, use default"
```

### Option 6: Response schema validation with graceful fallback

```python
from pydantic import BaseModel, Field, validator
from typing import Optional

class ProductSearchResponse(BaseModel):
    products: list[dict] = Field(default_factory=list)
    total: Optional[int] = None
    page: int = 1
    has_more: bool = False

    @validator("products", pre=True)
    def ensure_list(cls, v):
        """Handle None, missing, or non-list values"""
        if v is None:
            return []
        if not isinstance(v, list):
            return [v] if v else []
        return v

    @validator("total", pre=True)
    def infer_total(cls, v, values):
        if v is None:
            return len(values.get("products", []))
        return v

def parse_search_response(raw: dict | None) -> ProductSearchResponse:
    """
    Parse API response with automatic handling of all empty/null shapes.
    Never raises — always returns a valid ProductSearchResponse.
    """
    if not raw:
        return ProductSearchResponse()

    try:
        return ProductSearchResponse(**raw)
    except Exception as e:
        print(f"Response parse error (using defaults): {e}")
        return ProductSearchResponse()

# All these work without crashing:
parse_search_response(None)
parse_search_response({})
parse_search_response({"products": None})
parse_search_response({"products": [], "total": 0})
parse_search_response({"products": [{"id": 1}], "total": 1})
```

## Empty Response Shapes to Handle

| API returns | Fragile code | Safe handling |
|---|---|---|
| `{}` | `KeyError: 'data'` | `.get("data", [])` or default |
| `null` / `None` | `AttributeError` | Check `if data is not None` |
| `[]` | `IndexError` on `[0]` | `if items: use items[0]` |
| `{"items": null}` | `TypeError` on iteration | `items or []` |
| `200 OK` empty body | `JSONDecodeError` | Check `response.content` first |
| `{"total": 0, "data": []}` | Works fine | The ideal empty response shape |

## Expected Token Savings
Crash on empty result + debugging + fix: ~8,000 tokens
Defensive parsing handles empty as a valid state: 0 wasted

## Environment
- Any agent calling external APIs that may return empty results; especially search, filter, and list endpoints
- Source: direct experience; empty response crashes are one of the most common and easily preventable agent failures
