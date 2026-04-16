---
layout: solution
title: "Agent Doesn't Implement Cost Anomaly Detection"
category: token-cost
description: "Agents that don't monitor for abnormal token usage patterns can silently burn through budgets. Cost anomaly detection flags runaway prompts, prompt injection attacks, and usage spikes before they cause significant financial damage."
tags: [token-cost, cost-management, anomaly-detection, monitoring, budget, python]
---

## Problem

Without cost anomaly detection, a single misbehaving agent — a prompt injection that causes verbose responses, an infinite context growth bug, or a runaway batch job — can drain thousands of dollars before anyone notices. Anomaly detection applies statistical baselines to token usage, alerting operators when spending deviates significantly from normal patterns.

## Solutions

### Option 1: Rolling Z-Score Baseline with Threshold Alerts

```python
import anthropic
import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable

@dataclass
class UsageSample:
    timestamp: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    model: str
    session_id: str

class ZScoreAnomalyDetector:
    def __init__(self, window_size: int = 50, z_threshold: float = 3.0,
                 min_samples: int = 10):
        self._window: deque[float] = deque(maxlen=window_size)
        self._z_threshold = z_threshold
        self._min_samples = min_samples
        self._alerts_fired = 0

    def _stats(self) -> tuple[float, float]:
        n = len(self._window)
        if n < 2:
            return 0.0, 0.0
        mean = sum(self._window) / n
        variance = sum((x - mean) ** 2 for x in self._window) / (n - 1)
        return mean, math.sqrt(variance)

    def check(self, sample: UsageSample,
              alert_cb: Optional[Callable[[str], None]] = None) -> Optional[float]:
        """Returns z-score if anomalous, None otherwise."""
        total = float(sample.total_tokens)
        mean, std = self._stats()

        is_anomaly = False
        z_score = None
        if len(self._window) >= self._min_samples and std > 0:
            z_score = (total - mean) / std
            if abs(z_score) >= self._z_threshold:
                is_anomaly = True
                self._alerts_fired += 1
                msg = (f"ANOMALY: {sample.total_tokens} tokens (z={z_score:.2f}, "
                       f"mean={mean:.0f}, std={std:.0f}) session={sample.session_id}")
                if alert_cb:
                    alert_cb(msg)
                else:
                    print(f"[COST ALERT] {msg}")

        self._window.append(total)
        return z_score if is_anomaly else None

    @property
    def stats(self) -> dict:
        mean, std = self._stats()
        return {"samples": len(self._window), "mean": mean, "std": std,
                "alerts_fired": self._alerts_fired}

def run_with_anomaly_detection():
    client = anthropic.Anthropic()
    detector = ZScoreAnomalyDetector(window_size=20, z_threshold=2.5, min_samples=5)

    prompts = [
        ("session-1", "What is 2+2?"),
        ("session-2", "Name a color."),
        ("session-3", "Name an animal."),
        ("session-4", "What is the capital of France?"),
        ("session-5", "What is 3+3?"),
        # Simulate anomaly: very long output request
        ("session-6", "List the first 50 prime numbers with explanations for each."),
    ]

    total_cost_usd = 0.0
    COST_PER_1K = {"input": 0.00025, "output": 0.00125}  # Haiku pricing

    for session_id, prompt in prompts:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        sample = UsageSample(
            timestamp=time.time(),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
            model="claude-haiku-4-5-20251001",
            session_id=session_id,
        )
        cost = (sample.input_tokens / 1000 * COST_PER_1K["input"] +
                sample.output_tokens / 1000 * COST_PER_1K["output"])
        total_cost_usd += cost

        z = detector.check(sample)
        flag = " ← ANOMALY" if z is not None else ""
        print(f"[{session_id}] tokens={sample.total_tokens} cost=${cost:.6f}{flag}")

    print(f"\nDetector stats: {detector.stats}")
    print(f"Total cost: ${total_cost_usd:.6f}")

if __name__ == "__main__":
    run_with_anomaly_detection()

# Expected Token Savings: Early anomaly detection can prevent 10-100x cost spikes
# Environment: pip install anthropic
```

### Option 2: Per-Session Budget Tracker with Rate Anomaly

