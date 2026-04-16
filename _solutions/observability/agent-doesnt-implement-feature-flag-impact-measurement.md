---
title: "Agent Doesn't Implement Feature Flag Impact Measurement"
description: "Agents that roll out prompt changes, model upgrades, or new tool behaviors via feature flags have no way to measure whether those changes improved or degraded outcomes. Implement feature flag impact measurement to automatically A/B test changes and quantify their effect before full rollout."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-feature-flag-impact-measurement
tags: [feature-flags, ab-testing, experimentation, observability, measurement, rollout]
symptoms:
  - "Feature flags are toggled but no metrics show whether the change was beneficial"
  - "Prompt engineering changes rolled out without comparing success rate before/after"
  - "No statistical test to decide when to graduate a flag from 10% to 100%"
  - "Rollback decisions are made on gut feel rather than measured outcome data"
  - "Multiple flags active simultaneously with no isolation between their effects"
---

## Why This Happens

Feature flags are a deployment mechanism, not a measurement system. Toggling a flag changes behavior but generates no automatic comparison data. Without instrumenting each flag variant with outcome metrics and applying statistical significance tests, teams either roll forward blindly or revert conservatively. Proper flag impact measurement assigns users to stable cohorts, tracks per-cohort metrics, and surfaces statistically significant differences automatically.

## Solution 1: Flag Assignment with Stable Cohort Bucketing

```python
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class FeatureFlag:
    flag_id: str
    variants: List[str]                  # ["control", "treatment"]
    traffic_allocation: Dict[str, float] # {"control": 0.5, "treatment": 0.5}
    enabled: bool = True
    sticky: bool = True  # same user always gets same variant

class FlagAssigner:
    """
    Assigns users to flag variants using deterministic bucketing.
    Sticky=True: hash(user_id + flag_id) determines variant permanently.
    """

    def __init__(self, flags: Dict[str, FeatureFlag]):
        self._flags = flags

    def assign(self, flag_id: str, user_id: str) -> Optional[str]:
        flag = self._flags.get(flag_id)
        if flag is None or not flag.enabled:
            return None

        if flag.sticky:
            bucket = self._bucket(user_id, flag_id)
        else:
            import random
            bucket = random.random()

        cumulative = 0.0
        for variant, allocation in flag.traffic_allocation.items():
            cumulative += allocation
            if bucket < cumulative:
                return variant
        return flag.variants[-1]  # fallback to last variant

    def _bucket(self, user_id: str, flag_id: str) -> float:
        """Returns deterministic float in [0, 1) for user+flag pair."""
        key = f"{flag_id}:{user_id}".encode()
        digest = hashlib.sha256(key).hexdigest()[:8]
        return int(digest, 16) / (16 ** 8)

    def assignments(self, user_id: str) -> Dict[str, str]:
        """Returns all active flag assignments for a user."""
        return {
            flag_id: variant
            for flag_id, flag in self._flags.items()
            if flag.enabled
            for variant in [self.assign(flag_id, user_id)]
            if variant is not None
        }
```

## Solution 2: Outcome Metric Collector Per Flag Variant

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class OutcomeEvent:
    flag_id: str
    variant: str
    user_id: str
    metric_name: str   # success_rate | latency_ms | tokens_used | user_rating
    value: float
    timestamp: float = field(default_factory=time.time)
    session_id: Optional[str] = None

class FlagMetricsCollector:
    """
    Records outcome metrics tagged by flag variant.
    Aggregates per (flag_id, variant, metric_name) for analysis.
    """

    def __init__(self):
        # (flag_id, variant, metric) -> list of values
        self._data: Dict[tuple, List[float]] = defaultdict(list)
        self._event_log: List[OutcomeEvent] = []

    def record(self, event: OutcomeEvent) -> None:
        key = (event.flag_id, event.variant, event.metric_name)
        self._data[key].append(event.value)
        self._event_log.append(event)

    def summary(self, flag_id: str, metric_name: str) -> Dict[str, dict]:
        """Returns per-variant stats for a given flag and metric."""
        import statistics
        result = {}
        for variant in self._variants_for(flag_id):
            values = self._data.get((flag_id, variant, metric_name), [])
            if not values:
                continue
            result[variant] = {
                "n": len(values),
                "mean": statistics.mean(values),
                "median": statistics.median(values),
                "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
                "p95": sorted(values)[int(len(values) * 0.95)],
            }
        return result

    def _variants_for(self, flag_id: str) -> List[str]:
        return list({k[1] for k in self._data if k[0] == flag_id})

    def export_events(self, flag_id: Optional[str] = None) -> List[OutcomeEvent]:
        if flag_id is None:
            return list(self._event_log)
        return [e for e in self._event_log if e.flag_id == flag_id]
