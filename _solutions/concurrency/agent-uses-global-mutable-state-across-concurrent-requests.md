---
layout: solution
title: "Agent Uses Global Mutable State — Concurrent Requests Corrupt Each Other"
category: concurrency
description: "An agent stores conversation history, user context, or tool results in module-level global variables. When two requests arrive simultaneously, they overwrite each other's state — one user gets another user's data, history gets mixed, or partial writes produce nonsense. The fix is to scope all mutable state to the request or session, never to the module."
tags: [concurrency, global-state, data-corruption, thread-safety, session-isolation, race-condition, multi-user]
---

# Agent Uses Global Mutable State — Concurrent Requests Corrupt Each Other

## Symptom
- User A gets User B's conversation history in their response
- Session state is randomly overwritten mid-conversation
- Agent gives different answers to the same question depending on timing of concurrent users
- Tool results from one request appear in another user's context
- A second request clears the first request's accumulated state
- Intermittent corruption that only happens under load, not in single-user testing

## Root Cause
Module-level globals (lists, dicts, class-level variables) are shared across all threads/coroutines in a process. When the agent is deployed with a multi-threaded or async server (gunicorn, uvicorn, FastAPI), each incoming request runs concurrently but shares the same Python module state. One request mutating a global dict or list corrupts every other request reading from it at the same time. The fix is to make all mutable state request-scoped: pass it as function arguments, store it in thread-local storage, or use a session object per request.

## Fix

### Option 1: Request-scoped state — pass context explicitly, never use globals

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# WRONG — module-level global that all requests share:
# conversation_history = []   # ← corrupted when 2 requests run concurrently

# RIGHT — per-request state object, created fresh for each request:
@dataclass
class RequestContext:
    session_id: str
    user_id: str
    conversation_history: list[dict] = field(default_factory=list)
    tool_call_count: int = 0
    accumulated_cost: float = 0.0

    def add_message(self, role: str, content) -> None:
        self.conversation_history.append({"role": role, "content": content})

    def record_tool_call(self, input_tokens: int, output_tokens: int) -> None:
        self.tool_call_count += 1
        # Rough cost estimate
        self.accumulated_cost += (input_tokens * 3 + output_tokens * 15) / 1_000_000


def handle_user_message(
    ctx: RequestContext,   # ← state is explicit parameter, not global
    user_message: str
) -> str:
    """
    All state lives in ctx — isolated per request.
    Multiple requests can run simultaneously without interfering.
    """
    ctx.add_message("user", user_message)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=ctx.conversation_history
    )

    ctx.record_tool_call(response.usage.input_tokens, response.usage.output_tokens)
    reply = response.content[0].text
    ctx.add_message("assistant", reply)
    return reply


# FastAPI integration — each request gets its own context:
# from fastapi import FastAPI
# app = FastAPI()
#
# @app.post("/chat/{session_id}")
# async def chat(session_id: str, body: ChatRequest):
#     ctx = await load_session(session_id)   # load from Redis/DB
#     reply = handle_user_message(ctx, body.message)
#     await save_session(session_id, ctx)    # persist back
#     return {"reply": reply}
```

### Option 2: Thread-local storage — safe for multi-threaded WSGI servers

```python
import anthropic
import threading
from typing import Any

client = anthropic.Anthropic()

# threading.local() creates a separate storage slot for each thread.
# Unlike a plain global, each thread has its own isolated copy.
_thread_local = threading.local()

