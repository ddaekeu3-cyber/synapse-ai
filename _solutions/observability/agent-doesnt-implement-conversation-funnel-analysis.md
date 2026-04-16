---
title: "Agent Doesn't Implement Conversation Funnel Analysis"
description: "AI agents that lack conversation funnel tracking cannot identify where users abandon multi-step workflows, which intent categories have the highest failure rates, or whether a prompt change improved task completion. Funnel analysis instruments each conversation stage—intent classification, tool invocation, response delivery, user follow-up—and aggregates drop-off rates to expose exactly where the agent loses users."
date: 2025-02-21
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-conversation-funnel-analysis
tags:
  - funnel-analysis
  - conversation-analytics
  - user-behavior
  - drop-off
  - completion-rate
  - observability
  - product-analytics
symptoms:
  - "No data on what percentage of conversations result in a completed task vs. abandoned mid-flow"
  - "Cannot tell if a new prompt change improved task completion rate or hurt it"
  - "Unknown which intent categories have the highest tool call failure rates"
  - "Product team asks 'where do users drop off?' and the answer is always 'we don't know'"
  - "Agent handles 10k conversations per day but completion rate is a mystery"
---

## Problem

Without funnel instrumentation, agent metrics show aggregate counts (total requests, total errors) but not the sequential stages a user passes through to complete a task. A conversation that reaches tool invocation but not response delivery indicates a tool failure. A conversation that receives a response but gets no follow-up indicates a dead end. A conversation that loops through clarification more than twice indicates intent ambiguity. Funnel analysis tracks each conversation through defined stages, records where it exits the funnel, and aggregates drop-off rates per stage, intent category, and time window—giving product and engineering teams actionable data for improvement.

---

## Solution 1: ConversationFunnel — Stage Tracking Per Session

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class FunnelStage(str, Enum):
    RECEIVED = "received"
    INTENT_CLASSIFIED = "intent_classified"
    TOOL_INVOKED = "tool_invoked"
    TOOL_SUCCEEDED = "tool_succeeded"
    RESPONSE_GENERATED = "response_generated"
    RESPONSE_DELIVERED = "response_delivered"
    FOLLOW_UP_RECEIVED = "follow_up_received"
    TASK_COMPLETED = "task_completed"


@dataclass
class FunnelEvent:
    stage: FunnelStage
    ts: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationFunnel:
    """
    Tracks a single conversation's progression through defined funnel stages.
    Records timestamps and metadata at each stage transition.
    Calculates drop stage, time-to-stage, and completion status.

    Usage:
        funnel = ConversationFunnel(session_id="sess-001", intent="search")
        funnel.advance(FunnelStage.INTENT_CLASSIFIED, confidence=0.92)
        funnel.advance(FunnelStage.TOOL_INVOKED, tool="web_search")
        funnel.advance(FunnelStage.TOOL_SUCCEEDED, result_count=5)
        funnel.advance(FunnelStage.RESPONSE_DELIVERED)
        funnel.complete()
        print(funnel.summary())
    """

    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    intent: str = ""
    user_id: str = ""
    started_at: float = field(default_factory=time.time)
    events: List[FunnelEvent] = field(default_factory=list)
    drop_stage: Optional[FunnelStage] = None
    completed: bool = False
    _stage_set: set = field(default_factory=set, repr=False)

    def advance(self, stage: FunnelStage, **metadata):
        self.events.append(FunnelEvent(stage=stage, ts=time.time(), metadata=metadata))
        self._stage_set.add(stage)

    def complete(self):
        self.advance(FunnelStage.TASK_COMPLETED)
        self.completed = True

    def drop(self, at_stage: FunnelStage, reason: str = ""):
        self.drop_stage = at_stage
        self.events.append(FunnelEvent(
            stage=at_stage, ts=time.time(),
            metadata={"drop": True, "reason": reason},
        ))

    def reached(self, stage: FunnelStage) -> bool:
        return stage in self._stage_set

    def time_to_stage(self, stage: FunnelStage) -> Optional[float]:
        for event in self.events:
            if event.stage == stage:
                return round(event.ts - self.started_at, 3)
        return None

    def last_stage(self) -> Optional[FunnelStage]:
        return self.events[-1].stage if self.events else None

    def summary(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "intent": self.intent,
            "user_id": self.user_id,
            "completed": self.completed,
            "drop_stage": self.drop_stage.value if self.drop_stage else None,
            "last_stage": self.last_stage().value if self.last_stage() else None,
            "total_stages": len(self.events),
            "duration_s": round(time.time() - self.started_at, 3),
            "time_to_tool_s": self.time_to_stage(FunnelStage.TOOL_INVOKED),
            "time_to_response_s": self.time_to_stage(FunnelStage.RESPONSE_DELIVERED),
        }
