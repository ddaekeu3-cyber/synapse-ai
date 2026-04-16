---
title: "Agent Doesn't Implement Inter-Agent Message Latency Tracking"
description: "Multi-agent systems where orchestrators delegate to sub-agents have no visibility into how long each delegation hop takes: the orchestrator sees only total round-trip time with no breakdown of queue wait, sub-agent processing, and return trip. Implement inter-agent message latency tracking that timestamps every hop, computes per-hop latency, detects slow sub-agents, and surfaces the critical path through a multi-agent call graph."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-inter-agent-message-latency-tracking
tags: [inter-agent-latency, multi-agent, message-tracing, hop-tracking, critical-path, distributed-tracing]
symptoms:
  - "Multi-agent pipeline takes 30 seconds but there is no breakdown of where time is spent"
  - "Slow sub-agent is invisible — only total orchestrator latency is measured"
  - "Queue wait time between agents is conflated with processing time"
  - "No way to identify which hop in a five-agent chain is causing the P99 regression"
  - "Retry storms in one sub-agent inflate parent orchestrator latency with no attribution"
---

## Why This Happens

In single-agent systems, request latency maps directly to LLM call time plus tool execution time. In multi-agent systems, a request fans out across a call graph: orchestrator → planner → executor → verifier. Without tracing headers propagated across agent boundaries, each agent measures only its own processing time. The full request latency — including queue wait between agents, serialization overhead, and retry loops in sub-agents — is invisible at the orchestration layer. Tracking requires a trace context that is created at the root, propagated in every inter-agent message, and used to record hop timestamps at both the sender and receiver.

## Solution 1: Inter-Agent Trace Context

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AgentHopRecord:
    hop_id: str
    sender_agent_id: str
    receiver_agent_id: str
    sent_at: float
    received_at: Optional[float] = None
    processing_started_at: Optional[float] = None
    processing_finished_at: Optional[float] = None
    reply_sent_at: Optional[float] = None
    reply_received_at: Optional[float] = None

    def queue_wait_ms(self) -> Optional[float]:
        if self.received_at and self.processing_started_at:
            return round((self.processing_started_at - self.received_at) * 1000, 2)
        return None

    def processing_ms(self) -> Optional[float]:
        if self.processing_started_at and self.processing_finished_at:
            return round((self.processing_finished_at - self.processing_started_at) * 1000, 2)
        return None

    def round_trip_ms(self) -> Optional[float]:
        if self.sent_at and self.reply_received_at:
            return round((self.reply_received_at - self.sent_at) * 1000, 2)
        return None


@dataclass
class InterAgentTraceContext:
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    root_agent_id: str = ""
    created_at: float = field(default_factory=time.time)
    hops: List[AgentHopRecord] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    def new_hop(self, sender: str, receiver: str) -> AgentHopRecord:
        hop = AgentHopRecord(
            hop_id=uuid.uuid4().hex[:8],
            sender_agent_id=sender,
            receiver_agent_id=receiver,
            sent_at=time.time(),
        )
        self.hops.append(hop)
        return hop

    def get_hop(self, hop_id: str) -> Optional[AgentHopRecord]:
        return next((h for h in self.hops if h.hop_id == hop_id), None)
```

## Solution 2: Hop Latency Recorder

```python
import time
from typing import Optional


