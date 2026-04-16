---
title: "Agent Doesn't Implement Poison Pill Detection for Message Queues"
description: "Agents consuming work from message queues that repeatedly fail to process a specific message will dequeue it, fail, requeue it, dequeue it again, and loop indefinitely — consuming worker capacity and blocking other messages. Implement poison pill detection that tracks per-message failure counts, quarantines messages exceeding the retry threshold, and alerts on quarantine events for manual investigation."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-poison-pill-detection-for-message-queues
tags: [poison-pill, message-queue, dead-letter, retry-limit, quarantine, queue-reliability]
symptoms:
  - "Same message dequeued and failed 50 times — blocking the queue for hours"
  - "Worker restarts cause the same failing message to be reprocessed endlessly"
  - "No dead-letter queue — failed messages accumulate in the main queue"
  - "Queue depth never drains because one bad message keeps re-entering"
  - "No alert when a message has failed more than N times"
---

## Why This Happens

Message queues with visibility timeouts (SQS, RabbitMQ, Redis Streams) redeliver messages that are not explicitly acknowledged. A message that causes a processing exception is never acked, becomes visible again after the timeout, and is dequeued by the next available worker. Without a retry count check, this cycle repeats indefinitely. Poison pill detection requires tracking delivery attempts per message ID, comparing against a configured threshold, and routing expired messages to a dead-letter queue or quarantine store rather than back to the main queue.

## Solution 1: Message Envelope

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class QueueMessage:
    message_id: str
    payload: Dict[str, Any]
    queue_name: str
    enqueued_at: float = field(default_factory=time.time)
    delivery_attempt: int = 1
    last_error: Optional[str] = None
    source_message_id: Optional[str] = None   # original ID if requeued

    @classmethod
    def create(cls, payload: Dict[str, Any], queue_name: str) -> "QueueMessage":
        return cls(
            message_id=uuid.uuid4().hex,
            payload=payload,
            queue_name=queue_name,
        )
```

## Solution 2: Delivery Attempt Tracker

```python
import time
from threading import Lock
from typing import Dict, Optional, Tuple


class DeliveryAttemptTracker:
    """
    Tracks how many times each message has been attempted.
    Backed by an in-memory dict; replace with Redis for multi-worker deployments.
    TTL cleanup prevents unbounded growth from messages that eventually succeed.
    """

    def __init__(self, max_age_seconds: float = 86400.0):
        self._attempts: Dict[str, Tuple[int, float]] = {}  # id -> (count, last_seen)
        self._max_age = max_age_seconds
        self._lock = Lock()

    def increment(self, message_id: str) -> int:
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            count, _ = self._attempts.get(message_id, (0, now))
            count += 1
            self._attempts[message_id] = (count, now)
            return count

    def get(self, message_id: str) -> int:
        with self._lock:
            return self._attempts.get(message_id, (0, 0))[0]

    def clear(self, message_id: str) -> None:
        with self._lock:
            self._attempts.pop(message_id, None)

    def _purge_expired(self, now: float) -> None:
        cutoff = now - self._max_age
        expired = [mid for mid, (_, ts) in self._attempts.items() if ts < cutoff]
        for mid in expired:
            del self._attempts[mid]

    def stats(self) -> dict:
        with self._lock:
            return {"tracked_messages": len(self._attempts)}
```

## Solution 3: Poison Pill Detector

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MessageDisposition(str, Enum):
    PROCESS = "process"
    QUARANTINE = "quarantine"
    SKIP = "skip"


@dataclass
class PoisonCheckResult:
    disposition: MessageDisposition
    attempt_count: int
    max_attempts: int
    reason: str = ""


class PoisonPillDetector:
    """
    Checks each incoming message against the delivery attempt tracker.
    Returns a disposition: PROCESS if under the threshold, QUARANTINE if over.
    """

    def __init__(
        self,
        tracker: DeliveryAttemptTracker,
        max_attempts: int = 5,
    ):
        self._tracker = tracker
        self._max = max_attempts

    def check(self, message: QueueMessage) -> PoisonCheckResult:
        count = self._tracker.increment(message.message_id)
        if count > self._max:
            return PoisonCheckResult(
                disposition=MessageDisposition.QUARANTINE,
                attempt_count=count,
                max_attempts=self._max,
                reason=f"exceeded max attempts ({count}/{self._max})",
            )
        return PoisonCheckResult(
            disposition=MessageDisposition.PROCESS,
            attempt_count=count,
            max_attempts=self._max,
        )
```

## Solution 4: Quarantine Store

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class QuarantinedMessage:
    message: QueueMessage
    attempt_count: int
    quarantined_at: float = field(default_factory=time.time)
    last_error: Optional[str] = None


