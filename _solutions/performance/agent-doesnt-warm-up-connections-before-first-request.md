---
layout: solution
title: "Agent Doesn't Warm Up Connections Before First Request"
category: performance
description: "The first user request is slow because the agent must establish TCP connections, complete TLS handshakes, authenticate, and load config — all on the critical path of the very first call."
tags: [performance, startup, latency, connection-pooling, production]
---

## Symptom

The first request to the agent takes 3–8 seconds while subsequent requests complete in under 500 ms. Users on free tiers or after cold starts get a degraded first impression. In Kubernetes, liveness probes fail because the pod takes too long to serve its first request. In serverless environments (Lambda, Cloud Run), cold starts compound with connection establishment to produce multi-second p99 latencies.

## Root Cause

Without a warm-up phase, every resource is initialised lazily on the first real request: the HTTP client opens a new TCP socket, completes a TLS handshake with the Anthropic API, DNS resolution blocks, the config file is read from disk, and in-process caches are empty. Each step adds 50–500 ms. Sequentially on the critical path, they sum to seconds.

## Fix

### Option 1 — Eager client initialisation at module load time

```python
import anthropic
import time

# BAD: client created on first request — TLS handshake on critical path
# def get_client():
#     return anthropic.Anthropic()  # lazy

# GOOD: create at module import time — connection pool is ready before first request
_t0 = time.monotonic()
client = anthropic.Anthropic(
    # Pre-configure connection limits so the pool is allocated at startup
    timeout=30.0,
    max_retries=2,
)
print(f"[warmup] Anthropic client initialised in {(time.monotonic()-_t0)*1000:.0f}ms")

def handle_request(user_message: str) -> str:
    """First call is fast because the client (and its connection pool) already exists."""
    t0 = time.monotonic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": user_message}],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    print(f"[request] {latency_ms}ms")
    return response.content[0].text

# Simulate two requests
print(handle_request("First request — connection already warmed"))
print(handle_request("Second request — connection reused"))
```

**Expected Token Savings:** No wasted API tokens from timeouts caused by slow first-request latency; improves time-to-first-token for users, reducing perceived need to retry.
**Environment:** Any long-running agent service; works for Flask, FastAPI, asyncio servers, and CLI tools.

---

### Option 2 — Async startup hook: warm up before accepting traffic

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic(timeout=30.0)

_warmed_up = False

async def warm_up() -> None:
    """
    Send a minimal API call at startup to:
    1. Establish TCP + TLS connection (reused by subsequent calls)
    2. Verify the API key is valid
    3. Prime the connection pool
    """
    global _warmed_up
    t0 = time.monotonic()
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,   # minimal cost — we only care about the connection
            messages=[{"role": "user", "content": "."}],
        )
        ms = int((time.monotonic() - t0) * 1000)
        print(f"[warmup] connection ready in {ms}ms "
              f"(used {response.usage.input_tokens} input tokens)")
        _warmed_up = True
    except anthropic.AuthenticationError:
        print("[warmup] FATAL: invalid API key — check ANTHROPIC_API_KEY")
        raise
    except Exception as e:
        print(f"[warmup] WARNING: warm-up failed ({e}) — proceeding anyway")

async def handle_user_request(message: str) -> str:
    if not _warmed_up:
        print("[request] WARNING: serving request before warm-up completed")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text

async def main():
    # In a real server: call warm_up() in the lifespan/startup event handler
    # FastAPI: @app.on_event("startup") async def startup(): await warm_up()
    await warm_up()
    result = await handle_user_request("What is the speed of light?")
    print(f"[agent] {result[:80]}")

