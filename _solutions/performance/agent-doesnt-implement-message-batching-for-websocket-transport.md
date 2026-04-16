---
title: "Agent Doesn't Implement Message Batching for WebSocket Transport"
description: "Agents sending many small messages over WebSocket incur per-frame overhead for each message — header bytes, flush calls, and TCP segment fragmentation add up to significant throughput loss. Implement message batching with adaptive windows that coalesce small messages into single frames while preserving latency guarantees for time-sensitive payloads."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-message-batching-for-websocket-transport
tags: [websocket, message-batching, throughput, latency, transport-optimization, performance]
symptoms:
  - "Agent sends 50 tool-result chunks as 50 separate WebSocket frames instead of batching"
  - "WebSocket throughput drops under high message rate due to per-frame TCP overhead"
  - "Streaming token output sends one frame per token — 4-byte payload per frame"
  - "No nagle-equivalent for WebSocket — every send flushes immediately"
  - "Batch size is hardcoded at 1 — no adaptive window based on message arrival rate"
---

## Why This Happens

WebSocket frames carry 2–14 bytes of framing overhead per message. At high message rates (streaming tokens, tool result chunks, heartbeats), per-frame overhead becomes the dominant cost. TCP Nagle's algorithm handles this for raw TCP, but WebSocket libraries typically disable it or bypass it with explicit flush. Adaptive message batching holds outgoing messages for a short window (1–5ms) and sends them as one frame, multiplying throughput while adding acceptable latency only to non-urgent messages.

## Solution 1: Adaptive Message Batcher

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

@dataclass
class OutboundMessage:
    message_id: str
    payload: Any
    priority: str = "normal"   # "realtime" | "normal" | "bulk"
    submitted_at: float = field(default_factory=time.monotonic)

class AdaptiveMessageBatcher:
    """
    Coalesces outbound WebSocket messages into batches.
    Realtime-priority messages flush immediately.
    Normal messages wait up to max_wait_ms or until batch_size_limit is hit.
    Bulk messages wait up to bulk_wait_ms.
    """

    def __init__(
        self,
        send_fn: Callable[[bytes], asyncio.Coroutine],
        max_wait_ms: float = 5.0,
        bulk_wait_ms: float = 50.0,
        batch_size_limit: int = 64,
        max_batch_bytes: int = 65536,
    ):
        self._send = send_fn
        self._normal_wait = max_wait_ms / 1000.0
        self._bulk_wait = bulk_wait_ms / 1000.0
        self._size_limit = batch_size_limit
        self._max_bytes = max_batch_bytes

        self._normal_queue: List[OutboundMessage] = []
        self._bulk_queue: List[OutboundMessage] = []
        self._flush_event = asyncio.Event()
        self._running = False
        self._stats = {"batches_sent": 0, "messages_sent": 0, "bytes_sent": 0}

    async def send(self, message: OutboundMessage) -> None:
        if message.priority == "realtime":
            # Bypass batching — send immediately
            data = self._serialize([message])
            await self._send(data)
            self._stats["batches_sent"] += 1
            self._stats["messages_sent"] += 1
            self._stats["bytes_sent"] += len(data)
            return

        if message.priority == "bulk":
            self._bulk_queue.append(message)
        else:
            self._normal_queue.append(message)
            if len(self._normal_queue) >= self._size_limit:
                self._flush_event.set()

    def _serialize(self, messages: List[OutboundMessage]) -> bytes:
        if len(messages) == 1:
            return json.dumps(messages[0].payload).encode("utf-8")
        batch = {"_batch": True, "messages": [m.payload for m in messages]}
        return json.dumps(batch).encode("utf-8")

    async def _flush_normal(self) -> None:
        if not self._normal_queue:
            return
        batch = self._normal_queue[:self._size_limit]
        self._normal_queue = self._normal_queue[self._size_limit:]

        # Split if too large
        data = self._serialize(batch)
        if len(data) > self._max_bytes:
            mid = len(batch) // 2
            await self._flush_batch(batch[:mid])
            await self._flush_batch(batch[mid:])
        else:
            await self._flush_batch(batch)

    async def _flush_batch(self, batch: List[OutboundMessage]) -> None:
        if not batch:
            return
        data = self._serialize(batch)
        await self._send(data)
        self._stats["batches_sent"] += 1
        self._stats["messages_sent"] += len(batch)
        self._stats["bytes_sent"] += len(data)

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await asyncio.wait_for(
                    self._flush_event.wait(),
                    timeout=self._normal_wait,
                )
                self._flush_event.clear()
            except asyncio.TimeoutError:
                pass
            await self._flush_normal()

    async def run_bulk_flusher(self) -> None:
        while self._running:
            await asyncio.sleep(self._bulk_wait)
            await self._flush_batch(self._bulk_queue)
            self._bulk_queue = []

    def stats(self) -> dict:
        total_messages = max(self._stats["messages_sent"], 1)
        total_batches = max(self._stats["batches_sent"], 1)
        return {
            **self._stats,
            "avg_messages_per_batch": round(total_messages / total_batches, 2),
            "avg_batch_bytes": round(self._stats["bytes_sent"] / total_batches, 1),
            "pending_normal": len(self._normal_queue),
            "pending_bulk": len(self._bulk_queue),
        }
