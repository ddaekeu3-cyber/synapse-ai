---
title: "Agent Doesn't Implement Real User Monitoring for Agent Response Latency"
description: "AI agents that log only server-side processing times miss the latency experienced by real users: network round-trips, streaming time-to-first-token, client rendering delays, and retry penalties. Real User Monitoring (RUM) instruments the full client-perceived latency, captures p50/p95/p99 percentiles per user segment, and surfaces the delta between server metrics and what users actually wait for."
date: 2025-02-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-real-user-monitoring-for-agent-response-latency
tags:
  - rum
  - real-user-monitoring
  - latency
  - observability
  - performance
  - percentiles
  - time-to-first-token
symptoms:
  - "Server logs show p95 latency of 800ms but users report the assistant feels slow"
  - "No measurement of time-to-first-token from the client's perspective"
  - "Latency percentiles are computed server-side and do not account for network RTT"
  - "No breakdown of latency by user geography, model, or tool call count"
  - "Streaming responses show 200ms server time but 3s first-token on mobile"
---

## Problem

Server-side latency metrics measure processing time but not user-perceived latency. A streaming agent may send the first token after 200ms server-side, but a mobile user on a high-latency connection receives it after 2 seconds. RUM closes this gap: the client records wall-clock timestamps at key events (request sent, first token received, last token received, render complete), transmits them as beacon events, and the server aggregates them into percentile distributions segmented by user cohort, geography, model, and conversation length.

---

## Solution 1: ClientLatencyBeacon — Record and Transmit Client-Side Timestamps

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class LatencyBeacon:
    session_id: str
    request_id: str
    user_segment: str          # e.g. "free", "pro", "enterprise"
    model: str
    tool_call_count: int

    # Client-observed timestamps (Unix seconds, float precision)
    client_send_ts: float      # When client sent the request
    server_ack_ts: Optional[float] = None     # When client received HTTP 200/stream start
    first_token_ts: Optional[float] = None    # When client rendered first token
    last_token_ts: Optional[float] = None     # When stream completed
    render_complete_ts: Optional[float] = None  # When UI finished rendering

    # Derived (computed on flush)
    network_rtt_ms: Optional[float] = None
    ttft_ms: Optional[float] = None           # Time-to-first-token (client)
    total_latency_ms: Optional[float] = None
    streaming_duration_ms: Optional[float] = None

    extra: Dict[str, Any] = field(default_factory=dict)

    def compute_derived(self):
        if self.server_ack_ts and self.client_send_ts:
            self.network_rtt_ms = (self.server_ack_ts - self.client_send_ts) * 1000
        if self.first_token_ts and self.client_send_ts:
            self.ttft_ms = (self.first_token_ts - self.client_send_ts) * 1000
        if self.last_token_ts and self.client_send_ts:
            self.total_latency_ms = (self.last_token_ts - self.client_send_ts) * 1000
        if self.last_token_ts and self.first_token_ts:
            self.streaming_duration_ms = (
                (self.last_token_ts - self.first_token_ts) * 1000
            )

    def to_dict(self) -> Dict[str, Any]:
        self.compute_derived()
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "user_segment": self.user_segment,
            "model": self.model,
            "tool_call_count": self.tool_call_count,
            "network_rtt_ms": self.network_rtt_ms,
            "ttft_ms": self.ttft_ms,
            "total_latency_ms": self.total_latency_ms,
            "streaming_duration_ms": self.streaming_duration_ms,
            "client_send_ts": self.client_send_ts,
            **self.extra,
        }
