---
title: "Agent Doesn't Implement Tenant Context Isolation for Multi-Tenant Agents"
description: "Multi-tenant agents that share memory, tool state, or LLM context between tenants leak sensitive data across customer boundaries. Implement strict tenant context isolation to ensure each tenant sees only its own data and cannot influence another tenant's agent."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-tenant-context-isolation-for-multi-tenant-agents
tags: [multi-tenant, tenant-isolation, data-leakage, security, context-isolation, authorization]
symptoms:
  - "Agent memory from Tenant A leaks into Tenant B's responses"
  - "Shared in-process cache returns records belonging to other tenants"
  - "Vector store search returns documents from other tenants"
  - "LLM context window contains prior-tenant conversation fragments"
  - "Tool call logs mix records from multiple tenants in the same table"
---

## Why This Happens

Agents deployed as shared services often reuse connection pools, caches, vector stores, and even in-flight LLM contexts across tenants for efficiency. Without explicit tenant scoping at every data access boundary, a cache key collision, a missing WHERE clause, or a context bleed between sessions can silently return data belonging to another customer. Tenant isolation must be enforced at every layer: cache keys, DB queries, vector namespaces, and context construction.

## Solution 1: Tenant Context Propagation with Scoped DI Container

```python
from __future__ import annotations
import contextvars
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    plan: str           # free | pro | enterprise
    data_region: str    # us-east-1 | eu-west-1
    allowed_tools: frozenset = field(default_factory=frozenset)

# Thread/coroutine-local tenant context
_current_tenant: contextvars.ContextVar[Optional[TenantContext]] = \
    contextvars.ContextVar("current_tenant", default=None)

def get_tenant() -> TenantContext:
    ctx = _current_tenant.get()
    if ctx is None:
        raise RuntimeError("No tenant context set. Call set_tenant_context() first.")
    return ctx

def set_tenant_context(ctx: TenantContext) -> contextvars.Token:
    return _current_tenant.set(ctx)

def reset_tenant_context(token: contextvars.Token) -> None:
    _current_tenant.reset(token)


class TenantScopedComponent:
    """Base class for all components that must enforce tenant isolation."""

    def _assert_tenant(self, record_tenant_id: str) -> None:
        current = get_tenant()
        if record_tenant_id != current.tenant_id:
            raise PermissionError(
                f"Tenant isolation violation: component belongs to tenant "
                f"'{record_tenant_id}', but current tenant is '{current.tenant_id}'"
            )


import asyncio
from contextlib import asynccontextmanager

@asynccontextmanager
async def tenant_scope(ctx: TenantContext):
    """Async context manager that sets/resets tenant context for a coroutine."""
    token = set_tenant_context(ctx)
    try:
        yield ctx
    finally:
        reset_tenant_context(token)
```

## Solution 2: Tenant-Scoped Cache with Key Namespacing

```python
import asyncio
import hashlib
from typing import Any, Optional

class TenantScopedCache:
    """
    Wraps any key-value cache and automatically prefixes all keys
    with the current tenant_id. Prevents cross-tenant cache hits.
    """

    def __init__(self, backend):
        self._backend = backend

    def _scoped_key(self, key: str) -> str:
        tenant_id = get_tenant().tenant_id
        # Hash tenant_id to fixed length to avoid key size issues
        prefix = hashlib.sha256(tenant_id.encode()).hexdigest()[:16]
        return f"t:{prefix}:{key}"

    async def get(self, key: str) -> Optional[Any]:
        return await self._backend.get(self._scoped_key(key))

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        scoped = self._scoped_key(key)
        if ttl:
            await self._backend.setex(scoped, ttl, value)
        else:
            await self._backend.set(scoped, value)

    async def delete(self, key: str) -> None:
        await self._backend.delete(self._scoped_key(key))

    async def flush_tenant(self, tenant_id: str) -> int:
        """Delete all keys for a specific tenant (e.g., on account deletion)."""
        prefix = hashlib.sha256(tenant_id.encode()).hexdigest()[:16]
        pattern = f"t:{prefix}:*"
        keys = await self._backend.keys(pattern)
        if keys:
            await self._backend.delete(*keys)
        return len(keys)


class TenantScopedVectorStore:
    """
    Namespaces vector store queries by tenant_id.
    Prevents retrieval of embeddings from other tenants.
    """

    def __init__(self, vector_backend):
        self._backend = vector_backend

    def _namespace(self) -> str:
        return get_tenant().tenant_id

    async def upsert(self, doc_id: str, embedding: list, metadata: dict) -> None:
        metadata["_tenant_id"] = self._namespace()
        await self._backend.upsert(
            namespace=self._namespace(),
            id=doc_id,
            values=embedding,
            metadata=metadata,
        )

    async def query(self, embedding: list, top_k: int = 10) -> list:
        results = await self._backend.query(
            namespace=self._namespace(),
            vector=embedding,
            top_k=top_k,
            filter={"_tenant_id": {"$eq": self._namespace()}},
        )
        # Double-check: strip any results that somehow slipped through
        return [r for r in results if r["metadata"].get("_tenant_id") == self._namespace()]
```

