---
layout: solution
title: "Agent Doesn't Implement Load Testing for Agent Endpoints"
category: testing
description: "AI agents deployed as HTTP endpoints are never load tested, so throughput limits, queue saturation, and latency degradation under concurrency go undetected until production traffic spikes."
tags: [load-testing, locust, asyncio, concurrency, performance, k6, pytest]
---

# Agent Doesn't Implement Load Testing for Agent Endpoints

## Problem

Agent HTTP endpoints receive only functional tests before deployment. Nobody tests what happens at 10, 100, or 500 concurrent users. The result: queue exhaustion, timeout cascades, and latency spikes discovered during live incidents instead of in CI.

## Solutions

### Option 1: Locust Load Test with Ramp-Up Profile

```python
# load_tests/locustfile.py
import json
import time
from locust import HttpUser, task, between, events
from locust.runners import MasterRunner


class AgentUser(HttpUser):
    """Simulates a single concurrent user hitting the agent endpoint."""
    wait_time = between(1, 3)  # seconds between requests

    def on_start(self):
        """Called when a simulated user starts."""
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer test-token",
        }
        self.session_id = f"load-test-{id(self)}"

    @task(3)
    def simple_query(self):
        """High-frequency short query."""
        payload = {
            "message": "What is 2+2?",
            "session_id": self.session_id,
            "max_tokens": 64,
        }
        with self.client.post(
            "/api/agent/chat",
            json=payload,
            headers=self.headers,
            catch_response=True,
            timeout=30,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                if "response" not in data:
                    response.failure("Missing 'response' field in payload")
            elif response.status_code == 429:
                response.failure("Rate limited — capacity exceeded")
            else:
                response.failure(f"HTTP {response.status_code}")

    @task(1)
    def complex_query(self):
        """Low-frequency long query that exercises token budget."""
        payload = {
            "message": "Explain the differences between synchronous and asynchronous programming in Python with examples.",
            "session_id": self.session_id,
            "max_tokens": 512,
        }
        start = time.time()
        with self.client.post(
            "/api/agent/chat",
            json=payload,
            headers=self.headers,
            catch_response=True,
            timeout=60,
        ) as response:
            elapsed = time.time() - start
            if response.status_code == 200:
                if elapsed > 45:
                    response.failure(f"Latency too high: {elapsed:.1f}s")
            else:
                response.failure(f"HTTP {response.status_code}")


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("Load test started — target:", environment.host)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    stats = environment.stats.total
    print(f"\n=== Load Test Summary ===")
    print(f"Total requests:   {stats.num_requests}")
    print(f"Failures:         {stats.num_failures}")
    print(f"Failure rate:     {stats.fail_ratio:.1%}")
    print(f"Avg latency:      {stats.avg_response_time:.0f}ms")
    print(f"95th percentile:  {stats.get_response_time_percentile(0.95):.0f}ms")
    print(f"99th percentile:  {stats.get_response_time_percentile(0.99):.0f}ms")
    print(f"RPS:              {stats.current_rps:.1f}")

    # Fail CI if SLOs are breached
    if stats.fail_ratio > 0.01:
        raise SystemExit("FAIL: error rate > 1%")
    if stats.get_response_time_percentile(0.95) > 10000:
        raise SystemExit("FAIL: p95 latency > 10s")
```

```bash
# Run: ramp from 1 to 50 users over 60s, hold for 120s
locust -f load_tests/locustfile.py \
  --host=http://localhost:8000 \
  --users=50 \
  --spawn-rate=1 \
  --run-time=3m \
  --headless \
  --csv=load_test_results
```

**Expected Token Savings:** Not applicable — tests infrastructure, not model calls
**Environment:** `pip install locust`

---

### Option 2: asyncio Concurrent Stress Test (No External Tools)

