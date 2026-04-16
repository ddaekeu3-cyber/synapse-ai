---
title: "Agent doesn't implement poison pill detection and quarantine"
description: "Certain inputs consistently cause the agent to crash, hang, or consume excessive resources. Without poison pill detection, these inputs cycle through retry queues indefinitely, blocking healthy work and degrading the entire system."
difficulty: advanced
category: reliability
tags: [poison-pill, quarantine, circuit-breaker, dead-letter-queue, fault-isolation]
---

## Problem

A "poison pill" is an input that reliably breaks the agent — a malformed JSON payload that triggers a parsing bug, a 200,000-character message that exhausts memory, a specific tool argument combination that causes an infinite loop, or a prompt that causes the model to refuse and loop on retries. Without detection, these inputs:

1. Fill retry queues with work that will never succeed
2. Consume worker threads and API quota on hopeless retries
3. Block healthy requests behind poison ones in ordered queues
4. Eventually bring down the agent process entirely

```python
# BAD: retry everything with no poison pill awareness
async def process(item):
    for attempt in range(5):
        try:
            return await handle(item)
        except Exception:
            await asyncio.sleep(2 ** attempt)
    # Poison pill: retried 5 times, failed each time, then silently dropped
```

## Solution 1: Failure counter with automatic quarantine

Track failure counts per input fingerprint. After N failures, move the item to a quarantine store and stop retrying it.

```python
import asyncio
import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


def input_fingerprint(item: Any) -> str:
    """Stable hash of the input for tracking purposes."""
    serialized = json.dumps(item, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


@dataclass
class FailureRecord:
    fingerprint: str
    failure_count: int = 0
    first_failure: float = field(default_factory=time.time)
    last_failure: float = field(default_factory=time.time)
    last_error: str = ""
    quarantined: bool = False
    quarantine_reason: str = ""


class PoisonPillDetector:
    def __init__(
        self,
        max_failures: int = 3,
        quarantine_ttl: float = 3600.0,   # re-try quarantined items after 1 hour
        failure_window: float = 300.0,    # count failures within 5-minute window
    ):
        self.max_failures = max_failures
        self.quarantine_ttl = quarantine_ttl
        self.failure_window = failure_window
        self._records: dict[str, FailureRecord] = {}
        self._quarantine: dict[str, Any] = {}  # fp -> original item

    def is_quarantined(self, item: Any) -> bool:
        fp = input_fingerprint(item)
        record = self._records.get(fp)
        if not record or not record.quarantined:
            return False
        # Auto-release after TTL
        if time.time() - record.last_failure > self.quarantine_ttl:
            print(f"[{fp}] Quarantine TTL expired — releasing for retry")
            record.quarantined = False
            record.failure_count = 0
            return False
        return True

    def record_failure(self, item: Any, error: Exception) -> bool:
        """Returns True if the item should be quarantined."""
        fp = input_fingerprint(item)
        record = self._records.setdefault(fp, FailureRecord(fingerprint=fp))

        now = time.time()
        # Reset count if outside the failure window
        if now - record.last_failure > self.failure_window:
            record.failure_count = 0

        record.failure_count += 1
        record.last_failure = now
        record.last_error = str(error)

        if record.failure_count >= self.max_failures:
            record.quarantined = True
            record.quarantine_reason = f"Failed {record.failure_count}x: {error}"
            self._quarantine[fp] = item
            print(f"QUARANTINED [{fp}]: {record.quarantine_reason}")
            return True

        return False

    def record_success(self, item: Any):
        fp = input_fingerprint(item)
        if fp in self._records:
            self._records[fp].failure_count = 0
            self._records[fp].quarantined = False

    def quarantine_summary(self) -> list[dict]:
        return [
            {"fp": fp, "item": item, "reason": self._records[fp].quarantine_reason}
            for fp, item in self._quarantine.items()
        ]


# ── Worker using poison pill detection ───────────────────────────────
detector = PoisonPillDetector(max_failures=3)


async def safe_process(item: Any, handler: Callable) -> Any:
    if detector.is_quarantined(item):
        raise RuntimeError(f"Input is quarantined (poison pill)")

    try:
        result = await handler(item)
        detector.record_success(item)
        return result
    except Exception as e:
        quarantined = detector.record_failure(item, e)
        if quarantined:
            raise RuntimeError(f"Item quarantined after repeated failures: {e}") from e
        raise


# ── Demo ──────────────────────────────────────────────────────────────
async def demo():
    async def flaky_handler(item):
        if item.get("broken"):
            raise ValueError("This item always breaks")
        return {"ok": True}

    good = {"id": 1, "text": "normal"}
    bad = {"id": 2, "text": "poison", "broken": True}

    for _ in range(5):
        try:
            await safe_process(good, flaky_handler)
        except Exception as e:
            print(f"Good item error: {e}")

        try:
            await safe_process(bad, flaky_handler)
        except Exception as e:
            print(f"Bad item: {e}")

    print("\nQuarantine:", detector.quarantine_summary())


asyncio.run(demo())
```

