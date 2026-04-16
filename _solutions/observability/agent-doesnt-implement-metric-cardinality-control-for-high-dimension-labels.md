---
title: "Agent Doesn't Implement Metric Cardinality Control for High-Dimension Labels"
description: "AI agents that attach high-cardinality labels — user IDs, session IDs, raw prompt hashes, or model version strings — to Prometheus/OTel metrics create a combinatorial explosion of time series. A system with 10k users × 50 tools × 10 models produces 5M unique series, exhausting Prometheus memory and crashing the metrics backend. Cardinality control bounds label values to a finite set, replacing rare values with a catch-all bucket."
date: 2025-02-15
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-metric-cardinality-control-for-high-dimension-labels
tags:
  - cardinality
  - prometheus
  - opentelemetry
  - metrics
  - labels
  - time-series
  - observability
symptoms:
  - "Prometheus OOM-kills after deploying a new agent feature that adds a user_id label"
  - "Grafana dashboard shows 2M active time series for a single agent metric"
  - "Metrics ingestion cost triples after adding session_id as a label dimension"
  - "Alert rules become flaky because cardinality spikes cause Prometheus query timeouts"
  - "New label dimension is added without understanding how many unique values it has"
---

## Problem

Every unique combination of label values creates a new time series in Prometheus or any OTLP-compatible backend. Labels like `user_id`, `conversation_id`, or raw `model_version` strings are unbounded — each new user or deployment creates a new series that is never garbage-collected during the retention window. 10,000 users × 100 tool names × 5 environments = 5 million series. Prometheus stores each series in memory; at ~3–5 KB per series, 5M series consumes 15–25 GB of RAM. Cardinality control normalises label values: only the top-N values get their own bucket; everything else is labelled `__other__`.

---

## Solution 1: CardinalityBoundedLabels — Clamp Label Values to a Fixed Set

```python
import threading
from collections import Counter
from typing import Any, Dict, FrozenSet, Optional, Set


class CardinalityBoundedLabels:
    """
    Clamps metric label values to a bounded vocabulary.
    Values not in the top-N are replaced with the overflow label.
    Learns the vocabulary from observed traffic; locks it after warm-up.

    Usage:
        labels = CardinalityBoundedLabels(
            label_name="tool_name",
            max_values=50,
            overflow_value="__other__",
        )

        # In metric recording path:
        safe_tool = labels.clamp(tool_name)
        counter.labels(tool=safe_tool).inc()
    """

    def __init__(self, label_name: str,
                 max_values: int = 50,
                 overflow_value: str = "__other__",
                 allowed: Optional[Set[str]] = None):
        self.label_name = label_name
        self._max = max_values
        self._overflow = overflow_value
        self._lock = threading.Lock()
        self._counts: Counter = Counter()
        self._allowed: Optional[FrozenSet[str]] = (
            frozenset(allowed) if allowed else None
        )

    def clamp(self, value: str) -> str:
        if self._allowed is not None:
            return value if value in self._allowed else self._overflow

        with self._lock:
            self._counts[value] += 1
            if len(self._counts) <= self._max:
                return value
            # Value already in top-N?
            top_n = {v for v, _ in self._counts.most_common(self._max)}
            return value if value in top_n else self._overflow

    def lock_vocabulary(self):
        """Freeze the vocabulary after warm-up. New values go to overflow."""
        with self._lock:
            top_n = {v for v, _ in self._counts.most_common(self._max)}
            self._allowed = frozenset(top_n)

    def vocabulary(self) -> Set[str]:
        with self._lock:
            if self._allowed is not None:
                return set(self._allowed)
            return {v for v, _ in self._counts.most_common(self._max)}

    def overflow_rate(self) -> float:
        with self._lock:
            total = sum(self._counts.values())
            if not total:
                return 0.0
            top_n = {v for v, _ in self._counts.most_common(self._max)}
            overflowed = sum(
                c for v, c in self._counts.items() if v not in top_n
            )
            return overflowed / total

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "label": self.label_name,
                "unique_values": len(self._counts),
                "max_values": self._max,
                "overflow_rate": round(self.overflow_rate(), 4),
                "locked": self._allowed is not None,
            }
```

---

## Solution 2: MetricCardinalityGuard — Multi-Label Cardinality Enforcement

