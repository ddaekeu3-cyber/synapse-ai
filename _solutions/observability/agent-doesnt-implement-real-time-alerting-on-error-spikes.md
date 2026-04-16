---
layout: solution
title: "Agent Doesn't Implement Real-Time Alerting on Error Spikes"
category: observability
description: "Agents that silently absorb errors give operators no signal when error rates spike. Real-time alerting detects statistically significant error increases and notifies on-call before users experience widespread failures."
tags: [observability, alerting, error-rate, monitoring, reliability, python]
---

## Problem

Without error spike detection, a surge in API failures, tool errors, or model degradation can affect thousands of users before anyone is notified. By the time a human notices the issue in logs, significant damage has already occurred. Real-time alerting watches error rates against baselines and fires alerts within seconds of a spike beginning.

## Solutions

### Option 1: Sliding Window Error Rate with Threshold Alert

```python
import anthropic
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

@dataclass
class ErrorEvent:
    timestamp: float
    is_error: bool
    error_type: Optional[str] = None

AlertHandler = Callable[[str, dict], None]

class SlidingWindowAlerter:
    def __init__(self, window_seconds: float = 60.0,
                 error_rate_threshold: float = 0.3,
                 min_samples: int = 5,
                 cooldown_seconds: float = 30.0):
        self._window = deque()
        self._window_sec = window_seconds
        self._threshold = error_rate_threshold
        self._min_samples = min_samples
        self._cooldown = cooldown_seconds
        self._last_alert_at: float = 0.0
        self._alert_handlers: list[AlertHandler] = []
        self._stats = {"total_events": 0, "total_errors": 0, "alerts_fired": 0}

    def add_handler(self, handler: AlertHandler) -> None:
        self._alert_handlers.append(handler)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_sec
        while self._window and self._window[0].timestamp < cutoff:
            self._window.popleft()

    def record(self, is_error: bool, error_type: Optional[str] = None) -> Optional[dict]:
        now = time.monotonic()
        event = ErrorEvent(now, is_error, error_type)
        self._window.append(event)
        self._prune(now)
        self._stats["total_events"] += 1
        if is_error:
            self._stats["total_errors"] += 1

        n = len(self._window)
        if n < self._min_samples:
            return None

        errors = sum(1 for e in self._window if e.is_error)
        error_rate = errors / n

        if error_rate >= self._threshold:
            in_cooldown = (now - self._last_alert_at) < self._cooldown
            if not in_cooldown:
                self._last_alert_at = now
                self._stats["alerts_fired"] += 1
                alert = {
                    "error_rate": error_rate,
                    "errors": errors,
                    "total": n,
                    "window_sec": self._window_sec,
                    "threshold": self._threshold,
                    "top_error": error_type or "unknown",
                }
                for handler in self._alert_handlers:
                    handler("error_rate_spike", alert)
                return alert
        return None

def log_alert(alert_type: str, detail: dict) -> None:
    rate_pct = detail["error_rate"] * 100
    print(f"🚨 [ALERT:{alert_type}] Error rate {rate_pct:.1f}% "
          f"({detail['errors']}/{detail['total']} in {detail['window_sec']:.0f}s window) "
          f"— top error: {detail['top_error']}")

def run_demo():
    client = anthropic.Anthropic()
    alerter = SlidingWindowAlerter(
        window_seconds=30.0, error_rate_threshold=0.25,
        min_samples=4, cooldown_seconds=10.0
    )
    alerter.add_handler(log_alert)

    def call_agent(prompt: str) -> bool:
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=50,
                messages=[{"role": "user", "content": prompt}],
            )
            alerter.record(is_error=False)
            return True
        except Exception as e:
            alerter.record(is_error=True, error_type=type(e).__name__)
            return False

    # Normal calls
    for i in range(4):
        ok = call_agent(f"What is {i}+{i}?")
        print(f"[call-{i}] {'OK' if ok else 'ERR'}")

    # Simulate error spike by recording synthetic errors
    print("\n--- Simulating error spike ---")
    for i in range(4):
        alerter.record(is_error=True, error_type="RateLimitError")
        print(f"[synthetic-error-{i}]")
        time.sleep(0.1)

    print(f"\nStats: {alerter._stats}")

if __name__ == "__main__":
    run_demo()

# Expected Token Savings: N/A — early alerting prevents prolonged degradation
# Environment: pip install anthropic
```

