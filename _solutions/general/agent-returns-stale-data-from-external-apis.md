---
layout: solution
title: "Agent Returns Stale Data from External APIs"
category: general
description: "The agent caches external API responses too aggressively or indefinitely. Users get yesterday's stock prices, outdated inventory counts, or expired user records — without any indication the data is stale."
tags: [caching, stale-data, ttl, cache-invalidation, freshness, external-api, observability]
---

## Symptom

A user asks "Is product P-100 in stock?" The agent confidently says "Yes, 42 units available" — but the cache is 3 hours old and the item sold out at noon. Or the agent uses a stock price from before market open. Or it returns a user's old email address after the user updated their profile. The response looks authoritative but is wrong.

## Root Cause

Cached data has no expiry, no staleness check, and no indication to the LLM or user that the data may be outdated. Once a value is cached, it is returned forever. The agent has no concept of data freshness — it treats a 3-hour-old cache hit identically to a live API response.

## Fix

---

### Option 1: TTL-Based Cache with Freshness Metadata Injection

Add TTL to every cache entry. Include a `_data_freshness` field in tool results so the LLM can communicate staleness to users.

```python
import time
import json
import hashlib
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class CachedValue:
    data: dict
    fetched_at: float
    ttl_seconds: float

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.fetched_at

    @property
    def is_fresh(self) -> bool:
        return self.age_seconds < self.ttl_seconds

    @property
    def freshness_label(self) -> str:
        age = self.age_seconds
        if age < 60:
            return "just now"
        elif age < 300:
            return f"{int(age // 60)} minute(s) ago"
        elif age < 3600:
            return f"{int(age // 60)} minutes ago"
        else:
            return f"{age / 3600:.1f} hours ago"


# Per-tool TTL configuration (seconds)
TOOL_TTLS = {
    "get_stock_price":    15,     # Stock prices: 15 seconds
    "get_inventory":      60,     # Inventory: 1 minute
    "get_user_profile":   300,    # User profile: 5 minutes
    "get_exchange_rate":  3600,   # Exchange rates: 1 hour
    "get_static_config":  86400,  # Config: 24 hours
}

_cache: dict[str, CachedValue] = {}


def cache_key(tool_name: str, args: dict) -> str:
    payload = json.dumps({"tool": tool_name, **args}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def cached_tool_call(tool_name: str, args: dict) -> dict:
    """Execute tool with TTL caching. Annotates result with freshness info."""
    ttl = TOOL_TTLS.get(tool_name, 60)
    key = cache_key(tool_name, args)
    entry = _cache.get(key)

    if entry and entry.is_fresh:
        print(f"  [Cache HIT] {tool_name} — fetched {entry.freshness_label}, TTL {ttl}s")
        return {
            **entry.data,
            "_data_freshness": f"cached {entry.freshness_label}",
            "_cache_ttl_seconds": ttl,
            "_is_live": False,
        }

    # Stale or missing — fetch fresh
    print(f"  [Fetching LIVE] {tool_name}({args})")
    await asyncio.sleep(0.1)  # Simulate API call

    if tool_name == "get_stock_price":
        data = {"symbol": args["symbol"], "price": 182.50, "currency": "USD"}
    elif tool_name == "get_inventory":
        data = {"product_id": args["product_id"], "in_stock": True, "count": 37}
    elif tool_name == "get_user_profile":
        data = {"user_id": args["user_id"], "name": "Alice", "email": "alice@example.com"}
    elif tool_name == "get_exchange_rate":
        data = {"from": args["from_cur"], "to": args["to_cur"], "rate": 1.085}
    else:
        data = {"result": "ok"}

    _cache[key] = CachedValue(data=data, fetched_at=time.monotonic(), ttl_seconds=ttl)
    return {**data, "_data_freshness": "live", "_cache_ttl_seconds": ttl, "_is_live": True}


TOOLS = [
    {"name": "get_stock_price",  "description": "Get current stock price. Data may be up to 15 seconds old.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_inventory",    "description": "Check inventory levels. Data may be up to 1 minute old.",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]}},
    {"name": "get_exchange_rate","description": "Get exchange rate. Data refreshes every hour.",
     "input_schema": {"type": "object", "properties": {"from_cur": {"type": "string"}, "to_cur": {"type": "string"}}, "required": ["from_cur", "to_cur"]}},
]

SYSTEM = """You are a helpful assistant. When reporting data from tools:
- Always mention the data freshness (e.g., "as of 2 minutes ago")
- For time-sensitive data (stock prices, inventory), explicitly note when it was last updated
- If data is cached, say so rather than presenting it as live"""


async def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await cached_tool_call(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


print(asyncio.run(run_agent("What's the stock price for AAPL and is product P-100 in stock?")))
```

