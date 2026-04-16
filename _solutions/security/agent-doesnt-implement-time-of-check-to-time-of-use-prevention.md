---
title: "Agent Doesn't Implement Time-of-Check to Time-of-Use Prevention"
description: "AI agents validate permissions, resource existence, or state at one point in time, then use the resource later — leaving a TOCTOU window where an attacker or race condition can substitute a different resource."
category: security
difficulty: advanced
tags: [toctou, race-condition, security, atomicity, locking, file-security, permissions]
---

# Agent Doesn't Implement Time-of-Check to Time-of-Use Prevention

## Problem

TOCTOU (Time-of-Check to Time-of-Use) vulnerabilities occur when an agent checks a condition (e.g., file exists, user has permission, token is valid) and then separately acts on it — with a race window in between. In AI agents this surfaces as: checking that a file path is safe, then reading it (symlink substitution possible); validating a tool argument, then executing it (argument mutation possible); or checking API quota before a batch, then submitting (quota may be exhausted by concurrent requests).

## Solution 1: Atomic File Operations — Open Then Validate, Not Validate Then Open

Never check-then-open. Open the file first, then validate the opened file descriptor's metadata.

```python
import os
import stat
import asyncio
from pathlib import Path

ALLOWED_BASE = Path("/safe/data")
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

async def safe_read_file(user_path: str) -> bytes:
    """Open file atomically, then validate — no TOCTOU window."""
    try:
        path = Path(user_path).resolve()
    except Exception:
        raise PermissionError(f"Invalid path: {user_path}")

    # Open the file first (this is the atomic operation)
    loop = asyncio.get_event_loop()
    fd = await loop.run_in_executor(None, lambda: os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW))
    # O_NOFOLLOW: fail if the path is a symlink — prevents symlink substitution attacks

    try:
        # Now validate the fd's metadata (not the path — the path may have changed)
        file_stat = os.fstat(fd)

        # Check 1: must be a regular file (not symlink, device, pipe)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PermissionError(f"Not a regular file: {user_path}")

        # Check 2: must be within the allowed base directory
        # Use fd-relative check to avoid symlink-based path traversal
        real_path = Path(f"/proc/self/fd/{fd}").resolve()
        if not str(real_path).startswith(str(ALLOWED_BASE)):
            raise PermissionError(f"Path escapes allowed base: {real_path}")

        # Check 3: size limit
        if file_stat.st_size > MAX_FILE_SIZE:
            raise ValueError(f"File too large: {file_stat.st_size} bytes")

        # Check 4: must be owned by trusted user (not world-writable)
        if file_stat.st_mode & stat.S_IWOTH:
            raise PermissionError(f"World-writable file rejected: {user_path}")

        # Now read — we hold the fd, so no race possible
        with os.fdopen(fd, "rb") as f:
            return f.read()
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise

async def demo():
    try:
        data = await safe_read_file("/safe/data/report.txt")
        print(f"Read {len(data)} bytes safely")
    except PermissionError as e:
        print(f"Access denied: {e}")
```

**When to use**: Any agent that reads files based on user-supplied paths. O_NOFOLLOW + fstat eliminates the classic symlink TOCTOU.

---

## Solution 2: Optimistic Locking for Shared Resource Mutations

Use a version counter (optimistic lock) so concurrent mutations are detected and rejected rather than silently overwriting each other.

