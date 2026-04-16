---
title: "Agent Doesn't Implement Prometheus Pushgateway for Short-Lived Agent Metrics"
description: "AI agents that run as ephemeral jobs or serverless functions exit before Prometheus can scrape them, leaving gaps in metrics coverage. The Prometheus Pushgateway accepts metric pushes from short-lived processes before they terminate, making tool call counts, token usage, latency histograms, and error rates visible in Grafana dashboards even for sub-minute agent runs."
date: 2025-02-19
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-prometheus-pushgateway-for-short-lived-agent-metrics
tags:
  - prometheus
  - pushgateway
  - metrics
  - observability
  - short-lived
  - ephemeral
  - grafana
symptoms:
  - "Prometheus shows no metrics from agent job pods that complete in under 30 seconds"
  - "Tool call counts are missing from Grafana because scrape interval exceeds agent lifetime"
  - "Token usage is invisible for serverless agent invocations that terminate after each request"
  - "Error rate dashboards show flat zero because agents exit before the scrape window"
  - "No histogram data for agent latency because pull-based scrape never fires"
---

## Problem

Prometheus uses a pull model: it scrapes `/metrics` endpoints at a fixed interval (default 15s). Agents that complete in under 15 seconds are never scraped—all counters, histograms, and gauges they accumulate vanish on exit. The Pushgateway solves this by acting as an intermediary store: agents push metrics to it before terminating, and Prometheus scrapes the Pushgateway on its normal schedule. The Pushgateway persists the last pushed value until explicitly deleted, making ephemeral job metrics visible in long-running dashboards.

---

## Solution 1: AgentMetricsCollector — Core Metrics with Pushgateway Export

```python
import os
import socket
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        push_to_gateway,
    )
except ImportError:
    raise ImportError("pip install prometheus-client")


class AgentMetricsCollector:
    """
    Collects per-agent-run metrics and pushes them to a Prometheus
    Pushgateway on completion. Each metric family uses a fresh
    CollectorRegistry to avoid cross-run state leakage in long-lived
    processes that spawn multiple agent runs.

    Usage:
        metrics = AgentMetricsCollector(
            job="agent-run",
            pushgateway_url="http://pushgateway:9091",
            labels={"agent_id": "agent-A", "env": "production"},
        )
        with metrics.tool_timer("web_search"):
            result = await tool.execute(...)
        metrics.record_tokens(input_tokens=800, output_tokens=200)
        metrics.push()  # call before process exit
    """

    DEFAULT_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

    def __init__(
        self,
        job: str = "agent",
        pushgateway_url: str = "http://localhost:9091",
        labels: Optional[Dict[str, str]] = None,
        instance: Optional[str] = None,
    ):
        self._job = job
        self._url = pushgateway_url
        self._grouping: Dict[str, str] = {
            "instance": instance or socket.gethostname(),
            **(labels or {}),
        }
        self._registry = CollectorRegistry()
        self._start = time.time()

        self.tool_calls_total = Counter(
            "agent_tool_calls_total",
            "Total tool invocations",
            ["tool", "status"],
            registry=self._registry,
        )
        self.tool_latency = Histogram(
            "agent_tool_latency_seconds",
            "Tool call latency",
            ["tool"],
            buckets=self.DEFAULT_LATENCY_BUCKETS,
            registry=self._registry,
        )
        self.llm_tokens_total = Counter(
            "agent_llm_tokens_total",
            "LLM tokens consumed",
            ["direction"],  # input / output
            registry=self._registry,
        )
        self.llm_calls_total = Counter(
            "agent_llm_calls_total",
            "LLM API calls made",
            ["model", "stop_reason"],
            registry=self._registry,
        )
        self.errors_total = Counter(
            "agent_errors_total",
            "Agent errors by type",
            ["error_type"],
            registry=self._registry,
        )
        self.run_duration = Gauge(
            "agent_run_duration_seconds",
            "Total agent run duration",
            registry=self._registry,
        )

    @contextmanager
    def tool_timer(self, tool_name: str):
        """Context manager that records tool latency and success/failure."""
        t0 = time.monotonic()
        status = "success"
        try:
            yield
        except Exception:
            status = "error"
            self.errors_total.labels(error_type=f"tool_{tool_name}").inc()
            raise
        finally:
            elapsed = time.monotonic() - t0
            self.tool_calls_total.labels(tool=tool_name, status=status).inc()
            self.tool_latency.labels(tool=tool_name).observe(elapsed)

    def record_tokens(self, input_tokens: int = 0, output_tokens: int = 0):
        self.llm_tokens_total.labels(direction="input").inc(input_tokens)
        self.llm_tokens_total.labels(direction="output").inc(output_tokens)

    def record_llm_call(self, model: str, stop_reason: str = "end_turn"):
        self.llm_calls_total.labels(model=model, stop_reason=stop_reason).inc()

    def record_error(self, error_type: str):
        self.errors_total.labels(error_type=error_type).inc()

    def push(self, timeout: int = 5):
        """Push all metrics to Pushgateway. Call before process exit."""
        self.run_duration.set(time.time() - self._start)
        push_to_gateway(
            self._url,
            job=self._job,
            registry=self._registry,
            grouping_key=self._grouping,
            timeout=timeout,
        )
```

