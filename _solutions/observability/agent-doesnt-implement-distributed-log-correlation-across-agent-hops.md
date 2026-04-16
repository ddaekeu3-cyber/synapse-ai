---
title: "Agent Doesn't Implement Distributed Log Correlation Across Agent Hops"
description: "Multi-agent systems where each agent logs independently produce interleaved, uncorrelated log streams. Debugging a failure requires manually correlating timestamps across agents with no guaranteed ordering. Implement distributed log correlation that propagates a correlation ID through every agent hop, injects it into every log line, and provides a query interface to reconstruct the full log timeline for a single user request across all agents."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-distributed-log-correlation-across-agent-hops
tags: [log-correlation, distributed-logging, correlation-id, request-tracing, structured-logging, multi-agent-observability]
symptoms:
  - "Logs from orchestrator and sub-agents are interleaved with no way to filter by request"
  - "Debugging a failed multi-agent run requires grepping multiple log files by timestamp"
  - "No correlation ID in log lines — cannot reconstruct which logs belong to which user request"
  - "Sub-agent logs contain no reference to the parent agent that spawned them"
  - "Log aggregation system shows thousands of unrelated lines when searching for one failure"
---

## Why This Happens

Each agent initializes its own logger without inheriting context from the caller. Log lines contain timestamps and severity but no request-scoped identifier that links them to a specific user interaction. In a multi-agent system, the orchestrator spawns sub-agents as separate async tasks or processes, each producing their own log stream. Without a correlation ID that flows from the initial user request through every hop, the log streams are topologically disconnected. Distributed log correlation requires injecting a correlation ID at the entry point and propagating it through every async boundary, subprocess spawn, and HTTP call.

## Solution 1: Correlation Context

```python
import contextvars
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class CorrelationContext:
    correlation_id: str
    request_id: str
    parent_agent_id: Optional[str] = None
    current_agent_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    hop_depth: int = 0
    extra: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def new(cls, extra: Optional[Dict[str, str]] = None) -> "CorrelationContext":
        cid = uuid.uuid4().hex
        return cls(
            correlation_id=cid,
            request_id=uuid.uuid4().hex[:12],
            extra=extra or {},
        )

    def child(self, child_agent_id: Optional[str] = None) -> "CorrelationContext":
        """Create a child context for a spawned sub-agent."""
        return CorrelationContext(
            correlation_id=self.correlation_id,
            request_id=uuid.uuid4().hex[:12],
            parent_agent_id=self.current_agent_id,
            current_agent_id=child_agent_id or uuid.uuid4().hex[:8],
            hop_depth=self.hop_depth + 1,
            extra=dict(self.extra),
        )

    def to_headers(self) -> Dict[str, str]:
        headers = {
            "X-Correlation-ID": self.correlation_id,
            "X-Request-ID": self.request_id,
            "X-Agent-ID": self.current_agent_id,
            "X-Hop-Depth": str(self.hop_depth),
        }
        if self.parent_agent_id:
            headers["X-Parent-Agent-ID"] = self.parent_agent_id
        return headers

    @classmethod
    def from_headers(cls, headers: Dict[str, str]) -> "CorrelationContext":
        return cls(
            correlation_id=headers.get("X-Correlation-ID", uuid.uuid4().hex),
            request_id=headers.get("X-Request-ID", uuid.uuid4().hex[:12]),
            parent_agent_id=headers.get("X-Parent-Agent-ID"),
            current_agent_id=headers.get("X-Agent-ID", uuid.uuid4().hex[:8]),
            hop_depth=int(headers.get("X-Hop-Depth", 0)),
        )


_CORRELATION_CTX: contextvars.ContextVar[Optional[CorrelationContext]] = \
    contextvars.ContextVar("correlation_ctx", default=None)


def get_correlation_ctx() -> Optional[CorrelationContext]:
    return _CORRELATION_CTX.get()


def set_correlation_ctx(ctx: CorrelationContext) -> contextvars.Token:
    return _CORRELATION_CTX.set(ctx)


def reset_correlation_ctx(token: contextvars.Token) -> None:
    _CORRELATION_CTX.reset(token)
```

## Solution 2: Correlated Logger