## Solution 2: Exponential-backoff with poison pill escalation to dead letter queue

Combine exponential backoff with a dead letter queue (DLQ). After max retries, the item goes to the DLQ with full diagnostic context for manual inspection.

```python
import asyncio
import json
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Awaitable
from pathlib import Path


@dataclass
class DLQEntry:
    item: Any
    fingerprint: str
    attempts: int
    first_attempt: float
    last_attempt: float
    error_history: list[str]
    final_traceback: str


class DeadLetterQueue:
    def __init__(self, path: str = ".dlq.jsonl"):
        self.path = path

    def push(self, entry: DLQEntry):
        with open(self.path, "a") as f:
            f.write(json.dumps(asdict(entry), default=str) + "\n")
        print(f"[DLQ] Added item {entry.fingerprint} after {entry.attempts} attempts")

    def drain(self) -> list[DLQEntry]:
        entries = []
        try:
            with open(self.path) as f:
                for line in f:
                    entries.append(DLQEntry(**json.loads(line)))
        except FileNotFoundError:
            pass
        return entries


import hashlib


def fingerprint(item: Any) -> str:
    return hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:12]


async def process_with_dlq(
    item: Any,
    handler: Callable[[Any], Awaitable[Any]],
    dlq: DeadLetterQueue,
    max_retries: int = 4,
    base_delay: float = 1.0,
) -> Any | None:
    fp = fingerprint(item)
    errors: list[str] = []
    first_attempt = time.time()

    for attempt in range(max_retries + 1):
        try:
            result = await asyncio.wait_for(handler(item), timeout=30.0)
            return result
        except Exception as e:
            errors.append(f"attempt {attempt+1}: {type(e).__name__}: {e}")
            tb = traceback.format_exc()

            if attempt == max_retries:
                dlq.push(DLQEntry(
                    item=item,
                    fingerprint=fp,
                    attempts=attempt + 1,
                    first_attempt=first_attempt,
                    last_attempt=time.time(),
                    error_history=errors,
                    final_traceback=tb,
                ))
                return None

            # Exponential backoff with jitter
            delay = base_delay * (2 ** attempt) + (hash(fp + str(attempt)) % 100) / 100
            print(f"[{fp}] Attempt {attempt+1} failed ({e}), retrying in {delay:.1f}s")
            await asyncio.sleep(delay)

    return None


# ── Demo ──────────────────────────────────────────────────────────────
async def demo():
    dlq = DeadLetterQueue()

    async def bad_handler(item):
        raise ValueError(f"Always fails: {item}")

    await process_with_dlq({"id": 99, "data": "poison"}, bad_handler, dlq, max_retries=2)
    print("DLQ entries:", len(dlq.drain()))


asyncio.run(demo())
```

## Solution 3: Resource-based poison pill detection (memory, timeout, CPU)

Detect poison pills not by error count but by resource consumption — inputs that consistently time out, spike memory, or run excessively long.

