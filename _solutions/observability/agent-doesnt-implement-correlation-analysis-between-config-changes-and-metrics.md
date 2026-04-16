---
title: "Agent Doesn't Implement Correlation Analysis Between Config Changes and Metrics"
description: "How to automatically detect when configuration changes (prompt updates, model swaps, parameter tweaks) causally impact agent metrics — using before/after comparison, time-series change detection, and statistical significance testing."
date: 2025-01-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-correlation-analysis-between-config-changes-and-metrics
tags:
  - observability
  - config-changes
  - metrics
  - correlation-analysis
  - change-detection
  - statistical-testing
  - deployment-safety
symptoms:
  - "No way to tell whether a prompt change improved or degraded response quality"
  - "Latency regression after model upgrade discovered days later by users, not monitors"
  - "Config changes and metric shifts are in separate systems with no join"
  - "Engineers manually correlate deploy timestamps with Grafana graphs"
  - "No automatic alerting when a config change precedes a metric degradation"
  - "Cannot attribute a business metric change (e.g., session length) to a specific config change"
---

## Why This Happens

Agent systems accumulate a stream of configuration changes — prompt template updates, model version bumps, temperature adjustments, tool definition changes — alongside continuous metric streams — error rates, latency percentiles, quality scores, cost per session. These two streams live in separate systems (config store vs. metrics database) and are never automatically joined.

Without correlation analysis, teams discover regressions through user complaints or manual Grafana inspection. Automating this join — annotating the metric time series with config change events and computing before/after statistics — turns config changes into automatically evaluated experiments, enabling fast detection of regressions and confident attribution of improvements.

---

## Solution 1: Config Change Event Store

Record every configuration change as a timestamped event that can be joined with metric time series.

```python
import time
import uuid
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class ConfigChangeEvent:
    event_id: str
    timestamp: float
    config_key: str
    old_value: Any
    new_value: Any
    author: str
    change_type: str  # "prompt", "model", "parameter", "tool", "other"
    description: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def value_diff_hash(self) -> str:
        payload = json.dumps({"old": str(self.old_value), "new": str(self.new_value)}, sort_keys=True)
        return hashlib.md5(payload.encode()).hexdigest()[:8]

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "config_key": self.config_key,
            "old_value": str(self.old_value)[:200],
            "new_value": str(self.new_value)[:200],
            "author": self.author,
            "change_type": self.change_type,
            "description": self.description,
            "tags": self.tags,
        }


class ConfigChangeEventStore:
    """
    Records and queries configuration change events.
    Designed for joining with metric time series.
    """

    def __init__(self):
        self._events: list[ConfigChangeEvent] = []

    def record(
        self,
        config_key: str,
        old_value: Any,
        new_value: Any,
        author: str = "system",
        change_type: str = "other",
        description: str = "",
        tags: list[str] | None = None,
    ) -> ConfigChangeEvent:
        event = ConfigChangeEvent(
            event_id=str(uuid.uuid4()),
            timestamp=time.time(),
            config_key=config_key,
            old_value=old_value,
            new_value=new_value,
            author=author,
            change_type=change_type,
            description=description,
            tags=tags or [],
        )
        self._events.append(event)
        return event

    def get_in_range(self, start_ts: float, end_ts: float) -> list[ConfigChangeEvent]:
        return [e for e in self._events if start_ts <= e.timestamp <= end_ts]

    def get_for_key(self, config_key: str, limit: int = 50) -> list[ConfigChangeEvent]:
        return [e for e in self._events if e.config_key == config_key][-limit:]

    def get_recent(self, hours: float = 24.0) -> list[ConfigChangeEvent]:
        cutoff = time.time() - hours * 3600
        return [e for e in self._events if e.timestamp >= cutoff]

    def get_by_type(self, change_type: str) -> list[ConfigChangeEvent]:
        return [e for e in self._events if e.change_type == change_type]
```

---

## Solution 2: Metric Time Series Store

