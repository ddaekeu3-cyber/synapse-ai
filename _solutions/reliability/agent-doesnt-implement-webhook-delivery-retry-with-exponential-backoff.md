---
title: "Agent Doesn't Implement Webhook Delivery Retry with Exponential Backoff"
description: "Agents that attempt webhook delivery once and discard on failure lose critical notifications: a deployment notification, task completion event, or alert that fails due to a momentary downstream unavailability is dropped with no retry. Implement webhook delivery retry with exponential backoff, per-endpoint failure tracking, and delivery confirmation so no event is permanently lost due to transient recipient unavailability."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-webhook-delivery-retry-with-exponential-backoff
tags: [webhook-retry, exponential-backoff, delivery-guarantee, event-delivery, at-least-once, notification-reliability]
symptoms:
  - "Webhook deliveries that fail are never retried — events are silently dropped"
  - "Downstream service restart during a high-event period causes permanent event loss"
  - "No delivery confirmation tracking — agent cannot tell if an event was received"
  - "All webhook failures treated identically regardless of HTTP status code"
  - "No per-endpoint backoff — a failing endpoint is hammered until it recovers or gives up"
---

## Why This Happens

Fire-and-forget webhook delivery is the path of least resistance. Adding retry requires persistent state: which events are pending, how many times each has been attempted, and when the next attempt should occur. Without this state, every failure is permanent. Exponential backoff is necessary because a recipient that is overloaded will be made worse by immediate retry — each retry attempt must wait longer than the previous one, with jitter to prevent synchronized retries from multiple senders.

## Solution 1: Webhook Delivery Event

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    EXHAUSTED = "exhausted"     # all retries exhausted
    CANCELLED = "cancelled"


@dataclass
class DeliveryAttempt:
    attempt_number: int
    attempted_at: float
    http_status: Optional[int]
    latency_ms: float
    error_message: str = ""
    success: bool = False


@dataclass
class WebhookDeliveryEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    endpoint_url: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    event_type: str = ""
    created_at: float = field(default_factory=time.time)
    next_attempt_at: float = field(default_factory=time.time)
    attempts: List[DeliveryAttempt] = field(default_factory=list)
    status: DeliveryStatus = DeliveryStatus.PENDING
    max_attempts: int = 8
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def last_attempt(self) -> Optional[DeliveryAttempt]:
        return self.attempts[-1] if self.attempts else None
```

## Solution 2: Backoff Calculator

```python
import random
from dataclasses import dataclass


@dataclass
class BackoffConfig:
    base_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 3600.0   # cap at 1 hour
    jitter_fraction: float = 0.25       # ±25% jitter
    retryable_status_codes: tuple = (429, 500, 502, 503, 504, 529)


class ExponentialBackoffCalculator:
    """
    Computes the next retry delay using exponential backoff with full jitter.
    Non-retryable status codes (400, 401, 404) do not get retried.
    """

    def __init__(self, config: BackoffConfig):
        self._config = config

    def next_delay(self, attempt_number: int) -> float:
        cfg = self._config
        base = cfg.base_delay_seconds * (cfg.multiplier ** (attempt_number - 1))
        capped = min(base, cfg.max_delay_seconds)
        jitter = capped * cfg.jitter_fraction * (2 * random.random() - 1)
        return max(0.0, capped + jitter)

    def is_retryable(self, http_status: Optional[int], error_message: str = "") -> bool:
        if http_status is None:
            return True  # network error — always retry
        if http_status in self._config.retryable_status_codes:
            return True
        return False
