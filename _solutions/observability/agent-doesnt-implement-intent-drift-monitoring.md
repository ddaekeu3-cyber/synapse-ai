---
title: "Agent Doesn't Implement Intent Drift Monitoring"
description: "AI agents trained or prompted on a historical distribution of user intents become misaligned when the actual intent distribution shifts—users start asking different kinds of questions, new intent categories emerge, or existing categories change in phrasing. Intent drift monitoring tracks the distribution of classified intents over time, alerts when significant distributional shifts occur, and identifies emerging intent categories that the agent was not designed to handle."
date: 2025-02-23
difficulty: advanced
category: observability
slug: agent-doesnt-implement-intent-drift-monitoring
tags:
  - intent-drift
  - distribution-shift
  - monitoring
  - intent-classification
  - observability
  - data-drift
  - alerting
symptoms:
  - "New type of user request has been silently failing for weeks because no intent category covers it"
  - "Agent accuracy degraded after a product launch but no alert fired — the new feature attracted different users"
  - "Intent distribution shifted from 60% 'lookup' to 60% 'generate' over two months with no detection"
  - "Emerging intent category 'complaint' appearing in 15% of requests but routed to the wrong handler"
  - "Model fine-tuned on old intent distribution underperforms on current user requests"
---

## Problem

An agent's behavior is optimized for the intent distribution observed at training or prompt-design time. When users shift their patterns—after a product launch, marketing campaign, or seasonal change—the distribution of incoming intents drifts. A "search" heavy system gets flooded with "generate" requests it handles poorly. A new product feature attracts users asking about capabilities the agent was not designed for. Without distribution monitoring, this drift is invisible until it surfaces as user complaints or quality regression in A/B tests. Intent drift monitoring classifies each request's intent, maintains a rolling distribution, compares it to a reference baseline using statistical tests, and alerts when drift exceeds a threshold.

---

## Solution 1: IntentDistributionTracker — Rolling Window Distribution

```python
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class IntentObservation:
    intent: str
    confidence: float
    ts: float
    session_id: str = ""


class IntentDistributionTracker:
    """
    Maintains a rolling window of classified intents and computes
    per-window distribution statistics. Supports comparison of the
    current window against a reference (baseline) distribution.

    Usage:
        tracker = IntentDistributionTracker(window_size=1000, reference_window=5000)
        tracker.record("search", confidence=0.92, session_id="sess-001")
        tracker.record("generate", confidence=0.87, session_id="sess-002")
        drift = tracker.distribution_delta()
        print(drift)  # {"search": -0.12, "generate": +0.15, ...}
    """

    def __init__(self, window_size: int = 1000, reference_window: int = 5000):
        self._window_size = window_size
        self._ref_window = reference_window
        self._current: Deque[IntentObservation] = deque(maxlen=window_size)
        self._reference: Deque[IntentObservation] = deque(maxlen=reference_window)
        self._baseline_frozen: Optional[Dict[str, float]] = None

    def record(self, intent: str, confidence: float = 1.0, session_id: str = ""):
        obs = IntentObservation(intent=intent, confidence=confidence,
                                 ts=time.time(), session_id=session_id)
        self._current.append(obs)
        self._reference.append(obs)

    def _distribution(self, window: deque) -> Dict[str, float]:
        if not window:
            return {}
        counts = Counter(obs.intent for obs in window)
        total = sum(counts.values())
        return {intent: round(count / total, 4) for intent, count in counts.items()}

    def current_distribution(self) -> Dict[str, float]:
        return self._distribution(self._current)

    def reference_distribution(self) -> Dict[str, float]:
        if self._baseline_frozen:
            return self._baseline_frozen
        return self._distribution(self._reference)

    def freeze_baseline(self):
        """Lock the current reference distribution as the permanent baseline."""
        self._baseline_frozen = self._distribution(self._reference)
        logger.info("intent_baseline_frozen intents=%d total=%d",
                     len(self._baseline_frozen), len(self._reference))

    def distribution_delta(self) -> Dict[str, float]:
        """Per-intent delta: current_freq - reference_freq."""
        current = self.current_distribution()
        reference = self.reference_distribution()
        all_intents = set(current) | set(reference)
        return {
            intent: round(current.get(intent, 0.0) - reference.get(intent, 0.0), 4)
            for intent in sorted(all_intents)
        }

    def emerging_intents(self, threshold: float = 0.02) -> List[str]:
        """Intents that appear in current but not in reference (or with very low ref freq)."""
        current = self.current_distribution()
        reference = self.reference_distribution()
        return [
            intent for intent, freq in current.items()
            if freq >= threshold and reference.get(intent, 0.0) < threshold / 2
        ]

    def vanishing_intents(self, threshold: float = 0.02) -> List[str]:
        """Intents that were common in reference but rare in current."""
        current = self.current_distribution()
        reference = self.reference_distribution()
        return [
            intent for intent, freq in reference.items()
            if freq >= threshold and current.get(intent, 0.0) < threshold / 2
        ]

    @property
    def current_window_size(self) -> int:
        return len(self._current)
```

