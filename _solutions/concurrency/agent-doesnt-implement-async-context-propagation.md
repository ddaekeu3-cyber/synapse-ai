---
layout: solution
title: "Agent Doesn't Implement Async Context Propagation"
category: concurrency
description: "Use contextvars.ContextVar to propagate request-scoped data (trace IDs, user identity, deadlines) through async tasks without threading globals or explicit parameter passing."
tags: [concurrency, contextvars, async, tracing, python]
---

# Agent Doesn't Implement Async Context Propagation

When an async agent spawns subtasks, request-scoped data (trace ID, user ID, deadline) disappears unless explicitly threaded through every function signature. `contextvars.ContextVar` solves this: each task inherits a copy of the caller's context automatically, so metadata flows through the call graph without parameter pollution.

## Option 1: Basic ContextVar for Trace ID

```python
import anthropic
import asyncio
import contextvars
import uuid

client = anthropic.AsyncAnthropic()

# Declare context variables at module level
request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="none")
user_id:    contextvars.ContextVar[str] = contextvars.ContextVar("user_id",    default="anonymous")

async def call_model(prompt: str) -> str:
    rid = request_id.get()
    uid = user_id.get()
    print(f"[{rid}] user={uid} calling model")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

async def handle_request(prompt: str, uid: str) -> str:
    # Set context for this request; inherited by all subtasks
    request_id.set(uuid.uuid4().hex[:8])
    user_id.set(uid)
    result = await call_model(prompt)
    return result

async def main():
    # Concurrent requests each get isolated context
    results = await asyncio.gather(
        handle_request("What is a coroutine?", "alice"),
        handle_request("Explain asyncio.gather", "bob"),
    )
    for r in results:
        print(r[:80])

asyncio.run(main())

# Expected Token Savings: N/A (observability pattern, not cost-focused)
# Environment: any async Python 3.7+; no external dependencies
```

## Option 2: Context Propagation Through asyncio.create_task

```python
import anthropic
import asyncio
import contextvars
import uuid
import time

client = anthropic.AsyncAnthropic()

trace_id:   contextvars.ContextVar[str]   = contextvars.ContextVar("trace_id")
deadline:   contextvars.ContextVar[float] = contextvars.ContextVar("deadline", default=float("inf"))

async def fetch_context(topic: str) -> str:
    """Simulates a tool call that respects the caller's deadline."""
    remaining = deadline.get() - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError(f"[{trace_id.get()}] Deadline exceeded before tool call")
    print(f"[{trace_id.get()}] fetching context for '{topic}' ({remaining:.1f}s left)")
    await asyncio.sleep(0.05)  # simulate I/O
    return f"Context about {topic}"

async def summarize(topic: str) -> str:
    ctx_text = await fetch_context(topic)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarize: {ctx_text}"}],
    )
    return resp.content[0].text

async def handle(prompt: str, timeout_s: float = 10.0) -> str:
    trace_id.set(uuid.uuid4().hex[:8])
    deadline.set(time.monotonic() + timeout_s)

    # create_task copies the current Context automatically
    task = asyncio.create_task(summarize(prompt))
    try:
        return await asyncio.wait_for(task, timeout=timeout_s)
    except asyncio.TimeoutError:
        print(f"[{trace_id.get()}] Request timed out")
        raise

async def main():
    results = await asyncio.gather(
        handle("async Python patterns", timeout_s=15.0),
        handle("distributed tracing", timeout_s=15.0),
        return_exceptions=True,
    )
    for r in results:
        print(r[:80] if isinstance(r, str) else r)

asyncio.run(main())

# Expected Token Savings: N/A; deadline propagation prevents wasted calls on expired requests
# Environment: Python 3.7+; asyncio.create_task copies context automatically
```

## Option 3: Context Tokens for Cleanup (Token-Based Reset)

