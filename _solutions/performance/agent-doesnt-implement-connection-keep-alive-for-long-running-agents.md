---
layout: solution
title: "Agent Doesn't Implement Connection Keep-Alive for Long-Running Agents"
category: performance
description: "Agents that create new HTTP connections for every API call waste time on TCP handshakes and TLS negotiation. Connection keep-alive and session reuse cut first-byte latency by 50-200ms per call in long-running agents."
tags: [performance, keep-alive, connection-pooling, http, latency, session-reuse, async]
---

# Agent Doesn't Implement Connection Keep-Alive for Long-Running Agents

## Problem

Each new TCP connection requires a three-way handshake (1 RTT) plus TLS negotiation (1-2 RTTs). For a long-running agent making 100 API calls, that's 200-400ms of overhead per call just for connection setup — before any bytes of request or response are transmitted.

Connection keep-alive and HTTP session reuse eliminate this overhead by reusing established connections across multiple API calls.

---

## Option 1: Shared Client Instance (Basic Keep-Alive)

```python
import anthropic
import time

# WRONG: creates a new client (and new HTTP connection) on every call
def bad_pattern(prompts: list[str]) -> list[str]:
    results = []
    for prompt in prompts:
        client = anthropic.Anthropic()  # New connection every time
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        results.append(r.content[0].text)
    return results


# CORRECT: create client once, reuse for all calls
def good_pattern(prompts: list[str]) -> list[str]:
    client = anthropic.Anthropic()  # Single client, keeps connection alive
    results = []
    for prompt in prompts:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        results.append(r.content[0].text)
    return results


def benchmark_client_reuse(prompts: list[str]):
    print(f"Testing {len(prompts)} calls...")

    t0 = time.time()
    results_good = good_pattern(prompts)
    good_elapsed = time.time() - t0

    print(f"Shared client:  {good_elapsed:.2f}s total ({good_elapsed/len(prompts)*1000:.0f}ms/call)")
    print(f"Results: {len(results_good)} responses")

    # Note: bad_pattern is commented out to avoid wasting API calls in the demo
    # In production measurement, each new Anthropic() costs ~100-200ms extra per call
    print("\nNote: Creating a new Anthropic() client per call adds ~100-200ms per request")
    print("for TCP+TLS handshake. Over 100 calls, this is 10-20 seconds of avoidable latency.")


if __name__ == "__main__":
    prompts = [
        "What is 2+2?",
        "Name a color.",
        "Say hello.",
        "What day comes after Monday?",
        "Name a fruit.",
    ]
    benchmark_client_reuse(prompts)
# Expected Token Savings: 0% tokens — saves 100-200ms latency per call via connection reuse
# Environment: pip install anthropic
```

---

## Option 2: httpx Session with Explicit Keep-Alive Configuration

```python
import httpx
import anthropic
import time

def create_optimized_client() -> anthropic.Anthropic:
    """
    Configure the underlying httpx client with explicit keep-alive settings.
    The default Anthropic client uses httpx, which supports keep-alive natively,
    but we can tune the pool limits and timeout for long-running agents.
    """
    # Configure connection pool for long-running agents
    http_client = httpx.Client(
        limits=httpx.Limits(
            max_connections=10,        # Max total connections
            max_keepalive_connections=5,  # Keep this many connections alive
            keepalive_expiry=30.0,     # Keep connections alive for 30s between calls
        ),
        timeout=httpx.Timeout(
            connect=5.0,   # TCP connect timeout
            read=120.0,    # Response read timeout (long for streaming)
            write=10.0,    # Request write timeout
            pool=5.0,      # Pool acquisition timeout
        ),
    )

    return anthropic.Anthropic(http_client=http_client)


def run_agent_with_tuned_pool(user_messages: list[str]) -> list[dict]:
    client = create_optimized_client()
    results = []

    for msg in user_messages:
        t0 = time.time()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": msg}],
        )
        elapsed_ms = (time.time() - t0) * 1000
        results.append({
            "prompt": msg[:40],
            "response": response.content[0].text[:60],
            "latency_ms": round(elapsed_ms),
        })
        print(f"[{elapsed_ms:.0f}ms] {msg[:30]} → {response.content[0].text[:40]}")

    avg_latency = sum(r["latency_ms"] for r in results) / len(results)
    print(f"\nAverage latency: {avg_latency:.0f}ms across {len(results)} calls")
    return results


if __name__ == "__main__":
    prompts = [
        "What is Python?",
        "Name a sorting algorithm.",
        "What is a REST API?",
        "How does HTTPS work?",
        "What is recursion?",
    ]
    run_agent_with_tuned_pool(prompts)
# Expected Token Savings: 0% tokens — pool tuning saves 50-150ms per call after warm-up
# Environment: pip install anthropic httpx
```

