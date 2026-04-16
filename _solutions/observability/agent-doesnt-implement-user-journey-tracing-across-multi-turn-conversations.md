---
title: "Agent Doesn't Implement User Journey Tracing Across Multi-Turn Conversations"
description: "How to trace complete user journeys — from first message to final outcome — across multi-turn conversations, agent handoffs, and tool calls using conversation-scoped trace contexts, funnel analytics, and drop-off detection."
date: 2025-01-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-user-journey-tracing-across-multi-turn-conversations
tags:
  - observability
  - tracing
  - user-journey
  - conversation
  - multi-turn
  - funnel-analytics
  - drop-off-detection
symptoms:
  - "No visibility into how users progress through multi-step agent workflows"
  - "Cannot identify which conversation turns cause users to abandon sessions"
  - "Distributed traces show individual tool calls but not the complete user journey"
  - "Impossible to correlate conversation quality with downstream user outcomes"
  - "No funnel analysis to know where complex tasks break down"
  - "Agent handoffs lose conversation context and break trace continuity"
---

## Why This Happens

Standard distributed tracing (OpenTelemetry, Jaeger) excels at capturing individual request spans but treats each API call as an independent event. A multi-turn AI conversation is fundamentally different: it's a *stateful journey* spanning minutes or hours, with causally linked turns, tool calls, agent handoffs, and an eventual outcome. Without conversation-scoped trace context, you know that a tool call took 200ms but not *which user goal* it served, *how many turns* the user needed to reach satisfaction, or *at what step* most users give up.

The solution is a two-level tracing model: low-level span traces for latency and errors, and high-level journey traces that stitch together the entire conversation as a single observable unit.

---

## Solution 1: Conversation-Scoped Trace Context

Attach a persistent `journey_id` to every event in a conversation. This ID propagates through all turns, tool calls, and agent handoffs, making the complete journey queryable as a single unit.

```python
import uuid
import time
import contextvars
from dataclasses import dataclass, field
from typing import Any, Optional

# Async-safe context variable for the current journey
_current_journey: contextvars.ContextVar["JourneyContext"] = contextvars.ContextVar(
    "_current_journey"
)

@dataclass
class JourneyContext:
    journey_id: str
    user_id: Optional[str]
    session_id: str
    started_at: float
    turn_number: int = 0
    goal: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @classmethod
    def new(cls, user_id: Optional[str] = None, goal: Optional[str] = None) -> "JourneyContext":
        return cls(
            journey_id=str(uuid.uuid4()),
            user_id=user_id,
            session_id=str(uuid.uuid4()),
            started_at=time.time(),
            goal=goal,
        )

    def next_turn(self) -> "JourneyContext":
        import copy
        ctx = copy.copy(self)
        ctx.turn_number += 1
        return ctx

    def to_headers(self) -> dict[str, str]:
        """Serialize for propagation across service boundaries."""
        return {
            "X-Journey-ID":   self.journey_id,
            "X-Session-ID":   self.session_id,
            "X-User-ID":      self.user_id or "",
            "X-Turn-Number":  str(self.turn_number),
            "X-Journey-Goal": self.goal or "",
        }

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> "JourneyContext":
        return cls(
            journey_id=headers.get("X-Journey-ID", str(uuid.uuid4())),
            user_id=headers.get("X-User-ID") or None,
            session_id=headers.get("X-Session-ID", str(uuid.uuid4())),
            started_at=time.time(),
            turn_number=int(headers.get("X-Turn-Number", 0)),
            goal=headers.get("X-Journey-Goal") or None,
        )


def get_journey() -> Optional[JourneyContext]:
    return _current_journey.get(None)

def set_journey(ctx: JourneyContext) -> contextvars.Token:
    return _current_journey.set(ctx)


class JourneyPropagator:
    """Injects/extracts journey context from HTTP headers for cross-service propagation."""

    def inject(self, headers: dict) -> None:
        ctx = get_journey()
        if ctx:
            headers.update(ctx.to_headers())

    def extract(self, headers: dict) -> Optional[JourneyContext]:
        if "X-Journey-ID" in headers:
            return JourneyContext.from_headers(headers)
        return None
```

