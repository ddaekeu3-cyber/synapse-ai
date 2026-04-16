---
title: "Agent Doesn't Implement Distributed Correlation ID Propagation"
description: "Agents that generate a fresh request ID per LLM call but never thread it through tool executions, sub-agent spawns, and external API calls produce logs where a single user action appears as dozens of unrelated events. Implement distributed correlation ID propagation that mints a root trace ID at session entry, attaches it to every downstream call as a header or context field, and ensures all log records for a single user action can be joined by that ID."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-distributed-correlation-id-propagation
tags: [correlation-id, distributed-tracing, request-id, log-correlation, context-propagation, observability]
symptoms:
  - "Logs for a single user request are scattered across dozens of unrelated trace IDs"
  - "Tool call logs cannot be joined to the LLM call that triggered them"
  - "Sub-agent spawns generate new root IDs, breaking the parent trace"
  - "External API calls carry no correlation header — vendor support cannot trace by request"
  - "On-call engineers reconstruct request flows manually by timestamp proximity"
---

## Why This Happens

Every component in an agent system — the LLM client, each tool executor, sub-agent spawners, HTTP clients — generates its own identifiers unless explicitly told otherwise. Without a propagation contract, a `correlation_id` minted at the HTTP gateway dies at the first internal function boundary. The fix requires three things: a context carrier that holds the correlation ID for the lifetime of a request, an injection mechanism that attaches it to every outbound call, and an extraction mechanism that restores it when a downstream component receives a call. In Python, `contextvars.ContextVar` provides the right scope: it follows async task boundaries without leaking between concurrent requests.

## Solution 1: Correlation Context

```python
import contextvars
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


_CORRELATION_CTX: contextvars.ContextVar["CorrelationContext"] = contextvars.ContextVar(
    "correlation_ctx"
)


@dataclass
class CorrelationContext:
    trace_id: str           # root ID for the entire user action
    span_id: str            # ID for the current processing unit
    parent_span_id: Optional[str] = None
    session_id: str = ""
    user_id: str = ""
    feature: str = ""
    created_at: float = field(default_factory=time.time)

    @classmethod
    def new_root(
        cls,
        session_id: str = "",
        user_id: str = "",
        feature: str = "",
    ) -> "CorrelationContext":
        return cls(
            trace_id=_new_id(),
            span_id=_new_id(),
            session_id=session_id,
            user_id=user_id,
            feature=feature,
        )

    def child_span(self) -> "CorrelationContext":
        """Create a child context that inherits trace_id but gets a new span_id."""
        return CorrelationContext(
            trace_id=self.trace_id,
            span_id=_new_id(),
            parent_span_id=self.span_id,
            session_id=self.session_id,
            user_id=self.user_id,
            feature=self.feature,
        )

    def to_headers(self) -> dict:
        headers = {
            "X-Trace-Id": self.trace_id,
            "X-Span-Id": self.span_id,
        }
        if self.parent_span_id:
            headers["X-Parent-Span-Id"] = self.parent_span_id
        if self.session_id:
            headers["X-Session-Id"] = self.session_id
        return headers

    def to_log_fields(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "feature": self.feature,
        }


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def current_correlation() -> Optional[CorrelationContext]:
    return _CORRELATION_CTX.get(None)


def set_correlation(ctx: CorrelationContext) -> contextvars.Token:
    return _CORRELATION_CTX.set(ctx)
```

## Solution 2: Correlation Context Manager

```python
import contextlib
from typing import Generator, Optional


@contextlib.contextmanager
def correlation_scope(
    ctx: Optional[CorrelationContext] = None,
    *,
    session_id: str = "",
    user_id: str = "",
    feature: str = "",
) -> Generator[CorrelationContext, None, None]:
    """
    Context manager that installs a correlation context for the duration
    of the block. If no context is provided, a new root context is created.
    On exit, the previous context is restored.
    """
    if ctx is None:
        ctx = CorrelationContext.new_root(
            session_id=session_id,
            user_id=user_id,
            feature=feature,
        )
    token = set_correlation(ctx)
    try:
        yield ctx
    finally:
        _CORRELATION_CTX.reset(token)


@contextlib.contextmanager
def child_span_scope() -> Generator[CorrelationContext, None, None]:
    """
    Creates a child span from the current context. Raises if no
    correlation context is active.
    """
    parent = current_correlation()
    if parent is None:
        raise RuntimeError("No active correlation context — wrap caller in correlation_scope()")
    child = parent.child_span()
    token = set_correlation(child)
    try:
        yield child
    finally:
        _CORRELATION_CTX.reset(token)


class CorrelationContextExtractor:
    """
    Extracts a CorrelationContext from inbound HTTP headers.
    Falls back to a new root context if no headers are present.
    """

    HEADER_TRACE_ID = "X-Trace-Id"
    HEADER_SPAN_ID = "X-Span-Id"
    HEADER_PARENT_SPAN_ID = "X-Parent-Span-Id"
    HEADER_SESSION_ID = "X-Session-Id"

    def extract(self, headers: dict) -> CorrelationContext:
        trace_id = headers.get(self.HEADER_TRACE_ID, "").strip()
        if not trace_id:
            return CorrelationContext.new_root()
        return CorrelationContext(
            trace_id=trace_id,
            span_id=_new_id(),
            parent_span_id=headers.get(self.HEADER_PARENT_SPAN_ID, "").strip() or None,
            session_id=headers.get(self.HEADER_SESSION_ID, "").strip(),
        )
```

## Solution 3: Correlated Tool Executor