```

## Solution 3: Statistical Significance Test for Flag Graduation

```python
import math
from typing import Dict, List, Optional, Tuple

class FlagSignificanceTester:
    """
    Tests whether the treatment variant is significantly better than control.
    Uses two-sample z-test for proportions (success rate) and
    Welch's t-test for continuous metrics (latency, token count).
    """

    def __init__(self, alpha: float = 0.05, min_sample_size: int = 100):
        self._alpha = alpha
        self._min_n = min_sample_size

    def test_proportion(
        self,
        control_successes: int, control_n: int,
        treatment_successes: int, treatment_n: int,
    ) -> dict:
        """Z-test for difference in proportions (e.g., task success rate)."""
        if control_n < self._min_n or treatment_n < self._min_n:
            return {"significant": False, "reason": "insufficient_sample_size",
                    "control_n": control_n, "treatment_n": treatment_n}

        p1 = control_successes / control_n
        p2 = treatment_successes / treatment_n
        p_pool = (control_successes + treatment_successes) / (control_n + treatment_n)
        se = math.sqrt(p_pool * (1 - p_pool) * (1/control_n + 1/treatment_n))
        if se == 0:
            return {"significant": False, "reason": "zero_variance"}
        z = (p2 - p1) / se
        p_value = 2 * (1 - self._normal_cdf(abs(z)))
        lift = (p2 - p1) / max(p1, 1e-9)
        return {
            "significant": p_value < self._alpha,
            "p_value": round(p_value, 4),
            "z_statistic": round(z, 3),
            "lift": round(lift, 4),
            "control_rate": round(p1, 4),
            "treatment_rate": round(p2, 4),
            "recommended_action": "graduate" if (p_value < self._alpha and lift > 0) else
                                   "rollback" if (p_value < self._alpha and lift < 0) else "continue_test",
        }

    def test_continuous(
        self, control_values: List[float], treatment_values: List[float]
    ) -> dict:
        """Welch's t-test for continuous metrics (latency, token usage)."""
        import statistics
        if len(control_values) < self._min_n or len(treatment_values) < self._min_n:
            return {"significant": False, "reason": "insufficient_sample_size"}

        m1, m2 = statistics.mean(control_values), statistics.mean(treatment_values)
        s1, s2 = statistics.stdev(control_values), statistics.stdev(treatment_values)
        n1, n2 = len(control_values), len(treatment_values)

        se = math.sqrt(s1**2/n1 + s2**2/n2)
        if se == 0:
            return {"significant": False, "reason": "zero_variance"}
        t = (m2 - m1) / se
        # Welch–Satterthwaite degrees of freedom
        df = (s1**2/n1 + s2**2/n2)**2 / (
            (s1**2/n1)**2/(n1-1) + (s2**2/n2)**2/(n2-1)
        )
        p_value = 2 * self._t_cdf_approx(abs(t), df)
        lift = (m2 - m1) / max(abs(m1), 1e-9)
        return {
            "significant": p_value < self._alpha,
            "p_value": round(p_value, 4),
            "t_statistic": round(t, 3),
            "degrees_of_freedom": round(df, 1),
            "lift": round(lift, 4),
            "control_mean": round(m1, 4),
            "treatment_mean": round(m2, 4),
            "recommended_action": "graduate" if (p_value < self._alpha and lift < 0) else  # lower=better for latency
                                   "rollback" if (p_value < self._alpha and lift > 0) else "continue_test",
        }

    def _normal_cdf(self, z: float) -> float:
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))

    def _t_cdf_approx(self, t: float, df: float) -> float:
        """Rough approximation using normal CDF for large df."""
        if df > 30:
            return 1 - self._normal_cdf(t)
        x = df / (df + t * t)
        # Regularized incomplete beta approximation (very rough)
        return 0.5 * x ** (df / 2)
