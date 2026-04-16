---
title: "Agent Doesn't Implement Root Cause Analysis Automation"
description: "When agents fail in production, engineers manually correlate error spikes with recent deployments, config changes, and metric shifts — a process that takes hours. Implement automated root cause analysis that correlates error signals with change events, isolates the most likely causal factor, and surfaces a ranked hypothesis list within seconds of incident detection."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-root-cause-analysis-automation
tags: [root-cause-analysis, incident-response, observability, correlation, causality, debugging]
symptoms:
  - "P1 incident takes 45 minutes to diagnose because engineers manually grep logs and dashboards"
  - "Error spike correlates with a deployment that happened 10 minutes earlier — nobody noticed"
  - "Same root cause recurs because post-mortem identified symptoms, not cause"
  - "On-call engineer has to check 12 dashboards before finding the failing component"
  - "No automated correlation between metric anomalies and recent change events"
---

## Why This Happens

Root cause analysis requires correlating signals from multiple sources: error rates, latency histograms, deployment events, config changes, downstream dependency health, and resource utilization. Humans do this intuitively but slowly. Automated RCA systems apply temporal correlation (did a change event precede the error spike?), causal graph traversal (which upstream service is most likely to cause this symptom?), and statistical change detection (did a metric shift at the same time as the error?) to surface hypotheses faster than any manual process.

## Solution 1: Change Event Correlator

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class ChangeEvent:
    event_id: str
    event_type: str     # "deployment" | "config_change" | "scale_event" | "flag_toggle"
    component: str
    description: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    author: str = ""

@dataclass
class ErrorSpike:
    component: str
    error_type: str
    baseline_rate: float
    spike_rate: float
    detected_at: float
    duration_seconds: float = 0.0

    @property
    def spike_multiplier(self) -> float:
        return self.spike_rate / max(self.baseline_rate, 0.001)

class ChangeEventCorrelator:
    """
    Correlates error spikes with preceding change events.
    Scores each change by: recency, component match, and event type risk weight.
    """

    EVENT_RISK_WEIGHTS = {
        "deployment": 0.9,
        "config_change": 0.75,
        "flag_toggle": 0.6,
        "scale_event": 0.4,
        "certificate_rotation": 0.5,
    }

    def __init__(
        self,
        correlation_window_seconds: float = 1800.0,   # 30 min
        max_lag_seconds: float = 900.0,
    ):
        self._window = correlation_window_seconds
        self._max_lag = max_lag_seconds
        self._events: List[ChangeEvent] = []

    def record_change(self, event: ChangeEvent) -> None:
        self._events.append(event)
        # Prune events older than window
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e.timestamp >= cutoff]

    def correlate(self, spike: ErrorSpike) -> List[dict]:
        """
        Returns ranked list of change events most likely to have caused the spike.
        Each entry includes the event and a correlation score [0, 1].
        """
        candidates = []

        for event in self._events:
            lag = spike.detected_at - event.timestamp
            if lag < 0 or lag > self._max_lag:
                continue   # event after spike, or too old

            # Recency score: exponential decay, half-life = 5 minutes
            import math
            recency = math.exp(-lag / 300.0)

            # Component match score
            comp_match = 1.0 if event.component == spike.component else 0.3
            if spike.component in event.component or event.component in spike.component:
                comp_match = 0.7

            # Event type risk
            risk = self.EVENT_RISK_WEIGHTS.get(event.event_type, 0.3)

            score = recency * comp_match * risk

            candidates.append({
                "event": event,
                "score": round(score, 4),
                "lag_seconds": round(lag, 1),
                "recency_score": round(recency, 3),
                "component_match": comp_match,
                "risk_weight": risk,
            })

        return sorted(candidates, key=lambda x: x["score"], reverse=True)
```

## Solution 2: Metric Change Detector

```python
import math
import time
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional
from collections import deque

@dataclass
class MetricChangePoint:
    metric_name: str
    component: str
    change_type: str     # "spike" | "drop" | "trend_shift" | "variance_increase"
    before_mean: float
    after_mean: float
    change_ratio: float
    detected_at: float
    confidence: float

