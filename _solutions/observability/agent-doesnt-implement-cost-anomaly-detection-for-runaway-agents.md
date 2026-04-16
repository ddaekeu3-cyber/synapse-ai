---
layout: solution
title: "Agent Doesn't Implement Cost Anomaly Detection for Runaway Agents"
category: observability
description: "Detect abnormal token consumption patterns in real time — when an agent suddenly spikes to 10× its baseline cost, catch it before the bill does."
tags: [observability, cost, anomaly-detection, monitoring, alerting, runaway, token-budget]
---

## Problem

Runaway agents are silent budget killers. An infinite loop silently burns through 2 million tokens over 4 hours. A prompt injection causes an agent to generate 50,000-token responses in a loop. A misconfigured retry policy hammers the API 300 times a minute. Without anomaly detection, the first alert is the monthly invoice — or an API rate limit hit that takes the whole system down.

```python
# Naive: track usage but never alert on anomalies
def respond(message: str) -> str:
    r = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=4096,
                               messages=[{"role": "user", "content": message}])
    total_tokens += r.usage.input_tokens + r.usage.output_tokens  # logged, never alerted
    return r.content[0].text
```

## Solution Options

### Option 1: Rolling Z-Score Anomaly Detector

Maintain a rolling baseline of token usage per call. Compute a Z-score for each new call. Alert when usage deviates more than N standard deviations from the baseline.

```python
import anthropic
import math
import time
from collections import deque
from dataclasses import dataclass, field

@dataclass
class UsageStats:
    timestamp: float
    input_tokens: int
    output_tokens: int
    total_tokens: int

class ZScoreAnomalyDetector:
    def __init__(
        self,
        window_size: int = 50,
        alert_threshold_sigma: float = 3.0,
        min_samples: int = 10,
    ):
        self.window = deque(maxlen=window_size)
        self.threshold = alert_threshold_sigma
        self.min_samples = min_samples
        self.alert_count = 0

    def _stats(self) -> tuple[float, float]:
        if len(self.window) < 2:
            return 0.0, float("inf")
        values = [s.total_tokens for s in self.window]
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return mean, math.sqrt(variance)

    def record(self, stats: UsageStats) -> bool:
        """Returns True if anomaly detected."""
        mean, std = self._stats()
        if len(self.window) >= self.min_samples and std > 0:
            z = (stats.total_tokens - mean) / std
            if abs(z) > self.threshold:
                self.alert_count += 1
                print(
                    f"[ANOMALY] Z={z:.1f} > {self.threshold}σ | "
                    f"tokens={stats.total_tokens} (baseline μ={mean:.0f} σ={std:.0f}) | "
                    f"alert #{self.alert_count}"
                )
                self.window.append(stats)
                return True
        self.window.append(stats)
        return False


client = anthropic.Anthropic()
detector = ZScoreAnomalyDetector(window_size=50, alert_threshold_sigma=3.0)

def monitored_respond(user_message: str, max_tokens: int = 512) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_message}],
    )
    stats = UsageStats(
        timestamp=time.time(),
        input_tokens=r.usage.input_tokens,
        output_tokens=r.usage.output_tokens,
        total_tokens=r.usage.input_tokens + r.usage.output_tokens,
    )
    is_anomaly = detector.record(stats)
    if is_anomaly:
        # In production: trigger alert, pause agent, notify on-call
        pass
    return r.content[0].text


# Warm up baseline
for q in ["What is Python?", "Explain loops", "What is async?"] * 5:
    monitored_respond(q)

# Simulate a runaway call with a very long prompt
print(monitored_respond("x " * 800 + "Summarize this."))
print(f"Total anomalies detected: {detector.alert_count}")

# Expected Token Savings: Detection overhead zero tokens; prevents runaway agents consuming thousands of dollars
# Environment: ANTHROPIC_API_KEY
```

### Option 2: Cost Budget with Hard Ceiling and Graceful Shutdown

Define a cost budget per session, per hour, and per day. When any budget is exceeded, gracefully shut down the agent and emit a structured alert.

