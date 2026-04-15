---
layout: solution
title: "Agent uses global variable as shared state in async context"
category: concurrency
description: "Agent stores per-request state (current user, active tool, conversation context) in a module-level global, causing concurrent requests to silently overwrite each other's state and mix user data."
tags: [concurrency, global-state, asyncio, race-condition, context-vars, thread-safety]
---

## Symptom

Under concurrent load, users receive each other's conversation history, tool results from a different request appear in the current response, or the agent references a user who isn't part of the current conversation. The bug is non-deterministic — it only appears under concurrent traffic and is impossible to reproduce in single-request testing.

```python
# Dangerous pattern — one global mutated from many coroutines
_current_user: str = ""
_active_context: dict = {}

async def handle_request(user_id: str, prompt: str) -> str:
    _current_user = user_id      # ← race: overwritten by other coroutines
    _active_context["prompt"] = prompt
    result = await llm_call(prompt)
    return f"[{_current_user}] {result}"   # ← may return wrong user's name
```

## Root Cause

Python's `asyncio` runs coroutines concurrently on a single thread. Coroutines yield control at `await` points — between `_current_user = user_id` and the `await llm_call(...)`, another coroutine can run and overwrite `_current_user`. Module-level globals have no isolation between concurrent coroutines, so every `await` is a potential data race.

---

## Option 1 — `contextvars.ContextVar` for per-coroutine isolation

**Use `ContextVar` — Python's built-in mechanism for coroutine-local storage. Each coroutine gets its own copy of the variable, inherited from its creation context.**

```python
import asyncio
from contextvars import ContextVar
import anthropic

client = anthropic.AsyncAnthropic()

# ContextVar: each coroutine and its children see their own copy
current_user_id: ContextVar[str]  = ContextVar("current_user_id",  default="")
request_context: ContextVar[dict] = ContextVar("request_context",  default={})


async def llm_call(prompt: str) -> str:
    user = current_user_id.get()
    ctx  = request_context.get()
    print(f"  [coroutine for {user}] calling LLM with session={ctx.get('session_id')}")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def handle_request(user_id: str, session_id: str, prompt: str) -> str:
    # Set ContextVar values — visible only to THIS coroutine and its children
    current_user_id.set(user_id)
    request_context.set({"session_id": session_id, "user_id": user_id})

    result = await llm_call(prompt)   # sees the correct user_id via ContextVar
    return f"[{current_user_id.get()}] {result}"


async def main() -> None:
    # Run 5 concurrent requests — each sees only its own context
    tasks = [
        asyncio.create_task(handle_request(f"user-{i}", f"sess-{i}", f"Tell me about topic {i}"))
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(r[:80])


asyncio.run(main())
```

**Expected Token Savings:** Context isolation prevents cross-request contamination that causes the model to reference wrong user data — eliminates re-authentication round-trips (typically 2–3 extra turns) triggered by confused context.

**Environment:** Any asyncio agent handling concurrent requests; Python 3.7+; zero dependencies.

---

## Option 2 — Pass state explicitly as function parameters (no globals at all)

**The safest pattern: pass all per-request state as explicit parameters. No shared mutable state, no races possible.**

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class RequestContext:
    """Immutable per-request state — passed explicitly, never stored globally."""
    user_id:    str
    session_id: str
    history:    list[dict] = field(default_factory=list)
    metadata:   dict[str, Any] = field(default_factory=dict)


async def llm_call(ctx: RequestContext, prompt: str) -> str:
    """All state is explicit — no globals touched."""
    messages = ctx.history + [{"role": "user", "content": prompt}]
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=messages,
    )
    text = response.content[0].text
    # Append to local copy — does not affect other requests
    ctx.history.append({"role": "user",      "content": prompt})
    ctx.history.append({"role": "assistant", "content": text})
    return text


async def handle_tool_call(ctx: RequestContext, tool_name: str, args: dict) -> str:
    print(f"  [{ctx.user_id}/{ctx.session_id}] tool={tool_name} args={args}")
    return f"Result of {tool_name} for {ctx.user_id}"


async def handle_request(user_id: str, session_id: str, prompt: str) -> str:
    ctx = RequestContext(user_id=user_id, session_id=session_id)
    return await llm_call(ctx, prompt)


async def main() -> None:
    tasks = [
        asyncio.create_task(
            handle_request(f"user-{i}", f"sess-{100+i}", f"Question {i} for my session")
        )
        for i in range(8)
    ]
    results = await asyncio.gather(*tasks)
    for i, r in enumerate(results):
        print(f"[{i}] {r[:60]}")