```python
import asyncio
import time
import hashlib
from dataclasses import dataclass, field
from typing import Any

@dataclass
class VersionedResource:
    key: str
    value: Any
    version: int = 0
    last_modified: float = field(default_factory=time.time)

class OptimisticStore:
    """Versioned key-value store with compare-and-swap semantics."""

    def __init__(self):
        self._store: dict[str, VersionedResource] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> tuple[Any, int]:
        """Return (value, version). Caller must pass version back on update."""
        async with self._lock:
            resource = self._store.get(key)
            if resource is None:
                return None, 0
            return resource.value, resource.version

    async def compare_and_set(
        self, key: str, new_value: Any, expected_version: int
    ) -> tuple[bool, int]:
        """
        Atomically update only if version matches expected.
        Returns (success, current_version).
        """
        async with self._lock:
            resource = self._store.get(key)
            current_version = resource.version if resource else 0

            if current_version != expected_version:
                # TOCTOU violation: resource was modified between check and update
                return False, current_version

            new_version = current_version + 1
            self._store[key] = VersionedResource(
                key=key,
                value=new_value,
                version=new_version,
                last_modified=time.time(),
            )
            return True, new_version

# Agent tool that updates shared state atomically
store = OptimisticStore()

async def safe_update_user_quota(user_id: str, delta: int, max_retries: int = 3) -> bool:
    for attempt in range(max_retries):
        current_quota, version = await store.get(f"quota:{user_id}")
        if current_quota is None:
            current_quota = 1000  # default

        new_quota = current_quota + delta
        if new_quota < 0:
            raise ValueError("Quota would go negative")

        success, _ = await store.compare_and_set(
            f"quota:{user_id}", new_quota, expected_version=version
        )
        if success:
            return True
        # Retry on conflict (another coroutine modified quota between our read and write)
        await asyncio.sleep(0.01 * (2 ** attempt))

    raise RuntimeError(f"Failed to update quota after {max_retries} retries")
```

**When to use**: Any shared mutable state accessed by concurrent agent coroutines (quotas, counters, config, session state).

---

## Solution 3: Token Binding with Single-Use Nonces for Tool Authorization

Prevent tool argument substitution by binding the authorization check to a single-use, time-limited nonce.

```python
import asyncio
import secrets
import time
import hmac
import hashlib
import json

NONCE_SECRET = secrets.token_bytes(32)
NONCE_TTL = 30.0  # nonces valid for 30 seconds

_used_nonces: set[str] = set()
_nonce_lock = asyncio.Lock()

def _sign_payload(payload: dict, nonce: str, ts: float) -> str:
    data = json.dumps({"payload": payload, "nonce": nonce, "ts": ts}, sort_keys=True).encode()
    return hmac.new(NONCE_SECRET, data, hashlib.sha256).hexdigest()

async def authorize_tool_call(tool_name: str, arguments: dict) -> str:
    """
    Issue a single-use authorization token binding tool_name + arguments.
    The token must be presented at execution time — no substitution possible.
    """
    nonce = secrets.token_hex(16)
    ts = time.time()
    sig = _sign_payload({"tool": tool_name, "args": arguments}, nonce, ts)
    return f"{nonce}:{ts}:{sig}"

async def execute_authorized_tool(
    token: str,
    tool_name: str,
    arguments: dict,
) -> Any:
    """
    Execute only if the token was issued for exactly this tool_name + arguments.
    Prevents: token replay, argument substitution between check and execute.
    """
    parts = token.split(":", 2)
    if len(parts) != 3:
        raise PermissionError("Invalid authorization token format")

    nonce, ts_str, sig = parts
    ts = float(ts_str)

    # Check 1: token not expired
    if time.time() - ts > NONCE_TTL:
        raise PermissionError(f"Authorization token expired ({NONCE_TTL}s TTL)")

    # Check 2: token not already used (replay prevention)
    async with _nonce_lock:
        if nonce in _used_nonces:
            raise PermissionError("Authorization token already used (replay detected)")
        _used_nonces.add(nonce)

    # Check 3: signature matches exact tool + arguments
    expected_sig = _sign_payload({"tool": tool_name, "args": arguments}, nonce, ts)
    if not hmac.compare_digest(expected_sig, sig):
        raise PermissionError("Authorization token signature mismatch (argument substitution detected)")

    # Safe to execute — check and use are now atomic via the token
    return await _dispatch_tool(tool_name, arguments)

async def _dispatch_tool(tool_name: str, arguments: dict):
    # Tool execution happens here
    return {"tool": tool_name, "result": "ok"}

# Usage pattern: check → issue token → execute (token binds all three steps)
async def agent_tool_flow(user_request: dict):
    tool_name = user_request["tool"]
    arguments = user_request["args"]

    # Phase 1: Authorization check (with current arguments)
    # ... validate permissions for tool_name + arguments ...

    # Phase 2: Bind check result to execution
    token = await authorize_tool_call(tool_name, arguments)

    # Phase 3: Execute — token proves arguments haven't changed
    return await execute_authorized_tool(token, tool_name, arguments)
```

