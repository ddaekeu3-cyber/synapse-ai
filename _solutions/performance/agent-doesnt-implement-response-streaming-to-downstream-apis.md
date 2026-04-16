---
title: "Agent Doesn't Implement Response Streaming to Downstream APIs"
description: "How to stream Claude's responses directly to downstream HTTP clients, WebSockets, and message queues without buffering the full response."
categories: [performance]
difficulty: intermediate
---

Buffering a full LLM response before forwarding it adds latency equal to the entire generation time. Streaming the response token-by-token to downstream consumers—HTTP clients, WebSocket subscribers, message queues—cuts time-to-first-token to milliseconds and lets users see results as they're generated.

## Solution 1: SSE Passthrough to HTTP Client (FastAPI)

Forward Claude's streaming response as Server-Sent Events directly to a browser or HTTP client.

```python
import anthropic
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()
client = anthropic.AsyncAnthropic()


async def claude_sse_generator(prompt: str):
    """Yield SSE-formatted chunks from Claude's streaming response."""
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            # SSE format: "data: <payload>\n\n"
            yield f"data: {text}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/stream")
async def stream_endpoint(prompt: str):
    return StreamingResponse(
        claude_sse_generator(prompt),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )
```

## Solution 2: WebSocket Fan-Out to Multiple Subscribers

Stream a single Claude generation to multiple WebSocket clients simultaneously.

```python
import asyncio
import json
import anthropic
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()
client = anthropic.AsyncAnthropic()


class StreamBroadcaster:
    """Fan out a single Claude stream to N WebSocket subscribers."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, stream_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(stream_id, []).append(q)
        return q

    def unsubscribe(self, stream_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(stream_id, [])
        if q in subs:
            subs.remove(q)

    async def broadcast(self, stream_id: str, chunk: str | None) -> None:
        for q in list(self._subscribers.get(stream_id, [])):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                pass  # Slow subscriber — drop chunk


broadcaster = StreamBroadcaster()


async def run_claude_stream(stream_id: str, prompt: str) -> None:
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            await broadcaster.broadcast(stream_id, text)
    await broadcaster.broadcast(stream_id, None)  # Sentinel: stream done


@app.websocket("/ws/{stream_id}")
async def websocket_subscriber(ws: WebSocket, stream_id: str):
    await ws.accept()
    q = broadcaster.subscribe(stream_id)
    try:
        while True:
            chunk = await q.get()
            if chunk is None:
                await ws.send_json({"type": "done"})
                break
            await ws.send_json({"type": "chunk", "text": chunk})
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unsubscribe(stream_id, q)


@app.post("/start/{stream_id}")
async def start_stream(stream_id: str, prompt: str):
    asyncio.create_task(run_claude_stream(stream_id, prompt))
    return {"status": "streaming", "stream_id": stream_id}
```

## Solution 3: Streaming to a Message Queue (Redis Pub/Sub)

Publish each token to a Redis channel so any number of downstream consumers can subscribe independently.

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()


class RedisPubSubSimulator:
    """In-process pub/sub that mirrors Redis PUBLISH/SUBSCRIBE semantics."""

    def __init__(self):
        self._channels: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._channels.setdefault(channel, []).append(q)
        return q

    async def publish(self, channel: str, message: str) -> None:
        for q in self._channels.get(channel, []):
            await q.put(message)


pubsub = RedisPubSubSimulator()


async def stream_to_redis(prompt: str, channel: str) -> None:
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            msg = json.dumps({"type": "chunk", "text": text})
            await pubsub.publish(channel, msg)

    await pubsub.publish(channel, json.dumps({"type": "done"}))


async def downstream_consumer(channel: str, consumer_id: str) -> str:
    q = pubsub.subscribe(channel)
    parts = []
    while True:
        raw = await q.get()
        msg = json.loads(raw)
        if msg["type"] == "done":
            break
        parts.append(msg["text"])
        print(f"[Consumer {consumer_id}] received: {msg['text']!r}")
    return "".join(parts)


async def main():
    channel = "generation:123"
    # Start two consumers before the stream begins
    c1 = asyncio.create_task(downstream_consumer(channel, "A"))
    c2 = asyncio.create_task(downstream_consumer(channel, "B"))
    await stream_to_redis("Explain asyncio in Python.", channel)
    result_a, result_b = await asyncio.gather(c1, c2)
    assert result_a == result_b


asyncio.run(main())
```

## Solution 4: Bidirectional Streaming with Tool-Call Passthrough

Stream assistant text chunks while intercepting tool-call events and forwarding their results back mid-stream.

```python
import asyncio
import json
import anthropic
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()
client = anthropic.AsyncAnthropic()


async def simulate_tool(name: str, args: dict) -> str:
    await asyncio.sleep(0.05)  # Simulate async I/O
    return json.dumps({"tool": name, "result": f"result for {args}"})


async def agentic_sse_stream(prompt: str):
    messages = [{"role": "user", "content": prompt}]
    tools = [
        {
            "name": "get_weather",
            "description": "Get current weather",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]

    while True:
        tool_uses = []
        response_content = []

        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        ) as stream:
            async for event in stream:
                if hasattr(event, "type"):
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            yield f"data: {json.dumps({'type': 'text', 'text': delta.text})}\n\n"
                        elif hasattr(delta, "partial_json"):
                            # Tool input streaming — forward progress indicator
                            yield f"data: {json.dumps({'type': 'tool_progress'})}\n\n"
                    elif event.type == "content_block_stop":
                        pass

            final = await stream.get_final_message()
            response_content = final.content

            for block in final.content:
                if block.type == "tool_use":
                    tool_uses.append(block)

        if not tool_uses:
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Resolve all tool calls concurrently
        tool_results = await asyncio.gather(
            *[simulate_tool(t.name, t.input) for t in tool_uses]
        )

        messages.append({"role": "assistant", "content": response_content})
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": t.id,
                    "content": r,
                }
                for t, r in zip(tool_uses, tool_results)
            ],
        })

        yield f"data: {json.dumps({'type': 'tool_resolved', 'count': len(tool_uses)})}\n\n"


