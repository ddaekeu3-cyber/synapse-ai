---
title: "Agent Doesn't Implement Connection Multiplexing with HTTP/2"
description: "AI agents open a new TCP connection for every API call or use HTTP/1.1 connection pooling, leaving significant latency and throughput on the table compared to HTTP/2 multiplexing."
category: performance
difficulty: intermediate
tags: [http2, multiplexing, connection-pooling, latency, throughput, httpx, aiohttp, grpc]
---

# Agent Doesn't Implement Connection Multiplexing with HTTP/2

## Problem

HTTP/1.1 can handle only one request per connection at a time (pipelining is rarely used). Agents that make concurrent API calls either open many connections (expensive TLS handshakes, TCP slow start) or queue requests behind a small pool. HTTP/2 multiplexes hundreds of requests over a single connection, sharing the same TLS session and window — eliminating head-of-line blocking at the connection level and reducing latency by 20–50% for bursty workloads.

## Solution 1: httpx AsyncClient with HTTP/2 Enabled

The simplest upgrade: replace `aiohttp` or `requests` with `httpx` and set `http2=True`.

```python
import asyncio
import httpx
import time

# Single long-lived client — HTTP/2 multiplexes all concurrent requests
_http2_client: httpx.AsyncClient | None = None

async def get_http2_client() -> httpx.AsyncClient:
    global _http2_client
    if _http2_client is None or _http2_client.is_closed:
        _http2_client = httpx.AsyncClient(
            http2=True,
            limits=httpx.Limits(
                max_connections=10,          # far fewer connections needed with HTTP/2
                max_keepalive_connections=5,
                keepalive_expiry=60.0,
            ),
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )
    return _http2_client

async def api_call(url: str, payload: dict, api_key: str) -> dict:
    client = await get_http2_client()
    resp = await client.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()

# Demonstrate multiplexing: 20 concurrent calls share one HTTP/2 connection
async def benchmark():
    t0 = time.monotonic()
    tasks = [
        api_call("https://httpbin.org/post", {"idx": i}, "dummy-key")
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.monotonic() - t0
    successful = sum(1 for r in results if not isinstance(r, Exception))
    print(f"20 concurrent calls: {elapsed:.2f}s, {successful} succeeded")
    # With HTTP/2: all 20 share one connection → single TLS handshake overhead

async def shutdown():
    if _http2_client:
        await _http2_client.aclose()
```

**When to use**: Any agent making concurrent API calls. Install `httpx[http2]` — one flag change, measurable latency reduction.

---

## Solution 2: Anthropic SDK with HTTP/2 Transport Override

Override the Anthropic SDK's underlying transport to use HTTP/2 without changing your SDK usage.

```python
import asyncio
import httpx
from anthropic import AsyncAnthropic
from anthropic._base_client import AsyncHttpxClientWrapper

# Create a shared HTTP/2 client for the SDK to use
_shared_h2_transport = httpx.AsyncHTTPTransport(http2=True)
_shared_h2_client = httpx.AsyncClient(
    transport=_shared_h2_transport,
    http2=True,
    limits=httpx.Limits(
        max_connections=5,
        max_keepalive_connections=5,
        keepalive_expiry=300.0,
    ),
)

def make_anthropic_h2_client() -> AsyncAnthropic:
    """Create an Anthropic client that uses HTTP/2 multiplexing."""
    return AsyncAnthropic(
        http_client=_shared_h2_client,
    )

anthropic_client = make_anthropic_h2_client()

async def concurrent_agent_calls(prompts: list[str]) -> list[str]:
    """All calls multiplex over a single HTTP/2 connection."""
    tasks = [
        anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": p}],
        )
        for p in prompts
    ]
    responses = await asyncio.gather(*tasks)
    return [r.content[0].text for r in responses]

async def cleanup():
    await _shared_h2_client.aclose()
```

**When to use**: Agents using the official Anthropic Python SDK. All concurrent messages.create calls benefit immediately.

---

## Solution 3: HTTP/2 Server Push for Pre-loading Tool Schemas

Use HTTP/2 server push (where supported) to pre-load frequently used resources before the client requests them.

