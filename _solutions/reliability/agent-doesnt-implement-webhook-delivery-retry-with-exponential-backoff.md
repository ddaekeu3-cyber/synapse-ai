---
title: "Agent Doesn't Implement Webhook Delivery Retry with Exponential Backoff"
description: "Agents that emit webhook notifications with a single fire-and-forget HTTP request lose events when the receiving endpoint is temporarily unavailable — a network blip, a deploying consumer, or a momentarily overloaded receiver causes silent event loss. Implement webhook delivery with exponential backoff retry, per-endpoint delivery tracking, a dead-letter queue for permanently failed deliveries, and signature verification headers."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-webhook-delivery-retry-with-exponential-backoff
tags: [webhook-retry, event-delivery, exponential-backoff, dead-letter-queue, webhook-signature, delivery-guarantee]
symptoms:
  - "Webhook events dropped silently when receiver returns 5xx or times out"
  - "No retry on temporary receiver unavailability — single attempt only"
  - "No signature on outgoing webhooks — receiver cannot verify authenticity"
  - "No dead-letter queue — permanently failed events are lost with no record"
  - "All endpoints share a single retry policy regardless of their reliability history"
---

## Why This Happens

HTTP webhook delivery is inherently unreliable: receivers go down, deployments cause brief 503s, and network partitions are transient. A single attempt with no retry means any temporary unavailability during the delivery window causes permanent event loss. Retry with exponential backoff gives the receiver time to recover before the next attempt. A dead-letter queue captures events that exhaust all retry attempts, enabling manual replay or alerting. HMAC-SHA256 signatures allow receivers to reject unauthenticated deliveries, preventing spoofing.

## Solution 1: Webhook Event

```python
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class WebhookEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    endpoint_url: str = ""
    created_at: float = field(default_factory=time.time)
    attempt_count: int = 0
    last_attempt_at: Optional[float] = None
    last_status_code: Optional[int] = None
    last_error: str = ""
    delivered: bool = False
    dead_lettered: bool = False

    def to_json(self) -> str:
        return json.dumps({
            "id": self.event_id,
            "type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        })

    def sign(self, secret: str) -> str:
        """HMAC-SHA256 signature over the canonical JSON payload."""
        body = self.to_json().encode()
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
```

## Solution 2: Webhook Retry Policy

```python
from dataclasses import dataclass
import math


@dataclass
class WebhookRetryPolicy:
    max_attempts: int = 7
    base_delay_s: float = 1.0
    max_delay_s: float = 3600.0   # cap at 1 hour
    jitter_fraction: float = 0.25
    retry_on_status: tuple = (408, 429, 500, 502, 503, 504)
    timeout_s: float = 10.0

    def delay_for_attempt(self, attempt: int) -> float:
        """Exponential backoff with full jitter."""
        import random
        exp_delay = min(self.base_delay_s * (2 ** attempt), self.max_delay_s)
        jitter = random.uniform(0, exp_delay * self.jitter_fraction)
        return round(exp_delay + jitter, 2)

    def should_retry(self, status_code: Optional[int], attempt: int) -> bool:
        if attempt >= self.max_attempts:
            return False
        if status_code is None:
            return True   # network error — always retry
        return status_code in self.retry_on_status
```

## Solution 3: Webhook Delivery Tracker

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class WebhookDeliveryTracker:
    """
    Tracks delivery state for pending and completed webhook events.
    Persists attempt history for each event for audit and replay.
    """

    def __init__(self, max_events: int = 100000):
        self._events: Dict[str, WebhookEvent] = {}
        self._dead_letter: List[WebhookEvent] = []
        self._lock = Lock()
        self._max = max_events
        self._delivered_count = 0

    def register(self, event: WebhookEvent) -> None:
        with self._lock:
            if len(self._events) >= self._max:
                # Evict oldest delivered events
                delivered = [eid for eid, e in self._events.items() if e.delivered]
                for eid in delivered[:len(delivered) // 2]:
                    del self._events[eid]
            self._events[event.event_id] = event

    def record_attempt(
        self,
        event_id: str,
        status_code: Optional[int],
        error: str = "",
        delivered: bool = False,
    ) -> None:
        with self._lock:
            event = self._events.get(event_id)
            if not event:
                return
            event.attempt_count += 1
            event.last_attempt_at = time.time()
            event.last_status_code = status_code
            event.last_error = error
            event.delivered = delivered

    def dead_letter(self, event_id: str) -> None:
        with self._lock:
            event = self._events.get(event_id)
            if event:
                event.dead_lettered = True
                self._dead_letter.append(event)

    def pending_events(self) -> List[WebhookEvent]:
        with self._lock:
            return [
                e for e in self._events.values()
                if not e.delivered and not e.dead_lettered
            ]

    def stats(self) -> dict:
        with self._lock:
            total = len(self._events)
            delivered = sum(1 for e in self._events.values() if e.delivered)
            return {
                "total_registered": total,
                "delivered": delivered,
                "pending": sum(1 for e in self._events.values() if not e.delivered and not e.dead_lettered),
                "dead_lettered": len(self._dead_letter),
                "delivery_rate": round(delivered / max(total, 1), 4),
            }
```

## Solution 4: Webhook Dispatcher

```python
import asyncio
import time
from typing import Callable, Dict, Optional


class WebhookDispatcher:
    """
    Delivers webhook events with exponential backoff retry.
    Signs each delivery with HMAC-SHA256.
    Moves permanently failed events to the dead-letter queue.
    """

    def __init__(
        self,
        tracker: WebhookDeliveryTracker,
        policy: WebhookRetryPolicy,
        http_post_fn: Callable,     # async (url, body, headers, timeout) -> (status_code, error)
        signing_secret: str = "",
        endpoint_secrets: Optional[Dict[str, str]] = None,
    ):
        self._tracker = tracker
        self._policy = policy
        self._http_post = http_post_fn
        self._default_secret = signing_secret
        self._endpoint_secrets = endpoint_secrets or {}

    def _secret_for(self, url: str) -> str:
        return self._endpoint_secrets.get(url, self._default_secret)

    async def deliver(self, event: WebhookEvent) -> bool:
        """Returns True if delivered successfully."""
        secret = self._secret_for(event.endpoint_url)
        body = event.to_json()
        sig = event.sign(secret) if secret else ""

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Event-Id": event.event_id,
            "X-Webhook-Event-Type": event.event_type,
            "X-Webhook-Timestamp": str(int(event.created_at)),
        }
        if sig:
            headers["X-Webhook-Signature"] = f"sha256={sig}"

        for attempt in range(self._policy.max_attempts):
            try:
                status_code, error = await self._http_post(
                    event.endpoint_url, body, headers, self._policy.timeout_s
                )
            except Exception as exc:
                status_code, error = None, str(exc)[:200]

            delivered = status_code is not None and 200 <= status_code < 300
            self._tracker.record_attempt(event.event_id, status_code, error, delivered)

            if delivered:
                return True

            if not self._policy.should_retry(status_code, attempt + 1):
                break

            delay = self._policy.delay_for_attempt(attempt)
            await asyncio.sleep(delay)

        self._tracker.dead_letter(event.event_id)
        return False