class MetricChangeDetector:
    """
    Detects significant metric changes using a sliding window comparison.
    Compares the recent window (last N samples) against the baseline window
    (previous M samples) using a simple t-statistic for mean shift detection.
    """

    def __init__(self, baseline_window: int = 60, recent_window: int = 10):
        self._baseline_n = baseline_window
        self._recent_n = recent_window
        # metric_key -> deque of (value, timestamp)
        self._series: Dict[str, Deque] = {}

    def _key(self, metric_name: str, component: str) -> str:
        return f"{component}:{metric_name}"

    def record(self, metric_name: str, component: str, value: float) -> None:
        k = self._key(metric_name, component)
        if k not in self._series:
            self._series[k] = deque(maxlen=self._baseline_n + self._recent_n)
        self._series[k].append((value, time.monotonic()))

    def _stats(self, values: List[float]) -> tuple[float, float]:
        n = len(values)
        if n == 0:
            return 0.0, 0.0
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / max(n - 1, 1)
        return mean, math.sqrt(variance)

    def detect_changes(
        self,
        metric_name: str,
        component: str,
        spike_threshold: float = 2.0,
    ) -> Optional[MetricChangePoint]:
        k = self._key(metric_name, component)
        series = list(self._series.get(k, []))

        min_total = self._recent_n + self._baseline_n // 2
        if len(series) < min_total:
            return None

        recent_vals = [v for v, _ in series[-self._recent_n:]]
        baseline_vals = [v for v, _ in series[:-self._recent_n]]

        recent_mean, recent_std = self._stats(recent_vals)
        baseline_mean, baseline_std = self._stats(baseline_vals)

        if baseline_mean == 0:
            return None

        change_ratio = recent_mean / baseline_mean

        # Welch's t-statistic for unequal variance
        pooled_se = math.sqrt(
            (recent_std ** 2 / max(len(recent_vals), 1))
            + (baseline_std ** 2 / max(len(baseline_vals), 1))
        )
        if pooled_se < 1e-9:
            return None
        t_stat = abs(recent_mean - baseline_mean) / pooled_se

        # Convert t-stat to rough confidence (sigmoid approximation)
        confidence = 1.0 / (1.0 + math.exp(-0.5 * (t_stat - 4.0)))

        if abs(change_ratio - 1.0) < (spike_threshold - 1.0) / spike_threshold:
            return None

        change_type = (
            "spike" if change_ratio > spike_threshold
            else "drop" if change_ratio < 1.0 / spike_threshold
            else "variance_increase"
        )

        return MetricChangePoint(
            metric_name=metric_name,
            component=component,
            change_type=change_type,
            before_mean=round(baseline_mean, 4),
            after_mean=round(recent_mean, 4),
            change_ratio=round(change_ratio, 3),
            detected_at=time.time(),
            confidence=round(confidence, 3),
        )
```

## Solution 3: Causal Graph Traverser

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

@dataclass
class ServiceNode:
    name: str
    tier: str    # "frontend" | "api" | "worker" | "database" | "external"
    dependencies: List[str] = field(default_factory=list)

class CausalGraphTraverser:
    """
    Models service dependencies as a directed graph.
    When a symptom (error spike) is detected in a node, traverses
    upstream dependencies to find the most likely causal origin.
    Weighted by: dependency distance, current health status, recent changes.
    """

    def __init__(self):
        self._nodes: Dict[str, ServiceNode] = {}
        self._health: Dict[str, float] = {}   # node_name -> health score 0.0–1.0

    def add_node(self, node: ServiceNode) -> None:
        self._nodes[node.name] = node

    def update_health(self, node_name: str, health_score: float) -> None:
        self._health[node_name] = max(0.0, min(1.0, health_score))

    def _upstream_nodes(self, node_name: str, visited: Optional[Set[str]] = None) -> List[tuple]:
        """BFS upstream — returns [(node_name, distance)]."""
        if visited is None:
            visited = set()
        result = []
        queue = [(node_name, 0)]
        while queue:
            current, dist = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            node = self._nodes.get(current)
            if node:
                for dep in node.dependencies:
                    if dep not in visited:
                        result.append((dep, dist + 1))
                        queue.append((dep, dist + 1))
        return result

    def hypothesize_causes(
        self,
        affected_component: str,
        unhealthy_components: List[str],
        changed_components: List[str],
    ) -> List[dict]:
        """
        Returns ranked hypothesis list: each entry is a component that
        could have caused the failure in affected_component.
        """
        upstream = self._upstream_nodes(affected_component)
        unhealthy_set = set(unhealthy_components)
        changed_set = set(changed_components)

        hypotheses = []
        for dep_name, distance in upstream:
            score = 0.0

            # Unhealthy upstream is strong signal
            if dep_name in unhealthy_set:
                score += 0.6 / (distance + 1)

            # Recent change upstream is medium signal
            if dep_name in changed_set:
                score += 0.4 / (distance + 1)

            # Low health score
            health = self._health.get(dep_name, 1.0)
            if health < 0.7:
                score += (1.0 - health) * 0.3 / (distance + 1)

            if score > 0.05:
                hypotheses.append({
                    "component": dep_name,
                    "distance": distance,
                    "score": round(score, 4),
                    "health": health,
                    "is_unhealthy": dep_name in unhealthy_set,
                    "recently_changed": dep_name in changed_set,
                })

        return sorted(hypotheses, key=lambda x: x["score"], reverse=True)
```