## Solution 3: Row-Level Security Enforcement for DB Tool Calls

```python
from typing import Any, List, Optional, Tuple

class TenantAwareQueryBuilder:
    """
    Automatically appends tenant_id WHERE clause to every query.
    Agents cannot issue queries without the tenant filter.
    """

    def __init__(self, pool):
        self._pool = pool

    def _inject_tenant_filter(self, sql: str, params: tuple) -> Tuple[str, tuple]:
        """
        Rewrites 'SELECT ... FROM table WHERE ...' to include tenant_id = $N.
        For safety, always appends AND tenant_id = $N rather than relying on
        the agent-supplied WHERE clause.
        """
        tenant_id = get_tenant().tenant_id
        # Inject tenant filter; use next available param slot
        n = len(params) + 1
        if "WHERE" in sql.upper():
            new_sql = sql + f" AND tenant_id = ${n}"
        else:
            new_sql = sql + f" WHERE tenant_id = ${n}"
        return new_sql, params + (tenant_id,)

    async def fetch(self, sql: str, *params) -> List[dict]:
        scoped_sql, scoped_params = self._inject_tenant_filter(sql, params)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(scoped_sql, *scoped_params)
            return [dict(r) for r in rows]

    async def execute(self, sql: str, *params) -> None:
        scoped_sql, scoped_params = self._inject_tenant_filter(sql, params)
        async with self._pool.acquire() as conn:
            await conn.execute(scoped_sql, *scoped_params)

    async def insert(self, table: str, data: dict) -> dict:
        """Automatically injects tenant_id into INSERT."""
        tenant_id = get_tenant().tenant_id
        data = dict(data)
        data["tenant_id"] = tenant_id
        cols = list(data.keys())
        placeholders = [f"${i+1}" for i in range(len(cols))]
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"RETURNING *"
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, *data.values())
            return dict(row)
```

## Solution 4: Tenant-Isolated LLM Context Builder

```python
from typing import List, Optional

class TenantIsolatedContextBuilder:
    """
    Builds LLM context windows that are strictly scoped to the current tenant.
    Prevents cross-tenant memory injection via shared message history stores.
    """

    def __init__(self, memory_store, system_prompt_template: str):
        self._memory = memory_store
        self._system_template = system_prompt_template

    async def build_messages(
        self,
        conversation_id: str,
        new_user_message: str,
        max_history: int = 20,
    ) -> List[dict]:
        tenant = get_tenant()

        # Always scope memory retrieval to current tenant + conversation
        history = await self._memory.get_history(
            conversation_id=conversation_id,
            tenant_id=tenant.tenant_id,  # explicit tenant filter
            limit=max_history,
        )

        # Verify every history message belongs to this tenant
        for msg in history:
            if msg.get("tenant_id") != tenant.tenant_id:
                raise PermissionError(
                    f"Tenant isolation breach: history message belongs to "
                    f"'{msg.get('tenant_id')}', not '{tenant.tenant_id}'"
                )

        system_prompt = self._system_template.format(
            tenant_id=tenant.tenant_id,
            plan=tenant.plan,
            data_region=tenant.data_region,
        )

        messages = [{"role": "system", "content": system_prompt}]
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": new_user_message})
        return messages

    async def save_turn(
        self,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        tenant = get_tenant()
        await self._memory.append(
            conversation_id=conversation_id,
            tenant_id=tenant.tenant_id,
            messages=[
                {"role": "user", "content": user_message, "tenant_id": tenant.tenant_id},
                {"role": "assistant", "content": assistant_message, "tenant_id": tenant.tenant_id},
            ],
        )
```

## Solution 5: Tenant Isolation Audit Logger

