---
title: "Agent Doesn't Implement Anomaly Detection for Token Usage Spikes"
description: "Agents that don't monitor token consumption per session miss runaway loops, prompt injection attacks that inflate context, and gradual cost drift until the billing alert fires at end of month. Implement statistical anomaly detection on per-session token usage to catch spikes in real time and halt or alert before damage accumulates."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-anomaly-detection-for-token-usage-spikes
tags: [token-usage, anomaly-detection, observability, cost-monitoring, prompt-injection, runaway-agent]
symptoms:
  - "Monthly API bill is 10x expected — no alerts fired during the spike"
  - "One session consumed 2M tokens due to an infinite tool-call loop"
  - "Prompt injection caused the agent to repeat a large document in every reply"
  - "Token usage grows session-over-session with no baseline to compare against"
  - "No per-session token budget enforcement — only global monthly limits"
---

## Why This Happens

Token consumption is a lagging signal when monitored only at billing boundaries. Individual sessions can spike due to runaway loops (tool call triggers another tool call indefinitely), prompt injection payloads that embed large repetitive content, or model behavior drift after a fine-tune. Anomaly detection on per-session and per-turn token counts provides an early warning system: flag sessions that deviate from the historical baseline before they consume thousands of dollars.

## Solution 1: Rolling Z-Score Token Anomaly Detector

```python
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

@dataclass
class TokenUsagePoint:
    session_id: str
    turn: int
    input_tokens: int
    output_tokens: int
    timestamp: float = field(default_factory=time.time)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

@dataclass
class AnomalySignal:
    session_id: str
    turn: int
    observed_tokens: int
    baseline_mean: float
    baseline_std: float
    z_score: float
    severity: str   # "warning" | "critical"
    timestamp: float = field(default_factory=time.time)

class RollingZScoreDetector:
    """
    Maintains a rolling window of per-session token counts.
    Emits AnomalySignal when a turn's token count exceeds Z standard
    deviations above the session's own rolling mean.
    Separate baselines per session prevent cross-session interference.
    """

    def __init__(
        self,
        window_size: int = 20,
        warning_z: float = 2.5,
        critical_z: float = 4.0,
        min_samples: int = 5,
    ):
        self._window = window_size
        self._warn_z = warning_z
        self._crit_z = critical_z
        self._min_samples = min_samples
        # session_id -> deque of total token counts
        self._histories: Dict[str, Deque[int]] = {}

    def _get_history(self, session_id: str) -> Deque[int]:
        if session_id not in self._histories:
            self._histories[session_id] = deque(maxlen=self._window)
        return self._histories[session_id]

    def _stats(self, history: Deque[int]) -> tuple[float, float]:
        n = len(history)
        if n == 0:
            return 0.0, 0.0
        mean = sum(history) / n
        variance = sum((x - mean) ** 2 for x in history) / n
        return mean, math.sqrt(variance)

    def observe(self, point: TokenUsagePoint) -> Optional[AnomalySignal]:
        history = self._get_history(point.session_id)
        mean, std = self._stats(history)

        # Add current observation to history
        history.append(point.total)

        if len(history) < self._min_samples or std < 1.0:
            return None

        z = (point.total - mean) / std

        if z >= self._crit_z:
            severity = "critical"
        elif z >= self._warn_z:
            severity = "warning"
        else:
            return None

        return AnomalySignal(
            session_id=point.session_id,
            turn=point.turn,
            observed_tokens=point.total,
            baseline_mean=round(mean, 1),
            baseline_std=round(std, 1),
            z_score=round(z, 2),
            severity=severity,
        )

    def reset_session(self, session_id: str) -> None:
        self._histories.pop(session_id, None)
```

## Solution 2: Exponential Weighted Moving Average (EWMA) Detector

