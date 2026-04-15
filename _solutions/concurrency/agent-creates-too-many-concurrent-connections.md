---
layout: solution
title: "Agent Creates Too Many Concurrent Connections"
category: concurrency
description: "Agent spawns unbounded parallel tool calls that exhaust connection pools, hit file descriptor limits, or overwhelm downstream APIs with simultaneous requests."
tags: [concurrency, asyncio, connection-pool, rate-limiting, performance]
---

## Symptom

An agent receives a task that requires calling external tools many times — fetching 200 URLs, querying a database for each of 500 records, or running the same API call for every item in a list. Without limits, it launches all calls simultaneously. The result is one or more of:

- `ConnectionError: Max retries exceeded` — the connection pool is exhausted
- `OSError: [Errno 24] Too many open files` — file descriptor limit hit
- `429 Too Many Requests` — the downstream API is overwhelmed
- The host process OOMs from 500 in-flight HTTP responses buffered in memory
- Other services on the same host degrade because their connections are stolen

```python
# BROKEN: agent gets a list of 500 URLs and launches them all at once
async def process_all_items(items: list[str]) -> list:
    tasks = [fetch_item(item) for item in items]
    return await asyncio.gather(*tasks)  # 500 concurrent connections!

# BROKEN: tool implementation has no internal connection limiting
def run_tool_for_each(tool_name: str, inputs: list[dict]) -> list:
    with ThreadPoolExecutor() as executor:  # default: min(32, cpu_count+4) threads — still too many
        return list(executor.map(lambda inp: call_tool(tool_name, inp), inputs))
```

Root causes:
- `asyncio.gather(*tasks)` with no concurrency limit is the most common mistake
- `ThreadPoolExecutor()` default pool size is large enough to overwhelm many APIs
- Agent receives a batch task and calls tools in a tight loop without yielding
- No respect for target service rate limits or connection pool size
- Memory cost of simultaneous responses not considered

---

## Root Cause

`asyncio.gather` and `ThreadPoolExecutor` are both designed for correctness, not rate control. They will happily schedule thousands of operations simultaneously. The issues compound:

1. **Connection pool exhaustion** — `requests` and `httpx` default pools have 10 connections per host. 500 concurrent calls queue on those 10 connections, creating lock contention and timeouts.
2. **File descriptor limits** — each open socket consumes a file descriptor. Default OS limit is 1024 on Linux. 500+ concurrent requests each holding a socket can exhaust this.
3. **Memory pressure** — 500 in-flight HTTP responses buffered = 500× average_response_size in heap.
4. **Cascading failures** — if the target API returns 429s, an unbounded agent will retry all 500 simultaneously, making things worse.

The solution is always a concurrency limit: `asyncio.Semaphore`, connection pool sizing, or chunked processing.

---

## Fix

### Option 1 — asyncio.Semaphore for Bounded Concurrency

The simplest and most idiomatic asyncio fix: wrap every concurrent operation with a shared semaphore.