```python
import anthropic
import time
from dataclasses import dataclass, field
from enum import Enum

class BudgetExceededError(Exception):
    def __init__(self, level: str, spent: float, limit: float):
        self.level = level
        self.spent = spent
        self.limit = limit
        super().__init__(f"[BUDGET EXCEEDED] {level}: spent=${spent:.4f} limit=${limit:.4f}")

@dataclass
class CostBudget:
    # Costs in USD (Haiku: input $0.80/M, output $4.00/M)
    input_cost_per_token: float = 0.80 / 1_000_000
    output_cost_per_token: float = 4.00 / 1_000_000
    session_limit_usd: float = 0.50
    hourly_limit_usd: float = 5.00
    daily_limit_usd: float = 20.00

    session_spent: float = 0.0
    hourly_spent: float = 0.0
    daily_spent: float = 0.0

    session_start: float = field(default_factory=time.time)
    hour_start: float = field(default_factory=time.time)
    day_start: float = field(default_factory=time.time)
    call_count: int = 0

    def charge(self, input_tokens: int, output_tokens: int) -> float:
        cost = (input_tokens * self.input_cost_per_token +
                output_tokens * self.output_cost_per_token)
        now = time.time()
        # Reset hourly/daily windows
        if now - self.hour_start > 3600:
            self.hourly_spent = 0.0
            self.hour_start = now
        if now - self.day_start > 86400:
            self.daily_spent = 0.0
            self.day_start = now
        self.session_spent += cost
        self.hourly_spent += cost
        self.daily_spent += cost
        self.call_count += 1
        print(
            f"[BUDGET] call={self.call_count} cost=${cost:.5f} "
            f"session=${self.session_spent:.4f}/{self.session_limit_usd} "
            f"hourly=${self.hourly_spent:.4f}/{self.hourly_limit_usd}"
        )
        if self.session_spent > self.session_limit_usd:
            raise BudgetExceededError("SESSION", self.session_spent, self.session_limit_usd)
        if self.hourly_spent > self.hourly_limit_usd:
            raise BudgetExceededError("HOURLY", self.hourly_spent, self.hourly_limit_usd)
        if self.daily_spent > self.daily_limit_usd:
            raise BudgetExceededError("DAILY", self.daily_spent, self.daily_limit_usd)
        return cost

    def remaining_session(self) -> float:
        return max(0.0, self.session_limit_usd - self.session_spent)


client = anthropic.Anthropic()
budget = CostBudget(session_limit_usd=0.01)  # tight limit to demo

def budget_controlled_respond(message: str) -> str:
    # Pre-flight: estimate cost from message length
    estimated_input = len(message.split()) * 1.3
    if estimated_input * budget.input_cost_per_token > budget.remaining_session():
        raise BudgetExceededError("PRE-FLIGHT", budget.session_spent, budget.session_limit_usd)

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": message}],
    )
    budget.charge(r.usage.input_tokens, r.usage.output_tokens)
    return r.content[0].text


try:
    for i in range(100):  # would normally run forever
        budget_controlled_respond(f"Question {i}: Explain Python concept #{i}")
except BudgetExceededError as e:
    print(f"Agent stopped: {e}")
    print(f"Total calls made: {budget.call_count}")

# Expected Token Savings: Budget ceiling prevents runaway agents; pre-flight check avoids partial charges
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Rate-of-Change Spike Detector with Auto-Pause

Monitor tokens-per-minute. When the rate spikes by more than a configurable factor over the recent baseline, pause the agent and require manual or automated approval to continue.

```python
import anthropic
import asyncio
import time
from collections import deque
from dataclasses import dataclass

@dataclass
class RateWindow:
    start_time: float
    total_tokens: int = 0
    call_count: int = 0

    def tpm(self, now: float) -> float:
        elapsed_minutes = max((now - self.start_time) / 60.0, 1e-9)
        return self.total_tokens / elapsed_minutes