### Option 2: Async Error Monitor with Multiple Alert Channels

```python
import anthropic
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

class AlertSeverity(Enum):
    WARNING = "warning"   # 20-40% error rate
    CRITICAL = "critical" # >40% error rate
    RESOLVED = "resolved" # Back below threshold

@dataclass
class AlertEvent:
    alert_id: str
    severity: AlertSeverity
    error_rate: float
    sample_size: int
    window_sec: float
    fired_at: float = field(default_factory=time.time)

AsyncAlertHandler = Callable[[AlertEvent], Awaitable[None]]

class AsyncErrorAlerter:
    def __init__(self, window_sec: float = 60.0,
                 warn_threshold: float = 0.20,
                 crit_threshold: float = 0.40,
                 min_samples: int = 5):
        self._window = deque()
        self._window_sec = window_sec
        self._warn_threshold = warn_threshold
        self._crit_threshold = crit_threshold
        self._min_samples = min_samples
        self._current_severity: AlertSeverity = AlertSeverity.RESOLVED
        self._handlers: list[AsyncAlertHandler] = []
        self._lock = asyncio.Lock()
        self._alert_count = 0

    def add_handler(self, handler: AsyncAlertHandler) -> None:
        self._handlers.append(handler)

    async def record(self, is_error: bool) -> None:
        now = time.monotonic()
        async with self._lock:
            self._window.append((now, is_error))
            cutoff = now - self._window_sec
            while self._window and self._window[0][0] < cutoff:
                self._window.popleft()

            n = len(self._window)
            if n < self._min_samples:
                return

            errors = sum(1 for _, err in self._window if err)
            rate = errors / n
            new_severity = (
                AlertSeverity.CRITICAL if rate >= self._crit_threshold else
                AlertSeverity.WARNING if rate >= self._warn_threshold else
                AlertSeverity.RESOLVED
            )

            if new_severity != self._current_severity:
                self._current_severity = new_severity
                self._alert_count += 1
                event = AlertEvent(
                    alert_id=f"alert-{self._alert_count:04d}",
                    severity=new_severity, error_rate=rate,
                    sample_size=n, window_sec=self._window_sec,
                )
                await asyncio.gather(*[h(event) for h in self._handlers],
                                     return_exceptions=True)

async def console_handler(event: AlertEvent) -> None:
    icons = {AlertSeverity.WARNING: "⚠️", AlertSeverity.CRITICAL: "🔴", AlertSeverity.RESOLVED: "✅"}
    icon = icons[event.severity]
    print(f"\n{icon} [{event.alert_id}] {event.severity.value.upper()} "
          f"error_rate={event.error_rate:.1%} "
          f"samples={event.sample_size} window={event.window_sec:.0f}s")

async def pagerduty_stub(event: AlertEvent) -> None:
    """Stub for PagerDuty/Opsgenie/Slack webhook."""
    if event.severity == AlertSeverity.CRITICAL:
        print(f"  [PagerDuty] Would page on-call for {event.alert_id}")

async def run_agent_calls(client: anthropic.AsyncAnthropic,
                           alerter: AsyncErrorAlerter) -> None:
    prompts = [
        ("What is 2+2?", False),
        ("Name a planet.", False),
        ("What color is grass?", False),
        # Simulate failures
        (None, True), (None, True), (None, True), (None, True),
        # Recovery
        ("What is gravity?", False),
        ("Name an element.", False),
        ("Define recursion.", False),
    ]

    for prompt, force_error in prompts:
        if force_error:
            await alerter.record(is_error=True)
            print("[synthetic error]", end=" ")
        else:
            try:
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=30,
                    messages=[{"role": "user", "content": prompt}],
                )
                await alerter.record(is_error=False)
                print(f"[OK] {resp.content[0].text[:30]}", end=" ")
            except Exception as e:
                await alerter.record(is_error=True)
                print(f"[ERR] {e}", end=" ")
        await asyncio.sleep(0.2)

async def main():
    client = anthropic.AsyncAnthropic()
    alerter = AsyncErrorAlerter(
        window_sec=30.0, warn_threshold=0.25,
        crit_threshold=0.40, min_samples=4
    )
    alerter.add_handler(console_handler)
    alerter.add_handler(pagerduty_stub)

    await run_agent_calls(client, alerter)
    print(f"\n\nTotal alerts fired: {alerter._alert_count}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — alerting enables faster incident response
# Environment: pip install anthropic
```

