---
title: "Agent Doesn't Implement Streaming Response Buffering Control"
description: "Agents that stream LLM responses to clients without buffering control either block on slow consumers causing upstream backpressure into the model, or drop tokens when consumers are too slow to read. Implement streaming response buffering with configurable high-water marks, consumer lag detection, and graceful degradation that switches from streaming to buffered delivery when a consumer falls too far behind."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-streaming-response-buffering-control
tags: [streaming, buffering, backpressure, token-streaming, consumer-lag, flow-control]
symptoms:
  - "Slow network clients cause the agent to stall mid-generation waiting for the consumer to drain"
  - "Tokens are dropped silently when the streaming buffer overflows under high load"
  - "No visibility into consumer read lag — buffer is always assumed to be drained"
  - "Streaming connections held open by idle clients exhaust file descriptor limits"
  - "P99 generation latency inflated by slow consumers blocking the producer"
---

## Why This Happens

Streaming LLM output involves a producer (the model generating tokens) and a consumer (the client reading them). When the consumer is slower than the producer, tokens accumulate in a buffer. Without a high-water mark, the buffer grows unbounded, consuming memory. With a naive high-water mark, the producer blocks, which stalls generation and inflates latency for all concurrent requests sharing the inference server. Buffering control requires decoupling the producer and consumer with an asyncio queue, monitoring consumer lag, and switching to buffered-then-flush delivery when lag exceeds a threshold — trading time-to-first-token for throughput.

## Solution 1: Stream Buffer

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, List, Optional


class StreamDeliveryMode(str, Enum):
    STREAMING = "streaming"       # tokens sent as produced
    BUFFERED = "buffered"         # accumulated, flushed when complete


@dataclass
class StreamToken:
    text: str
    index: int
    is_final: bool = False
    produced_at: float = field(default_factory=time.time)


class StreamBuffer:
    """
    An asyncio queue with a configurable high-water mark.
    Tracks producer and consumer positions to compute lag.
    """

    def __init__(self, high_water_mark: int = 256):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=high_water_mark)
        self._produced = 0
        self._consumed = 0
        self._dropped = 0
        self._created_at = time.time()

    async def put(self, token: StreamToken, timeout: float = 1.0) -> bool:
        """Returns False if the put timed out (consumer too slow)."""
        try:
            await asyncio.wait_for(self._queue.put(token), timeout=timeout)
            self._produced += 1
            return True
        except asyncio.TimeoutError:
            self._dropped += 1
            return False

    async def get(self) -> StreamToken:
        token = await self._queue.get()
        self._consumed += 1
        return token

    def lag(self) -> int:
        return self._produced - self._consumed

    def utilization(self) -> float:
        size = self._queue.maxsize
        return self._queue.qsize() / size if size > 0 else 0.0

    def stats(self) -> dict:
        return {
            "produced": self._produced,
            "consumed": self._consumed,
            "dropped": self._dropped,
            "lag": self.lag(),
            "utilization": round(self.utilization(), 3),
            "age_seconds": round(time.time() - self._created_at, 1),
        }
```

## Solution 2: Consumer Lag Monitor

```python
import time
from typing import Optional


class ConsumerLagMonitor:
    """
    Monitors buffer lag over time and determines whether a consumer
    has fallen too far behind to continue in streaming mode.
    """

    def __init__(
        self,
        lag_switch_threshold: int = 64,      # tokens behind before switching to buffered
        lag_check_interval_seconds: float = 0.5,
        stall_timeout_seconds: float = 5.0,  # close if consumer stalls completely
    ):
        self._lag_threshold = lag_switch_threshold
        self._check_interval = lag_check_interval_seconds
        self._stall_timeout = stall_timeout_seconds
        self._last_consumed = 0
        self._last_check_at = time.time()
        self._stall_since: Optional[float] = None

    def update(self, buffer: StreamBuffer) -> dict:
        now = time.time()
        current_consumed = buffer._consumed
        lag = buffer.lag()

        # Stall detection: consumed count has not changed
        if current_consumed == self._last_consumed and lag > 0:
            if self._stall_since is None:
                self._stall_since = now
        else:
            self._stall_since = None

        self._last_consumed = current_consumed
        self._last_check_at = now

        stalled = (
            self._stall_since is not None
            and (now - self._stall_since) >= self._stall_timeout
        )
        should_switch = lag >= self._lag_threshold

        return {
            "lag": lag,
            "should_switch_to_buffered": should_switch,
            "consumer_stalled": stalled,
            "stall_duration_seconds": round(now - self._stall_since, 1) if self._stall_since else 0.0,
        }
```

## Solution 3: Adaptive Stream Dispatcher

```python
import asyncio
import time
from typing import AsyncIterator, Callable, List, Optional