class SpikeDetector:
    def __init__(
        self,
        baseline_window_minutes: float = 5.0,
        alert_window_seconds: float = 60.0,
        spike_factor: float = 5.0,   # alert if current rate > 5× baseline
        auto_pause_factor: float = 10.0,
    ):
        self.baseline_window = baseline_window_minutes * 60
        self.alert_window = alert_window_seconds
        self.spike_factor = spike_factor
        self.auto_pause_factor = auto_pause_factor
        self.history: deque[tuple[float, int]] = deque()  # (timestamp, tokens)
        self._paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # start unpaused

    def record(self, tokens: int) -> None:
        now = time.time()
        self.history.append((now, tokens))
        # Evict entries older than baseline window
        cutoff = now - self.baseline_window
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()
        self._check_spike(now)

    def _rate_in_window(self, now: float, window_seconds: float) -> float:
        cutoff = now - window_seconds
        recent = [t for ts, t in self.history if ts >= cutoff]
        if not recent:
            return 0.0
        return sum(recent) / (window_seconds / 60.0)  # tokens per minute

    def _check_spike(self, now: float) -> None:
        current_tpm = self._rate_in_window(now, self.alert_window)
        baseline_tpm = self._rate_in_window(now, self.baseline_window)
        if baseline_tpm < 10:
            return  # not enough data for baseline
        ratio = current_tpm / max(baseline_tpm, 1)
        print(f"[RATE] current={current_tpm:.0f} tpm baseline={baseline_tpm:.0f} tpm ratio={ratio:.1f}×")
        if ratio > self.auto_pause_factor and not self._paused:
            self._paused = True
            self._pause_event.clear()
            print(f"[AUTO-PAUSE] Rate spike {ratio:.1f}× > {self.auto_pause_factor}× threshold — PAUSED")
        elif ratio > self.spike_factor:
            print(f"[SPIKE ALERT] Rate {ratio:.1f}× above baseline — monitor closely")

    async def wait_if_paused(self) -> None:
        await self._pause_event.wait()

    def resume(self) -> None:
        self._paused = False
        self._pause_event.set()
        print("[RESUME] Agent resumed after manual approval")


client = anthropic.AsyncAnthropic()
detector = SpikeDetector(spike_factor=3.0, auto_pause_factor=8.0)

async def spike_monitored_respond(message: str) -> str:
    await detector.wait_if_paused()
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": message}],
    )
    total = r.usage.input_tokens + r.usage.output_tokens
    detector.record(total)
    return r.content[0].text

async def main():
    # Establish baseline
    baseline_msgs = ["What is Python?", "Explain classes", "What are generators?"] * 5
    for msg in baseline_msgs:
        await spike_monitored_respond(msg)
        await asyncio.sleep(0.1)

    # Simulate spike: many rapid large calls
    print("\n--- Simulating spike ---")
    spike_msgs = ["Explain all of computer science in detail " * 20] * 3
    for msg in spike_msgs:
        try:
            await spike_monitored_respond(msg)
        except Exception as e:
            print(f"Error: {e}")

asyncio.run(main())

# Expected Token Savings: Auto-pause prevents runaway loops; baseline comparison catches gradual drift too
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Per-Session Cost Dashboard with Prometheus Metrics

Emit structured cost metrics that can be scraped by Prometheus, alerted on via Alertmanager, and visualized in Grafana — for production-grade observability.

