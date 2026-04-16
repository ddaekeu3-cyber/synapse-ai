---
title: "Agent Doesn't Implement Structured Event Bus for Agent Telemetry"
description: "Agents that call logging functions directly from business logic couple telemetry to implementation: adding a new metric requires touching every call site, and consumers (metrics pipeline, audit log, alert system) each require separate integration. Implement a structured event bus that decouples event emission from event handling — emitters publish typed events, subscribers handle them independently, and the bus provides delivery guarantees, filtering, and replay."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-structured-event-bus-for-agent-telemetry
tags: [event-bus, telemetry, structured-events, pub-sub, observability-pipeline, decoupling]
symptoms:
  - "Adding a new metric requires modifying 15 different call sites across the codebase"
  - "Metrics pipeline, audit logger, and alert system are each wired directly to business logic"
  - "No way to replay past telemetry events to backfill a new downstream consumer"
  - "Tool call telemetry is inconsistent — some emit events, others call print()"
  - "Downstream consumers miss events during their restart because there is no buffer"
---

## Why This Happens

Instrumentation grows organically: a developer adds `logger.info(...)` here, a metrics call there, a direct Prometheus `Counter.inc()` somewhere else. The result is coupling: the business logic knows about every consumer. An event bus reverses this: emitters publish structured events without knowing who consumes them. New consumers subscribe without touching emitter code. The bus buffers events for slow consumers, filters by event type, and provides backpressure. All telemetry in the agent flows through one typed pipeline.

## Solution 1: Telemetry Event

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class EventType(str, Enum):
    TOOL_CALL_START = "tool_call.start"
    TOOL_CALL_END = "tool_call.end"
    TOOL_CALL_ERROR = "tool_call.error"
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    AGENT_ERROR = "agent.error"
    COST_INCURRED = "cost.incurred"
    CUSTOM = "custom"


@dataclass
class TelemetryEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    event_type: EventType = EventType.CUSTOM
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    agent_id: str = ""
    tool_name: str = ""
    model: str = ""
    duration_ms: Optional[float] = None
    token_count: Optional[int] = None
    error: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def matches(self, event_type: EventType) -> bool:
        return self.event_type == event_type

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "tool_name": self.tool_name,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "token_count": self.token_count,
            "error": self.error,
            "payload": self.payload,
        }
```

## Solution 2: Event Subscription

```python
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set


@dataclass
class EventSubscription:
    subscriber_id: str
    handler: Callable[[TelemetryEvent], None]
    event_types: Optional[Set[EventType]] = None   # None = subscribe to all
    filter_fn: Optional[Callable[[TelemetryEvent], bool]] = None
    async_handler: bool = False

    def matches(self, event: TelemetryEvent) -> bool:
        if self.event_types and event.event_type not in self.event_types:
            return False
        if self.filter_fn and not self.filter_fn(event):
            return False
        return True
```

## Solution 3: Structured Event Bus

```python
import asyncio
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional


class StructuredEventBus:
    """
    Async pub-sub event bus for agent telemetry.
    Publishers call emit() to publish events.
    Subscribers register handlers that are called for matching events.
    Maintains a ring buffer for replay and diagnostics.
    Handlers are called concurrently for async subscribers.
    """

    def __init__(
        self,
        buffer_size: int = 10_000,
        max_handler_errors: int = 5,
    ):
        self._subscriptions: Dict[str, EventSubscription] = {}
        self._buffer: Deque[TelemetryEvent] = deque(maxlen=buffer_size)
        self._max_handler_errors = max_handler_errors
        self._handler_errors: Dict[str, int] = {}
        self._emit_count = 0
        self._error_count = 0

    def subscribe(self, subscription: EventSubscription) -> None:
        self._subscriptions[subscription.subscriber_id] = subscription

    def unsubscribe(self, subscriber_id: str) -> None:
        self._subscriptions.pop(subscriber_id, None)

    async def emit(self, event: TelemetryEvent) -> None:
        self._buffer.append(event)
        self._emit_count += 1

        tasks = []
        for sub in list(self._subscriptions.values()):
            if not sub.matches(event):
                continue
            errors = self._handler_errors.get(sub.subscriber_id, 0)
            if errors >= self._max_handler_errors:
                continue   # silently drop for broken handlers

            if sub.async_handler:
                tasks.append(self._call_async(sub, event))
            else:
                self._call_sync(sub, event)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _call_async(
        self, sub: EventSubscription, event: TelemetryEvent
    ) -> None:
        try:
            await sub.handler(event)
        except Exception as exc:
            self._handler_errors[sub.subscriber_id] = (
                self._handler_errors.get(sub.subscriber_id, 0) + 1
            )
            self._error_count += 1

    def _call_sync(self, sub: EventSubscription, event: TelemetryEvent) -> None:
        try:
            sub.handler(event)
        except Exception:
            self._handler_errors[sub.subscriber_id] = (
                self._handler_errors.get(sub.subscriber_id, 0) + 1
            )
            self._error_count += 1

    def replay(
        self,
        event_types: Optional[set] = None,
        since_timestamp: Optional[float] = None,
        handler: Optional[Callable[[TelemetryEvent], None]] = None,
    ) -> List[TelemetryEvent]:
        events = [
            e for e in self._buffer
            if (event_types is None or e.event_type in event_types)
            and (since_timestamp is None or e.timestamp >= since_timestamp)
        ]
        if handler:
            for event in events:
                handler(event)
        return events

    def stats(self) -> dict:
        return {
            "total_emitted": self._emit_count,
            "buffer_size": len(self._buffer),
            "subscriber_count": len(self._subscriptions),
            "handler_errors": self._error_count,
            "disabled_subscribers": sum(
                1 for sid, ec in self._handler_errors.items()
                if ec >= self._max_handler_errors
            ),
        }