---

## Solution 2: DriftStatisticalTest — Chi-Squared and PSI Drift Detection

```python
import logging
import math
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class DriftStatisticalTest:
    """
    Applies statistical tests to detect significant distributional drift
    between current and reference intent distributions.

    Tests:
    - Chi-squared goodness-of-fit: tests if current distribution matches reference
    - Population Stability Index (PSI): industry standard for distribution shift
      PSI < 0.1: no drift; 0.1-0.25: moderate drift; > 0.25: significant drift

    Usage:
        tester = DriftStatisticalTest()
        psi = tester.psi(current_dist, reference_dist)
        chi2_result = tester.chi_squared(current_dist, reference_dist, total_n=500)
        if psi > 0.1:
            alert(f"Intent drift detected: PSI={psi:.3f}")
    """

    PSI_THRESHOLD_MODERATE = 0.1
    PSI_THRESHOLD_SIGNIFICANT = 0.25

    def psi(self, current: Dict[str, float], reference: Dict[str, float],
             epsilon: float = 1e-4) -> float:
        """
        Population Stability Index.
        PSI = sum((current_i - reference_i) * ln(current_i / reference_i))
        """
        all_intents = set(current) | set(reference)
        psi_score = 0.0
        for intent in all_intents:
            c = max(current.get(intent, 0.0), epsilon)
            r = max(reference.get(intent, 0.0), epsilon)
            psi_score += (c - r) * math.log(c / r)
        return round(psi_score, 6)

    def chi_squared(
        self, current: Dict[str, float], reference: Dict[str, float],
        total_n: int, significance: float = 0.05
    ) -> Dict:
        """
        Chi-squared goodness-of-fit test.
        Returns: {statistic, p_value_approx, significant}
        """
        all_intents = sorted(set(current) | set(reference))
        chi2 = 0.0
        degrees_of_freedom = max(len(all_intents) - 1, 1)

        for intent in all_intents:
            observed = current.get(intent, 0.0) * total_n
            expected = max(reference.get(intent, 1e-6) * total_n, 0.5)
            chi2 += (observed - expected) ** 2 / expected

        # Approximate p-value using chi-squared CDF approximation
        p_approx = self._chi2_p_approx(chi2, degrees_of_freedom)

        return {
            "chi2_statistic": round(chi2, 4),
            "degrees_of_freedom": degrees_of_freedom,
            "p_value_approx": round(p_approx, 4),
            "significant": p_approx < significance,
            "significance_level": significance,
        }

    def _chi2_p_approx(self, x: float, df: int) -> float:
        """Wilson-Hilferty approximation for chi-squared p-value."""
        if x <= 0 or df <= 0:
            return 1.0
        z = ((x / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
        # Standard normal survival function approximation
        return max(0.0, min(1.0, 0.5 * math.erfc(z / math.sqrt(2))))

    def psi_severity(self, psi: float) -> str:
        if psi < self.PSI_THRESHOLD_MODERATE:
            return "stable"
        if psi < self.PSI_THRESHOLD_SIGNIFICANT:
            return "moderate_drift"
        return "significant_drift"

    def full_report(
        self, current: Dict[str, float], reference: Dict[str, float], total_n: int
    ) -> Dict:
        psi = self.psi(current, reference)
        chi2 = self.chi_squared(current, reference, total_n)
        return {
            "psi": psi,
            "psi_severity": self.psi_severity(psi),
            "chi_squared": chi2,
            "drift_detected": psi >= self.PSI_THRESHOLD_MODERATE or chi2["significant"],
        }
```

