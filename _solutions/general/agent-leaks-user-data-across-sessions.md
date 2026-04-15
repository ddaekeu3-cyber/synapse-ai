---
layout: solution
title: "Agent Leaks User Data Across Sessions — Shared In-Memory State"
category: general
description: "Agent stores user data in a module-level dict. User A's session writes to it. User B's session reads from it. User B sees User A's name, preferences, and conversation history. In-process global state is shared across all concurrent users."
tags: [general, data-leak, session-isolation, multi-tenant, privacy, global-state, shared-memory, security]
---

# Agent Leaks User Data Across Sessions — Shared In-Memory State

## Symptom
- User B sees User A's name or conversation history in responses
- Agent says "As you mentioned earlier, your name is Alice" to a user named Bob
- Global dict or list accumulates data from multiple users without isolation
- Module-level variable modified in one request is visible in all subsequent requests
- Tool call uses previous user's context — sends email to wrong person
- Privacy incident: one user's query returns another user's data

## Root Cause
Python module-level variables, class variables, and any state not scoped to a request or session are shared across all concurrent users in the same process. A FastAPI handler that reads from `user_data["name"]` without a session key mixes all users into the same dict. `asyncio` doesn't isolate coroutines — they share the same memory space. Multi-threaded servers share global state unless explicitly synchronized and namespaced per session.

## Fix

### Option 1: Scope all state to session ID — never use global dicts

```python
from dataclasses import dataclass, field
from typing import Any
import uuid
import time

@dataclass
class SessionState:
    """All state for a single user session — never shared with other sessions"""
    session_id: str
    user_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    # Session-scoped data — isolated per user
    conversation_history: list[dict] = field(default_factory=list)
    user_preferences: dict = field(default_factory=dict)
    task_state: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)

    def touch(self):
        self.last_active = time.time()

class SessionStore:
    """
    Thread-safe session storage — each session completely isolated.
    Sessions are identified by session_id, never shared across users.
    """

    def __init__(self, ttl_seconds: int = 3600):
        self._sessions: dict[str, SessionState] = {}
        self._ttl = ttl_seconds
        self._lock = __import__("threading").Lock()

    def create(self, user_id: str | None = None) -> SessionState:
        session_id = str(uuid.uuid4())
        session = SessionState(session_id=session_id, user_id=user_id)
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> SessionState | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.touch()
            return session

    def get_or_create(self, session_id: str, user_id: str | None = None) -> SessionState:
        session = self.get(session_id)
        if session is None:
            session = SessionState(session_id=session_id, user_id=user_id)
            with self._lock:
                self._sessions[session_id] = session
        return session

    def delete(self, session_id: str):
        with self._lock:
            self._sessions.pop(session_id, None)

    def prune_expired(self):
        """Remove sessions that have been idle beyond TTL"""
        now = time.time()
        with self._lock:
            expired = [
                sid for sid, s in self._sessions.items()
                if now - s.last_active > self._ttl
            ]
            for sid in expired:
                del self._sessions[sid]
        if expired:
            print(f"Pruned {len(expired)} expired sessions")

# One global store — all session data isolated by session_id:
sessions = SessionStore(ttl_seconds=3600)

# WRONG — global state shared across all users:
# conversation_history = []  # ALL users share this!
# user_name = ""             # Last writer wins!

# RIGHT — session-scoped state:
def add_message(session_id: str, role: str, content: str):
    session = sessions.get(session_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found")
    session.conversation_history.append({"role": role, "content": content})
```

### Option 2: FastAPI dependency injection — session per request

```python
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.security import HTTPBearer
from typing import Annotated
import jwt

app = FastAPI()

sessions = SessionStore()

async def get_session(
    x_session_id: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> SessionState:
    """
    FastAPI dependency — injects the correct session for each request.
    Every request gets its own isolated SessionState.
    No global state accessible to handlers.
    """
    if x_session_id:
        session = sessions.get(x_session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found or expired")
        return session

    # Create new session from auth token
    if authorization:
        try:
            token = authorization.replace("Bearer ", "")
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            user_id = payload["sub"]
            session = sessions.create(user_id=user_id)
            return session
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")

    # Anonymous session
    return sessions.create()

@app.post("/chat")
async def chat(
    request: dict,
    session: Annotated[SessionState, Depends(get_session)]
) -> dict:
    """
    Handler receives isolated session — no global state possible.
    User A's handler and User B's handler have completely separate SessionState objects.
    """
    user_message = request.get("message", "")

    # All state goes through session — never global vars:
    session.conversation_history.append({"role": "user", "content": user_message})

    response = await call_llm(
        history=session.conversation_history,  # Session-specific history
        user_preferences=session.user_preferences  # Session-specific prefs
    )

    session.conversation_history.append({"role": "assistant", "content": response})

    return {
        "response": response,
        "session_id": session.session_id
    }
```