```python
import asyncio
import httpx
import json
import time
from functools import lru_cache

# Tool schema CDN endpoint that supports HTTP/2
TOOL_SCHEMA_BASE = "https://schemas.api.example.com/tools"

class H2PrefetchingSchemaLoader:
    """Load tool schemas over HTTP/2 with connection reuse and prefetching."""

    def __init__(self):
        self._client = httpx.AsyncClient(http2=True, timeout=10.0)
        self._cache: dict[str, dict] = {}
        self._loading: dict[str, asyncio.Task] = {}

    async def preload(self, tool_names: list[str]) -> None:
        """Concurrently prefetch all tool schemas — multiplexed over single H/2 connection."""
        tasks = {
            name: asyncio.create_task(self._fetch_schema(name))
            for name in tool_names
            if name not in self._cache
        }
        if tasks:
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for name, result in zip(tasks.keys(), results):
                if not isinstance(result, Exception):
                    self._cache[name] = result

    async def _fetch_schema(self, tool_name: str) -> dict:
        resp = await self._client.get(f"{TOOL_SCHEMA_BASE}/{tool_name}.json")
        resp.raise_for_status()
        return resp.json()

    async def get(self, tool_name: str) -> dict:
        if tool_name not in self._cache:
            self._cache[tool_name] = await self._fetch_schema(tool_name)
        return self._cache[tool_name]

    def protocol_info(self) -> dict:
        """Inspect which HTTP version is actually being used."""
        # httpx exposes this per-response; here we approximate
        return {"http2_enabled": True, "cached_schemas": len(self._cache)}

    async def close(self):
        await self._client.aclose()

loader = H2PrefetchingSchemaLoader()

# On startup: preload all tool schemas in one H/2 burst
async def startup():
    await loader.preload(["search", "write_file", "read_file", "execute_code", "query_db"])
```

**When to use**: Agents that load tool schemas or configuration from remote endpoints. H/2 multiplexing turns N sequential round trips into 1 RTT.

---

## Solution 4: gRPC for Model API Calls (Where Supported)

gRPC uses HTTP/2 natively and adds streaming, bidirectional communication, and binary framing — lower overhead than JSON over HTTP/1.1.

```python
import asyncio
import grpc
from typing import AsyncIterator

# Illustrative: connect to a gRPC-enabled inference service
# (e.g., Triton Inference Server, TensorFlow Serving, or a custom gRPC wrapper)

async def create_grpc_channel(endpoint: str) -> grpc.aio.Channel:
    """Create a gRPC channel with HTTP/2 multiplexing and TLS."""
    credentials = grpc.ssl_channel_credentials()
    channel = grpc.aio.secure_channel(
        endpoint,
        credentials,
        options=[
            ("grpc.keepalive_time_ms", 10_000),
            ("grpc.keepalive_timeout_ms", 5_000),
            ("grpc.keepalive_permit_without_calls", True),
            ("grpc.http2.max_pings_without_data", 0),
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),  # 64 MB
        ],
    )
    return channel

# Streaming inference over gRPC / HTTP/2
async def stream_inference(channel: grpc.aio.Channel, prompt: str) -> AsyncIterator[str]:
    """Stream tokens from a gRPC inference endpoint."""
    # Pseudo-stub (replace with your actual protobuf-generated stub)
    # stub = InferenceServiceStub(channel)
    # request = InferRequest(prompt=prompt, max_tokens=1024)
    # async for response in stub.StreamInfer(request):
    #     yield response.token

    # Illustrative placeholder:
    for token in prompt.split():
        yield token
        await asyncio.sleep(0.01)

async def concurrent_grpc_inference(prompts: list[str], endpoint: str) -> list[str]:
    channel = await create_grpc_channel(endpoint)
    try:
        # All gRPC streams share one HTTP/2 connection
        async def collect(prompt: str) -> str:
            return "".join([token async for token in stream_inference(channel, prompt)])

        results = await asyncio.gather(*[collect(p) for p in prompts])
        return list(results)
    finally:
        await channel.close()
```

**When to use**: Self-hosted model inference (Triton, TF Serving, vLLM) where you control the server. 40% lower overhead than REST for token streaming.

---

## Solution 5: Connection Warm-Up to Eliminate First-Request Latency

HTTP/2 connections take one RTT to establish. Pre-warm the connection pool on startup so the first real request doesn't pay this cost.

```python
import asyncio
import httpx
import logging
import time

logger = logging.getLogger(__name__)

class WarmH2Pool:
    """Pre-warmed HTTP/2 connection pool ready before first request."""

    def __init__(self, base_url: str, pool_size: int = 3):
        self._base_url = base_url
        self._pool_size = pool_size
        self._clients: list[httpx.AsyncClient] = []
        self._idx = 0
        self._warmed = False

    async def warm_up(self) -> None:
        """Establish pool_size HTTP/2 connections to the target host."""
        t0 = time.monotonic()
        warm_tasks = []
        for _ in range(self._pool_size):
            client = httpx.AsyncClient(
                base_url=self._base_url,
                http2=True,
                limits=httpx.Limits(max_connections=20, keepalive_expiry=300.0),
            )
            self._clients.append(client)
            # Send a HEAD request to establish and cache the TCP+TLS+H2 connection
            warm_tasks.append(client.head("/"))

        results = await asyncio.gather(*warm_tasks, return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            logger.warning("h2_warmup_partial", extra={"errors": len(errors)})

        self._warmed = True
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info("h2_pool_warmed", extra={
            "connections": self._pool_size - len(errors),
            "elapsed_ms": round(elapsed_ms, 1),
        })

    def _next_client(self) -> httpx.AsyncClient:
        client = self._clients[self._idx % len(self._clients)]
        self._idx += 1
        return client

    async def post(self, path: str, **kwargs) -> httpx.Response:
        if not self._warmed:
            raise RuntimeError("Pool not warmed up — call warm_up() first")
        return await self._next_client().post(path, **kwargs)

    async def close(self):
        await asyncio.gather(*[c.aclose() for c in self._clients])

# Usage with FastAPI lifespan
from contextlib import asynccontextmanager
from fastapi import FastAPI

pool = WarmH2Pool("https://api.anthropic.com", pool_size=3)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.warm_up()
    yield
    await pool.close()

app = FastAPI(lifespan=lifespan)
```

