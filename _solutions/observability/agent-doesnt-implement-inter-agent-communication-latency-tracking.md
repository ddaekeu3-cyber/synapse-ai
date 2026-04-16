---
title: "Agent Doesn't Implement Inter-Agent Communication Latency Tracking"
description: "Multi-agent systems where a coordinator dispatches to sub-agents have no visibility into how long each sub-agent takes to respond if inter-agent communication latency is not tracked separately from tool call latency. A slow sub-agent looks identical to a slow tool. Implement inter-agent communication latency tracking that records dispatch time, response time, sub-agent identity, and task complexity for each agent-to-agent call, enabling bottleneck identification in multi-agent pipelines."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-inter-agent-communication-latency-tracking
tags: [inter-agent, multi-agent, communication-latency, sub-agent-tracing, agent-pipeline, coordinator-observability]
symptoms:
  - "Multi-agent pipeline P99 is 15 seconds but no breakdown by sub-agent is available"
  - "Cannot tell whether the coordinator or a sub-agent is responsible for slow responses"
  - "Sub-agent dispatch and response times are not measured separately from tool calls"
  - "Sub-agent identity is not recorded in traces — only the task type is visible"
  - "No SLO tracking per sub-agent to detect regressions in specific agent types"
---

## Why This Happens

Multi-agent frameworks typically model sub-agent calls as tool calls, which means they are instrumented identically to API calls or database lookups. This is adequate for whether a call succeeded, but insufficient for latency attribution within the pipeline: a sub-agent call involves model inference, tool execution, and potentially recursive sub-agent calls of its own. Tracking inter-agent latency as a distinct dimension requires recording the sub-agent identity, the request payload size, the response payload size, and the full round-trip duration separately from the tool call span.

## Solution 1: Agent Call Span

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentCallSpan:
    span_id: str
    parent_span_id: Optional[str]
    coordinator_id: str
    sub_agent_id: str
    task_type: str
    request_chars: int
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    response_chars: int = 0
    succeeded: bool = True
    error_type: Optional[str] = None

    @property
    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)

    def finish(
        self,
        response_chars: int,
        succeeded: bool = True,
        error_type: Optional[str] = None,
    ) -> None:
        self.ended_at = time.time()
        self.response_chars = response_chars
        self.succeeded = succeeded
        self.error_type = error_type
