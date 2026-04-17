---
title: "Agent Doesn't Implement Message Queue Backpressure Handling"
description: "Agents connected to message queues that consume messages faster than they can process them accumulate in-flight work, exhaust memory, and crash — or consume too slowly and let the queue grow unboundedly. Implement backpressure handling that monitors queue depth and processing rate, adjusts consumption speed dynamically, and signals producers to slow down when the agent is overwhelmed."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-message-queue-backpressure-handling
tags: [backpressure, message-queue, flow-control, consumer-throttling, queue-depth, overload-protection]
symptoms:
  - "Agent OOMs when queue depth spikes — all messages accepted but not yet processed"
  - "Processing rate drops under load but consumption rate stays high"
  - "No mechanism to pause consumption when the agent is behind"
  - "Queue depth grows unboundedly during traffic spikes with no admission control"
  - "Messages time out in the processing pipeline because the agent accepted too many"
---

## Why This Happens

Message queue consumers typically pull messages as fast as the queue delivers them, using a prefetch count or concurrency setting. Under load, if processing is slower than consumption — due to LLM latency, slow tool calls, or database contention — the consumer accumulates a growing backlog of in-flight messages it has acknowledged but not yet processed. This exhausts memory and increases processing latency for each message. Backpressure requires measuring the gap between consumption rate and completion rate, and dynamically reducing the prefetch count or pausing consumption when that gap exceeds a threshold.

## Solution 1: Queue Consumer Metrics

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, List, Optional
from collections import deque
from threading import Lock


class ConsumerState(str, Enum):
    RUNNING = "running"
    THROTTLED = "throttled"
    PAUSED = "paused"
    DRAINING = "draining"


@dataclass
class MessageProcessingRecord:
    message_id: str
    received_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    success: bool = False

    @property
    def queue_wait_ms(self) -> float:
        if self.started_at is None:
            return (time.time() - self.received_at) * 1000
        return (self.started_at - self.received_at) * 1000

    @property
    def processing_ms(self) -> Optional[float]:
        if self.started_at is None or self.completed_at is None:
            return None
        return (self.completed_at - self.started_at) * 1000


class QueueConsumerMetrics:
    """
    Tracks message throughput, in-flight count, and processing latency
    for a queue consumer to enable backpressure decisions.
    """

    def __init__(self, window_seconds: float = 60.0, max_records: int = 10000):
        self._window = window_seconds
        self._records: Deque[MessageProcessingRecord] = deque(maxlen=max_records)
        self._in_flight: dict = {}
        self._lock = Lock()

    def on_received(self, message_id: str) -> None:
        record = MessageProcessingRecord(
            message_id=message_id,
            received_at=time.time(),
        )
        with self._lock:
            self._in_flight[message_id] = record
            self._records.append(record)

    def on_started(self, message_id: str) -> None:
        with self._lock:
            if message_id in self._in_flight:
                self._in_flight[message_id].started_at = time.time()

    def on_completed(self, message_id: str, success: bool = True) -> None:
        with self._lock:
            record = self._in_flight.pop(message_id, None)
            if record:
                record.completed_at = time.time()
                record.success = success

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def throughput_per_second(self, window_seconds: Optional[float] = None) -> float:
        window = window_seconds or self._window
        cutoff = time.time() - window
        with self._lock:
            completed = sum(
                1 for r in self._records
                if r.completed_at and r.completed_at >= cutoff
            )
        return round(completed / window, 2)

    def avg_processing_ms(self) -> float:
        cutoff = time.time() - self._window
        with self._lock:
            times = [
                r.processing_ms for r in self._records
                if r.processing_ms and r.completed_at and r.completed_at >= cutoff
            ]
        return round(sum(times) / len(times), 2) if times else 0.0

    def summary(self) -> dict:
        return {
            "in_flight": self.in_flight_count(),
            "throughput_per_sec": self.throughput_per_second(),
            "avg_processing_ms": self.avg_processing_ms(),
        }