A lightweight time-series store for agent metrics, designed to be queryable in before/after windows relative to config events.

```python
from dataclasses import dataclass

@dataclass
class MetricPoint:
    metric_name: str
    value: float
    timestamp: float
    labels: dict = field(default_factory=dict)

class MetricTimeSeriesStore:
    """Simple in-memory metric store for correlation analysis."""

    def __init__(self):
        self._series: dict[str, list[MetricPoint]] = {}

    def record(self, metric_name: str, value: float, labels: dict | None = None) -> None:
        pt = MetricPoint(
            metric_name=metric_name,
            value=value,
            timestamp=time.time(),
            labels=labels or {},
        )
        self._series.setdefault(metric_name, []).append(pt)

    def query(
        self,
        metric_name: str,
        start_ts: float,
        end_ts: float,
        label_filters: dict | None = None,
    ) -> list[MetricPoint]:
        points = [
            p for p in self._series.get(metric_name, [])
            if start_ts <= p.timestamp <= end_ts
        ]
        if label_filters:
            points = [p for p in points if all(p.labels.get(k) == v for k, v in label_filters.items())]
        return sorted(points, key=lambda p: p.timestamp)

    def get_values(
        self, metric_name: str, start_ts: float, end_ts: float
    ) -> list[float]:
        return [p.value for p in self.query(metric_name, start_ts, end_ts)]

    def available_metrics(self) -> list[str]:
        return list(self._series.keys())
```

---

## Solution 3: Before/After Change Analyzer

For each config change event, compute statistical before/after metric windows and detect significant shifts.

```python
import math
import statistics
from dataclasses import dataclass

@dataclass
class ChangeImpactResult:
    event_id: str
    config_key: str
    metric_name: str
    change_timestamp: float
    before_mean: float
    after_mean: float
    before_std: float
    after_std: float
    before_n: int
    after_n: int
    relative_change: float      # (after - before) / before
    p_value: float              # Welch's t-test p-value
    is_significant: bool        # p_value < significance_level
    direction: str              # "improved", "degraded", "unchanged"
    higher_is_better: bool


class BeforeAfterAnalyzer:
    """
    For each config change event, queries metric windows before and after,
    runs statistical significance tests, and classifies the impact.
    """

    def __init__(
        self,
        metric_store: MetricTimeSeriesStore,
        window_seconds: float = 3600.0,  # 1 hour before/after
        min_samples: int = 10,
        significance_level: float = 0.05,
    ):
        self._metrics = metric_store
        self._window = window_seconds
        self._min_samples = min_samples
        self._alpha = significance_level

    def _welch_t_test(
        self, before: list[float], after: list[float]
    ) -> float:
        """Welch's t-test p-value (two-sample, unequal variance). Returns p-value."""
        if len(before) < 2 or len(after) < 2:
            return 1.0

        n1, n2 = len(before), len(after)
        m1, m2 = statistics.mean(before), statistics.mean(after)
        v1 = statistics.variance(before) if n1 > 1 else 0.0
        v2 = statistics.variance(after) if n2 > 1 else 0.0

        if v1 == 0 and v2 == 0:
            return 0.0 if m1 != m2 else 1.0

        se = math.sqrt(v1 / n1 + v2 / n2)
        if se == 0:
            return 1.0

        t = (m1 - m2) / se
        # Welch-Satterthwaite degrees of freedom
        if v1 / n1 + v2 / n2 == 0:
            return 1.0
        df_num = (v1 / n1 + v2 / n2) ** 2
        df_den = (v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1) if n1 > 1 and n2 > 1 else 1
        df = df_num / df_den if df_den > 0 else 1.0

        # Approximate p-value using normal distribution for large df
        z = abs(t)
        p_value = 2 * (1 - self._normal_cdf(z))
        return p_value

    @staticmethod
    def _normal_cdf(z: float) -> float:
        """Approximate standard normal CDF."""
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))

    def analyze_event(
        self,
        event: ConfigChangeEvent,
        metric_name: str,
        higher_is_better: bool = True,
    ) -> Optional[ChangeImpactResult]:
        """Compute before/after statistics for a single config change event."""
        before_values = self._metrics.get_values(
            metric_name,
            start_ts=event.timestamp - self._window,
            end_ts=event.timestamp,
        )
        after_values = self._metrics.get_values(
            metric_name,
            start_ts=event.timestamp,
            end_ts=event.timestamp + self._window,
        )

        if len(before_values) < self._min_samples or len(after_values) < self._min_samples:
            return None

        before_mean = statistics.mean(before_values)
        after_mean = statistics.mean(after_values)
        before_std = statistics.stdev(before_values) if len(before_values) > 1 else 0.0
        after_std = statistics.stdev(after_values) if len(after_values) > 1 else 0.0
        relative_change = (after_mean - before_mean) / before_mean if before_mean != 0 else 0.0
        p_value = self._welch_t_test(before_values, after_values)
        is_significant = p_value < self._alpha

        if not is_significant or abs(relative_change) < 0.01:
            direction = "unchanged"
        elif (relative_change > 0) == higher_is_better:
            direction = "improved"
        else:
            direction = "degraded"

        return ChangeImpactResult(
            event_id=event.event_id,
            config_key=event.config_key,
            metric_name=metric_name,
            change_timestamp=event.timestamp,
            before_mean=round(before_mean, 4),
            after_mean=round(after_mean, 4),
            before_std=round(before_std, 4),
            after_std=round(after_std, 4),
            before_n=len(before_values),
            after_n=len(after_values),
            relative_change=round(relative_change, 4),
            p_value=round(p_value, 4),
            is_significant=is_significant,
            direction=direction,
            higher_is_better=higher_is_better,
        )

    def analyze_all(
        self,
        events: list[ConfigChangeEvent],
        metric_configs: dict[str, bool],  # {metric_name: higher_is_better}
    ) -> list[ChangeImpactResult]:
        """Analyze all config change events against all metrics."""
        results = []
        for event in events:
            for metric_name, higher_is_better in metric_configs.items():
                result = self.analyze_event(event, metric_name, higher_is_better)
                if result:
                    results.append(result)
        return results
```

