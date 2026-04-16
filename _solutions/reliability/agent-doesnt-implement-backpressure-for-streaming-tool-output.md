---
title: "Agent Doesn't Implement Backpressure for Streaming Tool Output"
description: "AI agents that stream tool output to consumers without flow control allow fast producers to overwhelm slow consumers, causing unbounded memory growth, dropped events, or OOM crashes. Backpressure signals from the consumer to the producer to slow down or pause, coupling production rate to consumption capacity and keeping buffer sizes bounded under any load."
date: 2025-02-18
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-backpressure-for-streaming-tool-output
tags:
  - backpressure
  - flow-control
  - streaming
  - producer-consumer
  - reliability
  - asyncio
  - buffer-management
symptoms:
  - "Memory grows unboundedly when a slow UI client receives a fast tool output stream"
  - "asyncio.Queue fills without bound when LLM produces tokens faster than they are consumed"
  - "OOM crash after 10 minutes of streaming a large document tool response"
  - "No mechanism for the consumer to signal the producer to pause output"
  - "Dropped events when internal buffer overflows and no retry logic exists"
---

## Problem

In a producer-consumer streaming pipeline, a fast producer (LLM token emitter, tool output streamer) paired with a slow consumer (WebSocket client on a mobile connection, file writer on a spinning disk) accumulates items in an intermediate buffer. Without backpressure, the buffer grows until memory is exhausted. Backpressure inverts control: when the consumer's queue is full or its processing rate drops, it signals the producer to pause, slow down, or drop lower-priority items. The producer resumes when the consumer signals readiness.

---

## Solution 1: BoundedAsyncQueue — Queue with Backpressure Semantics

```python
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Generic, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class QueueStats:
    enqueued: int = 0
    dequeued: int = 0
    dropped: int = 0
    backpressure_events: int = 0
    max_size_reached: bool = False


class BoundedAsyncQueue(Generic[T]):
    """
    Bounded asyncio queue with explicit backpressure modes:
    - BLOCK: producer awaits until space is available (natural backpressure)
    - DROP: producer drops new items when full (lossy, for non-critical streams)
    - DROP_OLDEST: evict oldest item to make room (sliding window)

    Usage:
        queue = BoundedAsyncQueue(maxsize=100, mode="block")
        await queue.put(item)       # Blocks if full (backpressure)
        item = await queue.get()    # Blocks until item available
    """

    MODES = ("block", "drop", "drop_oldest")

    def __init__(self, maxsize: int = 100,
                  mode: str = "block",
                  high_watermark: float = 0.8):
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {self.MODES}")
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._mode = mode
        self._maxsize = maxsize
        self._hwm = int(maxsize * high_watermark)
        self._stats = QueueStats()
        self._paused = False

    async def put(self, item: T, timeout: Optional[float] = None) -> bool:
        """
        Enqueue an item. Returns True if enqueued, False if dropped.
        Applies backpressure based on the configured mode.
        """
        current_size = self._q.qsize()

        if current_size >= self._hwm and not self._paused:
            self._paused = True
            self._stats.backpressure_events += 1
            logger.debug(
                "backpressure_activated size=%d hwm=%d",
                current_size, self._hwm,
            )

        if self._mode == "drop" and self._q.full():
            self._stats.dropped += 1
            logger.debug("queue_item_dropped size=%d", current_size)
            return False

        if self._mode == "drop_oldest" and self._q.full():
            try:
                self._q.get_nowait()
                self._stats.dropped += 1
            except asyncio.QueueEmpty:
                pass

        if timeout is not None:
            try:
                await asyncio.wait_for(self._q.put(item), timeout=timeout)
            except asyncio.TimeoutError:
                self._stats.dropped += 1
                return False
        else:
            await self._q.put(item)

        self._stats.enqueued += 1
        if current_size >= self._maxsize:
            self._stats.max_size_reached = True
        return True

    async def get(self) -> T:
        item = await self._q.get()
        self._stats.dequeued += 1
        if self._paused and self._q.qsize() < self._hwm // 2:
            self._paused = False
            logger.debug("backpressure_released size=%d", self._q.qsize())
        return item

    def task_done(self):
        self._q.task_done()

    @property
    def is_under_pressure(self) -> bool:
        return self._paused

    def qsize(self) -> int:
        return self._q.qsize()

    async def __aiter__(self) -> AsyncIterator[T]:
        while True:
            item = await self.get()
            if item is None:  # Sentinel for end-of-stream
                return
            yield item

    def stats(self) -> QueueStats:
        return self._stats
```

