---
title: "Agent doesn't implement exponential moving average health scoring"
description: "The agent makes binary up/down decisions about dependencies based on the last single call result. A momentary timeout marks a healthy service as down; a single success after many failures marks an unhealthy service as up."
difficulty: intermediate
category: reliability
tags: [health-scoring, EMA, circuit-breaker, dependency-monitoring, resilience]
---

## Problem

Binary health checks produce noisy, unstable routing decisions. If the last call to a dependency failed (due to a 500ms network blip), the agent immediately routes all traffic away — even though 99% of calls succeed. Conversely, after a service partially recovers, a single success opens the circuit and floods the recovering service with traffic.

Exponential Moving Average (EMA) health scoring smooths this by maintaining a continuous score between 0 (always failing) and 1 (always succeeding). Routing decisions are based on the stable score, not the last single call.

```python
# BAD: binary last-call health check
class NaiveCircuitBreaker:
    def is_healthy(self, dependency: str) -> bool:
        return self._last_call_succeeded.get(dependency, True)
    # Flips on every single failure/success — extremely noisy
```

## Solution 1: Basic EMA health scorer

Maintain an EMA score per dependency. Each call outcome (success=1.0, failure=0.0) updates the score. Route only when score exceeds a threshold.

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EMAHealthScore:
    """
    Tracks health as an Exponential Moving Average.
    score = alpha * latest_outcome + (1 - alpha) * previous_score
    Lower alpha → slower to change (more stable, less responsive).
    Higher alpha → faster to react (more responsive, noisier).
    """
    name: str
    alpha: float = 0.1            # smoothing factor (0.0–1.0)
    initial_score: float = 1.0   # assume healthy at start
    healthy_threshold: float = 0.7
    unhealthy_threshold: float = 0.3

    score: float = field(init=False)
    last_update: float = field(default_factory=time.monotonic)
    total_calls: int = 0
    total_successes: int = 0

    def __post_init__(self):
        self.score = self.initial_score

    def record(self, success: bool) -> float:
        outcome = 1.0 if success else 0.0
        self.score = self.alpha * outcome + (1 - self.alpha) * self.score
        self.last_update = time.monotonic()
        self.total_calls += 1
        if success:
            self.total_successes += 1
        return self.score

    @property
    def is_healthy(self) -> bool:
        return self.score >= self.healthy_threshold

    @property
    def is_degraded(self) -> bool:
        return self.unhealthy_threshold <= self.score < self.healthy_threshold

    @property
    def is_unhealthy(self) -> bool:
        return self.score < self.unhealthy_threshold

    @property
    def status(self) -> str:
        if self.is_healthy:
            return "healthy"
        elif self.is_degraded:
            return "degraded"
        return "unhealthy"

    @property
    def success_rate(self) -> float:
        return self.total_successes / max(self.total_calls, 1)

    def __repr__(self) -> str:
        return (
            f"EMAHealth({self.name}: score={self.score:.3f} "
            f"status={self.status} calls={self.total_calls})"
        )


# ── Demo ──────────────────────────────────────────────────────────────
scorer = EMAHealthScore("payment_api", alpha=0.2)

# Simulate: 5 successes, then 3 failures, then recovery
sequence = [True] * 5 + [False] * 3 + [True] * 5
for i, success in enumerate(sequence):
    score = scorer.record(success)
    print(f"call {i+1:2d}: {'OK' if success else 'FAIL'} → score={score:.3f} [{scorer.status}]")
```

## Solution 2: Time-decayed EMA with staleness detection

If a dependency goes quiet (no calls for a while), the stale score shouldn't influence routing decisions. Apply time-based decay so scores drift toward the neutral midpoint when no calls are made.

```python
import time
from dataclasses import dataclass, field
import math


@dataclass
class TimeDecayedHealthScore:
    """
    EMA that decays toward a neutral value when no observations arrive.
    Decay rate is configurable; after `half_life_seconds`, the score
    moves halfway toward `neutral_score`.
    """
    name: str
    alpha: float = 0.15
    neutral_score: float = 0.5      # where score drifts without observations
    half_life_seconds: float = 300.0  # 5 minutes to reach neutral
    healthy_threshold: float = 0.65

    _score: float = field(init=False)
    _last_observation: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self._score = 1.0   # assume healthy at start

    def _apply_decay(self) -> float:
        """Apply time-based decay toward neutral_score."""
        now = time.monotonic()
        elapsed = now - self._last_observation
        if elapsed < 0.1:
            return self._score
        # Exponential decay toward neutral
        decay_factor = math.exp(-math.log(2) * elapsed / self.half_life_seconds)
        decayed = self.neutral_score + (self._score - self.neutral_score) * decay_factor
        return decayed

    @property
    def score(self) -> float:
        return self._apply_decay()

    def record(self, success: bool) -> float:
        # First apply decay for elapsed time, then blend new observation
        decayed = self._apply_decay()
        outcome = 1.0 if success else 0.0
        self._score = self.alpha * outcome + (1 - self.alpha) * decayed
        self._last_observation = time.monotonic()
        return self._score

    @property
    def is_healthy(self) -> bool:
        return self.score >= self.healthy_threshold

    @property
    def staleness_seconds(self) -> float:
        return time.monotonic() - self._last_observation