---

## Solution 2: PushgatewaySession — Context Manager with Guaranteed Push on Exit

```python
import atexit
import logging
import signal
import sys
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PushgatewaySession:
    """
    Wraps AgentMetricsCollector in a session that guarantees metrics
    are pushed even on SIGTERM, unhandled exceptions, or atexit.
    Registers signal handlers for graceful shutdown in container environments.

    Usage:
        async with PushgatewaySession(job="batch-agent", url="http://pushgateway:9091") as sess:
            with sess.metrics.tool_timer("web_search"):
                ...
            sess.metrics.record_tokens(800, 200)
        # Metrics automatically pushed on context exit or SIGTERM
    """

    def __init__(
        self,
        job: str,
        url: str,
        labels: Optional[Dict[str, str]] = None,
        push_on_signal: bool = True,
    ):
        self._job = job
        self._url = url
        self._labels = labels or {}
        self._push_on_signal = push_on_signal
        self.metrics: Optional[Any] = None
        self._pushed = False

    def _safe_push(self):
        if self._pushed or self.metrics is None:
            return
        try:
            self.metrics.push()
            self._pushed = True
            logger.info("pushgateway_push_success job=%s", self._job)
        except Exception as exc:
            logger.error("pushgateway_push_failed job=%s error=%s", self._job, exc)

    async def __aenter__(self):
        self.metrics = AgentMetricsCollector(
            job=self._job,
            pushgateway_url=self._url,
            labels=self._labels,
        )
        if self._push_on_signal:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, self._signal_handler)
        atexit.register(self._safe_push)
        return self

    def _signal_handler(self, signum, frame):
        logger.info("signal_received sig=%d pushing_metrics", signum)
        self._safe_push()
        sys.exit(0)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.metrics.record_error(exc_type.__name__)
        self._safe_push()
        return False  # do not suppress exceptions
```

---

## Solution 3: MetricsBatchPusher — Aggregate Metrics from Parallel Agent Workers

```python
import threading
import time
from typing import Any, Dict, List, Optional

try:
    from prometheus_client import CollectorRegistry, Counter, Histogram, push_to_gateway
except ImportError:
    raise ImportError("pip install prometheus-client")


class MetricsBatchPusher:
    """
    Collects metrics from multiple parallel agent workers and pushes
    them as a single aggregated batch to Pushgateway. Workers submit
    partial metric dicts; the pusher merges them and flushes on a
    configurable interval or on explicit flush().

    Usage:
        pusher = MetricsBatchPusher(url="http://pushgateway:9091", job="parallel-agents")
        pusher.start()
        # Worker threads call:
        pusher.submit({"tool_calls": {"web_search": 3}, "tokens": {"input": 400}})
        pusher.flush()  # final push before shutdown
        pusher.stop()
    """

    def __init__(
        self,
        url: str,
        job: str = "parallel-agents",
        flush_interval: float = 30.0,
        labels: Optional[Dict[str, str]] = None,
    ):
        self._url = url
        self._job = job
        self._interval = flush_interval
        self._labels = labels or {}
        self._lock = threading.Lock()
        self._tool_calls: Dict[str, int] = {}
        self._tokens: Dict[str, int] = {}
        self._errors: Dict[str, int] = {}
        self._latencies: List[float] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def submit(self, partial: Dict[str, Any]):
        """Thread-safe metric submission from a worker."""
        with self._lock:
            for tool, count in partial.get("tool_calls", {}).items():
                self._tool_calls[tool] = self._tool_calls.get(tool, 0) + count
            for direction, count in partial.get("tokens", {}).items():
                self._tokens[direction] = self._tokens.get(direction, 0) + count
            for err, count in partial.get("errors", {}).items():
                self._errors[err] = self._errors.get(err, 0) + count
            self._latencies.extend(partial.get("latencies_seconds", []))

    def flush(self):
        registry = CollectorRegistry()
        tool_counter = Counter(
            "agent_tool_calls_total", "Tool calls", ["tool"], registry=registry
        )
        token_counter = Counter(
            "agent_llm_tokens_total", "LLM tokens", ["direction"], registry=registry
        )
        error_counter = Counter(
            "agent_errors_total", "Errors", ["error_type"], registry=registry
        )
        latency_hist = Histogram(
            "agent_tool_latency_seconds", "Latency",
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=registry,
        )

        with self._lock:
            for tool, count in self._tool_calls.items():
                tool_counter.labels(tool=tool).inc(count)
            for direction, count in self._tokens.items():
                token_counter.labels(direction=direction).inc(count)
            for err, count in self._errors.items():
                error_counter.labels(error_type=err).inc(count)
            for lat in self._latencies:
                latency_hist.observe(lat)

        push_to_gateway(self._url, job=self._job, registry=registry,
                         grouping_key=self._labels)

    def _loop(self):
        while self._running:
            time.sleep(self._interval)
            if self._running:
                try:
                    self.flush()
                except Exception:
                    pass

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self.flush()
```