```python
import logging
import time
from typing import Any, Dict, Optional


class CorrelatedLogger:
    """
    Wraps stdlib logging and automatically injects correlation context
    fields into every log record. Uses structured logging (dict payload)
    so log aggregators can index by correlation_id.
    """

    def __init__(self, name: str, level: int = logging.INFO):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        self._name = name

    def _enrich(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ctx = get_correlation_ctx()
        fields: Dict[str, Any] = {
            "logger": self._name,
            "ts": time.time(),
        }
        if ctx:
            fields["correlation_id"] = ctx.correlation_id
            fields["request_id"] = ctx.request_id
            fields["agent_id"] = ctx.current_agent_id
            fields["hop_depth"] = ctx.hop_depth
            if ctx.parent_agent_id:
                fields["parent_agent_id"] = ctx.parent_agent_id
            fields.update(ctx.extra)
        if extra:
            fields.update(extra)
        return fields

    def info(self, message: str, **extra: Any) -> None:
        self._logger.info(message, extra={"structured": self._enrich(extra)})

    def warning(self, message: str, **extra: Any) -> None:
        self._logger.warning(message, extra={"structured": self._enrich(extra)})

    def error(self, message: str, **extra: Any) -> None:
        self._logger.error(message, extra={"structured": self._enrich(extra)})

    def debug(self, message: str, **extra: Any) -> None:
        self._logger.debug(message, extra={"structured": self._enrich(extra)})
```

## Solution 3: Correlation Propagating Agent Wrapper

```python
import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, Optional


class CorrelationPropagatingAgentWrapper:
    """
    Context manager that sets the correlation context for an agent's execution scope.
    For the root agent: call with a new CorrelationContext.
    For sub-agents: call with ctx.child() to propagate the parent correlation ID.
    Automatically resets the context when the agent scope exits.
    """

    def __init__(self, logger: CorrelatedLogger):
        self._logger = logger

    @asynccontextmanager
    async def run(
        self,
        ctx: CorrelationContext,
        agent_name: str,
    ) -> AsyncGenerator[CorrelationContext, None]:
        token = set_correlation_ctx(ctx)
        self._logger.info(f"Agent started: {agent_name}", agent_name=agent_name)
        try:
            yield ctx
            self._logger.info(f"Agent completed: {agent_name}", agent_name=agent_name)
        except Exception as exc:
            self._logger.error(
                f"Agent failed: {agent_name}",
                agent_name=agent_name,
                error=str(exc)[:200],
            )
            raise
        finally:
            reset_correlation_ctx(token)

    async def spawn_child(
        self,
        parent_ctx: CorrelationContext,
        child_name: str,
        child_fn: Callable[[CorrelationContext], Any],
    ) -> Any:
        """Run a child agent with propagated correlation context."""
        child_ctx = parent_ctx.child(child_agent_id=child_name)
        token = set_correlation_ctx(child_ctx)
        self._logger.info(
            f"Spawning child agent: {child_name}",
            child_agent=child_name,
            parent_agent=parent_ctx.current_agent_id,
        )
        try:
            result = await child_fn(child_ctx)
            return result
        finally:
            reset_correlation_ctx(token)
```

## Solution 4: Structured Log Formatter

```python
import json
import logging
import time
from typing import Any, Dict


class StructuredCorrelationFormatter(logging.Formatter):
    """
    JSON log formatter that extracts the 'structured' extra field and
    emits one JSON object per line. Suitable for log aggregators
    (Datadog, Loki, CloudWatch) that ingest JSONL.
    """

    def format(self, record: logging.LogRecord) -> str:
        structured: Dict[str, Any] = getattr(record, "structured", {})
        payload = {
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            **structured,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_correlated_logging(log_level: int = logging.INFO) -> None:
    """
    Configures the root logger to emit structured JSONL to stdout.
    Call once at application startup.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredCorrelationFormatter())
    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(handler)
```

## Solution 5: In-Process Log Correlator

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class CorrelatedLogEntry:
    correlation_id: str
    request_id: str
    agent_id: str
    hop_depth: int
    level: str
    message: str
    extra: dict
    ts: float = field(default_factory=time.time)
    parent_agent_id: Optional[str] = None