```python
import time
from typing import Any, Callable, Optional


class CorrelatedToolExecutor:
    """
    Wraps tool execution so every call is logged with the active
    correlation context. Opens a child span for each tool call so
    the tool's logs can be distinguished from the parent LLM call.
    """

    def __init__(self, write_log_fn: Optional[Callable[[dict], None]] = None):
        import json
        self._write = write_log_fn or (lambda r: print(json.dumps(r)))

    async def execute(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        with child_span_scope() as span_ctx:
            start = time.time()
            outcome = "ok"
            error_msg = None
            try:
                result = await tool_fn(*args, **kwargs)
                return result
            except Exception as exc:
                outcome = "error"
                error_msg = str(exc)
                raise
            finally:
                latency_ms = round((time.time() - start) * 1000, 2)
                record = {
                    "event": "tool_call",
                    "tool_name": tool_name,
                    "outcome": outcome,
                    "latency_ms": latency_ms,
                    **span_ctx.to_log_fields(),
                }
                if error_msg:
                    record["error"] = error_msg
                self._write(record)
```

## Solution 4: HTTP Client Correlation Injector

```python
from typing import Any, Callable, Dict, Optional
import time


class HTTPCorrelationInjector:
    """
    Middleware that injects the active correlation context into outbound
    HTTP request headers before the request is dispatched. Works with
    any HTTP client that accepts a headers dict.
    """

    def inject(self, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """
        Returns a headers dict with correlation fields added.
        If no correlation context is active, returns headers unchanged.
        """
        merged = dict(headers or {})
        ctx = current_correlation()
        if ctx is not None:
            merged.update(ctx.to_headers())
        return merged

    async def call(
        self,
        http_fn: Callable,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Any:
        injected_headers = self.inject(headers)
        with child_span_scope() as span_ctx:
            start = time.time()
            try:
                return await http_fn(url, headers=injected_headers, **kwargs)
            finally:
                pass  # latency logged by caller or middleware
```

## Solution 5: Sub-Agent Correlation Forwarder

```python
import json
from typing import Any, Callable, Dict, Optional


class SubAgentCorrelationForwarder:
    """
    Serializes the active correlation context into the payload
    passed to a sub-agent spawn call, and provides a matching
    deserializer for the sub-agent to restore context on startup.
    """

    PAYLOAD_KEY = "__correlation__"

    def inject_into_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ctx = current_correlation()
        if ctx is None:
            return payload
        enriched = dict(payload)
        enriched[self.PAYLOAD_KEY] = {
            "trace_id": ctx.trace_id,
            "parent_span_id": ctx.span_id,
            "session_id": ctx.session_id,
            "user_id": ctx.user_id,
            "feature": ctx.feature,
        }
        return enriched

    def extract_from_payload(self, payload: Dict[str, Any]) -> Optional[CorrelationContext]:
        data = payload.get(self.PAYLOAD_KEY)
        if not data:
            return None
        return CorrelationContext(
            trace_id=data["trace_id"],
            span_id=_new_id(),
            parent_span_id=data.get("parent_span_id"),
            session_id=data.get("session_id", ""),
            user_id=data.get("user_id", ""),
            feature=data.get("feature", ""),
        )

    def restore_from_payload(self, payload: Dict[str, Any]) -> Optional[contextvars.Token]:
        ctx = self.extract_from_payload(payload)
        if ctx:
            return set_correlation(ctx)
        return None
```

## Solution 6: Correlation Coverage Auditor

```python
import time
from collections import defaultdict
from typing import Dict, List


class CorrelationCoverageAuditor:
    """
    Inspects a sample of log records to detect events that are missing
    correlation IDs. Reports coverage rates and which event types have
    the worst propagation gaps.
    """

    REQUIRED_FIELDS = {"trace_id", "span_id"}

    def __init__(self):
        self._records: List[dict] = []
        self._recorded_at: List[float] = []

    def ingest(self, log_record: dict) -> None:
        self._records.append(log_record)
        self._recorded_at.append(time.time())

    def audit(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._records, self._recorded_at)
            if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "records_audited": 0}

        missing_by_event: Dict[str, int] = defaultdict(int)
        total_missing = 0

        for record in recent:
            has_all = all(f in record and record[f] for f in self.REQUIRED_FIELDS)
            if not has_all:
                total_missing += 1
                event_type = record.get("event", "unknown")
                missing_by_event[event_type] += 1

        coverage_rate = 1.0 - total_missing / len(recent)

        return {
            "window_seconds": window_seconds,
            "records_audited": len(recent),
            "missing_correlation": total_missing,
            "coverage_rate": round(coverage_rate, 4),
            "gaps_by_event_type": dict(missing_by_event),
        }
```

## Comparison

| Approach | Context Propagation | Outbound Injection | Sub-Agent Forwarding | HTTP Headers | Coverage Audit |
|---|---|---|---|---|---|
| CorrelationContext | Yes (ContextVar) | No | No | Via to_headers() | No |
| CorrelationContextManager | Via scope | No | No | No | No |
| CorrelatedToolExecutor | Via child span | No | No | No | No |
| HTTPCorrelationInjector | Via context | Yes | No | Yes | No |
| SubAgentCorrelationForwarder | Via payload | No | Yes | No | No |
| CorrelationCoverageAuditor | No | No | No | No | Yes |

**Best for production**: Mount `correlation_scope()` at the outermost request handler — before any async task is spawned — so all concurrent tool calls inherit the root `trace_id` via `contextvars`. Always call `child_span_scope()` inside each tool executor so tool-level logs have a unique `span_id` while sharing the parent `trace_id`. Use `HTTPCorrelationInjector` for every outbound HTTP call — include `X-Trace-Id` as a required field in vendor SLA agreements so support tickets can be traced end-to-end. Run `CorrelationCoverageAuditor.audit()` weekly against production logs: a coverage rate below 0.95 means a new code path is missing the propagation wrapper.