```python
import anthropic
import time
import uuid
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional

@dataclass
class SessionBudget:
    session_id: str
    budget_tokens: int = 50_000
    used_tokens: int = 0
    started_at: float = field(default_factory=time.time)
    requests: int = 0
    anomaly_flags: list[str] = field(default_factory=list)

    @property
    def usage_pct(self) -> float:
        return (self.used_tokens / self.budget_tokens) * 100

    @property
    def tokens_per_request(self) -> float:
        return self.used_tokens / self.requests if self.requests > 0 else 0

    @property
    def elapsed_minutes(self) -> float:
        return (time.time() - self.started_at) / 60

class SessionBudgetAnomalyDetector:
    def __init__(self,
                 per_request_spike_threshold: int = 2000,
                 tokens_per_minute_limit: float = 10000,
                 budget_warn_pct: float = 80.0,
                 budget_hard_limit_pct: float = 95.0):
        self._sessions: dict[str, SessionBudget] = {}
        self._spike_threshold = per_request_spike_threshold
        self._tpm_limit = tokens_per_minute_limit
        self._warn_pct = budget_warn_pct
        self._hard_pct = budget_hard_limit_pct

    def get_or_create(self, session_id: str, budget: int = 50_000) -> SessionBudget:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionBudget(session_id=session_id,
                                                        budget_tokens=budget)
        return self._sessions[session_id]

    def record(self, session_id: str, tokens_used: int) -> tuple[bool, list[str]]:
        """Returns (should_block, anomaly_reasons)."""
        session = self.get_or_create(session_id)
        session.used_tokens += tokens_used
        session.requests += 1
        reasons = []

        # Per-request spike detection
        if tokens_used > self._spike_threshold:
            msg = (f"Request spike: {tokens_used} tokens > threshold {self._spike_threshold}")
            session.anomaly_flags.append(msg)
            reasons.append(msg)

        # Rate anomaly: tokens per minute
        elapsed = max(session.elapsed_minutes, 0.01)
        tpm = session.used_tokens / elapsed
        if tpm > self._tpm_limit:
            msg = f"Rate anomaly: {tpm:.0f} TPM > limit {self._tpm_limit:.0f}"
            session.anomaly_flags.append(msg)
            reasons.append(msg)

        # Budget thresholds
        if session.usage_pct >= self._hard_pct:
            msg = f"Budget critical: {session.usage_pct:.1f}% used"
            reasons.append(msg)
            return True, reasons  # BLOCK

        if session.usage_pct >= self._warn_pct:
            msg = f"Budget warning: {session.usage_pct:.1f}% used"
            reasons.append(msg)

        for r in reasons:
            print(f"[ANOMALY:{session_id[:8]}] {r}")

        return False, reasons

    def summary(self) -> list[dict]:
        return [
            {"session_id": s.session_id[:8], "used": s.used_tokens,
             "budget": s.budget_tokens, "pct": f"{s.usage_pct:.1f}%",
             "tpr": f"{s.tokens_per_request:.0f}", "flags": len(s.anomaly_flags)}
            for s in self._sessions.values()
        ]

def demo():
    client = anthropic.Anthropic()
    detector = SessionBudgetAnomalyDetector(
        per_request_spike_threshold=300,
        tokens_per_minute_limit=2000,
        budget_warn_pct=60.0,
        budget_hard_limit_pct=90.0,
    )

    calls = [
        ("session-alice", "What is 5+5?", 10_000),
        ("session-alice", "Name a planet.", 10_000),
        ("session-bob", "Describe quantum computing in detail.", 5_000),
        ("session-alice", "List 20 countries with their capitals.", 10_000),
        ("session-alice", "Explain machine learning in 3 sentences.", 10_000),
    ]

    for session_id, prompt, budget in calls:
        blocked, reasons = detector.record.__func__(detector, session_id, 0)  # Pre-check

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        total = response.usage.input_tokens + response.usage.output_tokens
        blocked, reasons = detector.record(session_id, total)

        status = "BLOCKED" if blocked else "OK"
        print(f"[{status}] {session_id[:12]} tokens={total} | "
              f"{response.content[0].text[:40]}")

    print("\nSession summary:")
    for s in detector.summary():
        print(f"  {s}")

if __name__ == "__main__":
    demo()

# Expected Token Savings: Can prevent budget overruns by 80-95% when thresholds are set correctly
# Environment: pip install anthropic
```

### Option 3: Time-Series Anomaly with EWMA and Adaptive Threshold