```

## Solution 3: Webhook Retry Queue

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class WebhookRetryQueue:
    """
    Persistent queue of webhook delivery events awaiting retry.
    Backed by a JSON file for durability across restarts.
    """

    def __init__(self, path: str = "/tmp/webhook_retry_queue.json"):
        self._path = Path(path)
        self._lock = Lock()
        self._events: Dict[str, WebhookDeliveryEvent] = {}
        self._load()

    def _serialize(self, event: WebhookDeliveryEvent) -> dict:
        return {
            "event_id": event.event_id,
            "endpoint_url": event.endpoint_url,
            "payload": event.payload,
            "event_type": event.event_type,
            "created_at": event.created_at,
            "next_attempt_at": event.next_attempt_at,
            "attempts": [
                {
                    "attempt_number": a.attempt_number,
                    "attempted_at": a.attempted_at,
                    "http_status": a.http_status,
                    "latency_ms": a.latency_ms,
                    "error_message": a.error_message,
                    "success": a.success,
                }
                for a in event.attempts
            ],
            "status": event.status.value,
            "max_attempts": event.max_attempts,
            "metadata": event.metadata,
        }

    def _deserialize(self, data: dict) -> WebhookDeliveryEvent:
        event = WebhookDeliveryEvent(
            event_id=data["event_id"],
            endpoint_url=data["endpoint_url"],
            payload=data["payload"],
            event_type=data.get("event_type", ""),
            created_at=data["created_at"],
            next_attempt_at=data["next_attempt_at"],
            status=DeliveryStatus(data["status"]),
            max_attempts=data.get("max_attempts", 8),
            metadata=data.get("metadata", {}),
        )
        event.attempts = [DeliveryAttempt(**a) for a in data.get("attempts", [])]
        return event

    def _load(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text())
                self._events = {eid: self._deserialize(d) for eid, d in raw.items()}
            except Exception:
                self._events = {}

    def _flush(self) -> None:
        self._path.write_text(json.dumps({
            eid: self._serialize(e) for eid, e in self._events.items()
        }, indent=2))

    def enqueue(self, event: WebhookDeliveryEvent) -> None:
        with self._lock:
            self._events[event.event_id] = event
            self._flush()

    def due_events(self) -> List[WebhookDeliveryEvent]:
        now = time.time()
        with self._lock:
            return [
                e for e in self._events.values()
                if e.status == DeliveryStatus.PENDING and e.next_attempt_at <= now
            ]

    def update(self, event: WebhookDeliveryEvent) -> None:
        with self._lock:
            self._events[event.event_id] = event
            self._flush()

    def remove_delivered(self) -> int:
        with self._lock:
            done = [
                eid for eid, e in self._events.items()
                if e.status in (DeliveryStatus.DELIVERED, DeliveryStatus.EXHAUSTED, DeliveryStatus.CANCELLED)
            ]
            for eid in done:
                del self._events[eid]
            if done:
                self._flush()
            return len(done)

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._events.values() if e.status == DeliveryStatus.PENDING)
```

## Solution 4: Webhook Retry Dispatcher

```python
import asyncio
import time
from typing import Any, Callable


class WebhookRetryDispatcher:
    """
    Processes due webhook delivery events, attempts delivery, and
    schedules retries with exponential backoff on failure.
    """

    def __init__(
        self,
        queue: WebhookRetryQueue,
        http_client,
        backoff_calculator: ExponentialBackoffCalculator,
        timeout_seconds: float = 10.0,
    ):
        self._queue = queue
        self._http = http_client
        self._backoff = backoff_calculator
        self._timeout = timeout_seconds
        self._delivered_count = 0
        self._exhausted_count = 0
        self._running = False

    async def _attempt_delivery(self, event: WebhookDeliveryEvent) -> DeliveryAttempt:
        start = time.time()
        attempt_num = event.attempt_count + 1
        try:
            response = await asyncio.wait_for(
                self._http.post(
                    event.endpoint_url,
                    json=event.payload,
                    headers={"Content-Type": "application/json", "X-Event-Type": event.event_type},
                ),
                timeout=self._timeout,
            )
            latency_ms = round((time.time() - start) * 1000, 2)
            success = 200 <= response.status_code < 300
            return DeliveryAttempt(
                attempt_number=attempt_num,
                attempted_at=time.time(),
                http_status=response.status_code,
                latency_ms=latency_ms,
                success=success,
            )
        except Exception as exc:
            latency_ms = round((time.time() - start) * 1000, 2)
            return DeliveryAttempt(
                attempt_number=attempt_num,
                attempted_at=time.time(),
                http_status=None,
                latency_ms=latency_ms,
                error_message=str(exc),
                success=False,
            )

    async def process_due(self) -> dict:
        due = self._queue.due_events()
        results = {"attempted": len(due), "delivered": 0, "retried": 0, "exhausted": 0}

        for event in due:
            attempt = await self._attempt_delivery(event)
            event.attempts.append(attempt)

            if attempt.success:
                event.status = DeliveryStatus.DELIVERED
                self._delivered_count += 1
                results["delivered"] += 1
            elif event.attempt_count >= event.max_attempts:
                event.status = DeliveryStatus.EXHAUSTED
                self._exhausted_count += 1
                results["exhausted"] += 1
            elif self._backoff.is_retryable(attempt.http_status, attempt.error_message):
                delay = self._backoff.next_delay(event.attempt_count)
                event.next_attempt_at = time.time() + delay
                results["retried"] += 1
            else:
                event.status = DeliveryStatus.EXHAUSTED
                self._exhausted_count += 1
                results["exhausted"] += 1

            self._queue.update(event)

        return results

    async def run_loop(self, poll_interval_seconds: float = 5.0) -> None:
        self._running = True
        while self._running:
            await self.process_due()
            self._queue.remove_delivered()
            await asyncio.sleep(poll_interval_seconds)

    def stop(self) -> None:
        self._running = False

    def stats(self) -> dict:
        return {
            "delivered_count": self._delivered_count,
            "exhausted_count": self._exhausted_count,
            "pending_count": self._queue.pending_count(),
        }
```