asyncio.run(main())
```

**Expected Token Savings:** Warm-up costs 1 input token per restart; saves the latency penalty on the first real user request which would otherwise spend tokens while the user waits and possibly retries.
**Environment:** FastAPI or aiohttp servers; Kubernetes pods with readiness probes that wait for warm-up to complete.

---

### Option 3 — Database connection pool warm-up alongside API client

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

# pip install asyncpg
try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False

_pool = None

async def warm_up_all() -> dict:
    """Warm up all connections in parallel — total latency = max(individual), not sum."""
    global _pool
    results = {}
    t0 = time.monotonic()

    async def warm_anthropic():
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "."}],
        )
        return resp.usage.input_tokens

    async def warm_database():
        global _pool
        if not HAS_ASYNCPG:
            return "asyncpg not available"
        _pool = await asyncpg.create_pool(
            "postgresql://localhost/demo",
            min_size=2,
            max_size=10,
        )
        val = await _pool.fetchval("SELECT 1")
        return f"db pool ready (ping={val})"

    # Run all warm-ups concurrently
    anthropic_result, db_result = await asyncio.gather(
        warm_anthropic(),
        warm_database(),
        return_exceptions=True,
    )
    total_ms = int((time.monotonic() - t0) * 1000)

    results = {
        "anthropic_tokens": anthropic_result,
        "database":         db_result,
        "total_warmup_ms":  total_ms,
    }
    print(f"[warmup] all connections ready in {total_ms}ms: {results}")
    return results

async def process_request(user_id: int, question: str) -> str:
    # DB and API connections are already established — no cold-start penalty
    if _pool and HAS_ASYNCPG:
        async with _pool.acquire() as conn:
            user = await conn.fetchrow("SELECT name FROM users WHERE id = $1", user_id)
            name = user["name"] if user else f"user-{user_id}"
    else:
        name = f"user-{user_id}"

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Hello {name}. {question}"}],
    )
    return response.content[0].text

async def main():
    await warm_up_all()
    result = await process_request(1, "What's the capital of France?")
    print(f"[agent] {result[:60]}")
    if _pool:
        await _pool.close()

asyncio.run(main())
```

**Expected Token Savings:** Parallel warm-up takes max(API latency, DB latency) instead of sum — typically 200–400 ms instead of 600–1200 ms; faster readiness means Kubernetes starts routing traffic sooner.
**Environment:** Agents that query both Anthropic and a database on every request; FastAPI lifespan handlers.

---

### Option 4 — Kubernetes readiness probe gated on warm-up

```python
import asyncio
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import anthropic

client = anthropic.AsyncAnthropic()

# Shared state: ready flag set after warm-up completes
_ready = False
_healthy = True

class ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            if _healthy:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"unhealthy")
        elif self.path == "/readyz":
            if _ready:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ready")
            else:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"warming up")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # suppress access logs

def start_probe_server(port: int = 8080):
    server = HTTPServer(("", port), ProbeHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[probe] health/readiness server on :{port}")
    return server

async def warm_up():
    global _ready
    t0 = time.monotonic()
    try:
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "."}],
        )
        ms = int((time.monotonic() - t0) * 1000)
        print(f"[warmup] done in {ms}ms — marking /readyz ready")
        _ready = True
    except Exception as e:
        print(f"[warmup] failed: {e} — /readyz will stay 503")

async def serve_requests():
    """Simulate request handling after warm-up."""
    await asyncio.sleep(0.1)  # let probe server start
    if not _ready:
        print("[agent] waiting for warm-up...")
        while not _ready:
            await asyncio.sleep(0.1)
    print("[agent] warm-up complete — serving requests")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": "First user request after warm-up."}],
    )
    print(f"[agent] {response.content[0].text[:60]}")

async def main():
    start_probe_server(int(os.environ.get("PROBE_PORT", "8080")))
    await asyncio.gather(warm_up(), serve_requests())

asyncio.run(main())

# kubernetes/deployment.yaml readiness probe:
# readinessProbe:
#   httpGet:
#     path: /readyz
#     port: 8080
#   initialDelaySeconds: 5
#   periodSeconds: 2
#   failureThreshold: 10
```

**Expected Token Savings:** Kubernetes only routes traffic after `/readyz` returns 200; users never hit a cold-start response; eliminates timeout-triggered retries on the first request.
**Environment:** Kubernetes deployments with rolling updates; any agent that needs a readiness signal separate from liveness.

---

### Option 5 — Background prefetch: warm cache while serving first requests

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

# In-process cache for frequently requested prompts
_response_cache: dict[str, str] = {}

PREFETCH_PROMPTS = [
    "What is today's date?",
    "Who are you?",
    "What can you help me with?",
]