---

## Solution 3: IntentDriftAlerter — Threshold-Based Alert Dispatch

```python
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DriftAlert:
    severity: str
    psi: float
    emerging_intents: List[str]
    vanishing_intents: List[str]
    top_deltas: List[Dict]
    current_window_size: int
    ts: float

    def __str__(self):
        return (
            f"DriftAlert(severity={self.severity}, psi={self.psi:.3f}, "
            f"emerging={self.emerging_intents}, vanishing={self.vanishing_intents})"
        )


class IntentDriftAlerter:
    """
    Monitors IntentDistributionTracker for distributional drift and
    dispatches alerts when PSI exceeds thresholds or emerging intents
    exceed a frequency threshold. Applies per-severity cooldown to
    prevent alert fatigue.

    Usage:
        alerter = IntentDriftAlerter(
            tracker=intent_tracker,
            notify_fn=send_slack_alert,
            psi_moderate=0.1,
            psi_significant=0.25,
            emerging_threshold=0.05,
        )
        # Run periodically (e.g., every 5 minutes):
        await alerter.check()
    """

    def __init__(
        self,
        tracker: IntentDistributionTracker,
        notify_fn: Optional[Callable] = None,
        psi_moderate: float = 0.1,
        psi_significant: float = 0.25,
        emerging_threshold: float = 0.05,
        min_window: int = 100,
        cooldown_seconds: float = 600.0,
    ):
        self._tracker = tracker
        self._tester = DriftStatisticalTest()
        self._notify = notify_fn
        self._psi_moderate = psi_moderate
        self._psi_significant = psi_significant
        self._emerging_threshold = emerging_threshold
        self._min_window = min_window
        self._cooldown = cooldown_seconds
        self._last_alert: Dict[str, float] = {}
        self._alerts_fired: List[DriftAlert] = []

    def _severity_cooldown(self, severity: str) -> bool:
        last = self._last_alert.get(severity, 0.0)
        return time.time() - last < self._cooldown

    def check(self) -> Optional[DriftAlert]:
        if self._tracker.current_window_size < self._min_window:
            return None

        current = self._tracker.current_distribution()
        reference = self._tracker.reference_distribution()
        if not reference:
            return None

        psi = self._tester.psi(current, reference)
        severity = self._tester.psi_severity(psi)
        emerging = self._tracker.emerging_intents(self._emerging_threshold)
        vanishing = self._tracker.vanishing_intents(self._emerging_threshold)

        if severity == "stable" and not emerging:
            return None

        # Determine alert severity
        if emerging or severity == "significant_drift":
            alert_level = "high"
        elif severity == "moderate_drift":
            alert_level = "medium"
        else:
            alert_level = "low"

        if self._severity_cooldown(alert_level):
            return None

        delta = self._tracker.distribution_delta()
        top_deltas = sorted(
            [{"intent": k, "delta": v} for k, v in delta.items()],
            key=lambda x: abs(x["delta"]), reverse=True,
        )[:5]

        alert = DriftAlert(
            severity=alert_level,
            psi=psi,
            emerging_intents=emerging,
            vanishing_intents=vanishing,
            top_deltas=top_deltas,
            current_window_size=self._tracker.current_window_size,
            ts=time.time(),
        )
        self._alerts_fired.append(alert)
        self._last_alert[alert_level] = time.time()
        logger.warning("intent_drift_alert %s", alert)

        if self._notify:
            try:
                self._notify({"alert": str(alert), "psi": psi,
                               "emerging": emerging, "top_deltas": top_deltas})
            except Exception as exc:
                logger.error("drift_alert_notify_failed error=%s", exc)

        return alert

    @property
    def total_alerts(self) -> int:
        return len(self._alerts_fired)
```

---

## Solution 4: IntentClassifierPipeline — LLM-Based Intent Classification at Scale