```python
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

@dataclass
class EWMAState:
    mean: float = 0.0
    variance: float = 0.0
    n: int = 0

class EWMATokenDetector:
    """
    Online EWMA detector — adapts quickly to gradual drift while still
    flagging sudden spikes. Lower alpha = slower adaptation (more memory).
    More suitable than rolling Z-score when session usage legitimately
    grows over time (e.g., longer conversations accumulating context).
    """

    def __init__(self, alpha: float = 0.2, spike_multiplier: float = 3.0, min_samples: int = 3):
        self._alpha = alpha
        self._spike_mult = spike_multiplier
        self._min_samples = min_samples
        self._states: Dict[str, EWMAState] = {}

    def _get_state(self, session_id: str) -> EWMAState:
        if session_id not in self._states:
            self._states[session_id] = EWMAState()
        return self._states[session_id]

    def observe(self, session_id: str, total_tokens: int) -> Optional[dict]:
        s = self._get_state(session_id)

        if s.n == 0:
            s.mean = total_tokens
            s.variance = 0.0
            s.n = 1
            return None

        prev_mean = s.mean
        s.mean = self._alpha * total_tokens + (1 - self._alpha) * s.mean
        diff = total_tokens - prev_mean
        s.variance = self._alpha * diff ** 2 + (1 - self._alpha) * s.variance
        s.n += 1

        if s.n < self._min_samples or s.variance < 1.0:
            return None

        import math
        std = math.sqrt(s.variance)
        threshold = s.mean + self._spike_mult * std

        if total_tokens > threshold:
            return {
                "session_id": session_id,
                "observed": total_tokens,
                "ewma_mean": round(s.mean, 1),
                "ewma_std": round(std, 1),
                "threshold": round(threshold, 1),
                "ratio": round(total_tokens / max(s.mean, 1), 2),
                "severity": "critical" if total_tokens > threshold * 1.5 else "warning",
            }
        return None

    def reset_session(self, session_id: str) -> None:
        self._states.pop(session_id, None)
```

## Solution 3: Per-Session Budget Enforcer

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

@dataclass
class SessionBudget:
    session_id: str
    max_tokens_per_turn: int = 8000
    max_tokens_per_session: int = 200_000
    max_turns: int = 100
    soft_limit_fraction: float = 0.8   # warn at 80%, halt at 100%

@dataclass
class SessionUsage:
    session_id: str
    total_tokens: int = 0
    turn_count: int = 0
    started_at: float = field(default_factory=time.time)
    halted: bool = False
    halt_reason: str = ""