@app.get("/agent-stream")
async def agent_stream(prompt: str):
    return StreamingResponse(
        agentic_sse_stream(prompt),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

## Solution 5: Streaming Proxy with Backpressure

Rate-limit the downstream write speed to match the consumer's capacity, applying backpressure to the generator.

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()


class BackpressureStream:
    """
    Wraps Claude streaming with a bounded buffer.
    If the consumer is slower than generation, the producer pauses.
    """

    def __init__(self, max_buffer: int = 64):
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=max_buffer)
        self._done = asyncio.Event()

    async def produce(self, prompt: str) -> None:
        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    # put() blocks when queue is full — backpressure!
                    await self._queue.put(text)
        finally:
            await self._queue.put(None)  # Sentinel

    async def consume(self):
        """Async generator that yields chunks, applying backpressure upstream."""
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk
            self._queue.task_done()


async def slow_downstream_writer(stream: BackpressureStream, delay: float = 0.01):
    """Simulates a slow downstream consumer (e.g., a sluggish HTTP client)."""
    parts = []
    async for chunk in stream.consume():
        await asyncio.sleep(delay)  # Simulate slow write
        parts.append(chunk)
    return "".join(parts)


async def main():
    prompt = "Write a detailed technical essay on distributed systems."
    stream = BackpressureStream(max_buffer=16)

    producer = asyncio.create_task(stream.produce(prompt))
    result = await slow_downstream_writer(stream, delay=0.005)
    await producer

    print(f"Received {len(result)} characters via backpressure stream.")


asyncio.run(main())
```

## Solution 6: Multi-Destination Tee Stream

Simultaneously stream to an HTTP response, a log file, and a metrics collector without buffering the full response.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class StreamMetrics:
    first_token_time: float | None = None
    total_tokens: int = 0
    start_time: float = field(default_factory=time.monotonic)

    def record_token(self, text: str) -> None:
        if self.first_token_time is None:
            self.first_token_time = time.monotonic() - self.start_time
        self.total_tokens += len(text.split())

    @property
    def tokens_per_second(self) -> float:
        elapsed = time.monotonic() - self.start_time
        return self.total_tokens / elapsed if elapsed > 0 else 0.0


class TeeStream:
    """Tee a Claude stream to multiple async queues simultaneously."""

    def __init__(self, destinations: int = 3):
        self._queues: list[asyncio.Queue[str | None]] = [
            asyncio.Queue(maxsize=512) for _ in range(destinations)
        ]

    def get_consumer(self, idx: int) -> "asyncio.Queue[str | None]":
        return self._queues[idx]

    async def distribute(self, chunk: str | None) -> None:
        await asyncio.gather(*[q.put(chunk) for q in self._queues])

    async def run(self, prompt: str) -> None:
        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                await self.distribute(text)
        await self.distribute(None)  # Sentinel to all consumers


async def http_consumer(q: asyncio.Queue) -> str:
    """Simulates writing to an HTTP response buffer."""
    parts = []
    while True:
        chunk = await q.get()
        if chunk is None:
            break
        parts.append(chunk)
    return "".join(parts)


async def log_consumer(q: asyncio.Queue, path: str = "/tmp/stream.log") -> None:
    """Write each chunk to a log file as it arrives."""
    with open(path, "w") as f:
        while True:
            chunk = await q.get()
            if chunk is None:
                break
            f.write(chunk)
            f.flush()


async def metrics_consumer(q: asyncio.Queue) -> StreamMetrics:
    """Collect latency and throughput metrics from the stream."""
    metrics = StreamMetrics()
    while True:
        chunk = await q.get()
        if chunk is None:
            break
        metrics.record_token(chunk)
    return metrics


async def main():
    tee = TeeStream(destinations=3)
    prompt = "Describe the history of the internet in detail."

    http_q = tee.get_consumer(0)
    log_q = tee.get_consumer(1)
    metrics_q = tee.get_consumer(2)

    http_task = asyncio.create_task(http_consumer(http_q))
    log_task = asyncio.create_task(log_consumer(log_q))
    metrics_task = asyncio.create_task(metrics_consumer(metrics_q))

    await tee.run(prompt)
    full_text, _, metrics = await asyncio.gather(http_task, log_task, metrics_task)

    print(f"HTTP response: {len(full_text)} chars")
    print(f"First token latency: {metrics.first_token_time:.3f}s")
    print(f"Throughput: {metrics.tokens_per_second:.1f} tokens/s")


asyncio.run(main())
```

## Comparison

| Solution | Transport | Fan-out | Backpressure | Tool calls | Best for |
|---|---|---|---|---|---|
| **SSE passthrough** | HTTP SSE | No | No | No | Single browser client |
| **WebSocket fan-out** | WebSocket | Yes | No | No | Real-time multi-user UI |
| **Redis pub/sub** | Message queue | Yes | No | No | Microservice consumers |
| **Bidirectional + tools** | HTTP SSE | No | No | Yes | Agentic streaming APIs |
| **Backpressure stream** | In-process | No | Yes | No | Slow downstream consumers |
| **Tee multi-destination** | In-process | Yes | Partial | No | Logging + metrics + HTTP |

Start with **SSE passthrough** (Solution 1) for straightforward web APIs. Add **backpressure** (Solution 5) when your downstream consumer is slower than Claude's generation rate. Use **tee stream** (Solution 6) when you need simultaneous delivery to logging, metrics, and HTTP without buffering.