```

---

## Solution 2: FunnelStore — In-Memory Aggregation of Funnel Events

```python
import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FunnelStore:
    """
    Aggregates ConversationFunnel summaries and computes per-stage
    drop-off rates, per-intent completion rates, and time-to-stage
    percentiles. Designed for lightweight in-process aggregation;
    flush to a time-series database for long-term storage.

    Usage:
        store = FunnelStore()
        store.record(funnel)
        report = store.drop_off_report()
        intent_report = store.intent_completion_report()
    """

    def __init__(self, max_records: int = 50_000):
        self._max = max_records
        self._summaries: List[Dict[str, Any]] = []
        self._stage_counts: Dict[str, int] = defaultdict(int)
        self._drop_counts: Dict[str, int] = defaultdict(int)
        self._intent_stats: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"total": 0, "completed": 0, "dropped": 0}
        )

    def record(self, funnel: ConversationFunnel):
        summary = funnel.summary()
        if len(self._summaries) >= self._max:
            self._summaries.pop(0)
        self._summaries.append(summary)

        for event in funnel.events:
            self._stage_counts[event.stage.value] += 1

        if funnel.drop_stage:
            self._drop_counts[funnel.drop_stage.value] += 1

        intent = funnel.intent or "unknown"
        self._intent_stats[intent]["total"] += 1
        if funnel.completed:
            self._intent_stats[intent]["completed"] += 1
        elif funnel.drop_stage:
            self._intent_stats[intent]["dropped"] += 1

        logger.info("funnel_recorded session=%s completed=%s drop_stage=%s intent=%s",
                     funnel.session_id, funnel.completed,
                     funnel.drop_stage.value if funnel.drop_stage else None, funnel.intent)

    def drop_off_report(self) -> Dict[str, Any]:
        """Compute drop-off rate at each funnel stage."""
        stages = [s.value for s in FunnelStage]
        total = len(self._summaries)
        if total == 0:
            return {"total_conversations": 0}

        report = {"total_conversations": total, "stages": {}}
        for stage in stages:
            reached = self._stage_counts.get(stage, 0)
            dropped = self._drop_counts.get(stage, 0)
            report["stages"][stage] = {
                "reached": reached,
                "reached_pct": round(reached / total * 100, 1),
                "dropped_here": dropped,
                "drop_rate_pct": round(dropped / max(reached, 1) * 100, 1),
            }
        return report

    def intent_completion_report(self) -> Dict[str, Any]:
        return {
            intent: {
                "total": s["total"],
                "completed": s["completed"],
                "completion_rate_pct": round(s["completed"] / max(s["total"], 1) * 100, 1),
                "drop_rate_pct": round(s["dropped"] / max(s["total"], 1) * 100, 1),
            }
            for intent, s in sorted(
                self._intent_stats.items(),
                key=lambda x: x[1]["total"],
                reverse=True,
            )
        }

    def recent(self, n: int = 100) -> List[Dict]:
        return self._summaries[-n:]
```

---

## Solution 3: FunnelMiddleware — Automatic Stage Advancement via Decorator

```python
import asyncio
import functools
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class FunnelMiddleware:
    """
    Provides decorators that automatically advance funnel stages when
    agent handler functions are called and completed successfully.
    Eliminates the need to manually call funnel.advance() inside
    each tool handler.

    Usage:
        middleware = FunnelMiddleware(store=funnel_store)

        @middleware.stage(FunnelStage.TOOL_INVOKED, FunnelStage.TOOL_SUCCEEDED)
        async def call_tool(funnel, tool_name, **kwargs):
            return await tool_registry.execute(tool_name, **kwargs)
    """

    def __init__(self, store: FunnelStore):
        self._store = store

    def stage(
        self,
        on_enter: FunnelStage,
        on_success: Optional[FunnelStage] = None,
        on_failure_drop: bool = True,
    ):
        """Decorator: advances funnel stage on function entry and success."""
        def decorator(fn: Callable):
            @functools.wraps(fn)
            async def async_wrapper(funnel: ConversationFunnel, *args, **kwargs):
                funnel.advance(on_enter, **{k: str(v)[:100] for k, v in kwargs.items()
                                             if not k.startswith("_")})
                try:
                    result = await fn(funnel, *args, **kwargs)
                    if on_success:
                        funnel.advance(on_success)
                    return result
                except Exception as exc:
                    if on_failure_drop:
                        funnel.drop(on_enter, reason=type(exc).__name__)
                        self._store.record(funnel)
                    logger.error("funnel_stage_failed stage=%s error=%s",
                                  on_enter.value, exc)
                    raise

            @functools.wraps(fn)
            def sync_wrapper(funnel: ConversationFunnel, *args, **kwargs):
                funnel.advance(on_enter)
                try:
                    result = fn(funnel, *args, **kwargs)
                    if on_success:
                        funnel.advance(on_success)
                    return result
                except Exception as exc:
                    if on_failure_drop:
                        funnel.drop(on_enter, reason=type(exc).__name__)
                        self._store.record(funnel)
                    raise

            return async_wrapper if asyncio.iscoroutinefunction(fn) else sync_wrapper
        return decorator
