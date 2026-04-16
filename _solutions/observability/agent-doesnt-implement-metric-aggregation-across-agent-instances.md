---
title: "Agent Doesn't Implement Metric Aggregation Across Agent Instances"
description: "Six solutions for collecting and aggregating metrics from multiple concurrent agent instances into unified dashboards and alerting."
difficulty: intermediate
category: observability
tags: [metrics, prometheus, opentelemetry, aggregation, monitoring, multi-instance]
---

# Agent Doesn't Implement Metric Aggregation Across Agent Instances

When multiple agent instances run concurrently—across threads, processes, or hosts—metrics fragment per-instance. No single vantage point shows aggregate throughput, error rates, or latency distributions. Dashboards show one shard; alerts miss correlated spikes. These six solutions span in-process merging through full OpenTelemetry pipelines.

## Solution 1: Prometheus Pushgateway for Short-Lived Agents

Agents push their metrics to a shared Pushgateway; Prometheus scrapes it for a unified view.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from prometheus_client import (
    CollectorRegistry, Counter, Histogram, Gauge,
    push_to_gateway, REGISTRY
)
from anthropic import AsyncAnthropic

@dataclass
class AgentMetrics:
    registry: CollectorRegistry = field(default_factory=CollectorRegistry)
    instance_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def __post_init__(self):
        labels = ["instance_id", "model"]
        self.requests_total = Counter(
            "agent_requests_total",
            "Total LLM requests",
            labels,
            registry=self.registry,
        )
        self.tokens_used = Counter(
            "agent_tokens_total",
            "Total tokens consumed",
            ["instance_id", "model", "type"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "agent_request_latency_seconds",
            "Request latency",
            labels,
            buckets=[0.1, 0.5, 1, 2, 5, 10, 30],
            registry=self.registry,
        )
        self.errors_total = Counter(
            "agent_errors_total",
            "Total errors",
            ["instance_id", "model", "error_type"],
            registry=self.registry,
        )
        self.active_requests = Gauge(
            "agent_active_requests",
            "In-flight requests",
            ["instance_id"],
            registry=self.registry,
        )

    def push(self, gateway: str = "localhost:9091"):
        push_to_gateway(
            gateway,
            job="ai_agent",
            grouping_key={"instance": self.instance_id},
            registry=self.registry,
        )


class InstrumentedAgent:
    def __init__(self, gateway: str = "localhost:9091"):
        self.client = AsyncAnthropic()
        self.metrics = AgentMetrics()
        self.gateway = gateway
        self._push_task: asyncio.Task | None = None

    async def start(self):
        self._push_task = asyncio.create_task(self._periodic_push())

    async def stop(self):
        if self._push_task:
            self._push_task.cancel()
        # Final push before shutdown
        self.metrics.push(self.gateway)

    async def _periodic_push(self, interval: float = 15.0):
        while True:
            await asyncio.sleep(interval)
            try:
                self.metrics.push(self.gateway)
            except Exception as e:
                print(f"Metrics push failed: {e}")

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        labels = [self.metrics.instance_id, model]
        self.metrics.active_requests.labels(self.metrics.instance_id).inc()
        start = time.perf_counter()
        try:
            self.metrics.requests_total.labels(*labels).inc()
            response = await self.client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": message}],
            )
            elapsed = time.perf_counter() - start
            self.metrics.latency.labels(*labels).observe(elapsed)
            self.metrics.tokens_used.labels(
                self.metrics.instance_id, model, "input"
            ).inc(response.usage.input_tokens)
            self.metrics.tokens_used.labels(
                self.metrics.instance_id, model, "output"
            ).inc(response.usage.output_tokens)
            return response.content[0].text
        except Exception as e:
            self.metrics.errors_total.labels(
                self.metrics.instance_id, model, type(e).__name__
            ).inc()
            raise
        finally:
            self.metrics.active_requests.labels(self.metrics.instance_id).dec()