### Option 3: Statistical Anomaly Detection with Burn Rate

```python
import anthropic
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class BurnRateAlert:
    """SLO burn rate: how fast the error budget is being consumed."""
    slo_target: float = 0.99     # 99% success rate SLO
    error_budget: float = 0.01   # 1% allowed errors
    window_short: float = 60.0   # 1-minute window
    window_long: float = 300.0   # 5-minute window
    # Alert if consuming budget N× faster than sustainable
    fast_burn_multiplier: float = 14.4   # 1h burn at this rate = 1% budget
    slow_burn_multiplier: float = 6.0

class BurnRateAlerter:
    def __init__(self, config: BurnRateAlert = BurnRateAlert()):
        self.cfg = config
        self._events_short: deque = deque()  # (timestamp, is_error)
        self._events_long: deque = deque()
        self._alerts: list[dict] = []

    def _prune(self, events: deque, window: float, now: float) -> None:
        cutoff = now - window
        while events and events[0][0] < cutoff:
            events.popleft()

    def _error_rate(self, events: deque) -> Optional[float]:
        if len(events) < 3:
            return None
        errors = sum(1 for _, e in events if e)
        return errors / len(events)

    def _burn_rate(self, error_rate: Optional[float]) -> Optional[float]:
        if error_rate is None:
            return None
        return error_rate / self.cfg.error_budget

    def record(self, is_error: bool) -> Optional[dict]:
        now = time.monotonic()
        self._events_short.append((now, is_error))
        self._events_long.append((now, is_error))
        self._prune(self._events_short, self.cfg.window_short, now)
        self._prune(self._events_long, self.cfg.window_long, now)

        short_rate = self._error_rate(self._events_short)
        long_rate = self._error_rate(self._events_long)
        short_burn = self._burn_rate(short_rate)
        long_burn = self._burn_rate(long_rate)

        alert = None
        if (short_burn is not None and long_burn is not None and
                short_burn > self.cfg.fast_burn_multiplier and
                long_burn > self.cfg.fast_burn_multiplier):
            alert = {
                "type": "fast_burn", "severity": "page",
                "short_burn": short_burn, "long_burn": long_burn,
                "short_error_rate": short_rate, "long_error_rate": long_rate,
            }
            self._alerts.append(alert)
            print(f"🔥 [FAST BURN] {short_burn:.1f}× burn rate "
                  f"(short={short_rate:.1%}, long={long_rate:.1%}) — PAGE ON-CALL")
        elif (short_burn is not None and long_burn is not None and
              short_burn > self.cfg.slow_burn_multiplier and
              long_burn > self.cfg.slow_burn_multiplier):
            alert = {
                "type": "slow_burn", "severity": "ticket",
                "short_burn": short_burn, "long_burn": long_burn,
            }
            self._alerts.append(alert)
            print(f"⚠️ [SLOW BURN] {short_burn:.1f}× burn rate — CREATE TICKET")

        return alert

    @property
    def alert_count(self) -> int:
        return len(self._alerts)

def run_demo():
    client = anthropic.Anthropic()
    alerter = BurnRateAlerter(BurnRateAlert(
        slo_target=0.99,
        fast_burn_multiplier=3.0,  # Lowered for demo
        slow_burn_multiplier=1.5,
    ))

    def call(prompt: Optional[str], force_error: bool = False) -> None:
        if force_error:
            alerter.record(is_error=True)
            print("[ERR]", end=" ", flush=True)
            return
        try:
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=20,
                messages=[{"role": "user", "content": prompt}],
            )
            alerter.record(is_error=False)
            print("[OK]", end=" ", flush=True)
        except Exception:
            alerter.record(is_error=True)
            print("[ERR]", end=" ", flush=True)

    # Healthy period
    for i in range(5):
        call(f"What is {i}?")

    print("\n--- Error spike ---")
    for _ in range(8):
        call(None, force_error=True)

    print(f"\n\nTotal burn rate alerts: {alerter.alert_count}")

if __name__ == "__main__":
    run_demo()

# Expected Token Savings: Burn rate alerting catches slow degradation before budget exhaustion
# Environment: pip install anthropic
```

### Option 4: Error Categorization with Per-Type Thresholds