## Solution 5: Per-Endpoint Health Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, Tuple


class EndpointHealthTracker:
    """
    Tracks per-endpoint delivery success rates to identify persistently
    failing endpoints that need manual intervention.
    """

    def __init__(self, window_seconds: int = 3600):
        self._window = window_seconds
        self._events: Dict[str, Deque[Tuple[float, bool]]] = {}
        self._lock = Lock()

    def record(self, endpoint_url: str, success: bool) -> None:
        with self._lock:
            if endpoint_url not in self._events:
                self._events[endpoint_url] = deque()
            self._events[endpoint_url].append((time.time(), success))

    def success_rate(self, endpoint_url: str) -> Tuple[float, int]:
        cutoff = time.time() - self._window
        with self._lock:
            events = [(ts, ok) for ts, ok in self._events.get(endpoint_url, []) if ts >= cutoff]
        if not events:
            return 1.0, 0
        successes = sum(1 for _, ok in events if ok)
        return successes / len(events), len(events)

    def failing_endpoints(self, threshold: float = 0.5) -> list:
        with self._lock:
            endpoints = list(self._events.keys())
        result = []
        for url in endpoints:
            rate, count = self.success_rate(url)
            if count >= 5 and rate < threshold:
                result.append({"endpoint": url, "success_rate": round(rate, 4), "attempts": count})
        return result
```

## Solution 6: Webhook Retry Dashboard

```python
import time


class WebhookRetryDashboard:
    """
    Combines retry dispatcher stats, queue status, and endpoint
    health into a single operational view.
    """

    def __init__(
        self,
        dispatcher: WebhookRetryDispatcher,
        endpoint_health: EndpointHealthTracker,
    ):
        self._dispatcher = dispatcher
        self._health = endpoint_health

    def render(self) -> dict:
        stats = self._dispatcher.stats()
        failing = self._health.failing_endpoints()

        return {
            "generated_at": time.time(),
            "dispatcher_stats": stats,
            "failing_endpoints": failing,
            "alert": stats["exhausted_count"] > 0 or len(failing) > 0,
        }
```

## Comparison

| Approach | Persistent Queue | Exponential Backoff | Retryable Detection | Endpoint Health | Dashboard |
|---|---|---|---|---|---|
| WebhookRetryQueue | Yes (JSON file) | No | No | No | No |
| ExponentialBackoffCalculator | No | Yes (with jitter) | Yes (status codes) | No | No |
| WebhookRetryDispatcher | Via queue | Via backoff | Via backoff | No | No |
| EndpointHealthTracker | No | No | No | Yes | No |
| WebhookRetryDashboard | No | No | No | Via tracker | Yes |

**Best for production**: Use Redis as the retry queue backend for multi-instance deployments — a file-based queue on one instance is invisible to others. Set `max_attempts=8` with base delay of 1 second and multiplier of 2: this gives retry delays of 1s, 2s, 4s, 8s, 16s, 32s, 64s, 128s (≈35 minutes total). Do not retry on 400, 401, 403, or 404 — these indicate a permanent problem (bad payload or authentication failure) that will not self-resolve. Alert on `exhausted_count > 0`: every exhausted event is a permanently lost notification that may need manual redelivery.
