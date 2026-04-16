---
title: "Agent Doesn't Implement Optimistic Locking for Concurrent Updates"
description: "Agents that read-modify-write shared state without version checking silently overwrite concurrent changes, causing lost updates and data corruption. Implement optimistic locking with version tokens or ETags to detect and resolve write conflicts."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-optimistic-locking-for-concurrent-updates
tags: [optimistic-locking, concurrency, version-control, etag, conflict-detection, reliability]
symptoms:
  - "Last-write-wins overwrites concurrent agent state changes silently"
  - "Two agents reading the same record both write their updates, second wins"
  - "No ConflictError raised when two sessions update the same document"
  - "User B's changes disappear after User A saves the same record"
  - "Retry logic on tool calls causes double-increments on counters"
---

## Why This Happens

Agents often follow a read-modify-write pattern: fetch a document, apply a transformation, save it back. When two agents (or two retries of the same agent) execute this sequence concurrently, the second write overwrites the first without knowing it happened. Optimistic locking adds a version field; the write only succeeds if the version matches what was read. If it doesn't match, someone else has written since the read and the caller must retry.

## Solution 1: Version-Based Optimistic Lock on DB Records

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Optional
import uuid

class OptimisticLockError(Exception):
    def __init__(self, record_id: str, expected_version: int, actual_version: int):
        super().__init__(
            f"Optimistic lock conflict on '{record_id}': "
            f"expected version {expected_version}, found {actual_version}"
        )
        self.record_id = record_id
        self.expected_version = expected_version
        self.actual_version = actual_version

@dataclass
class VersionedRecord:
    id: str
    data: dict
    version: int

class OptimisticLockRepository:
    """
    All writes include the version the caller last read.
    If the DB version has advanced, the update is rejected.
    Caller must re-read, re-apply changes, and retry.
    """

    def __init__(self, db):
        self._db = db

    async def get(self, record_id: str) -> Optional[VersionedRecord]:
        row = await self._db.fetchrow(
            "SELECT id, data, version FROM records WHERE id = $1", record_id
        )
        if row is None:
            return None
        return VersionedRecord(id=row["id"], data=row["data"], version=row["version"])

    async def update(self, record: VersionedRecord) -> VersionedRecord:
        """
        Attempts to update record only if version matches.
        Raises OptimisticLockError on conflict.
        Returns updated record with incremented version.
        """
        result = await self._db.fetchrow(
            """
            UPDATE records
            SET data = $1, version = version + 1, updated_at = NOW()
            WHERE id = $2 AND version = $3
            RETURNING version
            """,
            record.data, record.id, record.version,
        )
        if result is None:
            current = await self.get(record.id)
            actual_version = current.version if current else -1
            raise OptimisticLockError(record.id, record.version, actual_version)
        return VersionedRecord(id=record.id, data=record.data, version=result["version"])

    async def create(self, data: dict, record_id: Optional[str] = None) -> VersionedRecord:
        rid = record_id or str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO records (id, data, version) VALUES ($1, $2, 1)", rid, data
        )
        return VersionedRecord(id=rid, data=data, version=1)

    async def update_with_retry(
        self,
        record_id: str,
        transform,
        max_retries: int = 5,
    ) -> VersionedRecord:
        """
        Fetch → transform → update with automatic retry on conflict.
        `transform` is a function(data: dict) -> dict.
        """
        for attempt in range(max_retries):
            record = await self.get(record_id)
            if record is None:
                raise ValueError(f"Record '{record_id}' not found")
            record.data = transform(record.data)
            try:
                return await self.update(record)
            except OptimisticLockError:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(0.05 * (2 ** attempt))
        raise RuntimeError("unreachable")
```

## Solution 2: ETag-Based Optimistic Lock for HTTP Tool Responses

```python
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

@dataclass
class ETaggedResource:
    resource_id: str
    data: Any
    etag: str

    @classmethod
    def from_data(cls, resource_id: str, data: Any) -> "ETaggedResource":
        body = json.dumps(data, sort_keys=True, default=str).encode()
        etag = hashlib.sha256(body).hexdigest()[:16]
        return cls(resource_id=resource_id, data=data, etag=etag)