```python
import anthropic
import asyncio
import contextvars
import uuid
from contextlib import asynccontextmanager

client = anthropic.AsyncAnthropic()

auth_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("auth_token", default=None)
tenant_id:  contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant_id",  default=None)
span_id:    contextvars.ContextVar[str]        = contextvars.ContextVar("span_id",    default="root")

@asynccontextmanager
async def request_context(token: str, tenant: str):
    """Scope context variables to the lifetime of a request block."""
    tok1 = auth_token.set(token)
    tok2 = tenant_id.set(tenant)
    tok3 = span_id.set(uuid.uuid4().hex[:6])
    try:
        yield
    finally:
        auth_token.reset(tok1)
        tenant_id.reset(tok2)
        span_id.reset(tok3)

async def call_with_auth(prompt: str) -> str:
    auth = auth_token.get()
    tenant = tenant_id.get()
    sid = span_id.get()
    if not auth:
        raise PermissionError("No auth token in context")
    print(f"[span={sid}] tenant={tenant} auth={auth[:6]}...")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

async def handle_tenant_request(prompt: str, token: str, tenant: str) -> str:
    async with request_context(token, tenant):
        return await call_with_auth(prompt)

async def main():
    results = await asyncio.gather(
        handle_tenant_request("What is RBAC?", "tok_abc123", "tenant_acme"),
        handle_tenant_request("Explain JWT",   "tok_xyz789", "tenant_globex"),
    )
    for r in results:
        print(r[:80])
    # Verify context is cleaned up
    print(f"After requests: auth={auth_token.get()}, tenant={tenant_id.get()}")

asyncio.run(main())

# Expected Token Savings: N/A; prevents auth/tenant bleed-through between concurrent requests
# Environment: multi-tenant async APIs; context manager ensures cleanup on exception too
```

## Option 4: Propagating Context to Thread Pool (run_in_executor)

```python
import anthropic
import asyncio
import contextvars
import concurrent.futures
import uuid

client = anthropic.AsyncAnthropic()

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="none")
log_level:  contextvars.ContextVar[str] = contextvars.ContextVar("log_level",  default="INFO")

def cpu_bound_preprocessing(text: str) -> str:
    """Runs in thread pool; needs context for logging."""
    rid = request_id.get()
    lvl = log_level.get()
    print(f"[thread][{rid}][{lvl}] preprocessing: {text[:30]}")
    # Simulate CPU-bound work
    return text.upper().strip()

async def process_request(prompt: str, rid: str, level: str = "DEBUG") -> str:
    request_id.set(rid)
    log_level.set(level)

    # copy_context() captures current ContextVar state for the thread
    ctx = contextvars.copy_context()
    loop = asyncio.get_event_loop()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        preprocessed = await loop.run_in_executor(
            pool,
            ctx.run,           # run the function inside copied context
            cpu_bound_preprocessing,
            prompt,
        )

    print(f"[async][{request_id.get()}] calling model with preprocessed input")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": preprocessed}],
    )
    return resp.content[0].text

async def main():
    results = await asyncio.gather(
        process_request("what is asyncio?", uuid.uuid4().hex[:6], "DEBUG"),
        process_request("explain GIL",      uuid.uuid4().hex[:6], "INFO"),
    )
    for r in results:
        print(r[:80])

asyncio.run(main())

# Expected Token Savings: N/A; ensures context integrity when mixing async and thread pool work
# Environment: Python 3.7+; contextvars.copy_context() + ctx.run() for thread propagation
```

## Option 5: Middleware-Style Context Injection (FastAPI)

```python
import anthropic
import asyncio
import contextvars
import uuid
from typing import Callable

# Simulated FastAPI-style middleware without importing FastAPI
client = anthropic.AsyncAnthropic()

request_id:    contextvars.ContextVar[str] = contextvars.ContextVar("request_id")
authenticated: contextvars.ContextVar[bool] = contextvars.ContextVar("authenticated", default=False)
user_role:     contextvars.ContextVar[str]  = contextvars.ContextVar("user_role",     default="guest")

class Request:
    def __init__(self, path: str, headers: dict):
        self.path = path
        self.headers = headers

class Response:
    def __init__(self, body: str, status: int = 200):
        self.body = body
        self.status = status

async def context_middleware(request: Request, call_next: Callable) -> Response:
    """Sets request-scoped context before passing to handler."""
    rid = request.headers.get("X-Request-ID", uuid.uuid4().hex[:8])
    token = request.headers.get("Authorization", "")

    request_id.set(rid)
    authenticated.set(token.startswith("Bearer valid-"))
    user_role.set("admin" if "admin" in token else "user")

    print(f"[middleware] rid={rid} auth={authenticated.get()} role={user_role.get()}")
    response = await call_next(request)
    response.body = f"[{rid}] {response.body}"
    return response

async def agent_handler(request: Request) -> Response:
    if not authenticated.get():
        return Response("Unauthorized", status=401)

    role = user_role.get()
    model = "claude-opus-4-6" if role == "admin" else "claude-haiku-4-5-20251001"
    print(f"[handler] role={role} -> model={model}")

    resp = await client.messages.create(
        model=model,
        max_tokens=128,
        messages=[{"role": "user", "content": request.path}],
    )
    return Response(resp.content[0].text)

async def dispatch(request: Request) -> Response:
    return await context_middleware(request, agent_handler)

async def main():
    reqs = [
        Request("Explain OAuth2", {"X-Request-ID": "req-001", "Authorization": "Bearer valid-admin-tok"}),
        Request("What is REST?",  {"X-Request-ID": "req-002", "Authorization": "Bearer valid-user-tok"}),
        Request("Who are you?",   {"Authorization": "bad-token"}),
    ]
    results = await asyncio.gather(*[dispatch(r) for r in reqs])
    for r in results:
        print(f"[{r.status}] {r.body[:80]}")

asyncio.run(main())

# Expected Token Savings: Admin routes get Opus; guest/user gets Haiku; context-driven routing
# Environment: async web frameworks; pattern applies directly to FastAPI/Starlette middleware
```