```python
import asyncio
import time
import resource as sys_resource
from collections import defaultdict
from typing import Any, Callable, Awaitable


@dataclass
class ResourceProfile:
    fingerprint: str
    samples: list[dict] = field(default_factory=list)
    quarantined: bool = False

    def add_sample(self, elapsed: float, timed_out: bool):
        self.samples.append({"elapsed": elapsed, "timed_out": timed_out, "ts": time.time()})
        if len(self.samples) > 20:
            self.samples.pop(0)

    @property
    def timeout_rate(self) -> float:
        if not self.samples:
            return 0.0
        return sum(1 for s in self.samples if s["timed_out"]) / len(self.samples)

    @property
    def avg_elapsed(self) -> float:
        if not self.samples:
            return 0.0
        return sum(s["elapsed"] for s in self.samples) / len(self.samples)


from dataclasses import dataclass, field
import hashlib
import json


def fp(item: Any) -> str:
    return hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:12]


class ResourcePoisonDetector:
    def __init__(
        self,
        timeout: float = 10.0,
        quarantine_timeout_rate: float = 0.6,  # quarantine if >60% of samples timed out
        min_samples: int = 3,
    ):
        self.timeout = timeout
        self.quarantine_threshold = quarantine_timeout_rate
        self.min_samples = min_samples
        self._profiles: dict[str, ResourceProfile] = {}

    async def run(self, item: Any, handler: Callable) -> Any:
        key = fp(item)
        profile = self._profiles.setdefault(key, ResourceProfile(fingerprint=key))

        if profile.quarantined:
            raise RuntimeError(f"Item {key} is quarantined (resource poison pill)")

        start = time.monotonic()
        timed_out = False
        try:
            result = await asyncio.wait_for(handler(item), timeout=self.timeout)
        except asyncio.TimeoutError:
            timed_out = True
            result = None
        finally:
            elapsed = time.monotonic() - start
            profile.add_sample(elapsed, timed_out)

        if (
            len(profile.samples) >= self.min_samples
            and profile.timeout_rate >= self.quarantine_threshold
        ):
            profile.quarantined = True
            print(
                f"QUARANTINED (resource) [{key}]: "
                f"timeout_rate={profile.timeout_rate:.0%}, avg={profile.avg_elapsed:.1f}s"
            )

        if timed_out:
            raise asyncio.TimeoutError(f"Handler timed out for item {key}")

        return result
```

## Solution 4: Pattern-based poison pill classifier using LLM

Use a lightweight judge model to inspect a failing item and its error, then classify whether it's a transient error (retry) or a structural poison pill (quarantine).

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from typing import Any

client = AsyncAnthropic()

CLASSIFIER_PROMPT = """You are a failure analyst for an AI agent system.

A task item failed processing. Classify whether it is:
- "transient": Temporary issue (network blip, rate limit, model overload) — should retry
- "poison_pill": Structural issue with the input itself — retrying will always fail

Item (truncated to 500 chars):
{item}

Error:
{error}

Respond ONLY with JSON: {{"classification": "transient"|"poison_pill", "reason": "brief explanation"}}"""


async def classify_failure(item: Any, error: Exception) -> dict:
    item_str = json.dumps(item, default=str)[:500]
    error_str = f"{type(error).__name__}: {str(error)[:300]}"

    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": CLASSIFIER_PROMPT.format(item=item_str, error=error_str),
        }],
    )
    text = message.content[0].text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"classification": "transient", "reason": "classifier parse error"}


class SmartRetryHandler:
    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self._quarantined: set[str] = set()

    async def process(self, item: Any, handler) -> Any:
        import hashlib
        key = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:12]

        if key in self._quarantined:
            raise RuntimeError(f"Item {key} is a known poison pill")

        for attempt in range(self.max_retries + 1):
            try:
                return await handler(item)
            except Exception as e:
                if attempt == self.max_retries:
                    raise

                # Ask LLM to classify
                classification = await classify_failure(item, e)
                if classification["classification"] == "poison_pill":
                    self._quarantined.add(key)
                    raise RuntimeError(
                        f"Poison pill detected: {classification['reason']}"
                    ) from e

                delay = 2 ** attempt
                print(f"[{key}] Transient error, retrying in {delay}s: {e}")
                await asyncio.sleep(delay)
```

## Solution 5: Queue-level poison pill isolation with parallel bypass lane

Route suspect items through a low-priority "quarantine lane" that processes them at a fraction of normal throughput, preventing them from blocking the main queue.

```python
import asyncio
from typing import Any, Callable, Awaitable