```python
import anthropic
import math
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class EWMATracker:
    """Exponentially weighted moving average for adaptive baseline."""
    alpha: float = 0.1      # Smoothing factor (lower = slower adaptation)
    beta: float = 0.1       # For tracking variance
    ewma: float = 0.0
    ewmvar: float = 0.0
    initialized: bool = False
    sigma_threshold: float = 3.0
    n_samples: int = 0

    def update(self, value: float) -> tuple[bool, float]:
        """Returns (is_anomaly, deviation_sigmas)."""
        if not self.initialized:
            self.ewma = value
            self.ewmvar = 0.0
            self.initialized = True
            self.n_samples = 1
            return False, 0.0

        diff = value - self.ewma
        self.ewma = self.alpha * value + (1 - self.alpha) * self.ewma
        self.ewmvar = self.beta * diff ** 2 + (1 - self.beta) * self.ewmvar
        self.n_samples += 1

        sigma = math.sqrt(self.ewmvar) if self.ewmvar > 0 else 1.0
        deviation = abs(diff) / sigma if sigma > 0 else 0.0

        is_anomaly = (self.n_samples >= 5 and
                      deviation >= self.sigma_threshold and
                      value > self.ewma)  # only flag upward spikes
        return is_anomaly, deviation

class EWMAcostAnomalyMonitor:
    def __init__(self, alpha: float = 0.15, sigma_threshold: float = 2.5):
        self._trackers: dict[str, EWMATracker] = {}
        self._alpha = alpha
        self._sigma = sigma_threshold
        self._anomaly_log: list[dict] = []

    def _get_tracker(self, key: str) -> EWMATracker:
        if key not in self._trackers:
            self._trackers[key] = EWMATracker(alpha=self._alpha,
                                               sigma_threshold=self._sigma)
        return self._trackers[key]

    def observe(self, model: str, session_id: str,
                input_tokens: int, output_tokens: int) -> Optional[dict]:
        total = float(input_tokens + output_tokens)
        tracker = self._get_tracker(f"{model}:{session_id}")
        is_anomaly, deviation = tracker.update(total)

        if is_anomaly:
            alert = {
                "timestamp": time.time(),
                "model": model, "session_id": session_id,
                "tokens": int(total), "baseline_ewma": tracker.ewma,
                "deviation_sigma": deviation,
            }
            self._anomaly_log.append(alert)
            print(f"[EWMA ANOMALY] {model}:{session_id[:8]} "
                  f"tokens={int(total)} ewma={tracker.ewma:.0f} "
                  f"deviation={deviation:.1f}σ")
            return alert
        else:
            print(f"[OK] {session_id[:8]} tokens={int(total)} "
                  f"ewma={tracker.ewma:.0f} dev={deviation:.1f}σ")
            return None

    @property
    def anomaly_count(self) -> int:
        return len(self._anomaly_log)

def demo():
    client = anthropic.Anthropic()
    monitor = EWMAcostAnomalyMonitor(alpha=0.2, sigma_threshold=2.0)
    session = "session-test"

    # Normal usage baseline
    normal_prompts = [
        "Say 'yes' or 'no' only.",
        "What is 2+2? Answer with a single number.",
        "Is the sky blue? Yes or no.",
        "Name one planet.",
        "What color is grass?",
    ]
    for p in normal_prompts:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=30,
            messages=[{"role": "user", "content": p}],
        )
        monitor.observe("claude-haiku-4-5-20251001", session,
                        r.usage.input_tokens, r.usage.output_tokens)

    # Anomalous request
    anomalous = "List every country in the world alphabetically with their capital cities."
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": anomalous}],
    )
    monitor.observe("claude-haiku-4-5-20251001", session,
                    r.usage.input_tokens, r.usage.output_tokens)

    print(f"\nTotal anomalies detected: {monitor.anomaly_count}")

if __name__ == "__main__":
    demo()

# Expected Token Savings: EWMA adapts to legitimate usage growth while catching real spikes
# Environment: pip install anthropic
```

### Option 4: Cross-Session Cost Aggregator with Hourly Budget Caps