class InProcessLogCorrelator:
    """
    In-process log store for development and testing.
    Collects all log entries tagged with a correlation ID and
    allows reconstructing the full timeline for a single user request.
    Not suitable for production at high volume — use a log aggregator instead.
    """

    def __init__(self, max_entries: int = 10_000):
        self._entries: List[CorrelatedLogEntry] = []
        self._by_correlation: Dict[str, List[CorrelatedLogEntry]] = defaultdict(list)
        self._max = max_entries

    def append(self, entry: CorrelatedLogEntry) -> None:
        if len(self._entries) >= self._max:
            oldest = self._entries.pop(0)
            self._by_correlation[oldest.correlation_id].pop(0)
        self._entries.append(entry)
        self._by_correlation[entry.correlation_id].append(entry)

    def get_timeline(self, correlation_id: str) -> List[dict]:
        entries = self._by_correlation.get(correlation_id, [])
        return [
            {
                "ts": e.ts,
                "level": e.level,
                "agent_id": e.agent_id,
                "hop_depth": e.hop_depth,
                "parent_agent_id": e.parent_agent_id,
                "message": e.message,
                **e.extra,
            }
            for e in sorted(entries, key=lambda x: x.ts)
        ]

    def agent_hop_summary(self, correlation_id: str) -> dict:
        entries = self._by_correlation.get(correlation_id, [])
        agents = {}
        for e in entries:
            if e.agent_id not in agents:
                agents[e.agent_id] = {
                    "agent_id": e.agent_id,
                    "parent_agent_id": e.parent_agent_id,
                    "hop_depth": e.hop_depth,
                    "log_count": 0,
                    "errors": 0,
                }
            agents[e.agent_id]["log_count"] += 1
            if e.level in ("ERROR", "CRITICAL"):
                agents[e.agent_id]["errors"] += 1
        return {
            "correlation_id": correlation_id,
            "total_log_lines": len(entries),
            "agent_count": len(agents),
            "agents": list(sorted(agents.values(), key=lambda x: x["hop_depth"])),
        }
```

## Solution 6: Log Correlation Health Monitor

```python
import time
from typing import List


class LogCorrelationHealthMonitor:
    """
    Monitors whether correlation IDs are being properly propagated.
    Detects log lines missing correlation context, which indicates
    an agent or tool is logging outside the correlation scope.
    """

    def __init__(self, correlator: InProcessLogCorrelator):
        self._correlator = correlator
        self._uncorrelated_count = 0
        self._total_count = 0

    def record_log(self, has_correlation: bool) -> None:
        self._total_count += 1
        if not has_correlation:
            self._uncorrelated_count += 1

    def health(self) -> dict:
        total = max(self._total_count, 1)
        uncorrelated_rate = self._uncorrelated_count / total
        alerts = []
        if uncorrelated_rate > 0.05 and self._total_count > 20:
            alerts.append({
                "type": "high_uncorrelated_rate",
                "severity": "warning",
                "rate": round(uncorrelated_rate, 4),
                "message": (
                    f"{self._uncorrelated_count} of {self._total_count} log lines "
                    "lack correlation context. Check agents that spawn threads "
                    "or subprocesses without propagating the correlation context."
                ),
            })
        return {
            "generated_at": time.time(),
            "total_log_lines": self._total_count,
            "uncorrelated_lines": self._uncorrelated_count,
            "uncorrelated_rate": round(uncorrelated_rate, 4),
            "healthy": len(alerts) == 0,
            "alerts": alerts,
        }
```

## Comparison

| Approach | Context Propagation | Structured Fields | Child Spawn Support | Timeline Reconstruction | Health Monitoring |
|---|---|---|---|---|---|
| CorrelationContext | Yes (contextvars + headers) | No | Yes (child()) | No | No |
| CorrelatedLogger | Via context | Yes (auto-inject) | No | No | No |
| CorrelationPropagatingAgentWrapper | Yes (scope management) | Via logger | Yes | No | No |
| StructuredCorrelationFormatter | No | Yes (JSONL) | No | No | No |
| InProcessLogCorrelator | No | No | No | Yes (timeline + hop summary) | No |
| LogCorrelationHealthMonitor | No | No | No | No | Yes |

**Best for production**: Call `CorrelationContext.new()` at the HTTP request handler or message queue consumer — the single entry point for the entire agent workflow — and set it via `set_correlation_ctx()`. Use `ctx.child()` every time you spawn a sub-agent so the `correlation_id` flows through but `agent_id` and `hop_depth` differentiate the agents. Configure `StructuredCorrelationFormatter` on your root logger so every log line emitted anywhere in the process automatically includes the correlation fields. In your log aggregator (Datadog, Loki), create a saved search on `correlation_id` — this instantly reconstructs the full multi-agent execution timeline without any manual timestamp correlation.
