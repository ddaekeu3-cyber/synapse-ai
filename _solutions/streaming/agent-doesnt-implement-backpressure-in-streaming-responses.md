---
layout: solution
title: "Agent Doesn't Implement Backpressure in Streaming Responses"
category: streaming
description: "Streaming agents that push tokens as fast as the model produces them can overwhelm slow clients, exhaust server memory with unbounded buffers, and cause silent data loss when queues overflow — backpressure signals the producer to slow down."
tags: [streaming, backpressure, asyncio, fastapi, flow-control, queue, memory]
---

# Agent Doesn't Implement Backpressure in Streaming Responses

## Problem

A streaming agent generates tokens and pushes them downstream as fast as the model produces them. If the client connection is slow, a mobile device on a poor network, or a downstream service processing tokens synchronously, the gap between production speed and consumption speed grows without bound. Without backpressure, agents respond by buffering all undelivered tokens in memory, eventually causing OOM crashes or silently dropping chunks. Proper backpressure lets the slow consumer signal "slow down" so the producer stops generating until the buffer drains.

## Solutions

### Option 1: asyncio.Queue with Bounded Buffer

Use a bounded asyncio Queue between the producer (model stream) and consumer (HTTP send). When the queue is full, the producer blocks — that's backpressure.

```python
import asyncio
import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
client = anthropic.AsyncAnthropic()

BUFFER_SIZE = 32  # Max buffered chunks before producer blocks

async def stream_with_backpressure(prompt: str, request: Request):
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=BUFFER_SIZE)

    async def producer():
        """Reads from model stream, blocks when queue is full."""
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    if await request.is_disconnected():
                        break
                    # put() blocks if queue is full — natural backpressure
                    await queue.put(f"data: {text}\n\n")
        finally:
            await queue.put(None)  # Sentinel to signal completion

    async def consumer():
        """Reads from queue and yields to HTTP client."""
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
            queue.task_done()

    # Run producer as background task, consumer drives the response
    producer_task = asyncio.create_task(producer())

    async def generate():
        try:
            async for chunk in consumer():
                yield chunk
        finally:
            producer_task.cancel()
            try:
                await producer_task
            except asyncio.CancelledError:
                pass

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.post("/stream")
async def stream_endpoint(request: Request):
    body = await request.json()
    return await stream_with_backpressure(body["prompt"], request)
# Expected Token Savings: Indirect — prevents OOM that forces restarts; saves retry costs
# Environment: FastAPI/Starlette streaming endpoints serving variable-speed clients
```

### Option 2: Semaphore-Gated Chunk Delivery

Use a semaphore to limit how many chunks can be in-flight at once. The producer can only advance after the consumer acquires and releases a slot.

```python
import asyncio
import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
client = anthropic.AsyncAnthropic()

async def semaphore_backpressure_stream(prompt: str, request: Request, max_inflight: int = 8):
    semaphore = asyncio.Semaphore(max_inflight)
    chunks: list[str] = []
    done = asyncio.Event()
    lock = asyncio.Lock()

    async def producer():
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    if await request.is_disconnected():
                        return
                    await semaphore.acquire()  # Block when max_inflight slots taken
                    async with lock:
                        chunks.append(text)
        finally:
            done.set()

    async def consumer():
        producer_task = asyncio.create_task(producer())
        idx = 0
        try:
            while not done.is_set() or idx < len(chunks):
                await asyncio.sleep(0)  # yield control
                async with lock:
                    if idx < len(chunks):
                        chunk = chunks[idx]
                        idx += 1
                else:
                    continue
                yield f"data: {chunk}\n\n"
                semaphore.release()  # Signal producer: one slot freed
        finally:
            producer_task.cancel()
            try:
                await producer_task
            except asyncio.CancelledError:
                pass

    return StreamingResponse(consumer(), media_type="text/event-stream")

@app.post("/stream-semaphore")
async def stream_semaphore(request: Request):
    body = await request.json()
    return await semaphore_backpressure_stream(body["prompt"], request)
# Expected Token Savings: Prevents buffer bloat; no indirect retry cost from crashes
# Environment: Servers with strict memory limits; agents serving many concurrent streams
```

### Option 3: Token Bucket Rate-Limited Stream

Apply a token bucket to the outbound stream — the agent never sends faster than the bucket allows, regardless of how fast the model produces.