---

## Solution 2: Journey Event Recorder

Every significant event in the conversation — user message, agent reply, tool call, handoff, error — is recorded as a typed journey event with the journey ID attached.

```python
import asyncio
import json
import logging
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)

class JourneyEventType(str, Enum):
    JOURNEY_START      = "journey_start"
    USER_MESSAGE       = "user_message"
    AGENT_REPLY        = "agent_reply"
    TOOL_CALL_START    = "tool_call_start"
    TOOL_CALL_END      = "tool_call_end"
    AGENT_HANDOFF      = "agent_handoff"
    ERROR              = "error"
    GOAL_ACHIEVED      = "goal_achieved"
    USER_ABANDONED     = "user_abandoned"
    JOURNEY_END        = "journey_end"

@dataclass
class JourneyEvent:
    journey_id: str
    session_id: str
    event_type: JourneyEventType
    turn_number: int
    timestamp: float
    user_id: Optional[str] = None
    data: dict = field(default_factory=dict)
    duration_ms: Optional[float] = None
    parent_event_id: Optional[str] = None
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        return d


class JourneyEventRecorder:
    """
    Records journey events to a pluggable backend.
    Supports batching for high-throughput environments.
    """

    def __init__(self, backend: "JourneyBackend", batch_size: int = 50, flush_interval: float = 5.0):
        self.backend = backend
        self._buffer: list[JourneyEvent] = []
        self._lock = asyncio.Lock()
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._flush_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._flush_task = asyncio.create_task(self._periodic_flush())

    async def record(self, event: JourneyEvent) -> None:
        async with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= self._batch_size:
                await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()
        try:
            await self.backend.write_batch(batch)
        except Exception as exc:
            logger.error("Failed to flush journey events: %s", exc)
            # Re-buffer on failure (with limit to prevent unbounded growth)
            self._buffer = batch[:500] + self._buffer

    async def _periodic_flush(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            async with self._lock:
                await self._flush_locked()

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def stop(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
        await self.flush()


class JourneyBackend:
    """Abstract backend — implement for your storage system."""
    async def write_batch(self, events: list[JourneyEvent]) -> None:
        raise NotImplementedError

class LoggingJourneyBackend(JourneyBackend):
    async def write_batch(self, events: list[JourneyEvent]) -> None:
        for e in events:
            logger.info("JOURNEY_EVENT %s", json.dumps(e.to_dict()))

class InMemoryJourneyBackend(JourneyBackend):
    def __init__(self):
        self.events: list[JourneyEvent] = []

    async def write_batch(self, events: list[JourneyEvent]) -> None:
        self.events.extend(events)

    def get_journey(self, journey_id: str) -> list[JourneyEvent]:
        return [e for e in self.events if e.journey_id == journey_id]
```

---

## Solution 3: Instrumented Conversation Agent

A mixin that automatically records journey events around every conversation turn and tool call.