### Option 3: asyncio context variables — per-coroutine state

```python
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

# ContextVar is isolated per asyncio Task — perfect for coroutine-per-request patterns
current_session_id: ContextVar[str] = ContextVar("current_session_id")
current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)

@dataclass
class RequestContext:
    session_id: str
    user_id: str | None
    request_id: str
    started_at: float

current_request: ContextVar[RequestContext | None] = ContextVar("current_request", default=None)

async def handle_request(session_id: str, user_id: str, user_message: str) -> str:
    """
    Set context variables at request start — they don't leak to other coroutines.
    """
    import uuid
    import time

    ctx = RequestContext(
        session_id=session_id,
        user_id=user_id,
        request_id=str(uuid.uuid4()),
        started_at=time.time()
    )

    # Set context vars — only visible to this coroutine and its children
    token_session = current_session_id.set(session_id)
    token_user = current_user_id.set(user_id)
    token_ctx = current_request.set(ctx)

    try:
        return await process_message(user_message)
    finally:
        # Reset — context vars don't carry over to next request on same event loop
        current_session_id.reset(token_session)
        current_user_id.reset(token_user)
        current_request.reset(token_ctx)

async def process_message(message: str) -> str:
    """Can safely access context vars — they're isolated to this coroutine"""
    session_id = current_session_id.get()  # Always this user's session
    user_id = current_user_id.get()

    # No risk of seeing another user's session_id here
    session = sessions.get(session_id)
    return await call_llm(session.conversation_history, message)
```

### Option 4: Audit existing code for global state leaks

```python
import ast
import sys
from pathlib import Path

class GlobalStateAuditor(ast.NodeVisitor):
    """
    AST visitor that identifies potential global state mutations.
    Run this in CI to catch session isolation bugs before they reach production.
    """

    def __init__(self, filename: str):
        self.filename = filename
        self.violations: list[dict] = []
        self._global_names: set[str] = set()

    def visit_Global(self, node: ast.Global):
        """Track variables declared global"""
        for name in node.names:
            self._global_names.add(name)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        """Flag module-level assignments to mutable types"""
        # Only care about module-level (not inside functions/classes)
        for target in node.targets:
            if isinstance(target, ast.Name):
                # Check if value is a mutable type
                if isinstance(node.value, (ast.Dict, ast.List, ast.Set)):
                    self.violations.append({
                        "type": "mutable_module_level",
                        "name": target.id,
                        "line": node.lineno,
                        "file": self.filename,
                        "issue": f"Module-level mutable {type(node.value).__name__} '{target.id}' "
                                f"will be shared across all sessions"
                    })
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):
        """Flag augmented assignments to potentially global vars"""
        if isinstance(node.target, ast.Subscript):
            # dict[key] = value or list.append() patterns
            if isinstance(node.target.value, ast.Name):
                if node.target.value.id in self._global_names:
                    self.violations.append({
                        "type": "global_mutation",
                        "name": node.target.value.id,
                        "line": node.lineno,
                        "file": self.filename,
                        "issue": f"Mutation of global variable '{node.target.value.id}' "
                                f"will affect all concurrent sessions"
                    })
        self.generic_visit(node)

def audit_file_for_session_leaks(file_path: str) -> list[dict]:
    source = Path(file_path).read_text()
    tree = ast.parse(source, filename=file_path)
    auditor = GlobalStateAuditor(file_path)
    auditor.visit(tree)
    return auditor.violations

# Run in CI:
violations = []
for py_file in Path("src").rglob("*.py"):
    violations.extend(audit_file_for_session_leaks(str(py_file)))

if violations:
    print("SESSION ISOLATION VIOLATIONS:")
    for v in violations:
        print(f"  {v['file']}:{v['line']}: {v['issue']}")
    sys.exit(1)
```

### Option 5: Test for session data leakage