```python
import anthropic
import time
from dataclasses import dataclass, field
from collections import defaultdict

# Minimal Prometheus-compatible metric emitter (no extra dependencies)
class MetricRegistry:
    def __init__(self):
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = defaultdict(list)

    def counter_inc(self, name: str, value: float = 1.0, labels: dict = None) -> None:
        key = f"{name}{self._label_str(labels)}"
        self._counters[key] += value

    def gauge_set(self, name: str, value: float, labels: dict = None) -> None:
        key = f"{name}{self._label_str(labels)}"
        self._gauges[key] = value

    def histogram_observe(self, name: str, value: float, labels: dict = None) -> None:
        key = f"{name}{self._label_str(labels)}"
        self._histograms[key].append(value)

    def _label_str(self, labels: dict | None) -> str:
        if not labels:
            return ""
        pairs = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return "{" + pairs + "}"

    def prometheus_format(self) -> str:
        lines = []
        for key, val in self._counters.items():
            name = key.split("{")[0]
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{key} {val}")
        for key, val in self._gauges.items():
            name = key.split("{")[0]
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{key} {val}")
        for key, vals in self._histograms.items():
            name = key.split("{")[0]
            lines.append(f"# TYPE {name} histogram")
            if vals:
                sorted_vals = sorted(vals)
                n = len(sorted_vals)
                for p, pct in [(0.5, "0.5"), (0.9, "0.9"), (0.95, "0.95"), (0.99, "0.99")]:
                    idx = min(int(p * n), n - 1)
                    lines.append(f'{name}_quantile{{quantile="{pct}"}} {sorted_vals[idx]:.2f}')
                lines.append(f"{name}_sum {sum(vals):.2f}")
                lines.append(f"{name}_count {n}")
        return "\n".join(lines)

metrics = MetricRegistry()

# Cost constants (USD per token)
COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.80e-6, "output": 4.00e-6},
    "claude-sonnet-4-6":         {"input": 3.00e-6, "output": 15.00e-6},
    "claude-opus-4-6":           {"input": 15.00e-6, "output": 75.00e-6},
}

ANOMALY_COST_THRESHOLD_USD = 0.05  # alert if single call costs > $0.05

client = anthropic.Anthropic()

def instrumented_respond(
    user_message: str,
    model: str = "claude-haiku-4-5-20251001",
    agent_id: str = "agent-01",
    session_id: str = "session-01",
) -> str:
    labels = {"model": model, "agent_id": agent_id}
    t0 = time.monotonic()
    r = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    latency_ms = (time.monotonic() - t0) * 1000
    inp = r.usage.input_tokens
    out = r.usage.output_tokens
    cost_usd = inp * COSTS[model]["input"] + out * COSTS[model]["output"]

    # Emit metrics
    metrics.counter_inc("agent_input_tokens_total", inp, labels)
    metrics.counter_inc("agent_output_tokens_total", out, labels)
    metrics.counter_inc("agent_cost_usd_total", cost_usd, labels)
    metrics.counter_inc("agent_api_calls_total", 1, labels)
    metrics.histogram_observe("agent_call_latency_ms", latency_ms, labels)
    metrics.histogram_observe("agent_cost_per_call_usd", cost_usd, labels)
    metrics.gauge_set("agent_last_call_cost_usd", cost_usd, labels)

    # Anomaly check
    if cost_usd > ANOMALY_COST_THRESHOLD_USD:
        metrics.counter_inc("agent_cost_anomalies_total", 1, labels)
        print(
            f"[COST ANOMALY] agent={agent_id} model={model} "
            f"cost=${cost_usd:.5f} > threshold=${ANOMALY_COST_THRESHOLD_USD}"
        )

    print(f"[METRICS] in={inp} out={out} cost=${cost_usd:.6f} lat={latency_ms:.0f}ms")
    return r.content[0].text


# Run several calls
for msg in ["What is a decorator?", "Explain async/await", "What is GIL?"]:
    instrumented_respond(msg)

print("\n=== Prometheus Metrics ===")
print(metrics.prometheus_format())

# Expected Token Savings: Metrics emit zero tokens; enables Grafana dashboards + Alertmanager rules for cost spikes
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Comparative Baseline Alerting with Rolling EMA

Use Exponential Moving Average (EMA) to maintain a smooth cost baseline. Alert when a single call deviates from the EMA by more than a threshold, with severity levels.

```python
import anthropic
import time
from dataclasses import dataclass, field
from enum import Enum