## Solution 4: Automated RCA Engine

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class RCAReport:
    incident_id: str
    affected_component: str
    error_spike: ErrorSpike
    top_hypothesis: Optional[str]
    hypotheses: List[dict]
    change_correlations: List[dict]
    metric_changes: List[MetricChangePoint]
    causal_graph_hints: List[dict]
    confidence: float
    generated_at: float = field(default_factory=time.time)
    summary: str = ""

class AutomatedRCAEngine:
    """
    Orchestrates all RCA signals into a single actionable report.
    Triggered on error spike detection; returns ranked hypotheses
    with supporting evidence within milliseconds.
    """

    def __init__(
        self,
        correlator: ChangeEventCorrelator,
        metric_detector: MetricChangeDetector,
        graph_traverser: CausalGraphTraverser,
    ):
        self._correlator = correlator
        self._metric_detector = metric_detector
        self._graph = graph_traverser

    def analyze(
        self,
        spike: ErrorSpike,
        metrics_to_check: List[tuple],   # [(metric_name, component), ...]
        unhealthy_components: List[str],
    ) -> RCAReport:
        import uuid
        incident_id = str(uuid.uuid4())[:8]

        # 1. Correlate change events
        change_corrs = self._correlator.correlate(spike)

        # 2. Detect concurrent metric changes
        metric_changes = []
        for metric_name, component in metrics_to_check:
            change = self._metric_detector.detect_changes(metric_name, component)
            if change:
                metric_changes.append(change)

        # 3. Causal graph hints
        changed_comps = list({c["event"].component for c in change_corrs[:5]})
        unhealthy_comps = unhealthy_components + [
            mc.component for mc in metric_changes if mc.change_type == "drop"
        ]
        graph_hints = self._graph.hypothesize_causes(
            spike.component, unhealthy_comps, changed_comps
        )

        # 4. Merge and rank all hypotheses
        all_hypotheses = []
        for corr in change_corrs[:5]:
            all_hypotheses.append({
                "source": "change_event",
                "component": corr["event"].component,
                "score": corr["score"],
                "evidence": f"{corr['event'].event_type} by {corr['event'].author} ({corr['lag_seconds']}s before spike)",
            })
        for hint in graph_hints[:5]:
            all_hypotheses.append({
                "source": "causal_graph",
                "component": hint["component"],
                "score": hint["score"],
                "evidence": f"upstream dependency, distance={hint['distance']}, health={hint['health']:.2f}",
            })

        all_hypotheses.sort(key=lambda x: x["score"], reverse=True)

        top = all_hypotheses[0]["component"] if all_hypotheses else None
        confidence = all_hypotheses[0]["score"] if all_hypotheses else 0.0

        summary = self._build_summary(spike, top, all_hypotheses[:3], metric_changes)

        return RCAReport(
            incident_id=incident_id,
            affected_component=spike.component,
            error_spike=spike,
            top_hypothesis=top,
            hypotheses=all_hypotheses[:10],
            change_correlations=change_corrs[:5],
            metric_changes=metric_changes,
            causal_graph_hints=graph_hints[:5],
            confidence=round(confidence, 3),
            summary=summary,
        )

    def _build_summary(
        self,
        spike: ErrorSpike,
        top: Optional[str],
        hypotheses: List[dict],
        metric_changes: List[MetricChangePoint],
    ) -> str:
        parts = [
            f"Error spike in {spike.component}: "
            f"{spike.spike_rate:.1f}/s vs baseline {spike.baseline_rate:.1f}/s "
            f"({spike.spike_multiplier:.1f}x)."
        ]
        if top:
            parts.append(f"Most likely cause: {top}.")
        if metric_changes:
            changed = ", ".join(f"{mc.metric_name}({mc.change_type})" for mc in metric_changes[:3])
            parts.append(f"Concurrent metric changes: {changed}.")
        return " ".join(parts)
```

## Solution 5: RCA Feedback Loop

```python
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class RCAFeedback:
    incident_id: str
    actual_root_cause: str
    top_hypothesis_was_correct: bool
    rank_of_correct_hypothesis: Optional[int]
    notes: str = ""
    submitted_at: float = 0.0

