---
title: "Agent Doesn't Implement Inter-Agent Message Latency Tracking"
description: "Multi-agent systems that do not measure the time between an agent sending a message and the downstream agent beginning to process it cannot locate where latency accumulates in the pipeline — whether delays are in the message broker, the receiving agent's queue, or agent initialization. Implement inter-agent message latency tracking with per-hop timing, queue depth correlation, and end-to-end trace assembly."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-inter-agent-message-latency-tracking
tags: [inter-agent-latency, message-tracing, multi-agent, queue-depth, hop-timing, distributed-tracing]
symptoms:
  - "End-to-end latency is high but no single agent shows slow tool calls"
  - "Messages sit in the broker queue for seconds before being picked up"
  - "No visibility into how long each agent-to-agent handoff takes"
  - "Cannot determine whether latency is in the sender, broker, or receiver"
  - "Multi-agent pipeline has no distributed trace spanning all hops"
---

## Why This Happens

In a multi-agent system, latency has three components: the sending agent's processing time, the message transit time through the broker, and the receiving agent's queue wait time before processing begins. Standard request latency metrics measure only the third component from inside the receiver. Without timestamps embedded in the message envelope and recorded at each hop, the first two components are invisible. A message that takes 2 seconds to process may have spent 1.8 seconds waiting in the broker queue — a broker-scaling problem, not an agent-optimization problem. Inter-agent message latency tracking requires clock-stamped envelopes, hop recording, and trace assembly across agent boundaries.

## Solution 1: Message Envelope with Latency Headers

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class HopRecord:
    agent_id: str
    hop_type: str           # "sent" | "enqueued" | "dequeued" | "processing_start" | "processing_end"
    timestamp: float
    queue_depth: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageEnvelope:
    message_id: str
    trace_id: str
    sender_agent_id: str
    receiver_agent_id: str
    payload: Any
    created_at: float = field(default_factory=time.time)
    hops: List[HopRecord] = field(default_factory=list)
    priority: int = 5       # 1=highest, 10=lowest

    @classmethod
    def create(
        cls,
        sender_agent_id: str,
        receiver_agent_id: str,
        payload: Any,
        trace_id: Optional[str] = None,
    ) -> "MessageEnvelope":
        return cls(
            message_id=str(uuid.uuid4())[:8],
            trace_id=trace_id or str(uuid.uuid4())[:8],
            sender_agent_id=sender_agent_id,
            receiver_agent_id=receiver_agent_id,
            payload=payload,
        )

    def add_hop(
        self,
        agent_id: str,
        hop_type: str,
        queue_depth: Optional[int] = None,
        **metadata,
    ) -> None:
        self.hops.append(HopRecord(
            agent_id=agent_id,
            hop_type=hop_type,
            timestamp=time.time(),
            queue_depth=queue_depth,
            metadata=metadata,
        ))

    def transit_latency_ms(self) -> Optional[float]:
        """Time from 'sent' to 'dequeued'."""
        sent = next((h for h in self.hops if h.hop_type == "sent"), None)
        dequeued = next((h for h in self.hops if h.hop_type == "dequeued"), None)
        if sent and dequeued:
            return round((dequeued.timestamp - sent.timestamp) * 1000, 2)
        return None

    def queue_wait_ms(self) -> Optional[float]:
        """Time from 'enqueued' to 'processing_start'."""
        enqueued = next((h for h in self.hops if h.hop_type == "enqueued"), None)
        start = next((h for h in self.hops if h.hop_type == "processing_start"), None)
        if enqueued and start:
            return round((start.timestamp - enqueued.timestamp) * 1000, 2)
        return None

    def total_latency_ms(self) -> Optional[float]:
        if not self.hops:
            return None
        return round((self.hops[-1].timestamp - self.created_at) * 1000, 2)
