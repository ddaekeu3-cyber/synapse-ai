---
title: "Agent Doesn't Implement Thread-Local Storage for Request Context Isolation"
description: "AI agents running in multi-threaded or asyncio environments share global state — loggers, metrics tags, user IDs, trace spans — across concurrent requests. Thread-local and context-var storage isolates per-request state so that request A's user ID is never visible to request B's tool calls, eliminating a class of data leakage and log contamination bugs that only manifest under concurrency."
date: 2025-02-15
difficulty: intermediate
category: concurrency
slug: agent-doesnt-implement-thread-local-storage-for-request-context-isolation
tags:
  - thread-local
  - contextvar
  - request-isolation
  - concurrency
  - context
  - asyncio
  - data-leakage
symptoms:
  - "User ID from request A appears in logs for request B when both execute concurrently"
  - "Trace span for one request is closed by a different concurrent request"
  - "A global variable modified by one tool call corrupts the state seen by another"
  - "Metrics labels from a previous request bleed into the current request's measurements"
  - "agent.current_user is read by tool B but was set by a different concurrent agent invocation"
---

## Problem

Python `threading.local()` and `contextvars.ContextVar` provide per-thread and per-asyncio-task storage respectively. Without them, mutable globals — `current_user`, `request_id`, `active_span`, `log_context` — are shared across all concurrent requests. In a server handling 100 concurrent users, request A's tool call may read `current_user` that was just overwritten by request B. `ContextVar` is the correct primitive for asyncio: each task gets its own copy, and child tasks inherit but do not share parent values.

---

## Solution 1: RequestContextStore — ContextVar-Based Per-Request Storage

```python
import contextvars
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class RequestContext:
    request_id: str
    user_id: Optional[str]
    session_id: Optional[str]
    started_at: float
    metadata: Dict[str, Any] = field(default_factory=dict)


_CTX: contextvars.ContextVar[Optional[RequestContext]] = contextvars.ContextVar(
    "agent_request_context", default=None
)


class RequestContextStore:
    """
    Per-request context stored in a ContextVar.
    Each asyncio Task (and child Tasks) inherits a copy of the context,
    so concurrent requests never see each other's context.

    Usage:
        async with RequestContextStore.scope(user_id="u-123"):
            # Any code in this scope (including tool calls) can read:
            ctx = RequestContextStore.current()
            logger.info("user=%s", ctx.user_id)
    """

    @staticmethod
    def from_contextmanager(user_id: Optional[str] = None,
                              session_id: Optional[str] = None,
                              **metadata) -> RequestContext:
        return RequestContext(
            request_id=str(uuid.uuid4()),
            user_id=user_id,
            session_id=session_id,
            started_at=time.monotonic(),
            metadata=metadata,
        )

    @staticmethod
    def set(ctx: RequestContext) -> contextvars.Token:
        return _CTX.set(ctx)

    @staticmethod
    def reset(token: contextvars.Token):
        _CTX.reset(token)

    @staticmethod
    def current() -> Optional[RequestContext]:
        return _CTX.get()

    @staticmethod
    def current_or_raise() -> RequestContext:
        ctx = _CTX.get()
        if ctx is None:
            raise RuntimeError(
                "No request context active. "
                "Wrap the call in RequestContextStore.scope()."
            )
        return ctx

    @staticmethod
    def request_id() -> Optional[str]:
        ctx = _CTX.get()
        return ctx.request_id if ctx else None

    @staticmethod
    def user_id() -> Optional[str]:
        ctx = _CTX.get()
        return ctx.user_id if ctx else None
```

---

## Solution 2: RequestContextMiddleware — Set Context at Request Entry Point