**Expected Token Savings**: Freshness metadata adds ~10 tokens per result but prevents users from acting on stale data — avoiding costly downstream errors.
**Environment**: Async Python, in-memory cache. TTLs configurable per tool.

---

### Option 2: Tiered Cache with Stale-While-Revalidate

Serve stale data immediately while refreshing in the background. Zero latency for users, eventual freshness.

```python
import asyncio
import time
import json
import hashlib
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()


@dataclass
class CacheEntry:
    data: dict
    fetched_at: float
    soft_ttl: float   # Serve fresh up to this age
    hard_ttl: float   # Never serve beyond this age (force-refresh)
    refresh_task: asyncio.Task | None = field(default=None, compare=False)

    @property
    def age(self) -> float:
        return time.monotonic() - self.fetched_at

    @property
    def is_soft_stale(self) -> bool:
        return self.age > self.soft_ttl

    @property
    def is_hard_stale(self) -> bool:
        return self.age > self.hard_ttl

    @property
    def staleness_tag(self) -> str:
        if not self.is_soft_stale:
            return "fresh"
        if not self.is_hard_stale:
            return f"revalidating (data is {self.age:.0f}s old)"
        return f"expired ({self.age:.0f}s old)"


_swr_cache: dict[str, CacheEntry] = {}

# Soft TTL: serve as fresh. Hard TTL: block until refreshed.
CACHE_CONFIG = {
    "get_weather":  (30, 300),     # Fresh for 30s, serve up to 5min while refreshing
    "get_rates":    (60, 600),     # Fresh for 1min, serve up to 10min
    "get_inventory": (10, 120),    # Very volatile: 10s fresh, 2min hard limit
}


async def _fetch_live(tool_name: str, args: dict) -> dict:
    """Simulate live API fetch."""
    await asyncio.sleep(0.15)
    if tool_name == "get_weather":
        return {"city": args["city"], "temp": "19°C", "condition": "partly cloudy", "ts": time.time()}
    if tool_name == "get_rates":
        return {"pair": f"{args['from']}/{args['to']}", "rate": 1.087, "ts": time.time()}
    if tool_name == "get_inventory":
        return {"sku": args["sku"], "count": 14, "ts": time.time()}
    return {}


async def swr_get(tool_name: str, args: dict) -> dict:
    """Stale-while-revalidate cache lookup."""
    soft_ttl, hard_ttl = CACHE_CONFIG.get(tool_name, (60, 600))
    key = f"{tool_name}:{hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:12]}"
    entry = _swr_cache.get(key)

    if entry is None or entry.is_hard_stale:
        # No cache or hard expired — must fetch synchronously
        print(f"  [SYNC FETCH] {tool_name} (no cache or hard-expired)")
        data = await _fetch_live(tool_name, args)
        _swr_cache[key] = CacheEntry(data=data, fetched_at=time.monotonic(), soft_ttl=soft_ttl, hard_ttl=hard_ttl)
        tag = "fresh"
    elif entry.is_soft_stale:
        # Soft stale — serve immediately, refresh in background
        tag = entry.staleness_tag
        print(f"  [SERVE STALE + BG REFRESH] {tool_name} — {tag}")

        async def _bg_refresh():
            fresh = await _fetch_live(tool_name, args)
            _swr_cache[key] = CacheEntry(data=fresh, fetched_at=time.monotonic(), soft_ttl=soft_ttl, hard_ttl=hard_ttl)
            print(f"  [BG refresh done] {tool_name}")

        if not entry.refresh_task or entry.refresh_task.done():
            entry.refresh_task = asyncio.create_task(_bg_refresh())
    else:
        tag = "fresh"
        print(f"  [CACHE HIT] {tool_name} — age {entry.age:.0f}s")

    return {**_swr_cache[key].data, "_freshness": tag}


TOOLS = [
    {"name": "get_weather",   "description": "Get weather. May serve slightly stale data.",
     "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "get_inventory", "description": "Check inventory.",
     "input_schema": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]}},
]


async def run_swr_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await swr_get(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


print(asyncio.run(run_swr_agent("What's the weather in Berlin and how many units of SKU-500 are left?")))
```

