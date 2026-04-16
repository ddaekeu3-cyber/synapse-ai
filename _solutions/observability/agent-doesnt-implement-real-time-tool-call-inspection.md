---
title: "Agent Doesn't Implement Real-Time Tool Call Inspection"
description: "AI agents execute tool calls as black boxes with no live visibility into what arguments were passed, how long each call took, or what result was returned — making debugging production issues nearly impossible."
category: observability
difficulty: intermediate
tags: [tool-calls, inspection, debugging, tracing, logging, asyncio, middleware, fastapi]
---

# Agent Doesn't Implement Real-Time Tool Call Inspection

## Problem

When a production agent silently returns a wrong answer or hangs, the first question is: "what tool calls did it make, with what arguments, and what did they return?" Without structured tool call logging, you're blind. Real-time tool call inspection captures the full lifecycle — arguments, timing, result size, errors — and makes this information queryable in dashboards, searchable in logs, and streamable to connected debugging clients.

## Solution 1: Tool Call Interceptor Decorator

Wrap every tool function with a decorator that logs the full call lifecycle.

```python
import asyncio
import time
import json
import logging
import functools
from typing import Any, Callable, Awaitable

logger = logging.getLogger("agent.tools")

def inspect_tool(tool_name: str | None = None, log_args: bool = True, log_result: bool = True):
    """Decorator that logs tool call lifecycle: args, timing, result, errors."""

    def decorator(fn: Callable[..., Awaitable[Any]]):
        name = tool_name or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> Any:
            call_id = f"{name}_{int(time.monotonic()*1000) % 99999:05d}"
            t0 = time.monotonic()

            # Log invocation
            log_payload: dict = {"call_id": call_id, "tool": name, "phase": "invoke"}
            if log_args:
                try:
                    safe_args = json.dumps(kwargs, default=str)[:500]
                    log_payload["args"] = safe_args
                except Exception:
                    log_payload["args"] = repr(kwargs)[:200]
            logger.info("tool_call_start", extra=log_payload)

            try:
                result = await fn(*args, **kwargs)
                elapsed_ms = (time.monotonic() - t0) * 1000

                # Log success
                result_log: dict = {
                    "call_id": call_id,
                    "tool": name,
                    "phase": "complete",
                    "elapsed_ms": round(elapsed_ms, 1),
                }
                if log_result:
                    try:
                        result_str = json.dumps(result, default=str)
                        result_log["result_size"] = len(result_str)
                        result_log["result_preview"] = result_str[:200]
                    except Exception:
                        result_log["result_preview"] = repr(result)[:200]
                logger.info("tool_call_complete", extra=result_log)
                return result

            except Exception as exc:
                elapsed_ms = (time.monotonic() - t0) * 1000
                logger.error(
                    "tool_call_error",
                    extra={
                        "call_id": call_id,
                        "tool": name,
                        "elapsed_ms": round(elapsed_ms, 1),
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:200],
                    },
                    exc_info=True,
                )
                raise

        return wrapper
    return decorator

# Usage
@inspect_tool(log_args=True, log_result=True)
async def search_web(query: str, max_results: int = 5) -> list[dict]:
    await asyncio.sleep(0.2)  # simulate API call
    return [{"title": f"Result {i}", "url": f"https://example.com/{i}"} for i in range(max_results)]

@inspect_tool("write_file", log_args=True, log_result=False)  # don't log file contents
async def write_file(path: str, content: str) -> bool:
    await asyncio.sleep(0.05)
    return True
```

**When to use**: Any agent with discrete tool functions. Zero application logic changes needed.

---

## Solution 2: Centralized Tool Registry with Lifecycle Hooks

Register all tools in a registry that automatically instruments every call with hooks for pre/post/error.