# ── Demo ──────────────────────────────────────────────────────────────
import asyncio


async def demo():
    scorer = TimeDecayedHealthScore("search_api", half_life_seconds=2.0)

    # 5 successes
    for _ in range(5):
        scorer.record(True)
    print(f"After 5 successes: {scorer.score:.3f}")

    # Wait 2 seconds (one half-life) — score should drift toward 0.5
    await asyncio.sleep(2.0)
    print(f"After 2s idle (decay): {scorer.score:.3f} (staleness={scorer.staleness_seconds:.1f}s)")

    # Resume observations
    scorer.record(True)
    print(f"After recovery call: {scorer.score:.3f}")


asyncio.run(demo())
```

## Solution 3: Multi-metric health score with weighted dimensions

Combine multiple health signals (error rate, latency percentile, timeout rate) into a single weighted composite score.

```python
import time
from dataclasses import dataclass, field
from collections import deque
import statistics


@dataclass
class LatencyTracker:
    window: int = 100
    _samples: deque = field(default_factory=lambda: deque(maxlen=100))

    def record(self, latency_ms: float):
        self._samples.append(latency_ms)

    def p99(self) -> float:
        if not self._samples:
            return 0.0
        data = sorted(self._samples)
        return data[int(len(data) * 0.99)]

    def score_vs_budget(self, budget_ms: float) -> float:
        """1.0 if p99 < budget; 0.0 if p99 > 2x budget."""
        p99 = self.p99()
        if p99 == 0.0:
            return 1.0
        ratio = p99 / budget_ms
        return max(0.0, min(1.0, 2.0 - ratio))


class CompositeHealthScore:
    """
    Weighted combination of:
      - error_rate_score (EMA of success/failure)
      - latency_score (P99 vs SLO budget)
      - timeout_score (EMA of timeout rate)
    """

    def __init__(
        self,
        name: str,
        latency_budget_ms: float = 500.0,
        weights: dict[str, float] | None = None,
        alpha: float = 0.1,
    ):
        self.name = name
        self.alpha = alpha
        self.latency_budget = latency_budget_ms
        self.weights = weights or {
            "error_rate": 0.5,
            "latency": 0.3,
            "timeout": 0.2,
        }
        self._error_ema = 1.0
        self._timeout_ema = 1.0
        self._latency = LatencyTracker()

    def record_success(self, latency_ms: float):
        self._error_ema = self.alpha * 1.0 + (1 - self.alpha) * self._error_ema
        self._timeout_ema = self.alpha * 1.0 + (1 - self.alpha) * self._timeout_ema
        self._latency.record(latency_ms)

    def record_error(self):
        self._error_ema = self.alpha * 0.0 + (1 - self.alpha) * self._error_ema

    def record_timeout(self):
        self._error_ema = self.alpha * 0.0 + (1 - self.alpha) * self._error_ema
        self._timeout_ema = self.alpha * 0.0 + (1 - self.alpha) * self._timeout_ema

    @property
    def score(self) -> float:
        latency_score = self._latency.score_vs_budget(self.latency_budget)
        return (
            self.weights["error_rate"] * self._error_ema
            + self.weights["latency"] * latency_score
            + self.weights["timeout"] * self._timeout_ema
        )

    @property
    def is_healthy(self) -> bool:
        return self.score >= 0.7

    def breakdown(self) -> dict:
        return {
            "composite": round(self.score, 3),
            "error_ema": round(self._error_ema, 3),
            "timeout_ema": round(self._timeout_ema, 3),
            "latency_p99_ms": round(self._latency.p99(), 1),
            "latency_score": round(self._latency.score_vs_budget(self.latency_budget), 3),
            "is_healthy": self.is_healthy,
        }


# ── Demo ──────────────────────────────────────────────────────────────
scorer = CompositeHealthScore("database", latency_budget_ms=100.0)
import random

for _ in range(50):
    if random.random() < 0.85:
        scorer.record_success(random.uniform(20, 90))
    elif random.random() < 0.5:
        scorer.record_error()
    else:
        scorer.record_timeout()