class AlertSeverity(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

@dataclass
class CostAlert:
    severity: AlertSeverity
    call_cost_usd: float
    ema_cost_usd: float
    deviation_pct: float
    message: str
    timestamp: float = field(default_factory=time.time)

class EMACostMonitor:
    def __init__(
        self,
        ema_alpha: float = 0.1,          # smoothing factor (lower = more stable baseline)
        warning_deviation_pct: float = 200.0,    # 3× baseline
        critical_deviation_pct: float = 500.0,   # 6× baseline
        min_calls_before_alert: int = 5,
    ):
        self.alpha = ema_alpha
        self.warning_pct = warning_deviation_pct
        self.critical_pct = critical_deviation_pct
        self.min_calls = min_calls_before_alert
        self.ema: float | None = None
        self.call_count: int = 0
        self.alerts: list[CostAlert] = []

    def observe(self, cost_usd: float) -> CostAlert | None:
        self.call_count += 1
        if self.ema is None:
            self.ema = cost_usd
            return None
        # Update EMA
        self.ema = self.alpha * cost_usd + (1 - self.alpha) * self.ema
        if self.call_count < self.min_calls:
            return None
        deviation_pct = ((cost_usd - self.ema) / max(self.ema, 1e-9)) * 100
        if deviation_pct > self.critical_pct:
            alert = CostAlert(
                severity=AlertSeverity.CRITICAL,
                call_cost_usd=cost_usd,
                ema_cost_usd=self.ema,
                deviation_pct=deviation_pct,
                message=f"CRITICAL: Cost {deviation_pct:.0f}% above EMA baseline",
            )
        elif deviation_pct > self.warning_pct:
            alert = CostAlert(
                severity=AlertSeverity.WARNING,
                call_cost_usd=cost_usd,
                ema_cost_usd=self.ema,
                deviation_pct=deviation_pct,
                message=f"WARNING: Cost {deviation_pct:.0f}% above EMA baseline",
            )
        else:
            return None
        self.alerts.append(alert)
        print(
            f"[{alert.severity.value}] cost=${cost_usd:.6f} ema=${self.ema:.6f} "
            f"dev={deviation_pct:.0f}% | {alert.message}"
        )
        return alert


client = anthropic.Anthropic()
monitor = EMACostMonitor(ema_alpha=0.15, warning_deviation_pct=150.0, critical_deviation_pct=400.0)

INPUT_COST = 0.80e-6
OUTPUT_COST = 4.00e-6

def ema_monitored_call(message: str, max_tokens: int = 256) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": message}],
    )
    cost = r.usage.input_tokens * INPUT_COST + r.usage.output_tokens * OUTPUT_COST
    alert = monitor.observe(cost)
    if alert and alert.severity == AlertSeverity.CRITICAL:
        # In production: notify PagerDuty, pause agent
        print(f"[ACTION] Would trigger PagerDuty alert and pause agent")
    return r.content[0].text


# Establish baseline
for msg in ["What is Python?", "What is a list?", "What is a dict?",
            "What is async?", "What is a class?", "What is a module?"]:
    ema_monitored_call(msg)

# Simulate a runaway call
print("\n--- Sending anomalously large request ---")
ema_monitored_call("Explain Python " * 100 + "in extreme detail", max_tokens=1024)

print(f"\nTotal alerts: {len(monitor.alerts)}")

# Expected Token Savings: EMA-based detection avoids noisy one-off spikes; smooth baseline reduces false positives
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Multi-Dimensional Anomaly Scoring with Composite Risk Score

Combine multiple signals — cost per call, calls per minute, output/input token ratio, consecutive high-cost calls — into a composite risk score. Alert when the composite score exceeds a threshold.