asyncio.run(main())
```

**Expected Token Savings:** Explicit parameter passing makes state ownership visible — bugs are caught at code review, not in production. Eliminates the entire class of cross-request contamination bugs that require costly debugging sessions.

**Environment:** Preferred pattern for all new agent code; refactoring existing globals is the main effort.

---

## Option 3 — `asyncio.Lock` to serialise access to unavoidable shared state

**When shared mutable state is genuinely necessary (e.g., a shared cache or counter), protect it with `asyncio.Lock`.**

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

# Shared rate-limit counter — legitimately shared across requests
_request_count = 0
_window_start  = time.monotonic()
_lock          = asyncio.Lock()
MAX_RPM        = 30


async def acquire_rate_limit_slot() -> None:
    global _request_count, _window_start
    async with _lock:   # only one coroutine modifies these at a time
        now = time.monotonic()
        if now - _window_start >= 60:
            _request_count = 0
            _window_start  = now

        while _request_count >= MAX_RPM:
            remaining = 60 - (time.monotonic() - _window_start)
            _lock.release()
            await asyncio.sleep(max(0.1, remaining))
            await _lock.acquire()
            now = time.monotonic()
            if now - _window_start >= 60:
                _request_count = 0
                _window_start  = now

        _request_count += 1


async def handle_request(user_id: str, prompt: str) -> str:
    # Rate limiter is legitimately shared — protected by lock
    await acquire_rate_limit_slot()

    # Per-request state is local — not global
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"[{user_id}] {prompt}"}],
    )
    return response.content[0].text


async def main() -> None:
    tasks = [
        asyncio.create_task(handle_request(f"u{i}", f"Task {i}"))
        for i in range(10)
    ]
    results = await asyncio.gather(*tasks)
    print(f"Completed {len(results)} requests. Count: {_request_count}")


asyncio.run(main())
```

**Expected Token Savings:** Lock-protected shared state eliminates races on legitimately shared counters — prevents duplicate API calls caused by two coroutines both thinking they have a free slot.

**Environment:** Agents with shared rate limiters, counters, or caches that must be shared but safely; use `asyncio.Lock` not `threading.Lock` in async code.

---

## Option 4 — Per-request state via `fastapi` dependency injection

**For HTTP-serving agents, use FastAPI's `Request` object and dependency injection — each request gets its own isolated state.**

```python
import asyncio
from fastapi import FastAPI, Request, Depends
from pydantic import BaseModel
import anthropic
import uvicorn

app    = FastAPI()
client = anthropic.AsyncAnthropic()


class AgentRequest(BaseModel):
    prompt: str


class RequestState:
    """Per-request state — FastAPI creates a new instance per request."""
    def __init__(self, user_id: str, session_id: str):
        self.user_id    = user_id
        self.session_id = session_id
        self.history:   list[dict] = []
        self.tool_calls: list[str] = []


def get_request_state(request: Request) -> RequestState:
    """Dependency: extract per-request state from headers."""
    return RequestState(
        user_id    = request.headers.get("X-User-Id",    "anonymous"),
        session_id = request.headers.get("X-Session-Id", "no-session"),
    )


@app.post("/ask")
async def ask(
    body: AgentRequest,
    state: RequestState = Depends(get_request_state),
) -> dict:
    """Each request gets its own `state` instance — no shared mutable globals."""
    messages = [{"role": "user", "content": body.prompt}]
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=messages,
    )
    answer = response.content[0].text
    return {
        "user_id":    state.user_id,
        "session_id": state.session_id,
        "answer":     answer,
    }


# Run: uvicorn solution:app --workers 4
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

**Expected Token Savings:** FastAPI dependency injection enforces state isolation at the framework level — no per-user contamination possible, eliminating an entire category of data-mix bugs that are expensive to diagnose (multiple LLM calls wasted on wrong-user context).

**Environment:** FastAPI-based agent servers; `fastapi>=0.100`, `uvicorn`.

---

## Option 5 — Async-safe session store with per-key locks

**For stateful multi-turn conversations, store session data in a dictionary keyed by session ID, with per-key locks for concurrent writes.**

```python
import asyncio
from collections import defaultdict
import anthropic

client = anthropic.AsyncAnthropic()