```python
# load_tests/stress_test.py
"""
Stress test using only asyncio + aiohttp — no extra dependencies beyond aiohttp.
Suitable for CI pipelines where installing locust is not desired.
"""
import asyncio
import time
import statistics
from dataclasses import dataclass, field
from typing import List
import aiohttp


@dataclass
class RequestResult:
    status: int
    latency_ms: float
    success: bool
    error: str = ""


@dataclass
class LoadTestReport:
    total: int = 0
    successes: int = 0
    failures: int = 0
    latencies: List[float] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        return self.failures / max(self.total, 1)

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0

    @property
    def p95(self) -> float:
        if not self.latencies:
            return 0
        sorted_l = sorted(self.latencies)
        idx = int(len(sorted_l) * 0.95)
        return sorted_l[idx]

    @property
    def p99(self) -> float:
        if not self.latencies:
            return 0
        sorted_l = sorted(self.latencies)
        idx = int(len(sorted_l) * 0.99)
        return sorted_l[idx]

    def print_summary(self):
        print(f"\n{'='*40}")
        print(f"Total requests : {self.total}")
        print(f"Successes      : {self.successes}")
        print(f"Failures       : {self.failures}")
        print(f"Error rate     : {self.error_rate:.1%}")
        print(f"P50 latency    : {self.p50:.0f}ms")
        print(f"P95 latency    : {self.p95:.0f}ms")
        print(f"P99 latency    : {self.p99:.0f}ms")
        print(f"{'='*40}\n")


async def single_request(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    timeout_s: float = 30.0,
) -> RequestResult:
    start = time.perf_counter()
    try:
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            elapsed_ms = (time.perf_counter() - start) * 1000
            body = await resp.json()
            success = resp.status == 200 and "response" in body
            return RequestResult(
                status=resp.status,
                latency_ms=elapsed_ms,
                success=success,
                error="" if success else f"status={resp.status}",
            )
    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RequestResult(status=0, latency_ms=elapsed_ms, success=False, error="timeout")
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RequestResult(status=0, latency_ms=elapsed_ms, success=False, error=str(e))


async def run_load_test(
    url: str,
    concurrency: int,
    total_requests: int,
    payload: dict,
) -> LoadTestReport:
    semaphore = asyncio.Semaphore(concurrency)
    report = LoadTestReport()

    async def bounded_request() -> RequestResult:
        async with semaphore:
            return await single_request(session, url, payload)

    connector = aiohttp.TCPConnector(limit=concurrency + 10)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [bounded_request() for _ in range(total_requests)]
        results = await asyncio.gather(*tasks)

    for r in results:
        report.total += 1
        report.latencies.append(r.latency_ms)
        if r.success:
            report.successes += 1
        else:
            report.failures += 1

    return report


async def main():
    url = "http://localhost:8000/api/agent/chat"
    payload = {"message": "Hello, agent!", "max_tokens": 128}

    # Warm-up
    print("Warming up (10 requests, concurrency=2)...")
    await run_load_test(url, concurrency=2, total_requests=10, payload=payload)

    # Light load
    print("\nLight load test (50 requests, concurrency=5)...")
    report = await run_load_test(url, concurrency=5, total_requests=50, payload=payload)
    report.print_summary()
    assert report.error_rate < 0.05, f"Error rate too high at light load: {report.error_rate:.1%}"

    # Medium load
    print("Medium load test (200 requests, concurrency=20)...")
    report = await run_load_test(url, concurrency=20, total_requests=200, payload=payload)
    report.print_summary()
    assert report.error_rate < 0.05, f"Error rate too high at medium load: {report.error_rate:.1%}"
    assert report.p95 < 15000, f"p95 latency too high: {report.p95:.0f}ms"

    print("All load assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Not applicable — infrastructure test
**Environment:** `pip install aiohttp`

---

### Option 3: pytest-benchmark for Endpoint Latency Regression

```python
# tests/load/test_endpoint_benchmark.py
"""
Use pytest-benchmark to track per-request latency and detect regressions
between deployments. Integrates with CI artifact storage.
"""
import pytest
import httpx
import asyncio
from unittest.mock import patch, AsyncMock


# --- Fixture: mock the Anthropic client so benchmarks don't hit real API ---

@pytest.fixture
def mock_anthropic():
    """Replace the Anthropic client with a fast stub for benchmarking."""
    mock_response = AsyncMock()
    mock_response.content = [AsyncMock(text="Hello! How can I help you?")]
    mock_response.usage.input_tokens = 20
    mock_response.usage.output_tokens = 10
    mock_response.stop_reason = "end_turn"

    with patch("your_app.agent.client.messages.create", return_value=mock_response):
        yield


@pytest.fixture
def test_client():
    """Synchronous HTTPX client for the FastAPI app."""
    from your_app.main import app  # import your FastAPI app
    with httpx.Client(app=app, base_url="http://test") as client:
        yield client


# --- Benchmarks ---