---

## Solution 2: StreamingBackpressureController — Rate-Limit Producer Based on Consumer Lag

```python
import asyncio
import logging
import time
from typing import Any, AsyncIterator, Callable, Optional

logger = logging.getLogger(__name__)


class StreamingBackpressureController:
    """
    Controls a streaming producer's rate based on consumer lag.
    Measures the gap between produced and consumed item counts; when
    the gap grows beyond the threshold, inserts increasing sleep delays
    into the producer to slow it down proportionally.

    Usage:
        controller = StreamingBackpressureController(max_lag=50, max_delay_s=0.5)

        async for token in llm_stream:
            await controller.before_produce()   # May delay producer
            await output_queue.put(token)
            # Consumer calls controller.on_consumed() after processing each item
    """

    def __init__(self, max_lag: int = 50,
                  max_delay_s: float = 0.5,
                  base_delay_s: float = 0.01):
        self._max_lag = max_lag
        self._max_delay = max_delay_s
        self._base_delay = base_delay_s
        self._produced = 0
        self._consumed = 0
        self._total_delay_s = 0.0
        self._pause_events = 0

    def on_consumed(self, count: int = 1):
        self._consumed += count

    @property
    def lag(self) -> int:
        return self._produced - self._consumed

    async def before_produce(self):
        """Call before producing each item. Applies delay if lag is high."""
        self._produced += 1
        lag = self.lag

        if lag <= self._max_lag * 0.5:
            return  # No pressure

        # Linear backoff: delay scales from 0 to max_delay as lag approaches max_lag
        pressure = min(lag / self._max_lag, 1.0)
        delay = self._base_delay + (self._max_delay - self._base_delay) * pressure

        if lag >= self._max_lag:
            # Hard pause until consumer catches up
            self._pause_events += 1
            logger.debug(
                "backpressure_hard_pause lag=%d max_lag=%d", lag, self._max_lag
            )
            while self.lag >= self._max_lag * 0.8:
                await asyncio.sleep(0.05)

        self._total_delay_s += delay
        await asyncio.sleep(delay)

    def stats(self) -> dict:
        return {
            "produced": self._produced,
            "consumed": self._consumed,
            "current_lag": self.lag,
            "total_delay_s": round(self._total_delay_s, 3),
            "pause_events": self._pause_events,
        }
```

---

## Solution 3: TokenStreamBuffer — Backpressure-Aware LLM Token Buffer