```python
import logging
import threading
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class MetricCardinalityGuard:
    """
    Enforces cardinality limits across multiple label dimensions for a
    single metric. Counts unique label combinations (not per-dimension),
    which is the actual driver of time series count.

    Usage:
        guard = MetricCardinalityGuard(
            metric_name="agent_tool_calls_total",
            max_series=5000,
            overflow_suffix="__overflow__",
        )

        safe_labels = guard.safe_labels({
            "tool": tool_name,
            "model": model_id,
            "env": environment,
        })
        counter.labels(**safe_labels).inc()
    """

    def __init__(self, metric_name: str,
                 max_series: int = 5000,
                 overflow_suffix: str = "__overflow__",
                 per_label_limits: Optional[Dict[str, int]] = None):
        self._metric = metric_name
        self._max = max_series
        self._overflow = overflow_suffix
        self._per_label = per_label_limits or {}
        self._seen: Set[Tuple] = set()
        self._lock = threading.Lock()
        self._overflow_count = 0
        self._total_count = 0

        # Per-label clamps
        self._label_clamps: Dict[str, CardinalityBoundedLabels] = {
            label: CardinalityBoundedLabels(label, limit)
            for label, limit in self._per_label.items()
        }

    def safe_labels(self, labels: Dict[str, str]) -> Dict[str, str]:
        self._total_count += 1

        # Apply per-label clamping
        clamped = {
            k: (self._label_clamps[k].clamp(v) if k in self._label_clamps else v)
            for k, v in labels.items()
        }

        # Check total series count
        key = tuple(sorted(clamped.items()))
        with self._lock:
            if key in self._seen or len(self._seen) < self._max:
                self._seen.add(key)
                return clamped

        # Overflow: replace all label values with overflow marker
        self._overflow_count += 1
        if self._overflow_count % 1000 == 1:
            logger.warning(
                "metric_cardinality_overflow metric=%s series_count=%d max=%d",
                self._metric, len(self._seen), self._max,
            )
        return {k: self._overflow for k in clamped}

    def series_count(self) -> int:
        with self._lock:
            return len(self._seen)

    def report(self) -> Dict[str, Any]:
        return {
            "metric": self._metric,
            "active_series": self.series_count(),
            "max_series": self._max,
            "total_observations": self._total_count,
            "overflow_observations": self._overflow_count,
            "overflow_rate": round(
                self._overflow_count / max(self._total_count, 1), 4
            ),
        }
```

---

## Solution 3: LabelNormaliser — Reduce Cardinality via Value Bucketing

```python
import re
from typing import Callable, Dict, List, Optional, Tuple


class LabelNormaliser:
    """
    Normalises raw label values into low-cardinality buckets using
    regex rules. Transforms unbounded values like model version strings,
    HTTP paths, or error messages into bucketed dimensions.

    Usage:
        norm = LabelNormaliser()
        norm.add_rule("model", [
            (r"claude-.*-4-.*", "claude-4"),
            (r"gpt-4.*",        "gpt-4"),
            (r"gpt-3\.5.*",     "gpt-3.5"),
        ], default="other-model")
        norm.add_rule("path", [
            (r"/api/v\d+/tools.*", "/api/tools"),
            (r"/api/v\d+/chat.*",  "/api/chat"),
        ], default="/api/other")

        safe = norm.normalise({"model": "claude-sonnet-4-6", "path": "/api/v2/tools/web_search"})
        # -> {"model": "claude-4", "path": "/api/tools"}
    """

    def __init__(self):
        self._rules: Dict[str, Tuple[List[Tuple[re.Pattern, str]], str]] = {}

    def add_rule(self, label: str,
                  patterns: List[Tuple[str, str]],
                  default: str = "__other__"):
        compiled = [(re.compile(p), replacement) for p, replacement in patterns]
        self._rules[label] = (compiled, default)

    def normalise_value(self, label: str, value: str) -> str:
        rule = self._rules.get(label)
        if rule is None:
            return value
        patterns, default = rule
        for pattern, replacement in patterns:
            if pattern.search(value):
                return replacement
        return default

    def normalise(self, labels: Dict[str, str]) -> Dict[str, str]:
        return {k: self.normalise_value(k, v) for k, v in labels.items()}

    def add_truncation_rule(self, label: str,
                             max_length: int = 32,
                             truncate_to: Optional[Callable[[str], str]] = None):
        """Truncate long string values (e.g., error messages) to fixed length."""
        fn = truncate_to or (lambda v: v[:max_length] + "…" if len(v) > max_length else v)
        self.add_rule(label, [], default="")
        patterns, _ = self._rules[label]
        # Inject a catch-all that applies truncation
        self._rules[label] = (patterns, "")
        _fn = fn

        class _TruncRule:
            def normalise_value(self_, lbl, val):
                if lbl == label:
                    return _fn(val)
                return val

        # Simpler: wrap normalise
        original = self.normalise_value

        def wrapped(lbl, val):
            if lbl == label:
                return _fn(val)
            return original(lbl, val)

        self.normalise_value = wrapped
```