```

## Solution 2: Token Streaming Batcher

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, List

@dataclass
class TokenChunk:
    token: str
    sequence: int
    is_final: bool = False

class TokenStreamingBatcher:
    """
    Batches LLM token streaming output before sending over WebSocket.
    Collects tokens for up to batch_window_ms, then sends them as a
    single frame with accumulated text. Preserves is_final signal.
    """

    def __init__(
        self,
        send_fn: Callable[[dict], asyncio.Coroutine],
        batch_window_ms: float = 20.0,
        max_batch_tokens: int = 10,
    ):
        self._send = send_fn
        self._window = batch_window_ms / 1000.0
        self._max_tokens = max_batch_tokens
        self._buffer: List[TokenChunk] = []
        self._last_flush = time.monotonic()

    async def feed(self, chunk: TokenChunk) -> None:
        self._buffer.append(chunk)

        should_flush = (
            chunk.is_final
            or len(self._buffer) >= self._max_tokens
            or (time.monotonic() - self._last_flush) >= self._window
        )

        if should_flush:
            await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        text = "".join(c.token for c in self._buffer)
        is_final = any(c.is_final for c in self._buffer)
        payload = {
            "type": "token_batch",
            "text": text,
            "token_count": len(self._buffer),
            "is_final": is_final,
            "sequence_start": self._buffer[0].sequence,
            "sequence_end": self._buffer[-1].sequence,
        }
        await self._send(payload)
        self._buffer = []
        self._last_flush = time.monotonic()

    async def stream(self, token_iter: AsyncIterator[TokenChunk]) -> None:
        async for chunk in token_iter:
            await self.feed(chunk)
        await self._flush()
```

## Solution 3: Priority-Aware Send Queue

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

@dataclass
class PrioritizedFrame:
    data: bytes
    priority: int   # 0 = urgent, 1 = normal, 2 = bulk
    submitted_at: float

class PriorityAwareSendQueue:
    """
    Three-tier send queue: urgent frames bypass batching, normal and bulk
    frames are interleaved at a configurable ratio to prevent bulk work
    from starving normal-priority sends.
    """

    def __init__(
        self,
        send_fn,
        urgent_ratio: int = 3,    # send 3 urgent per 1 normal
        normal_ratio: int = 2,    # send 2 normal per 1 bulk
    ):
        self._send = send_fn
        self._urgent: Deque[PrioritizedFrame] = deque()
        self._normal: Deque[PrioritizedFrame] = deque()
        self._bulk: Deque[PrioritizedFrame] = deque()
        self._urgent_ratio = urgent_ratio
        self._normal_ratio = normal_ratio
        self._counters = {"urgent": 0, "normal": 0, "bulk": 0}

    def enqueue(self, data: bytes, priority: int = 1) -> None:
        frame = PrioritizedFrame(data=data, priority=priority, submitted_at=time.monotonic())
        if priority == 0:
            self._urgent.appendleft(frame)
        elif priority == 1:
            self._normal.append(frame)
        else:
            self._bulk.append(frame)

    async def drain(self) -> None:
        """Send one frame according to priority ratios."""
        if self._urgent:
            frame = self._urgent.popleft()
            await self._send(frame.data)
            self._counters["urgent"] += 1
        elif self._normal and (
            not self._bulk or
            self._counters["normal"] % self._normal_ratio != 0
        ):
            frame = self._normal.popleft()
            await self._send(frame.data)
            self._counters["normal"] += 1
        elif self._bulk:
            frame = self._bulk.popleft()
            await self._send(frame.data)
            self._counters["bulk"] += 1

    async def run(self, interval_ms: float = 1.0) -> None:
        while True:
            await self.drain()
            if not any([self._urgent, self._normal, self._bulk]):
                await asyncio.sleep(interval_ms / 1000.0)

    def depth(self) -> dict:
        return {
            "urgent": len(self._urgent),
            "normal": len(self._normal),
            "bulk": len(self._bulk),
        }
```

## Solution 4: Batch Decompressor (Receiver Side)

```python
import json
from dataclasses import dataclass
from typing import Any, Callable, List

@dataclass
class ReceivedMessage:
    payload: Any
    is_batch: bool
    batch_size: int