---

## Solution 4: Change Impact Dashboard

Aggregate analysis results into a ranked dashboard showing the most impactful config changes.

```python
from dataclasses import dataclass
from collections import defaultdict

@dataclass
class ConfigChangeImpactSummary:
    event: ConfigChangeEvent
    impacts: list[ChangeImpactResult]
    degraded_metrics: list[str]
    improved_metrics: list[str]
    unchanged_metrics: list[str]
    max_degradation: float   # Worst relative change (negative = degradation)
    risk_score: float        # 0-1; higher = more likely a regression

class ChangeImpactDashboard:
    """
    Aggregates impact results into summaries ranked by regression risk.
    """

    def __init__(self, analyzer: BeforeAfterAnalyzer):
        self._analyzer = analyzer

    def build_report(
        self,
        events: list[ConfigChangeEvent],
        metric_configs: dict[str, bool],
    ) -> list[ConfigChangeImpactSummary]:
        all_results = self._analyzer.analyze_all(events, metric_configs)

        # Group by event
        by_event: dict[str, list[ChangeImpactResult]] = defaultdict(list)
        for r in all_results:
            by_event[r.event_id].append(r)

        event_map = {e.event_id: e for e in events}
        summaries = []

        for event_id, impacts in by_event.items():
            event = event_map[event_id]
            degraded = [i.metric_name for i in impacts if i.direction == "degraded"]
            improved = [i.metric_name for i in impacts if i.direction == "improved"]
            unchanged = [i.metric_name for i in impacts if i.direction == "unchanged"]

            # Risk score: weighted by significance and magnitude of degradation
            risk = 0.0
            max_deg = 0.0
            for i in impacts:
                if i.direction == "degraded" and i.is_significant:
                    severity = abs(i.relative_change) * (1 - i.p_value)
                    risk = max(risk, severity)
                    max_deg = min(max_deg, i.relative_change)  # most negative

            summaries.append(ConfigChangeImpactSummary(
                event=event,
                impacts=impacts,
                degraded_metrics=degraded,
                improved_metrics=improved,
                unchanged_metrics=unchanged,
                max_degradation=max_deg,
                risk_score=min(1.0, risk),
            ))

        return sorted(summaries, key=lambda s: -s.risk_score)

    def format_text(self, summaries: list[ConfigChangeImpactSummary]) -> str:
        lines = ["CONFIG CHANGE IMPACT REPORT", "=" * 50]
        for s in summaries:
            risk_label = "CRITICAL" if s.risk_score > 0.5 else ("WARNING" if s.risk_score > 0.2 else "OK")
            lines.append(
                f"\n[{risk_label}] {s.event.config_key} ({s.event.change_type}) "
                f"by {s.event.author}"
            )
            lines.append(f"  Time: {time.strftime('%Y-%m-%d %H:%M', time.localtime(s.event.timestamp))}")
            if s.degraded_metrics:
                lines.append(f"  Degraded: {', '.join(s.degraded_metrics)}")
            if s.improved_metrics:
                lines.append(f"  Improved: {', '.join(s.improved_metrics)}")
            for impact in s.impacts:
                if impact.is_significant:
                    lines.append(
                        f"    {impact.metric_name}: {impact.before_mean:.3f} -> {impact.after_mean:.3f} "
                        f"({impact.relative_change:+.1%}, p={impact.p_value:.3f}) [{impact.direction}]"
                    )
        return "\n".join(lines)
```