**Expected Token Savings**: Background refresh eliminates user-visible latency for cache misses. Hard TTL prevents serving data older than 2× soft TTL.
**Environment**: Async Python with background tasks. Works within a single event loop.

---

### Option 3: Explicit Cache-Busting for High-Stakes Queries

Detect user intent signals that require live data. Bypass cache on "right now", "current", "latest" queries.

```python
import re
import time
import json
import hashlib
import anthropic

client = anthropic.Anthropic()

# Phrases that signal the user wants truly live data
FRESHNESS_REQUIRED_PATTERNS = [
    r"\b(right now|this second|immediately|urgent|asap)\b",
    r"\b(current|live|real-?time|up.?to.?date|latest|today)\b",
    r"\b(just happened|just changed|recently updated)\b",
    r"\b(before I buy|before I order|before I decide)\b",
]

_cache: dict[str, tuple[dict, float]] = {}
DEFAULT_TTL = 120


def requires_fresh_data(user_message: str) -> bool:
    msg_lower = user_message.lower()
    return any(re.search(p, msg_lower, re.IGNORECASE) for p in FRESHNESS_REQUIRED_PATTERNS)


def get_cached(tool_name: str, args: dict, bypass: bool = False) -> dict | None:
    if bypass:
        return None
    key = f"{tool_name}:{hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:12]}"
    entry = _cache.get(key)
    if entry:
        data, ts = entry
        if time.time() - ts < DEFAULT_TTL:
            return data
    return None


def set_cached(tool_name: str, args: dict, data: dict):
    key = f"{tool_name}:{hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:12]}"
    _cache[key] = (data, time.time())


def execute_tool(tool_name: str, args: dict, bypass_cache: bool = False) -> dict:
    cached = get_cached(tool_name, args, bypass=bypass_cache)
    if cached:
        print(f"  [Cache] {tool_name} — using cached ({DEFAULT_TTL}s TTL)")
        return {**cached, "_freshness": "cached"}

    print(f"  [Live] {tool_name}({'CACHE BYPASSED' if bypass_cache else 'cache miss'})")
    if tool_name == "get_price":
        result = {"item": args["item"], "price": 299.99, "in_stock": True, "ts": time.time()}
    elif tool_name == "get_order_status":
        result = {"order_id": args["order_id"], "status": "shipped", "eta": "2025-04-17"}
    else:
        result = {"status": "ok"}

    set_cached(tool_name, args, result)
    return {**result, "_freshness": "live"}


TOOLS = [
    {"name": "get_price", "description": "Get product price and availability.",
     "input_schema": {"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]}},
    {"name": "get_order_status", "description": "Get order status.",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}},
]


def run_agent(user_message: str) -> str:
    bypass = requires_freshness_required = requires_fresh_data(user_message)
    if bypass:
        print(f"  [Freshness required] Bypassing cache for this query")

    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input, bypass_cache=bypass)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


# Cache works normally
print(run_agent("What's the price for the Pro Widget?"))
print(run_agent("What's the price for the Pro Widget?"))  # cache hit

# High-stakes: bypass cache
print(run_agent("What's the current live price for the Pro Widget right now? I'm about to buy."))
```

**Expected Token Savings**: Cache hit rate stays high for casual queries. Freshness bypass used only for the ~10–15% of queries that genuinely need live data.
**Environment**: Synchronous Python. Regex patterns are editable to match your domain.

---

### Option 4: Event-Driven Cache Invalidation

Invalidate cache entries when the underlying data changes, rather than relying on TTL expiry.