```python
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class IntentClassifierPipeline:
    """
    Classifies user messages into intent categories using an LLM and
    feeds classifications into IntentDistributionTracker. Runs async
    classification without blocking the main agent response path.

    Usage:
        pipeline = IntentClassifierPipeline(
            tracker=drift_tracker,
            infer_fn=haiku_client.complete,
            intent_taxonomy=["search", "generate", "explain", "debug", "other"],
        )
        # In the agent request handler (non-blocking):
        asyncio.create_task(pipeline.classify_and_record(
            user_message=message, session_id=sess_id
        ))
    """

    CLASSIFY_PROMPT = (
        "Classify the user's intent into exactly one category.\n"
        "Categories: {categories}\n"
        "User message: {message}\n"
        "Respond with only the category name."
    )

    def __init__(
        self,
        tracker: IntentDistributionTracker,
        infer_fn: Callable,
        intent_taxonomy: List[str],
        model: str = "claude-haiku-4-5-20251001",
        confidence_threshold: float = 0.5,
    ):
        self._tracker = tracker
        self._infer = infer_fn
        self._taxonomy = intent_taxonomy
        self._model = model
        self._confidence = confidence_threshold
        self._classify_errors = 0

    async def classify_and_record(self, user_message: str, session_id: str = "") -> Optional[str]:
        prompt = self.CLASSIFY_PROMPT.format(
            categories=", ".join(self._taxonomy),
            message=user_message[:500],
        )
        try:
            response = await self._infer(
                model=self._model,
                max_tokens=20,
                messages=[{"role": "user", "content": prompt}],
            ) if asyncio.iscoroutinefunction(self._infer) else \
                await asyncio.get_event_loop().run_in_executor(None, lambda: self._infer(
                    model=self._model, max_tokens=20,
                    messages=[{"role": "user", "content": prompt}],
                ))
            raw = str(response).strip().lower()
            # Match against taxonomy
            for intent in self._taxonomy:
                if intent.lower() in raw:
                    self._tracker.record(intent, confidence=0.9, session_id=session_id)
                    return intent
            # Fallback: record as "other"
            self._tracker.record("other", confidence=0.5, session_id=session_id)
            return "other"
        except Exception as exc:
            self._classify_errors += 1
            logger.warning("intent_classify_failed session=%s error=%s", session_id, exc)
            return None

    @property
    def error_count(self) -> int:
        return self._classify_errors
```

---

## Solution 5: IntentDriftReport — Time-Series Dashboard Data

```python
import time
from typing import Any, Dict, List


class IntentDriftReport:
    """
    Generates a time-series report of intent distribution snapshots,
    enabling Grafana or a custom dashboard to plot intent trends over time.
    Snapshots are taken periodically and stored in a ring buffer.

    Usage:
        reporter = IntentDriftReport(tracker=intent_tracker, max_snapshots=288)
        reporter.take_snapshot()  # call every 5 minutes
        data = reporter.time_series_data()
        # Serve at: GET /internal/intent-distribution-history
    """

    def __init__(self, tracker: IntentDistributionTracker, max_snapshots: int = 288):
        self._tracker = tracker
        self._tester = DriftStatisticalTest()
        self._snapshots: List[Dict[str, Any]] = []
        self._max = max_snapshots

    def take_snapshot(self):
        current = self._tracker.current_distribution()
        reference = self._tracker.reference_distribution()
        psi = self._tester.psi(current, reference) if reference else 0.0

        snapshot = {
            "ts": time.time(),
            "distribution": current,
            "window_size": self._tracker.current_window_size,
            "psi": psi,
            "psi_severity": self._tester.psi_severity(psi),
            "emerging": self._tracker.emerging_intents(),
            "vanishing": self._tracker.vanishing_intents(),
        }
        if len(self._snapshots) >= self._max:
            self._snapshots.pop(0)
        self._snapshots.append(snapshot)

    def time_series_data(self) -> List[Dict]:
        return list(self._snapshots)

    def trend_by_intent(self, intent: str) -> List[Dict]:
        return [
            {"ts": s["ts"], "freq": s["distribution"].get(intent, 0.0), "psi": s["psi"]}
            for s in self._snapshots
        ]

    def latest(self) -> Dict:
        return self._snapshots[-1] if self._snapshots else {}
```