---

## Solution 5: Automatic Regression Alerter

Watch for newly recorded config changes and alert when associated metrics degrade beyond a threshold.

```python
import asyncio
import logging
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

class ConfigRegressionAlerter:
    """
    Monitors config changes and fires alerts when they correlate with metric regressions.
    Runs as a background task; fires alerts after sufficient post-change data is collected.
    """

    def __init__(
        self,
        event_store: ConfigChangeEventStore,
        analyzer: BeforeAfterAnalyzer,
        metric_configs: dict[str, bool],
        alert_fn: Callable[[ConfigChangeEvent, list[ChangeImpactResult]], Awaitable[None]],
        check_delay: float = 3600.0,   # Wait 1h for data to accumulate
        check_interval: float = 300.0,
        degradation_threshold: float = 0.05,
    ):
        self._events = event_store
        self._analyzer = analyzer
        self._metric_configs = metric_configs
        self._alert_fn = alert_fn
        self._check_delay = check_delay
        self._check_interval = check_interval
        self._degradation_threshold = degradation_threshold
        self._alerted: set[str] = set()  # event_ids already alerted
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            await self._check_recent_changes()

    async def _check_recent_changes(self) -> None:
        now = time.time()
        # Check events that happened check_delay ago (enough data collected)
        window_start = now - self._check_delay - self._check_interval
        window_end = now - self._check_delay
        events = self._events.get_in_range(window_start, window_end)

        for event in events:
            if event.event_id in self._alerted:
                continue

            regressions = []
            for metric_name, higher_is_better in self._metric_configs.items():
                result = self._analyzer.analyze_event(event, metric_name, higher_is_better)
                if (
                    result
                    and result.direction == "degraded"
                    and result.is_significant
                    and abs(result.relative_change) >= self._degradation_threshold
                ):
                    regressions.append(result)

            self._alerted.add(event.event_id)

            if regressions:
                logger.warning(
                    "Config regression detected: %s changed '%s' -> %d metrics degraded",
                    event.author, event.config_key, len(regressions),
                )
                await self._alert_fn(event, regressions)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
```

---

## Solution 6: Multi-Window Trend Analyzer

Detect gradual metric drift following config changes using multiple time windows.