```python
import asyncio
import contextvars
import functools
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional


class RequestContextMiddleware:
    """
    Establishes a fresh RequestContext for every incoming request.
    All downstream code (tools, LLM calls, logging) reads context
    from the ContextVar without any explicit passing.

    Usage (as ASGI middleware):
        app = RequestContextMiddleware.wrap(app)

    Usage (as async context manager):
        async with RequestContextMiddleware.scope(user_id="u-123") as ctx:
            result = await agent.handle(query)
    """

    @staticmethod
    @asynccontextmanager
    async def scope(user_id: Optional[str] = None,
                     session_id: Optional[str] = None,
                     request_id: Optional[str] = None,
                     **metadata):
        ctx = RequestContext(
            request_id=request_id or str(uuid.uuid4()),
            user_id=user_id,
            session_id=session_id,
            started_at=__import__("time").monotonic(),
            metadata=metadata,
        )
        token = _CTX.set(ctx)
        try:
            yield ctx
        finally:
            _CTX.reset(token)

    @staticmethod
    def wrap_handler(fn: Callable) -> Callable:
        """Decorator that wraps an async handler function in a context scope."""
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            user_id = kwargs.pop("user_id", None)
            session_id = kwargs.pop("session_id", None)
            async with RequestContextMiddleware.scope(
                user_id=user_id, session_id=session_id
            ):
                return await fn(*args, **kwargs)
        return wrapper
```

---

## Solution 3: ContextAwareLogger — Automatically Include Request Context in Logs

```python
import logging
from typing import Any, Dict, Optional


class ContextAwareLogger:
    """
    Logging wrapper that automatically prepends the current request context
    (request_id, user_id) to every log message without explicit passing.

    Usage:
        logger = ContextAwareLogger(__name__)

        async with RequestContextMiddleware.scope(user_id="u-123"):
            logger.info("processing query")
            # Logs: "[req=abc123 user=u-123] processing query"
    """

    def __init__(self, name: str):
        self._logger = logging.getLogger(name)

    def _prefix(self) -> str:
        ctx = RequestContextStore.current()
        if ctx is None:
            return ""
        parts = [f"req={ctx.request_id[:8]}"]
        if ctx.user_id:
            parts.append(f"user={ctx.user_id}")
        if ctx.session_id:
            parts.append(f"session={ctx.session_id[:8]}")
        return "[" + " ".join(parts) + "] "

    def _extra(self) -> Dict[str, Any]:
        ctx = RequestContextStore.current()
        if ctx is None:
            return {}
        return {
            "request_id": ctx.request_id,
            "user_id": ctx.user_id,
            "session_id": ctx.session_id,
        }

    def debug(self, msg: str, *args, **kwargs):
        self._logger.debug(self._prefix() + msg, *args,
                            extra=self._extra(), **kwargs)

    def info(self, msg: str, *args, **kwargs):
        self._logger.info(self._prefix() + msg, *args,
                           extra=self._extra(), **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        self._logger.warning(self._prefix() + msg, *args,
                              extra=self._extra(), **kwargs)

    def error(self, msg: str, *args, **kwargs):
        self._logger.error(self._prefix() + msg, *args,
                            extra=self._extra(), **kwargs)
```

---

## Solution 4: ContextPropagatingTaskFactory — Preserve Context in Child Tasks

```python
import asyncio
import contextvars
from typing import Any, Callable


class ContextPropagatingTaskFactory:
    """
    Custom asyncio task factory that copies the current ContextVar snapshot
    into child tasks. Without this, tasks created with asyncio.create_task()
    in Python ≥ 3.7 already inherit the context snapshot — but this factory
    makes it explicit and adds optional parent-context linkage for tracing.

    Usage:
        loop = asyncio.get_event_loop()
        loop.set_task_factory(ContextPropagatingTaskFactory())

        async with RequestContextMiddleware.scope(user_id="u-123"):
            # Child task inherits user_id automatically:
            task = asyncio.create_task(tool_call())
    """

    def __call__(self, loop: asyncio.AbstractEventLoop,
                  coro) -> asyncio.Task:
        # Copy the current context snapshot into the new task
        ctx = contextvars.copy_context()
        task = loop.create_task(coro)
        # Python 3.7+ create_task already copies context; this is a no-op
        # for most cases but makes the intent explicit
        return task

    @staticmethod
    def run_in_context(fn: Callable, *args, **kwargs) -> Any:
        """Run a sync function in the current context snapshot."""
        ctx = contextvars.copy_context()
        return ctx.run(fn, *args, **kwargs)
```

---

## Solution 5: ThreadLocalRequestContext — Thread-Based Isolation for Sync Code

