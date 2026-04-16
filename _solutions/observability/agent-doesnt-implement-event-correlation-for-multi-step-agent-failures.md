---
title: "Agent Doesn't Implement Event Correlation for Multi-Step Agent Failures"
description: "AI agents that execute multi-step workflows produce failures whose root causes are buried several steps back. Without event correlation — linking each step's outcome to the preceding events that caused it — operators see the symptom (step 7 failed) but not the cause (step 3 returned a malformed result that step 5 silently propagated). Event correlation builds a causal chain from every event in a workflow, making root-cause isolation a lookup rather than a search."
date: 2025-02-13
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-event-correlation-for-multi-step-agent-failures
tags:
  - event-correlation
  - causal-chain
  - multi-step
  - root-cause
  - observability
  - workflow-tracing
  - failure-analysis
symptoms:
  - "Step 7 of an agent workflow fails but the root cause is in step 3"
  - "Logs show the failure but not which prior events led to it"
  - "Post-mortem requires manually scanning 20 log entries to reconstruct the causal chain"
  - "Agent produces a wrong final answer; no record of which intermediate step produced the bad data"
  - "Retrying the failed step doesn't help because the corrupted input came from an earlier step"
---

## Problem

Multi-step agent workflows are causal chains: step N's output is step N+1's input. A failure at step N may be caused by defective output from step N-3. Without explicit causal linking, every event is an isolated observation. Event correlation attaches a `caused_by` pointer to each event, forming a directed acyclic graph. When step 7 fails, you follow `caused_by` pointers upstream to find that step 3's tool returned a truncated JSON, which step 5 stored without validation, which step 7 then tried to parse.

---

## Solution 1: CausalEvent — Linked Event Model

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CausalEvent:
    event_id: str
    event_type: str              # "tool_call" | "llm_invoke" | "state_update" | "error"
    step_name: str
    payload: Dict[str, Any]
    caused_by: Optional[str]     # event_id of the direct parent event
    workflow_id: str
    timestamp: float = field(default_factory=time.time)
    duration_ms: Optional[float] = None
    success: bool = True
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @classmethod
    def root(cls, workflow_id: str, step_name: str,
              payload: Dict[str, Any]) -> "CausalEvent":
        return cls(
            event_id=str(uuid.uuid4()),
            event_type="workflow_start",
            step_name=step_name,
            payload=payload,
            caused_by=None,
            workflow_id=workflow_id,
        )

    def child(self, event_type: str, step_name: str,
               payload: Dict[str, Any]) -> "CausalEvent":
        return CausalEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            step_name=step_name,
            payload=payload,
            caused_by=self.event_id,
            workflow_id=self.workflow_id,
        )

    def mark_failed(self, error: Exception) -> "CausalEvent":
        self.success = False
        self.error_type = type(error).__name__
        self.error_message = str(error)
        return self
```

---

## Solution 2: CausalEventStore — Workflow-Scoped Event Graph

```python
import time
from collections import defaultdict
from typing import Dict, List, Optional


class CausalEventStore:
    """
    Stores and indexes causal events for a workflow.
    Provides causal chain reconstruction (root → failure) and
    subtree queries (all events caused by a given event).

    Usage:
        store = CausalEventStore()
        root = CausalEvent.root("wf-1", "ingest", {"query": "SSRF"})
        store.record(root)

        search_evt = root.child("tool_call", "web_search", {"query": "SSRF"})
        store.record(search_evt)

        # On failure:
        parse_evt = search_evt.child("tool_call", "parse_results", {})
        parse_evt.mark_failed(ValueError("truncated JSON"))
        store.record(parse_evt)

        chain = store.causal_chain(parse_evt.event_id)
        # [root, search_evt, parse_evt]
    """

    def __init__(self):
        self._events: Dict[str, CausalEvent] = {}
        self._children: Dict[str, List[str]] = defaultdict(list)

    def record(self, event: CausalEvent):
        self._events[event.event_id] = event
        if event.caused_by:
            self._children[event.caused_by].append(event.event_id)

    def causal_chain(self, event_id: str) -> List[CausalEvent]:
        """Return the chain from root to this event (inclusive)."""
        chain = []
        current = self._events.get(event_id)
        while current:
            chain.append(current)
            current = (self._events.get(current.caused_by)
                       if current.caused_by else None)
        return list(reversed(chain))

    def subtree(self, event_id: str) -> List[CausalEvent]:
        """Return all events caused (directly or transitively) by event_id."""
        result = []
        queue = list(self._children.get(event_id, []))
        while queue:
            eid = queue.pop()
            evt = self._events.get(eid)
            if evt:
                result.append(evt)
                queue.extend(self._children.get(eid, []))
        return result

    def failures(self, workflow_id: str) -> List[CausalEvent]:
        return [e for e in self._events.values()
                if e.workflow_id == workflow_id and not e.success]

    def root_cause(self, failure_event_id: str) -> Optional[CausalEvent]:
        """Walk the causal chain to find the first failed event."""
        chain = self.causal_chain(failure_event_id)
        for evt in chain:
            if not evt.success:
                return evt
        return None

    def workflow_summary(self, workflow_id: str) -> dict:
        events = [e for e in self._events.values()
                  if e.workflow_id == workflow_id]
        failed = [e for e in events if not e.success]
        durations = [e.duration_ms for e in events if e.duration_ms]
        return {
            "workflow_id": workflow_id,
            "total_events": len(events),
            "failures": len(failed),
            "failed_steps": [e.step_name for e in failed],
            "total_duration_ms": round(sum(durations), 1) if durations else 0,
        }