def test_single_request_latency(benchmark, test_client, mock_anthropic):
    """Benchmark: single request end-to-end latency."""
    def make_request():
        resp = test_client.post(
            "/api/agent/chat",
            json={"message": "Hello", "max_tokens": 64},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 200
        return resp

    result = benchmark(make_request)
    # benchmark automatically tracks min/max/mean/stddev and stores history
    assert result.status_code == 200


def test_concurrent_requests_throughput(benchmark, mock_anthropic):
    """Benchmark: throughput for N concurrent requests using asyncio."""
    from your_app.main import app

    async def run_concurrent(n: int = 10):
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            tasks = [
                client.post(
                    "/api/agent/chat",
                    json={"message": f"Query {i}", "max_tokens": 64},
                    headers={"Authorization": "Bearer test"},
                )
                for i in range(n)
            ]
            responses = await asyncio.gather(*tasks)
            return [r.status_code for r in responses]

    def run():
        return asyncio.run(run_concurrent(10))

    statuses = benchmark(run)
    assert all(s == 200 for s in statuses), f"Some requests failed: {statuses}"


@pytest.mark.parametrize("payload_size", ["small", "medium", "large"])
def test_latency_by_payload_size(benchmark, test_client, mock_anthropic, payload_size):
    """Benchmark: latency across different input sizes."""
    messages = {
        "small": "Hi",
        "medium": "Explain Python decorators.",
        "large": "Write a detailed comparison of microservices vs monolithic architectures, "
                 "covering scalability, maintainability, deployment complexity, and team organization.",
    }
    msg = messages[payload_size]

    def make_request():
        return test_client.post(
            "/api/agent/chat",
            json={"message": msg, "max_tokens": 256},
            headers={"Authorization": "Bearer test"},
        )

    result = benchmark(make_request)
    assert result.status_code == 200
```

```bash
# Run benchmarks with JSON output for CI artifact storage
pytest tests/load/test_endpoint_benchmark.py \
  --benchmark-json=benchmark_results.json \
  --benchmark-compare \
  --benchmark-compare-fail=mean:10%  # fail if mean latency regresses > 10%
```

**Expected Token Savings:** Not applicable — mock-backed benchmarks
**Environment:** `pip install pytest-benchmark httpx`

---

### Option 4: Gradual Ramp-Up with SLO Gate

```python
# load_tests/ramp_test.py
"""
Gradually increase concurrency and stop as soon as an SLO is violated.
Identifies the maximum safe operating concurrency for an agent endpoint.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional
import aiohttp


@dataclass
class SLO:
    max_error_rate: float = 0.01      # 1%
    max_p95_latency_ms: float = 8000  # 8s
    max_p99_latency_ms: float = 20000 # 20s


async def probe_concurrency_level(
    url: str,
    concurrency: int,
    num_requests: int = 50,
    payload: Optional[dict] = None,
) -> dict:
    """Send num_requests at given concurrency and return stats."""
    if payload is None:
        payload = {"message": "What is the capital of France?", "max_tokens": 64}

    latencies = []
    errors = 0
    semaphore = asyncio.Semaphore(concurrency)

    async def one_request(session):
        nonlocal errors
        async with semaphore:
            start = time.perf_counter()
            try:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    elapsed = (time.perf_counter() - start) * 1000
                    latencies.append(elapsed)
                    if resp.status != 200:
                        errors += 1
            except Exception:
                elapsed = (time.perf_counter() - start) * 1000
                latencies.append(elapsed)
                errors += 1

    connector = aiohttp.TCPConnector(limit=concurrency + 5)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[one_request(session) for _ in range(num_requests)])

    sorted_l = sorted(latencies)
    n = len(sorted_l)
    return {
        "concurrency": concurrency,
        "total": num_requests,
        "errors": errors,
        "error_rate": errors / num_requests,
        "p50": sorted_l[int(n * 0.50)] if n else 0,
        "p95": sorted_l[int(n * 0.95)] if n else 0,
        "p99": sorted_l[int(n * 0.99)] if n else 0,
    }


async def find_max_safe_concurrency(
    url: str,
    slo: SLO,
    levels: list[int] = [1, 5, 10, 20, 30, 50, 75, 100],
) -> int:
    max_safe = 0
    for level in levels:
        print(f"\nProbing concurrency={level}...")
        stats = await probe_concurrency_level(url, concurrency=level)
        print(
            f"  error_rate={stats['error_rate']:.1%}, "
            f"p95={stats['p95']:.0f}ms, p99={stats['p99']:.0f}ms"
        )

        if stats["error_rate"] > slo.max_error_rate:
            print(f"  -> SLO BREACH: error_rate {stats['error_rate']:.1%} > {slo.max_error_rate:.1%}")
            break
        if stats["p95"] > slo.max_p95_latency_ms:
            print(f"  -> SLO BREACH: p95 {stats['p95']:.0f}ms > {slo.max_p95_latency_ms:.0f}ms")
            break
        if stats["p99"] > slo.max_p99_latency_ms:
            print(f"  -> SLO BREACH: p99 {stats['p99']:.0f}ms > {slo.max_p99_latency_ms:.0f}ms")
            break

        max_safe = level
        print(f"  -> OK, max safe concurrency updated to {max_safe}")

    return max_safe


async def main():
    url = "http://localhost:8000/api/agent/chat"
    slo = SLO()
    max_safe = await find_max_safe_concurrency(url, slo)
    print(f"\n=== Result: max safe concurrency = {max_safe} ===")

    # Write result for CI artifact
    import json
    with open("max_concurrency.json", "w") as f:
        json.dump({"max_safe_concurrency": max_safe, "slo": vars(slo)}, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Not applicable — capacity planning test
**Environment:** `pip install aiohttp`

---

### Option 5: k6 Script with Threshold Assertions

```javascript
// load_tests/agent_load_test.js
// Run with: k6 run load_tests/agent_load_test.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';

// Custom metrics
const agentLatency = new Trend('agent_latency_ms', true);
const successRate = new Rate('agent_success_rate');
const tokenCost = new Counter('estimated_token_cost');

// SLO thresholds — test fails if any are breached
export const options = {
  stages: [
    { duration: '30s', target: 5 },   // ramp up
    { duration: '1m',  target: 20 },  // hold
    { duration: '30s', target: 50 },  // stress spike
    { duration: '30s', target: 0 },   // ramp down
  ],
  thresholds: {
    agent_latency_ms: ['p(95)<10000', 'p(99)<20000'],
    agent_success_rate: ['rate>0.99'],
    http_req_failed: ['rate<0.01'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const AUTH_TOKEN = __ENV.AUTH_TOKEN || 'test-token';

export default function () {
  const payload = JSON.stringify({
    message: 'Summarize the benefits of cloud-native architecture.',
    max_tokens: 128,
  });

  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${AUTH_TOKEN}`,
    },
    timeout: '30s',
  };

  const res = http.post(`${BASE_URL}/api/agent/chat`, payload, params);

  // Record custom metrics
  agentLatency.add(res.timings.duration);
  const ok = check(res, {
    'status is 200': (r) => r.status === 200,
    'has response field': (r) => {
      try { return JSON.parse(r.body).response !== undefined; }
      catch { return false; }
    },
    'latency < 15s': (r) => r.timings.duration < 15000,
  });
  successRate.add(ok);

  sleep(Math.random() * 2 + 1); // 1-3s think time
}

export function handleSummary(data) {
  return {
    'load_test_summary.json': JSON.stringify(data, null, 2),
    stdout: `
=== k6 Agent Load Test Summary ===
Requests:      ${data.metrics.http_reqs.values.count}
Error rate:    ${(data.metrics.http_req_failed.values.rate * 100).toFixed(2)}%
P95 latency:   ${data.metrics.agent_latency_ms.values['p(95)'].toFixed(0)}ms
P99 latency:   ${data.metrics.agent_latency_ms.values['p(99)'].toFixed(0)}ms
Success rate:  ${(data.metrics.agent_success_rate.values.rate * 100).toFixed(2)}%
`,
  };
}
```

```python
# Companion Python wrapper to run k6 from pytest and assert on the report
import subprocess
import json
import pytest


@pytest.mark.load
def test_k6_load_test():
    result = subprocess.run(
        ["k6", "run", "load_tests/agent_load_test.js",
         "--env", "BASE_URL=http://localhost:8000",
         "--summary-export=k6_summary.json"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"k6 thresholds failed:\n{result.stdout}\n{result.stderr}"
    with open("k6_summary.json") as f:
        report = json.load(f)
    error_rate = report["metrics"]["http_req_failed"]["values"]["rate"]
    assert error_rate < 0.01, f"Error rate {error_rate:.1%} exceeds 1%"
```

**Expected Token Savings:** Not applicable — load test harness
**Environment:** `k6` binary + `pip install pytest`

---

### Option 6: Chaos + Load Combined — Drop Connections Under Load

```python
# load_tests/chaos_load_test.py
"""
Simultaneously run load AND inject failures (random connection drops, slow
responses) to validate that the agent endpoint degrades gracefully and
recovers without cascading failures.
"""
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import List
import aiohttp
from aiohttp import web


# --- Chaos proxy that randomly drops or delays requests ---

class ChaosProxy:
    """
    Thin aiohttp reverse proxy that randomly drops or delays requests.
    Start on an alternate port and point load test at it.
    """
    def __init__(self, target_url: str, drop_rate: float = 0.05, slow_rate: float = 0.10):
        self.target_url = target_url
        self.drop_rate = drop_rate
        self.slow_rate = slow_rate

    async def handle(self, request: web.Request) -> web.Response:
        roll = random.random()

        if roll < self.drop_rate:
            # Simulate a dropped connection
            raise web.HTTPServiceUnavailable(reason="chaos: connection dropped")

        if roll < self.drop_rate + self.slow_rate:
            # Simulate a slow backend
            await asyncio.sleep(random.uniform(3, 8))

        # Forward the request
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method=request.method,
                url=f"{self.target_url}{request.path_qs}",
                headers=dict(request.headers),
                data=await request.read(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.read()
                return web.Response(
                    body=body,
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items() if k != "Transfer-Encoding"},
                )

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{path_info:.*}", self.handle)
        return app


@dataclass
class ChaosTestReport:
    total: int = 0
    successes: int = 0
    client_errors: int = 0  # 4xx
    server_errors: int = 0  # 5xx
    timeouts: int = 0
    chaos_drops: int = 0
    latencies: List[float] = field(default_factory=list)

    def print_summary(self):
        p95 = sorted(self.latencies)[int(len(self.latencies) * 0.95)] if self.latencies else 0
        print(f"\n{'='*45}")
        print(f"Total requests : {self.total}")
        print(f"Successes      : {self.successes} ({self.successes/max(self.total,1):.1%})")
        print(f"Server errors  : {self.server_errors}")
        print(f"Client errors  : {self.client_errors}")
        print(f"Timeouts       : {self.timeouts}")
        print(f"Chaos drops    : {self.chaos_drops}")
        print(f"P95 latency    : {p95:.0f}ms")
        print(f"{'='*45}\n")


async def chaos_load_test(
    proxy_url: str,
    concurrency: int = 20,
    total_requests: int = 200,
) -> ChaosTestReport:
    report = ChaosTestReport()
    sem = asyncio.Semaphore(concurrency)

    async def one_request(session):
        async with sem:
            start = time.perf_counter()
            try:
                async with session.post(
                    f"{proxy_url}/api/agent/chat",
                    json={"message": "Hello", "max_tokens": 64},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    elapsed = (time.perf_counter() - start) * 1000
                    report.total += 1
                    report.latencies.append(elapsed)
                    if resp.status == 200:
                        report.successes += 1
                    elif resp.status == 503:
                        report.chaos_drops += 1
                    elif resp.status >= 500:
                        report.server_errors += 1
                    else:
                        report.client_errors += 1
            except asyncio.TimeoutError:
                report.total += 1
                report.timeouts += 1
                report.latencies.append(15000)

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*[one_request(session) for _ in range(total_requests)])

    return report


async def main():
    # Start chaos proxy
    proxy = ChaosProxy("http://localhost:8000", drop_rate=0.05, slow_rate=0.10)
    runner = web.AppRunner(proxy.build_app())
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 8001)
    await site.start()
    print("Chaos proxy running on :8001 (5% drops, 10% slow)")

    try:
        report = await chaos_load_test("http://localhost:8001")
        report.print_summary()

        # Under chaos: expect ~85%+ success (95% not dropped * 90% not slow ≈ 85.5%)
        success_rate = report.successes / max(report.total, 1)
        assert success_rate >= 0.80, f"Success rate too low under chaos: {success_rate:.1%}"
        assert report.timeouts < report.total * 0.15, "Too many timeouts under chaos"
        print("Chaos load test PASSED.")
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Not applicable — chaos engineering test
**Environment:** `pip install aiohttp`

---

## Comparison Table

| Option | Tool | Concurrency Model | SLO Assertion | CI-Friendly | Chaos Support |
|--------|------|-------------------|---------------|-------------|---------------|
| 1: Locust ramp-up | Locust | process workers | Yes (events) | Yes | No |
| 2: asyncio stress | aiohttp | asyncio gather | Yes (asserts) | Yes | No |
| 3: pytest-benchmark | pytest-benchmark | asyncio gather | Regression % | Yes (history) | No |
| 4: Gradual ramp | aiohttp | asyncio semaphore | SLO gate | Yes | No |
| 5: k6 script | k6 + subprocess | VU goroutines | Thresholds | Yes | No |
| 6: Chaos + load | aiohttp proxy | asyncio semaphore | Custom asserts | Yes | Yes |