```python
import asyncio
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger("agent.tool_registry")

@dataclass
class ToolInvocation:
    call_id: str
    tool_name: str
    args: dict
    started_at: float
    completed_at: float | None = None
    result: Any = None
    error: str | None = None

    @property
    def elapsed_ms(self) -> float | None:
        if self.completed_at:
            return round((self.completed_at - self.started_at) * 1000, 1)
        return None

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if self.completed_at:
            return "complete"
        return "pending"

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Callable] = {}
        self._pre_hooks: list[Callable] = []
        self._post_hooks: list[Callable] = []
        self._error_hooks: list[Callable] = []
        self._recent_calls: list[ToolInvocation] = []
        self._max_history = 100
        self._call_counter = 0

    def register(self, name: str, fn: Callable[..., Awaitable[Any]]):
        self._tools[name] = fn
        return self

    def on_pre_call(self, hook: Callable):
        self._pre_hooks.append(hook)
        return self

    def on_post_call(self, hook: Callable):
        self._post_hooks.append(hook)
        return self

    def on_error(self, hook: Callable):
        self._error_hooks.append(hook)
        return self

    async def call(self, tool_name: str, **kwargs) -> Any:
        if tool_name not in self._tools:
            raise ValueError(f"Unknown tool: {tool_name}")

        self._call_counter += 1
        call_id = f"{tool_name}_{self._call_counter:06d}"
        invocation = ToolInvocation(
            call_id=call_id,
            tool_name=tool_name,
            args={k: str(v)[:200] for k, v in kwargs.items()},
            started_at=time.monotonic(),
        )
        self._add_to_history(invocation)

        # Pre-call hooks
        for hook in self._pre_hooks:
            await hook(invocation)

        try:
            result = await self._tools[tool_name](**kwargs)
            invocation.completed_at = time.monotonic()
            invocation.result = result
            for hook in self._post_hooks:
                await hook(invocation)
            return result
        except Exception as exc:
            invocation.completed_at = time.monotonic()
            invocation.error = str(exc)[:300]
            for hook in self._error_hooks:
                await hook(invocation)
            raise

    def _add_to_history(self, inv: ToolInvocation):
        self._recent_calls.append(inv)
        if len(self._recent_calls) > self._max_history:
            self._recent_calls.pop(0)

    def recent_calls(self, n: int = 20) -> list[dict]:
        return [
            {"call_id": c.call_id, "tool": c.tool_name, "status": c.status,
             "elapsed_ms": c.elapsed_ms, "error": c.error}
            for c in self._recent_calls[-n:]
        ]

# Setup
registry = ToolRegistry()

@registry.on_pre_call
async def log_pre(inv: ToolInvocation):
    logger.info("tool_invoke", extra={"call_id": inv.call_id, "tool": inv.tool_name, "args": inv.args})

@registry.on_post_call
async def log_post(inv: ToolInvocation):
    logger.info("tool_complete", extra={"call_id": inv.call_id, "elapsed_ms": inv.elapsed_ms})

@registry.on_error
async def log_error(inv: ToolInvocation):
    logger.error("tool_error", extra={"call_id": inv.call_id, "error": inv.error})
```

**When to use**: Agents with many tools where centralized registration is cleaner than per-function decorators.

---

## Solution 3: Real-Time Tool Call Stream via WebSocket

Stream live tool call events to connected debugging clients (browser DevTools, CLI watchers).

```python
import asyncio
import json
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()

# Broadcast queue for tool call events
_event_subscribers: list[asyncio.Queue] = []

async def broadcast_tool_event(event: dict):
    """Send tool call event to all connected WebSocket clients."""
    event["ts"] = time.time()
    event_json = json.dumps(event)
    dead = []
    for q in _event_subscribers:
        try:
            q.put_nowait(event_json)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _event_subscribers.remove(q)

class InspectableToolRunner:
    async def run(self, tool_name: str, fn, **kwargs) -> Any:
        call_id = f"{tool_name}_{int(time.time()*1000)}"
        await broadcast_tool_event({
            "event": "tool_start",
            "call_id": call_id,
            "tool": tool_name,
            "args": {k: str(v)[:100] for k, v in kwargs.items()},
        })
        t0 = time.monotonic()
        try:
            result = await fn(**kwargs)
            elapsed_ms = (time.monotonic() - t0) * 1000
            result_preview = json.dumps(result, default=str)[:200] if result is not None else "null"
            await broadcast_tool_event({
                "event": "tool_complete",
                "call_id": call_id,
                "tool": tool_name,
                "elapsed_ms": round(elapsed_ms, 1),
                "result_preview": result_preview,
            })
            return result
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            await broadcast_tool_event({
                "event": "tool_error",
                "call_id": call_id,
                "tool": tool_name,
                "elapsed_ms": round(elapsed_ms, 1),
                "error": str(exc)[:200],
            })
            raise

@app.websocket("/debug/tool-stream")
async def tool_stream(ws: WebSocket):
    """Connect to receive real-time tool call events."""
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _event_subscribers.append(q)
    try:
        while True:
            event = await q.get()
            await ws.send_text(event)
    except WebSocketDisconnect:
        _event_subscribers.remove(q)

# CLI watcher: wscat -c ws://localhost:8000/debug/tool-stream
runner = InspectableToolRunner()
```