class RCAFeedbackLoop:
    """
    Collects post-incident feedback on RCA accuracy.
    Tracks precision@1 and MRR to measure hypothesis quality over time.
    Feeds back into scoring weights if hypotheses are consistently wrong.
    """

    def __init__(self):
        self._feedback: List[RCAFeedback] = []

    def submit_feedback(
        self,
        incident_id: str,
        actual_root_cause: str,
        report: RCAReport,
    ) -> RCAFeedback:
        rank = None
        for i, h in enumerate(report.hypotheses):
            if actual_root_cause in h["component"] or h["component"] in actual_root_cause:
                rank = i + 1
                break

        fb = RCAFeedback(
            incident_id=incident_id,
            actual_root_cause=actual_root_cause,
            top_hypothesis_was_correct=(rank == 1),
            rank_of_correct_hypothesis=rank,
            submitted_at=time.time(),
        )
        self._feedback.append(fb)
        return fb

    def precision_at_1(self) -> float:
        if not self._feedback:
            return 0.0
        correct = sum(1 for f in self._feedback if f.top_hypothesis_was_correct)
        return correct / len(self._feedback)

    def mean_reciprocal_rank(self) -> float:
        if not self._feedback:
            return 0.0
        scores = []
        for f in self._feedback:
            if f.rank_of_correct_hypothesis:
                scores.append(1.0 / f.rank_of_correct_hypothesis)
            else:
                scores.append(0.0)
        return sum(scores) / len(scores)

    def accuracy_report(self) -> dict:
        return {
            "total_incidents": len(self._feedback),
            "precision_at_1": round(self.precision_at_1(), 3),
            "mean_reciprocal_rank": round(self.mean_reciprocal_rank(), 3),
            "not_found_rate": round(
                sum(1 for f in self._feedback if f.rank_of_correct_hypothesis is None)
                / max(len(self._feedback), 1), 3
            ),
        }
```

## Solution 6: RCA Runbook Generator

```python
from typing import List

class RCARunbookGenerator:
    """
    Converts an RCA report into a structured investigation runbook:
    ordered list of checks and commands for on-call engineers.
    """

    INVESTIGATION_STEPS = {
        "deployment": [
            "Check deployment logs: `kubectl rollout history deploy/{component}`",
            "Compare error rates before/after deploy: filter logs to ±15min of deploy time",
            "Verify new version health checks are passing",
            "If rollback needed: `kubectl rollout undo deploy/{component}`",
        ],
        "config_change": [
            "Diff the config change: `git diff HEAD~1 configs/{component}.yaml`",
            "Check if the changed config key is used by the error path",
            "Verify config was applied: check application /config or /env endpoints",
            "Rollback: revert the config commit and apply",
        ],
        "upstream_unhealthy": [
            "Check upstream {component} health: `curl {component}/health`",
            "Review upstream error rate in monitoring",
            "Check circuit breaker state for {component}",
            "If upstream is down: activate fallback mode or queue requests",
        ],
        "default": [
            "Check recent error logs: filter by error_type in the last 30 minutes",
            "Review metric dashboards for the affected component",
            "Check downstream dependencies for cascading failures",
            "Review recent changes in git log",
        ],
    }

    def generate(self, report: RCAReport) -> str:
        lines = [
            f"# RCA Runbook — Incident {report.incident_id}",
            f"## Summary",
            f"{report.summary}",
            f"",
            f"## Top Hypotheses",
        ]

        for i, h in enumerate(report.hypotheses[:3], 1):
            lines.append(
                f"{i}. **{h['component']}** (score={h['score']:.3f}) — {h['evidence']}"
            )

        lines += ["", "## Investigation Steps"]

        top_event_type = "default"
        if report.change_correlations:
            top_event_type = report.change_correlations[0]["event"].event_type

        steps = self.INVESTIGATION_STEPS.get(top_event_type, self.INVESTIGATION_STEPS["default"])
        comp = report.top_hypothesis or report.affected_component
        for step in steps:
            lines.append(f"- {step.replace('{component}', comp)}")

        if report.metric_changes:
            lines += ["", "## Anomalous Metrics at Time of Incident"]
            for mc in report.metric_changes:
                lines.append(
                    f"- **{mc.metric_name}** ({mc.component}): "
                    f"{mc.before_mean} → {mc.after_mean} ({mc.change_type}, "
                    f"confidence={mc.confidence:.2f})"
                )

        return "\n".join(lines)
```

## Comparison

| Approach | Signal Type | Requires History | Automated | Feedback Loop |
|---|---|---|---|---|
| ChangeEventCorrelator | Temporal proximity | Yes (change log) | Yes | No |
| MetricChangeDetector | Statistical shift | Yes (metric series) | Yes | No |
| CausalGraphTraverser | Topology + health | Yes (service graph) | Yes | No |
| AutomatedRCAEngine | All combined | Yes | Yes | No |
| RCAFeedbackLoop | Outcome accuracy | Yes (feedback) | No | Yes |
| RCARunbookGenerator | Hypothesis → action | No | Yes | No |

**Best for production**: Deploy `AutomatedRCAEngine` as an incident webhook subscriber — on any PagerDuty/Alertmanager alert, it auto-runs within seconds and posts the top hypotheses to the incident channel. Feed `ChangeEventCorrelator` from CI/CD webhooks and config management events. Update `CausalGraphTraverser` health scores from your existing health check system. Collect `RCAFeedbackLoop` responses from post-incident reviews to track hypothesis accuracy and identify systematic blind spots.
