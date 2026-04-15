---
layout: solution
title: "Agent Reads Stale Cached Data and Ignores Recent Updates"
category: memory
description: "Agent caches tool results or external data at session start and never refreshes. User updates a file or database record; agent keeps using the old version from its cache."
tags: [cache, stale-data, memory, refresh, tool-results]
---

# Agent Reads Stale Cached Data and Ignores Recent Updates

## Symptom
- Agent reads a file at session start; user edits the file; agent still uses old content
- Database record updated during session; agent references old value
- Agent says "the config is X" but config was changed 10 minutes ago
- Re-running a query returns different results than what agent reported earlier
- Agent's knowledge of system state drifts from actual state over session lifetime

## Root Cause
Agents implicitly cache tool results in their context window. Once a file or API result appears in conversation history, the model uses that cached version rather than re-fetching. There is no automatic cache invalidation — the agent doesn't know external state has changed.

## Fix

### Option 1: Always re-fetch on explicit user signals

```
System prompt:
"Data freshness rules:
- Never assume cached data is still valid
- Re-read files before modifying them, even if you read them recently
- Re-query databases before making decisions based on their state
- If the user says 'I updated X' or 'I changed Y', immediately re-fetch X or Y
- Treat all tool results as having a 5-minute TTL — re-fetch if older than that"
```

### Option 2: Timestamp tool results in context

```python
from datetime import datetime

async def read_file_with_timestamp(path: str) -> str:
    """Wrap file reads with timestamp so agent knows when data was fetched"""
    content = open(path).read()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"[Read at {timestamp}]\n{content}\n[End of file: {path}]"

async def query_db_with_timestamp(query: str) -> str:
    """Wrap DB queries with timestamp"""
    result = await db.execute(query)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"[Query at {timestamp}]\n{result}\n[End of query result]"
```

The timestamp in context signals the model when data was fetched, helping it decide whether to re-fetch.

### Option 3: Explicit cache invalidation tracking

```python
from dataclasses import dataclass, field
from datetime import datetime, timedelta

@dataclass
class CachedResult:
    data: str
    fetched_at: datetime
    ttl_seconds: int = 300  # 5 minutes

    def is_stale(self) -> bool:
        return datetime.now() > self.fetched_at + timedelta(seconds=self.ttl_seconds)

class AgentCache:
    def __init__(self):
        self._cache: dict[str, CachedResult] = {}

    def get(self, key: str) -> str | None:
        cached = self._cache.get(key)
        if cached is None or cached.is_stale():
            return None  # Force re-fetch
        return cached.data

    def set(self, key: str, data: str, ttl: int = 300):
        self._cache[key] = CachedResult(data=data, fetched_at=datetime.now(), ttl_seconds=ttl)

    def invalidate(self, key: str):
        self._cache.pop(key, None)

    def invalidate_pattern(self, prefix: str):
        keys_to_remove = [k for k in self._cache if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._cache[k]

cache = AgentCache()

async def get_file_content(path: str) -> str:
    cached = cache.get(f"file:{path}")
    if cached:
        return cached
    content = open(path).read()
    cache.set(f"file:{path}", content, ttl=60)  # Files stale after 1 minute
    return content
```

### Option 4: Re-fetch before write operations

```python
async def safe_file_edit(path: str, edit_fn, agent) -> str:
    """Always re-read before editing to avoid overwriting concurrent changes"""
    # Re-fetch current content (ignore any cached version)
    current_content = open(path).read()
    fetch_time = datetime.now()

    # Let agent see the fresh content
    fresh_context = f"Current content of {path} (fetched at {fetch_time}):\n{current_content}"

    # Generate edit based on fresh content
    new_content = await agent.complete(
        f"{fresh_context}\n\nApply this change: {edit_fn}"
    )

    # Write back
    open(path, 'w').write(new_content)
    return new_content
```

### Option 5: State version tracking for databases

```python
async def get_record_with_version(record_id: str) -> dict:
    """Always fetch with version/updated_at to detect staleness"""
    record = await db.get(record_id)
    return {
        "data": record,
        "version": record.get("version") or record.get("updated_at"),
        "fetched_at": datetime.now().isoformat()
    }

async def update_record_with_check(record_id: str, updates: dict, expected_version) -> bool:
    """Optimistic update — fails if record was modified since we read it"""
    current = await db.get(record_id)
    current_version = current.get("version") or current.get("updated_at")

    if current_version != expected_version:
        # Data changed since we read it — re-fetch and retry
        return False

    await db.update(record_id, {**updates, "version": current_version + 1})
    return True
```

## When to Re-Fetch vs. Use Cache

| Data type | Cache TTL | Re-fetch when |
|---|---|---|
| Config files | 60s | User says "I updated config" |
| Database records | 30s | Any write operation nearby |
| API responses | Per `Cache-Control` header | After TTL or on mutation |
| User-provided files | Never cache | Always re-read before edit |
| External prices/rates | 5–60s | High-stakes decisions |
| Auth tokens | Until expiry | On 401 response |
| System state (disk, processes) | 10s | Before resource operations |

## Expected Token Savings
Debugging stale data bugs: ~8,000 tokens
Cache invalidation policy prevents it: 0 wasted

## Environment
- Long-running agents interacting with mutable external state
- Source: direct experience with file-editing and database-querying agents