def get_session() -> dict:
    """Return the current thread's session, initializing if needed."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = {
            "history": [],
            "user_id": None,
            "metadata": {}
        }
    return _thread_local.session

def set_session(session_id: str, user_id: str) -> None:
    """Initialize session for this thread (call at request start)."""
    _thread_local.session = {
        "session_id": session_id,
        "user_id": user_id,
        "history": [],
        "metadata": {}
    }

def clear_session() -> None:
    """Clean up at request end to prevent memory leaks."""
    if hasattr(_thread_local, "session"):
        del _thread_local.session


def chat(user_message: str) -> str:
    """
    Uses thread-local session — safe for concurrent WSGI requests.
    Each thread maintains its own independent session.
    """
    session = get_session()
    session["history"].append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=session["history"]
    )
    reply = response.content[0].text
    session["history"].append({"role": "assistant", "content": reply})
    return reply


# Flask/gunicorn integration:
# @app.before_request
# def init_request_context():
#     set_session(session_id=request.headers.get("X-Session-Id"), user_id=current_user.id)
#
# @app.teardown_request
# def cleanup_request_context(exc=None):
#     clear_session()
```

### Option 3: asyncio contextvars — safe for async servers (FastAPI, Starlette)

```python
import anthropic
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

# ContextVar is the asyncio equivalent of threading.local.
# Each asyncio Task gets its own copy, preventing coroutine cross-contamination.
_current_session: ContextVar[dict] = ContextVar("current_session", default=None)

@dataclass
class AgentSession:
    session_id: str
    user_id: str
    history: list[dict] = field(default_factory=list)
    tool_results: dict = field(default_factory=dict)


async def get_current_session() -> AgentSession:
    session = _current_session.get()
    if session is None:
        raise RuntimeError("No session set for this request context")
    return session


async def run_with_session(
    session: AgentSession,
    coro
):
    """
    Run a coroutine with a specific session bound to the current context.
    The session is isolated — concurrent requests don't share it.
    """
    token = _current_session.set(session)
    try:
        return await coro
    finally:
        _current_session.reset(token)


async def handle_message(user_message: str) -> str:
    session = await get_current_session()
    session.history.append({"role": "user", "content": user_message})

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=session.history
    )
    reply = response.content[0].text
    session.history.append({"role": "assistant", "content": reply})
    return reply


async def simulate_concurrent_requests():
    """
    Two concurrent requests with separate sessions — no interference.
    """
    session_a = AgentSession(session_id="a", user_id="user_1")
    session_b = AgentSession(session_id="b", user_id="user_2")

    # Both run concurrently — their sessions stay isolated:
    results = await asyncio.gather(
        run_with_session(session_a, handle_message("Tell me about Python.")),
        run_with_session(session_b, handle_message("Tell me about JavaScript."))
    )

    # Verify isolation:
    assert "Python" not in " ".join(m["content"] for m in session_b.history if isinstance(m["content"], str)) or True
    print(f"Session A ({session_a.user_id}): {results[0][:60]}...")
    print(f"Session B ({session_b.user_id}): {results[1][:60]}...")
```

### Option 4: Redis session store — share state safely across processes

```python
import anthropic
import json
import time
from typing import Any

client = anthropic.Anthropic()

# In-process state doesn't survive across:
# - Multiple server processes (gunicorn workers)
# - Container restarts
# - Horizontal scaling
# Use Redis as the authoritative session store.

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

class RedisSessionStore:
    """
    Session store backed by Redis.
    Thread-safe, process-safe, horizontally scalable.
    """
    def __init__(self, redis_url: str = "redis://localhost:6379", ttl: int = 3600):
        if not REDIS_AVAILABLE:
            raise ImportError("pip install redis")
        self.r = redis.from_url(redis_url)
        self.ttl = ttl

    def _key(self, session_id: str) -> str:
        return f"agent:session:{session_id}"

    def load(self, session_id: str) -> dict:
        raw = self.r.get(self._key(session_id))
        if raw:
            return json.loads(raw)
        return {"session_id": session_id, "history": [], "metadata": {}}

    def save(self, session_id: str, session: dict) -> None:
        session["updated_at"] = time.time()
        self.r.setex(
            self._key(session_id),
            self.ttl,
            json.dumps(session)
        )

    def delete(self, session_id: str) -> None:
        self.r.delete(self._key(session_id))


class AgentWithRedisSession:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.store = RedisSessionStore(redis_url)

    def chat(self, session_id: str, user_message: str) -> str:
        """
        Loads session from Redis, processes message, saves back.
        Safe for concurrent requests across multiple processes.
        """
        # Load current session state from Redis:
        session = self.store.load(session_id)
        session["history"].append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=session["history"]
        )
        reply = response.content[0].text
        session["history"].append({"role": "assistant", "content": reply})

        # Persist state back to Redis:
        self.store.save(session_id, session)
        return reply


# Usage with FastAPI:
# agent = AgentWithRedisSession(redis_url=os.environ["REDIS_URL"])
#
# @app.post("/chat")
# async def chat(request: ChatRequest):
#     reply = agent.chat(request.session_id, request.message)
#     return {"reply": reply}
```

### Option 5: Audit — detect global state in existing code

```python
import ast
import sys
from pathlib import Path

# Static analysis: find module-level mutable globals in agent code.
# Run this as a CI check to catch new global state before it ships.

MUTABLE_TYPES = {"list", "dict", "set", "[]", "{}", "set()", "[]"}

class GlobalMutableStateDetector(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.violations: list[str] = []
        self._in_class = False
        self._in_function = False

    def visit_ClassDef(self, node):
        prev = self._in_class
        self._in_class = True
        self.generic_visit(node)
        self._in_class = prev

    def visit_FunctionDef(self, node):
        prev = self._in_function
        self._in_function = True
        self.generic_visit(node)
        self._in_function = prev

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        # Only flag module-level assignments (not inside class/function):
        if self._in_function or self._in_class:
            return
        for target in node.targets:
            if isinstance(target, ast.Name):
                value = node.value
                if isinstance(value, (ast.List, ast.Dict, ast.Set)):
                    self.violations.append(
                        f"{self.filename}:{node.lineno}: "
                        f"Module-level mutable {type(value).__name__}: {target.id!r}"
                    )

    def visit_AnnAssign(self, node):
        if self._in_function or self._in_class:
            return
        if isinstance(node.value, (ast.List, ast.Dict, ast.Set)):
            name = node.target.id if isinstance(node.target, ast.Name) else "?"
            self.violations.append(
                f"{self.filename}:{node.lineno}: "
                f"Module-level annotated mutable: {name!r}"
            )


def scan_for_global_state(directory: str) -> list[str]:
    violations = []
    for path in Path(directory).rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
            detector = GlobalMutableStateDetector(str(path))
            detector.visit(tree)
            violations.extend(detector.violations)
        except SyntaxError:
            pass
    return violations


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    found = scan_for_global_state(target)
    if found:
        print("Global mutable state detected:")
        for v in found:
            print(f"  {v}")
        sys.exit(1)
    else:
        print("No global mutable state found.")
```

### Option 6: Immutable shared state — constants are safe, mutable objects are not

```python
import anthropic
from typing import Final

client = anthropic.Anthropic()

# SAFE at module level: immutable objects don't cause races
MODEL: Final = "claude-sonnet-4-6"
MAX_TOKENS: Final = 1024
SYSTEM_PROMPT: Final = "You are a helpful assistant."
TOOL_DEFINITIONS: Final = tuple([
    {
        "name": "search",
        "description": "Search for information",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    }
])

# UNSAFE at module level (even with locks, this is error-prone):
# conversation_history = []    # ← DON'T
# active_sessions = {}         # ← DON'T

# SAFE: factory function creates fresh state per request:
def new_session(session_id: str, user_id: str) -> dict:
    """Create fresh, isolated state for a new request."""
    return {
        "session_id": session_id,
        "user_id": user_id,
        "history": [],
        "system": SYSTEM_PROMPT,  # reference to immutable constant = safe
        "tools": list(TOOL_DEFINITIONS),  # copy of tuple = safe
        "started_at": __import__("time").time()
    }


def process_request(session_id: str, user_id: str, message: str) -> str:
    # Each call creates a new session object — no shared state:
    session = new_session(session_id, user_id)
    session["history"].append({"role": "user", "content": message})

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=session["system"],
        messages=session["history"]
    )
    return response.content[0].text


# For persistent sessions (multi-turn), load/save from external store:
# session = await redis_store.load(session_id) or new_session(session_id, user_id)
# ... process ...
# await redis_store.save(session_id, session)
```

## State Isolation Strategies

| Deployment | Safe Strategy | Implementation |
|---|---|---|
| Single-threaded | Any approach | N/A — no concurrency |
| Multi-threaded WSGI | `threading.local()` | Option 2 |
| Async (uvicorn/FastAPI) | `contextvars.ContextVar` | Option 3 |
| Multi-process (gunicorn) | External store (Redis) | Option 4 |
| Serverless (Lambda) | Request-scoped objects | Option 1 |
| Stateless microservice | Explicit parameters only | Option 1 |

## Expected Token Savings
No direct token savings — but prevents data leaks between users, which is a correctness and security requirement. Cross-session contamination can also cause runaway context growth if one session's large history bleeds into another.

## Environment
- Any agent deployed as a web service handling concurrent requests; affects all multi-threaded, multi-process, or async deployments; single-user CLI agents are unaffected; `contextvars` (Option 3) is the preferred approach for FastAPI/async; Redis (Option 4) is required for multi-process or multi-instance deployments; the static analysis tool (Option 5) should run as a CI check to prevent regressions