```

## Solution 2: Backpressure Policy

```python
from dataclasses import dataclass


@dataclass
class BackpressurePolicy:
    max_in_flight: int = 50             # pause when in-flight exceeds this
    throttle_in_flight: int = 30        # throttle when in-flight exceeds this
    min_throughput_per_sec: float = 0.5 # pause if throughput drops below this
    max_queue_wait_ms: float = 30_000.0 # pause if avg wait exceeds this (ms)
    recovery_in_flight: int = 20        # resume from pause when in-flight drops to this
    throttle_delay_seconds: float = 0.5 # delay between messages when throttled
    max_prefetch: int = 10              # maximum prefetch count
    min_prefetch: int = 1               # minimum prefetch count when throttled
```

## Solution 3: Backpressure Controller

```python
import asyncio
import time
from typing import Callable, Optional


class BackpressureController:
    """
    Evaluates consumer metrics against the backpressure policy and
    determines whether to run, throttle, or pause consumption.
    Provides an async gate that consumers await before processing.
    """

    def __init__(
        self,
        metrics: QueueConsumerMetrics,
        policy: BackpressurePolicy,
        state_change_fn: Optional[Callable[[ConsumerState], None]] = None,
    ):
        self._metrics = metrics
        self._policy = policy
        self._state_change_fn = state_change_fn
        self._state = ConsumerState.RUNNING
        self._state_changed_at = time.time()
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # start unpaused
        self._prefetch = policy.max_prefetch

    async def gate(self) -> None:
        """
        Await this before processing each message.
        Blocks when paused; introduces delay when throttled.
        """
        await self._pause_event.wait()
        if self._state == ConsumerState.THROTTLED:
            await asyncio.sleep(self._policy.throttle_delay_seconds)

    def evaluate(self) -> ConsumerState:
        in_flight = self._metrics.in_flight_count()
        throughput = self._metrics.throughput_per_second(window_seconds=30.0)
        avg_processing = self._metrics.avg_processing_ms()

        old_state = self._state

        if (
            in_flight >= self._policy.max_in_flight
            or (throughput < self._policy.min_throughput_per_sec and in_flight > 5)
        ):
            new_state = ConsumerState.PAUSED
            self._pause_event.clear()
            self._prefetch = self._policy.min_prefetch

        elif in_flight >= self._policy.throttle_in_flight:
            new_state = ConsumerState.THROTTLED
            self._pause_event.set()
            self._prefetch = max(
                self._policy.min_prefetch,
                self._policy.max_prefetch // 2,
            )

        elif (
            self._state == ConsumerState.PAUSED
            and in_flight <= self._policy.recovery_in_flight
        ):
            new_state = ConsumerState.RUNNING
            self._pause_event.set()
            self._prefetch = self._policy.max_prefetch

        else:
            new_state = self._state if self._state != ConsumerState.PAUSED else ConsumerState.RUNNING
            if new_state == ConsumerState.RUNNING:
                self._pause_event.set()
                self._prefetch = self._policy.max_prefetch

        if new_state != old_state:
            self._state = new_state
            self._state_changed_at = time.time()
            if self._state_change_fn:
                self._state_change_fn(new_state)

        return new_state

    @property
    def current_state(self) -> ConsumerState:
        return self._state

    @property
    def recommended_prefetch(self) -> int:
        return self._prefetch
```

## Solution 4: Backpressure-Aware Message Consumer

```python
import asyncio
from typing import Any, AsyncGenerator, Callable, Optional