```python
import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

class JourneyTracedAgent:
    """
    Agent mixin that automatically records journey events.
    Wraps turn execution and tool calls with journey event recording.
    """

    def __init__(self, recorder: JourneyEventRecorder):
        self.recorder = recorder

    async def start_journey(
        self,
        user_id: Optional[str] = None,
        goal: Optional[str] = None,
        initial_message: Optional[str] = None,
    ) -> JourneyContext:
        ctx = JourneyContext.new(user_id=user_id, goal=goal)
        set_journey(ctx)

        await self.recorder.record(JourneyEvent(
            journey_id=ctx.journey_id,
            session_id=ctx.session_id,
            event_type=JourneyEventType.JOURNEY_START,
            turn_number=0,
            timestamp=time.time(),
            user_id=user_id,
            data={"goal": goal, "initial_message": initial_message},
        ))
        return ctx

    async def record_user_message(self, message: str, metadata: dict | None = None) -> None:
        ctx = get_journey()
        if not ctx:
            return
        ctx = ctx.next_turn()
        set_journey(ctx)

        await self.recorder.record(JourneyEvent(
            journey_id=ctx.journey_id,
            session_id=ctx.session_id,
            event_type=JourneyEventType.USER_MESSAGE,
            turn_number=ctx.turn_number,
            timestamp=time.time(),
            user_id=ctx.user_id,
            data={
                "message_length": len(message),
                "message_preview": message[:100],
                **(metadata or {}),
            },
        ))

    async def record_agent_reply(
        self,
        reply: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
    ) -> None:
        ctx = get_journey()
        if not ctx:
            return
        await self.recorder.record(JourneyEvent(
            journey_id=ctx.journey_id,
            session_id=ctx.session_id,
            event_type=JourneyEventType.AGENT_REPLY,
            turn_number=ctx.turn_number,
            timestamp=time.time(),
            user_id=ctx.user_id,
            duration_ms=latency_ms,
            data={
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reply_length": len(reply),
            },
        ))

    @asynccontextmanager
    async def trace_tool_call(
        self, tool_name: str, args: dict
    ) -> AsyncGenerator[None, None]:
        ctx = get_journey()
        start = time.time()
        event_id = str(uuid.uuid4())

        if ctx:
            await self.recorder.record(JourneyEvent(
                journey_id=ctx.journey_id,
                session_id=ctx.session_id,
                event_type=JourneyEventType.TOOL_CALL_START,
                turn_number=ctx.turn_number,
                timestamp=start,
                user_id=ctx.user_id,
                event_id=event_id,
                data={"tool": tool_name, "args_preview": str(args)[:200]},
            ))
        try:
            yield
            status = "success"
        except Exception as exc:
            status = f"error: {exc}"
            raise
        finally:
            if ctx:
                await self.recorder.record(JourneyEvent(
                    journey_id=ctx.journey_id,
                    session_id=ctx.session_id,
                    event_type=JourneyEventType.TOOL_CALL_END,
                    turn_number=ctx.turn_number,
                    timestamp=time.time(),
                    user_id=ctx.user_id,
                    parent_event_id=event_id,
                    duration_ms=(time.time() - start) * 1000,
                    data={"tool": tool_name, "status": status},
                ))

    async def end_journey(self, outcome: str, goal_achieved: bool = False) -> None:
        ctx = get_journey()
        if not ctx:
            return
        event_type = JourneyEventType.GOAL_ACHIEVED if goal_achieved else JourneyEventType.JOURNEY_END
        await self.recorder.record(JourneyEvent(
            journey_id=ctx.journey_id,
            session_id=ctx.session_id,
            event_type=event_type,
            turn_number=ctx.turn_number,
            timestamp=time.time(),
            user_id=ctx.user_id,
            duration_ms=(time.time() - ctx.started_at) * 1000,
            data={
                "outcome": outcome,
                "total_turns": ctx.turn_number,
                "goal_achieved": goal_achieved,
            },
        ))
```

---

## Solution 4: Journey Funnel Analyzer

Query recorded journey events to compute drop-off rates at each conversation step.

