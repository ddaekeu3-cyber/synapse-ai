---
title: "Agent Doesn't Implement Adaptive Sampling for High-Volume Traces"
description: "Agents that emit a trace span for every operation quickly overwhelm storage and ingestion pipelines at scale, while agents that disable tracing entirely lose the visibility needed to debug production issues."
difficulty: intermediate
category: observability
tags: [sampling, tracing, observability, opentelemetry, tail-sampling, adaptive, performance]
---

## Problem

At low volume, emitting every trace is fine. At production scale — thousands of agent invocations per second — recording every span bloats storage, saturates trace ingestion endpoints, and adds measurable latency to hot paths. The naive fix (sample 1% uniformly) discards 99% of errors and slow outliers, which are exactly the traces you need. Adaptive sampling keeps what matters and discards the rest.

```python
# Broken: emit every span → overwhelms backend at scale
from opentelemetry import trace

tracer = trace.get_tracer("agent")

async def handle_request(request: dict) -> dict:
    with tracer.start_as_current_span("handle_request") as span:
        span.set_attribute("request.id", request["id"])
        result = await process(request)  # every call emits a full trace
        return result
```

---

## Solution 1: Head-Based Probabilistic Sampling with Rate Control

```python
import random
import time
from dataclasses import dataclass, field
from threading import Lock

@dataclass
class RateControlledSampler:
    """
    Sample at most `target_per_second` traces per second.
    Uses token-bucket to enforce the rate cap.
    """
    target_per_second: float
    _tokens: float = field(default=0.0, init=False)
    _last_refill: float = field(default_factory=time.monotonic, init=False)
    _lock: Lock = field(default_factory=Lock, init=False)

    def should_sample(self) -> bool:
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._last_refill
            self._tokens = min(
                self.target_per_second,
                self._tokens + elapsed * self.target_per_second
            )
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
        return False

class ProbabilisticSampler:
    """Sample a fixed fraction of traces (head-based decision)."""

    def __init__(self, rate: float = 0.1):
        assert 0.0 <= rate <= 1.0
        self.rate = rate

    def should_sample(self, trace_id: str | None = None) -> bool:
        if trace_id:
            # Deterministic: same trace ID always makes the same decision
            # (important for distributed tracing consistency)
            hash_val = int(trace_id.replace("-", ""), 16) % 10000
            return hash_val < int(self.rate * 10000)
        return random.random() < self.rate

class CompositeSampler:
    """
    Always sample if rate_sampler allows; fall back to probabilistic.
    Guarantees at least N traces/sec even when load is low.
    """

    def __init__(self, rate_sampler: RateControlledSampler,
                 probabilistic_sampler: ProbabilisticSampler):
        self.rate_sampler = rate_sampler
        self.probabilistic_sampler = probabilistic_sampler

    def should_sample(self, trace_id: str | None = None) -> bool:
        return (self.rate_sampler.should_sample() or
                self.probabilistic_sampler.should_sample(trace_id))

# OpenTelemetry-compatible sampler
from opentelemetry.sdk.trace.sampling import Sampler, SamplingResult, Decision
from opentelemetry.trace import SpanContext, SpanKind
from opentelemetry.util.types import Attributes

class RateLimitingSampler(Sampler):
    def __init__(self, max_per_second: float = 100.0):
        self._rate_sampler = RateControlledSampler(max_per_second)

    def should_sample(self, parent_context, trace_id: int,
                      name: str, kind: SpanKind | None = None,
                      attributes: Attributes = None,
                      links=None, trace_state=None) -> SamplingResult:
        if self._rate_sampler.should_sample():
            return SamplingResult(Decision.RECORD_AND_SAMPLE)
        return SamplingResult(Decision.DROP)

    def get_description(self) -> str:
        return f"RateLimitingSampler({self._rate_sampler.target_per_second}/s)"
```

---

## Solution 2: Tail-Based Sampling (Buffer, Decide at Completion)

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class SpanBuffer:
    trace_id: str
    spans: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    completed: bool = False
    had_error: bool = False
    max_duration_ms: float = 0.0