```python
import anthropic
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class CostBucket:
    """Tracks cost for a given (tenant, hour) bucket."""
    tenant_id: str
    hour_key: str  # "2026-04-16T14"
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    budget_usd: float = 1.0  # $1/hour default
    anomaly_triggered: bool = False

    # Haiku pricing
    INPUT_COST_PER_1K = 0.00025
    OUTPUT_COST_PER_1K = 0.00125

    @property
    def cost_usd(self) -> float:
        return (self.input_tokens / 1000 * self.INPUT_COST_PER_1K +
                self.output_tokens / 1000 * self.OUTPUT_COST_PER_1K)

    @property
    def budget_pct(self) -> float:
        return (self.cost_usd / self.budget_usd) * 100

class HourlyBudgetAnomalyDetector:
    def __init__(self, default_budget_usd: float = 0.50):
        self._buckets: dict[str, CostBucket] = {}
        self._default_budget = default_budget_usd
        self._tenant_budgets: dict[str, float] = {}

    def set_budget(self, tenant_id: str, hourly_usd: float) -> None:
        self._tenant_budgets[tenant_id] = hourly_usd

    def _bucket_key(self, tenant_id: str) -> str:
        hour = time.strftime("%Y-%m-%dT%H")
        return f"{tenant_id}:{hour}"

    def _get_bucket(self, tenant_id: str) -> CostBucket:
        key = self._bucket_key(tenant_id)
        if key not in self._buckets:
            hour = time.strftime("%Y-%m-%dT%H")
            budget = self._tenant_budgets.get(tenant_id, self._default_budget)
            self._buckets[key] = CostBucket(tenant_id=tenant_id,
                                             hour_key=hour, budget_usd=budget)
        return self._buckets[key]

    def record(self, tenant_id: str, input_tokens: int,
               output_tokens: int) -> tuple[bool, str]:
        """Returns (is_blocked, status_message)."""
        bucket = self._get_bucket(tenant_id)
        bucket.input_tokens += input_tokens
        bucket.output_tokens += output_tokens
        bucket.requests += 1

        pct = bucket.budget_pct

        if pct >= 100.0:
            if not bucket.anomaly_triggered:
                bucket.anomaly_triggered = True
                print(f"[BUDGET EXCEEDED] {tenant_id} spent ${bucket.cost_usd:.4f} "
                      f"({pct:.1f}% of ${bucket.budget_usd}/hr budget)")
            return True, f"Budget exceeded: ${bucket.cost_usd:.4f}/${bucket.budget_usd}"

        if pct >= 80.0:
            print(f"[BUDGET WARNING] {tenant_id} {pct:.1f}% of hourly budget used "
                  f"(${bucket.cost_usd:.4f}/${bucket.budget_usd})")

        return False, f"OK: ${bucket.cost_usd:.6f} ({pct:.1f}%)"

    def report(self) -> list[dict]:
        return [
            {"tenant": b.tenant_id, "hour": b.hour_key,
             "cost_usd": f"${b.cost_usd:.6f}", "budget_pct": f"{b.budget_pct:.1f}%",
             "requests": b.requests, "anomaly": b.anomaly_triggered}
            for b in self._buckets.values()
        ]

def demo():
    client = anthropic.Anthropic()
    detector = HourlyBudgetAnomalyDetector(default_budget_usd=0.005)  # tiny budget for demo
    detector.set_budget("tenant-enterprise", 0.01)
    detector.set_budget("tenant-free", 0.002)

    calls = [
        ("tenant-enterprise", "Define AI."),
        ("tenant-free", "What is 2+2?"),
        ("tenant-enterprise", "Explain neural networks briefly."),
        ("tenant-free", "Name a color."),
        ("tenant-free", "Explain quantum computing in depth."),
    ]

    for tenant, prompt in calls:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        blocked, status = detector.record(
            tenant, response.usage.input_tokens, response.usage.output_tokens
        )
        print(f"[{tenant[:20]}] {status} | {response.content[0].text[:40]}")
        if blocked:
            print(f"  ↳ Would block subsequent requests for this hour")

    print("\nHourly budget report:")
    for row in detector.report():
        print(f"  {row}")

if __name__ == "__main__":
    demo()

# Expected Token Savings: Hard budget caps prevent runaway spending entirely
# Environment: pip install anthropic
```

### Option 5: Async Real-Time Anomaly Monitor with Sliding Window