```python
import anthropic
import asyncio
import httpx
import json
from typing import Any

client = anthropic.AsyncAnthropic()

MAX_CONCURRENT = 10  # never more than this many simultaneous connections

async def fetch_with_limit(
    semaphore: asyncio.Semaphore,
    http_client: httpx.AsyncClient,
    url: str,
    item_id: str,
) -> dict:
    """Fetch a URL, but only when a semaphore slot is available."""
    async with semaphore:  # blocks here if MAX_CONCURRENT slots are all in use
        try:
            response = await http_client.get(url, timeout=10.0)
            response.raise_for_status()
            return {"item_id": item_id, "status": "ok", "data": response.json()}
        except httpx.HTTPStatusError as e:
            return {"item_id": item_id, "status": "error", "code": e.response.status_code}
        except Exception as e:
            return {"item_id": item_id, "status": "error", "message": str(e)}

async def batch_fetch(items: list[dict]) -> list[dict]:
    """Fetch all items with bounded concurrency."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    # Single shared connection pool — httpx manages it efficiently
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=MAX_CONCURRENT, max_keepalive_connections=MAX_CONCURRENT)
    ) as http_client:
        tasks = [
            fetch_with_limit(semaphore, http_client, item["url"], item["id"])
            for item in items
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    return [
        r if not isinstance(r, Exception) else {"status": "exception", "error": str(r)}
        for r in results
    ]

# Tool implementation used by the agent
async def tool_batch_fetch_urls(item_list_json: str) -> str:
    """
    Agent-facing tool: fetch multiple URLs with bounded concurrency.
    Input: JSON array of {"id": str, "url": str} objects.
    """
    try:
        items = json.loads(item_list_json)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON input"})

    if len(items) > 1000:
        return json.dumps({"error": f"Too many items ({len(items)}). Max 1000 per call."})

    print(f"  Fetching {len(items)} items with max {MAX_CONCURRENT} concurrent connections")
    results = await batch_fetch(items)

    success = sum(1 for r in results if r.get("status") == "ok")
    return json.dumps({
        "total": len(items),
        "succeeded": success,
        "failed": len(items) - success,
        "results": results[:10],  # return first 10 for brevity; agent can ask for more
        "has_more": len(items) > 10,
    })

tools = [{
    "name": "batch_fetch_urls",
    "description": (
        "Fetch multiple URLs efficiently. Handles batches up to 1000 items. "
        "Always use this instead of calling fetch_url repeatedly in a loop."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "item_list_json": {
                "type": "string",
                "description": 'JSON array: [{"id": "...", "url": "..."}, ...]'
            }
        },
        "required": ["item_list_json"]
    }
}]

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages,
            system="When fetching multiple URLs, always use batch_fetch_urls — never call a single-URL tool in a loop.",
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "batch_fetch_urls":
                result = await tool_batch_fetch_urls(block.input["item_list_json"])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": results})

# Test: 500 items, only 10 run concurrently at any moment
items = [{"id": str(i), "url": f"https://httpbin.org/get?n={i}"} for i in range(500)]
print(asyncio.run(run_agent(f"Fetch these items: {json.dumps(items[:5])}")))
```

**Expected Token Savings:** Not a token optimization — prevents connection exhaustion crashes that would terminate the session entirely.

**Environment:** Python 3.9+, `asyncio`, `httpx>=0.24.0`, `anthropic>=0.40.0`.

---

### Option 2 — Chunked Processing with Progress Reporting

Split large batches into chunks and process sequentially, with progress updates back to the agent.

```python
import anthropic
import asyncio
import httpx
import json
from itertools import islice

client = anthropic.AsyncAnthropic()

def chunk_list(lst: list, size: int):
    """Split list into chunks of at most `size` elements."""
    it = iter(lst)
    while batch := list(islice(it, size)):
        yield batch

async def process_chunk(
    chunk: list[dict],
    http_client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    """Process one chunk of items with bounded internal concurrency."""
    async def fetch_one(item: dict) -> dict:
        async with semaphore:
            try:
                resp = await http_client.get(item["url"], timeout=8.0)
                return {"id": item["id"], "ok": resp.status_code == 200, "status": resp.status_code}
            except Exception as e:
                return {"id": item["id"], "ok": False, "error": str(e)[:100]}

    return await asyncio.gather(*[fetch_one(item) for item in chunk])

async def tool_chunked_batch_process(items_json: str, chunk_size: int = 50) -> str:
    """
    Process a large list in sequential chunks to avoid overwhelming connections.
    Reports progress after each chunk.
    """
    items = json.loads(items_json)
    chunks = list(chunk_list(items, chunk_size))
    total = len(items)
    all_results = []
    errors = 0

    semaphore = asyncio.Semaphore(chunk_size)  # within each chunk, all run concurrently
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=chunk_size, max_keepalive_connections=chunk_size // 2)
    ) as http_client:
        for i, chunk in enumerate(chunks):
            print(f"  Processing chunk {i+1}/{len(chunks)} ({len(chunk)} items)...")
            chunk_results = await process_chunk(chunk, http_client, semaphore)
            all_results.extend(chunk_results)
            errors += sum(1 for r in chunk_results if not r.get("ok"))

            # Add small delay between chunks to avoid bursting the target API
            if i < len(chunks) - 1:
                await asyncio.sleep(0.1)

    return json.dumps({
        "total_processed": total,
        "succeeded": total - errors,
        "failed": errors,
        "chunks_processed": len(chunks),
        "sample_results": all_results[:5],
    })

# Demonstrate: 200 items processed in 50-item chunks = 4 sequential bursts
async def demo():
    items = [{"id": str(i), "url": f"https://httpbin.org/status/200"} for i in range(200)]
    result = await tool_chunked_batch_process(json.dumps(items), chunk_size=50)
    print(json.loads(result))

asyncio.run(demo())
```