---

## Solution 6: IntentDriftMonitorOrchestrator — End-to-End Drift Monitoring

```python
import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class IntentDriftMonitorOrchestrator:
    """
    Orchestrates the full intent drift monitoring pipeline:
    classify incoming messages -> track distribution -> detect drift ->
    snapshot for time-series -> alert on threshold breach.

    Designed to run as a background component alongside the agent service.

    Usage:
        monitor = IntentDriftMonitorOrchestrator(
            infer_fn=haiku_client.complete,
            intent_taxonomy=["search", "generate", "debug", "explain", "other"],
            alert_fn=send_alert,
            snapshot_interval=300,
            check_interval=60,
        )
        await monitor.start()

        # In request handler:
        await monitor.observe(user_message, session_id)
    """

    def __init__(
        self,
        infer_fn: Callable,
        intent_taxonomy: List[str],
        alert_fn: Optional[Callable] = None,
        window_size: int = 1000,
        snapshot_interval: float = 300.0,
        check_interval: float = 60.0,
        baseline_samples: int = 500,
    ):
        self._tracker = IntentDistributionTracker(window_size=window_size)
        self._classifier = IntentClassifierPipeline(
            tracker=self._tracker, infer_fn=infer_fn, intent_taxonomy=intent_taxonomy
        )
        self._alerter = IntentDriftAlerter(
            tracker=self._tracker, notify_fn=alert_fn
        )
        self._reporter = IntentDriftReport(tracker=self._tracker)
        self._snapshot_interval = snapshot_interval
        self._check_interval = check_interval
        self._baseline_samples = baseline_samples
        self._baseline_established = False
        self._observed = 0

    async def observe(self, user_message: str, session_id: str = ""):
        asyncio.create_task(
            self._classifier.classify_and_record(user_message, session_id)
        )
        self._observed += 1
        if not self._baseline_established and self._observed >= self._baseline_samples:
            self._tracker.freeze_baseline()
            self._baseline_established = True
            logger.info("intent_baseline_established samples=%d", self._baseline_samples)

    async def start(self):
        asyncio.create_task(self._snapshot_loop())
        asyncio.create_task(self._check_loop())
        logger.info("intent_drift_monitor_started")

    async def _snapshot_loop(self):
        while True:
            await asyncio.sleep(self._snapshot_interval)
            self._reporter.take_snapshot()

    async def _check_loop(self):
        while True:
            await asyncio.sleep(self._check_interval)
            if self._baseline_established:
                self._alerter.check()

    def status(self) -> Dict[str, Any]:
        latest = self._reporter.latest()
        return {
            "observed_total": self._observed,
            "baseline_established": self._baseline_established,
            "current_distribution": self._tracker.current_distribution(),
            "psi": latest.get("psi", 0.0),
            "psi_severity": latest.get("psi_severity", "unknown"),
            "emerging_intents": self._tracker.emerging_intents(),
            "total_alerts": self._alerter.total_alerts,
        }
```

---

## Comparison

| Approach | Distribution Tracking | Statistical Test | Alert Dispatch | LLM Classification | Time-Series | Integrated |
|---|---|---|---|---|---|---|
| **IntentDistributionTracker** | Yes | No | No | No | No | No |
| **DriftStatisticalTest** | No | PSI + Chi² | No | No | No | No |
| **IntentDriftAlerter** | Via tracker | Via tester | Yes | No | No | No |
| **IntentClassifierPipeline** | Via tracker | No | No | Yes | No | No |
| **IntentDriftReport** | Via tracker | Via tester | No | No | Yes | No |
| **IntentDriftMonitorOrchestrator** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: start by adding `IntentDistributionTracker.record()` after each intent classification in the existing request handler—no infrastructure change needed. After 500 requests, call `freeze_baseline()` to lock the reference distribution. From then on, check `DriftStatisticalTest.psi(current, reference)` every 5 minutes: a PSI above 0.10 warrants investigation, above 0.25 warrants a prompt or routing update. The most actionable signal is `emerging_intents(threshold=0.05)`: if a new intent category is appearing in 5%+ of requests but the agent has no handler for it, you have found a product gap that users are actively experiencing.