```python
import json
import time
from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class TenantAccessEvent:
    event_type: str       # data_access | tool_call | cache_hit | isolation_violation
    tenant_id: str
    resource_type: str    # db_record | vector_doc | cache_key | llm_context
    resource_id: str
    allowed: bool
    violation_detail: Optional[str]
    timestamp: float

class TenantIsolationAuditLog:
    def __init__(self, sink):
        self._sink = sink

    async def record(self, event: TenantAccessEvent) -> None:
        if not event.allowed:
            # Violations are always logged at CRITICAL level
            print(f"[CRITICAL][tenant_isolation] VIOLATION: {event.violation_detail} "
                  f"tenant={event.tenant_id} resource={event.resource_id}")
        await self._sink.append(json.dumps(asdict(event)))

    async def detect_cross_tenant_pattern(
        self, tenant_id: str, window_seconds: float = 60.0
    ) -> int:
        """Returns count of isolation violations for tenant in recent window."""
        cutoff = time.time() - window_seconds
        violations = await self._sink.query(
            tenant_id=tenant_id,
            event_type="isolation_violation",
            since=cutoff,
        )
        return len(violations)


class AuditedTenantScopedCache(TenantScopedCache):
    """Adds audit logging to TenantScopedCache operations."""

    def __init__(self, backend, audit_log: TenantIsolationAuditLog):
        super().__init__(backend)
        self._audit = audit_log

    async def get(self, key: str) -> Optional[Any]:
        result = await super().get(key)
        await self._audit.record(TenantAccessEvent(
            event_type="cache_hit" if result else "cache_miss",
            tenant_id=get_tenant().tenant_id,
            resource_type="cache_key",
            resource_id=key,
            allowed=True,
            violation_detail=None,
            timestamp=time.time(),
        ))
        return result
```

## Solution 6: Tenant Isolation Test Harness

```python
import asyncio
from typing import List

class TenantIsolationTestHarness:
    """
    Automated test harness: seeds data for tenant A and B, then
    verifies that each tenant cannot see the other's data.
    """

    def __init__(self, cache: TenantScopedCache, db: TenantAwareQueryBuilder):
        self._cache = cache
        self._db = db

    async def run_isolation_checks(self) -> List[str]:
        failures: List[str] = []

        tenant_a = TenantContext(tenant_id="tenant_a", plan="pro", data_region="us-east-1",
                                 allowed_tools=frozenset())
        tenant_b = TenantContext(tenant_id="tenant_b", plan="free", data_region="us-east-1",
                                 allowed_tools=frozenset())

        # Seed data as tenant A
        async with tenant_scope(tenant_a):
            await self._cache.set("user:1", {"name": "Alice"})

        # Attempt to read as tenant B
        async with tenant_scope(tenant_b):
            result = await self._cache.get("user:1")
            if result is not None:
                failures.append(
                    f"FAIL: Tenant B read Tenant A's cache key 'user:1' -> {result}"
                )
            else:
                print("PASS: Tenant B cannot read Tenant A cache key")

        # DB isolation check
        async with tenant_scope(tenant_a):
            await self._db.insert("agents", {"name": "agent-a", "config": "{}"})

        async with tenant_scope(tenant_b):
            rows = await self._db.fetch("SELECT * FROM agents WHERE name = $1", "agent-a")
            if rows:
                failures.append(f"FAIL: Tenant B read Tenant A's DB row: {rows}")
            else:
                print("PASS: Tenant B cannot read Tenant A DB rows")

        return failures
```

## Comparison

| Approach | Isolation Scope | Enforcement Point | Detects Violations | Overhead |
|---|---|---|---|---|
| TenantContext + contextvars | All layers | Runtime assertion | On explicit check | Negligible |
| TenantScopedCache | Cache layer | Key namespacing | Silent prevention | Negligible |
| TenantAwareQueryBuilder | DB layer | SQL WHERE injection | Silent prevention | Negligible |
| TenantIsolatedContextBuilder | LLM context | History filter + verify | On verification | Low |
| TenantIsolationAuditLog | Audit layer | Post-access logging | Full audit trail | Low (async) |
| TenantIsolationTestHarness | Test layer | Automated cross-tenant probes | Test failures | Test-time only |

**Best for production**: Use `TenantContext` + `contextvars` as the propagation backbone, `TenantScopedCache` for all caches, `TenantAwareQueryBuilder` for all DB access, and `TenantIsolatedContextBuilder` for LLM context. Add `TenantIsolationAuditLog` to record any violations and run `TenantIsolationTestHarness` in CI to catch regressions.