async def prefetch_common_responses() -> None:
    """
    Pre-populate cache with answers to frequent questions.
    Runs in background — doesn't block startup.
    """
    t0 = time.monotonic()
    tasks = []
    for prompt in PREFETCH_PROMPTS:
        async def fetch(p=prompt):
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": p}],
            )
            _response_cache[p] = resp.content[0].text
        tasks.append(asyncio.create_task(fetch()))

    await asyncio.gather(*tasks, return_exceptions=True)
    ms = int((time.monotonic() - t0) * 1000)
    print(f"[prefetch] {len(_response_cache)} responses cached in {ms}ms")

async def handle_request(message: str) -> str:
    if message in _response_cache:
        print(f"[cache] hit for '{message[:40]}'")
        return _response_cache[message]
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text

async def main():
    # Start prefetch in background — don't await, keep serving requests
    prefetch_task = asyncio.create_task(prefetch_common_responses())

    # Serve first user request immediately (may be a cache miss)
    result = await handle_request("What is today's date?")
    print(f"[agent] {result[:60]}")

    # By the time the second request comes in, cache is likely warm
    await asyncio.sleep(0.5)
    result2 = await handle_request("What is today's date?")
    print(f"[agent] {result2[:60]}")

    await prefetch_task  # wait for cleanup

asyncio.run(main())
```

**Expected Token Savings:** Cache hits cost zero tokens; prefetching amortises the cost of common questions across many users; connection established during prefetch serves all subsequent requests without cold-start latency.
**Environment:** Customer-facing agents where a small set of questions accounts for a large share of traffic (FAQ bots, onboarding agents).

---

### Option 6 — Lambda / Cloud Run: reuse warm instances with connection caching

```python
import os
import time
import anthropic

# Module-level initialisation runs once per warm Lambda/Cloud Run instance
# (NOT once per request — Python module state persists across invocations)
_INIT_START = time.monotonic()

client = anthropic.Anthropic(
    timeout=25.0,    # slightly under Lambda's 30s default
    max_retries=1,   # aggressive — cold starts budget is tight
)

# Pre-validate config at module load time
_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not _API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY not set — Lambda will fail all invocations")

_INIT_MS = int((time.monotonic() - _INIT_START) * 1000)
print(f"[init] module loaded in {_INIT_MS}ms (cold start)")

def lambda_handler(event: dict, context) -> dict:
    """
    Lambda entry point.
    First invocation: module already initialised above.
    Subsequent invocations on warm instances: client is reused, no new connections.
    """
    message = event.get("message", "Hello.")
    invocation_type = "warm" if _INIT_MS > 0 else "cold"

    t0 = time.monotonic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": message}],
    )
    request_ms = int((time.monotonic() - t0) * 1000)

    return {
        "statusCode": 200,
        "body": response.content[0].text,
        "metadata": {
            "invocation_type": invocation_type,
            "init_ms":         _INIT_MS,
            "request_ms":      request_ms,
            "input_tokens":    response.usage.input_tokens,
            "output_tokens":   response.usage.output_tokens,
        },
    }

# Local test
if __name__ == "__main__":
    result = lambda_handler({"message": "What is 2+2?"}, None)
    print(result)
```

**Expected Token Savings:** Warm Lambda instances reuse the HTTP connection pool — subsequent requests skip TCP + TLS establishment (saving 100–300 ms per call); connection reuse also avoids the rare token waste from API calls that time out during TCP setup.
**Environment:** AWS Lambda, Google Cloud Run, Azure Functions; any FaaS platform where module-level state persists across warm invocations.

---

## Comparison

| Option | Warm-up Timing | Blocks Startup | Readiness Signal | Cold-start Reduction | Best For |
|---|---|---|---|---|---|
| 1. Eager init at import | Module load | Yes (brief) | No | High (pool pre-created) | Long-running services |
| 2. Async startup hook | Lifespan event | No (async) | Via flag | High | FastAPI / aiohttp servers |
| 3. Parallel warm-up | Lifespan event | No (parallel) | Via flag | Very high | Multi-resource agents (DB + API) |
| 4. Readiness probe | Lifespan event | No | Yes (/readyz) | High | Kubernetes rolling deploys |
| 5. Background prefetch | After startup | No | No | Medium (cache+conn) | High-traffic FAQ agents |
| 6. Lambda module init | Module load | Yes (cold only) | No | Medium (warm reuse) | FaaS / serverless agents |