class TailBasedSampler:
    """
    Buffer all spans for a trace until the root span completes,
    then decide whether to keep or drop the entire trace based on
    its outcome (error, slow, interesting).
    """

    def __init__(self,
                 buffer_timeout: float = 30.0,
                 keep_errors: bool = True,
                 keep_slow_threshold_ms: float = 5000.0,
                 baseline_sample_rate: float = 0.05):
        self._buffers: dict[str, SpanBuffer] = {}
        self._lock = asyncio.Lock()
        self.buffer_timeout = buffer_timeout
        self.keep_errors = keep_errors
        self.keep_slow_threshold_ms = keep_slow_threshold_ms
        self.baseline_sample_rate = baseline_sample_rate
        self._exporters: list[Callable[[list[dict]], None]] = []

    def add_exporter(self, fn: Callable[[list[dict]], None]):
        self._exporters.append(fn)

    async def record_span(self, span: dict):
        trace_id = span["trace_id"]
        async with self._lock:
            if trace_id not in self._buffers:
                self._buffers[trace_id] = SpanBuffer(trace_id=trace_id)
            buf = self._buffers[trace_id]
            buf.spans.append(span)

            if span.get("status") == "error":
                buf.had_error = True

            duration = span.get("duration_ms", 0)
            buf.max_duration_ms = max(buf.max_duration_ms, duration)

            if span.get("is_root"):
                buf.completed = True
                await self._evaluate(trace_id, buf)

    async def _evaluate(self, trace_id: str, buf: SpanBuffer):
        keep = self._should_keep(buf)
        if keep:
            for exporter in self._exporters:
                exporter(buf.spans)
        del self._buffers[trace_id]

    def _should_keep(self, buf: SpanBuffer) -> bool:
        # Always keep error traces
        if self.keep_errors and buf.had_error:
            return True
        # Always keep slow traces
        if buf.max_duration_ms >= self.keep_slow_threshold_ms:
            return True
        # Baseline sample for "normal" traces
        import random
        return random.random() < self.baseline_sample_rate

    async def flush_stale(self):
        """Periodically flush timed-out incomplete traces."""
        now = time.monotonic()
        async with self._lock:
            stale = [
                tid for tid, buf in self._buffers.items()
                if now - buf.started_at > self.buffer_timeout
            ]
            for tid in stale:
                buf = self._buffers.pop(tid)
                # Treat timed-out traces like normal for sampling decision
                if self._should_keep(buf):
                    for exporter in self._exporters:
                        exporter(buf.spans)

    async def flush_loop(self, interval: float = 10.0):
        while True:
            await asyncio.sleep(interval)
            await self.flush_stale()
```

---

## Solution 3: Priority Sampling — Always Sample What Matters

```python
import enum
from dataclasses import dataclass
from typing import Any

class TracePriority(enum.IntEnum):
    DROP    = 0
    LOW     = 1
    NORMAL  = 2
    HIGH    = 3
    ALWAYS  = 4  # never dropped regardless of rate

@dataclass
class SamplingDecision:
    priority: TracePriority
    reason: str
    sample_rate: float  # effective rate applied