```python
from collections import defaultdict
from dataclasses import dataclass

@dataclass
class FunnelStep:
    name: str
    event_type: JourneyEventType
    count: int
    drop_off_rate: float  # fraction who did not reach next step

@dataclass
class JourneyFunnelReport:
    total_journeys: int
    completed_journeys: int
    completion_rate: float
    avg_turns_to_completion: float
    avg_duration_seconds: float
    steps: list[FunnelStep]
    top_drop_off_steps: list[str]

class JourneyFunnelAnalyzer:
    """
    Analyzes recorded journey events to produce funnel metrics and drop-off reports.
    """

    def __init__(self, backend: InMemoryJourneyBackend):
        self.backend = backend

    def analyze(self, funnel_steps: list[JourneyEventType] | None = None) -> JourneyFunnelReport:
        if funnel_steps is None:
            funnel_steps = [
                JourneyEventType.JOURNEY_START,
                JourneyEventType.USER_MESSAGE,
                JourneyEventType.TOOL_CALL_START,
                JourneyEventType.AGENT_REPLY,
                JourneyEventType.GOAL_ACHIEVED,
            ]

        # Group events by journey_id
        by_journey: dict[str, list[JourneyEvent]] = defaultdict(list)
        for e in self.backend.events:
            by_journey[e.journey_id].append(e)

        total = len(by_journey)
        if total == 0:
            return JourneyFunnelReport(0, 0, 0.0, 0.0, 0.0, [], [])

        # Count journeys that reached each funnel step
        step_counts: dict[str, int] = {s.value: 0 for s in funnel_steps}
        completed = 0
        turns_list: list[int] = []
        durations: list[float] = []

        for jid, events in by_journey.items():
            event_types = {e.event_type for e in events}

            for step in funnel_steps:
                if step in event_types:
                    step_counts[step.value] += 1

            if JourneyEventType.GOAL_ACHIEVED in event_types:
                completed += 1
                end_events = [e for e in events if e.event_type == JourneyEventType.GOAL_ACHIEVED]
                if end_events:
                    durations.append((end_events[-1].duration_ms or 0) / 1000)
                max_turn = max((e.turn_number for e in events), default=0)
                turns_list.append(max_turn)

        # Build funnel steps with drop-off rates
        steps_report = []
        prev_count = total
        for i, step in enumerate(funnel_steps):
            count = step_counts[step.value]
            drop_off = 1.0 - (count / prev_count) if prev_count > 0 else 0.0
            steps_report.append(FunnelStep(
                name=step.value,
                event_type=step,
                count=count,
                drop_off_rate=round(drop_off, 3),
            ))
            prev_count = count

        top_drop_offs = sorted(steps_report, key=lambda s: s.drop_off_rate, reverse=True)

        return JourneyFunnelReport(
            total_journeys=total,
            completed_journeys=completed,
            completion_rate=round(completed / total, 3),
            avg_turns_to_completion=round(sum(turns_list) / len(turns_list), 1) if turns_list else 0.0,
            avg_duration_seconds=round(sum(durations) / len(durations), 1) if durations else 0.0,
            steps=steps_report,
            top_drop_off_steps=[s.name for s in top_drop_offs[:3]],
        )

    def get_abandoned_journeys(self, idle_threshold_seconds: float = 300.0) -> list[str]:
        """Return journey IDs where the last event was more than threshold seconds ago with no completion."""
        now = time.time()
        abandoned = []
        by_journey: dict[str, list[JourneyEvent]] = defaultdict(list)
        for e in self.backend.events:
            by_journey[e.journey_id].append(e)

        for jid, events in by_journey.items():
            last_ts = max(e.timestamp for e in events)
            has_end = any(e.event_type in (JourneyEventType.GOAL_ACHIEVED, JourneyEventType.JOURNEY_END)
                         for e in events)
            if not has_end and (now - last_ts) > idle_threshold_seconds:
                abandoned.append(jid)
        return abandoned
```

---

## Solution 5: Agent Handoff Trace Continuity

When a conversation is handed from one agent to another, the journey context must propagate so tracing is not broken.

```python
@dataclass
class HandoffPacket:
    journey_id: str
    session_id: str
    user_id: Optional[str]
    turn_number: int
    from_agent: str
    to_agent: str
    reason: str
    conversation_summary: str
    metadata: dict = field(default_factory=dict)

class HandoffTracer:
    """Records and propagates journey context across agent handoffs."""

    def __init__(self, recorder: JourneyEventRecorder, agent_name: str):
        self.recorder = recorder
        self.agent_name = agent_name

    async def initiate_handoff(
        self,
        target_agent: str,
        reason: str,
        conversation_summary: str,
    ) -> HandoffPacket:
        ctx = get_journey()
        if ctx is None:
            raise RuntimeError("No active journey context for handoff")

        packet = HandoffPacket(
            journey_id=ctx.journey_id,
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            turn_number=ctx.turn_number,
            from_agent=self.agent_name,
            to_agent=target_agent,
            reason=reason,
            conversation_summary=conversation_summary,
        )

        await self.recorder.record(JourneyEvent(
            journey_id=ctx.journey_id,
            session_id=ctx.session_id,
            event_type=JourneyEventType.AGENT_HANDOFF,
            turn_number=ctx.turn_number,
            timestamp=time.time(),
            user_id=ctx.user_id,
            data={
                "from_agent": self.agent_name,
                "to_agent": target_agent,
                "reason": reason,
            },
        ))
        return packet

    async def receive_handoff(self, packet: HandoffPacket) -> JourneyContext:
        """Restore journey context from a handoff packet."""
        ctx = JourneyContext(
            journey_id=packet.journey_id,
            user_id=packet.user_id,
            session_id=packet.session_id,
            started_at=time.time(),
            turn_number=packet.turn_number,
        )
        set_journey(ctx)
        return ctx
```