class SessionBudgetEnforcer:
    """
    Tracks cumulative token usage per session and enforces hard stops.
    Supports soft-limit callbacks for warnings before hard halt.
    """

    def __init__(
        self,
        on_soft_limit: Optional[Callable[[SessionUsage, SessionBudget], None]] = None,
        on_hard_limit: Optional[Callable[[SessionUsage, SessionBudget], None]] = None,
    ):
        self._budgets: Dict[str, SessionBudget] = {}
        self._usages: Dict[str, SessionUsage] = {}
        self._on_soft = on_soft_limit
        self._on_hard = on_hard_limit

    def register_session(self, budget: SessionBudget) -> None:
        self._budgets[budget.session_id] = budget
        self._usages[budget.session_id] = SessionUsage(session_id=budget.session_id)

    def record_turn(
        self,
        session_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> dict:
        """Returns {"allow": bool, "reason": str, "usage": SessionUsage}."""
        budget = self._budgets.get(session_id)
        usage = self._usages.get(session_id)

        if not budget or not usage:
            return {"allow": True, "reason": "unregistered_session"}

        if usage.halted:
            return {"allow": False, "reason": usage.halt_reason, "usage": usage}

        turn_total = input_tokens + output_tokens
        usage.turn_count += 1
        usage.total_tokens += turn_total

        # Per-turn hard limit
        if turn_total > budget.max_tokens_per_turn:
            usage.halted = True
            usage.halt_reason = f"turn_limit_exceeded:{turn_total}>{budget.max_tokens_per_turn}"
            if self._on_hard:
                self._on_hard(usage, budget)
            return {"allow": False, "reason": usage.halt_reason, "usage": usage}

        # Turn count limit
        if usage.turn_count > budget.max_turns:
            usage.halted = True
            usage.halt_reason = f"max_turns_exceeded:{usage.turn_count}"
            if self._on_hard:
                self._on_hard(usage, budget)
            return {"allow": False, "reason": usage.halt_reason, "usage": usage}

        # Session total soft limit
        soft_threshold = int(budget.max_tokens_per_session * budget.soft_limit_fraction)
        if usage.total_tokens >= soft_threshold and self._on_soft:
            self._on_soft(usage, budget)

        # Session total hard limit
        if usage.total_tokens >= budget.max_tokens_per_session:
            usage.halted = True
            usage.halt_reason = f"session_limit_exceeded:{usage.total_tokens}"
            if self._on_hard:
                self._on_hard(usage, budget)
            return {"allow": False, "reason": usage.halt_reason, "usage": usage}

        return {"allow": True, "reason": "ok", "usage": usage}

    def get_usage(self, session_id: str) -> Optional[SessionUsage]:
        return self._usages.get(session_id)
```

## Solution 4: Cross-Session Baseline Comparator

```python
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class SessionSummary:
    session_id: str
    total_tokens: int
    turn_count: int
    avg_tokens_per_turn: float
    max_turn_tokens: int
    duration_seconds: float

class CrossSessionBaselineComparator:
    """
    Aggregates completed session stats to build a rolling baseline.
    New sessions are compared against the population distribution.
    Useful for detecting anomalous sessions post-hoc or at session close.
    """

    def __init__(self, baseline_window: int = 500):
        self._window = baseline_window
        self._summaries: List[SessionSummary] = []

    def add_completed_session(self, summary: SessionSummary) -> None:
        self._summaries.append(summary)
        if len(self._summaries) > self._window:
            self._summaries.pop(0)

    def _percentile(self, values: List[float], p: float) -> float:
        if not values:
            return 0.0
        sorted_v = sorted(values)
        idx = int(len(sorted_v) * p / 100)
        return sorted_v[min(idx, len(sorted_v) - 1)]

    def baseline_stats(self) -> dict:
        if not self._summaries:
            return {}
        totals = [s.total_tokens for s in self._summaries]
        avgs = [s.avg_tokens_per_turn for s in self._summaries]
        n = len(totals)
        mean_total = sum(totals) / n
        std_total = math.sqrt(sum((x - mean_total) ** 2 for x in totals) / n)
        return {
            "n_sessions": n,
            "mean_total_tokens": round(mean_total, 1),
            "std_total_tokens": round(std_total, 1),
            "p50_total": self._percentile(totals, 50),
            "p95_total": self._percentile(totals, 95),
            "p99_total": self._percentile(totals, 99),
            "mean_tokens_per_turn": round(sum(avgs) / n, 1),
        }

    def score_session(self, summary: SessionSummary) -> dict:
        stats = self.baseline_stats()
        if not stats or stats["std_total_tokens"] < 1:
            return {"z_score": 0.0, "percentile_rank": 50.0, "anomalous": False}

        z = (summary.total_tokens - stats["mean_total_tokens"]) / stats["std_total_tokens"]
        # Percentile rank: fraction of baseline sessions below this session's total
        rank = sum(1 for s in self._summaries if s.total_tokens < summary.total_tokens)
        pct_rank = round(100 * rank / max(len(self._summaries), 1), 1)

        return {
            "session_id": summary.session_id,
            "total_tokens": summary.total_tokens,
            "z_score": round(z, 2),
            "percentile_rank": pct_rank,
            "anomalous": z > 3.0 or pct_rank > 99.0,
            "baseline_mean": stats["mean_total_tokens"],
            "baseline_p99": stats["p99_total"],
        }
```

## Solution 5: Token Spike Alert Pipeline

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

@dataclass
class TokenSpikeAlert:
    session_id: str
    alert_type: str     # "per_turn_spike" | "session_budget" | "cross_session_outlier"
    severity: str       # "warning" | "critical"
    message: str
    details: dict
    timestamp: float

AlertHandler = Callable[[TokenSpikeAlert], None]

class TokenSpikeAlertPipeline:
    """
    Wires together RollingZScoreDetector, SessionBudgetEnforcer, and
    CrossSessionBaselineComparator into a single call-site interface.
    Deduplicates alerts and routes to registered handlers.
    """

    def __init__(
        self,
        z_detector: "RollingZScoreDetector",
        budget_enforcer: "SessionBudgetEnforcer",
        baseline_comparator: "CrossSessionBaselineComparator",
        dedup_window_seconds: float = 60.0,
    ):
        self._z = z_detector
        self._budget = budget_enforcer
        self._baseline = baseline_comparator
        self._dedup_window = dedup_window_seconds
        self._handlers: List[AlertHandler] = []
        self._last_alert: dict = {}   # session_id+type -> last_alert_time

    def add_handler(self, handler: AlertHandler) -> None:
        self._handlers.append(handler)

    def _should_fire(self, session_id: str, alert_type: str) -> bool:
        key = f"{session_id}:{alert_type}"
        last = self._last_alert.get(key, 0.0)
        if time.time() - last < self._dedup_window:
            return False
        self._last_alert[key] = time.time()
        return True

    def _fire(self, alert: TokenSpikeAlert) -> None:
        for handler in self._handlers:
            try:
                handler(alert)
            except Exception as exc:
                print(f"[alert_pipeline] handler error: {exc}")

    def record_turn(
        self,
        session_id: str,
        turn: int,
        input_tokens: int,
        output_tokens: int,
    ) -> dict:
        point = TokenUsagePoint(
            session_id=session_id,
            turn=turn,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        # Z-score per-turn anomaly
        signal = self._z.observe(point)
        if signal and self._should_fire(session_id, "per_turn_spike"):
            self._fire(TokenSpikeAlert(
                session_id=session_id,
                alert_type="per_turn_spike",
                severity=signal.severity,
                message=f"Turn token spike: {signal.observed_tokens} tokens (z={signal.z_score})",
                details={"z_score": signal.z_score, "mean": signal.baseline_mean, "std": signal.baseline_std},
                timestamp=time.time(),
            ))

        # Budget enforcement
        budget_result = self._budget.record_turn(session_id, input_tokens, output_tokens)
        if not budget_result.get("allow") and self._should_fire(session_id, "session_budget"):
            self._fire(TokenSpikeAlert(
                session_id=session_id,
                alert_type="session_budget",
                severity="critical",
                message=f"Session budget exceeded: {budget_result.get('reason')}",
                details=budget_result,
                timestamp=time.time(),
            ))

        return budget_result
```

## Solution 6: Token Anomaly Dashboard

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List

@dataclass
class AnomalyRecord:
    session_id: str
    alert_type: str
    severity: str
    tokens: int
    z_score: float
    timestamp: float

class TokenAnomalyDashboard:
    """
    Aggregates all fired anomaly signals for operational visibility.
    Tracks alert frequency, top offending sessions, and severity distribution.
    """

    def __init__(self, alert_window_seconds: float = 3600.0):
        self._window = alert_window_seconds
        self._alerts: Deque[AnomalyRecord] = deque()
        self._session_alert_counts: Dict[str, int] = defaultdict(int)
        self._severity_counts: Dict[str, int] = defaultdict(int)

    def record_alert(self, record: AnomalyRecord) -> None:
        self._alerts.append(record)
        self._session_alert_counts[record.session_id] += 1
        self._severity_counts[record.severity] += 1
        self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - self._window
        while self._alerts and self._alerts[0].timestamp < cutoff:
            old = self._alerts.popleft()
            self._session_alert_counts[old.session_id] = max(
                0, self._session_alert_counts[old.session_id] - 1
            )
            self._severity_counts[old.severity] = max(
                0, self._severity_counts[old.severity] - 1
            )

    def top_sessions(self, top_k: int = 10) -> List[dict]:
        return sorted(
            [{"session_id": sid, "alert_count": cnt}
             for sid, cnt in self._session_alert_counts.items() if cnt > 0],
            key=lambda x: x["alert_count"], reverse=True,
        )[:top_k]

    def summary(self) -> dict:
        self._prune()
        return {
            "window_seconds": self._window,
            "total_alerts": len(self._alerts),
            "critical_alerts": self._severity_counts.get("critical", 0),
            "warning_alerts": self._severity_counts.get("warning", 0),
            "unique_sessions_affected": sum(1 for c in self._session_alert_counts.values() if c > 0),
            "top_sessions": self.top_sessions(5),
            "generated_at": time.time(),
        }

    def recent_alerts(self, limit: int = 20) -> List[dict]:
        alerts = list(self._alerts)[-limit:]
        return [
            {
                "session_id": a.session_id,
                "alert_type": a.alert_type,
                "severity": a.severity,
                "tokens": a.tokens,
                "z_score": a.z_score,
                "timestamp": a.timestamp,
            }
            for a in reversed(alerts)
        ]
```

## Comparison

| Approach | Detection Latency | False Positive Risk | Adapts to Growth | Cross-Session |
|---|---|---|---|---|
| RollingZScoreDetector | Per turn | Medium (needs min_samples) | No (static window) | No |
| EWMATokenDetector | Per turn | Low (adapts to drift) | Yes (alpha controls) | No |
| SessionBudgetEnforcer | Per turn | None (hard rules) | No | No |
| CrossSessionBaselineComparator | Session close | Low (population stats) | Yes (rolling window) | Yes |
| TokenSpikeAlertPipeline | Per turn | Low (dedup + combo) | Partial | Via comparator |
| TokenAnomalyDashboard | Aggregated | N/A (display only) | N/A | Yes |

**Best for production**: Deploy `SessionBudgetEnforcer` as the hard gate (always blocks runaway sessions) and `EWMATokenDetector` as the soft anomaly signal (adapts to legitimate growth). Wire both into `TokenSpikeAlertPipeline` for unified alerting with deduplication. Use `CrossSessionBaselineComparator` to score sessions at close for post-hoc analysis. Surface everything in `TokenAnomalyDashboard` to give on-call engineers a live view of cost-anomalous sessions.