```python
import time
import json
import hashlib
import asyncio
import anthropic
from collections import defaultdict

client = anthropic.AsyncAnthropic()

# Cache stores: key → (data, timestamp)
_cache: dict[str, tuple[dict, float]] = {}
# Tag → set of cache keys (for tag-based invalidation)
_tag_index: dict[str, set[str]] = defaultdict(set)


def cache_key(tool_name: str, args: dict) -> str:
    return hashlib.sha256(json.dumps({"t": tool_name, **args}, sort_keys=True).encode()).hexdigest()[:16]


def cache_set(tool_name: str, args: dict, data: dict, tags: list[str] = None):
    key = cache_key(tool_name, args)
    _cache[key] = (data, time.monotonic())
    for tag in (tags or []):
        _tag_index[tag].add(key)


def cache_get(tool_name: str, args: dict) -> dict | None:
    key = cache_key(tool_name, args)
    entry = _cache.get(key)
    return entry[0] if entry else None


def cache_invalidate_tag(tag: str):
    """Invalidate all cache entries associated with a tag."""
    keys = _tag_index.pop(tag, set())
    for key in keys:
        _cache.pop(key, None)
    if keys:
        print(f"  [Cache invalidated] tag='{tag}' → {len(keys)} entries cleared")


def cache_invalidate_tool(tool_name: str):
    """Invalidate all entries for a specific tool."""
    to_delete = [k for k in _cache if k.startswith(f"{tool_name}:")]
    for k in to_delete:
        del _cache[k]
    print(f"  [Cache invalidated] tool='{tool_name}' → {len(to_delete)} entries cleared")


async def fetch_live(tool_name: str, args: dict) -> dict:
    await asyncio.sleep(0.1)
    if tool_name == "get_product":
        return {"product_id": args["product_id"], "name": "Widget Pro", "price": 49.99, "stock": 23}
    if tool_name == "get_user":
        return {"user_id": args["user_id"], "name": "Bob", "email": "bob@example.com"}
    return {}


async def tagged_tool_call(tool_name: str, args: dict) -> dict:
    cached = cache_get(tool_name, args)
    if cached:
        print(f"  [Cache HIT] {tool_name}")
        return {**cached, "_source": "cache"}

    data = await fetch_live(tool_name, args)

    # Tag entries for selective invalidation
    tags = []
    if tool_name == "get_product" and "product_id" in args:
        tags = [f"product:{args['product_id']}", "all_products"]
    elif tool_name == "get_user" and "user_id" in args:
        tags = [f"user:{args['user_id']}"]

    cache_set(tool_name, args, data, tags=tags)
    return {**data, "_source": "live"}


# Simulate an event stream that triggers invalidation
async def simulate_product_update(product_id: str):
    """Called when product data changes in the source system."""
    print(f"\n  [EVENT] Product {product_id} updated — invalidating cache")
    cache_invalidate_tag(f"product:{product_id}")


async def main():
    TOOLS = [
        {"name": "get_product", "description": "Get product details.",
         "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]}},
    ]

    async def agent_query(msg: str) -> str:
        messages = [{"role": "user", "content": msg}]
        while True:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                tools=TOOLS,
                messages=messages,
            )
            if response.stop_reason == "end_turn":
                return next(b.text for b in response.content if b.type == "text")
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await tagged_tool_call(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
            messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]

    print(await agent_query("Tell me about product P-500."))
    print(await agent_query("Tell me about product P-500."))  # cache hit

    await simulate_product_update("P-500")  # Invalidate on product update event

    print(await agent_query("Tell me about product P-500."))  # Cache miss — fetches fresh


asyncio.run(main())
```

**Expected Token Savings**: Event-driven invalidation means cache is always fresh after changes — no need for short TTLs. Longer TTLs possible, more cache hits overall.
**Environment**: Event-driven. Wire `cache_invalidate_tag()` to your webhook handlers, CDC stream, or message queue consumers.

---

### Option 5: Data Versioning — Include ETag/Version in Cache Keys

Use ETags or version tokens to detect stale data without TTL.

```python
import asyncio
import json
import hashlib
import anthropic

client = anthropic.AsyncAnthropic()

# Version store: entity → current_version (updated by source system)
_entity_versions: dict[str, str] = {
    "product:P-100": "v3",
    "user:U-42":     "v1",
    "config:main":   "v12",
}

# Cache: (key, version) → data
_versioned_cache: dict[tuple[str, str], dict] = {}


def get_current_version(entity: str) -> str:
    return _entity_versions.get(entity, "v0")


def bump_version(entity: str):
    """Called when entity is updated."""
    parts = _entity_versions.get(entity, "v0").split("v")
    new_v = int(parts[1]) + 1 if len(parts) == 2 else 1
    _entity_versions[entity] = f"v{new_v}"
    print(f"  [Version bumped] {entity}: {_entity_versions[entity]}")


async def versioned_fetch(entity_type: str, entity_id: str) -> dict:
    entity = f"{entity_type}:{entity_id}"
    version = get_current_version(entity)
    cache_entry_key = (entity, version)

    if cache_entry_key in _versioned_cache:
        print(f"  [Versioned cache HIT] {entity} @ {version}")
        return {**_versioned_cache[cache_entry_key], "_version": version, "_source": "cache"}

    print(f"  [Versioned cache MISS] {entity} @ {version} — fetching live")
    await asyncio.sleep(0.1)

    if entity_type == "product":
        data = {"id": entity_id, "name": "Pro Widget", "price": 79.99, "stock": 31}
    elif entity_type == "user":
        data = {"id": entity_id, "name": "Carol", "email": "carol@example.com", "tier": "gold"}
    else:
        data = {"id": entity_id, "type": entity_type}

    _versioned_cache[cache_entry_key] = data
    return {**data, "_version": version, "_source": "live"}


TOOLS = [
    {"name": "get_product", "description": "Get product by ID.",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]}},
    {"name": "get_user", "description": "Get user profile.",
     "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
]


async def run_versioned_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "get_product":
                    data = await versioned_fetch("product", block.input["product_id"])
                elif block.name == "get_user":
                    data = await versioned_fetch("user", block.input["user_id"])
                else:
                    data = {}
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(data)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


async def main():
    print(await run_versioned_agent("Tell me about product P-100."))  # cache miss → live
    print(await run_versioned_agent("Tell me about product P-100."))  # cache hit @ v3

    bump_version("product:P-100")  # Simulate price change

    print(await run_versioned_agent("Tell me about product P-100."))  # cache miss @ v4 → live


asyncio.run(main())
```