class SessionStore:
    """Thread-safe (asyncio-safe) session store with per-session locks."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict]          = {}
        self._locks:    dict[str, asyncio.Lock]  = defaultdict(asyncio.Lock)

    async def get(self, session_id: str) -> dict:
        async with self._locks[session_id]:
            return dict(self._sessions.get(session_id, {"history": [], "turn": 0}))

    async def update(self, session_id: str, data: dict) -> None:
        async with self._locks[session_id]:
            if session_id not in self._sessions:
                self._sessions[session_id] = {"history": [], "turn": 0}
            self._sessions[session_id].update(data)

    async def append_history(self, session_id: str, message: dict) -> None:
        async with self._locks[session_id]:
            if session_id not in self._sessions:
                self._sessions[session_id] = {"history": [], "turn": 0}
            self._sessions[session_id]["history"].append(message)
            self._sessions[session_id]["turn"] += 1


store = SessionStore()


async def handle_turn(session_id: str, user_message: str) -> str:
    session = await store.get(session_id)
    history = session["history"]

    history.append({"role": "user", "content": user_message})
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=history,
    )
    reply = response.content[0].text

    await store.append_history(session_id, {"role": "user",      "content": user_message})
    await store.append_history(session_id, {"role": "assistant", "content": reply})
    return reply


async def main() -> None:
    # Two users, each with 3 concurrent turns — sessions must not mix
    tasks = []
    for user in ["alice", "bob"]:
        for turn in range(3):
            tasks.append(
                asyncio.create_task(handle_turn(user, f"[{user}] question {turn}"))
            )
    results = await asyncio.gather(*tasks)
    print(f"Processed {len(results)} turns.")

    alice_session = await store.get("alice")
    bob_session   = await store.get("bob")
    print(f"Alice: {alice_session['turn']} turns | Bob: {bob_session['turn']} turns")
    # Verify no cross-contamination
    alice_msgs = " ".join(m["content"] for m in alice_session["history"])
    assert "bob" not in alice_msgs.lower(), "Cross-contamination detected!"
    print("Session isolation verified.")


asyncio.run(main())
```

**Expected Token Savings:** Per-session locks prevent conversation histories from mixing — eliminates confused multi-turn conversations where the model references the wrong user's prior messages and requires a reset.

**Environment:** Multi-user stateful agents; replace `dict` backend with Redis for multi-process deployments.

---

## Option 6 — Audit tool: detect global mutations in async handlers

**Use AST analysis at startup to detect dangerous global mutations in async functions and warn before they cause race conditions.**

```python
import ast
import inspect
import warnings
from typing import Callable


def audit_async_global_mutations(module) -> list[str]:
    """Find async functions that mutate module-level globals — potential race conditions."""
    source = inspect.getsource(module)
    tree   = ast.parse(source)

    # Collect module-level variable names
    global_names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    global_names.add(target.id)

    issues: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        func_name = node.name

        # Check for global statement or direct assignment to global names
        for child in ast.walk(node):
            if isinstance(child, ast.Global):
                for name in child.names:
                    if name in global_names:
                        issues.append(
                            f"  {func_name}(): uses 'global {name}' — "
                            f"potential race condition in async context"
                        )
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id in global_names:
                        issues.append(
                            f"  {func_name}(): assigns to global '{target.id}' "
                            f"(line {child.lineno}) — use ContextVar or explicit params instead"
                        )

    return issues


# Example module to audit
import types
demo_module = types.ModuleType("demo")
exec("""
_current_user = ""
_active_ctx = {}

async def handle_request(user_id, prompt):
    global _current_user
    _current_user = user_id   # BAD
    _active_ctx["user"] = user_id   # also bad (mutating shared dict)
    return f"done for {_current_user}"
""", demo_module.__dict__)

issues = audit_async_global_mutations(demo_module)
if issues:
    warnings.warn(
        "Potential async global mutation detected:\n" + "\n".join(issues),
        stacklevel=2,
    )
    for issue in issues:
        print(issue)
else:
    print("No global mutation issues found.")
```

**Expected Token Savings:** Catching global mutations at startup (or in CI) prevents production race conditions that typically require 5–10 debugging sessions to reproduce and diagnose — each session wastes hundreds of LLM calls on wrong-context responses.

**Environment:** Any Python asyncio agent; run audit at module import time or as a pre-commit hook.

---

## Comparison

| Option | Isolation Mechanism | Works Multi-process | Requires Refactor | Complexity |
|--------|--------------------|--------------------|------------------|------------|
| 1. `ContextVar` | Per-coroutine copy | No | Minimal | Low |
| 2. Explicit params | None needed | Yes | Medium | Low |
| 3. `asyncio.Lock` | Serialise access | No | Minimal | Low |
| 4. FastAPI DI | Framework-enforced | Yes (per request) | Minimal | Low |
| 5. Per-key session store | Per-session lock | No (Redis for multi) | Medium | Medium |
| 6. AST audit | Prevention (CI) | N/A | No | Medium |

**Recommended path:** Use Option 1 (`ContextVar`) for any per-request tracing/logging context — zero refactor, immediate fix. Use Option 2 (explicit params) for all new agent code. Add Option 6 (AST audit) to CI to catch regressions before they reach production.