async def run_agent_pool(messages: list[str], n_agents: int = 3):
    agents = [InstrumentedAgent() for _ in range(n_agents)]
    for agent in agents:
        await agent.start()

    async def process(agent: InstrumentedAgent, msgs: list[str]):
        results = []
        for msg in msgs:
            result = await agent.chat(msg)
            results.append(result)
        return results

    # Distribute work across agents
    chunk = max(1, len(messages) // n_agents)
    chunks = [messages[i:i+chunk] for i in range(0, len(messages), chunk)]
    tasks = [process(agent, chunk) for agent, chunk in zip(agents, chunks)]

    results = await asyncio.gather(*tasks)
    for agent in agents:
        await agent.stop()
    return results
```

## Solution 2: Redis-Based Real-Time Metric Aggregation

Each agent atomically increments shared Redis counters; a FastAPI endpoint reads aggregated totals.

```python
import asyncio
import time
import json
import uuid
from dataclasses import dataclass
from typing import Any
import redis.asyncio as aioredis
from anthropic import AsyncAnthropic
from fastapi import FastAPI
import uvicorn


@dataclass
class RedisMetricKey:
    PREFIX = "agent:metrics"

    @staticmethod
    def counter(name: str) -> str:
        return f"{RedisMetricKey.PREFIX}:counter:{name}"

    @staticmethod
    def histogram_bucket(name: str, le: float) -> str:
        return f"{RedisMetricKey.PREFIX}:hist:{name}:le_{le}"

    @staticmethod
    def histogram_sum(name: str) -> str:
        return f"{RedisMetricKey.PREFIX}:hist:{name}:sum"

    @staticmethod
    def histogram_count(name: str) -> str:
        return f"{RedisMetricKey.PREFIX}:hist:{name}:count"

    @staticmethod
    def gauge(name: str) -> str:
        return f"{RedisMetricKey.PREFIX}:gauge:{name}"


class RedisMetricCollector:
    LATENCY_BUCKETS = [0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float("inf")]

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis: aioredis.Redis = aioredis.from_url(redis_url, decode_responses=True)
        self.instance_id = str(uuid.uuid4())[:8]

    async def inc_counter(self, name: str, value: float = 1.0, tags: dict[str, str] | None = None):
        tag_str = ",".join(f"{k}={v}" for k, v in (tags or {}).items())
        key = RedisMetricKey.counter(f"{name}{{{tag_str}}}")
        await self.redis.incrbyfloat(key, value)

    async def observe_histogram(self, name: str, value: float, tags: dict[str, str] | None = None):
        tag_str = ",".join(f"{k}={v}" for k, v in (tags or {}).items())
        pipe = self.redis.pipeline()
        # Increment appropriate buckets
        for le in self.LATENCY_BUCKETS:
            if value <= le:
                bucket_key = RedisMetricKey.histogram_bucket(f"{name}{{{tag_str}}}", le)
                pipe.incr(bucket_key)
        pipe.incrbyfloat(RedisMetricKey.histogram_sum(f"{name}{{{tag_str}}}"), value)
        pipe.incr(RedisMetricKey.histogram_count(f"{name}{{{tag_str}}}"))
        await pipe.execute()

    async def set_gauge(self, name: str, value: float, tags: dict[str, str] | None = None):
        tag_str = ",".join(f"{k}={v}" for k, v in (tags or {}).items())
        await self.redis.set(RedisMetricKey.gauge(f"{name}{{{tag_str}}}"), value)

    async def get_all_metrics(self) -> dict[str, Any]:
        """Aggregate all metrics from Redis into a single dict."""
        keys = await self.redis.keys(f"{RedisMetricKey.PREFIX}:*")
        metrics: dict[str, Any] = {"counters": {}, "histograms": {}, "gauges": {}}
        for key in keys:
            val = await self.redis.get(key)
            parts = key.split(":")
            metric_type = parts[2]  # counter, hist, gauge
            if metric_type == "counter":
                metrics["counters"][key] = float(val or 0)
            elif metric_type == "hist":
                metrics["histograms"][key] = float(val or 0)
            elif metric_type == "gauge":
                metrics["gauges"][key] = float(val or 0)
        return metrics


class RedisInstrumentedAgent:
    def __init__(self, collector: RedisMetricCollector):
        self.client = AsyncAnthropic()
        self.metrics = collector
        self.instance_id = collector.instance_id

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        tags = {"instance": self.instance_id, "model": model}
        await self.metrics.inc_counter("requests_total", tags=tags)
        await self.metrics.set_gauge(
            "active_requests",
            1,
            tags={"instance": self.instance_id},
        )
        start = time.perf_counter()
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": message}],
            )
            elapsed = time.perf_counter() - start
            await self.metrics.observe_histogram("request_latency_seconds", elapsed, tags=tags)
            await self.metrics.inc_counter(
                "tokens_total",
                response.usage.input_tokens,
                tags={**tags, "type": "input"},
            )
            await self.metrics.inc_counter(
                "tokens_total",
                response.usage.output_tokens,
                tags={**tags, "type": "output"},
            )
            return response.content[0].text
        except Exception as e:
            await self.metrics.inc_counter(
                "errors_total",
                tags={**tags, "error_type": type(e).__name__},
            )
            raise
        finally:
            await self.metrics.set_gauge(
                "active_requests",
                0,
                tags={"instance": self.instance_id},
            )