class AdaptiveStreamDispatcher:
    """
    Dispatches tokens from an LLM stream to a consumer with adaptive
    buffering. Starts in STREAMING mode; switches to BUFFERED if the
    consumer falls too far behind; closes the connection if stalled.
    """

    def __init__(
        self,
        buffer: StreamBuffer,
        lag_monitor: ConsumerLagMonitor,
        mode: StreamDeliveryMode = StreamDeliveryMode.STREAMING,
    ):
        self._buffer = buffer
        self._monitor = lag_monitor
        self._mode = mode
        self._all_tokens: List[StreamToken] = []
        self._switched_at: Optional[float] = None

    async def produce(self, token_stream: AsyncIterator[str]) -> None:
        """Drive tokens from the LLM stream into the buffer."""
        idx = 0
        async for text in token_stream:
            token = StreamToken(text=text, index=idx)
            self._all_tokens.append(token)
            status = self._monitor.update(self._buffer)

            if status["consumer_stalled"]:
                # Abandon the stalled connection
                sentinel = StreamToken(text="", index=idx + 1, is_final=True)
                await self._buffer.put(sentinel, timeout=0.1)
                return

            if status["should_switch_to_buffered"] and self._mode == StreamDeliveryMode.STREAMING:
                self._mode = StreamDeliveryMode.BUFFERED
                self._switched_at = time.time()

            if self._mode == StreamDeliveryMode.STREAMING:
                await self._buffer.put(token)
            idx += 1

        # Final token
        final = StreamToken(text="", index=idx, is_final=True)
        if self._mode == StreamDeliveryMode.BUFFERED:
            # Flush all accumulated tokens at once
            for t in self._all_tokens:
                await self._buffer.put(t)
        await self._buffer.put(final)

    def delivery_stats(self) -> dict:
        return {
            "mode": self._mode.value,
            "switched_to_buffered": self._switched_at is not None,
            "switched_at": self._switched_at,
            "total_tokens": len(self._all_tokens),
            "buffer": self._buffer.stats(),
        }
```

## Solution 4: Streaming Session Manager

```python
import asyncio
import time
from threading import Lock
from typing import Dict, Optional


class StreamingSessionManager:
    """
    Manages active streaming sessions, enforcing a maximum concurrent
    stream limit and evicting stalled sessions to reclaim resources.
    """

    def __init__(
        self,
        max_concurrent_streams: int = 100,
        stall_eviction_seconds: float = 10.0,
    ):
        self._max = max_concurrent_streams
        self._stall_eviction = stall_eviction_seconds
        self._sessions: Dict[str, dict] = {}
        self._lock = Lock()

    def register(self, session_id: str, dispatcher: AdaptiveStreamDispatcher) -> bool:
        with self._lock:
            if len(self._sessions) >= self._max:
                return False
            self._sessions[session_id] = {
                "dispatcher": dispatcher,
                "created_at": time.time(),
                "last_active": time.time(),
            }
            return True

    def touch(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["last_active"] = time.time()

    def evict_stalled(self) -> int:
        cutoff = time.time() - self._stall_eviction
        with self._lock:
            stalled = [
                sid for sid, data in self._sessions.items()
                if data["last_active"] < cutoff
            ]
            for sid in stalled:
                del self._sessions[sid]
        return len(stalled)

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def summary(self) -> dict:
        with self._lock:
            return {
                "active_streams": len(self._sessions),
                "max_concurrent": self._max,
                "utilization": round(len(self._sessions) / max(self._max, 1), 3),
            }
```

## Solution 5: Buffering Metrics Recorder

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class StreamBufferingMetricsRecorder:
    """
    Records buffer utilization samples and mode switch events
    for streaming performance analysis.
    """

    def __init__(self, max_samples: int = 10000):
        self._max = max_samples
        self._utilization_samples: Deque[Tuple[float, float]] = deque()
        self._mode_switches = 0
        self._stall_evictions = 0
        self._lock = Lock()

    def record_utilization(self, utilization: float) -> None:
        with self._lock:
            self._utilization_samples.append((time.time(), utilization))
            if len(self._utilization_samples) > self._max:
                self._utilization_samples.popleft()

    def record_mode_switch(self) -> None:
        with self._lock:
            self._mode_switches += 1

    def record_stall_eviction(self) -> None:
        with self._lock:
            self._stall_evictions += 1

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [u for ts, u in self._utilization_samples if ts >= cutoff]
            switches = self._mode_switches
            evictions = self._stall_evictions
        if not recent:
            return {"window_seconds": window_seconds, "samples": 0}
        return {
            "window_seconds": window_seconds,
            "samples": len(recent),
            "avg_utilization": round(sum(recent) / len(recent), 3),
            "max_utilization": round(max(recent), 3),
            "mode_switches": switches,
            "stall_evictions": evictions,
        }
```

## Solution 6: Streaming Control Dashboard

```python
import time


class StreamingControlDashboard:
    """
    Combines session manager status and buffering metrics
    into a single operational view.
    """

    def __init__(
        self,
        session_manager: StreamingSessionManager,
        metrics: StreamBufferingMetricsRecorder,
    ):
        self._sessions = session_manager
        self._metrics = metrics

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "sessions": self._sessions.summary(),
            "buffering_metrics": self._metrics.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Buffer with HWM | Lag Monitoring | Adaptive Mode Switch | Session Management | Metrics |
|---|---|---|---|---|---|
| StreamBuffer | Yes (asyncio.Queue) | Via utilization | No | No | Basic stats |
| ConsumerLagMonitor | No | Yes (lag + stall) | Signal only | No | No |
| AdaptiveStreamDispatcher | Via buffer | Via monitor | Yes (stream→buffered) | No | No |
| StreamingSessionManager | No | No | No | Yes (eviction) | Count |
| StreamBufferingMetricsRecorder | No | No | No | No | Yes |
| StreamingControlDashboard | No | No | No | No | Yes |

**Best for production**: Set `high_water_mark=256` tokens — this gives a slow consumer about 2–3 seconds of buffer at typical token rates before triggering the mode switch. Set `lag_switch_threshold=64` to switch to buffered mode early, before the buffer fills completely. Set `stall_timeout_seconds=5.0` and close stalled connections aggressively — a client that hasn't consumed a single token in 5 seconds is almost certainly disconnected, and holding the connection wastes a file descriptor and memory. Monitor `mode_switches` per hour: above 10% of streams switching to buffered mode indicates your consumer clients are systematically slower than generation speed and you should consider reducing model concurrency.