```

---

## Solution 4: FunnelExporter — Emit Events to Analytics Backend

```python
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class FunnelExporter:
    """
    Batches funnel summary records and flushes them to an analytics
    backend (Segment, Amplitude, BigQuery, S3) at a configurable interval.
    Supports pluggable export functions to avoid hard-coding a specific sink.

    Usage:
        exporter = FunnelExporter(
            export_fn=segment_track_batch,
            batch_size=100,
            flush_interval=30.0,
        )
        exporter.submit(funnel)
        await exporter.flush()  # or run as a background task
    """

    def __init__(
        self,
        export_fn: Callable[[List[Dict]], None],
        batch_size: int = 100,
        flush_interval: float = 30.0,
    ):
        self._export = export_fn
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._buffer: List[Dict[str, Any]] = []
        self._last_flush = time.monotonic()

    def submit(self, funnel: ConversationFunnel):
        summary = funnel.summary()
        summary["exported_at"] = time.time()
        self._buffer.append(summary)
        if len(self._buffer) >= self._batch_size:
            self._flush_sync()

    def _flush_sync(self):
        if not self._buffer:
            return
        batch = list(self._buffer)
        self._buffer.clear()
        try:
            self._export(batch)
            logger.info("funnel_export_flushed count=%d", len(batch))
        except Exception as exc:
            logger.error("funnel_export_failed error=%s count=%d", exc, len(batch))
            # Re-queue for next flush
            self._buffer[:0] = batch

    async def flush(self):
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._flush_sync)
        self._last_flush = time.monotonic()

    async def run_background(self):
        import asyncio
        while True:
            await asyncio.sleep(self._flush_interval)
            if time.monotonic() - self._last_flush >= self._flush_interval:
                await self.flush()

    @staticmethod
    def jsonl_writer(path: str) -> Callable[[List[Dict]], None]:
        """Factory: creates an export function that appends to a JSONL file."""
        def _write(batch: List[Dict]):
            with open(path, "a") as f:
                for record in batch:
                    f.write(json.dumps(record) + "\n")
        return _write
```

---

## Solution 5: FunnelAlerter — Alert on Drop-Off Rate Threshold Breach

```python
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FunnelAlert:
    stage: str
    drop_rate_pct: float
    threshold_pct: float
    sample_size: int
    ts: float = 0.0

    def __post_init__(self):
        if not self.ts:
            self.ts = time.time()

    def __str__(self):
        return (
            f"FunnelAlert: drop_rate at '{self.stage}' is {self.drop_rate_pct:.1f}% "
            f"(threshold={self.threshold_pct:.1f}%, n={self.sample_size})"
        )