```python
import asyncio
import logging
import time
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)


class TokenStreamBuffer:
    """
    Buffers LLM token stream output between the producer (LLM stream)
    and consumer (WebSocket, file, UI). When the consumer is slow, the
    buffer fills and signals the LLM stream iteration to pause.

    Usage:
        buffer = TokenStreamBuffer(max_tokens=200, flush_interval_s=0.05)

        # Producer task:
        async for token in llm.stream(messages):
            await buffer.write(token)
        await buffer.close()

        # Consumer task:
        async for chunk in buffer.read():
            await websocket.send(chunk)
    """

    def __init__(self, max_tokens: int = 200,
                  flush_interval_s: float = 0.05,
                  drop_on_full: bool = False):
        self._max = max_tokens
        self._flush_interval = flush_interval_s
        self._drop = drop_on_full
        self._queue: BoundedAsyncQueue = BoundedAsyncQueue(
            maxsize=max_tokens,
            mode="drop" if drop_on_full else "block",
        )
        self._closed = False
        self._total_written = 0
        self._total_read = 0

    async def write(self, token: str) -> bool:
        if self._closed:
            return False
        ok = await self._queue.put(token)
        if ok:
            self._total_written += 1
        return ok

    async def close(self):
        """Signal end-of-stream to consumer."""
        self._closed = True
        await self._queue.put(None)  # Sentinel

    async def read(self, batch_size: int = 1) -> AsyncIterator[str]:
        """Yield tokens; coalesces up to batch_size tokens per yield for efficiency."""
        while True:
            tokens = []
            try:
                first = await asyncio.wait_for(
                    self._queue.get(), timeout=self._flush_interval
                )
                if first is None:
                    if tokens:
                        yield "".join(tokens)
                    return
                tokens.append(first)
                self._total_read += 1

                # Drain up to batch_size without waiting
                for _ in range(batch_size - 1):
                    try:
                        next_token = self._queue._q.get_nowait()
                        if next_token is None:
                            self._closed = True
                            break
                        tokens.append(next_token)
                        self._total_read += 1
                    except asyncio.QueueEmpty:
                        break

                yield "".join(tokens)
            except asyncio.TimeoutError:
                pass

    @property
    def buffer_utilization(self) -> float:
        return self._queue.qsize() / self._max

    def stats(self) -> dict:
        return {
            "written": self._total_written,
            "read": self._total_read,
            "pending": self._queue.qsize(),
            "utilization": round(self.buffer_utilization, 3),
            "queue_stats": self._queue.stats().__dict__,
        }
```

---

## Solution 4: AdaptiveWindowController — Dynamic Window Sizing

```python
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class AdaptiveWindowController:
    """
    Implements TCP-style additive-increase/multiplicative-decrease (AIMD)
    window sizing for item batches. When transmissions succeed, the window
    grows by 1; when congestion is detected (queue full, timeout), the
    window halves. Converges to the maximum sustainable throughput.

    Usage:
        controller = AdaptiveWindowController(initial_window=10)

        while items_to_send:
            batch = items_to_send[:controller.window]
            success = await send_batch(batch)
            if success:
                controller.on_success()
                items_to_send = items_to_send[len(batch):]
            else:
                controller.on_congestion()
    """

    def __init__(self, initial_window: int = 10,
                  min_window: int = 1,
                  max_window: int = 500,
                  decrease_factor: float = 0.5):
        self._window = initial_window
        self._min = min_window
        self._max = max_window
        self._decrease = decrease_factor
        self._successes = 0
        self._congestion_events = 0

    @property
    def window(self) -> int:
        return int(self._window)

    def on_success(self):
        """Additive increase."""
        self._window = min(self._window + 1, self._max)
        self._successes += 1

    def on_congestion(self):
        """Multiplicative decrease."""
        self._window = max(self._window * self._decrease, self._min)
        self._congestion_events += 1
        logger.debug(
            "aimd_congestion new_window=%.1f events=%d",
            self._window, self._congestion_events,
        )

    def stats(self) -> dict:
        return {
            "window": self.window,
            "successes": self._successes,
            "congestion_events": self._congestion_events,
        }
```

---

## Solution 5: StreamHealthMonitor — Detect Stalled Consumers

```python
import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class StreamHealthMonitor:
    """
    Monitors a streaming pipeline for stalled consumers: when the
    consumption rate drops to near zero while the queue remains non-empty,
    it fires a stall callback (to log, alert, or restart the consumer).

    Usage:
        monitor = StreamHealthMonitor(
            queue=output_queue,
            stall_threshold_s=5.0,
            on_stall=lambda: restart_consumer(),
        )
        await monitor.start()
        # ... streaming pipeline runs ...
        await monitor.stop()
    """

    def __init__(self, queue: BoundedAsyncQueue,
                  stall_threshold_s: float = 5.0,
                  check_interval_s: float = 1.0,
                  on_stall: Optional[Callable] = None):
        self._queue = queue
        self._stall_threshold = stall_threshold_s
        self._interval = check_interval_s
        self._on_stall = on_stall or self._log_stall
        self._task: Optional[asyncio.Task] = None
        self._last_dequeued = 0
        self._stall_start: Optional[float] = None

    @staticmethod
    def _log_stall():
        logger.warning("stream_consumer_stalled")

    async def start(self):
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self):
        while True:
            await asyncio.sleep(self._interval)
            current_dequeued = self._queue.stats().dequeued
            queue_size = self._queue.qsize()

            if queue_size > 0 and current_dequeued == self._last_dequeued:
                # Queue non-empty but no progress
                if self._stall_start is None:
                    self._stall_start = time.monotonic()
                elif time.monotonic() - self._stall_start > self._stall_threshold:
                    logger.critical(
                        "stream_stall_detected queue_size=%d stall_s=%.1f",
                        queue_size,
                        time.monotonic() - self._stall_start,
                    )
                    self._on_stall()
                    self._stall_start = None
            else:
                self._stall_start = None

            self._last_dequeued = current_dequeued
```