```

---

## Solution 2: RUMBeaconCollector — Receive and Store Client Beacons

```python
import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RUMBeaconCollector:
    """
    Server-side endpoint that receives LatencyBeacon payloads from clients,
    validates them, and stores them in a rolling in-memory buffer for
    real-time percentile computation. Optionally forwards to a time-series
    store (InfluxDB, Prometheus remote write, BigQuery).

    Usage:
        collector = RUMBeaconCollector(max_buffer=10_000)

        # In HTTP handler:
        @app.post("/rum/beacon")
        async def beacon(payload: dict):
            collector.ingest(payload)
            return 204

        # Periodic reporting:
        report = collector.percentiles(segment="pro", window_s=300)
    """

    MAX_LATENCY_MS = 120_000   # Discard beacons > 2 minutes (clock skew)
    MIN_LATENCY_MS = 0

    def __init__(self, max_buffer: int = 10_000):
        self._max = max_buffer
        # segment -> deque of beacon dicts
        self._buffers: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max_buffer)
        )
        self._total_received = 0
        self._total_rejected = 0

    def ingest(self, payload: Dict[str, Any]):
        """Validate and store a beacon payload."""
        ttft = payload.get("ttft_ms")
        total = payload.get("total_latency_ms")

        if ttft is not None and (
            ttft < self.MIN_LATENCY_MS or ttft > self.MAX_LATENCY_MS
        ):
            self._total_rejected += 1
            logger.debug("rum_beacon_rejected ttft_ms=%s (out of range)", ttft)
            return

        payload["_ingested_at"] = time.time()
        segment = payload.get("user_segment", "unknown")
        self._buffers[segment].append(payload)
        self._buffers["_all"].append(payload)
        self._total_received += 1

    def percentiles(
        self,
        segment: str = "_all",
        window_s: float = 300.0,
        metric: str = "ttft_ms",
    ) -> Dict[str, Any]:
        """Compute p50/p95/p99 for a metric within the time window."""
        now = time.time()
        cutoff = now - window_s
        values = sorted(
            p[metric]
            for p in self._buffers.get(segment, [])
            if p.get("_ingested_at", 0) >= cutoff and p.get(metric) is not None
        )
        if not values:
            return {"segment": segment, "metric": metric, "n": 0}

        def pct(p: float) -> float:
            idx = int(len(values) * p / 100)
            return round(values[min(idx, len(values) - 1)], 1)

        return {
            "segment": segment,
            "metric": metric,
            "n": len(values),
            "p50": pct(50),
            "p75": pct(75),
            "p95": pct(95),
            "p99": pct(99),
            "min": round(values[0], 1),
            "max": round(values[-1], 1),
            "mean": round(sum(values) / len(values), 1),
            "window_s": window_s,
        }

    def stats(self) -> Dict[str, Any]:
        return {
            "total_received": self._total_received,
            "total_rejected": self._total_rejected,
            "segments": {k: len(v) for k, v in self._buffers.items()},
        }