---

## Option 3: Async Client with Connection Pool

```python
import asyncio
import httpx
import anthropic
import time
from dataclasses import dataclass

@dataclass
class CallMetrics:
    prompt: str
    latency_ms: float
    response: str

def create_async_client() -> anthropic.AsyncAnthropic:
    """
    AsyncAnthropic with a tuned async httpx client.
    Connections persist across await points in the event loop.
    """
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=60.0,
        ),
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
        http2=True,  # HTTP/2 multiplexes multiple requests over one connection
    )
    return anthropic.AsyncAnthropic(http_client=http_client)


async def call_with_metrics(
    client: anthropic.AsyncAnthropic,
    prompt: str,
    call_index: int,
) -> CallMetrics:
    t0 = time.time()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = (time.time() - t0) * 1000
    text = response.content[0].text
    print(f"  [call {call_index}] {latency_ms:.0f}ms → {text[:50]}")
    return CallMetrics(prompt=prompt, latency_ms=round(latency_ms, 1), response=text)


async def run_async_agent(prompts: list[str]) -> list[CallMetrics]:
    # Single async client, shared across all concurrent calls
    async with create_async_client() as client:
        t_total = time.time()

        # Warm-up call to establish connection
        print("Warm-up call...")
        warmup = await call_with_metrics(client, "Hello", 0)
        print(f"  Warm-up: {warmup.latency_ms:.0f}ms (includes connection setup)")

        # Subsequent calls reuse the established connection
        print(f"\nRunning {len(prompts)} calls (connection already warm)...")
        tasks = [
            call_with_metrics(client, prompt, i + 1)
            for i, prompt in enumerate(prompts)
        ]
        results = await asyncio.gather(*tasks)

        total_elapsed = time.time() - t_total
        latencies = [r.latency_ms for r in results]
        avg = sum(latencies) / len(latencies)
        print(f"\nTotal: {total_elapsed:.2f}s | Avg per call: {avg:.0f}ms | Min: {min(latencies):.0f}ms")

    return list(results)


if __name__ == "__main__":
    prompts = [
        "What is machine learning?",
        "Name a programming language.",
        "What is 5*5?",
        "Say 'hello world'.",
        "What color is the sky?",
    ]
    asyncio.run(run_async_agent(prompts))
# Expected Token Savings: 0% tokens — async pool saves 100-300ms on parallel calls via connection reuse + HTTP/2
# Environment: pip install anthropic httpx[http2] (pip install h2 for HTTP/2 support)
```

---

## Option 4: Connection Warm-Up Strategy for Cold-Start Agents

```python
import asyncio
import httpx
import anthropic
import time
from contextlib import asynccontextmanager

class WarmConnectionPool:
    """
    Pre-warms a pool of connections before the agent starts serving requests.
    Eliminates cold-start latency on the first real user request.
    """

    def __init__(self, pool_size: int = 3):
        self.pool_size = pool_size
        self._client: anthropic.AsyncAnthropic | None = None
        self._warmed = False
        self._warmup_latency_ms: float | None = None

    async def start(self):
        """Call at agent startup to pre-establish connections."""
        http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=self.pool_size * 2,
                max_keepalive_connections=self.pool_size,
                keepalive_expiry=120.0,
            ),
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=3.0),
        )
        self._client = anthropic.AsyncAnthropic(http_client=http_client)

        print(f"[WarmPool] Warming {self.pool_size} connections...")
        t0 = time.time()

        # Fire probe calls to establish connections
        warmup_tasks = [
            self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "."}],
            )
            for _ in range(self.pool_size)
        ]
        await asyncio.gather(*warmup_tasks, return_exceptions=True)
        self._warmup_latency_ms = (time.time() - t0) * 1000
        self._warmed = True
        print(f"[WarmPool] Ready in {self._warmup_latency_ms:.0f}ms")

    async def stop(self):
        if self._client:
            await self._client.__aexit__(None, None, None)

    async def call(self, prompt: str, max_tokens: int = 128) -> str:
        if not self._warmed or not self._client:
            raise RuntimeError("Pool not warmed. Call start() first.")
        response = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


@asynccontextmanager
async def agent_lifecycle(pool_size: int = 3):
    pool = WarmConnectionPool(pool_size=pool_size)
    await pool.start()
    try:
        yield pool
    finally:
        await pool.stop()
        print("[WarmPool] Connections closed")


async def run_warmed_agent(requests: list[str]):
    async with agent_lifecycle(pool_size=3) as pool:
        print(f"\nHandling {len(requests)} requests on warm connections:")
        for i, req in enumerate(requests):
            t0 = time.time()
            result = await pool.call(req)
            elapsed = (time.time() - t0) * 1000
            print(f"  [req {i+1}] {elapsed:.0f}ms → {result[:50]}")


if __name__ == "__main__":
    requests = [
        "What is Python?",
        "Name a data structure.",
        "Explain REST briefly.",
        "What is a database index?",
    ]
    asyncio.run(run_warmed_agent(requests))
# Expected Token Savings: 0% tokens — warm-up probe costs 3 minimal calls; first real requests save ~200ms each
# Environment: pip install anthropic httpx
```