```python
import asyncio
import time
import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
client = anthropic.AsyncAnthropic()

class TokenBucket:
    """Leaky bucket rate limiter for outbound chunks."""

    def __init__(self, rate: float, burst: int):
        self.rate = rate        # tokens per second
        self.burst = burst      # max burst size
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens >= n:
                self.tokens -= n
                return

        # Not enough tokens — wait
        wait = (n - self.tokens) / self.rate
        await asyncio.sleep(wait)
        async with self._lock:
            self.tokens = max(0, self.tokens - n)

async def rate_limited_stream(prompt: str, request: Request):
    # Allow 50 chunks/sec burst up to 100, then steady 50/sec
    bucket = TokenBucket(rate=50.0, burst=100)

    async def generate():
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                if await request.is_disconnected():
                    return
                await bucket.acquire()  # Rate limit outbound
                yield f"data: {text}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.post("/stream-rate-limited")
async def stream_rate_limited(request: Request):
    body = await request.json()
    return await rate_limited_stream(body["prompt"], request)
# Expected Token Savings: Prevents client-side buffer overflow that causes reconnects
# Environment: Mobile clients, IoT devices, bandwidth-constrained consumers
```

### Option 4: Write-Aware Backpressure via drain()

For raw TCP/WebSocket connections, use `drain()` after each write. If the write buffer is full, `drain()` yields until the OS flushes it — exact OS-level backpressure.

```python
import asyncio
import anthropic
import json

client = anthropic.AsyncAnthropic()

async def handle_websocket_with_backpressure(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    prompt: str
):
    """WebSocket-style handler using StreamWriter drain() for backpressure."""
    bytes_sent = 0
    chunks_sent = 0

    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                # Check if client disconnected
                if writer.is_closing():
                    print("Client disconnected — stopping stream")
                    break

                # Encode and write chunk
                payload = json.dumps({"type": "text", "text": text}).encode() + b"\n"
                writer.write(payload)
                bytes_sent += len(payload)
                chunks_sent += 1

                # drain() blocks until OS write buffer has capacity
                # This IS the backpressure — slow client = slow producer
                await writer.drain()

        # Send completion signal
        writer.write(json.dumps({"type": "done", "chunks": chunks_sent, "bytes": bytes_sent}).encode() + b"\n")
        await writer.drain()

    except (ConnectionResetError, BrokenPipeError) as e:
        print(f"Client disconnected mid-stream: {e}")
    except asyncio.CancelledError:
        print("Stream cancelled")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

async def backpressure_server(host: str = "127.0.0.1", port: int = 8765):
    async def handle(reader, writer):
        data = await reader.readline()
        try:
            request = json.loads(data.decode())
            prompt = request.get("prompt", "Hello")
        except json.JSONDecodeError:
            prompt = "Hello"

        await handle_websocket_with_backpressure(reader, writer, prompt)

    server = await asyncio.start_server(handle, host, port)
    print(f"Backpressure server on {host}:{port}")
    async with server:
        await server.serve_forever()

# To run: asyncio.run(backpressure_server())
# Expected Token Savings: Zero dropped chunks = no client retries = no duplicate API calls
# Environment: Raw TCP, WebSocket servers; high-throughput streaming pipelines
```

### Option 5: Adaptive Chunk Batching Under Load

Monitor queue depth in real time. When the queue grows (client falling behind), batch multiple tokens into fewer, larger chunks to reduce overhead.

```python
import asyncio
import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import time

app = FastAPI()
client = anthropic.AsyncAnthropic()

async def adaptive_batch_stream(prompt: str, request: Request):
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=200)
    stats = {"produced": 0, "batches": 0, "avg_batch_size": 0.0}

    async def producer():
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    if await request.is_disconnected():
                        break
                    try:
                        queue.put_nowait(text)
                        stats["produced"] += 1
                    except asyncio.QueueFull:
                        # Queue full = consumer is slow; wait for space
                        await queue.put(text)
                        stats["produced"] += 1
        finally:
            await queue.put(None)

    async def consumer():
        producer_task = asyncio.create_task(producer())
        pending: list[str] = []

        try:
            while True:
                # Adaptive batch size based on queue pressure
                queue_depth = queue.qsize()
                if queue_depth > 100:
                    batch_target = 20   # High pressure: large batches
                elif queue_depth > 50:
                    batch_target = 10   # Medium pressure
                elif queue_depth > 10:
                    batch_target = 5    # Low pressure
                else:
                    batch_target = 1    # Draining: send immediately

                chunk = await queue.get()
                if chunk is None:
                    if pending:
                        yield f"data: {''.join(pending)}\n\n"
                    break

                pending.append(chunk)

                # Drain additional chunks up to batch_target without waiting
                while len(pending) < batch_target:
                    try:
                        next_chunk = queue.get_nowait()
                        if next_chunk is None:
                            yield f"data: {''.join(pending)}\n\n"
                            return
                        pending.append(next_chunk)
                    except asyncio.QueueEmpty:
                        break

                # Send the batch
                stats["batches"] += 1
                stats["avg_batch_size"] = stats["produced"] / max(stats["batches"], 1)
                yield f"data: {''.join(pending)}\n\n"
                pending = []

        finally:
            producer_task.cancel()
            try:
                await producer_task
            except asyncio.CancelledError:
                pass

    return StreamingResponse(consumer(), media_type="text/event-stream")

@app.post("/stream-adaptive")
async def stream_adaptive(request: Request):
    body = await request.json()
    return await adaptive_batch_stream(body["prompt"], request)
# Expected Token Savings: Reduces HTTP chunk overhead by 5-20x under load
# Environment: High-concurrency servers; slow mobile clients; SSE over HTTP/1.1
```

