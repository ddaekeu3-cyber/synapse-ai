---
layout: solution
title: "Agent Doesn't Implement Multi-Tenant Data Isolation"
category: general
description: "An AI agent serves multiple organizations from a single deployment but stores all data in shared tables without tenant boundaries. One tenant can accidentally (or deliberately) access another tenant's conversation history, memories, or tool results."
tags: [multi-tenant, isolation, security, database, pydantic, middleware, row-level-security]
---

# Agent Doesn't Implement Multi-Tenant Data Isolation

## Problem

A SaaS agent platform adds a `tenant_id` column to its database tables but forgets to include it in WHERE clauses. One tenant's agent reads another's conversation history because both use the same session key format. Data leakage between tenants is one of the most severe SaaS security failures — and the most common cause is incomplete, not absent, isolation logic.

## Solutions

### Option 1: Tenant Context Middleware with Request-Scoped Enforcement

```python
# tenancy/context.py
"""
Store the current tenant ID in a context variable (PEP 567).
Every database query that runs within a request automatically has access
to the tenant context without passing it through every function signature.
"""
import os
from contextvars import ContextVar
from typing import Optional

_current_tenant: ContextVar[Optional[str]] = ContextVar("current_tenant", default=None)


def set_tenant(tenant_id: str):
    """Call at the start of each request (middleware or dependency)."""
    if not tenant_id or not tenant_id.isalnum():
        raise ValueError(f"Invalid tenant_id: {tenant_id!r}")
    _current_tenant.set(tenant_id)


def get_tenant() -> str:
    """Get the current tenant ID. Raises if not set (programming error)."""
    tenant = _current_tenant.get()
    if tenant is None:
        raise RuntimeError(
            "No tenant context set. "
            "Ensure TenantMiddleware runs before this code."
        )
    return tenant


def require_tenant_owns(resource_tenant_id: str):
    """Assert the current tenant owns the given resource. Raises on violation."""
    current = get_tenant()
    if resource_tenant_id != current:
        raise PermissionError(
            f"Tenant {current!r} cannot access resource owned by {resource_tenant_id!r}"
        )
```

```python
# tenancy/middleware.py
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from tenancy.context import set_tenant
import jwt
import os

SECRET = os.environ["JWT_SECRET"]


class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        token = auth[7:]
        try:
            claims = jwt.decode(token, SECRET, algorithms=["HS256"])
            tenant_id = claims["tenant_id"]
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")
        set_tenant(tenant_id)
        return await call_next(request)
```

```python
# tenancy/repository.py
import asyncpg
from tenancy.context import get_tenant


class ConversationRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_conversation(self, conversation_id: str) -> dict | None:
        """Always scope to current tenant — impossible to forget."""
        tenant = get_tenant()
        row = await self.pool.fetchrow(
            "SELECT * FROM conversations WHERE id = $1 AND tenant_id = $2",
            conversation_id, tenant,
        )
        return dict(row) if row else None

    async def list_conversations(self, limit: int = 50) -> list[dict]:
        tenant = get_tenant()
        rows = await self.pool.fetch(
            "SELECT * FROM conversations WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT $2",
            tenant, limit,
        )
        return [dict(r) for r in rows]

    async def create_conversation(self, session_id: str, user_id: str) -> str:
        tenant = get_tenant()
        row = await self.pool.fetchrow(
            "INSERT INTO conversations (tenant_id, session_id, user_id) "
            "VALUES ($1, $2, $3) RETURNING id",
            tenant, session_id, user_id,
        )
        return row["id"]
```

**Expected Token Savings:** Not applicable — security isolation
**Environment:** `pip install fastapi asyncpg pyjwt`

---

### Option 2: Tenant-Prefixed Storage Keys