class ETagOptimisticLock:
    """
    For agents calling REST/HTTP tools: store the ETag from GET responses
    and include If-Match on subsequent PUT/PATCH calls.
    Raises ConflictError when the server returns 412 Precondition Failed.
    """

    def __init__(self, http_client):
        self._client = http_client
        self._etag_cache: Dict[str, str] = {}

    async def get(self, url: str) -> ETaggedResource:
        response = await self._client.get(url)
        response.raise_for_status()
        etag = response.headers.get("ETag", "").strip('"')
        data = response.json()
        self._etag_cache[url] = etag
        return ETaggedResource(resource_id=url, data=data, etag=etag)

    async def put(self, url: str, data: Any, etag: Optional[str] = None) -> ETaggedResource:
        """Use the stored ETag for conditional update."""
        stored_etag = etag or self._etag_cache.get(url)
        headers = {}
        if stored_etag:
            headers["If-Match"] = f'"{stored_etag}"'

        response = await self._client.put(url, json=data, headers=headers)
        if response.status_code == 412:
            raise OptimisticLockError(url, expected_version=0, actual_version=1)
        response.raise_for_status()
        new_etag = response.headers.get("ETag", "").strip('"')
        self._etag_cache[url] = new_etag
        return ETaggedResource(resource_id=url, data=data, etag=new_etag)

    async def patch_with_retry(
        self, url: str, patch_fn, max_retries: int = 3
    ) -> ETaggedResource:
        for attempt in range(max_retries):
            resource = await self.get(url)
            patched_data = patch_fn(resource.data)
            try:
                return await self.put(url, patched_data, etag=resource.etag)
            except OptimisticLockError:
                if attempt == max_retries - 1:
                    raise
        raise RuntimeError("unreachable")
```

## Solution 3: In-Memory Optimistic Lock for Agent State

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

@dataclass
class StateEntry:
    value: Any
    version: int
    last_modified: float = field(default_factory=time.monotonic)

class OptimisticStateStore:
    """
    In-process optimistic lock store for agent shared state.
    Suitable for multi-coroutine agents in a single process.
    """

    def __init__(self):
        self._store: Dict[str, StateEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[tuple]:
        """Returns (value, version) or None."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            return entry.value, entry.version

    async def set(self, key: str, value: Any, expected_version: int) -> int:
        """
        Sets key=value only if current version == expected_version.
        Returns new version on success.
        Raises OptimisticLockError on version mismatch.
        """
        async with self._lock:
            entry = self._store.get(key)
            current_version = entry.version if entry else 0
            if current_version != expected_version:
                raise OptimisticLockError(key, expected_version, current_version)
            new_version = current_version + 1
            self._store[key] = StateEntry(value=value, version=new_version)
            return new_version

    async def init(self, key: str, value: Any) -> int:
        """Creates key only if it doesn't exist. Returns version 1."""
        async with self._lock:
            if key in self._store:
                raise ValueError(f"Key '{key}' already exists")
            self._store[key] = StateEntry(value=value, version=1)
            return 1

    async def atomic_update(self, key: str, transform, max_retries: int = 10) -> Any:
        """Retry-loop: read current value, apply transform, CAS write."""
        for attempt in range(max_retries):
            result = await self.get(key)
            if result is None:
                raise KeyError(f"Key '{key}' not found")
            current_value, version = result
            new_value = transform(current_value)
            try:
                await self.set(key, new_value, expected_version=version)
                return new_value
            except OptimisticLockError:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(0)  # yield to other coroutines
        raise RuntimeError("unreachable")
```

## Solution 4: Conflict Resolver for Automatic Merge

```python
from typing import Any, Callable, Dict, Optional

MergeFn = Callable[[Any, Any, Any], Any]  # (base, local, remote) -> merged

def last_write_wins(base: Any, local: Any, remote: Any) -> Any:
    return remote

def merge_dicts(base: dict, local: dict, remote: dict) -> dict:
    """
    Three-way merge for dicts: apply both local and remote changes
    relative to base. Remote wins on field-level conflicts.
    """
    result = dict(base)
    for key in set(local) | set(remote):
        in_local = key in local
        in_remote = key in remote
        local_changed = in_local and local.get(key) != base.get(key)
        remote_changed = in_remote and remote.get(key) != base.get(key)

        if local_changed and not remote_changed:
            result[key] = local[key]
        elif remote_changed:
            result[key] = remote[key]  # remote wins
        elif not in_local and not in_remote:
            result.pop(key, None)
    return result

class ConflictResolvingRepository:
    """
    Wraps OptimisticLockRepository with a conflict resolution strategy.
    On OptimisticLockError, fetches the remote version and merges.
    """

    def __init__(self, repo: OptimisticLockRepository, merge_fn: MergeFn = merge_dicts):
        self._repo = repo
        self._merge = merge_fn

    async def update_with_merge(
        self,
        record_id: str,
        local_changes: dict,
        base_version: int,
        max_retries: int = 5,
    ) -> VersionedRecord:
        base_record = VersionedRecord(id=record_id, data={}, version=base_version)
        local_data = local_changes

        for attempt in range(max_retries):
            try:
                candidate = VersionedRecord(id=record_id, data=local_data, version=base_record.version)
                return await self._repo.update(candidate)
            except OptimisticLockError:
                remote = await self._repo.get(record_id)
                if remote is None:
                    raise
                # Three-way merge: base (before both edits) + local + remote
                merged = self._merge(base_record.data, local_data, remote.data)
                base_record = remote
                local_data = merged
                if attempt == max_retries - 1:
                    raise
        raise RuntimeError("unreachable")
```

## Solution 5: Distributed Optimistic Lock with Redis