class BiLaneQueue:
    """
    Two-lane queue: main lane for healthy items, quarantine lane for suspects.
    Quarantine lane is processed at 1/N the rate of the main lane.
    """

    def __init__(self, quarantine_ratio: int = 5):
        self.main: asyncio.Queue[Any] = asyncio.Queue()
        self.quarantine: asyncio.Queue[Any] = asyncio.Queue()
        self.quarantine_ratio = quarantine_ratio  # process 1 quarantine per N main items
        self._main_counter = 0

    async def put(self, item: Any, suspect: bool = False):
        if suspect:
            await self.quarantine.put(item)
        else:
            await self.main.put(item)

    async def get(self) -> tuple[Any, bool]:
        """Returns (item, is_suspect). Prioritizes main lane."""
        self._main_counter += 1

        # Every N main items, try one quarantine item
        if self._main_counter % self.quarantine_ratio == 0:
            try:
                return self.quarantine.get_nowait(), True
            except asyncio.QueueEmpty:
                pass

        # Try main lane first
        try:
            return self.main.get_nowait(), False
        except asyncio.QueueEmpty:
            pass

        # Block on whichever has something
        done, _ = await asyncio.wait(
            [
                asyncio.ensure_future(self.main.get()),
                asyncio.ensure_future(self.quarantine.get()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        task = done.pop()
        return task.result(), False


async def bi_lane_worker(queue: BiLaneQueue, handler: Callable):
    while True:
        item, is_suspect = await queue.get()
        label = "[SUSPECT]" if is_suspect else "[MAIN]"
        try:
            result = await asyncio.wait_for(handler(item), timeout=5.0 if is_suspect else 30.0)
            print(f"{label} OK: {item}")
        except Exception as e:
            print(f"{label} FAILED: {item} → {e}")
```

## Solution 6: Adaptive quarantine with automatic graduated re-admission

Instead of permanent quarantine, use graduated re-admission: quarantined items are retried with progressively longer cooldowns. If they succeed after the cooldown, they exit quarantine. If they keep failing, the cooldown doubles.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
import hashlib, json


@dataclass
class QuarantineEntry:
    fingerprint: str
    retry_after: float       # monotonic timestamp
    cooldown: float          # current cooldown in seconds
    failure_count: int = 0
    admission_attempts: int = 0

    def schedule_next_retry(self, success: bool):
        if success:
            # Halve cooldown on success
            self.cooldown = max(60.0, self.cooldown / 2)
            self.failure_count = 0
        else:
            # Double cooldown on failure (max 24h)
            self.cooldown = min(86400.0, self.cooldown * 2)
            self.failure_count += 1
        self.retry_after = time.monotonic() + self.cooldown
        self.admission_attempts += 1


def _fp(item: Any) -> str:
    return hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:12]


class AdaptiveQuarantine:
    INITIAL_COOLDOWN = 300.0   # 5 minutes to first retry

    def __init__(self, failure_threshold: int = 3):
        self.failure_threshold = failure_threshold
        self._failures: dict[str, int] = {}
        self._quarantine: dict[str, QuarantineEntry] = {}

    def is_blocked(self, item: Any) -> bool:
        fp = _fp(item)
        entry = self._quarantine.get(fp)
        if not entry:
            return False
        return time.monotonic() < entry.retry_after

    async def run(self, item: Any, handler: Callable) -> Any:
        fp = _fp(item)

        if self.is_blocked(item):
            entry = self._quarantine[fp]
            wait = entry.retry_after - time.monotonic()
            raise RuntimeError(
                f"Quarantined for {wait:.0f}s more "
                f"(cooldown={entry.cooldown:.0f}s, failures={entry.failure_count})"
            )

        try:
            result = await handler(item)
            # Success — reduce quarantine pressure if applicable
            if fp in self._quarantine:
                self._quarantine[fp].schedule_next_retry(success=True)
                print(f"[{fp}] Admitted successfully — cooldown reduced to {self._quarantine[fp].cooldown:.0f}s")
            self._failures[fp] = 0
            return result

        except Exception as e:
            self._failures[fp] = self._failures.get(fp, 0) + 1

            if self._failures[fp] >= self.failure_threshold:
                if fp not in self._quarantine:
                    self._quarantine[fp] = QuarantineEntry(
                        fingerprint=fp,
                        retry_after=time.monotonic() + self.INITIAL_COOLDOWN,
                        cooldown=self.INITIAL_COOLDOWN,
                        failure_count=1,
                    )
                else:
                    self._quarantine[fp].schedule_next_retry(success=False)

                print(
                    f"QUARANTINED [{fp}] — next retry in "
                    f"{self._quarantine[fp].cooldown:.0f}s"
                )

            raise
```

## Comparison

| Approach | Detection mechanism | DLQ support | Re-admission | LLM-assisted | Resource-aware |
|---|---|---|---|---|---|
| Failure counter quarantine | Error count threshold | No | TTL-based | No | No |
| DLQ with exponential backoff | Max retry exceeded | Yes | Manual | No | No |
| Resource-based detection | Timeout rate / latency | No | Manual | No | Yes |
| LLM classifier | Error + item analysis | No | Per-classification | Yes | No |
| Bi-lane queue isolation | Suspect routing | No | Gradual | No | No |
| Adaptive graduated re-admission | Failure history | No | Automatic | No | No |

**Recommendation**: Combine **failure counter quarantine** (Solution 1) for fast detection with a **dead letter queue** (Solution 2) for manual inspection and reprocessing. Add **resource-based detection** (Solution 3) for inputs that cause timeouts rather than exceptions. Use **adaptive graduated re-admission** (Solution 6) for items that might recover after transient infrastructure issues.