class PrioritySampler:
    """
    Classify traces into priority tiers before the sampling decision.
    HIGH and ALWAYS tiers bypass rate limiting.
    """

    def __init__(self, normal_rate: float = 0.1, low_rate: float = 0.01):
        self.normal_rate = normal_rate
        self.low_rate = low_rate
        self._rate_limiter = RateControlledSampler(target_per_second=200.0)

    def classify(self, span_attrs: dict[str, Any]) -> SamplingDecision:
        # ALWAYS: user-flagged debug traces
        if span_attrs.get("debug") or span_attrs.get("force_sample"):
            return SamplingDecision(TracePriority.ALWAYS, "force_sample", 1.0)

        # HIGH: errors and exceptions
        if span_attrs.get("error") or span_attrs.get("exception.type"):
            return SamplingDecision(TracePriority.HIGH, "error", 1.0)

        # HIGH: slow operations (pre-estimated)
        estimated_ms = span_attrs.get("estimated_duration_ms", 0)
        if estimated_ms > 3000:
            return SamplingDecision(TracePriority.HIGH, "slow_estimated", 1.0)

        # HIGH: critical business operations
        operation = span_attrs.get("operation", "")
        if operation in {"payment", "auth", "data_deletion", "compliance_check"}:
            return SamplingDecision(TracePriority.HIGH, "critical_operation", 1.0)

        # LOW: health checks, internal probes
        if operation in {"health_check", "ping", "metrics_scrape"}:
            return SamplingDecision(TracePriority.LOW, "low_value", self.low_rate)

        # NORMAL: everything else
        return SamplingDecision(TracePriority.NORMAL, "default", self.normal_rate)

    def should_sample(self, span_attrs: dict[str, Any]) -> tuple[bool, SamplingDecision]:
        decision = self.classify(span_attrs)

        if decision.priority == TracePriority.ALWAYS:
            return True, decision
        if decision.priority == TracePriority.HIGH:
            # Rate-limit HIGH priority to prevent error storms from overwhelming storage
            return self._rate_limiter.should_sample(), decision
        if decision.priority == TracePriority.LOW:
            import random
            return random.random() < self.low_rate, decision
        if decision.priority == TracePriority.DROP:
            return False, decision

        # NORMAL: probabilistic
        import random
        return random.random() < self.normal_rate, decision

# OpenTelemetry-compatible wrapper
from opentelemetry.sdk.trace.sampling import Sampler, SamplingResult, Decision
from opentelemetry.trace.span import TraceState

class PrioritySamplerOTel(Sampler):
    def __init__(self):
        self._sampler = PrioritySampler()

    def should_sample(self, parent_context, trace_id: int,
                      name: str, kind=None, attributes=None,
                      links=None, trace_state=None) -> SamplingResult:
        attrs = dict(attributes or {})
        attrs["operation"] = name
        keep, decision = self._sampler.should_sample(attrs)

        if keep:
            return SamplingResult(
                Decision.RECORD_AND_SAMPLE,
                attributes={"sampling.priority": decision.priority.name,
                            "sampling.reason": decision.reason}
            )
        return SamplingResult(Decision.DROP)

    def get_description(self) -> str:
        return "PrioritySampler"
```

---

## Solution 4: Adaptive Rate Adjustment (Target N Traces/Sec)

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

@dataclass
class AdaptiveSampler:
    """
    Continuously adjusts the sample rate to hit a target trace/sec throughput.
    Uses a PID-like controller: if observed rate > target, decrease rate; if < target, increase.
    """
    target_traces_per_second: float
    min_rate: float = 0.001
    max_rate: float = 1.0
    adjustment_interval: float = 5.0   # seconds between adjustments
    observation_window: float = 10.0   # seconds of history to measure

    _current_rate: float = field(init=False)
    _observation: deque = field(default_factory=deque, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        self._current_rate = min(
            self.max_rate,
            self.target_traces_per_second / 100.0  # conservative start
        )
        self._observation = deque()

    async def should_sample(self) -> bool:
        import random
        sampled = random.random() < self._current_rate
        if sampled:
            async with self._lock:
                self._observation.append(time.monotonic())
        return sampled

    def _observed_rate(self) -> float:
        now = time.monotonic()
        cutoff = now - self.observation_window
        # Drop old observations
        while self._observation and self._observation[0] < cutoff:
            self._observation.popleft()
        count = len(self._observation)
        return count / self.observation_window

    async def adjust_loop(self):
        """Background loop that adjusts sample rate every `adjustment_interval` seconds."""
        while True:
            await asyncio.sleep(self.adjustment_interval)
            async with self._lock:
                observed = self._observed_rate()
                if observed <= 0:
                    continue

                # Proportional adjustment
                error_ratio = self.target_traces_per_second / max(observed, 0.001)
                new_rate = self._current_rate * error_ratio

                # Clamp and apply
                new_rate = max(self.min_rate, min(self.max_rate, new_rate))
                old_rate = self._current_rate
                self._current_rate = new_rate

                if abs(new_rate - old_rate) / max(old_rate, 0.001) > 0.05:
                    print(f"[AdaptiveSampler] Rate adjusted: "
                          f"{old_rate:.4f} → {new_rate:.4f} "
                          f"(observed {observed:.1f}/s, "
                          f"target {self.target_traces_per_second}/s)")

    @property
    def current_rate(self) -> float:
        return self._current_rate
```