**When to use**: Development and staging environments. Lets developers watch live agent execution without tailing logs.

---

## Solution 4: Structured Tool Call Spans with OpenTelemetry

Emit each tool call as an OpenTelemetry span for full distributed tracing integration.

```python
import asyncio
import time
import json
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Status, StatusCode

# Setup tracing
provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent.tools")

async def traced_tool_call(
    tool_name: str,
    fn,
    conversation_id: str = "",
    turn_id: str = "",
    **kwargs,
) -> Any:
    """Execute a tool call as an OpenTelemetry span."""
    with tracer.start_as_current_span(
        f"tool.{tool_name}",
        attributes={
            "tool.name": tool_name,
            "agent.conversation_id": conversation_id,
            "agent.turn_id": turn_id,
        },
    ) as span:
        # Record arguments as span attributes (sanitized)
        for k, v in kwargs.items():
            val = str(v)[:500]
            span.set_attribute(f"tool.arg.{k}", val)

        t0 = time.monotonic()
        try:
            result = await fn(**kwargs)
            elapsed_ms = (time.monotonic() - t0) * 1000

            span.set_attribute("tool.elapsed_ms", round(elapsed_ms, 1))
            span.set_attribute("tool.status", "success")

            result_str = json.dumps(result, default=str) if result is not None else ""
            span.set_attribute("tool.result.size_bytes", len(result_str))
            span.set_attribute("tool.result.preview", result_str[:200])
            span.set_status(Status(StatusCode.OK))
            return result

        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            span.set_attribute("tool.elapsed_ms", round(elapsed_ms, 1))
            span.set_attribute("tool.status", "error")
            span.set_attribute("tool.error.type", type(exc).__name__)
            span.set_attribute("tool.error.message", str(exc)[:300])
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)[:100]))
            raise

# Usage
async def search_tool(query: str) -> list[str]:
    await asyncio.sleep(0.1)
    return ["result1", "result2"]

async def agent_turn(prompt: str, conv_id: str):
    with tracer.start_as_current_span("agent.turn", attributes={"conversation_id": conv_id}):
        return await traced_tool_call(
            "search",
            search_tool,
            conversation_id=conv_id,
            turn_id="turn_1",
            query=prompt,
        )
```

**When to use**: Production agents with existing OTel infrastructure (Jaeger, Grafana Tempo, Datadog APM).

---

## Solution 5: Tool Call Replay Log for Debugging

Record every tool call's full input and output to a structured log; replay it to reproduce agent behavior exactly.

```python
import asyncio
import json
import time
import uuid
from pathlib import Path

class ToolCallReplayLog:
    """Record tool calls to a JSONL file for later replay and debugging."""

    def __init__(self, log_path: str = "/var/log/agent-tool-calls.jsonl"):
        self._path = Path(log_path)
        self._session_id = str(uuid.uuid4())[:8]
        self._fh = open(self._path, "a")

    def record(
        self,
        tool_name: str,
        args: dict,
        result: Any,
        elapsed_ms: float,
        error: str | None = None,
    ):
        entry = {
            "session_id": self._session_id,
            "call_id": str(uuid.uuid4())[:8],
            "ts": time.time(),
            "tool": tool_name,
            "args": {k: v for k, v in args.items() if not isinstance(v, bytes)},
            "elapsed_ms": round(elapsed_ms, 1),
            "result": result if error is None else None,
            "error": error,
        }
        try:
            line = json.dumps(entry, default=str)
        except Exception:
            line = json.dumps({"error": "serialization_failed", "tool": tool_name})
        self._fh.write(line + "\n")
        self._fh.flush()

    async def wrap(self, tool_name: str, fn, **kwargs) -> Any:
        t0 = time.monotonic()
        try:
            result = await fn(**kwargs)
            self.record(tool_name, kwargs, result, (time.monotonic()-t0)*1000)
            return result
        except Exception as exc:
            self.record(tool_name, kwargs, None, (time.monotonic()-t0)*1000, str(exc))
            raise

    def close(self):
        self._fh.close()

    @staticmethod
    def replay(log_path: str, session_id: str | None = None) -> list[dict]:
        """Load recorded calls for a session (for debugging)."""
        entries = []
        with open(log_path) as f:
            for line in f:
                entry = json.loads(line.strip())
                if session_id is None or entry.get("session_id") == session_id:
                    entries.append(entry)
        return entries

replay_log = ToolCallReplayLog()
```