**Expected Token Savings**: Version-based cache has 100% hit rate until the entity version changes — no unnecessary TTL-based expirations. Eliminates stale data without short TTLs.
**Environment**: Version store updated by source system (DB triggers, CDC, webhooks). Works with any versioned entity.

---

### Option 6: Freshness Declaration in System Prompt + Per-Field Timestamps

Inject data age into tool results field-by-field. LLM uses timestamps to qualify its answers.

```python
import time
import json
import asyncio
import anthropic
from datetime import datetime, timezone

client = anthropic.AsyncAnthropic()


def stamp(data: dict, fetched_at: float | None = None) -> dict:
    """Add per-record timestamp to tool result."""
    ts = fetched_at or time.time()
    age = time.time() - ts
    return {
        **data,
        "_fetched_at": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_age_seconds": round(age),
        "_age_human": (
            "just now" if age < 5 else
            f"{int(age)}s ago" if age < 60 else
            f"{int(age // 60)}m ago" if age < 3600 else
            f"{age / 3600:.1f}h ago"
        ),
    }


# Simulate a cache with timestamps
_ts_cache: dict[str, tuple[dict, float]] = {}


async def timestamped_fetch(tool_name: str, args: dict, ttl: float = 60) -> dict:
    key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
    if key in _ts_cache:
        cached_data, ts = _ts_cache[key]
        age = time.time() - ts
        if age < ttl:
            return stamp(cached_data, ts)

    await asyncio.sleep(0.1)
    if tool_name == "market_data":
        data = {"symbol": args["symbol"], "price": 155.40, "volume": 1_200_000}
    elif tool_name == "news_headlines":
        data = {"topic": args["topic"], "headlines": ["Headline A", "Headline B"]}
    else:
        data = {"result": "ok"}

    _ts_cache[key] = (data, time.time())
    return stamp(data)


TOOLS = [
    {"name": "market_data",    "description": "Get market data.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "news_headlines", "description": "Get recent news.",
     "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
]

SYSTEM = """You are a financial assistant. All data from tools includes a _fetched_at timestamp and _age_human field.
Always qualify your responses with data freshness:
- For market data: mention exact timestamp if under 1 minute old, "approximately X minutes ago" otherwise
- For news: note when headlines were last refreshed
- Never present data as "current" without noting its age"""


async def run_timestamped_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                data = await timestamped_fetch(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(data)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


print(asyncio.run(run_timestamped_agent("What's the TSLA price and any recent tech news?")))
```

**Expected Token Savings**: Timestamps add ~15 tokens per result. System prompt adds ~80 tokens. Both prevent expensive corrections from users acting on stale data.
**Environment**: Per-field timestamps work with any data source. System prompt instructs the LLM to communicate freshness naturally.

---

| Option | Cache Strategy | Invalidation | Latency Impact | Best For |
|--------|--------------|-------------|---------------|---------|
| 1 | TTL + freshness metadata | Time-based | None | General caching with transparency |
| 2 | Stale-while-revalidate | Time-based + async refresh | ~0ms | Low-latency, eventual freshness |
| 3 | Cache-busting on intent signals | User-triggered | None | High-stakes user queries |
| 4 | Event-driven tag invalidation | Event-based | None | Systems with update webhooks/CDC |
| 5 | Version-based key invalidation | Version bump | None | Versioned entities |
| 6 | Per-field timestamps | Time-based | None | Financial/time-sensitive domains |