**Expected Token Savings:** No direct savings, but chunked processing allows agents to return partial results and retry only failed chunks, avoiding full restarts on failure.

**Environment:** Python 3.9+, `httpx`, `asyncio`, `anthropic>=0.40.0`.

---

### Option 3 — Token Bucket Rate Limiter for Tool Calls

Implement a token bucket that limits the rate of tool calls per second, regardless of concurrency.

```python
import anthropic
import asyncio
import time
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class AsyncTokenBucket:
    """
    Token bucket for rate limiting async operations.
    rate: tokens added per second
    capacity: maximum tokens (burst size)
    """
    rate: float        # tokens per second
    capacity: float    # max burst
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self):
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    async def acquire(self, tokens: float = 1.0):
        """Wait until `tokens` tokens are available, then consume them."""
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last_refill = now

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return

                # How long until enough tokens are available?
                wait_time = (tokens - self._tokens) / self.rate

            await asyncio.sleep(wait_time)

# Global rate limiter: max 20 API calls per second, burst of 30
TOOL_RATE_LIMITER = AsyncTokenBucket(rate=20.0, capacity=30.0)

async def rate_limited_tool_call(tool_name: str, tool_input: dict) -> dict:
    """Execute a tool call, blocking until the rate limit allows it."""
    await TOOL_RATE_LIMITER.acquire(tokens=1.0)

    # Simulate actual tool work
    await asyncio.sleep(0.05)
    return {"tool": tool_name, "input": tool_input, "result": "ok", "timestamp": time.time()}

async def process_many_with_rate_limit(requests: list[dict]) -> list[dict]:
    """Process all requests with rate limiting — no connection storm."""

    async def process_one(req: dict) -> dict:
        return await rate_limited_tool_call(req["tool"], req["params"])

    # No semaphore needed — rate limiter controls throughput
    # Use gather with return_exceptions so one failure doesn't cancel all
    results = await asyncio.gather(
        *[process_one(req) for req in requests],
        return_exceptions=True
    )

    return [
        r if not isinstance(r, Exception)
        else {"error": str(r), "type": type(r).__name__}
        for r in results
    ]

# Tool that the agent uses
async def tool_bulk_database_query(queries_json: str) -> str:
    queries = json.loads(queries_json)

    if len(queries) > 500:
        return json.dumps({"error": "Maximum 500 queries per call"})

    print(f"  Processing {len(queries)} queries at max 20/sec...")
    start = time.monotonic()
    results = await process_many_with_rate_limit(queries)
    elapsed = time.monotonic() - start

    succeeded = sum(1 for r in results if not r.get("error"))
    return json.dumps({
        "total": len(queries),
        "succeeded": succeeded,
        "failed": len(queries) - succeeded,
        "elapsed_seconds": round(elapsed, 2),
        "actual_rate": round(len(queries) / elapsed, 1),
        "results": results[:5],
    })

# Demo: 100 queries, rate-limited to 20/sec → takes ~5 seconds
async def demo():
    queries = [
        {"tool": "db_lookup", "params": {"id": str(i)}}
        for i in range(100)
    ]
    result = await tool_bulk_database_query(json.dumps(queries))
    data = json.loads(result)
    print(f"Processed {data['total']} queries at {data['actual_rate']} req/sec in {data['elapsed_seconds']}s")

asyncio.run(demo())
```

**Expected Token Savings:** Prevents 429 retry cascades that can multiply token usage by 3-5× on busy APIs.

**Environment:** Python 3.9+, `asyncio`, `anthropic>=0.40.0`. No external dependencies for the rate limiter.

---

### Option 4 — Connection Pool with Explicit Sizing

Explicitly size connection pools to match the target API's limits, preventing pool exhaustion.