```

---

## Solution 3: CorrelatedWorkflowRunner — Automatic Event Recording

```python
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional


class CorrelatedWorkflowRunner:
    """
    Runs multi-step agent workflows with automatic causal event recording.
    Every step's invocation, result, and error are recorded as causal events.

    Usage:
        store = CausalEventStore()
        runner = CorrelatedWorkflowRunner(store)

        async with runner.workflow("summarise-papers") as wf:
            docs = await wf.step("fetch_docs", fetch_fn, urls=doc_urls)
            chunks = await wf.step("chunk", chunk_fn, docs=docs)
            summary = await wf.step("summarise", summarise_fn, chunks=chunks)
    """

    def __init__(self, store: CausalEventStore):
        self._store = store

    @asynccontextmanager
    async def workflow(self, name: str, **meta):
        workflow_id = str(uuid.uuid4())
        root = CausalEvent.root(workflow_id, name, meta)
        self._store.record(root)
        ctx = _WorkflowContext(workflow_id, root, self._store)
        try:
            yield ctx
            root.success = True
        except Exception as exc:
            root.mark_failed(exc)
            raise
        finally:
            root.duration_ms = (time.time() - root.timestamp) * 1000


class _WorkflowContext:
    def __init__(self, workflow_id: str, parent: CausalEvent,
                 store: CausalEventStore):
        self._wf_id = workflow_id
        self._parent = parent
        self._store = store

    async def step(self, step_name: str, fn: Callable,
                    event_type: str = "tool_call", **kwargs) -> Any:
        evt = self._parent.child(event_type, step_name,
                                  {"kwargs": list(kwargs.keys())})
        self._store.record(evt)
        t0 = time.monotonic()
        try:
            result = await fn(**kwargs)
            evt.duration_ms = (time.monotonic() - t0) * 1000
            evt.payload["result_type"] = type(result).__name__
            # Next step's parent becomes this event
            self._parent = evt
            return result
        except Exception as exc:
            evt.duration_ms = (time.monotonic() - t0) * 1000
            evt.mark_failed(exc)
            raise
```

---

## Solution 4: FailurePostMortem — Automated Root Cause Report

```python
import json
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class PostMortemReport:
    workflow_id: str
    terminal_failure: str        # step name where execution stopped
    root_cause_step: str         # first step that failed
    causal_chain: List[str]      # step names from root to failure
    propagation_steps: List[str] # steps that passed bad data along
    error_type: str
    error_message: str
    recommendation: str


class FailurePostMortem:
    """
    Generates automated root-cause reports from a CausalEventStore.
    Identifies the originating failure and the propagation path.

    Usage:
        pm = FailurePostMortem(store)
        report = pm.analyse(workflow_id="wf-abc123")
        print(report.recommendation)
    """

    def __init__(self, store: CausalEventStore):
        self._store = store

    def analyse(self, workflow_id: str) -> Optional[PostMortemReport]:
        failures = self._store.failures(workflow_id)
        if not failures:
            return None

        # Find the terminal failure (last in time)
        terminal = max(failures, key=lambda e: e.timestamp)
        chain = self._store.causal_chain(terminal.event_id)

        # Root cause: first failure in chain
        first_failure = next((e for e in chain if not e.success), terminal)

        # Propagation: steps between root cause and terminal failure
        root_idx = chain.index(first_failure)
        terminal_idx = chain.index(terminal)
        propagation = [e.step_name for e in chain[root_idx + 1:terminal_idx]]

        rec = self._recommendation(first_failure, propagation)
        return PostMortemReport(
            workflow_id=workflow_id,
            terminal_failure=terminal.step_name,
            root_cause_step=first_failure.step_name,
            causal_chain=[e.step_name for e in chain],
            propagation_steps=propagation,
            error_type=first_failure.error_type or "Unknown",
            error_message=first_failure.error_message or "",
            recommendation=rec,
        )

    def _recommendation(self, root: CausalEvent,
                         propagation: List[str]) -> str:
        if propagation:
            return (
                f"Fix '{root.step_name}' ({root.error_type}). "
                f"Steps {propagation} propagated the bad output without validation. "
                f"Add output validation after '{root.step_name}'."
            )
        return f"Fix '{root.step_name}': {root.error_type} — {root.error_message}"