**When to use**: Agents where tool authorization and execution are in different code paths or happen at different times.

---

## Solution 4: Database-Level Atomic Check-and-Act with SELECT FOR UPDATE

For database-backed agents, use transactions with row-level locking to eliminate TOCTOU between quota check and debit.

```python
import asyncio
import asyncpg

async def atomic_quota_check_and_debit(
    pool: asyncpg.Pool,
    user_id: str,
    cost_tokens: int,
) -> bool:
    """
    Atomically check quota and debit — no race between check and debit.
    Returns True if quota was sufficient and deducted.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # SELECT FOR UPDATE: lock this row until the transaction commits
            # Prevents concurrent transactions from reading the same (un-debited) quota
            row = await conn.fetchrow(
                "SELECT quota_remaining FROM user_quotas WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if row is None:
                return False

            remaining = row["quota_remaining"]
            if remaining < cost_tokens:
                return False  # Insufficient quota — checked AND acted atomically

            # Debit within the same transaction (no window for concurrent depletion)
            await conn.execute(
                """
                UPDATE user_quotas
                SET quota_remaining = quota_remaining - $1,
                    last_used = NOW()
                WHERE user_id = $2
                """,
                cost_tokens,
                user_id,
            )
            return True

async def agent_api_call_with_quota(pool: asyncpg.Pool, user_id: str, prompt: str) -> str:
    estimated_cost = len(prompt.split()) * 2  # rough token estimate

    # Atomic check-and-debit: no TOCTOU window
    allowed = await atomic_quota_check_and_debit(pool, user_id, estimated_cost)
    if not allowed:
        raise PermissionError(f"Quota insufficient for user {user_id}")

    # Proceed with API call — quota is guaranteed to be held
    from anthropic import AsyncAnthropic
    resp = await AsyncAnthropic().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text
```

**When to use**: Multi-tenant agents with per-user quotas stored in PostgreSQL. SELECT FOR UPDATE is the correct primitive.

---

## Solution 5: Redis Atomic Check-and-Set for Distributed TOCTOU Prevention

Use Redis Lua scripts or SET NX for atomic check-and-set across distributed agent instances.

```python
import asyncio
import redis.asyncio as aioredis
import time

redis_client = aioredis.from_url("redis://localhost:6379")

# Lua script: atomic check-and-decrement
# Returns 1 if quota was sufficient and decremented, 0 if not.
ATOMIC_QUOTA_SCRIPT = """
local key = KEYS[1]
local cost = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', key) or '0')
if current >= cost then
    redis.call('DECRBY', key, cost)
    return 1
else
    return 0
end
"""

async def atomic_redis_quota_check(user_id: str, cost: int) -> bool:
    """Single-round-trip atomic check-and-debit via Lua script."""
    key = f"quota:{user_id}"
    result = await redis_client.eval(
        ATOMIC_QUOTA_SCRIPT,
        1,          # number of keys
        key,        # KEYS[1]
        str(cost),  # ARGV[1]
    )
    return bool(result)

# Atomic lock acquisition (SET NX EX) — prevents concurrent tool execution
async def acquire_exclusive_lock(
    resource_id: str,
    ttl_seconds: int = 30,
) -> str | None:
    """
    Atomically acquire an exclusive lock.
    Returns lock token if acquired, None if already locked.
    """
    import secrets
    lock_token = secrets.token_hex(16)
    key = f"lock:{resource_id}"

    # SET NX EX is atomic: set only if not exists, with expiry
    acquired = await redis_client.set(key, lock_token, nx=True, ex=ttl_seconds)
    return lock_token if acquired else None

async def release_lock(resource_id: str, lock_token: str) -> bool:
    """Release lock only if we hold it (prevents releasing another holder's lock)."""
    RELEASE_SCRIPT = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        redis.call('DEL', KEYS[1])
        return 1
    else
        return 0
    end
    """
    key = f"lock:{resource_id}"
    result = await redis_client.eval(RELEASE_SCRIPT, 1, key, lock_token)
    return bool(result)

# Usage
async def safe_tool_execution(tool_id: str, execute_fn) -> Any:
    lock_token = await acquire_exclusive_lock(tool_id, ttl_seconds=60)
    if lock_token is None:
        raise RuntimeError(f"Tool {tool_id} is already executing")
    try:
        return await execute_fn()
    finally:
        await release_lock(tool_id, lock_token)
```