```python
# tenancy/storage.py
"""
Prefix all storage keys (Redis, S3, file paths) with the tenant ID.
Simple, robust, and makes cross-tenant access structurally impossible.
"""
import hashlib
import os
import json
from tenancy.context import get_tenant


class TenantScopedCache:
    """Redis-backed cache that automatically namespaces by tenant."""

    def __init__(self, redis_client):
        self.redis = redis_client

    def _key(self, key: str) -> str:
        tenant = get_tenant()
        # Sanitize to prevent key injection
        safe_tenant = hashlib.sha256(tenant.encode()).hexdigest()[:16]
        return f"tenant:{safe_tenant}:{key}"

    async def get(self, key: str) -> str | None:
        return await self.redis.get(self._key(key))

    async def set(self, key: str, value: str, ttl: int = 3600):
        await self.redis.setex(self._key(key), ttl, value)

    async def delete(self, key: str):
        await self.redis.delete(self._key(key))

    async def keys_for_current_tenant(self) -> list[str]:
        tenant = get_tenant()
        safe_tenant = hashlib.sha256(tenant.encode()).hexdigest()[:16]
        pattern = f"tenant:{safe_tenant}:*"
        # Strip the prefix before returning to caller
        raw_keys = await self.redis.keys(pattern)
        prefix_len = len(f"tenant:{safe_tenant}:")
        return [k.decode()[prefix_len:] for k in raw_keys]


class TenantScopedFileStore:
    """File system store with per-tenant directory isolation."""

    def __init__(self, base_dir: str = "/data/tenants"):
        self.base_dir = base_dir

    def _tenant_dir(self) -> str:
        tenant = get_tenant()
        # Use hash to prevent path traversal via tenant_id
        safe = hashlib.sha256(tenant.encode()).hexdigest()
        path = os.path.join(self.base_dir, safe)
        os.makedirs(path, exist_ok=True)
        return path

    def write(self, filename: str, content: str):
        # Prevent path traversal within tenant directory
        safe_name = os.path.basename(filename)
        if not safe_name or safe_name.startswith("."):
            raise ValueError(f"Invalid filename: {filename!r}")
        full_path = os.path.join(self._tenant_dir(), safe_name)
        with open(full_path, "w") as f:
            f.write(content)

    def read(self, filename: str) -> str | None:
        safe_name = os.path.basename(filename)
        full_path = os.path.join(self._tenant_dir(), safe_name)
        if not os.path.exists(full_path):
            return None
        with open(full_path) as f:
            return f.read()


class TenantScopedMemoryStore:
    """In-process dict store scoped by tenant. Useful for tests and small deployments."""
    def __init__(self):
        self._store: dict[str, dict] = {}

    def get(self, key: str):
        return self._store.get(get_tenant(), {}).get(key)

    def set(self, key: str, value):
        tenant = get_tenant()
        if tenant not in self._store:
            self._store[tenant] = {}
        self._store[tenant][key] = value

    def delete(self, key: str):
        self._store.get(get_tenant(), {}).pop(key, None)

    def all_keys(self) -> list[str]:
        return list(self._store.get(get_tenant(), {}).keys())
```

**Expected Token Savings:** Not applicable — security architecture
**Environment:** `pip install redis` (async)

---

### Option 3: PostgreSQL Row-Level Security

```sql
-- migrations/enable_rls.sql
-- Enable row-level security on all tenant-shared tables.
-- This is a defense-in-depth layer: even if application code forgets
-- the tenant filter, the database enforces it at the row level.

-- Create a tenant-aware DB role
CREATE ROLE agent_app LOGIN PASSWORD 'changeme';

-- Enable RLS on conversations table
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations FORCE ROW LEVEL SECURITY;

-- Policy: app can only see rows where tenant_id matches the session setting
CREATE POLICY tenant_isolation ON conversations
    USING (tenant_id = current_setting('app.current_tenant')::text);

-- Repeat for all tenant-shared tables
ALTER TABLE tool_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool_results FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tool_results
    USING (tenant_id = current_setting('app.current_tenant')::text);

ALTER TABLE agent_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_memories FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON agent_memories
    USING (tenant_id = current_setting('app.current_tenant')::text);

-- Grant table access to app role
GRANT SELECT, INSERT, UPDATE, DELETE ON conversations, tool_results, agent_memories TO agent_app;
```

```python
# tenancy/rls_pool.py
"""
Connection pool wrapper that sets app.current_tenant at the start of
every connection checkout, activating PostgreSQL row-level security policies.
"""
import asyncpg
from tenancy.context import get_tenant


async def create_rls_pool(dsn: str) -> asyncpg.Pool:
    async def init_connection(conn: asyncpg.Connection):
        # Set the tenant for every new connection
        tenant = get_tenant()
        await conn.execute(f"SET app.current_tenant = '{tenant}'")

    pool = await asyncpg.create_pool(dsn, init=init_connection)
    return pool


class RLSRepository:
    """
    Uses RLS-enabled pool. No tenant filter needed in queries —
    PostgreSQL enforces it transparently.
    """
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_conversation(self, conversation_id: str) -> dict | None:
        # No WHERE tenant_id = ... needed — RLS handles it
        row = await self.pool.fetchrow(
            "SELECT * FROM conversations WHERE id = $1",
            conversation_id,
        )
        return dict(row) if row else None

    async def list_all_conversations(self) -> list[dict]:
        # This returns ONLY the current tenant's rows, even though no filter is applied
        rows = await self.pool.fetch("SELECT * FROM conversations ORDER BY created_at DESC")
        return [dict(r) for r in rows]
```