---

## Solution 4: PushgatewayCleanup — Delete Stale Job Metrics After TTL

```python
import logging
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)


class PushgatewayCleanup:
    """
    Deletes job metrics from the Pushgateway after a configurable TTL.
    Prevents stale metrics from old agent runs from persisting indefinitely
    and polluting dashboards with phantom data.

    Usage:
        cleanup = PushgatewayCleanup(url="http://pushgateway:9091", ttl_seconds=300)
        # After agent run:
        cleanup.record_push(job="agent-run", grouping={"instance": "pod-abc"})
        # In a periodic task (cron or background thread):
        cleanup.evict_stale()
    """

    def __init__(self, url: str, ttl_seconds: float = 300.0, timeout: int = 5):
        self._url = url.rstrip("/")
        self._ttl = ttl_seconds
        self._timeout = timeout
        self._records: Dict[str, float] = {}  # key -> push timestamp

    @staticmethod
    def _build_key(job: str, grouping: Dict[str, str]) -> str:
        parts = [f"job={job}"] + [f"{k}={v}" for k, v in sorted(grouping.items())]
        return ";".join(parts)

    @staticmethod
    def _build_url_path(base: str, job: str, grouping: Dict[str, str]) -> str:
        path = f"{base}/metrics/job/{job}"
        for k, v in sorted(grouping.items()):
            path += f"/{k}/{v}"
        return path

    def record_push(self, job: str, grouping: Optional[Dict[str, str]] = None):
        key = self._build_key(job, grouping or {})
        self._records[key] = time.time()

    def evict_stale(self) -> int:
        now = time.time()
        evicted = 0
        for key, pushed_at in list(self._records.items()):
            if now - pushed_at > self._ttl:
                job, *pairs = key.split(";")
                job_name = job.split("=", 1)[1]
                grouping = dict(p.split("=", 1) for p in pairs)
                url = self._build_url_path(self._url, job_name, grouping)
                try:
                    resp = requests.delete(url, timeout=self._timeout)
                    if resp.status_code in (200, 202):
                        del self._records[key]
                        evicted += 1
                        logger.info("pushgateway_evicted key=%s", key)
                    else:
                        logger.warning("pushgateway_evict_failed status=%d key=%s",
                                        resp.status_code, key)
                except requests.RequestException as exc:
                    logger.error("pushgateway_evict_error key=%s error=%s", key, exc)
        return evicted
```

---

## Solution 5: HealthGauge — Liveness Heartbeat via Pushgateway

```python
import threading
import time
from typing import Dict, Optional

try:
    from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
except ImportError:
    raise ImportError("pip install prometheus-client")


class HealthGauge:
    """
    Pushes a liveness heartbeat gauge to Pushgateway at a regular interval.
    Grafana alerts fire when the gauge goes stale (last_push older than 2×interval).
    For long-running agents that are nominally short-lived but can hang,
    the missing heartbeat exposes the silent hang.

    Usage:
        hb = HealthGauge(url="http://pushgateway:9091", job="agent-worker",
                          interval=15.0, labels={"pod": "agent-0"})
        hb.start()
        # ... agent work ...
        hb.stop()   # also pushes final heartbeat with status=0
    """

    def __init__(
        self,
        url: str,
        job: str = "agent-worker",
        interval: float = 15.0,
        labels: Optional[Dict[str, str]] = None,
        timeout: int = 4,
    ):
        self._url = url
        self._job = job
        self._interval = interval
        self._labels = labels or {}
        self._timeout = timeout
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _push(self, alive: int = 1):
        registry = CollectorRegistry()
        g = Gauge("agent_alive", "1 if agent is running, 0 after shutdown",
                   registry=registry)
        g.set(alive)
        ts = Gauge("agent_last_heartbeat_timestamp",
                    "Unix timestamp of last heartbeat push", registry=registry)
        ts.set(time.time())
        push_to_gateway(
            self._url, job=self._job, registry=registry,
            grouping_key=self._labels, timeout=self._timeout,
        )

    def _loop(self):
        while self._running:
            try:
                self._push(alive=1)
            except Exception:
                pass
            time.sleep(self._interval)

    def start(self):
        self._running = True
        self._push(alive=1)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=self._interval + 1)
        try:
            self._push(alive=0)
        except Exception:
            pass
```