```

## Solution 4: Flag Lifecycle Manager (Test → Graduate → Archive)

```python
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

class FlagState(Enum):
    TESTING = "testing"        # A/B test in progress
    GRADUATING = "graduating"  # ramp-up to 100%
    GRADUATED = "graduated"    # fully rolled out
    ROLLED_BACK = "rolled_back"
    ARCHIVED = "archived"

@dataclass
class FlagLifecycle:
    flag_id: str
    state: FlagState
    treatment_allocation: float    # current % in treatment
    created_at: float
    last_transition_at: float
    graduation_target: float = 1.0  # ramp to 100% by default
    ramp_step: float = 0.1          # 10% increment per ramp step

class FlagLifecycleManager:
    def __init__(
        self,
        assigner: FlagAssigner,
        tester: FlagSignificanceTester,
        collector: FlagMetricsCollector,
        flag_store,
    ):
        self._assigner = assigner
        self._tester = tester
        self._collector = collector
        self._store = flag_store

    async def evaluate_flag(self, flag_id: str, primary_metric: str) -> dict:
        lifecycle = await self._store.get(flag_id)
        if lifecycle is None or lifecycle.state != FlagState.TESTING:
            return {"action": "noop", "reason": "not_in_testing"}

        summary = self._collector.summary(flag_id, primary_metric)
        control = summary.get("control", {})
        treatment = summary.get("treatment", {})

        if not control or not treatment:
            return {"action": "noop", "reason": "insufficient_data"}

        # Assume success rate metric (proportion test)
        n_c = control["n"]
        n_t = treatment["n"]
        # Reconstruct success counts from mean (assumes binary metric 0/1)
        successes_c = int(control["mean"] * n_c)
        successes_t = int(treatment["mean"] * n_t)
        result = self._tester.test_proportion(successes_c, n_c, successes_t, n_t)

        if result["recommended_action"] == "graduate":
            await self._ramp_up(flag_id, lifecycle)
            return {"action": "ramping_up", "test_result": result}
        elif result["recommended_action"] == "rollback":
            await self._rollback(flag_id, lifecycle)
            return {"action": "rolled_back", "test_result": result}
        return {"action": "continue_test", "test_result": result}

    async def _ramp_up(self, flag_id: str, lifecycle: FlagLifecycle) -> None:
        new_allocation = min(
            lifecycle.treatment_allocation + lifecycle.ramp_step,
            lifecycle.graduation_target,
        )
        flag = self._assigner._flags[flag_id]
        flag.traffic_allocation["treatment"] = new_allocation
        flag.traffic_allocation["control"] = 1.0 - new_allocation
        lifecycle.treatment_allocation = new_allocation
        if new_allocation >= lifecycle.graduation_target:
            lifecycle.state = FlagState.GRADUATED
        lifecycle.last_transition_at = time.time()
        await self._store.save(lifecycle)

    async def _rollback(self, flag_id: str, lifecycle: FlagLifecycle) -> None:
        flag = self._assigner._flags[flag_id]
        flag.traffic_allocation["treatment"] = 0.0
        flag.traffic_allocation["control"] = 1.0
        lifecycle.state = FlagState.ROLLED_BACK
        lifecycle.last_transition_at = time.time()
        await self._store.save(lifecycle)
```

## Solution 5: Agent Middleware that Auto-Records Flag Outcomes

```python
import asyncio
import time
from typing import Any, Callable, Optional