class BatchDecompressor:
    """
    Receiver-side decompressor for batched WebSocket messages.
    Unpacks batch frames into individual message callbacks.
    """

    def __init__(self, on_message: Callable[[Any], None]):
        self._on_message = on_message
        self._batch_count = 0
        self._message_count = 0

    def receive(self, raw_data: bytes) -> ReceivedMessage:
        try:
            payload = json.loads(raw_data)
        except Exception:
            payload = raw_data.decode("utf-8", errors="replace")

        if isinstance(payload, dict) and payload.get("_batch"):
            messages = payload.get("messages", [])
            for msg in messages:
                self._on_message(msg)
            self._batch_count += 1
            self._message_count += len(messages)
            return ReceivedMessage(payload=messages, is_batch=True, batch_size=len(messages))
        else:
            self._on_message(payload)
            self._message_count += 1
            return ReceivedMessage(payload=payload, is_batch=False, batch_size=1)

    def stats(self) -> dict:
        return {
            "batches_received": self._batch_count,
            "messages_received": self._message_count,
            "avg_batch_size": round(
                self._message_count / max(self._batch_count, 1), 2
            ),
        }
```

## Solution 5: Throughput Benchmark

```python
import asyncio
import time
from dataclasses import dataclass
from typing import List

@dataclass
class ThroughputSample:
    window_start: float
    messages_sent: int
    bytes_sent: int
    batches_sent: int

class WebSocketThroughputBenchmark:
    """
    Tracks and reports WebSocket send throughput to validate that
    batching is actually improving frame efficiency.
    """

    def __init__(self, window_seconds: float = 10.0):
        self._window = window_seconds
        self._samples: List[ThroughputSample] = []
        self._current = ThroughputSample(
            window_start=time.monotonic(), messages_sent=0, bytes_sent=0, batches_sent=0
        )

    def record_send(self, message_count: int, byte_count: int) -> None:
        now = time.monotonic()
        self._current.messages_sent += message_count
        self._current.bytes_sent += byte_count
        self._current.batches_sent += 1

        if now - self._current.window_start >= self._window:
            self._samples.append(self._current)
            if len(self._samples) > 20:
                self._samples.pop(0)
            self._current = ThroughputSample(
                window_start=now, messages_sent=0, bytes_sent=0, batches_sent=0
            )

    def report(self) -> dict:
        if not self._samples:
            return {"status": "insufficient_data"}
        last = self._samples[-1]
        elapsed = self._window
        return {
            "messages_per_second": round(last.messages_sent / elapsed, 1),
            "bytes_per_second": round(last.bytes_sent / elapsed, 0),
            "frames_per_second": round(last.batches_sent / elapsed, 1),
            "avg_messages_per_frame": round(
                last.messages_sent / max(last.batches_sent, 1), 2
            ),
            "avg_frame_bytes": round(last.bytes_sent / max(last.batches_sent, 1), 1),
            "window_seconds": self._window,
        }
```

## Solution 6: WebSocket Batch Config

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class WebSocketBatchConfig:
    """
    Centralized configuration for WebSocket batching behavior.
    Tune based on observed message rates and latency requirements.
    """
    # Batching windows
    realtime_flush_immediately: bool = True
    normal_batch_window_ms: float = 5.0
    bulk_batch_window_ms: float = 50.0
    token_stream_window_ms: float = 20.0

    # Size limits
    max_batch_messages: int = 64
    max_batch_bytes: int = 65536
    max_tokens_per_batch: int = 10

    # Queue limits
    max_queue_depth: int = 1000

    @classmethod
    def low_latency(cls) -> "WebSocketBatchConfig":
        """For real-time interactive agents."""
        return cls(
            normal_batch_window_ms=2.0,
            token_stream_window_ms=10.0,
            max_tokens_per_batch=5,
        )

    @classmethod
    def high_throughput(cls) -> "WebSocketBatchConfig":
        """For bulk data transfer agents."""
        return cls(
            normal_batch_window_ms=20.0,
            bulk_batch_window_ms=100.0,
            max_batch_messages=256,
            max_batch_bytes=1048576,
        )
```

## Comparison

| Approach | Latency Added | Throughput Gain | Streaming Support | Priority-Aware |
|---|---|---|---|---|
| AdaptiveMessageBatcher | 0–5ms normal, 0 realtime | High | No | Yes (3 tiers) |
| TokenStreamingBatcher | 0–20ms | Medium | Yes | No |
| PriorityAwareSendQueue | 0 urgent, variable others | Medium | No | Yes (ratio-based) |
| BatchDecompressor | None (receiver) | N/A | N/A | N/A |
| WebSocketThroughputBenchmark | None (metrics) | N/A | N/A | N/A |
| WebSocketBatchConfig | N/A (config) | N/A | N/A | N/A |

**Best for production**: Use `AdaptiveMessageBatcher` for all outbound WebSocket sends. Classify messages at send time: streaming token chunks → `TokenStreamingBatcher` with 20ms window; user-facing events → `normal` priority with 5ms window; telemetry and logs → `bulk` with 50ms window. Start with `WebSocketBatchConfig.low_latency()` for interactive agents and benchmark with `WebSocketThroughputBenchmark` to verify frame efficiency improves before shipping.