```python
import anthropic
import time
from collections import deque
from dataclasses import dataclass, field

@dataclass
class CallRecord:
    timestamp: float
    input_tokens: int
    output_tokens: int
    cost_usd: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def output_ratio(self) -> float:
        return self.output_tokens / max(self.input_tokens, 1)

@dataclass
class RiskScore:
    cost_score: float        # 0-1
    rate_score: float        # 0-1
    ratio_score: float       # 0-1
    streak_score: float      # 0-1
    composite: float         # weighted combination
    triggered: bool

INPUT_COST = 0.80e-6
OUTPUT_COST = 4.00e-6

class CompositeAnomalyDetector:
    WEIGHTS = {"cost": 0.35, "rate": 0.30, "ratio": 0.20, "streak": 0.15}
    ALERT_THRESHOLD = 0.65

    def __init__(
        self,
        window_size: int = 30,
        normal_cost_usd: float = 0.001,    # expected cost per call
        normal_calls_per_min: float = 10.0,
        normal_output_ratio: float = 2.0,   # output tokens / input tokens
        streak_threshold: int = 3,          # N consecutive high-cost calls
    ):
        self.window = deque(maxlen=window_size)
        self.normal_cost = normal_cost_usd
        self.normal_rate = normal_calls_per_min
        self.normal_ratio = normal_output_ratio
        self.streak_threshold = streak_threshold
        self._high_cost_streak = 0

    def _cost_score(self, record: CallRecord) -> float:
        ratio = record.cost_usd / max(self.normal_cost, 1e-9)
        return min(1.0, (ratio - 1.0) / 10.0) if ratio > 1 else 0.0

    def _rate_score(self) -> float:
        if len(self.window) < 2:
            return 0.0
        now = time.time()
        recent = [r for r in self.window if now - r.timestamp < 60]
        actual_rate = len(recent)
        ratio = actual_rate / max(self.normal_rate, 1)
        return min(1.0, (ratio - 1.0) / 5.0) if ratio > 1 else 0.0

    def _ratio_score(self, record: CallRecord) -> float:
        ratio = record.output_ratio / max(self.normal_ratio, 0.1)
        return min(1.0, (ratio - 1.0) / 5.0) if ratio > 1 else 0.0

    def _streak_score(self, record: CallRecord) -> float:
        if record.cost_usd > self.normal_cost * 2:
            self._high_cost_streak += 1
        else:
            self._high_cost_streak = 0
        return min(1.0, self._high_cost_streak / self.streak_threshold)

    def observe(self, record: CallRecord) -> RiskScore:
        cost_s = self._cost_score(record)
        rate_s = self._rate_score()
        ratio_s = self._ratio_score(record)
        streak_s = self._streak_score(record)
        composite = (
            self.WEIGHTS["cost"] * cost_s +
            self.WEIGHTS["rate"] * rate_s +
            self.WEIGHTS["ratio"] * ratio_s +
            self.WEIGHTS["streak"] * streak_s
        )
        self.window.append(record)
        risk = RiskScore(
            cost_score=cost_s,
            rate_score=rate_s,
            ratio_score=ratio_s,
            streak_score=streak_s,
            composite=composite,
            triggered=composite >= self.ALERT_THRESHOLD,
        )
        if risk.triggered:
            print(
                f"[RISK ALERT] composite={composite:.2f} | "
                f"cost={cost_s:.2f} rate={rate_s:.2f} ratio={ratio_s:.2f} streak={streak_s:.2f}"
            )
        else:
            print(f"[RISK] composite={composite:.2f} (below threshold {self.ALERT_THRESHOLD})")
        return risk


client = anthropic.Anthropic()
detector = CompositeAnomalyDetector(
    normal_cost_usd=0.0003,
    normal_calls_per_min=5.0,
    normal_output_ratio=1.5,
)

def composite_monitored_call(message: str, max_tokens: int = 256) -> tuple[str, RiskScore]:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": message}],
    )
    cost = r.usage.input_tokens * INPUT_COST + r.usage.output_tokens * OUTPUT_COST
    record = CallRecord(
        timestamp=time.time(),
        input_tokens=r.usage.input_tokens,
        output_tokens=r.usage.output_tokens,
        cost_usd=cost,
    )
    risk = detector.observe(record)
    return r.content[0].text, risk


# Normal calls
for msg in ["What is Python?", "What is async?", "Explain OOP"]:
    text, risk = composite_monitored_call(msg)

# Anomalous calls
for msg in ["Write 1000 words about Python " * 5]:
    text, risk = composite_monitored_call(msg, max_tokens=1024)
    if risk.triggered:
        print("[ACTION] High composite risk — would trigger alert + throttle")

# Expected Token Savings: Multi-dimensional scoring reduces false positives; catches subtle anomalies missed by single signals
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Detection Method | Response | False Positive Risk | Best For |
|--------|-----------------|---------|---------------------|----------|
| 1. Z-Score | Statistical deviation | Alert | Medium | General anomaly detection |
| 2. Hard Budget | Cost ceiling | Hard stop | None | Budget-critical applications |
| 3. Rate Spike | Tokens per minute ratio | Auto-pause | Low | Loop/retry detection |
| 4. Prometheus Metrics | Structured metric export | External alert | N/A (alerting in Prometheus) | Production observability stacks |
| 5. EMA Baseline | Exponential moving average | Alert + action | Low (smooth baseline) | Gradual drift + sudden spikes |
| 6. Composite Score | Multi-signal weighted | Alert + throttle | Very low | Nuanced runaway detection |

**Recommended**: Option 2 (hard budget) as a non-negotiable safety net + Option 5 (EMA) for intelligent alerting. Option 4 for production systems with existing Prometheus/Grafana infrastructure.