```python
import anthropic
import asyncio
import httpx
import json
from contextlib import asynccontextmanager

client = anthropic.AsyncAnthropic()

# Connection pool configurations per API target
API_POOL_CONFIGS = {
    "stripe": {
        "max_connections": 5,       # Stripe allows 100/s but we're conservative
        "max_keepalive": 5,
        "timeout": httpx.Timeout(10.0, connect=5.0),
        "base_url": "https://api.stripe.com",
    },
    "github": {
        "max_connections": 3,       # GitHub: 60 unauth / 5000 auth per hour
        "max_keepalive": 3,
        "timeout": httpx.Timeout(15.0, connect=5.0),
        "base_url": "https://api.github.com",
    },
    "internal": {
        "max_connections": 20,      # Internal services can handle more
        "max_keepalive": 10,
        "timeout": httpx.Timeout(5.0, connect=2.0),
        "base_url": "https://internal.example.com",
    },
}

class ManagedConnectionPool:
    """Manages multiple named HTTP clients with explicit pool sizing."""

    def __init__(self):
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def get(self, api_name: str) -> httpx.AsyncClient:
        if api_name not in self._clients:
            config = API_POOL_CONFIGS.get(api_name, API_POOL_CONFIGS["internal"])
            self._clients[api_name] = httpx.AsyncClient(
                base_url=config["base_url"],
                limits=httpx.Limits(
                    max_connections=config["max_connections"],
                    max_keepalive_connections=config["max_keepalive"],
                ),
                timeout=config["timeout"],
            )
        return self._clients[api_name]

    async def close_all(self):
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

# Global pool — created once, reused across all tool calls
CONNECTION_POOL = ManagedConnectionPool()

async def safe_api_get(api_name: str, path: str, params: dict = None) -> dict:
    """
    Make an API call using the managed connection pool.
    Pool limits ensure we never exceed per-API connection budgets.
    """
    client_http = await CONNECTION_POOL.get(api_name)
    try:
        resp = await client_http.get(path, params=params)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            await asyncio.sleep(retry_after)
            resp = await client_http.get(path, params=params)  # one retry

        resp.raise_for_status()
        return {"ok": True, "data": resp.json()}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "status": e.response.status_code, "error": str(e)[:200]}
    except httpx.TimeoutException:
        return {"ok": False, "status": 408, "error": "Request timed out"}

async def tool_multi_api_batch(operations_json: str) -> str:
    """
    Execute multiple API operations across different services simultaneously,
    respecting each service's individual connection pool limits.
    """
    operations = json.loads(operations_json)

    async def execute_op(op: dict) -> dict:
        return await safe_api_get(op["api"], op["path"], op.get("params"))

    results = await asyncio.gather(
        *[execute_op(op) for op in operations],
        return_exceptions=True
    )

    return json.dumps({
        "total": len(operations),
        "results": [
            r if not isinstance(r, Exception) else {"ok": False, "error": str(r)}
            for r in results
        ]
    })

# Example: agent calls 3 GitHub + 2 Stripe operations simultaneously
# Each pool limits its own concurrency independently
async def demo():
    operations = [
        {"api": "github", "path": "/repos/anthropics/anthropic-sdk-python"},
        {"api": "github", "path": "/repos/anthropics/anthropic-sdk-python/releases"},
        {"api": "github", "path": "/repos/anthropics/anthropic-sdk-python/issues"},
    ]
    result = await tool_multi_api_batch(json.dumps(operations))
    data = json.loads(result)
    print(f"Completed {data['total']} multi-API operations")
    await CONNECTION_POOL.close_all()

asyncio.run(demo())
```

**Expected Token Savings:** Prevents connection-related failures that would require the agent to restart entire batch operations.

**Environment:** Python 3.9+, `httpx>=0.24.0`, `anthropic>=0.40.0`.

---

### Option 5 — Async Worker Queue with Backpressure

Use an asyncio queue as a work distribution mechanism with backpressure to prevent producer/consumer mismatch.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

client = anthropic.AsyncAnthropic()

@dataclass
class WorkItem:
    item_id: str
    payload: dict
    result: Optional[Any] = None
    error: Optional[str] = None
    completed: bool = False