```python
import asyncio
import time
import uuid
from typing import Any, Optional

class RedisOptimisticLock:
    """
    Redis-backed optimistic lock using WATCH/MULTI/EXEC pattern.
    Suitable for distributed agents across multiple processes.
    """

    def __init__(self, redis_client):
        self._redis = redis_client

    async def get_versioned(self, key: str) -> Optional[tuple]:
        """Returns (value, version) from Redis hash."""
        data = await self._redis.hgetall(key)
        if not data:
            return None
        return data.get(b"value"), int(data.get(b"version", 0))

    async def compare_and_set(
        self, key: str, value: Any, expected_version: int, ttl_seconds: int = 3600
    ) -> bool:
        """
        Lua-script CAS: atomic check-version + set.
        Returns True on success, False on version mismatch.
        """
        lua_script = """
        local current = redis.call('HGET', KEYS[1], 'version')
        if current == false then current = '0' end
        if tonumber(current) ~= tonumber(ARGV[1]) then
            return 0
        end
        redis.call('HMSET', KEYS[1], 'value', ARGV[2], 'version', tostring(tonumber(ARGV[1]) + 1))
        redis.call('EXPIRE', KEYS[1], ARGV[3])
        return 1
        """
        result = await self._redis.eval(
            lua_script, 1, key,
            str(expected_version), str(value), str(ttl_seconds)
        )
        return bool(result)

    async def atomic_update(
        self, key: str, transform, max_retries: int = 10, ttl_seconds: int = 3600
    ) -> Any:
        for attempt in range(max_retries):
            result = await self.get_versioned(key)
            if result is None:
                raise KeyError(f"Key '{key}' not found in Redis")
            current_value, version = result
            new_value = transform(current_value)
            success = await self.compare_and_set(key, new_value, version, ttl_seconds)
            if success:
                return new_value
            if attempt < max_retries - 1:
                await asyncio.sleep(0.01 * (2 ** attempt))
        raise OptimisticLockError(key, expected_version=version, actual_version=-1)
```

## Solution 6: Agent Tool Wrapper with Automatic Conflict Retry

```python
import asyncio
from functools import wraps
from typing import Callable, Optional

def with_optimistic_retry(
    repo_attr: str = "_repo",
    max_retries: int = 5,
    base_delay: float = 0.05,
):
    """
    Decorator for agent tool methods that do read-modify-write.
    Automatically retries on OptimisticLockError with exponential backoff.
    """
    def decorator(fn: Callable):
        @wraps(fn)
        async def wrapper(self, *args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await fn(self, *args, **kwargs)
                except OptimisticLockError as exc:
                    if attempt == max_retries - 1:
                        raise RuntimeError(
                            f"Gave up on optimistic retry after {max_retries} attempts: {exc}"
                        ) from exc
                    delay = base_delay * (2 ** attempt)
                    print(f"[optimistic_retry] conflict on attempt {attempt+1}, retrying in {delay:.2f}s")
                    await asyncio.sleep(delay)
        return wrapper
    return decorator

class AgentStateTool:
    def __init__(self, repo: OptimisticLockRepository):
        self._repo = repo

    @with_optimistic_retry(max_retries=5)
    async def increment_counter(self, session_id: str, field: str) -> int:
        record = await self._repo.get(session_id)
        if record is None:
            record = await self._repo.create(data={field: 0}, record_id=session_id)
        record.data[field] = record.data.get(field, 0) + 1
        updated = await self._repo.update(record)
        return updated.data[field]

    @with_optimistic_retry(max_retries=3)
    async def append_to_list(self, session_id: str, list_key: str, item: Any) -> list:
        record = await self._repo.get(session_id)
        if record is None:
            record = await self._repo.create(data={list_key: []}, record_id=session_id)
        record.data.setdefault(list_key, []).append(item)
        updated = await self._repo.update(record)
        return updated.data[list_key]
```

## Comparison

| Approach | Backend | Conflict Strategy | Distributed | Auto-Retry |
|---|---|---|---|---|
| OptimisticLockRepository | PostgreSQL | Version column + CAS | No (single DB) | Manual / `update_with_retry` |
| ETagOptimisticLock | REST/HTTP | If-Match header | Yes (any HTTP API) | `patch_with_retry` |
| OptimisticStateStore | In-memory | asyncio CAS | No (single process) | `atomic_update` |
| ConflictResolvingRepository | PostgreSQL | Three-way merge | No | Built-in with merge |
| RedisOptimisticLock | Redis | Lua CAS script | Yes | `atomic_update` |
| `with_optimistic_retry` decorator | Any | Decorator retry | Any | Automatic |

**Best for production**: Use `OptimisticLockRepository` with a `version` column for DB-backed state, `RedisOptimisticLock` for distributed cross-process state, and `with_optimistic_retry` as a decorator on all agent tools that do read-modify-write. Apply `ConflictResolvingRepository` when merge semantics are preferable to hard conflict errors.