**When to use**: Distributed agents where multiple instances could race to execute the same tool or consume the same quota.

---

## Solution 6: Immutable Snapshot Pattern for Multi-Step Validation

Take an immutable snapshot of the resource at check time; pass the snapshot (not a reference) to the execution step.

```python
import asyncio
import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)  # frozen = immutable after creation
class ResourceSnapshot:
    """Immutable snapshot of resource state at check time."""
    resource_id: str
    data: tuple          # use tuple (not list) to enforce immutability
    checksum: str
    captured_at: float

    @classmethod
    def capture(cls, resource_id: str, data: Any) -> "ResourceSnapshot":
        # Deep copy and freeze the data
        frozen_data = tuple(json.dumps(data, sort_keys=True))
        checksum = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
        return cls(
            resource_id=resource_id,
            data=frozen_data,
            checksum=checksum,
            captured_at=time.time(),
        )

    def restore(self) -> Any:
        return json.loads("".join(self.data))

    def is_stale(self, max_age_seconds: float = 5.0) -> bool:
        return time.time() - self.captured_at > max_age_seconds

class SnapshotValidatingExecutor:
    def __init__(self, max_snapshot_age: float = 5.0):
        self._max_age = max_snapshot_age

    async def check_and_snapshot(self, resource_id: str, fetch_fn) -> ResourceSnapshot:
        """Fetch resource and take an immutable snapshot."""
        data = await fetch_fn(resource_id)
        # Validate at this point in time
        self._validate(resource_id, data)
        return ResourceSnapshot.capture(resource_id, data)

    def _validate(self, resource_id: str, data: Any):
        """Override with domain-specific validation."""
        if data is None:
            raise ValueError(f"Resource not found: {resource_id}")

    async def execute(self, snapshot: ResourceSnapshot, action_fn) -> Any:
        """Execute using the snapshot — not the live resource."""
        if snapshot.is_stale(self._max_age):
            raise RuntimeError(
                f"Snapshot for {snapshot.resource_id} is stale "
                f"({time.time() - snapshot.captured_at:.1f}s old)"
            )
        # Action receives the frozen snapshot, not a live reference
        # Even if the live resource changed, we act on what we validated
        data = snapshot.restore()
        return await action_fn(data)

executor = SnapshotValidatingExecutor(max_snapshot_age=5.0)

async def safe_agent_flow(resource_id: str):
    # Phase 1: check and snapshot (atomic view)
    snapshot = await executor.check_and_snapshot(
        resource_id,
        fetch_fn=lambda rid: fetch_resource(rid),
    )

    # Phase 2: execute on snapshot (not on live resource)
    result = await executor.execute(
        snapshot,
        action_fn=lambda data: process_resource(data),
    )
    return result

async def fetch_resource(rid: str) -> dict:
    return {"id": rid, "value": 42, "permissions": ["read", "write"]}

async def process_resource(data: dict) -> str:
    return f"processed {data['id']}"
```

**When to use**: Multi-step agent pipelines where validation and execution are naturally separated. Snapshots make the separation safe.

---

## Comparison

| Solution | Race Window | Distributed Safe | DB Required | Overhead | Best For |
|---|---|---|---|---|---|
| Atomic file open (O_NOFOLLOW) | Eliminated | No | No | Minimal | File path TOCTOU |
| Optimistic locking | Detected + retried | With CAS DB | Optional | Low | Shared mutable state |
| Token-bound authorization | Eliminated | No | No | Low | Tool argument substitution |
| SELECT FOR UPDATE | Eliminated | Yes (DB lock) | Yes | Medium | Quota management in Postgres |
| Redis Lua atomic | Eliminated | Yes | Redis | Low | Distributed quota/locking |
| Immutable snapshot | Eliminated (bounded) | Partial | No | Low | Multi-step validation flows |

**Rule of thumb**: Never check-then-use. Always use the atomic primitive: O_NOFOLLOW for files, SELECT FOR UPDATE for DB, Redis Lua for distributed state, frozen snapshots for multi-step pipelines.