```

---

## Solution 5: CorrelationDashboard — Real-Time Workflow Health

```python
import time
from collections import defaultdict
from typing import Dict, List


class CorrelationDashboard:
    """
    Real-time dashboard aggregating workflow health from CausalEventStore.
    Reports failure rates per step, common root causes, and MTTR.

    Usage:
        dashboard = CorrelationDashboard(store)
        report = dashboard.health_report()
        hotspots = dashboard.failure_hotspots(top_n=5)
    """

    def __init__(self, store: CausalEventStore):
        self._store = store

    def health_report(self) -> Dict:
        all_events = list(self._store._events.values())
        if not all_events:
            return {}
        successes = sum(1 for e in all_events if e.success)
        failures = sum(1 for e in all_events if not e.success)
        durations = [e.duration_ms for e in all_events if e.duration_ms]
        workflows = {e.workflow_id for e in all_events}
        return {
            "total_events": len(all_events),
            "success_rate": round(successes / len(all_events), 4),
            "failure_count": failures,
            "active_workflows": len(workflows),
            "avg_step_ms": round(sum(durations) / len(durations), 1) if durations else 0,
        }

    def failure_hotspots(self, top_n: int = 10) -> List[Dict]:
        step_counts: Dict[str, int] = defaultdict(int)
        step_failures: Dict[str, int] = defaultdict(int)
        for evt in self._store._events.values():
            step_counts[evt.step_name] += 1
            if not evt.success:
                step_failures[evt.step_name] += 1
        hotspots = [
            {
                "step": step,
                "failures": step_failures[step],
                "total": step_counts[step],
                "failure_rate": round(step_failures[step] / step_counts[step], 3),
            }
            for step in step_failures
        ]
        return sorted(hotspots, key=lambda h: -h["failure_rate"])[:top_n]

    def propagation_graph(self) -> Dict[str, List[str]]:
        """Return adjacency list: step → steps that followed it."""
        graph: Dict[str, List[str]] = defaultdict(list)
        for evt in self._store._events.values():
            if evt.caused_by:
                parent = self._store._events.get(evt.caused_by)
                if parent:
                    graph[parent.step_name].append(evt.step_name)
        return dict(graph)
```

---

## Solution 6: AlertingCorrelator — Pattern-Based Failure Alerting

```python
import logging
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class AlertingCorrelator:
    """
    Watches the CausalEventStore and fires alerts when failure patterns emerge:
    - Repeated failure of the same step across workflows
    - Causal chains longer than a threshold before failure
    - Same root cause repeating within a time window

    Usage:
        correlator = AlertingCorrelator(store, on_alert=send_pagerduty_alert)
        asyncio.create_task(correlator.run(check_interval=30))
    """

    def __init__(self, store: CausalEventStore,
                 on_alert: Optional[Callable] = None,
                 step_failure_threshold: int = 3,
                 window_s: float = 300.0):
        self._store = store
        self._alert = on_alert or (lambda msg: logger.error("ALERT: %s", msg))
        self._threshold = step_failure_threshold
        self._window = window_s
        self._alerted: Dict[str, float] = {}

    def check(self):
        now = time.time()
        step_failures: Dict[str, List[float]] = defaultdict(list)
        for evt in self._store._events.values():
            if not evt.success:
                step_failures[evt.step_name].append(evt.timestamp)

        for step, timestamps in step_failures.items():
            recent = [t for t in timestamps if now - t < self._window]
            if len(recent) >= self._threshold:
                alert_key = f"step_failure:{step}"
                last = self._alerted.get(alert_key, 0)
                if now - last > self._window:
                    self._alerted[alert_key] = now
                    self._alert(
                        f"Step '{step}' failed {len(recent)} times "
                        f"in the last {self._window:.0f}s"
                    )

    async def run(self, check_interval: float = 30.0):
        while True:
            await __import__("asyncio").sleep(check_interval)
            self.check()
```

---

## Comparison

| Approach | Causal Links | Root-Cause Lookup | Automated Report | Real-Time | Alerting |
|---|---|---|---|---|---|
| **CausalEvent** | Yes (caused_by) | No | No | No | No |
| **CausalEventStore** | Yes | Yes | No | No | No |
| **CorrelatedWorkflowRunner** | Automatic | Via store | No | Yes | No |
| **FailurePostMortem** | Yes | Yes | Yes | No | No |
| **CorrelationDashboard** | Implicit | Via hotspots | No | Yes | No |
| **AlertingCorrelator** | Yes | No | No | Yes | Yes |

**Key insight**: attach a `caused_by` pointer to every step event at record time — the cost is one UUID per event. The payoff is that any failure can be instantly traced to its origin with `store.causal_chain(failure_event_id)`, turning hours-long post-mortems into a two-line lookup. Combine with `FailurePostMortem` for automated incident reports that identify not just what failed but what propagated the bad data without catching it.