```python
import anthropic
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional

@dataclass
class TokenEvent:
    timestamp: float
    session_id: str
    total_tokens: int

AlertCallback = Callable[[str, dict], Awaitable[None]]

class SlidingWindowAnomalyMonitor:
    def __init__(self, window_seconds: float = 60.0,
                 max_tokens_per_window: int = 20_000,
                 spike_multiplier: float = 5.0):
        self._window_sec = window_seconds
        self._max_tpw = max_tokens_per_window
        self._spike_mult = spike_multiplier
        self._events: deque[TokenEvent] = deque()
        self._lock = asyncio.Lock()
        self._alert_cbs: list[AlertCallback] = []
        self._baseline_avg: float = 0.0
        self._n_baseline: int = 0

    def add_alert(self, cb: AlertCallback) -> None:
        self._alert_cbs.append(cb)

    async def _fire_alert(self, alert_type: str, detail: dict) -> None:
        await asyncio.gather(*[cb(alert_type, detail) for cb in self._alert_cbs],
                             return_exceptions=True)

    def _prune_window(self, now: float) -> None:
        cutoff = now - self._window_sec
        while self._events and self._events[0].timestamp < cutoff:
            self._events.popleft()

    def _window_total(self) -> int:
        return sum(e.total_tokens for e in self._events)

    async def record(self, session_id: str, input_t: int, output_t: int) -> Optional[str]:
        now = time.time()
        total = input_t + output_t
        event = TokenEvent(timestamp=now, session_id=session_id, total_tokens=total)

        async with self._lock:
            self._prune_window(now)
            self._events.append(event)
            window_total = self._window_total()

            # Update baseline (exponential averaging)
            if self._n_baseline < 5:
                self._baseline_avg = (self._baseline_avg * self._n_baseline + total) / (self._n_baseline + 1)
                self._n_baseline += 1
                return None

            anomaly = None

            # Window rate anomaly
            if window_total > self._max_tpw:
                anomaly = f"Window rate: {window_total} tokens in {self._window_sec:.0f}s"
                await self._fire_alert("window_rate_exceeded", {
                    "session_id": session_id, "window_tokens": window_total,
                    "limit": self._max_tpw, "window_sec": self._window_sec,
                })

            # Per-request spike
            if total > self._baseline_avg * self._spike_mult:
                spike_msg = (f"Spike: {total} tokens vs baseline "
                             f"{self._baseline_avg:.0f} (x{total/self._baseline_avg:.1f})")
                anomaly = spike_msg
                await self._fire_alert("request_spike", {
                    "session_id": session_id, "tokens": total,
                    "baseline": self._baseline_avg, "multiplier": total / self._baseline_avg,
                })

            # Update baseline
            self._baseline_avg = 0.9 * self._baseline_avg + 0.1 * total
            return anomaly

async def log_alert(alert_type: str, detail: dict) -> None:
    print(f"  [ALERT:{alert_type}] {detail}")

async def main():
    client = anthropic.AsyncAnthropic()
    monitor = SlidingWindowAnomalyMonitor(
        window_seconds=30.0,
        max_tokens_per_window=1500,
        spike_multiplier=4.0,
    )
    monitor.add_alert(log_alert)

    # Establish baseline with small requests
    baselines = ["Yes.", "No.", "Blue.", "Cat.", "Paris."]
    for text in baselines:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": f"Say only: {text}"}],
        )
        anomaly = await monitor.record("baseline-session",
                                       r.usage.input_tokens, r.usage.output_tokens)
        print(f"[baseline] tokens={r.usage.input_tokens+r.usage.output_tokens} "
              f"anomaly={'yes' if anomaly else 'no'}")

    # Spike request
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content":
                   "List 30 world capitals with their countries in detail."}],
    )
    anomaly = await monitor.record("attacker-session",
                                   r.usage.input_tokens, r.usage.output_tokens)
    print(f"[spike] tokens={r.usage.input_tokens+r.usage.output_tokens} anomaly={anomaly}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Sliding window catches sustained abuse patterns early
# Environment: pip install anthropic
```

### Option 6: Multi-Model Cost Anomaly with Alerting and Auto-Throttle

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class ThrottleState(Enum):
    NORMAL = "normal"
    WARNED = "warned"
    THROTTLED = "throttled"  # Downgrade to cheaper model
    BLOCKED = "blocked"      # Reject requests