**When to use**: Serverless agents (Lambda, Cloud Run) or services that must respond instantly to the first request.

---

## Solution 6: HTTP/2 vs HTTP/1.1 Benchmarking Harness

Measure the actual latency and throughput difference in your environment before committing to the migration.

```python
import asyncio
import time
import statistics
import httpx

async def benchmark_protocol(
    url: str,
    n_concurrent: int = 20,
    n_rounds: int = 5,
    use_http2: bool = True,
) -> dict:
    protocol = "HTTP/2" if use_http2 else "HTTP/1.1"
    all_latencies: list[float] = []

    async with httpx.AsyncClient(
        http2=use_http2,
        limits=httpx.Limits(
            max_connections=2 if use_http2 else n_concurrent,  # H/2 needs fewer conns
            max_keepalive_connections=2 if use_http2 else n_concurrent,
        ),
        timeout=30.0,
    ) as client:
        for round_num in range(n_rounds):
            t0 = time.monotonic()

            async def timed_get(idx: int) -> float:
                rt0 = time.monotonic()
                try:
                    await client.get(url)
                except Exception:
                    pass
                return time.monotonic() - rt0

            latencies = await asyncio.gather(*[timed_get(i) for i in range(n_concurrent)])
            all_latencies.extend(latencies)
            round_elapsed = time.monotonic() - t0
            print(f"  {protocol} round {round_num+1}: {round_elapsed:.3f}s for {n_concurrent} reqs")

    lat_ms = [l * 1000 for l in all_latencies]
    return {
        "protocol": protocol,
        "n_requests": len(all_latencies),
        "p50_ms": round(statistics.median(lat_ms), 1),
        "p95_ms": round(sorted(lat_ms)[int(len(lat_ms)*0.95)], 1),
        "p99_ms": round(sorted(lat_ms)[int(len(lat_ms)*0.99)], 1),
        "mean_ms": round(statistics.mean(lat_ms), 1),
    }

async def compare_protocols(url: str = "https://httpbin.org/get"):
    print(f"\nBenchmarking {url} with 20 concurrent requests, 5 rounds...\n")
    h2_result  = await benchmark_protocol(url, use_http2=True)
    h1_result  = await benchmark_protocol(url, use_http2=False)

    p95_improvement = (h1_result["p95_ms"] - h2_result["p95_ms"]) / h1_result["p95_ms"] * 100
    print(f"\nHTTP/2 p95: {h2_result['p95_ms']}ms  |  HTTP/1.1 p95: {h1_result['p95_ms']}ms")
    print(f"HTTP/2 improvement: {p95_improvement:.1f}% on p95")
    return h2_result, h1_result

# asyncio.run(compare_protocols())
```

**When to use**: Before migrating to HTTP/2. Verify real-world gains in your network environment (internal vs cross-region matters).

---

## Comparison

| Solution | Setup Effort | Multiplexing | Streaming | TLS Reuse | Best For |
|---|---|---|---|---|---|
| httpx with http2=True | Minimal | Yes | Yes (SSE) | Yes | Drop-in replacement for aiohttp/requests |
| Anthropic SDK H/2 transport | Low | Yes | Yes | Yes | Anthropic API calls |
| H/2 prefetching schema loader | Medium | Yes | No | Yes | Remote config/schema loading |
| gRPC (HTTP/2 native) | High | Yes | Yes (bidirectional) | Yes | Self-hosted inference servers |
| Warm H/2 pool | Medium | Yes | Yes | Yes | Cold-start-sensitive agents |
| Benchmarking harness | N/A | N/A | N/A | N/A | Validating migration ROI |

**Rule of thumb**: Switch to `httpx[http2]` first — it's a one-line change. Keep a single long-lived `AsyncClient` instance per process. Measure p95 latency before and after; most agents see 20–40% improvement on concurrent workloads.