print(scorer.breakdown())
```

## Solution 4: Health score registry with automatic circuit breaker integration

Maintain a registry of EMA scores per dependency. Automatically open/close circuit breakers based on score thresholds, and export scores as Prometheus metrics.

```python
import asyncio
import time
from typing import Any, Callable, Awaitable
from dataclasses import dataclass, field


@dataclass
class CircuitState:
    CLOSED = "closed"       # normal operation
    OPEN = "open"           # failing — reject requests
    HALF_OPEN = "half_open" # testing recovery


@dataclass
class ManagedDependency:
    name: str
    alpha: float = 0.15
    open_threshold: float = 0.3     # open circuit if score drops below this
    close_threshold: float = 0.7    # close circuit if score rises above this
    half_open_after: float = 30.0   # seconds before trying again

    score: float = field(init=False, default=1.0)
    state: str = field(init=False, default=CircuitState.CLOSED)
    opened_at: float | None = field(init=False, default=None)
    probes_sent: int = field(init=False, default=0)

    def record(self, success: bool) -> None:
        outcome = 1.0 if success else 0.0
        self.score = self.alpha * outcome + (1 - self.alpha) * self.score
        self._update_state()

    def _update_state(self):
        if self.state == CircuitState.CLOSED:
            if self.score < self.open_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()
                print(f"[{self.name}] Circuit OPENED (score={self.score:.3f})")
        elif self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - (self.opened_at or 0)
            if elapsed >= self.half_open_after:
                self.state = CircuitState.HALF_OPEN
                self.probes_sent = 0
                print(f"[{self.name}] Circuit HALF-OPEN (probing...)")
        elif self.state == CircuitState.HALF_OPEN:
            if self.score >= self.close_threshold:
                self.state = CircuitState.CLOSED
                print(f"[{self.name}] Circuit CLOSED (recovered, score={self.score:.3f})")
            elif self.score < self.open_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            return False
        # HALF_OPEN: allow limited probes
        if self.probes_sent < 3:
            self.probes_sent += 1
            return True
        return False


class HealthScoredCircuitBreakerRegistry:
    def __init__(self):
        self._deps: dict[str, ManagedDependency] = {}

    def register(self, name: str, **kwargs) -> ManagedDependency:
        dep = ManagedDependency(name=name, **kwargs)
        self._deps[name] = dep
        return dep

    async def call(
        self,
        name: str,
        fn: Callable[[], Awaitable[Any]],
        fallback: Any = None,
    ) -> Any:
        dep = self._deps.get(name)
        if dep is None:
            raise ValueError(f"Unregistered dependency: {name}")

        if not dep.allow_request():
            print(f"[{name}] Circuit {dep.state} — using fallback")
            return fallback

        try:
            result = await fn()
            dep.record(True)
            return result
        except Exception as e:
            dep.record(False)
            raise

    def scores(self) -> dict[str, dict]:
        return {
            name: {"score": round(d.score, 3), "state": d.state}
            for name, d in self._deps.items()
        }


# ── Demo ──────────────────────────────────────────────────────────────
async def demo():
    registry = HealthScoredCircuitBreakerRegistry()
    registry.register("payment_api", open_threshold=0.4, half_open_after=0.5)

    import random
    call_count = 0

    async def payment_api():
        nonlocal call_count
        call_count += 1
        if call_count in range(6, 20):   # simulate outage
            raise ConnectionError("Service unavailable")
        return {"status": "ok"}

    for _ in range(30):
        try:
            result = await registry.call("payment_api", payment_api, fallback={"status": "cached"})
        except Exception:
            pass
        await asyncio.sleep(0.1)

    print(registry.scores())


asyncio.run(demo())
```

## Solution 5: Adaptive alpha — react faster during degradation, slower during recovery

Use a higher alpha (more reactive) when the score is dropping (degradation) and a lower alpha (more conservative) when the score is rising (recovery). This prevents premature circuit closure after a brief recovery.

```python
from dataclasses import dataclass, field


@dataclass
class AdaptiveAlphaHealth:
    """
    Asymmetric EMA: reacts quickly to failures, slowly to recoveries.
    This prevents declaring a service healthy after just 1-2 successes
    following a prolonged failure period.
    """
    name: str
    alpha_degradation: float = 0.3   # fast reaction to failures
    alpha_recovery: float = 0.05     # slow reaction to successes
    healthy_threshold: float = 0.7
    unhealthy_threshold: float = 0.4

    score: float = field(init=False, default=1.0)
    call_count: int = 0

    def record(self, success: bool) -> float:
        outcome = 1.0 if success else 0.0
        self.call_count += 1

        # Use asymmetric alpha
        if outcome < self.score:
            # Score is dropping — use faster alpha
            alpha = self.alpha_degradation
        else:
            # Score is rising — use slower alpha (harder to recover)
            alpha = self.alpha_recovery

        self.score = alpha * outcome + (1 - alpha) * self.score
        return self.score

    @property
    def status(self) -> str:
        if self.score >= self.healthy_threshold:
            return "healthy"
        elif self.score >= self.unhealthy_threshold:
            return "degraded"
        return "unhealthy"