```python
import anthropic
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ErrorCategory:
    name: str
    threshold_rate: float  # Alert when this error type exceeds % of total calls
    window_sec: float = 60.0
    severity: str = "warning"

ERROR_CATEGORIES = [
    ErrorCategory("RateLimitError",    threshold_rate=0.10, severity="critical"),
    ErrorCategory("AuthenticationError", threshold_rate=0.01, severity="critical"),
    ErrorCategory("APIConnectionError", threshold_rate=0.20, severity="warning"),
    ErrorCategory("APIStatusError",   threshold_rate=0.15, severity="warning"),
    ErrorCategory("TimeoutError",     threshold_rate=0.15, severity="warning"),
]

class CategorizedErrorAlerter:
    def __init__(self):
        self._total_window: deque = deque()
        self._error_windows: dict[str, deque] = defaultdict(deque)
        self._categories = {c.name: c for c in ERROR_CATEGORIES}
        self._fired: set[str] = set()

    def _prune_all(self, now: float) -> None:
        cutoff = time.monotonic() - 60.0
        while self._total_window and self._total_window[0] < cutoff:
            self._total_window.popleft()
        for window in self._error_windows.values():
            while window and window[0] < cutoff:
                window.popleft()

    def record(self, error_type: Optional[str] = None) -> list[dict]:
        now = time.monotonic()
        self._total_window.append(now)
        if error_type:
            self._error_windows[error_type].append(now)
        self._prune_all(now)

        total = len(self._total_window)
        if total < 5:
            return []

        alerts = []
        for cat_name, category in self._categories.items():
            errors = len(self._error_windows.get(cat_name, deque()))
            rate = errors / total
            alert_key = f"{cat_name}:{rate > category.threshold_rate}"

            if rate > category.threshold_rate and alert_key not in self._fired:
                self._fired.add(alert_key)
                alert = {
                    "category": cat_name, "rate": rate,
                    "threshold": category.threshold_rate,
                    "total_calls": total, "error_calls": errors,
                    "severity": category.severity,
                }
                alerts.append(alert)
                icon = "🔴" if category.severity == "critical" else "⚠️"
                print(f"{icon} [{category.severity.upper()}] {cat_name} rate={rate:.1%} "
                      f"threshold={category.threshold_rate:.1%} "
                      f"({errors}/{total} calls)")
            elif rate <= category.threshold_rate:
                self._fired.discard(alert_key)

        return alerts

def run_demo():
    client = anthropic.Anthropic()
    alerter = CategorizedErrorAlerter()

    def call_agent(prompt: str, inject_error: Optional[str] = None) -> None:
        if inject_error:
            alerter.record(error_type=inject_error)
            print(f"[{inject_error[:15]}]", end=" ")
            return
        try:
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=20,
                messages=[{"role": "user", "content": prompt}],
            )
            alerter.record()
            print("[OK]", end=" ")
        except anthropic.RateLimitError:
            alerter.record(error_type="RateLimitError")
            print("[RateLimit]", end=" ")
        except Exception as e:
            alerter.record(error_type=type(e).__name__)
            print(f"[{type(e).__name__[:12]}]", end=" ")

    # Normal traffic
    for i in range(5):
        call_agent(f"What is {i}+1?")

    print("\n--- Injecting rate limit errors ---")
    for _ in range(3):
        call_agent("", inject_error="RateLimitError")

    print("\n--- More normal traffic ---")
    for i in range(3):
        call_agent(f"Name item {i}.")

if __name__ == "__main__":
    run_demo()

# Expected Token Savings: N/A — category-specific alerts allow faster targeted response
# Environment: pip install anthropic
```

### Option 5: Async Alert Aggregator with Deduplication

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Alert:
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    fingerprint: str = ""   # Dedup key
    title: str = ""
    severity: str = "warning"
    error_rate: float = 0.0
    fired_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    fire_count: int = 1     # How many times this fingerprint has fired