class HopLatencyRecorder:
    """
    Records timestamps at each stage of an inter-agent message hop:
    send, receive, processing start, processing finish, reply send, reply receive.
    """

    def on_send(self, hop: AgentHopRecord) -> None:
        hop.sent_at = time.time()

    def on_receive(self, hop: AgentHopRecord) -> None:
        hop.received_at = time.time()

    def on_processing_start(self, hop: AgentHopRecord) -> None:
        hop.processing_started_at = time.time()

    def on_processing_finish(self, hop: AgentHopRecord) -> None:
        hop.processing_finished_at = time.time()

    def on_reply_send(self, hop: AgentHopRecord) -> None:
        hop.reply_sent_at = time.time()

    def on_reply_receive(self, hop: AgentHopRecord) -> None:
        hop.reply_received_at = time.time()

    def hop_summary(self, hop: AgentHopRecord) -> dict:
        return {
            "hop_id": hop.hop_id,
            "sender": hop.sender_agent_id,
            "receiver": hop.receiver_agent_id,
            "queue_wait_ms": hop.queue_wait_ms(),
            "processing_ms": hop.processing_ms(),
            "round_trip_ms": hop.round_trip_ms(),
        }
```

## Solution 3: Critical Path Analyzer

```python
from typing import Dict, List, Optional, Tuple


class CriticalPathAnalyzer:
    """
    Identifies the critical path through a multi-agent call graph
    — the sequence of hops whose combined latency determines the
    total request duration. Surfaces the slowest hop and agent.
    """

    def analyze(self, trace: InterAgentTraceContext) -> dict:
        if not trace.hops:
            return {"critical_path": [], "total_ms": 0.0, "slowest_hop": None}

        hop_durations = []
        for hop in trace.hops:
            rtt = hop.round_trip_ms()
            if rtt is not None:
                hop_durations.append((hop, rtt))

        if not hop_durations:
            return {"critical_path": [], "total_ms": 0.0, "slowest_hop": None}

        total_ms = sum(d for _, d in hop_durations)
        slowest_hop, slowest_ms = max(hop_durations, key=lambda x: x[1])

        by_agent: Dict[str, float] = {}
        for hop, ms in hop_durations:
            by_agent[hop.receiver_agent_id] = by_agent.get(hop.receiver_agent_id, 0.0) + ms

        slowest_agent = max(by_agent, key=by_agent.get) if by_agent else None

        return {
            "trace_id": trace.trace_id,
            "total_measured_ms": round(total_ms, 2),
            "hop_count": len(hop_durations),
            "slowest_hop_id": slowest_hop.hop_id,
            "slowest_hop_ms": round(slowest_ms, 2),
            "slowest_agent": slowest_agent,
            "slowest_agent_total_ms": round(by_agent.get(slowest_agent, 0), 2) if slowest_agent else None,
            "by_agent_ms": {k: round(v, 2) for k, v in by_agent.items()},
        }
```

## Solution 4: Inter-Agent Latency Registry

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class InterAgentLatencyRegistry:
    """
    Accumulates completed trace analyses and computes per-agent
    and per-hop-pair latency percentiles across multiple requests.
    """

    def __init__(self, max_traces: int = 5000):
        self._max = max_traces
        self._traces: Deque[dict] = deque(maxlen=max_traces)
        self._lock = Lock()

    def record(self, analysis: dict) -> None:
        with self._lock:
            self._traces.append({**analysis, "recorded_at": time.time()})

    def agent_latency_stats(
        self,
        agent_id: str,
        window_seconds: float = 3600.0,
    ) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            samples = [
                t["by_agent_ms"].get(agent_id, 0)
                for t in self._traces
                if t.get("recorded_at", 0) >= cutoff
                and agent_id in t.get("by_agent_ms", {})
            ]
        if not samples:
            return {"agent_id": agent_id, "samples": 0}
        samples_sorted = sorted(samples)
        n = len(samples_sorted)
        return {
            "agent_id": agent_id,
            "samples": n,
            "p50_ms": samples_sorted[n // 2],
            "p95_ms": samples_sorted[int(n * 0.95)],
            "p99_ms": samples_sorted[int(n * 0.99)],
            "mean_ms": round(sum(samples_sorted) / n, 2),
        }

    def slowest_agents(self, top_n: int = 5, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        with self._lock:
            agent_totals: Dict[str, List[float]] = {}
            for t in self._traces:
                if t.get("recorded_at", 0) < cutoff:
                    continue
                for agent, ms in t.get("by_agent_ms", {}).items():
                    agent_totals.setdefault(agent, []).append(ms)

        ranked = sorted(
            agent_totals.items(),
            key=lambda x: sum(x[1]) / len(x[1]),
            reverse=True,
        )
        return [
            {"agent_id": k, "mean_ms": round(sum(v) / len(v), 2), "sample_count": len(v)}
            for k, v in ranked[:top_n]
        ]
```