class MessageQuarantineStore:
    """
    Stores quarantined messages for manual inspection and replay.
    Provides summary statistics for alert generation.
    """

    def __init__(self, max_entries: int = 10000):
        self._max = max_entries
        self._store: List[QuarantinedMessage] = []

    def quarantine(
        self,
        message: QueueMessage,
        attempt_count: int,
        error: Optional[str] = None,
    ) -> QuarantinedMessage:
        if len(self._store) >= self._max:
            self._store.pop(0)
        entry = QuarantinedMessage(
            message=message,
            attempt_count=attempt_count,
            last_error=error or message.last_error,
        )
        self._store.append(entry)
        return entry

    def get_all(self) -> List[QuarantinedMessage]:
        return list(self._store)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [q for q in self._store if q.quarantined_at >= cutoff]
        by_queue: Dict[str, int] = {}
        for q in recent:
            name = q.message.queue_name
            by_queue[name] = by_queue.get(name, 0) + 1
        return {
            "window_seconds": window_seconds,
            "quarantined": len(recent),
            "by_queue": by_queue,
            "total_quarantined": len(self._store),
        }
```

## Solution 5: Poison-Pill-Aware Queue Consumer

```python
import asyncio
from typing import Any, Callable, Optional


class PoisonPillAwareConsumer:
    """
    Wraps message processing with poison pill detection.
    On QUARANTINE disposition, moves the message to the quarantine store
    and acks (removes) it from the main queue to unblock processing.
    """

    def __init__(
        self,
        detector: PoisonPillDetector,
        quarantine_store: MessageQuarantineStore,
        on_quarantine: Optional[Callable[[QuarantinedMessage], None]] = None,
    ):
        self._detector = detector
        self._quarantine = quarantine_store
        self._on_quarantine = on_quarantine
        self._processed = 0
        self._quarantined = 0
        self._failed = 0

    async def consume(
        self,
        message: QueueMessage,
        handler: Callable[[QueueMessage], Any],
        ack_fn: Callable,
        nack_fn: Callable,
    ) -> None:
        check = self._detector.check(message)

        if check.disposition == MessageDisposition.QUARANTINE:
            entry = self._quarantine.quarantine(
                message, check.attempt_count, message.last_error
            )
            self._detector._tracker.clear(message.message_id)
            self._quarantined += 1
            if self._on_quarantine:
                self._on_quarantine(entry)
            await ack_fn(message)   # remove from main queue
            return

        try:
            await handler(message)
            self._detector._tracker.clear(message.message_id)
            self._processed += 1
            await ack_fn(message)
        except Exception as exc:
            message.last_error = str(exc)[:300]
            self._failed += 1
            await nack_fn(message)

    def stats(self) -> dict:
        total = self._processed + self._quarantined + self._failed
        return {
            "total": total,
            "processed": self._processed,
            "quarantined": self._quarantined,
            "failed_pending_retry": self._failed,
            "quarantine_rate": round(self._quarantined / max(total, 1), 4),
        }
```

## Solution 6: Poison Pill Dashboard

```python
import time


class PoisonPillDashboard:
    def __init__(
        self,
        consumer: PoisonPillAwareConsumer,
        quarantine_store: MessageQuarantineStore,
        tracker: DeliveryAttemptTracker,
    ):
        self._consumer = consumer
        self._quarantine = quarantine_store
        self._tracker = tracker

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "consumer": self._consumer.stats(),
            "quarantine": self._quarantine.summary(window_seconds=3600.0),
            "tracker": self._tracker.stats(),
        }
```

## Comparison

| Approach | Attempt Tracking | Quarantine Routing | Unblocks Queue | Alert Hook | Dashboard |
|---|---|---|---|---|---|
| DeliveryAttemptTracker | Yes (per message ID) | No | No | No | No |
| PoisonPillDetector | Via tracker | No | No | No | No |
| MessageQuarantineStore | No | Yes | No | No | No |
| PoisonPillAwareConsumer | Via detector | Via store | Yes (acks) | Yes (callback) | No |
| PoisonPillDashboard | No | No | No | No | Yes |

**Best for production**: Set `max_attempts=5` as the default and lower it to 3 for messages with financial side effects. Use Redis as the `DeliveryAttemptTracker` backend in multi-worker deployments — all workers share the same counter, preventing a message from getting 5 attempts per worker. Wire `on_quarantine` to PagerDuty or Slack so engineers are notified immediately when a message is quarantined; quarantined messages represent data loss unless manually inspected and replayed. Emit `quarantine_rate` to your metrics system and alert when it exceeds 1% of throughput.