### Option 6: Backpressure Metrics and Circuit Breaker

Measure backpressure pressure over time. If the queue stays full too long, trip a circuit breaker to reject new streams before the server runs out of memory.

```python
import asyncio
import anthropic
import time
from collections import deque
from dataclasses import dataclass, field
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI()
client = anthropic.AsyncAnthropic()

@dataclass
class BackpressureMonitor:
    window_seconds: float = 10.0
    pressure_threshold: float = 0.8  # Trip if >80% of samples show full queue
    max_queue_size: int = 64

    _samples: deque = field(default_factory=lambda: deque(maxlen=100))
    _tripped: bool = False
    _trip_time: float = 0.0
    _cooldown_seconds: float = 30.0
    active_streams: int = 0
    max_concurrent: int = 20

    def record_sample(self, queue_depth: int):
        pressure = queue_depth / self.max_queue_size
        self._samples.append((time.monotonic(), pressure))
        self._cleanup_old()

        avg_pressure = sum(p for _, p in self._samples) / max(len(self._samples), 1)
        if avg_pressure > self.pressure_threshold and not self._tripped:
            self._tripped = True
            self._trip_time = time.monotonic()
            print(f"BACKPRESSURE CIRCUIT TRIPPED: avg pressure={avg_pressure:.2f}")

        # Auto-reset after cooldown
        if self._tripped and (time.monotonic() - self._trip_time) > self._cooldown_seconds:
            self._tripped = False
            print("Backpressure circuit reset")

    def _cleanup_old(self):
        cutoff = time.monotonic() - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def should_accept(self) -> tuple[bool, str]:
        if self._tripped:
            return False, "Server under backpressure — try again shortly"
        if self.active_streams >= self.max_concurrent:
            return False, f"Max concurrent streams ({self.max_concurrent}) reached"
        return True, ""

monitor = BackpressureMonitor()

async def monitored_stream(prompt: str, request: Request):
    accept, reason = monitor.should_accept()
    if not accept:
        raise HTTPException(status_code=503, detail=reason)

    monitor.active_streams += 1
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=monitor.max_queue_size)

    async def producer():
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    if await request.is_disconnected():
                        break
                    monitor.record_sample(queue.qsize())
                    await queue.put(text)  # Blocks when full
        finally:
            await queue.put(None)

    async def consumer():
        producer_task = asyncio.create_task(producer())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield f"data: {chunk}\n\n"
        finally:
            monitor.active_streams -= 1
            producer_task.cancel()
            try:
                await producer_task
            except asyncio.CancelledError:
                pass

    return StreamingResponse(consumer(), media_type="text/event-stream")

@app.post("/stream-monitored")
async def stream_monitored(request: Request):
    body = await request.json()
    return await monitored_stream(body["prompt"], request)

@app.get("/backpressure-status")
async def backpressure_status():
    return {
        "tripped": monitor._tripped,
        "active_streams": monitor.active_streams,
        "sample_count": len(monitor._samples),
        "avg_pressure": sum(p for _, p in monitor._samples) / max(len(monitor._samples), 1),
    }
# Expected Token Savings: Prevents cascading OOM; avoids costly server restarts mid-stream
# Environment: Production streaming APIs; multi-tenant streaming servers
```

## Comparison Table

| Option | Mechanism | Memory Safety | Complexity | Best For |
|--------|-----------|--------------|------------|----------|
| 1: Bounded asyncio.Queue | Queue blocks producer at capacity | Strong — hard limit | Low | Most FastAPI/Starlette apps |
| 2: Semaphore-Gated | Semaphore limits in-flight count | Strong | Medium | Fine-grained flow control |
| 3: Token Bucket | Rate limits outbound chunks | Moderate — burst possible | Low-Medium | Bandwidth-limited clients |
| 4: StreamWriter drain() | OS-level write buffer pressure | Strong — OS enforces | Low | Raw TCP / WebSocket servers |
| 5: Adaptive Batching | Dynamic batch size from queue depth | Moderate | Medium | High-concurrency SSE servers |
| 6: Circuit Breaker Monitor | Pressure metrics + circuit trip | Strong + observability | High | Production multi-tenant servers |