---

## Solution 6: BackpressureStreamPipeline — Full Producer-Consumer Pipeline

```python
import asyncio
import logging
from typing import Any, AsyncIterator, Callable, Optional

logger = logging.getLogger(__name__)


class BackpressureStreamPipeline:
    """
    End-to-end streaming pipeline with backpressure between producer
    and consumer. Producer rate is limited by the consumer's processing
    speed via bounded queues and AIMD window control.

    Usage:
        pipeline = BackpressureStreamPipeline(
            buffer_size=200,
            mode="block",
        )
        await pipeline.run(
            producer=llm_token_stream(messages),
            consumer=websocket_sender(ws),
        )
        print(pipeline.stats())
    """

    def __init__(self, buffer_size: int = 200,
                  mode: str = "block",
                  stall_threshold_s: float = 10.0):
        self._buffer = TokenStreamBuffer(max_tokens=buffer_size,
                                          drop_on_full=(mode == "drop"))
        self._bp_controller = StreamingBackpressureController(
            max_lag=buffer_size // 2,
        )
        self._stall_monitor: Optional[StreamHealthMonitor] = None
        self._stall_threshold = stall_threshold_s

    async def run(self, producer: AsyncIterator[str],
                   consumer: Callable[[str], Any]):
        """Stream from producer through buffer to consumer with backpressure."""
        monitor = StreamHealthMonitor(
            self._buffer._queue,
            stall_threshold_s=self._stall_threshold,
        )
        await monitor.start()

        async def _produce():
            async for token in producer:
                await self._bp_controller.before_produce()
                await self._buffer.write(token)
            await self._buffer.close()

        async def _consume():
            async for chunk in self._buffer.read(batch_size=5):
                await consumer(chunk)
                self._bp_controller.on_consumed(len(chunk))

        try:
            await asyncio.gather(_produce(), _consume())
        finally:
            await monitor.stop()

    def stats(self) -> dict:
        return {
            "buffer": self._buffer.stats(),
            "backpressure": self._bp_controller.stats(),
        }
```

---

## Comparison

| Approach | Bounded Buffer | Producer Slowdown | Batch Coalescing | Window Sizing | Stall Detection | Integrated |
|---|---|---|---|---|---|---|
| **BoundedAsyncQueue** | Yes | Via blocking | No | No | No | No |
| **StreamingBackpressureController** | No | Yes | No | No | No | No |
| **TokenStreamBuffer** | Yes | Via blocking | Yes | No | No | No |
| **AdaptiveWindowController** | No | Via window | No | Yes | No | No |
| **StreamHealthMonitor** | No | No | No | No | Yes | No |
| **BackpressureStreamPipeline** | Yes | Yes | Yes | No | Yes | Yes |

**Key insight**: the simplest backpressure implementation is `asyncio.Queue(maxsize=N)` with `await queue.put()` — when the queue is full, the producer naturally awaits, coupling its rate to the consumer's throughput. This alone prevents OOM for most streaming use cases. Add `drop_oldest` mode only for real-time streams where staleness is worse than loss (audio/video). For LLM token streaming to WebSocket clients, `buffer_size=200` tokens is a good starting point — enough to smooth over 50ms consumer hiccups at 100 tokens/second without buffering more than 2 seconds of output.