```python
import threading
import time
import uuid
from typing import Optional


class ThreadLocalRequestContext:
    """
    Thread-local request context for synchronous code (WSGI, threaded
    FastAPI handlers, background threads). Each thread maintains its
    own copy of the context independently.

    Usage:
        ctx = ThreadLocalRequestContext()

        # In request handler (one per thread):
        ctx.set(user_id="u-123", session_id="sess-abc")

        # Anywhere in the call stack on the same thread:
        uid = ctx.user_id()
        rid = ctx.request_id()

        # Always clear at the end of the request:
        ctx.clear()
    """

    def __init__(self):
        self._local = threading.local()

    def set(self, user_id: Optional[str] = None,
             session_id: Optional[str] = None,
             **metadata):
        self._local.request_id = str(uuid.uuid4())
        self._local.user_id = user_id
        self._local.session_id = session_id
        self._local.started_at = time.monotonic()
        self._local.metadata = metadata

    def request_id(self) -> Optional[str]:
        return getattr(self._local, "request_id", None)

    def user_id(self) -> Optional[str]:
        return getattr(self._local, "user_id", None)

    def session_id(self) -> Optional[str]:
        return getattr(self._local, "session_id", None)

    def elapsed_ms(self) -> Optional[float]:
        started = getattr(self._local, "started_at", None)
        if started is None:
            return None
        return (time.monotonic() - started) * 1000

    def clear(self):
        for attr in ("request_id", "user_id", "session_id",
                      "started_at", "metadata"):
            self._local.__dict__.pop(attr, None)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id(),
            "user_id": self.user_id(),
            "session_id": self.session_id(),
        }
```

---

## Solution 6: IsolatedToolExecutor — Run Tools in Isolated Context Copies

```python
import asyncio
import contextvars
import functools
from typing import Any, Callable, Optional


class IsolatedToolExecutor:
    """
    Runs each tool call in a copy of the current context to prevent
    tools from inadvertently modifying ContextVars that affect other tools
    running concurrently in the same task.

    Usage:
        executor = IsolatedToolExecutor()

        # Tools run in isolated context copies:
        results = await asyncio.gather(
            executor.run(search_tool, query="SSRF"),
            executor.run(db_tool, user_id="u-123"),
        )
        # Each tool has its own context copy; mutations don't leak.
    """

    async def run(self, fn: Callable, *args, **kwargs) -> Any:
        """Run fn in a copy of the current context."""
        ctx = contextvars.copy_context()
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: ctx.run(asyncio.run, fn(*args, **kwargs))
        ) if not asyncio.iscoroutinefunction(fn) else \
            await self._run_async_in_copy(ctx, fn, *args, **kwargs)

    @staticmethod
    async def _run_async_in_copy(ctx: contextvars.Context,
                                   fn: Callable, *args, **kwargs) -> Any:
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        async def _wrapped():
            try:
                result = await fn(*args, **kwargs)
                future.set_result(result)
            except Exception as exc:
                future.set_exception(exc)

        # Create task with the copied context
        loop = asyncio.get_event_loop()
        task = loop.create_task(_wrapped())
        return await future

    def bind(self, fn: Callable) -> Callable:
        """Decorator that runs fn in an isolated context copy."""
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            return await self.run(fn, *args, **kwargs)
        return wrapper
```

---

## Comparison

| Approach | AsyncIO | Threading | Auto-Propagates | Child Tasks | Logging |
|---|---|---|---|---|---|
| **RequestContextStore** | Yes | No | No | Inherit | No |
| **RequestContextMiddleware** | Yes | No | Yes (scope) | Inherit | No |
| **ContextAwareLogger** | Yes | No | Via ContextVar | Yes | Yes |
| **ContextPropagatingTaskFactory** | Yes | No | Explicit | Yes | No |
| **ThreadLocalRequestContext** | No | Yes | No | No | No |
| **IsolatedToolExecutor** | Yes | No | Isolated copies | Isolated | No |

**Key insight**: Python's `asyncio.create_task()` already copies the context snapshot from the creating coroutine — child tasks inherit their parent's ContextVar values but cannot mutate them in a way visible to the parent. The dangerous pattern is using a global mutable object (a dict, a dataclass instance) as the "context" instead of a ContextVar: mutations to that object are visible to all tasks holding a reference. Always store request context in a `ContextVar[RequestContext]` where `RequestContext` is a frozen or newly-created instance per request.