```python
from dataclasses import dataclass

@dataclass
class TrendWindow:
    label: str
    seconds: float

class MultiWindowTrendAnalyzer:
    """
    Analyzes metric trends across multiple time windows after a config change.
    Detects immediate impacts vs. gradual drift vs. recovery patterns.
    """

    WINDOWS = [
        TrendWindow("5min",   5 * 60),
        TrendWindow("30min",  30 * 60),
        TrendWindow("1hr",    3600),
        TrendWindow("6hr",    6 * 3600),
        TrendWindow("24hr",   24 * 3600),
    ]

    def __init__(self, metric_store: MetricTimeSeriesStore, baseline_window: float = 3600.0):
        self._metrics = metric_store
        self._baseline_window = baseline_window

    def analyze_trend(
        self,
        event: ConfigChangeEvent,
        metric_name: str,
    ) -> dict[str, Optional[float]]:
        """
        Returns relative change vs. baseline for each time window after the event.
        {window_label: relative_change} — None if insufficient data.
        """
        baseline = self._metrics.get_values(
            metric_name,
            start_ts=event.timestamp - self._baseline_window,
            end_ts=event.timestamp,
        )
        if len(baseline) < 5:
            return {w.label: None for w in self.WINDOWS}

        baseline_mean = statistics.mean(baseline)
        results = {}
        for window in self.WINDOWS:
            after = self._metrics.get_values(
                metric_name,
                start_ts=event.timestamp,
                end_ts=event.timestamp + window.seconds,
            )
            if len(after) < 3:
                results[window.label] = None
            else:
                after_mean = statistics.mean(after)
                results[window.label] = round(
                    (after_mean - baseline_mean) / baseline_mean if baseline_mean != 0 else 0.0,
                    4,
                )
        return results

    def detect_recovery(self, trend: dict[str, Optional[float]]) -> str:
        """
        Classify the trend pattern:
        - "immediate_degradation": degraded in 5min window
        - "delayed_degradation": fine at 5min, degraded at 1hr+
        - "recovery": degraded early, recovered later
        - "stable": no significant change
        - "improvement": consistent improvement
        """
        values = [(k, v) for k, v in trend.items() if v is not None]
        if not values:
            return "insufficient_data"

        threshold = 0.03
        early = [v for k, v in values[:2] if v is not None]
        late  = [v for k, v in values[-2:] if v is not None]

        avg_early = statistics.mean(early) if early else 0.0
        avg_late  = statistics.mean(late)  if late  else 0.0

        if avg_early < -threshold and avg_late >= -threshold / 2:
            return "recovery"
        if avg_early < -threshold:
            return "immediate_degradation"
        if avg_late < -threshold:
            return "delayed_degradation"
        if avg_early > threshold and avg_late > threshold:
            return "improvement"
        return "stable"
```

---

## Comparison

| Solution | Analysis Type | Statistical Rigor | Automation | Best For |
|---|---|---|---|---|
| Config Change Event Store | Record keeping | None | Passive | Foundation for all analysis |
| Metric Time Series Store | Storage | None | Passive | Metric data source |
| Before/After Analyzer | Hypothesis testing | Welch's t-test | On-demand | Attributing changes to events |
| Change Impact Dashboard | Aggregation + ranking | Inherits from analyzer | On-demand | Post-mortem reviews |
| Regression Alerter | Continuous monitoring | Inherits from analyzer | Automatic | Real-time regression detection |
| Multi-Window Trend Analyzer | Temporal pattern analysis | None | On-demand | Gradual drift detection |

**Start by instrumenting config changes into the event store** — this is the enabler for everything else. **Deploy the regression alerter** as an automated safety net that fires within 1 hour of any significant degradation. **Use the before/after analyzer** in post-deploy review workflows to validate that changes had the intended effect. **Add multi-window trend analysis** for changes that may cause gradual drift (e.g., a new prompt that subtly increases token usage over time). Always record both `change_type` and `author` on config events to quickly identify who made a change and what category it falls into.