## Option 6: Distributed Span Context with SQLite Audit Trail

```python
import anthropic
import asyncio
import contextvars
import sqlite3
import uuid
import time

client = anthropic.AsyncAnthropic()
DB = "spans.db"

trace_id:  contextvars.ContextVar[str] = contextvars.ContextVar("trace_id",  default="none")
parent_id: contextvars.ContextVar[str] = contextvars.ContextVar("parent_id", default="none")
span_id:   contextvars.ContextVar[str] = contextvars.ContextVar("span_id",   default="none")

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS spans (
            trace_id TEXT, span_id TEXT, parent_id TEXT,
            operation TEXT, start_ts REAL, end_ts REAL,
            input_tokens INTEGER, output_tokens INTEGER
        )
    """)
    con.commit(); con.close()

def record_span(op: str, start: float, inp: int = 0, out: int = 0):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?)", (
        trace_id.get(), span_id.get(), parent_id.get(),
        op, start, time.time(), inp, out,
    ))
    con.commit(); con.close()

async def agent_step(prompt: str, step_name: str) -> str:
    # Create a child span: preserve parent context, set new span_id
    parent = span_id.get()
    parent_id.set(parent)
    span_id.set(uuid.uuid4().hex[:8])

    start = time.time()
    print(f"[{trace_id.get()}] span={span_id.get()} parent={parent} op={step_name}")

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    record_span(step_name, start, resp.usage.input_tokens, resp.usage.output_tokens)
    return resp.content[0].text

async def run_pipeline(user_query: str):
    # Root span
    trace_id.set(uuid.uuid4().hex[:12])
    span_id.set(uuid.uuid4().hex[:8])
    parent_id.set("root")
    start = time.time()

    step1 = await agent_step(f"Extract key concepts from: {user_query}", "extract")
    step2 = await agent_step(f"Summarize these concepts: {step1}", "summarize")
    record_span("pipeline", start)

    # Print audit trail
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT span_id, parent_id, operation, ROUND(end_ts-start_ts,3), input_tokens "
        "FROM spans WHERE trace_id=? ORDER BY start_ts",
        (trace_id.get(),)
    ).fetchall()
    con.close()
    print("\nSpan audit trail:")
    for row in rows:
        print(f"  span={row[0]} parent={row[1]} op={row[2]} dur={row[3]}s tok={row[4]}")
    return step2

init_db()
result = asyncio.run(run_pipeline("How do distributed systems handle network partitions?"))
print(f"\nFinal: {result[:120]}")

# Expected Token Savings: Full span audit enables per-operation cost attribution and optimization
# Environment: any async Python; SQLite stores distributed trace for post-hoc analysis
```

## Comparison

| Option | Propagation Target | Key Feature | Use Case |
|--------|-------------------|-------------|----------|
| 1 — Basic ContextVar | Coroutines (auto-inherited) | Zero boilerplate | Simple trace ID / user ID flow |
| 2 — create_task | Child tasks | Deadline enforcement | Timeout-aware subtask trees |
| 3 — Token-based reset | Scoped block | Clean teardown via `reset()` | Multi-tenant: prevent context bleed |
| 4 — Thread pool | `run_in_executor` threads | `copy_context().run()` | Mixed async + CPU-bound work |
| 5 — Middleware | Web request lifecycle | Role/auth-driven model selection | FastAPI / Starlette request handling |
| 6 — Distributed spans | Cross-coroutine + SQLite | Full audit trail per trace | Observability and cost attribution |