```

## Solution 2: Inter-Agent Latency Recorder

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class InterAgentLatencyRecorder:
    """
    Accumulates per-hop latency measurements from message envelopes.
    Supports percentile queries broken down by sender→receiver pair.
    """

    def __init__(self, max_records: int = 20000):
        self._max = max_records
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, envelope: MessageEnvelope) -> None:
        entry = {
            "ts": time.time(),
            "trace_id": envelope.trace_id,
            "sender": envelope.sender_agent_id,
            "receiver": envelope.receiver_agent_id,
            "transit_ms": envelope.transit_latency_ms(),
            "queue_wait_ms": envelope.queue_wait_ms(),
            "total_ms": envelope.total_latency_ms(),
            "hop_count": len(envelope.hops),
        }
        with self._lock:
            self._records.append(entry)
            if len(self._records) > self._max:
                self._records.popleft()

    def percentile(
        self,
        metric: str,
        pct: float,
        window_seconds: float = 3600.0,
        sender: Optional[str] = None,
        receiver: Optional[str] = None,
    ) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            vals = [
                r[metric]
                for r in self._records
                if r["ts"] >= cutoff
                and r[metric] is not None
                and (sender is None or r["sender"] == sender)
                and (receiver is None or r["receiver"] == receiver)
            ]
        if not vals:
            return None
        vals.sort()
        idx = min(int(len(vals) * pct / 100.0), len(vals) - 1)
        return round(vals[idx], 2)

    def pair_summary(self, window_seconds: float = 3600.0) -> Dict[str, dict]:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        pairs: Dict[str, List[dict]] = {}
        for r in recent:
            key = f"{r['sender']}->{r['receiver']}"
            pairs.setdefault(key, []).append(r)
        result = {}
        for pair, records in pairs.items():
            transits = [r["transit_ms"] for r in records if r["transit_ms"] is not None]
            queues = [r["queue_wait_ms"] for r in records if r["queue_wait_ms"] is not None]
            result[pair] = {
                "message_count": len(records),
                "p50_transit_ms": sorted(transits)[len(transits) // 2] if transits else None,
                "p95_transit_ms": sorted(transits)[int(len(transits) * 0.95)] if transits else None,
                "p50_queue_wait_ms": sorted(queues)[len(queues) // 2] if queues else None,
            }
        return result
```

## Solution 3: Trace Assembler

```python
from typing import Dict, List, Optional


class MultiHopTraceAssembler:
    """
    Reconstructs end-to-end traces across multiple agent hops by
    grouping envelopes by trace_id. Identifies the slowest hop in
    a multi-agent pipeline.
    """

    def __init__(self):
        self._envelopes: Dict[str, List[MessageEnvelope]] = {}

    def add_envelope(self, envelope: MessageEnvelope) -> None:
        self._envelopes.setdefault(envelope.trace_id, []).append(envelope)

    def assemble_trace(self, trace_id: str) -> Optional[dict]:
        envelopes = self._envelopes.get(trace_id)
        if not envelopes:
            return None

        hops = []
        total_transit = 0.0
        total_queue = 0.0
        slowest_hop = None
        slowest_ms = 0.0

        for env in sorted(envelopes, key=lambda e: e.created_at):
            transit = env.transit_latency_ms() or 0.0
            queue = env.queue_wait_ms() or 0.0
            total_transit += transit
            total_queue += queue
            hop = {
                "sender": env.sender_agent_id,
                "receiver": env.receiver_agent_id,
                "transit_ms": transit,
                "queue_wait_ms": queue,
            }
            hops.append(hop)
            if transit + queue > slowest_ms:
                slowest_ms = transit + queue
                slowest_hop = hop

        return {
            "trace_id": trace_id,
            "hop_count": len(envelopes),
            "hops": hops,
            "total_transit_ms": round(total_transit, 2),
            "total_queue_wait_ms": round(total_queue, 2),
            "slowest_hop": slowest_hop,
        }
```

## Solution 4: Queue Depth Correlator

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class QueueDepthCorrelator:
    """
    Records queue depth alongside message latency to determine
    whether latency spikes correlate with queue depth growth.
    """

    def __init__(self, max_records: int = 5000):
        self._records: Deque[Tuple[float, int, float]] = deque(maxlen=max_records)
        # (ts, queue_depth, queue_wait_ms)
        self._lock = Lock()

    def record(self, queue_depth: int, queue_wait_ms: float) -> None:
        with self._lock:
            self._records.append((time.time(), queue_depth, queue_wait_ms))

    def correlation_summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(d, w) for ts, d, w in self._records if ts >= cutoff]
        if len(recent) < 2:
            return {"window_seconds": window_seconds, "samples": len(recent)}
        depths = [d for d, _ in recent]
        waits = [w for _, w in recent]
        mean_d = sum(depths) / len(depths)
        mean_w = sum(waits) / len(waits)
        cov = sum((d - mean_d) * (w - mean_w) for d, w in zip(depths, waits)) / len(recent)
        std_d = (sum((d - mean_d) ** 2 for d in depths) / len(depths)) ** 0.5
        std_w = (sum((w - mean_w) ** 2 for w in waits) / len(waits)) ** 0.5
        correlation = cov / (std_d * std_w) if std_d > 0 and std_w > 0 else 0.0
        return {
            "window_seconds": window_seconds,
            "samples": len(recent),
            "mean_queue_depth": round(mean_d, 2),
            "mean_queue_wait_ms": round(mean_w, 2),
            "depth_wait_correlation": round(correlation, 4),
            "interpretation": "strong" if abs(correlation) > 0.7 else "weak",
        }