---

## Solution 6: MetricsExportPipeline — End-to-End Agent Run Instrumentation

```python
import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class RunMetricsSummary:
    job: str
    duration_seconds: float
    tool_calls: Dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    errors: List[str] = field(default_factory=list)
    pushed: bool = False


class MetricsExportPipeline:
    """
    Thin orchestration layer that composes AgentMetricsCollector,
    HealthGauge, and PushgatewayCleanup into a single pipeline.
    Designed for async agent frameworks that run one job per process.

    Usage:
        pipeline = MetricsExportPipeline(
            pushgateway_url="http://pushgateway:9091",
            job="rag-agent",
            labels={"env": "prod", "region": "us-east-1"},
            heartbeat_interval=10.0,
            metric_ttl=600.0,
        )
        async with pipeline.run() as metrics:
            with metrics.tool_timer("retrieval"):
                chunks = await retriever.retrieve(query)
            metrics.record_tokens(input_tokens=1200, output_tokens=350)
        summary = pipeline.summary()
    """

    def __init__(
        self,
        pushgateway_url: str,
        job: str = "agent",
        labels: Optional[Dict[str, str]] = None,
        heartbeat_interval: float = 15.0,
        metric_ttl: float = 300.0,
    ):
        self._url = pushgateway_url
        self._job = job
        self._labels = labels or {}
        self._hb_interval = heartbeat_interval
        self._ttl = metric_ttl
        self._summary: Optional[RunMetricsSummary] = None

    @asynccontextmanager
    async def run(self):
        collector = AgentMetricsCollector(
            job=self._job,
            pushgateway_url=self._url,
            labels=self._labels,
        )
        hb = HealthGauge(
            url=self._url,
            job=f"{self._job}-heartbeat",
            interval=self._hb_interval,
            labels=self._labels,
        )
        cleanup = PushgatewayCleanup(url=self._url, ttl_seconds=self._ttl)
        t0 = time.time()

        hb.start()
        try:
            yield collector
            pushed = False
            try:
                collector.push()
                cleanup.record_push(self._job, self._labels)
                pushed = True
            except Exception as exc:
                pass
        except Exception as exc:
            collector.record_error(type(exc).__name__)
            try:
                collector.push()
                pushed = True
            except Exception:
                pushed = False
            raise
        finally:
            hb.stop()
            # Schedule stale cleanup asynchronously
            asyncio.get_event_loop().call_later(
                self._ttl, lambda: cleanup.evict_stale()
            )
            self._summary = RunMetricsSummary(
                job=self._job,
                duration_seconds=round(time.time() - t0, 3),
                pushed=pushed,
            )

    def summary(self) -> Optional[RunMetricsSummary]:
        return self._summary
```

---

## Comparison

| Approach | Guaranteed Push | Signal Handling | Parallel Workers | Stale Cleanup | Heartbeat | Integrated |
|---|---|---|---|---|---|---|
| **AgentMetricsCollector** | Manual `.push()` | No | No | No | No | No |
| **PushgatewaySession** | Yes (atexit + signal) | Yes | No | No | No | No |
| **MetricsBatchPusher** | On `.flush()` | No | Yes | No | No | No |
| **PushgatewayCleanup** | N/A | N/A | N/A | Yes | No | No |
| **HealthGauge** | Periodic | No | No | No | Yes | No |
| **MetricsExportPipeline** | Yes | Via session | No | Yes | Yes | Yes |

**Key insight**: the minimal viable change is adding `AgentMetricsCollector.push()` in a `finally` block at the top of your agent entrypoint—this guarantees metrics reach Pushgateway even on exception. Add `PushgatewaySession` to also handle SIGTERM from container orchestrators. Set Pushgateway's `--persistence.file` flag to survive Pushgateway restarts. In Grafana, create an alert on `time() - agent_last_heartbeat_timestamp > 2 * heartbeat_interval` to detect silent hangs. Use `PushgatewayCleanup.evict_stale()` in a 5-minute cron to prevent stale metrics from old pods from skewing error-rate dashboards.