---

## Solution 4: CardinalityMonitor — Detect and Alert on Cardinality Growth

```python
import logging
import time
from collections import defaultdict
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)


class CardinalityMonitor:
    """
    Tracks unique label value counts per metric+label combination and
    fires an alert when cardinality exceeds a threshold or grows faster
    than expected. Attach to the metric recording path.

    Usage:
        monitor = CardinalityMonitor(
            on_alert=send_slack_alert,
            max_cardinality=1000,
            growth_alert_pct=50.0,
        )

        # Record every metric observation:
        monitor.observe("tool_calls_total", {"tool": t, "model": m, "user": u})

        # Periodic check (e.g., every 5 minutes):
        monitor.check()
    """

    def __init__(self, on_alert: Optional[Callable] = None,
                 max_cardinality: int = 1000,
                 growth_alert_pct: float = 50.0,
                 check_window_s: float = 300.0):
        self._alert = on_alert or (lambda msg: logger.error("CARDINALITY ALERT: %s", msg))
        self._max = max_cardinality
        self._growth_pct = growth_alert_pct
        self._window = check_window_s

        # metric -> label -> set of unique values
        self._values: Dict[str, Dict[str, Set]] = defaultdict(lambda: defaultdict(set))
        self._last_counts: Dict[str, int] = {}
        self._last_check = time.monotonic()
        self._alerted: Dict[str, float] = {}

    def observe(self, metric: str, labels: Dict[str, str]):
        for label, value in labels.items():
            self._values[metric][label].add(value)

    def check(self):
        now = time.monotonic()
        for metric, label_map in self._values.items():
            # Series count = product of unique values per label (upper bound)
            series_estimate = 1
            for label, values in label_map.items():
                count = len(values)
                series_estimate *= count
                if count > self._max:
                    self._fire(
                        metric,
                        f"label '{label}' has {count} unique values > limit {self._max}",
                        now,
                    )

            # Growth check
            prev = self._last_counts.get(metric, series_estimate)
            growth_pct = (series_estimate - prev) / max(prev, 1) * 100
            if growth_pct > self._growth_pct:
                self._fire(
                    metric,
                    f"estimated series grew {growth_pct:.0f}% "
                    f"({prev} -> {series_estimate}) in {self._window:.0f}s",
                    now,
                )
            self._last_counts[metric] = series_estimate
        self._last_check = now

    def _fire(self, metric: str, msg: str, now: float):
        key = f"{metric}:{msg[:40]}"
        if now - self._alerted.get(key, 0) > self._window:
            self._alerted[key] = now
            self._alert(f"metric={metric} {msg}")

    def cardinality_report(self) -> Dict[str, Any]:
        return {
            metric: {
                label: len(values)
                for label, values in label_map.items()
            }
            for metric, label_map in self._values.items()
        }
```

---

## Solution 5: SafeMetricsRecorder — Drop-In Prometheus Label Safety Wrapper