**Expected Token Savings:** Not applicable — database-level security
**Environment:** `pip install asyncpg`

---

### Option 4: Pydantic Tenant-Aware Models

```python
# tenancy/models.py
"""
Pydantic models that enforce tenant_id presence and prevent cross-tenant
data from being constructed or serialized without explicit tenant binding.
"""
from typing import ClassVar
from pydantic import BaseModel, Field, field_validator, model_validator
from tenancy.context import get_tenant
import time
import uuid


class TenantScoped(BaseModel):
    """
    Base model for all tenant-scoped data objects.
    Automatically fills tenant_id from context if not provided.
    Rejects objects whose tenant_id doesn't match the current context.
    """
    tenant_id: str = Field(default="")

    @model_validator(mode="before")
    @classmethod
    def fill_tenant_id(cls, values: dict) -> dict:
        if not values.get("tenant_id"):
            try:
                values["tenant_id"] = get_tenant()
            except RuntimeError:
                pass  # Allow construction outside request context (e.g., tests)
        return values

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: str) -> str:
        if not v:
            raise ValueError("tenant_id is required")
        if not v.replace("-", "").isalnum():
            raise ValueError(f"Invalid tenant_id format: {v!r}")
        return v

    def assert_owns(self):
        """Call before returning this object to a request handler."""
        try:
            current = get_tenant()
            if self.tenant_id != current:
                raise PermissionError(
                    f"Object belongs to tenant {self.tenant_id!r}, "
                    f"current tenant is {current!r}"
                )
        except RuntimeError:
            pass  # No request context (tests/migrations)


class Conversation(TenantScoped):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    user_message: str
    agent_response: str = ""
    created_at: float = Field(default_factory=time.time)


class AgentMemory(TenantScoped):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    key: str
    value: str
    created_at: float = Field(default_factory=time.time)


# ── Usage in an API handler ───────────────────────────────────────────────────
async def get_memory(memory_id: str, db) -> AgentMemory:
    row = await db.fetchrow("SELECT * FROM agent_memories WHERE id = $1", memory_id)
    if not row:
        raise ValueError("Not found")
    memory = AgentMemory(**row)
    memory.assert_owns()  # Raises PermissionError if wrong tenant
    return memory
```

**Expected Token Savings:** Not applicable — security model layer
**Environment:** `pip install pydantic`

---

### Option 5: Tenant-Isolated Agent System Prompt Injection