## Solution 5: Trace-Propagating Agent Wrapper

```python
import asyncio
import time
from typing import Any, Callable, Optional


class TracePropagatingAgentWrapper:
    """
    Wraps an agent's send/receive methods to automatically record
    hop timestamps and propagate the trace context in message envelopes.
    """

    def __init__(
        self,
        agent_id: str,
        recorder: HopLatencyRecorder,
    ):
        self._agent_id = agent_id
        self._recorder = recorder

    async def send(
        self,
        trace: InterAgentTraceContext,
        receiver_id: str,
        message: Any,
        send_fn: Callable,
    ) -> AgentHopRecord:
        hop = trace.new_hop(self._agent_id, receiver_id)
        self._recorder.on_send(hop)
        envelope = {
            "trace_id": trace.trace_id,
            "hop_id": hop.hop_id,
            "sent_at": hop.sent_at,
            "payload": message,
        }
        await send_fn(receiver_id, envelope)
        return hop

    async def receive_and_process(
        self,
        trace: InterAgentTraceContext,
        envelope: dict,
        process_fn: Callable,
    ) -> Any:
        hop_id = envelope.get("hop_id", "")
        hop = trace.get_hop(hop_id)
        if hop:
            self._recorder.on_receive(hop)
            self._recorder.on_processing_start(hop)
        try:
            result = await process_fn(envelope["payload"])
        finally:
            if hop:
                self._recorder.on_processing_finish(hop)
        return result
```

## Solution 6: Inter-Agent Latency Dashboard

```python
import time


class InterAgentLatencyDashboard:
    """
    Combines critical path analysis, per-agent latency stats, and
    slowest-agent ranking into a single operational report.
    """

    def __init__(
        self,
        registry: InterAgentLatencyRegistry,
        analyzer: CriticalPathAnalyzer,
    ):
        self._registry = registry
        self._analyzer = analyzer

    def render(self, window_seconds: float = 3600.0) -> dict:
        slowest = self._registry.slowest_agents(top_n=5, window_seconds=window_seconds)
        agent_stats = {}
        for entry in slowest:
            aid = entry["agent_id"]
            agent_stats[aid] = self._registry.agent_latency_stats(aid, window_seconds)

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "slowest_agents": slowest,
            "agent_latency_percentiles": agent_stats,
        }
```

## Comparison

| Approach | Hop Timestamps | Critical Path | Per-Agent Percentiles | Trace Propagation | Dashboard |
|---|---|---|---|---|---|
| InterAgentTraceContext | Yes (dataclass) | No | No | No | No |
| HopLatencyRecorder | Yes (per stage) | No | No | No | No |
| CriticalPathAnalyzer | No | Yes | No | No | No |
| InterAgentLatencyRegistry | No | No | Yes (P50/P95/P99) | No | No |
| TracePropagatingAgentWrapper | Via recorder | No | No | Yes (envelope) | No |
| InterAgentLatencyDashboard | No | No | Via registry | No | Yes |

**Best for production**: Propagate `trace_id` and `hop_id` in every inter-agent message envelope — this is the minimum required for end-to-end latency attribution. Record both `processing_started_at` and `received_at` on the receiver side to separate queue wait from actual processing; queue wait growing while processing time stays constant indicates a capacity bottleneck in the message broker, not the agent. Alert when any agent's P95 round-trip time exceeds 2× the baseline established during load testing — this catches regressions introduced by model updates or dependency changes before users notice degraded response quality.