class AlertAggregator:
    """Deduplicates repeated alerts; groups them into incidents."""
    def __init__(self, dedup_window_sec: float = 300.0):
        self._active: dict[str, Alert] = {}
        self._dedup_window = dedup_window_sec
        self._lock = asyncio.Lock()
        self._total_fired = 0

    async def fire(self, title: str, severity: str, error_rate: float,
                   fingerprint: str) -> Optional[Alert]:
        """Returns Alert only if it's new (not deduplicated)."""
        async with self._lock:
            now = time.time()
            if fingerprint in self._active:
                existing = self._active[fingerprint]
                if now - existing.fired_at < self._dedup_window:
                    existing.fire_count += 1
                    print(f"[DEDUP] {fingerprint} suppressed "
                          f"(fired {existing.fire_count}× since {existing.fired_at:.0f})")
                    return None  # Suppressed
                # Outside dedup window — re-fire
            alert = Alert(fingerprint=fingerprint, title=title,
                          severity=severity, error_rate=error_rate)
            self._active[fingerprint] = alert
            self._total_fired += 1
            return alert

    async def resolve(self, fingerprint: str) -> Optional[Alert]:
        async with self._lock:
            alert = self._active.pop(fingerprint, None)
            if alert:
                alert.resolved_at = time.time()
                duration = alert.resolved_at - alert.fired_at
                print(f"✅ [RESOLVED] {fingerprint} after {duration:.0f}s "
                      f"(fired {alert.fire_count}× total)")
            return alert

    @property
    def active_count(self) -> int:
        return len(self._active)

class ErrorRateMonitor:
    def __init__(self, aggregator: AlertAggregator, threshold: float = 0.25,
                 window_sec: float = 60.0):
        self._aggregator = aggregator
        self._threshold = threshold
        self._window_sec = window_sec
        self._events: list[tuple[float, bool]] = []
        self._lock = asyncio.Lock()

    async def record(self, is_error: bool, label: str = "default") -> None:
        now = time.monotonic()
        async with self._lock:
            self._events.append((now, is_error))
            cutoff = now - self._window_sec
            self._events = [(t, e) for t, e in self._events if t >= cutoff]

            n = len(self._events)
            if n < 5:
                return
            errors = sum(1 for _, e in self._events if e)
            rate = errors / n
            fingerprint = f"high_error_rate:{label}"

            if rate >= self._threshold:
                alert = await self._aggregator.fire(
                    title=f"High error rate on {label}",
                    severity="critical" if rate >= 0.5 else "warning",
                    error_rate=rate, fingerprint=fingerprint,
                )
                if alert:
                    icon = "🔴" if alert.severity == "critical" else "⚠️"
                    print(f"\n{icon} [NEW ALERT:{alert.alert_id}] {alert.title} "
                          f"rate={rate:.1%}")
            else:
                await self._aggregator.resolve(fingerprint)

async def main():
    client = anthropic.AsyncAnthropic()
    aggregator = AlertAggregator(dedup_window_sec=60.0)
    monitor = ErrorRateMonitor(aggregator, threshold=0.30, window_sec=30.0)

    async def call(prompt: Optional[str], force_error: bool = False) -> None:
        if force_error:
            await monitor.record(is_error=True, label="production")
            print("E", end="", flush=True)
            return
        try:
            await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=15,
                messages=[{"role": "user", "content": prompt}],
            )
            await monitor.record(is_error=False, label="production")
            print(".", end="", flush=True)
        except Exception:
            await monitor.record(is_error=True, label="production")
            print("E", end="", flush=True)

    # Good traffic
    for i in range(4):
        await call(f"Say '{i}'")

    # Error spike
    for _ in range(5):
        await call(None, force_error=True)
        await asyncio.sleep(0.1)

    # More errors — should be deduplicated
    for _ in range(3):
        await call(None, force_error=True)
        await asyncio.sleep(0.1)

    # Recovery
    for i in range(4):
        await call(f"What is {i}?")
        await asyncio.sleep(0.1)

    print(f"\n\nActive alerts: {aggregator.active_count} | "
          f"Total fired: {aggregator._total_fired}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — dedup prevents alert fatigue without missing new incidents
# Environment: pip install anthropic
```

### Option 6: Prometheus-Compatible Metrics with Alert Rules

```python
import anthropic
import time
import asyncio
from dataclasses import dataclass, field
from collections import deque
from typing import Optional

@dataclass
class Counter:
    name: str
    labels: dict = field(default_factory=dict)
    _value: float = 0.0

    def inc(self, amount: float = 1.0) -> None:
        self._value += amount

    @property
    def value(self) -> float:
        return self._value

@dataclass
class Gauge:
    name: str
    _value: float = 0.0

    def set(self, value: float) -> None:
        self._value = value

    @property
    def value(self) -> float:
        return self._value

class MetricsRegistry:
    def __init__(self):
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}

    def counter(self, name: str, **labels) -> Counter:
        key = f"{name}{sorted(labels.items())}"
        if key not in self._counters:
            self._counters[key] = Counter(name, labels)
        return self._counters[key]

    def gauge(self, name: str) -> Gauge:
        if name not in self._gauges:
            self._gauges[name] = Gauge(name)
        return self._gauges[name]

    def exposition(self) -> str:
        """Prometheus text format."""
        lines = []
        for c in self._counters.values():
            label_str = ",".join(f'{k}="{v}"' for k, v in c.labels.items())
            lines.append(f"{c.name}{{{label_str}}} {c.value}")
        for g in self._gauges.values():
            lines.append(f"{g.name} {g.value}")
        return "\n".join(lines)