```

## Solution 2: Agent Latency Tracker

```python
import uuid
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class AgentLatencyTracker:
    """
    Records inter-agent call spans and provides per-sub-agent latency statistics.
    """

    def __init__(self, window_seconds: float = 600.0, max_spans: int = 10_000):
        self._window = window_seconds
        self._spans: Deque[AgentCallSpan] = deque(maxlen=max_spans)
        self._lock = Lock()

    def start_span(
        self,
        coordinator_id: str,
        sub_agent_id: str,
        task_type: str,
        request_chars: int,
        parent_span_id: Optional[str] = None,
    ) -> AgentCallSpan:
        span = AgentCallSpan(
            span_id=str(uuid.uuid4())[:12],
            parent_span_id=parent_span_id,
            coordinator_id=coordinator_id,
            sub_agent_id=sub_agent_id,
            task_type=task_type,
            request_chars=request_chars,
        )
        return span

    def record(self, span: AgentCallSpan) -> None:
        with self._lock:
            self._spans.append(span)

    def _recent_complete(self) -> List[AgentCallSpan]:
        cutoff = time.time() - self._window
        return [
            s for s in self._spans
            if s.started_at >= cutoff and s.duration_ms is not None
        ]

    def per_agent_stats(self) -> Dict[str, dict]:
        spans = self._recent_complete()
        by_agent: Dict[str, List[float]] = {}
        by_agent_errors: Dict[str, int] = {}

        for span in spans:
            aid = span.sub_agent_id
            if aid not in by_agent:
                by_agent[aid] = []
                by_agent_errors[aid] = 0
            if span.succeeded and span.duration_ms is not None:
                by_agent[aid].append(span.duration_ms)
            else:
                by_agent_errors[aid] += 1

        stats = {}
        for agent_id, durations in by_agent.items():
            if not durations:
                stats[agent_id] = {"call_count": by_agent_errors[agent_id], "error_count": by_agent_errors[agent_id]}
                continue
            sorted_d = sorted(durations)
            stats[agent_id] = {
                "call_count": len(durations) + by_agent_errors[agent_id],
                "error_count": by_agent_errors[agent_id],
                "p50_ms": sorted_d[len(sorted_d) // 2],
                "p95_ms": sorted_d[min(int(len(sorted_d) * 0.95), len(sorted_d) - 1)],
                "mean_ms": round(sum(sorted_d) / len(sorted_d), 2),
                "max_ms": sorted_d[-1],
            }
        return stats
```

## Solution 3: Inter-Agent Instrumented Dispatcher

```python
import time
from typing import Any, Callable, Optional


class InstrumentedAgentDispatcher:
    """
    Wraps agent-to-agent dispatch calls with latency span recording.
    """

    def __init__(
        self,
        tracker: AgentLatencyTracker,
        coordinator_id: str,
    ):
        self._tracker = tracker
        self._coordinator_id = coordinator_id

    async def dispatch(
        self,
        sub_agent_id: str,
        task_type: str,
        request: Any,
        dispatch_fn: Callable,
        parent_span_id: Optional[str] = None,
    ) -> Any:
        request_str = str(request)
        span = self._tracker.start_span(
            coordinator_id=self._coordinator_id,
            sub_agent_id=sub_agent_id,
            task_type=task_type,
            request_chars=len(request_str),
            parent_span_id=parent_span_id,
        )
        try:
            result = await dispatch_fn(request)
            response_str = str(result)
            span.finish(response_chars=len(response_str), succeeded=True)
            return result
        except Exception as exc:
            span.finish(response_chars=0, succeeded=False, error_type=type(exc).__name__)
            raise
        finally:
            self._tracker.record(span)
```

## Solution 4: Pipeline Latency Profiler

```python
from typing import Dict, List, Optional


class AgentPipelineLatencyProfiler:
    """
    Reconstructs pipeline call trees from spans and identifies
    the critical path (slowest sequence of dependent calls).
    """

    def __init__(self, tracker: AgentLatencyTracker):
        self._tracker = tracker

    def pipeline_summary(self, coordinator_id: str) -> dict:
        spans = self._tracker._recent_complete()
        coord_spans = [s for s in spans if s.coordinator_id == coordinator_id]

        if not coord_spans:
            return {"coordinator_id": coordinator_id, "calls": 0}

        total_ms = sum(s.duration_ms for s in coord_spans if s.duration_ms)
        by_type: Dict[str, List[float]] = {}
        for span in coord_spans:
            if span.duration_ms:
                by_type.setdefault(span.task_type, []).append(span.duration_ms)

        return {
            "coordinator_id": coordinator_id,
            "calls": len(coord_spans),
            "total_agent_time_ms": round(total_ms, 2),
            "by_task_type": {
                task: {
                    "count": len(durations),
                    "mean_ms": round(sum(durations) / len(durations), 2),
                    "max_ms": round(max(durations), 2),
                }
                for task, durations in by_type.items()
            },
        }
```

## Solution 5: Sub-Agent SLO Monitor

```python
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class SubAgentSLO:
    agent_id: str
    p95_threshold_ms: float
    error_rate_threshold: float = 0.05


class SubAgentSLOMonitor:
    """
    Evaluates per-sub-agent latency and error rate against defined SLOs.
    Returns violations for alerting.
    """

    def __init__(self, slos: List[SubAgentSLO], tracker: AgentLatencyTracker):
        self._slos = {slo.agent_id: slo for slo in slos}
        self._tracker = tracker

    def evaluate(self) -> List[dict]:
        stats = self._tracker.per_agent_stats()
        violations = []
        for agent_id, slo in self._slos.items():
            agent_stats = stats.get(agent_id)
            if not agent_stats:
                continue
            p95 = agent_stats.get("p95_ms", 0)
            total = agent_stats.get("call_count", 1)
            errors = agent_stats.get("error_count", 0)
            error_rate = errors / max(total, 1)

            if p95 > slo.p95_threshold_ms:
                violations.append({
                    "agent_id": agent_id,
                    "violation": "p95_latency",
                    "actual_p95_ms": p95,
                    "threshold_ms": slo.p95_threshold_ms,
                })
            if error_rate > slo.error_rate_threshold:
                violations.append({
                    "agent_id": agent_id,
                    "violation": "error_rate",
                    "actual_rate": round(error_rate, 4),
                    "threshold": slo.error_rate_threshold,
                })
        return violations
```

## Solution 6: Inter-Agent Latency Dashboard

```python
import time
from typing import List, Optional


class InterAgentLatencyDashboard:
    """
    Renders per-agent latency stats, pipeline summaries, and SLO violations.
    """

    def __init__(
        self,
        tracker: AgentLatencyTracker,
        profiler: AgentPipelineLatencyProfiler,
        slo_monitor: SubAgentSLOMonitor,
        coordinator_ids: Optional[List[str]] = None,
    ):
        self._tracker = tracker
        self._profiler = profiler
        self._slo_monitor = slo_monitor
        self._coordinator_ids = coordinator_ids or []

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "per_agent_stats": self._tracker.per_agent_stats(),
            "pipeline_summaries": {
                cid: self._profiler.pipeline_summary(cid)
                for cid in self._coordinator_ids
            },
            "slo_violations": self._slo_monitor.evaluate(),
        }
```

## Comparison

| Approach | Call Span Recording | Per-Agent Stats | Pipeline Profiling | SLO Monitoring | Dashboard |
|---|---|---|---|---|---|
| AgentLatencyTracker | Yes | Yes (P50/P95) | No | No | No |
| InstrumentedAgentDispatcher | Via tracker | No | No | No | No |
| AgentPipelineLatencyProfiler | Via tracker | No | Yes | No | No |
| SubAgentSLOMonitor | Via tracker | Via tracker | No | Yes | No |
| InterAgentLatencyDashboard | No | No | No | No | Yes |

**Best for production**: Tag every inter-agent call with a `parent_span_id` propagated from the coordinator's own trace context — this enables full call tree reconstruction for debugging slow multi-agent pipelines. Set per-sub-agent SLOs based on task type: a research sub-agent has a different latency profile than a formatting agent. Alert when `p95_ms` for any sub-agent exceeds its SLO for 3 consecutive evaluation windows — this separates transient spikes from regressions. Monitor `total_agent_time_ms` in `pipeline_summary`: if it is consistently higher than the end-to-end request latency, sub-agents are running sequentially when they could be parallelized.