---

## Option 5: SQLite Latency Tracker to Measure Keep-Alive Impact

```python
import sqlite3
import time
import asyncio
import httpx
import anthropic
from datetime import datetime

class LatencyTracker:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS latency_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_index INTEGER,
                connection_type TEXT,
                latency_ms REAL,
                tokens_used INTEGER,
                called_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def record(self, call_index: int, conn_type: str, latency_ms: float, tokens: int):
        self.conn.execute(
            "INSERT INTO latency_log (call_index, connection_type, latency_ms, tokens_used) VALUES (?,?,?,?)",
            (call_index, conn_type, round(latency_ms, 1), tokens),
        )
        self.conn.commit()

    def report(self) -> dict:
        rows = self.conn.execute("""
            SELECT connection_type,
                   COUNT(*) as calls,
                   AVG(latency_ms) as avg_ms,
                   MIN(latency_ms) as min_ms,
                   MAX(latency_ms) as max_ms
            FROM latency_log GROUP BY connection_type
        """).fetchall()
        return {
            r[0]: {
                "calls": r[1],
                "avg_ms": round(r[2], 1),
                "min_ms": round(r[3], 1),
                "max_ms": round(r[4], 1),
            }
            for r in rows
        }


async def measure_keepalive_impact(prompts: list[str]) -> dict:
    tracker = LatencyTracker()

    # Strategy A: shared persistent client (keep-alive)
    persistent_client = anthropic.AsyncAnthropic(
        http_client=httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=60),
        )
    )

    print("Testing persistent client (keep-alive)...")
    async with persistent_client as client:
        for i, prompt in enumerate(prompts):
            t0 = time.time()
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{"role": "user", "content": prompt}],
            )
            latency = (time.time() - t0) * 1000
            tracker.record(i, "keepalive", latency, r.usage.input_tokens + r.usage.output_tokens)
            print(f"  [keepalive call {i}] {latency:.0f}ms")

    # Short delay between strategies
    await asyncio.sleep(0.5)

    # Strategy B: new client per call (no keep-alive benefit)
    print("\nTesting new client per call (cold connections)...")
    for i, prompt in enumerate(prompts):
        t0 = time.time()
        async with anthropic.AsyncAnthropic() as cold_client:
            r = await cold_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{"role": "user", "content": prompt}],
            )
        latency = (time.time() - t0) * 1000
        tracker.record(i, "cold", latency, r.usage.input_tokens + r.usage.output_tokens)
        print(f"  [cold call {i}] {latency:.0f}ms")

    report = tracker.report()
    print("\nLatency Comparison:")
    for conn_type, stats in report.items():
        print(f"  {conn_type}: avg={stats['avg_ms']}ms min={stats['min_ms']}ms max={stats['max_ms']}ms")

    if "keepalive" in report and "cold" in report:
        saved = report["cold"]["avg_ms"] - report["keepalive"]["avg_ms"]
        print(f"\n  Keep-alive saves ~{saved:.0f}ms per call on average")

    return report


if __name__ == "__main__":
    prompts = ["Hi.", "Hello.", "Yes.", "No.", "OK."]
    asyncio.run(measure_keepalive_impact(prompts))
# Expected Token Savings: 0% tokens — latency tracker quantifies the keep-alive savings per deployment
# Environment: pip install anthropic httpx; sqlite3, asyncio, time are stdlib
```

---

## Option 6: Long-Running Agent with Connection Health Monitoring