class FlagOutcomeMiddleware:
    """
    Wraps agent execution and automatically records outcome metrics
    for all active flag assignments on each session.
    """

    def __init__(
        self,
        assigner: FlagAssigner,
        collector: FlagMetricsCollector,
        outcome_extractor: Callable[[Any], dict],
    ):
        self._assigner = assigner
        self._collector = collector
        self._extract = outcome_extractor

    async def run(self, user_id: str, agent_fn: Callable, *args, **kwargs) -> Any:
        assignments = self._assigner.assignments(user_id)
        t0 = time.monotonic()

        try:
            result = await agent_fn(*args, **kwargs)
            elapsed_ms = (time.monotonic() - t0) * 1000
            outcomes = self._extract(result)

            for flag_id, variant in assignments.items():
                # Record latency
                self._collector.record(OutcomeEvent(
                    flag_id=flag_id, variant=variant, user_id=user_id,
                    metric_name="latency_ms", value=elapsed_ms,
                ))
                # Record all extracted outcomes
                for metric_name, value in outcomes.items():
                    self._collector.record(OutcomeEvent(
                        flag_id=flag_id, variant=variant, user_id=user_id,
                        metric_name=metric_name, value=float(value),
                    ))
            return result

        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            for flag_id, variant in assignments.items():
                self._collector.record(OutcomeEvent(
                    flag_id=flag_id, variant=variant, user_id=user_id,
                    metric_name="error_rate", value=1.0,
                ))
                self._collector.record(OutcomeEvent(
                    flag_id=flag_id, variant=variant, user_id=user_id,
                    metric_name="latency_ms", value=elapsed_ms,
                ))
            raise
```

## Solution 6: Flag Impact Dashboard Reporter

```python
import time
from typing import List, Optional

class FlagImpactReporter:
    """Generates per-flag impact reports across all tracked metrics."""

    METRICS = ["latency_ms", "success_rate", "tokens_used", "user_rating", "error_rate"]

    def __init__(
        self,
        collector: FlagMetricsCollector,
        tester: FlagSignificanceTester,
    ):
        self._collector = collector
        self._tester = tester

    def report(self, flag_id: str) -> dict:
        report = {"flag_id": flag_id, "metrics": {}, "generated_at": time.time()}

        for metric in self.METRICS:
            summary = self._collector.summary(flag_id, metric)
            if "control" not in summary or "treatment" not in summary:
                continue

            c = summary["control"]
            t = summary["treatment"]
            if c["n"] < 10 or t["n"] < 10:
                report["metrics"][metric] = {"status": "insufficient_data", **summary}
                continue

            # Use continuous test for all metrics
            c_events = [e.value for e in self._collector.export_events(flag_id)
                        if e.variant == "control" and e.metric_name == metric]
            t_events = [e.value for e in self._collector.export_events(flag_id)
                        if e.variant == "treatment" and e.metric_name == metric]

            test_result = self._tester.test_continuous(c_events, t_events)
            report["metrics"][metric] = {
                "control": c,
                "treatment": t,
                "test": test_result,
            }

        return report

    def print_report(self, flag_id: str) -> None:
        r = self.report(flag_id)
        print(f"\n=== Flag Impact Report: {flag_id} ===")
        for metric, data in r["metrics"].items():
            test = data.get("test", {})
            sig = "SIGNIFICANT" if test.get("significant") else "not significant"
            action = test.get("recommended_action", "N/A")
            lift = test.get("lift", 0)
            print(f"  {metric}: lift={lift:+.1%} p={test.get('p_value','?')} [{sig}] → {action}")
```

## Comparison

| Approach | Variant Assignment | Metric Collection | Statistical Test | Auto-Decision |
|---|---|---|---|---|
| FlagAssigner | Deterministic hash bucketing | No | No | No |
| FlagMetricsCollector | N/A | Per-variant aggregation | No | No |
| FlagSignificanceTester | N/A | N/A | z-test + Welch's t | Recommends action |
| FlagLifecycleManager | Via assigner | Via collector | Via tester | Yes (ramp/rollback) |
| FlagOutcomeMiddleware | Auto-tag all calls | Auto-record | No | No |
| FlagImpactReporter | N/A | Via collector | Via tester | Per-metric report |

**Best for production**: Use `FlagAssigner` for stable bucketing, `FlagOutcomeMiddleware` to auto-record outcomes on every agent call, `FlagSignificanceTester` to evaluate significance, and `FlagLifecycleManager` to automatically ramp or roll back based on test results. Run `FlagImpactReporter` in a scheduled job to surface results in dashboards.