```

## Solution 5: Webhook Queue Runner

```python
import asyncio
import time
from typing import List


class WebhookQueueRunner:
    """
    Processes pending webhook deliveries from the tracker queue.
    Runs as a background task, respecting per-event next-attempt timing.
    """

    def __init__(
        self,
        dispatcher: WebhookDispatcher,
        tracker: WebhookDeliveryTracker,
        policy: WebhookRetryPolicy,
        max_concurrent: int = 10,
    ):
        self._dispatcher = dispatcher
        self._tracker = tracker
        self._policy = policy
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running = False

    async def run_once(self) -> int:
        """Process all pending events. Returns count dispatched."""
        pending = self._tracker.pending_events()
        eligible = [
            e for e in pending
            if e.last_attempt_at is None
            or time.time() - e.last_attempt_at >= self._policy.delay_for_attempt(e.attempt_count)
        ]

        async def dispatch_one(event):
            async with self._semaphore:
                await self._dispatcher.deliver(event)

        tasks = [asyncio.create_task(dispatch_one(e)) for e in eligible]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(eligible)

    async def run_forever(self, poll_interval_s: float = 5.0) -> None:
        self._running = True
        while self._running:
            await self.run_once()
            await asyncio.sleep(poll_interval_s)

    def stop(self) -> None:
        self._running = False
```

## Solution 6: Webhook Delivery Dashboard

```python
import time


class WebhookDeliveryDashboard:
    """
    Combines delivery stats, dead-letter summary, and policy overview
    into a single operational view.
    """

    def __init__(
        self,
        tracker: WebhookDeliveryTracker,
        policy: WebhookRetryPolicy,
    ):
        self._tracker = tracker
        self._policy = policy

    def render(self) -> dict:
        stats = self._tracker.stats()
        dead = self._tracker._dead_letter[-10:]   # last 10 dead-letter events
        return {
            "generated_at": time.time(),
            "delivery_stats": stats,
            "retry_policy": {
                "max_attempts": self._policy.max_attempts,
                "base_delay_s": self._policy.base_delay_s,
                "max_delay_s": self._policy.max_delay_s,
                "retry_on_status": list(self._policy.retry_on_status),
            },
            "recent_dead_letter": [
                {
                    "event_id": e.event_id,
                    "event_type": e.event_type,
                    "endpoint": e.endpoint_url,
                    "attempts": e.attempt_count,
                    "last_status": e.last_status_code,
                    "last_error": e.last_error[:100],
                }
                for e in dead
            ],
        }
```

## Comparison

| Approach | Retry Logic | HMAC Signing | Dead-Letter Queue | Concurrent Dispatch | Delivery Tracking |
|---|---|---|---|---|---|
| WebhookRetryPolicy | Yes (exp + jitter) | No | No | No | No |
| WebhookDispatcher | Yes | Yes (per-endpoint) | Via tracker | No | Via tracker |
| WebhookDeliveryTracker | No | No | Yes | No | Yes |
| WebhookQueueRunner | Via dispatcher | Via dispatcher | Via dispatcher | Yes (semaphore) | Via tracker |
| WebhookDeliveryDashboard | No | No | No | No | Via tracker |

**Best for production**: Use `max_attempts=7` with `base_delay_s=1` — this gives retry windows at ~1s, 2s, 4s, 8s, 16s, 32s, 64s, covering a total of ~2 minutes of receiver downtime. Sign every delivery — receivers that do not validate signatures accept replayed or spoofed events. Alert when `dead_letter` queue depth exceeds 100 events; this indicates a receiver outage that requires manual intervention. Persist the `WebhookDeliveryTracker` state to Redis or a database so pending events survive agent restarts — in-memory tracking loses all pending events on restart.
