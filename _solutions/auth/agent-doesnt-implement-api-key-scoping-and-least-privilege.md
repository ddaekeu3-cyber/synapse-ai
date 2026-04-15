---
layout: solution
title: "Agent Doesn't Implement API Key Scoping and Least Privilege"
category: auth
description: "Every API key has full access to all agent capabilities. A key issued to a read-only dashboard can invoke destructive tools. A leaked partner key can execute admin operations. Scoped keys limit the blast radius of any single credential compromise."
tags: [auth, api-key, least-privilege, scoping, security, fastapi, rbac]
---

# Agent Doesn't Implement API Key Scoping and Least Privilege

## Problem

An API key issued to a third-party integration for read access can also call `POST /api/agent/delete-all-data` or invoke `execute_code` tool. When that key is leaked (in a log, a Git commit, a partner's config), the attacker has full agent capabilities. Scoped keys encode exactly what a key is allowed to do — nothing more.

## Solutions

### Option 1: Scope-Based API Key Validation

```python
# auth/api_keys.py
"""
API keys carry a set of scopes (permissions).
Each endpoint declares which scopes it requires.
Keys without the required scope receive 403, not 401.
"""
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Scope(str, Enum):
    # Read-only operations
    CHAT_READ = "chat:read"           # Read conversation history
    TOOLS_READ = "tools:read"         # List available tools

    # Write operations
    CHAT_WRITE = "chat:write"         # Send messages, start sessions
    TOOLS_EXECUTE = "tools:execute"   # Call agent tools

    # Privileged operations
    ADMIN_READ = "admin:read"         # Read admin dashboards
    ADMIN_WRITE = "admin:write"       # Modify agent config
    MEMORY_WRITE = "memory:write"     # Write to agent memory store
    CODE_EXECUTE = "code:execute"     # Run code via execute_code tool


@dataclass
class APIKey:
    key_id: str
    key_hash: str          # SHA-256 of the raw key — never store raw key
    name: str              # Human-readable description
    scopes: set[Scope]
    owner_id: str
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    last_used_at: Optional[float] = None
    revoked: bool = False

    def is_valid(self) -> bool:
        if self.revoked:
            return False
        if self.expires_at and time.time() > self.expires_at:
            return False
        return True

    def has_scope(self, *required: Scope) -> bool:
        return all(s in self.scopes for s in required)

    def has_any_scope(self, *scopes: Scope) -> bool:
        return any(s in self.scopes for s in scopes)


def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.
    Returns (raw_key, key_id, key_hash).
    Only raw_key is shown to the user once — store key_hash and key_id.
    """
    key_id = secrets.token_urlsafe(8)     # Short, human-readable ID
    raw_key = secrets.token_urlsafe(32)   # 256-bit random key
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    # Format: prefix_keyid_rawkey (similar to GitHub PATs)
    full_key = f"agk_{key_id}_{raw_key}"
    full_hash = hashlib.sha256(full_key.encode()).hexdigest()
    return full_key, key_id, full_hash


# ── In-memory key store (replace with database in production) ─────────────────

_KEY_STORE: dict[str, APIKey] = {}


def register_key(
    name: str,
    owner_id: str,
    scopes: set[Scope],
    expires_in_days: Optional[int] = None,
) -> tuple[str, APIKey]:
    """Create and store a new API key. Returns (raw_key, key_record)."""
    raw_key, key_id, key_hash = generate_api_key()
    expires_at = time.time() + expires_in_days * 86400 if expires_in_days else None
    record = APIKey(
        key_id=key_id,
        key_hash=key_hash,
        name=name,
        scopes=scopes,
        owner_id=owner_id,
        expires_at=expires_at,
    )
    _KEY_STORE[key_hash] = record
    return raw_key, record


def lookup_key(raw_key: str) -> Optional[APIKey]:
    """Look up a key by its raw value. Returns None if not found or invalid."""
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return _KEY_STORE.get(key_hash)
```

**Expected Token Savings:** Not applicable — security architecture
**Environment:** `pip install fastapi`

---

### Option 2: FastAPI Dependency for Scope Enforcement

```python
# auth/dependencies.py
"""
FastAPI dependencies that extract and validate API keys with scope checking.
Inject these into route handlers to enforce least privilege.
"""
from functools import lru_cache
from typing import Optional
from fastapi import Depends, Header, HTTPException, Security
from fastapi.security import APIKeyHeader
from auth.api_keys import APIKey, Scope, lookup_key

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_api_key(x_api_key: Optional[str] = Security(api_key_header)) -> APIKey:
    """Base dependency: validates the key exists and is not expired/revoked."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    key = lookup_key(x_api_key)
    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not key.is_valid():
        raise HTTPException(status_code=401, detail="API key expired or revoked")
    # Update last_used_at
    import time
    key.last_used_at = time.time()
    return key


def require_scopes(*scopes: Scope):
    """
    Dependency factory: requires the API key to have ALL listed scopes.
    Usage:
        @app.post("/api/chat", dependencies=[Depends(require_scopes(Scope.CHAT_WRITE))])
    """
    async def check_scopes(key: APIKey = Depends(get_api_key)) -> APIKey:
        if not key.has_scope(*scopes):
            missing = [s for s in scopes if s not in key.scopes]
            raise HTTPException(
                status_code=403,
                detail=f"API key missing required scopes: {[s.value for s in missing]}",
            )
        return key
    return check_scopes


def require_any_scope(*scopes: Scope):
    """Requires the API key to have at least ONE of the listed scopes."""
    async def check_scopes(key: APIKey = Depends(get_api_key)) -> APIKey:
        if not key.has_any_scope(*scopes):
            raise HTTPException(
                status_code=403,
                detail=f"API key needs one of: {[s.value for s in scopes]}",
            )
        return key
    return check_scopes


# ── FastAPI route examples ────────────────────────────────────────────────────
from fastapi import FastAPI
app = FastAPI()


@app.get("/api/conversations")
async def list_conversations(
    key: APIKey = Depends(require_scopes(Scope.CHAT_READ)),
):
    """Read-only: requires chat:read scope."""
    return {"owner": key.owner_id, "conversations": []}


@app.post("/api/agent/chat")
async def send_message(
    body: dict,
    key: APIKey = Depends(require_scopes(Scope.CHAT_WRITE)),
):
    """Write: requires chat:write scope."""
    return {"response": "Hello!", "key_name": key.name}


@app.post("/api/tools/execute-code")
async def execute_code_tool(
    body: dict,
    key: APIKey = Depends(require_scopes(Scope.TOOLS_EXECUTE, Scope.CODE_EXECUTE)),
):
    """Requires BOTH tools:execute AND code:execute — double scope protection."""
    return {"output": "..."}


@app.post("/admin/agent/config")
async def update_config(
    body: dict,
    key: APIKey = Depends(require_scopes(Scope.ADMIN_WRITE)),
):
    """Admin-only: requires admin:write scope."""
    return {"updated": True}
```

**Expected Token Savings:** Not applicable — access control
**Environment:** `pip install fastapi`

---

### Option 3: Scope Hierarchy and Token Downscoping

```python
# auth/downscoping.py
"""
Support scope hierarchy: a key with admin:write implicitly has all lower scopes.
Also support downscoping: create a temporary derived key with reduced scopes
for passing to untrusted third-party integrations.
"""
import secrets
import time
from auth.api_keys import Scope, APIKey, _KEY_STORE
import hashlib


# Scope hierarchy: higher scopes imply lower scopes
SCOPE_HIERARCHY: dict[Scope, set[Scope]] = {
    Scope.ADMIN_WRITE: {
        Scope.ADMIN_READ, Scope.CHAT_WRITE, Scope.CHAT_READ,
        Scope.TOOLS_EXECUTE, Scope.TOOLS_READ, Scope.MEMORY_WRITE,
    },
    Scope.ADMIN_READ: {Scope.CHAT_READ, Scope.TOOLS_READ},
    Scope.CHAT_WRITE: {Scope.CHAT_READ},
    Scope.TOOLS_EXECUTE: {Scope.TOOLS_READ},
    Scope.CODE_EXECUTE: {Scope.TOOLS_EXECUTE, Scope.TOOLS_READ},
}


def expand_scopes(requested: set[Scope]) -> set[Scope]:
    """Expand a set of scopes by adding all implied lower scopes."""
    expanded = set(requested)
    changed = True
    while changed:
        changed = False
        for scope in list(expanded):
            implied = SCOPE_HIERARCHY.get(scope, set())
            new = implied - expanded
            if new:
                expanded |= new
                changed = True
    return expanded


def downscope(
    parent_key: APIKey,
    new_scopes: set[Scope],
    ttl_seconds: int = 3600,
    label: str = "",
) -> tuple[str, APIKey]:
    """
    Create a time-limited derived key with reduced scopes.
    The new key can only have scopes the parent key already has.
    Use for passing to webhooks, CI pipelines, or third-party integrations.
    """
    # Prevent scope escalation
    parent_expanded = expand_scopes(parent_key.scopes)
    disallowed = new_scopes - parent_expanded
    if disallowed:
        raise PermissionError(
            f"Cannot create derived key with scopes not held by parent: "
            f"{[s.value for s in disallowed]}"
        )

    raw_key = f"agk_derived_{secrets.token_urlsafe(24)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    derived = APIKey(
        key_id=f"derived_{secrets.token_urlsafe(6)}",
        key_hash=key_hash,
        name=label or f"Derived from {parent_key.name}",
        scopes=new_scopes,
        owner_id=parent_key.owner_id,
        expires_at=time.time() + ttl_seconds,
    )
    _KEY_STORE[key_hash] = derived
    return raw_key, derived


# ── Usage ─────────────────────────────────────────────────────────────────────

def create_ci_key(master_key: APIKey) -> str:
    """Create a read-only CI key that expires in 24 hours."""
    raw_key, derived = downscope(
        parent_key=master_key,
        new_scopes={Scope.CHAT_READ, Scope.TOOLS_READ},
        ttl_seconds=86400,
        label="CI pipeline read-only key",
    )
    return raw_key


def create_webhook_key(master_key: APIKey) -> str:
    """Create a narrow key for a webhook integration: chat write only."""
    raw_key, derived = downscope(
        parent_key=master_key,
        new_scopes={Scope.CHAT_WRITE},
        ttl_seconds=30 * 86400,
        label="Webhook integration key",
    )
    return raw_key
```

**Expected Token Savings:** Not applicable — security architecture
**Environment:** `pip install fastapi`

---

### Option 4: Tool-Level Scope Enforcement

```python
# tools/scoped_dispatcher.py
"""
Enforce scopes at the tool call level, not just the HTTP endpoint level.
An agent receiving a tool_use block from Claude checks whether the active
API key has permission to execute that specific tool.
Prevents a chat:write key from being used to invoke code:execute tools
even through the standard chat endpoint.
"""
from auth.api_keys import APIKey, Scope

# Map tool names to required scopes
TOOL_SCOPE_MAP: dict[str, set[Scope]] = {
    "search_documents": {Scope.TOOLS_READ},
    "read_file": {Scope.TOOLS_READ},
    "list_conversations": {Scope.CHAT_READ},
    "send_message": {Scope.CHAT_WRITE},
    "write_file": {Scope.TOOLS_EXECUTE},
    "delete_file": {Scope.TOOLS_EXECUTE, Scope.ADMIN_WRITE},
    "execute_code": {Scope.CODE_EXECUTE},
    "run_sql": {Scope.CODE_EXECUTE, Scope.ADMIN_WRITE},
    "update_agent_config": {Scope.ADMIN_WRITE},
}


class ScopedToolDispatcher:
    def __init__(self, api_key: APIKey):
        self.api_key = api_key

    def check_tool_permission(self, tool_name: str):
        """Raise PermissionError if the key cannot call this tool."""
        required = TOOL_SCOPE_MAP.get(tool_name, {Scope.TOOLS_EXECUTE})
        if not self.api_key.has_scope(*required):
            missing = [s.value for s in required if s not in self.api_key.scopes]
            raise PermissionError(
                f"API key '{self.api_key.name}' cannot call tool '{tool_name}'. "
                f"Missing scopes: {missing}"
            )

    def dispatch(self, tool_name: str, tool_input: dict):
        """Check permission then dispatch the tool call."""
        self.check_tool_permission(tool_name)
        return _TOOL_REGISTRY[tool_name](**tool_input)

    def filter_available_tools(self, all_tools: list[dict]) -> list[dict]:
        """
        Return only tool definitions the key is allowed to use.
        Pass this filtered list to Claude instead of all tools,
        so Claude can't even attempt to call restricted tools.
        """
        allowed = []
        for tool in all_tools:
            name = tool.get("name", "")
            required = TOOL_SCOPE_MAP.get(name, {Scope.TOOLS_EXECUTE})
            if self.api_key.has_scope(*required):
                allowed.append(tool)
        return allowed


# ── Tool registry ─────────────────────────────────────────────────────────────
_TOOL_REGISTRY: dict = {}  # Populated at startup


# ── Usage in multi-turn agent loop ───────────────────────────────────────────
import anthropic

def run_agent(user_message: str, api_key: APIKey, all_tools: list[dict]) -> str:
    dispatcher = ScopedToolDispatcher(api_key)
    allowed_tools = dispatcher.filter_available_tools(all_tools)

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=allowed_tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                try:
                    result = dispatcher.dispatch(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result),
                    })
                except PermissionError as e:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "is_error": True,
                        "content": f"Permission denied: {e}",
                    })

        messages.append({"role": "user", "content": tool_results})
```

**Expected Token Savings:** Not applicable — defense in depth
**Environment:** `pip install anthropic fastapi`

---

### Option 5: Scope Audit Logging

```python
# auth/scope_audit.py
"""
Log every scope check — both grants and denials.
Enables security teams to:
- Detect scope escalation attempts.
- Identify over-privileged keys (keys with scopes they never use).
- Generate compliance reports.
"""
import json
import logging
import time
from auth.api_keys import APIKey, Scope

audit_logger = logging.getLogger("auth.scope_audit")
audit_logger.setLevel(logging.INFO)

# Configure structured JSON logging
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(message)s'))
audit_logger.addHandler(handler)


def log_scope_check(
    key: APIKey,
    required_scopes: list[Scope],
    endpoint: str,
    granted: bool,
    client_ip: str = "",
):
    """Emit a structured audit log entry for every scope check."""
    event = {
        "event": "scope_check",
        "timestamp": time.time(),
        "key_id": key.key_id,
        "key_name": key.name,
        "owner_id": key.owner_id,
        "endpoint": endpoint,
        "required_scopes": [s.value for s in required_scopes],
        "key_scopes": [s.value for s in key.scopes],
        "granted": granted,
        "client_ip": client_ip,
    }
    if not granted:
        missing = [s.value for s in required_scopes if s not in key.scopes]
        event["missing_scopes"] = missing
        audit_logger.warning(json.dumps(event))
    else:
        audit_logger.info(json.dumps(event))


# ── Usage with FastAPI dependency ─────────────────────────────────────────────
from fastapi import Request, Depends, HTTPException
from auth.dependencies import get_api_key


def audited_require_scopes(*scopes: Scope):
    """Like require_scopes() but emits audit log on every check."""
    async def check(request: Request, key: APIKey = Depends(get_api_key)) -> APIKey:
        granted = key.has_scope(*scopes)
        log_scope_check(
            key=key,
            required_scopes=list(scopes),
            endpoint=str(request.url.path),
            granted=granted,
            client_ip=request.client.host if request.client else "",
        )
        if not granted:
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient scopes for {request.url.path}",
            )
        return key
    return check
```

**Expected Token Savings:** Not applicable — security observability
**Environment:** `pip install fastapi`

---

### Option 6: API Key Scope Tests

```python
# tests/auth/test_api_key_scopes.py
"""
Verify that scope enforcement works correctly:
- Keys with correct scopes are granted access.
- Keys missing scopes receive 403 (not 401 or 500).
- Scope hierarchy works (admin:write implies chat:read).
- Downscoped derived keys cannot escalate privileges.
"""
import pytest
from fastapi.testclient import TestClient
from auth.api_keys import Scope, register_key
from auth.downscoping import downscope, expand_scopes
from main import app

client = TestClient(app)


@pytest.fixture
def admin_key():
    raw, record = register_key("admin", "user-1", {Scope.ADMIN_WRITE})
    return raw, record


@pytest.fixture
def readonly_key():
    raw, record = register_key("readonly", "user-2", {Scope.CHAT_READ, Scope.TOOLS_READ})
    return raw, record


@pytest.fixture
def chat_only_key():
    raw, record = register_key("chat", "user-3", {Scope.CHAT_WRITE})
    return raw, record


def test_missing_api_key_returns_401():
    resp = client.get("/api/conversations")
    assert resp.status_code == 401


def test_invalid_api_key_returns_401():
    resp = client.get("/api/conversations", headers={"X-API-Key": "invalid"})
    assert resp.status_code == 401


def test_correct_scope_grants_access(readonly_key):
    raw, _ = readonly_key
    resp = client.get("/api/conversations", headers={"X-API-Key": raw})
    assert resp.status_code == 200


def test_missing_scope_returns_403(readonly_key):
    """A read-only key cannot write messages."""
    raw, _ = readonly_key
    resp = client.post("/api/agent/chat", json={"message": "hi"}, headers={"X-API-Key": raw})
    assert resp.status_code == 403
    assert "scope" in resp.json().get("detail", "").lower()


def test_admin_key_can_access_all_endpoints(admin_key):
    raw, _ = admin_key
    for path, method, body in [
        ("/api/conversations", "get", None),
        ("/api/agent/chat", "post", {"message": "hi"}),
        ("/admin/agent/config", "post", {"model": "claude-haiku-4-5-20251001"}),
    ]:
        if method == "get":
            resp = client.get(path, headers={"X-API-Key": raw})
        else:
            resp = client.post(path, json=body, headers={"X-API-Key": raw})
        assert resp.status_code not in (401, 403), f"{path} returned {resp.status_code}"


def test_scope_hierarchy_expansion():
    """admin:write should expand to include all lower scopes."""
    expanded = expand_scopes({Scope.ADMIN_WRITE})
    assert Scope.CHAT_READ in expanded
    assert Scope.TOOLS_READ in expanded
    assert Scope.CHAT_WRITE in expanded


def test_downscoped_key_cannot_escalate(admin_key):
    """A derived key cannot have scopes the parent doesn't have."""
    _, parent = admin_key
    with pytest.raises(PermissionError):
        downscope(parent, {Scope.CODE_EXECUTE})  # parent doesn't have CODE_EXECUTE


def test_downscoped_key_respects_ttl(admin_key):
    """A derived key expires after its TTL."""
    import time
    _, parent = admin_key
    parent.scopes.add(Scope.CODE_EXECUTE)
    raw_derived, derived = downscope(parent, {Scope.CODE_EXECUTE}, ttl_seconds=1)
    assert derived.is_valid()
    time.sleep(2)
    assert not derived.is_valid()  # Should be expired
```

**Expected Token Savings:** Not applicable — access control verification
**Environment:** `pip install pytest fastapi`

---

## Comparison Table

| Option | Scope Model | Hierarchy Support | Downscoping | Tool-Level Check | Audit Log |
|--------|------------|-------------------|-------------|-----------------|-----------|
| 1: Key definition | Enum set | No | No | No | No |
| 2: FastAPI deps | Enum set | No | No | No | No |
| 3: Downscoping | Enum + hierarchy | Yes | Yes | No | No |
| 4: Tool-level | Per-tool map | No | No | Yes | No |
| 5: Audit logging | Any | N/A | N/A | N/A | Yes |
| 6: Test suite | All above | Verified | Verified | N/A | N/A |