---

## Solution 5: Reservoir Sampling for Guaranteed Representation

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ReservoirSampler:
    """
    Maintains a fixed-size reservoir of traces using Algorithm R.
    Guarantees uniform representation regardless of arrival order.
    Useful for offline analysis where you want a representative sample
    of all operations, not biased toward recent or high-volume.
    """
    reservoir_size: int = 1000
    _reservoir: list[dict] = field(default_factory=list, init=False)
    _count: int = field(default=0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def observe(self, trace: dict) -> bool:
        """
        Add trace to reservoir using Algorithm R.
        Returns True if the trace was kept.
        """
        async with self._lock:
            self._count += 1
            if len(self._reservoir) < self.reservoir_size:
                self._reservoir.append(trace)
                return True
            # Replace random element with decreasing probability
            j = random.randint(0, self._count - 1)
            if j < self.reservoir_size:
                self._reservoir[j] = trace
                return True
            return False

    async def flush(self) -> list[dict]:
        """Return and clear the reservoir."""
        async with self._lock:
            result = list(self._reservoir)
            self._reservoir.clear()
            self._count = 0
            return result

    @property
    def fill_ratio(self) -> float:
        return len(self._reservoir) / self.reservoir_size

class StratifiedReservoirSampler:
    """
    Separate reservoirs per operation type, ensuring rare operations
    are represented even if they're 0.1% of traffic.
    """

    def __init__(self, per_stratum_size: int = 100):
        self.per_stratum_size = per_stratum_size
        self._reservoirs: dict[str, ReservoirSampler] = {}
        self._lock = asyncio.Lock()

    async def observe(self, trace: dict) -> bool:
        operation = trace.get("operation", "unknown")
        async with self._lock:
            if operation not in self._reservoirs:
                self._reservoirs[operation] = ReservoirSampler(self.per_stratum_size)
            sampler = self._reservoirs[operation]

        return await sampler.observe(trace)

    async def flush_all(self) -> dict[str, list[dict]]:
        result = {}
        async with self._lock:
            keys = list(self._reservoirs.keys())
        for key in keys:
            result[key] = await self._reservoirs[key].flush()
        return result

    def stratum_stats(self) -> dict[str, dict]:
        return {
            op: {"fill_ratio": s.fill_ratio, "count": s._count}
            for op, s in self._reservoirs.items()
        }
```

---

## Solution 6: Per-Endpoint Sampling with Cardinality Controls

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

@dataclass
class EndpointSamplingConfig:
    name: str
    sample_rate: float
    max_per_minute: int = 600
    always_sample_errors: bool = True

DEFAULT_CONFIGS: dict[str, EndpointSamplingConfig] = {
    "health_check":       EndpointSamplingConfig("health_check", 0.001, 10),
    "metrics":            EndpointSamplingConfig("metrics",       0.001, 10),
    "tool_call":          EndpointSamplingConfig("tool_call",     0.1,   300),
    "llm_request":        EndpointSamplingConfig("llm_request",   0.5,   600),
    "payment":            EndpointSamplingConfig("payment",       1.0,   999_999),
    "auth":               EndpointSamplingConfig("auth",          1.0,   999_999),
}

class PerEndpointSampler:
    """
    Apply different sample rates per endpoint/operation type.
    Prevents high-frequency cheap operations from crowding out
    low-frequency important ones in trace storage.
    """

    def __init__(self, configs: dict[str, EndpointSamplingConfig] | None = None,
                 default_rate: float = 0.1,
                 default_max_per_minute: int = 300):
        self._configs = configs or DEFAULT_CONFIGS
        self._default = EndpointSamplingConfig("default", default_rate,
                                               default_max_per_minute)
        self._window_counts: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def _get_config(self, endpoint: str) -> EndpointSamplingConfig:
        return self._configs.get(endpoint, self._default)

    async def should_sample(self, endpoint: str,
                            attrs: dict[str, Any] | None = None) -> bool:
        import random
        cfg = self._get_config(endpoint)
        attrs = attrs or {}

        # Always sample errors (if configured)
        if cfg.always_sample_errors and attrs.get("error"):
            return True

        # Check per-minute rate limit
        async with self._lock:
            now = time.monotonic()
            window = self._window_counts[endpoint]
            # Prune entries older than 60 seconds
            cutoff = now - 60.0
            while window and window[0] < cutoff:
                window.pop(0)

            if len(window) >= cfg.max_per_minute:
                return False

            # Probabilistic check
            if random.random() >= cfg.sample_rate:
                return False

            window.append(now)
            return True

    def configure_endpoint(self, endpoint: str, rate: float,
                           max_per_minute: int = 300):
        self._configs[endpoint] = EndpointSamplingConfig(
            endpoint, rate, max_per_minute
        )

    def sampling_stats(self) -> dict[str, dict]:
        return {
            endpoint: {
                "config_rate": self._get_config(endpoint).sample_rate,
                "sampled_last_minute": len(times)
            }
            for endpoint, times in self._window_counts.items()
        }

# Unified sampling facade combining all strategies
class AgentTraceSampler:
    """
    Production-grade sampling facade that combines:
    - Per-endpoint configuration
    - Priority classification
    - Adaptive rate adjustment
    """

    def __init__(self,
                 target_per_second: float = 100.0,
                 endpoint_configs: dict | None = None):
        self._endpoint = PerEndpointSampler(endpoint_configs)
        self._adaptive = AdaptiveSampler(target_traces_per_second=target_per_second)
        self._priority = PrioritySampler()

    async def start(self):
        asyncio.create_task(self._adaptive.adjust_loop())

    async def should_sample(self, operation: str,
                            attrs: dict[str, Any] | None = None) -> bool:
        attrs = attrs or {}
        attrs["operation"] = operation

        # Priority check first (errors, critical operations always sampled)
        keep, decision = self._priority.should_sample(attrs)
        if decision.priority.value >= TracePriority.HIGH:
            return keep

        # Endpoint-level rate check
        if not await self._endpoint.should_sample(operation, attrs):
            return False

        # Global adaptive rate check
        return await self._adaptive.should_sample()
```

---

## Comparison

| Solution | Latency Impact | Storage Control | Error Coverage | Complexity | Best For |
|---|---|---|---|---|---|
| 1. Head-based probabilistic | None (decide upfront) | Good | Poor (misses errors) | Low | Simple rate control |
| 2. Tail-based | Buffering overhead | Excellent | Excellent | High | Error/latency-driven retention |
| 3. Priority sampling | Minimal | Good | Excellent | Med | Tiered importance |
| 4. Adaptive rate | Minimal | Excellent | Poor (unless combined) | Med | Stable throughput target |
| 5. Reservoir sampling | Minimal | Fixed storage | Proportional | Med | Offline analysis |
| 6. Per-endpoint | Minimal | Excellent | Per-config | Med | Mixed-traffic services |

**Key principle**: combine tail-based sampling (for correctness — keep errors and slow traces) with adaptive rate control (for economics — cap total volume). Use priority classification as the first filter so critical operations are never dropped regardless of rate limits. A common production stack is: priority → per-endpoint rate → tail buffer → adaptive global cap.