```

---

## Solution 3: LatencySegmentAnalyzer — Break Down Latency by Cohort

```python
import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class LatencySegmentAnalyzer:
    """
    Groups RUM beacons by dimensions (model, tool_call_count bucket,
    user_segment, hour-of-day) and surfaces the worst-performing cohorts.
    Used to find: "mobile pro users on claude-opus-4-6 with 3+ tool calls
    see p99 TTFT of 8.2s while desktop free users see 1.1s."

    Usage:
        analyzer = LatencySegmentAnalyzer(collector)
        worst = analyzer.worst_cohorts(metric="ttft_ms", top_n=5)
    """

    def __init__(self, collector: RUMBeaconCollector):
        self._collector = collector

    def _bucket_tool_calls(self, count: int) -> str:
        if count == 0:
            return "0"
        if count <= 2:
            return "1-2"
        if count <= 5:
            return "3-5"
        return "6+"

    def _extract_dimensions(self, beacon: Dict[str, Any]) -> Dict[str, str]:
        return {
            "segment": beacon.get("user_segment", "unknown"),
            "model": beacon.get("model", "unknown"),
            "tool_calls": self._bucket_tool_calls(
                int(beacon.get("tool_call_count", 0))
            ),
        }

    def cohort_percentiles(
        self,
        metric: str = "ttft_ms",
        window_s: float = 3600.0,
    ) -> List[Dict[str, Any]]:
        now = time.time()
        cutoff = now - window_s
        by_cohort: Dict[str, List[float]] = defaultdict(list)

        for beacon in self._collector._buffers.get("_all", []):
            if beacon.get("_ingested_at", 0) < cutoff:
                continue
            val = beacon.get(metric)
            if val is None:
                continue
            dims = self._extract_dimensions(beacon)
            key = "|".join(f"{k}={v}" for k, v in sorted(dims.items()))
            by_cohort[key].append(val)

        results = []
        for cohort_key, values in by_cohort.items():
            values.sort()
            n = len(values)
            if n < 5:
                continue
            p95_idx = int(n * 0.95)
            p99_idx = int(n * 0.99)
            results.append({
                "cohort": cohort_key,
                "n": n,
                "p50": round(values[n // 2], 1),
                "p95": round(values[min(p95_idx, n - 1)], 1),
                "p99": round(values[min(p99_idx, n - 1)], 1),
            })
        return results

    def worst_cohorts(
        self,
        metric: str = "ttft_ms",
        percentile: str = "p95",
        top_n: int = 5,
        window_s: float = 3600.0,
    ) -> List[Dict[str, Any]]:
        cohorts = self.cohort_percentiles(metric=metric, window_s=window_s)
        return sorted(
            cohorts, key=lambda x: -x.get(percentile, 0)
        )[:top_n]
```

---

## Solution 4: ServerClientLatencyDelta — Quantify Server vs User-Perceived Gap

```python
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ServerClientLatencyDelta:
    """
    Correlates server-side latency records with client RUM beacons
    by request_id and computes the gap: client_total - server_processing.
    A large delta indicates network overhead, queue time, or client-side
    rendering delays that server metrics alone would never reveal.

    Usage:
        delta_tracker = ServerClientLatencyDelta()
        delta_tracker.record_server(request_id="abc", server_ms=310.0)
        delta_tracker.record_client_beacon(beacon_dict)
        report = delta_tracker.delta_report(window_s=300)
    """

    def __init__(self, max_records: int = 5_000):
        self._server: Dict[str, Dict[str, Any]] = {}
        self._client: Dict[str, Dict[str, Any]] = {}
        self._deltas: List[float] = []
        self._max = max_records

    def record_server(self, request_id: str, server_ms: float,
                       model: str = "", tool_count: int = 0):
        self._server[request_id] = {
            "server_ms": server_ms,
            "model": model,
            "tool_count": tool_count,
            "ts": time.time(),
        }
        self._maybe_compute_delta(request_id)
        self._evict_old()

    def record_client_beacon(self, beacon: Dict[str, Any]):
        rid = beacon.get("request_id")
        if not rid:
            return
        self._client[rid] = beacon
        self._maybe_compute_delta(rid)

    def _maybe_compute_delta(self, request_id: str):
        server = self._server.get(request_id)
        client = self._client.get(request_id)
        if not server or not client:
            return
        client_total = client.get("total_latency_ms")
        if client_total is None:
            return
        delta = client_total - server["server_ms"]
        self._deltas.append(delta)
        if delta > 2000:
            logger.warning(
                "large_latency_delta request_id=%s server_ms=%.0f "
                "client_ms=%.0f delta_ms=%.0f",
                request_id, server["server_ms"], client_total, delta,
            )

    def _evict_old(self):
        if len(self._server) > self._max:
            oldest = sorted(self._server, key=lambda k: self._server[k]["ts"])
            for key in oldest[:100]:
                self._server.pop(key, None)
                self._client.pop(key, None)

    def delta_report(self) -> Dict[str, Any]:
        if not self._deltas:
            return {"n": 0}
        d = sorted(self._deltas)
        n = len(d)
        return {
            "n": n,
            "p50_delta_ms": round(d[n // 2], 1),
            "p95_delta_ms": round(d[int(n * 0.95)], 1),
            "p99_delta_ms": round(d[int(n * 0.99)], 1),
            "mean_delta_ms": round(sum(d) / n, 1),
            "overhead_fraction": round(
                sum(1 for x in d if x > 500) / n, 3
            ),
        }
```

---

## Solution 5: RUMAlertPolicy — Trigger Alerts on Latency Regressions

```python
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LatencyAlert:
    metric: str
    percentile: str
    segment: str
    current_ms: float
    threshold_ms: float
    triggered_at: float


class RUMAlertPolicy:
    """
    Evaluates RUM percentile thresholds on a schedule and fires alerts
    when user-perceived latency crosses configured SLO budgets.

    Usage:
        policy = RUMAlertPolicy(collector)
        policy.add_rule("ttft_ms", "p95", segment="pro", threshold_ms=2000)
        policy.add_rule("total_latency_ms", "p99", segment="_all", threshold_ms=15000)

        alerts = policy.evaluate()
        for alert in alerts:
            send_pagerduty(alert)
    """

    def __init__(self, collector: RUMBeaconCollector,
                  on_alert: Optional[Callable[[LatencyAlert], None]] = None):
        self._collector = collector
        self._rules: List[Dict[str, Any]] = []
        self._on_alert = on_alert or self._log_alert
        self._last_alert_ts: Dict[str, float] = {}
        self._cooldown_s = 300.0

    @staticmethod
    def _log_alert(alert: LatencyAlert):
        logger.critical(
            "rum_slo_breach metric=%s percentile=%s segment=%s "
            "current_ms=%.0f threshold_ms=%.0f",
            alert.metric, alert.percentile, alert.segment,
            alert.current_ms, alert.threshold_ms,
        )

    def add_rule(self, metric: str, percentile: str,
                  segment: str = "_all",
                  threshold_ms: float = 3000.0,
                  window_s: float = 300.0):
        self._rules.append({
            "metric": metric,
            "percentile": percentile,
            "segment": segment,
            "threshold_ms": threshold_ms,
            "window_s": window_s,
        })

    def evaluate(self) -> List[LatencyAlert]:
        alerts = []
        now = time.time()
        for rule in self._rules:
            report = self._collector.percentiles(
                segment=rule["segment"],
                window_s=rule["window_s"],
                metric=rule["metric"],
            )
            value = report.get(rule["percentile"])
            if value is None or report.get("n", 0) < 10:
                continue
            if value > rule["threshold_ms"]:
                rule_key = f"{rule['metric']}:{rule['percentile']}:{rule['segment']}"
                if now - self._last_alert_ts.get(rule_key, 0) > self._cooldown_s:
                    alert = LatencyAlert(
                        metric=rule["metric"],
                        percentile=rule["percentile"],
                        segment=rule["segment"],
                        current_ms=value,
                        threshold_ms=rule["threshold_ms"],
                        triggered_at=now,
                    )
                    self._on_alert(alert)
                    self._last_alert_ts[rule_key] = now
                    alerts.append(alert)
        return alerts
```

---

## Solution 6: RUMDashboard — Unified Real-User Monitoring View

```python
import logging
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class RUMDashboard:
    """
    Aggregates all RUM components into a single dashboard object
    with a health_report() suitable for a monitoring endpoint or
    Grafana JSON data source.

    Usage:
        dashboard = RUMDashboard()

        # Wire up beacon intake:
        dashboard.ingest(beacon_payload)

        # In server beacon handler, call after credential check:
        dashboard.record_server_latency(request_id, server_ms, model)

        # Expose via HTTP:
        @app.get("/internal/rum/dashboard")
        async def rum():
            return dashboard.report()
    """

    def __init__(self, alert_thresholds: Optional[Dict[str, float]] = None):
        self._collector = RUMBeaconCollector()
        self._analyzer = LatencySegmentAnalyzer(self._collector)
        self._delta = ServerClientLatencyDelta()
        self._alerts = RUMAlertPolicy(self._collector)

        thresholds = alert_thresholds or {}
        self._alerts.add_rule(
            "ttft_ms", "p95", threshold_ms=thresholds.get("ttft_p95", 3000)
        )
        self._alerts.add_rule(
            "total_latency_ms", "p99",
            threshold_ms=thresholds.get("total_p99", 15000)
        )

    def ingest(self, payload: Dict[str, Any]):
        self._collector.ingest(payload)
        self._delta.record_client_beacon(payload)

    def record_server_latency(self, request_id: str, server_ms: float,
                               model: str = "", tool_count: int = 0):
        self._delta.record_server(request_id, server_ms, model, tool_count)

    def report(self, window_s: float = 300.0) -> Dict[str, Any]:
        return {
            "ttft": {
                seg: self._collector.percentiles(
                    segment=seg, window_s=window_s, metric="ttft_ms"
                )
                for seg in ("_all", "free", "pro", "enterprise")
            },
            "total_latency": self._collector.percentiles(
                segment="_all", window_s=window_s, metric="total_latency_ms"
            ),
            "network_rtt": self._collector.percentiles(
                segment="_all", window_s=window_s, metric="network_rtt_ms"
            ),
            "server_client_delta": self._delta.delta_report(),
            "worst_cohorts": self._analyzer.worst_cohorts(
                metric="ttft_ms", top_n=5, window_s=window_s
            ),
            "collector_stats": self._collector.stats(),
            "active_alerts": [
                {
                    "metric": a.metric,
                    "percentile": a.percentile,
                    "segment": a.segment,
                    "current_ms": a.current_ms,
                }
                for a in self._alerts.evaluate()
            ],
            "generated_at": time.time(),
        }
```

---

## Comparison

| Approach | Client Timestamps | Percentiles | Segmentation | Server Delta | Alerting | Integrated |
|---|---|---|---|---|---|---|
| **ClientLatencyBeacon** | Yes | No | No | No | No | No |
| **RUMBeaconCollector** | Stores | Yes | By segment | No | No | No |
| **LatencySegmentAnalyzer** | No | Yes | Multi-dim | No | No | No |
| **ServerClientLatencyDelta** | No | No | No | Yes | No | No |
| **RUMAlertPolicy** | No | Via collector | Via collector | No | Yes | No |
| **RUMDashboard** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: the most actionable RUM metric is time-to-first-token (TTFT) measured client-side, not server-side. A 200ms server TTFT becomes 2–4s for users on high-latency mobile connections. Segment by `(user_tier, model, tool_call_count_bucket)` to find which combinations breach SLOs — often it's the "enterprise + opus + 3+ tools" cohort that drives p99 regressions while aggregate metrics look healthy. Set beacon collection to sample 10–20% of requests in production to keep overhead minimal; percentiles computed on 10% samples are statistically valid for p95/p99 with >1000 requests/minute.