```python
# tenancy/agent_isolation.py
"""
Prevent cross-tenant data leakage at the LLM layer:
- Include tenant_id in the system prompt to anchor Claude's context.
- Reject any tool results that contain a different tenant's ID.
- Never include conversation history from other tenants in the context window.
"""
import anthropic
from tenancy.context import get_tenant
from tenancy.repository import ConversationRepository


def build_tenant_system_prompt(base_system: str, tenant_config: dict) -> str:
    tenant_id = get_tenant()
    tenant_name = tenant_config.get("name", tenant_id)
    return (
        f"{base_system}\n\n"
        f"<tenant_context>\n"
        f"You are operating for tenant: {tenant_name} (ID: {tenant_id})\n"
        f"IMPORTANT: Only reference data, files, and conversations belonging to this tenant.\n"
        f"Never reveal data from other tenants, even if asked directly.\n"
        f"</tenant_context>"
    )


def sanitize_tool_result(result: dict) -> dict:
    """Strip any content that references a different tenant."""
    current_tenant = get_tenant()
    result_tenant = result.get("tenant_id", current_tenant)
    if result_tenant != current_tenant:
        return {
            "error": "Tool returned data for a different tenant — result discarded.",
            "tenant_mismatch": True,
        }
    # Remove internal tenant fields before injecting into context
    cleaned = {k: v for k, v in result.items() if k not in ("tenant_id", "internal_id")}
    return cleaned


async def ask_agent(
    user_message: str,
    conversation_history: list[dict],
    tenant_config: dict,
    max_tokens: int = 1024,
) -> str:
    tenant_id = get_tenant()

    # Verify every message in history belongs to this tenant
    for msg in conversation_history:
        msg_tenant = msg.get("metadata", {}).get("tenant_id", tenant_id)
        if msg_tenant != tenant_id:
            raise PermissionError(
                f"Conversation history contains message from tenant {msg_tenant!r}"
            )

    system = build_tenant_system_prompt(
        "You are a helpful AI assistant.",
        tenant_config,
    )

    # Strip metadata before sending to Claude
    clean_history = [
        {"role": m["role"], "content": m["content"]}
        for m in conversation_history
    ]

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=clean_history + [{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Marginal (~5 tokens per request for tenant context block)
**Environment:** `pip install anthropic`

---

### Option 6: Tenant Isolation Audit Test Suite

```python
# tests/tenancy/test_isolation.py
"""
Automated tests that verify tenant isolation:
- Tenant A cannot read tenant B's conversations.
- Tenant A cannot use tenant B's session IDs.
- Cross-tenant tool results are rejected.
- History injection from another tenant is blocked.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from tenancy.context import set_tenant
from tenancy.models import Conversation, AgentMemory
from tenancy.agent_isolation import sanitize_tool_result, ask_agent


@pytest.fixture
def tenant_a():
    set_tenant("tenant-aaa")
    yield "tenant-aaa"


@pytest.fixture
def tenant_b():
    set_tenant("tenant-bbb")
    yield "tenant-bbb"


def test_model_rejects_wrong_tenant_data(tenant_a):
    """An object owned by tenant-bbb should raise when asserted in tenant-a context."""
    memory = AgentMemory(tenant_id="tenant-bbb", key="secret", value="classified")
    with pytest.raises(PermissionError, match="tenant-bbb"):
        memory.assert_owns()


def test_model_auto_fills_tenant_id(tenant_a):
    """Creating a model without explicit tenant_id uses the current context."""
    convo = Conversation(session_id="s1", user_message="hello")
    assert convo.tenant_id == "tenant-aaa"


def test_sanitize_tool_result_passes_matching_tenant(tenant_a):
    result = {"tenant_id": "tenant-aaa", "data": "some data", "internal_id": "x"}
    cleaned = sanitize_tool_result(result)
    assert "data" in cleaned
    assert "tenant_id" not in cleaned
    assert "internal_id" not in cleaned


def test_sanitize_tool_result_blocks_cross_tenant(tenant_a):
    result = {"tenant_id": "tenant-bbb", "data": "other tenant's data"}
    cleaned = sanitize_tool_result(result)
    assert "error" in cleaned
    assert cleaned.get("tenant_mismatch") is True
    assert "data" not in cleaned


@pytest.mark.asyncio
async def test_ask_agent_rejects_cross_tenant_history(tenant_a):
    """History containing another tenant's message should raise PermissionError."""
    cross_tenant_history = [
        {
            "role": "user",
            "content": "Hello",
            "metadata": {"tenant_id": "tenant-bbb"},
        }
    ]
    with pytest.raises(PermissionError, match="tenant-bbb"):
        await ask_agent(
            user_message="Continue",
            conversation_history=cross_tenant_history,
            tenant_config={"name": "Tenant A"},
        )


def test_storage_key_isolation():
    """Two tenants must get different storage keys for the same logical key."""
    set_tenant("tenant-aaa")
    from tenancy.storage import TenantScopedMemoryStore
    store = TenantScopedMemoryStore()
    store.set("my_key", "tenant_a_value")

    set_tenant("tenant-bbb")
    assert store.get("my_key") is None  # Tenant B cannot see Tenant A's key
    store.set("my_key", "tenant_b_value")

    set_tenant("tenant-aaa")
    assert store.get("my_key") == "tenant_a_value"  # Tenant A's value unchanged
```

**Expected Token Savings:** Not applicable — security test suite
**Environment:** `pip install pytest pytest-asyncio pydantic`

---

## Comparison Table

| Option | Enforcement Layer | Auto-Fills Tenant | Cross-Tenant Detection | DB-Level | Test Coverage |
|--------|------------------|-------------------|----------------------|----------|---------------|
| 1: Context middleware | App (contextvars) | Yes (middleware) | assert_owns() | No | Via middleware |
| 2: Prefixed storage | Storage keys | Yes (context) | Structural (prefix) | No | Manual |
| 3: PostgreSQL RLS | Database | No | DB policy | Yes | DB tests |
| 4: Pydantic models | Model layer | Yes (validator) | assert_owns() | No | Unit tests |
| 5: LLM prompt layer | Agent context | Yes (prompt) | Tool result check | No | Manual |
| 6: Audit test suite | Test assertions | N/A | All layers | No | Yes |