async def worker(
    worker_id: int,
    queue: asyncio.Queue,
    results: dict,
    process_fn,
):
    """A single worker that pulls items from the queue and processes them."""
    while True:
        try:
            item: WorkItem = await asyncio.wait_for(queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            break  # No more work within timeout — shut down

        try:
            item.result = await process_fn(item.payload)
            item.completed = True
        except Exception as e:
            item.error = str(e)
            item.completed = True
        finally:
            results[item.item_id] = item
            queue.task_done()

async def process_with_worker_pool(
    items: list[dict],
    process_fn,
    num_workers: int = 10,
    queue_maxsize: int = 50,  # backpressure: producer blocks if queue is full
) -> dict[str, WorkItem]:
    """
    Distribute work across a fixed worker pool with backpressure.
    queue_maxsize prevents the producer from far outpacing workers.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
    results: dict[str, WorkItem] = {}

    # Start workers
    workers = [
        asyncio.create_task(worker(i, queue, results, process_fn))
        for i in range(num_workers)
    ]

    # Produce work — blocks when queue is full (backpressure)
    for item_data in items:
        work_item = WorkItem(item_id=item_data["id"], payload=item_data)
        await queue.put(work_item)  # blocks here if queue is at maxsize

    # Wait for all queued work to complete
    await queue.join()

    # Cancel idle workers
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    return results

# Simulated tool: database record enrichment
async def enrich_record(record: dict) -> dict:
    await asyncio.sleep(0.02)  # simulate DB query
    return {
        "id": record["id"],
        "enriched": True,
        "score": len(record["id"]) * 7,
        "processed_at": time.time(),
    }

async def tool_enrich_records(records_json: str, num_workers: int = 10) -> str:
    records = json.loads(records_json)

    if len(records) > 2000:
        return json.dumps({"error": "Max 2000 records per call. Split into smaller batches."})

    num_workers = min(num_workers, 20, len(records))  # cap workers

    print(f"  Enriching {len(records)} records with {num_workers} workers...")
    start = time.monotonic()

    results = await process_with_worker_pool(
        items=records,
        process_fn=enrich_record,
        num_workers=num_workers,
        queue_maxsize=num_workers * 3,  # 3× worker count for smooth flow
    )

    elapsed = time.monotonic() - start
    succeeded = sum(1 for r in results.values() if r.result is not None)

    return json.dumps({
        "total": len(records),
        "succeeded": succeeded,
        "failed": len(records) - succeeded,
        "workers_used": num_workers,
        "elapsed_seconds": round(elapsed, 2),
        "throughput_per_sec": round(len(records) / elapsed, 1),
        "sample": [r.result for r in list(results.values())[:3] if r.result],
    })

# Test: 1000 records, 10 workers, queue backpressure prevents memory spike
async def demo():
    records = [{"id": f"rec-{i:04d}", "name": f"Record {i}"} for i in range(1000)]
    result = await tool_enrich_records(json.dumps(records), num_workers=10)
    data = json.loads(result)
    print(
        f"Processed {data['total']} records in {data['elapsed_seconds']}s "
        f"({data['throughput_per_sec']} rec/s) using {data['workers_used']} workers"
    )

asyncio.run(demo())
```

**Expected Token Savings:** Worker pool with backpressure prevents OOM crashes that would lose all in-flight work, saving the cost of restarting from scratch.

**Environment:** Python 3.9+, `asyncio`, `anthropic>=0.40.0`.

---

### Option 6 — Adaptive Concurrency with Feedback Control

Dynamically adjust concurrency based on observed error rates and latency — starts conservative, scales up or down based on real feedback.

```python
import anthropic
import asyncio
import httpx
import json
import time
from collections import deque
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class AdaptiveConcurrencyController:
    """
    Adjusts concurrency level based on observed error rate and latency.
    Increases concurrency when things are going well; reduces on errors.
    """
    min_concurrency: int = 2
    max_concurrency: int = 50
    initial_concurrency: int = 5
    window_size: int = 20          # look at last N results
    target_error_rate: float = 0.05  # target <5% error rate
    scale_up_threshold: float = 0.01  # scale up if error rate < 1%
    scale_down_threshold: float = 0.10  # scale down if error rate > 10%

    _current: int = field(init=False)
    _results: deque = field(init=False)
    _semaphore: asyncio.Semaphore = field(init=False)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self):
        self._current = self.initial_concurrency
        self._results = deque(maxlen=self.window_size)
        self._semaphore = asyncio.Semaphore(self.initial_concurrency)

    async def record_result(self, success: bool, latency_ms: float):
        """Record a result and adjust concurrency if needed."""
        self._results.append({"success": success, "latency_ms": latency_ms})

        if len(self._results) < self.window_size // 2:
            return  # not enough data yet

        async with self._lock:
            error_rate = sum(1 for r in self._results if not r["success"]) / len(self._results)
            avg_latency = sum(r["latency_ms"] for r in self._results) / len(self._results)

            old = self._current

            if error_rate < self.scale_up_threshold and avg_latency < 500:
                # Going well — try increasing concurrency
                new = min(self._current + 2, self.max_concurrency)
            elif error_rate > self.scale_down_threshold or avg_latency > 2000:
                # Too many errors or slow — reduce concurrency
                new = max(self._current - 3, self.min_concurrency)
            else:
                return  # in the sweet spot

            if new != old:
                self._current = new
                # Replace semaphore with new limit
                self._semaphore = asyncio.Semaphore(new)
                direction = "↑" if new > old else "↓"
                print(f"  Concurrency {direction}: {old} → {new} (error_rate={error_rate:.1%}, latency={avg_latency:.0f}ms)")

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._semaphore

    @property
    def current_concurrency(self) -> int:
        return self._current

controller = AdaptiveConcurrencyController(
    min_concurrency=2,
    max_concurrency=30,
    initial_concurrency=5,
)

async def adaptive_fetch(http_client: httpx.AsyncClient, url: str) -> dict:
    """Fetch with adaptive concurrency control."""
    async with controller.semaphore:
        start = time.monotonic()
        try:
            resp = await http_client.get(url, timeout=8.0)
            latency_ms = (time.monotonic() - start) * 1000
            success = resp.status_code < 400
            await controller.record_result(success, latency_ms)
            return {"url": url, "status": resp.status_code, "latency_ms": round(latency_ms)}
        except Exception as e:
            latency_ms = (time.monotonic() - start) * 1000
            await controller.record_result(False, latency_ms)
            return {"url": url, "error": str(e)[:100], "latency_ms": round(latency_ms)}

async def tool_adaptive_batch_fetch(urls_json: str) -> str:
    """
    Fetch URLs with adaptive concurrency.
    Starts at 5 concurrent, adjusts up or down based on observed error rates.
    """
    urls = json.loads(urls_json)

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=controller.max_concurrency)
    ) as http_client:
        tasks = [adaptive_fetch(http_client, url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    succeeded = sum(1 for r in results if isinstance(r, dict) and r.get("status", 0) < 400)

    return json.dumps({
        "total": len(urls),
        "succeeded": succeeded,
        "failed": len(urls) - succeeded,
        "final_concurrency": controller.current_concurrency,
        "results": [r for r in results if not isinstance(r, Exception)][:10],
    })

# Demo: 100 URLs, concurrency adapts based on success/failure patterns
async def demo():
    # Mix of fast and slow URLs to test adaptation
    urls = (
        [f"https://httpbin.org/get?n={i}" for i in range(70)] +
        [f"https://httpbin.org/status/429" for _ in range(15)] +
        [f"https://httpbin.org/delay/3" for _ in range(15)]
    )
    import random
    random.shuffle(urls)

    print(f"Starting adaptive fetch of {len(urls)} URLs (initial concurrency: {controller.initial_concurrency})")
    result = await tool_adaptive_batch_fetch(json.dumps(urls))
    data = json.loads(result)
    print(f"Done: {data['succeeded']}/{data['total']} succeeded, final concurrency: {data['final_concurrency']}")

asyncio.run(demo())
```

**Expected Token Savings:** Adaptive concurrency prevents both under-utilization (saving wall-clock time) and over-utilization (preventing 429 cascades that triple token costs on retries).

**Environment:** Python 3.9+, `httpx>=0.24.0`, `asyncio`, `anthropic>=0.40.0`.

---

## Comparison

| Option | Approach | Burst Protection | Rate Limiting | Memory Safety | Complexity |
|--------|----------|-----------------|---------------|---------------|------------|
| 1 — Semaphore | Hard limit on simultaneous ops | Excellent | No | Good | Low |
| 2 — Chunked Processing | Sequential chunks | Excellent | Partial | Excellent | Low |
| 3 — Token Bucket | Rate limit per second | Good | Excellent | Good | Medium |
| 4 — Connection Pool | Per-API pool sizing | Excellent | No | Good | Medium |
| 5 — Worker Queue | Fixed workers + backpressure | Excellent | No | Excellent | Medium |
| 6 — Adaptive Concurrency | Self-tuning based on errors | Excellent | Partial | Good | High |

**Start with Option 1** (semaphore) — it's 3 lines of code and fixes the core problem. Add **Option 3** (token bucket) when you need per-second rate compliance with external APIs. Use **Option 6** (adaptive) only when load is highly variable and manual tuning is impractical.