# FastAPI aggregation endpoint
app = FastAPI()
collector = RedisMetricCollector()


@app.get("/metrics/aggregate")
async def aggregate_metrics():
    return await collector.get_all_metrics()


@app.get("/metrics/summary")
async def metrics_summary():
    raw = await collector.get_all_metrics()
    total_requests = sum(raw["counters"].get(k, 0) for k in raw["counters"] if "requests_total" in k)
    total_errors = sum(raw["counters"].get(k, 0) for k in raw["counters"] if "errors_total" in k)
    error_rate = total_errors / max(total_requests, 1)
    return {
        "total_requests": total_requests,
        "total_errors": total_errors,
        "error_rate_pct": round(error_rate * 100, 2),
        "instance_count": len(set(
            k.split("instance=")[1].split(",")[0]
            for k in raw["counters"]
            if "instance=" in k
        )),
    }
```

## Solution 3: In-Process Metric Merging with Shared Memory

For multi-threaded agents in one process, use a thread-safe shared registry with atomic operations.

```python
import asyncio
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import DefaultDict
import statistics
from anthropic import AsyncAnthropic


@dataclass
class MetricSnapshot:
    requests: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latencies: list[float] = field(default_factory=list)

    def merge(self, other: "MetricSnapshot") -> "MetricSnapshot":
        return MetricSnapshot(
            requests=self.requests + other.requests,
            errors=self.errors + other.errors,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            latencies=self.latencies + other.latencies,
        )

    def summary(self) -> dict:
        lats = sorted(self.latencies)
        return {
            "requests": self.requests,
            "errors": self.errors,
            "error_rate_pct": round(self.errors / max(self.requests, 1) * 100, 2),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_p50": statistics.median(lats) if lats else 0,
            "latency_p95": lats[int(len(lats) * 0.95)] if lats else 0,
            "latency_p99": lats[int(len(lats) * 0.99)] if lats else 0,
            "latency_mean": statistics.mean(lats) if lats else 0,
        }