class FunnelAlerter:
    """
    Periodically checks drop-off rates from a FunnelStore and fires
    alert callbacks when any stage exceeds its configured threshold.
    Prevents alert fatigue with per-stage cooldown periods.

    Usage:
        alerter = FunnelAlerter(
            store=funnel_store,
            thresholds={
                "tool_invoked": 5.0,       # alert if >5% drop at tool invoke
                "response_delivered": 10.0,
            },
            notify_fn=send_slack_alert,
        )
        await alerter.check()
    """

    def __init__(
        self,
        store: FunnelStore,
        thresholds: Dict[str, float],
        notify_fn: Optional[Callable[[FunnelAlert], None]] = None,
        cooldown_seconds: float = 300.0,
        min_sample: int = 50,
    ):
        self._store = store
        self._thresholds = thresholds
        self._notify = notify_fn
        self._cooldown = cooldown_seconds
        self._min_sample = min_sample
        self._last_alert: Dict[str, float] = {}

    def check(self) -> List[FunnelAlert]:
        report = self._store.drop_off_report()
        total = report.get("total_conversations", 0)
        if total < self._min_sample:
            return []

        fired: List[FunnelAlert] = []
        now = time.time()
        stages = report.get("stages", {})

        for stage, threshold in self._thresholds.items():
            info = stages.get(stage, {})
            drop_rate = info.get("drop_rate_pct", 0.0)
            reached = info.get("reached", 0)

            if reached < self._min_sample:
                continue
            if drop_rate <= threshold:
                continue
            if now - self._last_alert.get(stage, 0) < self._cooldown:
                continue

            alert = FunnelAlert(
                stage=stage,
                drop_rate_pct=drop_rate,
                threshold_pct=threshold,
                sample_size=reached,
            )
            fired.append(alert)
            self._last_alert[stage] = now
            logger.warning("funnel_alert %s", alert)
            if self._notify:
                try:
                    self._notify(alert)
                except Exception as exc:
                    logger.error("funnel_alert_notify_failed error=%s", exc)

        return fired
```

---

## Solution 6: FunnelDashboard — In-Process Metrics Summary for Operator Queries

```python
import time
from typing import Any, Dict, List, Optional


class FunnelDashboard:
    """
    Composes FunnelStore reports into a unified dashboard dict suitable
    for a health endpoint, operator CLI, or Grafana JSON datasource.
    Provides hourly and daily aggregations and highlights the worst-performing
    intent categories.

    Usage:
        dashboard = FunnelDashboard(store=funnel_store, alerter=alerter)
        data = dashboard.render()
        # Serve via: GET /internal/funnel-dashboard
    """

    def __init__(self, store: FunnelStore, alerter: Optional[FunnelAlerter] = None):
        self._store = store
        self._alerter = alerter

    def render(self) -> Dict[str, Any]:
        drop_report = self._store.drop_off_report()
        intent_report = self._store.intent_completion_report()

        worst_intents = sorted(
            intent_report.items(),
            key=lambda x: x[1].get("completion_rate_pct", 100),
        )[:5]

        best_intents = sorted(
            intent_report.items(),
            key=lambda x: x[1].get("completion_rate_pct", 0),
            reverse=True,
        )[:5]

        recent = self._store.recent(200)
        overall_completion = round(
            sum(1 for r in recent if r.get("completed")) / max(len(recent), 1) * 100, 1
        )

        alerts = self._alerter.check() if self._alerter else []

        return {
            "generated_at": time.time(),
            "overall_completion_rate_pct": overall_completion,
            "sample_size": len(recent),
            "drop_off_by_stage": drop_report.get("stages", {}),
            "worst_intents": [
                {"intent": k, **v} for k, v in worst_intents
            ],
            "best_intents": [
                {"intent": k, **v} for k, v in best_intents
            ],
            "active_alerts": [str(a) for a in alerts],
            "total_intents": len(intent_report),
        }
```

---

## Comparison

| Approach | Per-Session Tracking | Aggregation | Drop-Off Rates | Intent Breakdown | Alerting | Export |
|---|---|---|---|---|---|---|
| **ConversationFunnel** | Yes | No | Per session | Via intent field | No | No |
| **FunnelStore** | No | Yes | Yes | Yes | No | No |
| **FunnelMiddleware** | Via decorator | No | Auto drop | No | No | No |
| **FunnelExporter** | No | Batched | No | No | No | Yes |
| **FunnelAlerter** | No | Via store | Threshold | No | Yes | No |
| **FunnelDashboard** | No | Composed | Yes | Yes | Via alerter | No |

**Key insight**: start by adding `ConversationFunnel` instrumentation to the top-level request handler and recording it in `FunnelStore` on completion or error. With 100 conversations, you will immediately see which stage has the highest drop rate. The most common finding: 15-30% of conversations drop at `TOOL_SUCCEEDED → RESPONSE_GENERATED`, indicating that tool results are returned but the LLM fails to synthesize them into a useful answer—pointing to a prompt issue, not a tool issue. Add `FunnelAlerter` with a 20% threshold on `tool_invoked` drop-off to get notified when a tool starts failing silently without triggering the error counter.