```python
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SafeMetricsRecorder:
    """
    Wraps a Prometheus Counter/Histogram and intercepts label sets,
    applying normalisation, cardinality bounding, and monitoring before
    forwarding to the underlying metric.

    Usage:
        from prometheus_client import Counter, Histogram

        tool_calls = Counter(
            "agent_tool_calls_total",
            "Tool call count",
            ["tool", "model", "finish_reason"],
        )
        recorder = SafeMetricsRecorder(
            metric=tool_calls,
            guard=MetricCardinalityGuard("agent_tool_calls_total", max_series=2000),
            normaliser=label_normaliser,
            monitor=cardinality_monitor,
        )

        recorder.inc(tool="web_search", model="claude-sonnet-4-6", finish_reason="end_turn")
    """

    def __init__(self, metric,
                 guard: Optional[MetricCardinalityGuard] = None,
                 normaliser: Optional[LabelNormaliser] = None,
                 monitor: Optional[CardinalityMonitor] = None,
                 metric_name: str = ""):
        self._metric = metric
        self._guard = guard
        self._normaliser = normaliser
        self._monitor = monitor
        self._name = metric_name or getattr(metric, "_name", "unknown")

    def _safe(self, labels: Dict[str, str]) -> Dict[str, str]:
        if self._normaliser:
            labels = self._normaliser.normalise(labels)
        if self._guard:
            labels = self._guard.safe_labels(labels)
        if self._monitor:
            self._monitor.observe(self._name, labels)
        return labels

    def inc(self, amount: float = 1.0, **label_kwargs):
        safe = self._safe(label_kwargs)
        self._metric.labels(**safe).inc(amount)

    def observe(self, value: float, **label_kwargs):
        safe = self._safe(label_kwargs)
        self._metric.labels(**safe).observe(value)

    def set(self, value: float, **label_kwargs):
        safe = self._safe(label_kwargs)
        self._metric.labels(**safe).set(value)
```

---

## Solution 6: CardinalityBudgetManager — Global Series Budget Across All Metrics

```python
import logging
import threading
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class CardinalityBudgetManager:
    """
    Enforces a global time-series budget across all metrics in the agent.
    When the total budget is exceeded, new unique label combinations are
    rejected (mapped to overflow) regardless of which metric they belong to.
    Prevents a single runaway metric from starving all others.

    Usage:
        budget = CardinalityBudgetManager(total_budget=50_000)

        # Each metric gets a named guard backed by the shared budget:
        tool_guard = budget.guard_for("tool_calls_total", reserved=5000)
        llm_guard  = budget.guard_for("llm_invocations_total", reserved=2000)
    """

    def __init__(self, total_budget: int = 50_000):
        self._total = total_budget
        self._allocated: Dict[str, int] = {}
        self._guards: Dict[str, MetricCardinalityGuard] = {}
        self._lock = threading.Lock()

    def guard_for(self, metric_name: str,
                   reserved: int = 1000,
                   **guard_kwargs) -> MetricCardinalityGuard:
        with self._lock:
            already = sum(self._allocated.values())
            available = self._total - already
            actual = min(reserved, available)
            if actual <= 0:
                logger.warning(
                    "cardinality_budget_exhausted metric=%s requested=%d available=%d",
                    metric_name, reserved, available,
                )
                actual = 1  # all observations go to overflow
            self._allocated[metric_name] = actual
            guard = MetricCardinalityGuard(
                metric_name=metric_name,
                max_series=actual,
                **guard_kwargs,
            )
            self._guards[metric_name] = guard
            return guard

    def budget_report(self) -> Dict[str, Any]:
        with self._lock:
            used = {name: g.series_count() for name, g in self._guards.items()}
            return {
                "total_budget": self._total,
                "allocated": dict(self._allocated),
                "active_series": used,
                "total_active": sum(used.values()),
                "utilisation": round(sum(used.values()) / self._total, 3),
            }
```

---

## Comparison

| Approach | Per-Label Bound | Series Bound | Normalisation | Monitoring | Global Budget |
|---|---|---|---|---|---|
| **CardinalityBoundedLabels** | Yes | No | No | No | No |
| **MetricCardinalityGuard** | Yes | Yes | No | Partial | No |
| **LabelNormaliser** | No | No | Yes | No | No |
| **CardinalityMonitor** | No | No | No | Yes | No |
| **SafeMetricsRecorder** | Yes | Yes | Yes | Yes | No |
| **CardinalityBudgetManager** | No | Yes | No | No | Yes |

**Key insight**: the most common mistake is adding `user_id` or `session_id` as a Prometheus label. These are diagnostic IDs — they belong in structured logs or trace attributes, not metrics labels. Keep metric label sets to ≤ 5 dimensions, each with ≤ 50 unique values; that caps any single metric at 50^5 = 312M theoretical series, but in practice far fewer if values are correlated. Use `CardinalityBoundedLabels` to clamp open-ended dimensions at the point of recording, and `CardinalityMonitor` with an alerting callback to catch new high-cardinality labels before they reach production.