MODEL_COSTS_PER_1K = {
    "claude-haiku-4-5-20251001":  {"input": 0.00025,  "output": 0.00125},
    "claude-sonnet-4-6":          {"input": 0.003,    "output": 0.015},
    "claude-opus-4-6":            {"input": 0.015,    "output": 0.075},
}

FALLBACK_MODEL = "claude-haiku-4-5-20251001"

@dataclass
class TenantCostTracker:
    tenant_id: str
    hourly_budget_usd: float = 0.10
    _hourly_spent: float = 0.0
    _hour_key: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H"))
    _state: ThrottleState = ThrottleState.NORMAL
    _state_changed_at: float = field(default_factory=time.time)

    def _reset_if_new_hour(self) -> None:
        current_hour = time.strftime("%Y-%m-%dT%H")
        if current_hour != self._hour_key:
            self._hourly_spent = 0.0
            self._hour_key = current_hour
            self._state = ThrottleState.NORMAL

    def record_cost(self, cost_usd: float) -> ThrottleState:
        self._reset_if_new_hour()
        self._hourly_spent += cost_usd
        pct = (self._hourly_spent / self.hourly_budget_usd) * 100

        prev = self._state
        if pct >= 100.0:
            self._state = ThrottleState.BLOCKED
        elif pct >= 80.0:
            self._state = ThrottleState.THROTTLED
        elif pct >= 60.0:
            self._state = ThrottleState.WARNED
        else:
            self._state = ThrottleState.NORMAL

        if self._state != prev:
            print(f"[THROTTLE:{self.tenant_id}] {prev.value} → {self._state.value} "
                  f"(${self._hourly_spent:.4f}/{self.hourly_budget_usd:.2f} = {pct:.1f}%)")
        return self._state

    @property
    def spent_usd(self) -> float:
        return self._hourly_spent

async def adaptive_request(client: anthropic.AsyncAnthropic,
                            tenant_id: str, prompt: str, requested_model: str,
                            tracker: TenantCostTracker) -> Optional[str]:
    state = tracker._state

    if state == ThrottleState.BLOCKED:
        print(f"[BLOCKED] {tenant_id}: budget exhausted")
        return None

    # Auto-throttle: use cheaper model if throttled
    model = FALLBACK_MODEL if state == ThrottleState.THROTTLED else requested_model
    if model != requested_model:
        print(f"[THROTTLE] {tenant_id}: downgraded {requested_model} → {model}")

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        costs = MODEL_COSTS_PER_1K[model]
        cost = (response.usage.input_tokens / 1000 * costs["input"] +
                response.usage.output_tokens / 1000 * costs["output"])
        new_state = tracker.record_cost(cost)

        print(f"[{tenant_id}] model={model} cost=${cost:.6f} state={new_state.value} "
              f"| {response.content[0].text[:50]}")
        return response.content[0].text
    except Exception as e:
        print(f"[ERROR] {tenant_id}: {e}")
        return None

async def main():
    client = anthropic.AsyncAnthropic()
    tracker = TenantCostTracker(tenant_id="tenant-demo", hourly_budget_usd=0.001)

    prompts = [
        ("claude-sonnet-4-6", "Explain entropy briefly."),
        ("claude-sonnet-4-6", "What is machine learning?"),
        ("claude-sonnet-4-6", "Describe quantum computing."),
        ("claude-sonnet-4-6", "What is the speed of light?"),
        ("claude-sonnet-4-6", "Define recursion."),
    ]

    for model, prompt in prompts:
        await adaptive_request(client, "tenant-demo", prompt, model, tracker)

    print(f"\nFinal spent: ${tracker.spent_usd:.6f}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Auto-throttle to Haiku can save 92% vs Sonnet, 98% vs Opus
# Environment: pip install anthropic
```

## Comparison

| Option | Detection Method | Granularity | Auto-Action | Best For |
|--------|-----------------|-------------|-------------|----------|
| 1. Z-score | Statistical baseline | Per-session | Alert only | General purpose |
| 2. Session budget | Hard limits | Per-session | Block | Multi-user systems |
| 3. EWMA | Adaptive baseline | Per-session | Alert only | Variable workloads |
| 4. Hourly buckets | Time-based caps | Per-tenant/hour | Block | SaaS billing |
| 5. Sliding window | Rate + spike | Per-window | Alert only | Real-time abuse |
| 6. Multi-model throttle | Budget % tiers | Per-tenant | Downgrade/block | Cost optimization |