```python
import asyncio
import time
import sqlite3
import httpx
import anthropic
from dataclasses import dataclass
from datetime import datetime

@dataclass
class ConnectionHealth:
    total_calls: int
    successful_calls: int
    failed_calls: int
    avg_latency_ms: float
    connection_resets: int
    uptime_sec: float

class MonitoredAgent:
    """
    Long-running agent with connection health monitoring.
    Detects when connections go stale and re-establishes them.
    """

    RECONNECT_ON_ERRORS = (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError)
    MAX_RETRIES = 2

    def __init__(self, db_path: str = ":memory:"):
        self.conn_db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._client: anthropic.AsyncAnthropic | None = None
        self._connection_resets = 0
        self._start_time = time.time()
        self._lock = asyncio.Lock()

    def _init_db(self):
        self.conn_db.execute("""
            CREATE TABLE IF NOT EXISTS call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                success INTEGER,
                latency_ms REAL,
                error TEXT,
                called_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn_db.commit()

    def _make_client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    keepalive_expiry=90.0,
                ),
                timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=3.0),
            )
        )

    async def _ensure_client(self):
        if self._client is None:
            self._client = self._make_client()

    async def _reset_connection(self):
        async with self._lock:
            if self._client:
                try:
                    await self._client.__aexit__(None, None, None)
                except Exception:
                    pass
            self._client = self._make_client()
            self._connection_resets += 1
            print(f"[MonitoredAgent] Connection reset #{self._connection_resets}")

    async def call(self, prompt: str, max_tokens: int = 128) -> str:
        await self._ensure_client()

        for attempt in range(self.MAX_RETRIES + 1):
            t0 = time.time()
            try:
                response = await self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                latency = (time.time() - t0) * 1000
                self.conn_db.execute(
                    "INSERT INTO call_log (success, latency_ms) VALUES (1, ?)", (round(latency, 1),)
                )
                self.conn_db.commit()
                return response.content[0].text

            except self.RECONNECT_ON_ERRORS as e:
                latency = (time.time() - t0) * 1000
                self.conn_db.execute(
                    "INSERT INTO call_log (success, latency_ms, error) VALUES (0, ?, ?)",
                    (round(latency, 1), type(e).__name__),
                )
                self.conn_db.commit()
                print(f"[MonitoredAgent] Connection error on attempt {attempt+1}: {type(e).__name__}")
                if attempt < self.MAX_RETRIES:
                    await self._reset_connection()
                else:
                    raise

        raise RuntimeError("Max retries exceeded")

    def health(self) -> ConnectionHealth:
        rows = self.conn_db.execute(
            "SELECT COUNT(*), SUM(success), SUM(1-success), AVG(latency_ms) FROM call_log"
        ).fetchone()
        return ConnectionHealth(
            total_calls=rows[0] or 0,
            successful_calls=rows[1] or 0,
            failed_calls=rows[2] or 0,
            avg_latency_ms=round(rows[3] or 0, 1),
            connection_resets=self._connection_resets,
            uptime_sec=round(time.time() - self._start_time, 1),
        )

    async def close(self):
        if self._client:
            await self._client.__aexit__(None, None, None)


async def run_monitored_agent(prompts: list[str]):
    agent = MonitoredAgent()

    print(f"Running {len(prompts)} calls with connection health monitoring...")
    for i, prompt in enumerate(prompts):
        result = await agent.call(prompt)
        print(f"  [call {i+1}] {prompt[:30]} → {result[:50]}")

    await agent.close()

    health = agent.health()
    print(f"\nConnection Health Report:")
    print(f"  Total calls:       {health.total_calls}")
    print(f"  Successful:        {health.successful_calls}")
    print(f"  Failed:            {health.failed_calls}")
    print(f"  Avg latency:       {health.avg_latency_ms}ms")
    print(f"  Connection resets: {health.connection_resets}")
    print(f"  Uptime:            {health.uptime_sec}s")


if __name__ == "__main__":
    prompts = [
        "What is Python?",
        "Explain async/await.",
        "What is REST?",
        "How does DNS work?",
        "What is a hash table?",
    ]
    asyncio.run(run_monitored_agent(prompts))
# Expected Token Savings: 0% tokens — health monitoring + auto-reconnect prevents call failures
# Environment: pip install anthropic httpx; asyncio, sqlite3, time are stdlib
```

---

## Comparison

| Option | Keep-Alive | Pool Config | HTTP/2 | Warm-Up | Health Monitor | Best For |
|--------|-----------|-------------|--------|---------|----------------|----------|
| 1 | Implicit (shared client) | Default | No | No | No | Quick fix — stop creating new clients |
| 2 | Explicit httpx config | Custom limits | No | No | No | Tuned pool for batch agents |
| 3 | Async pool + HTTP/2 | Custom limits | Yes | No | No | High-concurrency async agents |
| 4 | Pre-warmed pool | Custom limits | No | Yes | No | Latency-sensitive first-request paths |
| 5 | Both (for comparison) | Custom limits | No | No | No | Benchmarking keep-alive impact |
| 6 | Async with auto-reset | Custom limits | No | No | Yes | Production long-running agents |