class SharedMetricRegistry:
    """Thread-safe metric registry shared across all agent instances."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshots: DefaultDict[str, MetricSnapshot] = defaultdict(MetricSnapshot)

    def record_request(
        self,
        instance_id: str,
        latency: float,
        input_tokens: int,
        output_tokens: int,
        error: bool = False,
    ):
        with self._lock:
            snap = self._snapshots[instance_id]
            snap.requests += 1
            snap.latencies.append(latency)
            snap.input_tokens += input_tokens
            snap.output_tokens += output_tokens
            if error:
                snap.errors += 1

    def aggregate(self) -> MetricSnapshot:
        with self._lock:
            result = MetricSnapshot()
            for snap in self._snapshots.values():
                result = result.merge(snap)
            return result

    def per_instance(self) -> dict[str, dict]:
        with self._lock:
            return {
                instance_id: snap.summary()
                for instance_id, snap in self._snapshots.items()
            }

    def reset(self) -> MetricSnapshot:
        with self._lock:
            aggregate = self.aggregate()
            self._snapshots.clear()
            return aggregate


# Global registry shared by all agent instances in this process
_GLOBAL_REGISTRY = SharedMetricRegistry()


class SharedRegistryAgent:
    def __init__(self, registry: SharedMetricRegistry = _GLOBAL_REGISTRY):
        self.client = AsyncAnthropic()
        self.registry = registry
        self.instance_id = str(uuid.uuid4())[:8]

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        start = time.perf_counter()
        error = False
        input_tokens = output_tokens = 0
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": message}],
            )
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            return response.content[0].text
        except Exception as e:
            error = True
            raise
        finally:
            elapsed = time.perf_counter() - start
            self.registry.record_request(
                self.instance_id, elapsed, input_tokens, output_tokens, error
            )


async def run_with_shared_metrics():
    registry = SharedMetricRegistry()
    agents = [SharedRegistryAgent(registry) for _ in range(5)]

    messages = [f"What is {i} + {i}?" for i in range(20)]
    chunk = len(messages) // len(agents)

    async def worker(agent: SharedRegistryAgent, msgs: list[str]):
        return [await agent.chat(m) for m in msgs]

    chunks = [messages[i:i+chunk] for i in range(0, len(messages), chunk)]
    await asyncio.gather(*[worker(a, c) for a, c in zip(agents, chunks)])

    # Aggregate after all work done
    aggregate = registry.aggregate()
    print("=== Aggregate Metrics ===")
    print(aggregate.summary())
    print("\n=== Per-Instance Metrics ===")
    for iid, summary in registry.per_instance().items():
        print(f"Instance {iid}: {summary}")
```

## Solution 4: OpenTelemetry Collector Pipeline

Emit OTLP spans and metrics; a shared OTel Collector aggregates across instances and exports to backends.

```python
import asyncio
import time
import uuid
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from anthropic import AsyncAnthropic


def setup_otel(instance_id: str, otlp_endpoint: str = "http://localhost:4317"):
    resource = Resource.create({
        "service.name": "ai-agent",
        "service.instance.id": instance_id,
        "service.version": "1.0.0",
    })

    # Metrics pipeline
    exporter = OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10_000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    # Tracing pipeline
    trace_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
    trace.set_tracer_provider(tracer_provider)

    return meter_provider, tracer_provider


class OTelAgent:
    def __init__(self, otlp_endpoint: str = "http://localhost:4317"):
        self.instance_id = str(uuid.uuid4())[:8]
        self.meter_provider, self.tracer_provider = setup_otel(
            self.instance_id, otlp_endpoint
        )
        self.client = AsyncAnthropic()

        meter = metrics.get_meter("ai.agent")
        self.request_counter = meter.create_counter(
            "agent.requests",
            description="Total LLM requests",
            unit="1",
        )
        self.token_counter = meter.create_counter(
            "agent.tokens",
            description="LLM tokens consumed",
            unit="token",
        )
        self.latency_histogram = meter.create_histogram(
            "agent.request.duration",
            description="LLM request latency",
            unit="s",
        )
        self.error_counter = meter.create_counter(
            "agent.errors",
            description="LLM request errors",
            unit="1",
        )
        self.active_gauge = meter.create_up_down_counter(
            "agent.active_requests",
            description="In-flight requests",
            unit="1",
        )
        self.tracer = trace.get_tracer("ai.agent")

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        attrs = {"model": model, "instance_id": self.instance_id}

        with self.tracer.start_as_current_span("agent.chat") as span:
            span.set_attribute("model", model)
            span.set_attribute("instance_id", self.instance_id)
            span.set_attribute("message_length", len(message))

            self.active_gauge.add(1, attrs)
            self.request_counter.add(1, attrs)
            start = time.perf_counter()

            try:
                response = await self.client.messages.create(
                    model=model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": message}],
                )
                elapsed = time.perf_counter() - start
                self.latency_histogram.record(elapsed, attrs)
                self.token_counter.add(
                    response.usage.input_tokens,
                    {**attrs, "token_type": "input"},
                )
                self.token_counter.add(
                    response.usage.output_tokens,
                    {**attrs, "token_type": "output"},
                )
                span.set_attribute("input_tokens", response.usage.input_tokens)
                span.set_attribute("output_tokens", response.usage.output_tokens)
                return response.content[0].text
            except Exception as e:
                self.error_counter.add(1, {**attrs, "error_type": type(e).__name__})
                span.record_exception(e)
                raise
            finally:
                self.active_gauge.add(-1, attrs)

    async def shutdown(self):
        self.meter_provider.shutdown()
        self.tracer_provider.shutdown()
```

## Solution 5: Statistical Summary Endpoint with Reservoir Sampling

Agents write to a bounded reservoir; a dedicated aggregator thread computes running statistics.

```python
import asyncio
import math
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Deque
from anthropic import AsyncAnthropic


@dataclass
class Sample:
    latency: float
    input_tokens: int
    output_tokens: int
    error: bool
    timestamp: float
    instance_id: str


class ReservoirSampler:
    """Thread-safe reservoir sampler (Algorithm R) for latency samples."""

    def __init__(self, size: int = 1000):
        self._size = size
        self._reservoir: list[Sample] = []
        self._count = 0
        self._lock = threading.Lock()

    def add(self, sample: Sample):
        with self._lock:
            self._count += 1
            if len(self._reservoir) < self._size:
                self._reservoir.append(sample)
            else:
                j = random.randint(0, self._count - 1)
                if j < self._size:
                    self._reservoir[j] = sample

    def snapshot(self) -> list[Sample]:
        with self._lock:
            return list(self._reservoir)


class MetricAggregator:
    """Background aggregator that computes statistics from all agent samples."""

    def __init__(self, window_seconds: float = 60.0):
        self._sampler = ReservoirSampler(size=5000)
        self._window = window_seconds
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {"requests": 0, "errors": 0}
        self._token_totals: dict[str, int] = {"input": 0, "output": 0}
        self._recent: Deque[Sample] = deque()
        self._agg_thread = threading.Thread(target=self._aggregate_loop, daemon=True)
        self._stats: dict = {}
        self._agg_thread.start()

    def record(self, sample: Sample):
        self._sampler.add(sample)
        with self._lock:
            self._counters["requests"] += 1
            if sample.error:
                self._counters["errors"] += 1
            self._token_totals["input"] += sample.input_tokens
            self._token_totals["output"] += sample.output_tokens
            self._recent.append(sample)

    def _aggregate_loop(self):
        while True:
            time.sleep(10)
            self._compute_stats()

    def _compute_stats(self):
        now = time.time()
        with self._lock:
            # Prune old samples from recent window
            while self._recent and (now - self._recent[0].timestamp) > self._window:
                self._recent.popleft()
            recent_latencies = sorted(s.latency for s in self._recent)
            recent_errors = sum(1 for s in self._recent if s.error)
            recent_count = len(self._recent)

        def percentile(data: list[float], p: float) -> float:
            if not data:
                return 0.0
            idx = max(0, int(math.ceil(len(data) * p / 100)) - 1)
            return data[idx]

        reservoir = self._sampler.snapshot()
        all_latencies = sorted(s.latency for s in reservoir)

        with self._lock:
            self._stats = {
                "all_time": {
                    "requests": self._counters["requests"],
                    "errors": self._counters["errors"],
                    "error_rate_pct": round(
                        self._counters["errors"] / max(self._counters["requests"], 1) * 100, 2
                    ),
                    "input_tokens": self._token_totals["input"],
                    "output_tokens": self._token_totals["output"],
                    "latency_p50": percentile(all_latencies, 50),
                    "latency_p95": percentile(all_latencies, 95),
                    "latency_p99": percentile(all_latencies, 99),
                },
                "window_60s": {
                    "requests": recent_count,
                    "errors": recent_errors,
                    "error_rate_pct": round(recent_errors / max(recent_count, 1) * 100, 2),
                    "latency_p50": percentile(recent_latencies, 50),
                    "latency_p95": percentile(recent_latencies, 95),
                    "latency_p99": percentile(recent_latencies, 99),
                },
                "instance_breakdown": self._per_instance_stats(reservoir),
                "computed_at": now,
            }

    def _per_instance_stats(self, samples: list[Sample]) -> dict:
        by_instance: dict[str, list[float]] = {}
        for s in samples:
            by_instance.setdefault(s.instance_id, []).append(s.latency)
        return {
            iid: {
                "sample_count": len(lats),
                "latency_mean": sum(lats) / len(lats),
            }
            for iid, lats in by_instance.items()
        }

    @property
    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)


_GLOBAL_AGGREGATOR = MetricAggregator()


class SamplingAgent:
    def __init__(self, aggregator: MetricAggregator = _GLOBAL_AGGREGATOR):
        self.client = AsyncAnthropic()
        self.agg = aggregator
        self.instance_id = str(uuid.uuid4())[:8]

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        start = time.perf_counter()
        error = False
        input_tokens = output_tokens = 0
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": message}],
            )
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            return response.content[0].text
        except Exception:
            error = True
            raise
        finally:
            elapsed = time.perf_counter() - start
            self.agg.record(Sample(
                latency=elapsed,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error=error,
                timestamp=time.time(),
                instance_id=self.instance_id,
            ))
```

## Solution 6: Prometheus Federation Pull Model

Each agent instance exposes its own `/metrics` endpoint; a Prometheus federation job scrapes them all.

```python
import asyncio
import time
import uuid
from aiohttp import web
from prometheus_client import (
    CollectorRegistry, Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST
)
from anthropic import AsyncAnthropic


class FederatedAgentServer:
    """Each instance serves its own /metrics; federation Prometheus scrapes all."""

    def __init__(self, port: int):
        self.port = port
        self.instance_id = str(uuid.uuid4())[:8]
        self.client = AsyncAnthropic()
        self.registry = CollectorRegistry()

        labels = ["instance_id", "model"]
        self.requests = Counter(
            "agent_requests_total", "Total requests",
            labels, registry=self.registry,
        )
        self.tokens = Counter(
            "agent_tokens_total", "Tokens used",
            ["instance_id", "model", "type"], registry=self.registry,
        )
        self.latency = Histogram(
            "agent_latency_seconds", "Request latency",
            labels, registry=self.registry,
        )
        self.errors = Counter(
            "agent_errors_total", "Errors",
            ["instance_id", "model", "error_type"], registry=self.registry,
        )
        self.active = Gauge(
            "agent_active_requests", "In-flight",
            ["instance_id"], registry=self.registry,
        )

        self.app = web.Application()
        self.app.router.add_get("/metrics", self._metrics_handler)
        self.app.router.add_post("/chat", self._chat_handler)

    async def _metrics_handler(self, request: web.Request) -> web.Response:
        output = generate_latest(self.registry)
        return web.Response(body=output, content_type=CONTENT_TYPE_LATEST)

    async def _chat_handler(self, request: web.Request) -> web.Response:
        data = await request.json()
        message = data.get("message", "")
        model = data.get("model", "claude-haiku-4-5-20251001")
        try:
            result = await self.chat(message, model)
            return web.json_response({"response": result})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        labels = [self.instance_id, model]
        self.active.labels(self.instance_id).inc()
        self.requests.labels(*labels).inc()
        start = time.perf_counter()
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": message}],
            )
            elapsed = time.perf_counter() - start
            self.latency.labels(*labels).observe(elapsed)
            self.tokens.labels(self.instance_id, model, "input").inc(
                response.usage.input_tokens
            )
            self.tokens.labels(self.instance_id, model, "output").inc(
                response.usage.output_tokens
            )
            return response.content[0].text
        except Exception as e:
            self.errors.labels(self.instance_id, model, type(e).__name__).inc()
            raise
        finally:
            self.active.labels(self.instance_id).dec()

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        print(f"Agent {self.instance_id} serving metrics on :{self.port}/metrics")
        return runner


# prometheus.yml federation config:
# scrape_configs:
#   - job_name: 'agent_federation'
#     metrics_path: /metrics
#     static_configs:
#       - targets: ['agent-1:8001', 'agent-2:8002', 'agent-3:8003']
#   - job_name: 'federated_aggregate'
#     honor_labels: true
#     metrics_path: /federate
#     params:
#       match[]: ['agent_requests_total', 'agent_tokens_total', 'agent_latency_seconds']
#     static_configs:
#       - targets: ['prometheus:9090']


async def run_federated_pool():
    ports = [8001, 8002, 8003]
    servers = [FederatedAgentServer(port=p) for p in ports]
    runners = await asyncio.gather(*[s.start() for s in servers])

    # Simulate traffic
    tasks = []
    for server in servers:
        for i in range(10):
            tasks.append(server.chat(f"Tell me fact number {i}"))
    await asyncio.gather(*tasks)

    # Keep running so Prometheus can scrape
    await asyncio.sleep(120)
    for runner in runners:
        await runner.cleanup()
```

## Comparison Table

| Solution | Aggregation Model | Latency | Cardinality Limit | Persistence | Best For |
|---|---|---|---|---|---|
| Pushgateway | Push-based batch | 15s flush | Medium | Gateway memory | Short-lived batch agents |
| Redis Counters | Shared atomic store | <1ms | Low–medium | Redis TTL | High-throughput real-time |
| Shared Memory | In-process merge | Zero | None | Process lifetime | Single-process multi-thread |
| OpenTelemetry | OTLP pipeline | 10s flush | High (labels) | Backend (Tempo/Mimir) | Full observability stack |
| Reservoir Sampler | Statistical approximation | Background | Bounded | Memory only | Memory-constrained hosts |
| Federation Pull | Per-instance scrape | Scrape interval | High | Prometheus TSDB | Stable long-running services |

**Recommended**: Use OpenTelemetry (Solution 4) when you have an observability stack (Grafana, Tempo, Mimir). Use Redis (Solution 2) for lightweight real-time aggregation without extra infrastructure. Use Shared Memory (Solution 3) for single-process deployments with multiple async agents.