```

## Solution 5: Latency Spike Detector

```python
import time
from typing import Optional


class InterAgentLatencySpikeDetector:
    """
    Compares recent inter-agent transit latency P95 against a baseline
    window to detect broker or network degradation between agent pairs.
    """

    def __init__(
        self,
        recorder: InterAgentLatencyRecorder,
        spike_threshold_pct: float = 50.0,
    ):
        self._recorder = recorder
        self._threshold = spike_threshold_pct / 100.0

    def detect(
        self,
        sender: Optional[str] = None,
        receiver: Optional[str] = None,
        baseline_seconds: float = 86400.0,
        recent_seconds: float = 1800.0,
    ) -> dict:
        baseline = self._recorder.percentile(
            "transit_ms", 95, baseline_seconds, sender, receiver
        )
        recent = self._recorder.percentile(
            "transit_ms", 95, recent_seconds, sender, receiver
        )
        if baseline is None or recent is None:
            return {"status": "insufficient_data", "sender": sender, "receiver": receiver}
        change = (recent - baseline) / max(baseline, 1)
        return {
            "status": "spike" if change > self._threshold else "normal",
            "sender": sender,
            "receiver": receiver,
            "baseline_p95_ms": baseline,
            "recent_p95_ms": recent,
            "change_pct": round(change * 100, 1),
        }
```

## Solution 6: Inter-Agent Latency Dashboard

```python
import time


class InterAgentLatencyDashboard:
    """
    Combines pair-level summaries, queue correlation, spike detection,
    and trace assembly into a multi-agent pipeline health report.
    """

    def __init__(
        self,
        recorder: InterAgentLatencyRecorder,
        assembler: MultiHopTraceAssembler,
        correlator: QueueDepthCorrelator,
        spike_detector: InterAgentLatencySpikeDetector,
    ):
        self._recorder = recorder
        self._assembler = assembler
        self._correlator = correlator
        self._spike = spike_detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "pair_summaries_1h": self._recorder.pair_summary(window_seconds=3600.0),
            "overall_p95_transit_ms": self._recorder.percentile("transit_ms", 95, 3600.0),
            "overall_p95_queue_wait_ms": self._recorder.percentile("queue_wait_ms", 95, 3600.0),
            "queue_depth_correlation": self._correlator.correlation_summary(3600.0),
            "spike_detection": self._spike.detect(),
        }
```

## Comparison

| Approach | Hop Timestamps | Per-Pair Latency | Trace Assembly | Queue Correlation | Spike Detection |
|---|---|---|---|---|---|
| MessageEnvelope / HopRecord | Yes (per hop) | No | No | No | No |
| InterAgentLatencyRecorder | Via envelope | Yes | No | No | No |
| MultiHopTraceAssembler | Via envelope | No | Yes | No | No |
| QueueDepthCorrelator | No | No | No | Yes | No |
| InterAgentLatencySpikeDetector | No | Via recorder | No | No | Yes |
| InterAgentLatencyDashboard | No | No | No | No | Yes |

**Best for production**: Embed `created_at` and `trace_id` in every message envelope at send time using the sender's clock — do not rely on broker timestamps, which introduce broker-side latency into the measurement. Record `queue_depth` at dequeue time alongside queue wait: a strong depth-wait correlation (>0.7) means the bottleneck is broker throughput, not agent processing. Emit `transit_ms` and `queue_wait_ms` as separate metrics tagged with `sender_agent` and `receiver_agent` — this lets you build per-edge latency heatmaps across the agent graph and pinpoint which edge degrades first under load.