```

## Solution 4: Built-in Telemetry Subscribers

```python
import time
from collections import defaultdict
from typing import Dict, List


class MetricsAggregatorSubscriber:
    """
    Subscribes to all events and builds per-type counters and latency buckets.
    Can be queried for Prometheus-style metric snapshots.
    """

    def __init__(self, bus: StructuredEventBus):
        self._counts: Dict[str, int] = defaultdict(int)
        self._error_counts: Dict[str, int] = defaultdict(int)
        self._latencies: Dict[str, List[float]] = defaultdict(list)
        self._token_totals: Dict[str, int] = defaultdict(int)

        bus.subscribe(EventSubscription(
            subscriber_id="metrics_aggregator",
            handler=self._handle,
            event_types=None,   # all events
        ))

    def _handle(self, event: TelemetryEvent) -> None:
        key = event.event_type.value
        self._counts[key] += 1
        if event.error:
            self._error_counts[key] += 1
        if event.duration_ms is not None:
            self._latencies[key].append(event.duration_ms)
            if len(self._latencies[key]) > 10_000:
                self._latencies[key] = self._latencies[key][-5_000:]
        if event.token_count:
            self._token_totals[key] += event.token_count

    def snapshot(self) -> dict:
        result = {}
        for key in self._counts:
            latencies = self._latencies.get(key, [])
            result[key] = {
                "count": self._counts[key],
                "errors": self._error_counts.get(key, 0),
                "total_tokens": self._token_totals.get(key, 0),
                "avg_latency_ms": (
                    round(sum(latencies) / len(latencies), 1) if latencies else None
                ),
            }
        return result


class AuditLogSubscriber:
    """
    Subscribes to security-relevant events and writes to an append-only audit log.
    """

    AUDIT_TYPES = {
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ERROR,
        EventType.AGENT_ERROR,
        EventType.SESSION_START,
        EventType.SESSION_END,
        EventType.MEMORY_WRITE,
    }

    def __init__(self, bus: StructuredEventBus, max_entries: int = 100_000):
        self._log: List[dict] = []
        self._max = max_entries
        bus.subscribe(EventSubscription(
            subscriber_id="audit_log",
            handler=self._handle,
            event_types=self.AUDIT_TYPES,
        ))

    def _handle(self, event: TelemetryEvent) -> None:
        if len(self._log) >= self._max:
            self._log.pop(0)
        self._log.append(event.to_dict())

    def recent(self, hours: float = 1.0) -> List[dict]:
        cutoff = time.time() - hours * 3600
        return [e for e in self._log if e["timestamp"] >= cutoff]
```

## Solution 5: Event Emitter Mixin

```python
from typing import Optional