```python
import asyncio
import pytest
from fastapi.testclient import TestClient

def test_no_data_leakage_between_sessions(client: TestClient):
    """
    Verify user A's data cannot be seen by user B.
    Run this test before every release.
    """
    # User A: start session and share personal info
    response_a = client.post("/chat", json={"message": "My name is Alice Smith"})
    assert response_a.status_code == 200
    session_a = response_a.headers.get("x-session-id") or response_a.json().get("session_id")
    assert session_a

    # User B: start a completely separate session
    response_b = client.post("/chat", json={"message": "Hello, who am I?"})
    assert response_b.status_code == 200
    session_b = response_b.headers.get("x-session-id") or response_b.json().get("session_id")
    assert session_b

    # Sessions must be different
    assert session_a != session_b, "Two separate requests got the same session ID"

    # User B should NOT see Alice's name in any subsequent response
    for _ in range(3):
        response = client.post(
            "/chat",
            json={"message": "What is my name?"},
            headers={"x-session-id": session_b}
        )
        body = response.json().get("response", "").lower()
        assert "alice" not in body, (
            f"Session B leaked Session A's data. Response: {body[:200]}"
        )
        assert "smith" not in body, (
            f"Session B leaked Session A's surname. Response: {body[:200]}"
        )

def test_concurrent_sessions_isolated():
    """Test isolation under concurrent load"""
    import threading
    import uuid

    results = {}
    errors = []

    def user_session(user_name: str, user_id: str):
        try:
            with TestClient(app) as client:
                # Establish identity
                client.post("/chat", json={"message": f"My name is {user_name}"})
                # Ask for recall
                response = client.post("/chat", json={"message": "What is my name?"})
                result_text = response.json().get("response", "").lower()
                results[user_id] = result_text
        except Exception as e:
            errors.append(str(e))

    users = [("Alice", "user_a"), ("Bob", "user_b"), ("Charlie", "user_c")]
    threads = [threading.Thread(target=user_session, args=u) for u in users]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert not errors, f"Session errors: {errors}"

    # Each user should only see their own name
    for name, user_id in users:
        result = results.get(user_id, "")
        assert name.lower() in result, f"{name} not found in own session response"
        for other_name, other_id in users:
            if other_id != user_id:
                assert other_name.lower() not in result, (
                    f"Data leak: {name}'s session contains {other_name}'s name"
                )
```

### Option 6: Redis-backed session store for distributed isolation

```python
import redis
import json
import time
import uuid
from typing import Any

class RedisSessionStore:
    """
    Redis-backed session store — works across multiple agent instances.
    Sessions isolated by session_id — no in-process global state.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379", ttl: int = 3600):
        self.r = redis.Redis.from_url(redis_url, decode_responses=True)
        self.ttl = ttl
        self._prefix = "agent:session:"

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def create(self, user_id: str | None = None) -> str:
        session_id = str(uuid.uuid4())
        data = {
            "session_id": session_id,
            "user_id": user_id,
            "created_at": time.time(),
            "conversation_history": [],
            "context": {}
        }
        self.r.setex(self._key(session_id), self.ttl, json.dumps(data))
        return session_id

    def get(self, session_id: str) -> dict | None:
        raw = self.r.get(self._key(session_id))
        if raw is None:
            return None
        data = json.loads(raw)
        # Refresh TTL on access
        self.r.expire(self._key(session_id), self.ttl)
        return data

    def update(self, session_id: str, field: str, value: Any):
        """Update a specific field in the session"""
        data = self.get(session_id)
        if data is None:
            raise KeyError(f"Session {session_id} not found")
        data[field] = value
        self.r.setex(self._key(session_id), self.ttl, json.dumps(data))

    def append_message(self, session_id: str, role: str, content: str):
        """Append to conversation history atomically"""
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(self._key(session_id))
                    data = json.loads(pipe.get(self._key(session_id)) or "{}")
                    history = data.get("conversation_history", [])
                    history.append({"role": role, "content": content})
                    data["conversation_history"] = history
                    pipe.multi()
                    pipe.setex(self._key(session_id), self.ttl, json.dumps(data))
                    pipe.execute()
                    break
                except redis.WatchError:
                    continue  # Retry on concurrent modification

    def delete(self, session_id: str):
        self.r.delete(self._key(session_id))
```

## Session Isolation Checklist

| Pattern | Risk | Fix |
|---|---|---|
| Module-level `dict` for user data | Critical | Scope to session object |
| Class variable (not instance) for state | Critical | Use instance variables |
| `global` keyword in handler functions | High | Pass session as parameter |
| `asyncio.Queue` without per-user queues | High | One queue per session |
| Cache with user data, no session key | High | Include session_id in cache key |
| Logging user data without session ID | Medium | Add session_id to all log records |

## Expected Token Savings
Privacy incident → incident response → disclosure → rebuild trust: priceless to avoid
Session isolation implemented correctly: 0 risk of cross-session data exposure

## Environment
- Any multi-tenant agent serving more than one user; critical for all production agents — single-user dev setups are safe, but any concurrent user scenario requires explicit session isolation
- Source: direct experience; global state leaks are the most common privacy vulnerability in agent deployments that were originally designed for single-user use and later scaled to multiple users