# ── Compare symmetric vs adaptive ────────────────────────────────────
def simulate(scorer, sequence):
    for success in sequence:
        scorer.record(success)
    return scorer.score


# 10 successes → 5 failures → 3 successes
test_seq = [True]*10 + [False]*5 + [True]*3

sym = dataclass_field_default = __import__("dataclasses").dataclass

symmetric = AdaptiveAlphaHealth("sym", alpha_degradation=0.15, alpha_recovery=0.15)
adaptive = AdaptiveAlphaHealth("adaptive")

for s in test_seq:
    symmetric.record(s)
    adaptive.record(s)

print(f"Symmetric after recovery: {symmetric.score:.3f} ({symmetric.status})")
print(f"Adaptive after recovery:  {adaptive.score:.3f} ({adaptive.status})")
# Adaptive score stays lower after 3 successes — more conservative
```

## Solution 6: Health score dashboard exporter (Prometheus + Grafana alerting)

Export all EMA health scores as Prometheus gauges. Trigger Grafana alerts when any dependency score drops below the SLO threshold.

```python
import asyncio
import time
from prometheus_client import Gauge, Counter, start_http_server
from dataclasses import dataclass, field


dependency_health_score = Gauge(
    "agent_dependency_health_score",
    "EMA health score per dependency (0=unhealthy, 1=healthy)",
    labelnames=["dependency", "status"],
)

dependency_calls_total = Counter(
    "agent_dependency_calls_total",
    "Total calls to each dependency",
    labelnames=["dependency", "outcome"],
)


class PrometheusHealthRegistry:
    def __init__(self, export_port: int = 8001):
        self._scores: dict[str, "EMAHealthScore"] = {}
        self._port = export_port

    def register(self, name: str, alpha: float = 0.1) -> "EMAHealthScore":
        from solution1 import EMAHealthScore
        score = EMAHealthScore(name=name, alpha=alpha)
        self._scores[name] = score
        return score

    def record(self, name: str, success: bool):
        score = self._scores.get(name)
        if score is None:
            return
        score.record(success)
        dependency_health_score.labels(
            dependency=name, status=score.status
        ).set(score.score)
        dependency_calls_total.labels(
            dependency=name, outcome="success" if success else "error"
        ).inc()

    async def export_loop(self, interval: float = 15.0):
        start_http_server(self._port)
        print(f"Health scores exported at :8001/metrics")
        while True:
            await asyncio.sleep(interval)
            for name, score in self._scores.items():
                dependency_health_score.labels(
                    dependency=name, status=score.status
                ).set(score.score)


GRAFANA_ALERT = """
groups:
  - name: dependency-health
    rules:
      - alert: DependencyHealthLow
        expr: agent_dependency_health_score < 0.5
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Dependency {{ $labels.dependency }} health={{ $value | printf \"%.2f\" }}"
      - alert: DependencyHealthCritical
        expr: agent_dependency_health_score < 0.3
        for: 30s
        labels:
          severity: critical
        annotations:
          summary: "CRITICAL: {{ $labels.dependency }} health below SLO"
"""


async def demo():
    registry = PrometheusHealthRegistry()
    db = registry.register("database", alpha=0.1)
    api = registry.register("payment_api", alpha=0.2)

    import random
    for _ in range(100):
        registry.record("database", random.random() > 0.05)
        registry.record("payment_api", random.random() > 0.3)

    for name, score in registry._scores.items():
        print(f"{name}: {score.score:.3f} ({score.status})")


asyncio.run(demo())
```

## Comparison

| Approach | Noise tolerance | Latency-aware | Asymmetric | Auto circuit break | Exportable |
|---|---|---|---|---|---|
| Basic EMA | Yes | No | No | No | No |
| Time-decayed EMA | Yes | No | No | No | No |
| Multi-metric composite | Yes | Yes | No | No | No |
| Circuit breaker integration | Yes | No | No | Yes | No |
| Adaptive alpha EMA | Yes | No | Yes | No | No |
| Prometheus exporter | Yes | No | No | No | Yes |

**Recommendation**: Start with **basic EMA** (Solution 1) for all dependencies — it eliminates binary flap immediately. Use **adaptive alpha** (Solution 5) for services that need conservative recovery (prevent premature circuit closure). Combine with **circuit breaker integration** (Solution 4) to automatically stop sending traffic when EMA score crosses the critical threshold. Export to **Prometheus** (Solution 6) for operational dashboards.