@dataclass
class AlertRule:
    name: str
    expr: str      # Human-readable description
    threshold: float
    for_seconds: float  # Must be true for this long before alerting
    severity: str
    _triggered_since: Optional[float] = None
    _fired: bool = False

    def evaluate(self, value: float, now: float) -> Optional[str]:
        exceeds = value > self.threshold
        if exceeds:
            if self._triggered_since is None:
                self._triggered_since = now
            elif (now - self._triggered_since >= self.for_seconds) and not self._fired:
                self._fired = True
                return f"[ALERT:{self.severity.upper()}] {self.name}: {value:.3f} > {self.threshold}"
        else:
            if self._fired:
                self._fired = False
                self._triggered_since = None
                return f"[RESOLVED] {self.name}"
            self._triggered_since = None
        return None

class PrometheusAlerter:
    def __init__(self):
        self.registry = MetricsRegistry()
        self._total = self.registry.counter("agent_requests_total")
        self._errors = self.registry.counter("agent_errors_total")
        self._error_rate = self.registry.gauge("agent_error_rate")
        self._window: deque = deque()

        self._rules = [
            AlertRule("HighErrorRate",    "error_rate > 0.25", 0.25, 10.0, "warning"),
            AlertRule("CriticalErrorRate","error_rate > 0.50", 0.50, 5.0,  "critical"),
        ]

    def record(self, is_error: bool) -> list[str]:
        now = time.monotonic()
        self._total.inc()
        if is_error:
            self._errors.inc()

        self._window.append((now, is_error))
        while self._window and self._window[0][0] < now - 60.0:
            self._window.popleft()

        n = len(self._window)
        rate = sum(1 for _, e in self._window if e) / n if n > 0 else 0.0
        self._error_rate.set(rate)

        fired = []
        for rule in self._rules:
            msg = rule.evaluate(rate, now)
            if msg:
                print(msg)
                fired.append(msg)
        return fired

    def metrics(self) -> str:
        return self.registry.exposition()

def run_demo():
    client = anthropic.Anthropic()
    alerter = PrometheusAlerter()

    def call(prompt: Optional[str], force_error: bool = False) -> None:
        if force_error:
            alerter.record(is_error=True)
            return
        try:
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=15,
                messages=[{"role": "user", "content": prompt or "Hi"}],
            )
            alerter.record(is_error=False)
        except Exception:
            alerter.record(is_error=True)

    for i in range(5):
        call(f"What is {i}?")
        print(".", end="", flush=True)

    print("\n--- Error spike ---")
    for _ in range(10):
        call(None, force_error=True)
        time.sleep(0.5)
        print("E", end="", flush=True)

    print("\n--- Recovery ---")
    for i in range(5):
        call(f"Name item {i}.")
        print(".", end="", flush=True)

    print(f"\n\nMetrics:\n{alerter.metrics()}")

if __name__ == "__main__":
    run_demo()

# Expected Token Savings: N/A — Prometheus-compatible metrics enable integration with Grafana/AlertManager
# Environment: pip install anthropic
```

## Comparison

| Option | Detection Method | Alert Channels | Deduplication | Best For |
|--------|-----------------|----------------|---------------|----------|
| 1. Sliding window | Error rate % | Callback | Cooldown | Simple threshold alerts |
| 2. Async multi-channel | Error rate % | Multi-handler | State machine | Production on-call |
| 3. Burn rate | SLO-relative | Console | None | SLO-based operations |
| 4. Per-category | Type-specific rate | Console | Active set | Mixed error types |
| 5. Aggregator | Error rate % | Async handlers | Fingerprint | Alert fatigue reduction |
| 6. Prometheus | Error rate gauge | AlertManager | Rule duration | Observability stack |