class BackpressureAwareMessageConsumer:
    """
    Wraps a message queue consumer with backpressure-aware flow control.
    Evaluates policy before each message and adjusts consumption rate.
    """

    def __init__(
        self,
        controller: BackpressureController,
        metrics: QueueConsumerMetrics,
        poll_interval_seconds: float = 5.0,
    ):
        self._controller = controller
        self._metrics = metrics
        self._poll_interval = poll_interval_seconds
        self._running = False

    async def _evaluation_loop(self) -> None:
        """Background loop that evaluates backpressure policy periodically."""
        while self._running:
            self._controller.evaluate()
            await asyncio.sleep(self._poll_interval)

    async def consume(
        self,
        message_source: AsyncGenerator,
        handler: Callable[[Any], Any],
    ) -> None:
        self._running = True
        eval_task = asyncio.create_task(self._evaluation_loop())

        try:
            async for message in message_source:
                msg_id = getattr(message, "id", str(id(message)))
                self._metrics.on_received(msg_id)

                await self._controller.gate()

                self._metrics.on_started(msg_id)
                try:
                    await handler(message)
                    self._metrics.on_completed(msg_id, success=True)
                except Exception as exc:
                    self._metrics.on_completed(msg_id, success=False)
                    raise
        finally:
            self._running = False
            eval_task.cancel()
```

## Solution 5: Backpressure Event Logger

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import List


class BackpressureEventLogger:
    """
    Logs backpressure state transitions and metrics snapshots
    for post-incident analysis of overload events.
    """

    def __init__(self, path: str = "/tmp/backpressure_events.jsonl"):
        self._path = Path(path)
        self._lock = Lock()

    def on_state_change(self, new_state: ConsumerState, metrics: QueueConsumerMetrics) -> None:
        event = {
            "ts": time.time(),
            "event": "state_change",
            "new_state": new_state.value,
            "metrics": metrics.summary(),
        }
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(event) + "\n")

    def recent_events(self, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        events = []
        if not self._path.exists():
            return events
        with self._lock:
            for line in self._path.read_text().splitlines():
                try:
                    e = json.loads(line)
                    if e["ts"] >= cutoff:
                        events.append(e)
                except (json.JSONDecodeError, KeyError):
                    continue
        return events

    def pause_count(self, window_seconds: float = 3600.0) -> int:
        return sum(
            1 for e in self.recent_events(window_seconds)
            if e.get("new_state") == "paused"
        )
```

## Solution 6: Backpressure Dashboard

```python
import time


class BackpressureDashboard:
    """
    Operational view of consumer health: state, in-flight count,
    throughput, and recent backpressure events.
    """

    def __init__(
        self,
        controller: BackpressureController,
        metrics: QueueConsumerMetrics,
        logger: BackpressureEventLogger,
    ):
        self._controller = controller
        self._metrics = metrics
        self._logger = logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "consumer_state": self._controller.current_state.value,
            "recommended_prefetch": self._controller.recommended_prefetch,
            "metrics": self._metrics.summary(),
            "pause_events_1h": self._logger.pause_count(3600.0),
            "recent_state_changes": self._logger.recent_events(300.0)[-5:],
        }
```

## Comparison

| Approach | In-Flight Tracking | Throughput Measurement | Dynamic Throttling | Pause/Resume | Event Logging |
|---|---|---|---|---|---|
| QueueConsumerMetrics | Yes | Yes (rolling window) | No | No | No |
| BackpressureController | Via metrics | Via metrics | Yes (3 states) | Yes (asyncio.Event) | No |
| BackpressureAwareMessageConsumer | Via metrics | Via metrics | Via controller | Via controller | No |
| BackpressureEventLogger | No | No | No | No | Yes (JSONL) |
| BackpressureDashboard | No | No | No | No | Yes |

**Best for production**: Set `max_in_flight` to 2× the number of concurrent async workers — beyond that, messages are queued in memory with no benefit. Trigger the evaluation loop every 5 seconds rather than on every message — per-message evaluation adds latency overhead that defeats the purpose of batching. When the controller transitions to PAUSED, emit a structured log event and alert if pauses exceed 3 per hour — frequent pausing indicates the consumer is systematically undersized for the load and needs more workers or faster processing, not just better backpressure. Use `recommended_prefetch` to update the actual queue prefetch count so the broker itself slows delivery during throttled states.