**When to use**: Debugging hard-to-reproduce agent failures. Record in production, replay in a sandbox.

---

## Solution 6: Admin API for Live Tool Call Inspection

Expose an admin REST API listing recent tool calls, their arguments, and results.

```python
import asyncio
import time
from collections import deque
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

app = FastAPI()

class ToolCallRecord(BaseModel):
    call_id: str
    tool: str
    args_preview: str
    result_preview: str | None
    elapsed_ms: float | None
    status: str
    ts: float

_call_history: deque[dict] = deque(maxlen=500)
_call_counter = 0

def record_call(tool: str, args: dict, result, elapsed_ms: float, error: str | None = None):
    global _call_counter
    _call_counter += 1
    import json
    _call_history.append({
        "call_id": f"{tool}_{_call_counter:06d}",
        "tool": tool,
        "args_preview": json.dumps(args, default=str)[:200],
        "result_preview": json.dumps(result, default=str)[:300] if result and not error else None,
        "elapsed_ms": round(elapsed_ms, 1),
        "status": "error" if error else "success",
        "error": error,
        "ts": time.time(),
    })

@app.get("/admin/tool-calls", response_model=list[ToolCallRecord])
async def list_tool_calls(
    n: int = 50,
    tool: str | None = None,
    status: str | None = None,
    x_admin_key: str = Header(...),
):
    if x_admin_key != "admin-secret":
        raise HTTPException(status_code=403)

    calls = list(_call_history)
    if tool:
        calls = [c for c in calls if c["tool"] == tool]
    if status:
        calls = [c for c in calls if c["status"] == status]

    return [ToolCallRecord(**c) for c in calls[-n:]]

@app.get("/admin/tool-calls/stats")
async def tool_call_stats(x_admin_key: str = Header(...)):
    if x_admin_key != "admin-secret":
        raise HTTPException(status_code=403)

    from collections import Counter
    calls = list(_call_history)
    tool_counts = Counter(c["tool"] for c in calls)
    error_counts = Counter(c["tool"] for c in calls if c["status"] == "error")
    avg_latency = {}
    for tool_name in tool_counts:
        tool_calls = [c for c in calls if c["tool"] == tool_name and c["elapsed_ms"]]
        if tool_calls:
            avg_latency[tool_name] = round(sum(c["elapsed_ms"] for c in tool_calls) / len(tool_calls), 1)

    return {
        "total_calls": len(calls),
        "by_tool": dict(tool_counts),
        "errors_by_tool": dict(error_counts),
        "avg_latency_ms": avg_latency,
    }

# Instrument tools to feed the history
async def instrumented_tool(tool_name: str, fn, **kwargs):
    t0 = time.monotonic()
    try:
        result = await fn(**kwargs)
        record_call(tool_name, kwargs, result, (time.monotonic()-t0)*1000)
        return result
    except Exception as exc:
        record_call(tool_name, kwargs, None, (time.monotonic()-t0)*1000, str(exc))
        raise
```

**When to use**: Production agents where ops teams need self-service visibility into tool call patterns without log access.

---

## Comparison

| Solution | Real-Time | Full History | Replay | Distributed Tracing | Admin UI | Best For |
|---|---|---|---|---|---|---|
| Decorator interceptor | Yes (logs) | Via log files | No | No | No | Simple agents, quick setup |
| Centralized registry | Yes (hooks) | In-memory (100 calls) | No | No | Programmatic | Multi-tool agents |
| WebSocket stream | Yes (live) | No | No | No | Browser/CLI | Development debugging |
| OpenTelemetry spans | Yes (OTel) | Via backend | No | Yes | Grafana/Jaeger | Production distributed tracing |
| Replay log | No | Full (JSONL) | Yes | No | No | Bug reproduction |
| Admin REST API | On-demand | In-memory (500) | No | No | Yes | Ops self-service |

**Rule of thumb**: Use the decorator interceptor as the baseline in all environments. Add OTel spans in production. Add the WebSocket stream in development. Add the replay log for debugging hard-to-reproduce failures.