class TelemetryEmitterMixin:
    """
    Mixin that gives any agent class convenient emit_* methods.
    Attach a shared StructuredEventBus at class or instance level.
    """

    _event_bus: Optional[StructuredEventBus] = None

    def set_event_bus(self, bus: StructuredEventBus) -> None:
        self._event_bus = bus

    async def emit_tool_call_start(
        self, tool_name: str, session_id: str = "", **kwargs
    ) -> None:
        if self._event_bus:
            await self._event_bus.emit(TelemetryEvent(
                event_type=EventType.TOOL_CALL_START,
                session_id=session_id,
                tool_name=tool_name,
                payload=kwargs,
            ))

    async def emit_tool_call_end(
        self,
        tool_name: str,
        duration_ms: float,
        token_count: int = 0,
        session_id: str = "",
        error: Optional[str] = None,
    ) -> None:
        if self._event_bus:
            event_type = EventType.TOOL_CALL_ERROR if error else EventType.TOOL_CALL_END
            await self._event_bus.emit(TelemetryEvent(
                event_type=event_type,
                session_id=session_id,
                tool_name=tool_name,
                duration_ms=duration_ms,
                token_count=token_count,
                error=error,
            ))

    async def emit_model_request(
        self, model: str, token_count: int, session_id: str = ""
    ) -> None:
        if self._event_bus:
            await self._event_bus.emit(TelemetryEvent(
                event_type=EventType.MODEL_REQUEST,
                session_id=session_id,
                model=model,
                token_count=token_count,
            ))
```

## Solution 6: Event Bus Health Monitor

```python
import time


class EventBusHealthMonitor:
    """
    Monitors event bus throughput, subscriber health, and buffer utilization.
    Alerts when handlers are being disabled due to repeated errors.
    """

    def __init__(
        self,
        bus: StructuredEventBus,
        metrics: MetricsAggregatorSubscriber,
        max_disabled_subscribers: int = 0,
    ):
        self._bus = bus
        self._metrics = metrics
        self._max_disabled = max_disabled_subscribers

    def check(self) -> dict:
        stats = self._bus.stats()
        snapshot = self._metrics.snapshot()
        alerts = []

        if stats["disabled_subscribers"] > self._max_disabled:
            alerts.append({
                "type": "subscribers_disabled",
                "count": stats["disabled_subscribers"],
                "recommendation": "check subscriber handler implementations for exceptions",
            })

        buffer_util = stats["buffer_size"] / self._bus._buffer.maxlen
        if buffer_util > 0.90:
            alerts.append({
                "type": "buffer_near_full",
                "utilization": round(buffer_util, 3),
                "recommendation": "increase buffer_size or add faster consumers",
            })

        error_events = snapshot.get(EventType.AGENT_ERROR.value, {}).get("count", 0)
        tool_events = snapshot.get(EventType.TOOL_CALL_END.value, {}).get("count", 0)
        if tool_events > 0:
            tool_errors = snapshot.get(EventType.TOOL_CALL_ERROR.value, {}).get("count", 0)
            error_rate = tool_errors / max(tool_events + tool_errors, 1)
            if error_rate > 0.05:
                alerts.append({
                    "type": "high_tool_error_rate",
                    "rate": round(error_rate, 4),
                    "recommendation": "inspect tool_call.error events for root cause",
                })

        return {
            "generated_at": time.time(),
            "healthy": len(alerts) == 0,
            "bus_stats": stats,
            "event_summary": {k: v["count"] for k, v in snapshot.items()},
            "alerts": alerts,
        }
```

## Comparison

| Approach | Pub-Sub | Event Buffer | Replay | Filtering | Handler Safety |
|---|---|---|---|---|---|
| StructuredEventBus | Yes | Yes (ring) | Yes | Yes | Yes (error limit) |
| MetricsAggregatorSubscriber | Via bus | No | No | Via bus | No |
| AuditLogSubscriber | Via bus | No | No | AUDIT_TYPES | No |
| TelemetryEmitterMixin | Via bus | No | No | No | No |
| EventBusHealthMonitor | No | No | No | No | No (monitors) |

**Best for production**: Create one `StructuredEventBus` at application startup and inject it via `TelemetryEmitterMixin.set_event_bus()`. Register `MetricsAggregatorSubscriber` and `AuditLogSubscriber` at startup — they subscribe automatically. Add new consumers (Prometheus exporter, Datadog client, Slack alerter) as new subscriptions without touching emitter code. Use `bus.replay()` to backfill new subscribers from the ring buffer when they first connect. Monitor `EventBusHealthMonitor.check()` every minute — a disabled subscriber silently drops events, which is harder to debug than a noisy error.