---

## Solution 6: Journey Dashboard Metrics Exporter

Export journey metrics to Prometheus or a time-series database for real-time dashboards.

```python
from collections import Counter

class JourneyMetricsExporter:
    """
    Exports journey funnel and quality metrics as Prometheus-compatible gauges.
    """

    def __init__(self, backend: InMemoryJourneyBackend):
        self.backend = backend

    def compute_metrics(self) -> dict[str, float]:
        events = self.backend.events
        if not events:
            return {}

        by_journey: dict[str, list[JourneyEvent]] = defaultdict(list)
        for e in events:
            by_journey[e.journey_id].append(e)

        total = len(by_journey)
        completed = sum(
            1 for evts in by_journey.values()
            if any(e.event_type == JourneyEventType.GOAL_ACHIEVED for e in evts)
        )
        error_journeys = sum(
            1 for evts in by_journey.values()
            if any(e.event_type == JourneyEventType.ERROR for e in evts)
        )

        tool_calls = [e for e in events if e.event_type == JourneyEventType.TOOL_CALL_END]
        tool_latencies = [e.duration_ms for e in tool_calls if e.duration_ms is not None]

        turn_counts = []
        for evts in by_journey.values():
            max_turn = max((e.turn_number for e in evts), default=0)
            turn_counts.append(max_turn)

        return {
            "journey_total": float(total),
            "journey_completed": float(completed),
            "journey_completion_rate": completed / total if total else 0.0,
            "journey_error_rate": error_journeys / total if total else 0.0,
            "journey_avg_turns": sum(turn_counts) / len(turn_counts) if turn_counts else 0.0,
            "tool_call_count": float(len(tool_calls)),
            "tool_call_avg_latency_ms": sum(tool_latencies) / len(tool_latencies) if tool_latencies else 0.0,
            "tool_call_p95_latency_ms": float(sorted(tool_latencies)[int(len(tool_latencies) * 0.95)]) if tool_latencies else 0.0,
        }

    def format_prometheus(self) -> str:
        lines = []
        for name, value in self.compute_metrics().items():
            lines.append(f"agent_journey_{name} {value:.4f}")
        return "\n".join(lines)
```

---

## Comparison

| Solution | Scope | Data Captured | Drop-off Detection | Distributed | Best For |
|---|---|---|---|---|---|
| Journey Context Propagation | Per conversation | Journey ID + metadata | No | Yes (headers) | Foundation for all other solutions |
| Event Recorder | Per event | All event types + data | Indirect | Yes (batched) | Full audit trail |
| Instrumented Agent | Per turn + tool | Turn, tool, reply events | No | No | Easy instrumentation |
| Funnel Analyzer | Aggregate | Step counts + drop rates | Yes | No | Product analytics |
| Handoff Tracer | Cross-agent | Handoff events | No | Yes | Multi-agent continuity |
| Metrics Exporter | Aggregate | Prometheus metrics | Indirect | No | Real-time dashboards |

**Start with journey context propagation** — without a persistent `journey_id` all other solutions are disconnected. **Add the event recorder** with a logging or database backend to capture the full event stream. **Use the instrumented agent mixin** to automate event emission at turn and tool call boundaries. **Run the funnel analyzer** periodically (e.g., daily) to identify which conversation steps have the highest abandonment rates. **Add the metrics exporter** for real-time dashboards tracking completion rate and tool latency P95.
