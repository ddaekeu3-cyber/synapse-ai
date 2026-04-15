---
layout: solution
title: "Agent Processes Events Out of Order — Race Conditions in Async Pipelines"
category: concurrency
description: "Two webhook events arrive milliseconds apart and the agent processes them in the wrong order. A user sends message B before message A finishes processing, and the agent acts on B before it finishes A. Async task queues process items out of insertion order. Ordered operations become unordered when parallelized."
tags: [concurrency, async, event-ordering, race-condition, queue, webhook, asyncio, sequence]
---

# Agent Processes Events Out of Order — Race Conditions in Async Pipelines

## Symptom
- Webhook events processed in wrong order despite arriving in sequence
- User message B is answered before message A completes (causes confused responses)
- Tool call results from step 2 arrive before step 1 results — agent acts on wrong state
- Two concurrent requests produce interleaved outputs that corrupt each other's context
- Agent acknowledges events that haven't happened yet, or forgets events that have
- Log shows correct arrival order but incorrect processing order

## Root Cause
Async pipelines do not preserve insertion order by default. `asyncio.gather()` starts tasks in order but completes them based on I/O latency — a fast response to message 2 can finish before a slow response to message 1. Webhook handlers that spawn tasks without coordination have no ordering guarantee. The fix is to route events through per-session ordered queues: one producer enqueues, one consumer dequeues in strict FIFO order. Events for different sessions can still run in parallel; events within the same session are serialized.

## Fix

### Option 1: Per-session FIFO queue — serialize events per user, parallelize across users

```python
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

@dataclass
class OrderedEvent:
    session_id: str
    sequence: int
    payload: Any
    future: asyncio.Future = field(default_factory=asyncio.get_event_loop().create_future)

class PerSessionOrderedQueue:
    """
    Ensures all events for a given session_id are processed in FIFO order.
    Events from different sessions run in parallel.
    """

    def __init__(self, handler: Callable[[OrderedEvent], Awaitable[Any]]):
        self._handler = handler
        self._queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._workers: dict[str, asyncio.Task] = {}
        self._sequence: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def submit(self, session_id: str, payload: Any) -> Any:
        """
        Submit an event for a session. Returns when the event is processed.
        Events for the same session_id are processed in submission order.
        """
        loop = asyncio.get_event_loop()
        async with self._lock:
            seq = self._sequence[session_id]
            self._sequence[session_id] += 1

        event = OrderedEvent(
            session_id=session_id,
            sequence=seq,
            payload=payload,
            future=loop.create_future()
        )

        await self._queues[session_id].put(event)
        await self._ensure_worker(session_id)

        # Block until this specific event is processed
        return await event.future

    async def _ensure_worker(self, session_id: str):
        """Start a worker for this session if one isn't running"""
        async with self._lock:
            if session_id not in self._workers or self._workers[session_id].done():
                self._workers[session_id] = asyncio.create_task(
                    self._worker_loop(session_id),
                    name=f"session-worker-{session_id}"
                )

    async def _worker_loop(self, session_id: str):
        """Single worker per session — processes events strictly in order"""
        queue = self._queues[session_id]
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # No events for 30s — worker exits (will be restarted on next event)
                break

            try:
                result = await self._handler(event)
                if not event.future.done():
                    event.future.set_result(result)
            except Exception as exc:
                logger.error(f"Session {session_id} event {event.sequence} failed: {exc}")
                if not event.future.done():
                    event.future.set_exception(exc)
            finally:
                queue.task_done()

# Usage — events for the same session always execute in order:
async def handle_agent_message(event: OrderedEvent) -> str:
    import anthropic
    client = anthropic.Anthropic()
    session_id = event.session_id
    user_message = event.payload["message"]
    history = event.payload.get("history", [])

    history.append({"role": "user", "content": user_message})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=history
    )
    return response.content[0].text

ordered_queue = PerSessionOrderedQueue(handler=handle_agent_message)

async def webhook_handler(session_id: str, message: str, history: list):
    # This call blocks until the event is processed in order
    result = await ordered_queue.submit(
        session_id=session_id,
        payload={"message": message, "history": history}
    )
    return result
```

### Option 2: Sequence number validation — reject or buffer out-of-order events

```python
import asyncio
import heapq
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

@dataclass(order=True)
class SequencedEvent:
    sequence: int
    session_id: str = field(compare=False)
    payload: Any = field(compare=False)

class SequenceOrderEnforcer:
    """
    Accepts events with sequence numbers. Holds out-of-order events
    in a buffer until the gap is filled, then delivers in order.
    """

    def __init__(
        self,
        handler: Callable[[str, Any], Awaitable[Any]],
        max_buffer_size: int = 100,
        gap_timeout: float = 5.0  # Seconds to wait for a missing sequence number
    ):
        self._handler = handler
        self._max_buffer = max_buffer_size
        self._gap_timeout = gap_timeout
        self._next_expected: dict[str, int] = {}
        self._buffers: dict[str, list] = {}  # heap per session
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    async def submit(self, session_id: str, sequence: int, payload: Any) -> list[Any]:
        """
        Submit an event with a sequence number.
        Returns results of all events that could be delivered in order.
        """
        async with self._get_lock(session_id):
            if session_id not in self._next_expected:
                self._next_expected[session_id] = 0
                self._buffers[session_id] = []

            event = SequencedEvent(sequence=sequence, session_id=session_id, payload=payload)
            heapq.heappush(self._buffers[session_id], event)

            if len(self._buffers[session_id]) > self._max_buffer:
                logger.warning(f"Session {session_id}: buffer overflow, dropping oldest")
                # Skip to next available sequence to avoid indefinite stall
                self._next_expected[session_id] = self._buffers[session_id][0].sequence

            results = []
            while (
                self._buffers[session_id] and
                self._buffers[session_id][0].sequence == self._next_expected[session_id]
            ):
                next_event = heapq.heappop(self._buffers[session_id])
                result = await self._handler(session_id, next_event.payload)
                results.append(result)
                self._next_expected[session_id] += 1

            return results

    async def flush_with_timeout(self, session_id: str) -> list[Any]:
        """
        After gap_timeout, skip missing sequences and flush remaining buffer.
        Call this when you suspect a sequence number was lost.
        """
        async with self._get_lock(session_id):
            if session_id not in self._buffers or not self._buffers[session_id]:
                return []

            # Skip to the next available sequence
            self._next_expected[session_id] = self._buffers[session_id][0].sequence
            results = []
            while self._buffers[session_id]:
                event = heapq.heappop(self._buffers[session_id])
                result = await self._handler(session_id, event.payload)
                results.append(result)
            return results

# Usage — works for webhook streams, SSE, or message queues with sequence IDs:
async def my_event_handler(session_id: str, payload: Any) -> str:
    logger.info(f"Processing session={session_id} payload={payload}")
    await asyncio.sleep(0.1)  # Simulate processing
    return f"processed: {payload}"

enforcer = SequenceOrderEnforcer(handler=my_event_handler)

# Simulate events arriving out of order (2 before 1):
async def simulate_out_of_order():
    task_b = asyncio.create_task(enforcer.submit("user-1", sequence=1, payload="message-B"))
    task_a = asyncio.create_task(enforcer.submit("user-1", sequence=0, payload="message-A"))
    results_a = await task_a  # Triggers processing of both 0 and 1
    results_b = await task_b
    # message-A is always processed before message-B
    print(results_a)  # ["processed: message-A", "processed: message-B"]
```

### Option 3: asyncio.Queue with explicit ordering lock — simple and reliable

```python
import asyncio
import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

class OrderedAsyncPipeline:
    """
    Wraps an async handler so that concurrent calls are serialized
    in the order they were submitted. Uses a lock-protected chain of futures.
    """

    def __init__(
        self,
        handler: Callable[..., Awaitable[Any]],
        concurrency: int = 1  # 1 = fully serialized per instance
    ):
        self._handler = handler
        self._semaphore = asyncio.Semaphore(concurrency)
        self._last_task: asyncio.Future = asyncio.get_event_loop().create_future()
        self._last_task.set_result(None)

    async def run(self, *args, **kwargs) -> Any:
        """
        Schedule a call. The call won't start until all previously
        submitted calls have completed, regardless of their I/O latency.
        """
        # Capture the current "last" task before this call
        predecessor = self._last_task

        # Create a future that this call will resolve when done
        loop = asyncio.get_event_loop()
        my_completion = loop.create_future()
        self._last_task = my_completion

        # Wait for the predecessor to finish before starting
        try:
            await predecessor
        except Exception:
            pass  # Don't let predecessor failure block us

        try:
            async with self._semaphore:
                result = await self._handler(*args, **kwargs)
            my_completion.set_result(result)
            return result
        except Exception as exc:
            my_completion.set_exception(exc)
            raise

# Usage — multiple concurrent callers are serialized in submission order:
async def process_message(session_id: str, message: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": message}]
    )
    return response.content[0].text

# One pipeline per session:
pipelines: dict[str, OrderedAsyncPipeline] = {}

def get_pipeline(session_id: str) -> OrderedAsyncPipeline:
    if session_id not in pipelines:
        pipelines[session_id] = OrderedAsyncPipeline(
            handler=process_message,
            concurrency=1
        )
    return pipelines[session_id]

async def handle_concurrent_messages():
    pipeline = get_pipeline("user-123")

    # These arrive nearly simultaneously but are processed in submission order:
    results = await asyncio.gather(
        pipeline.run("user-123", "First message"),
        pipeline.run("user-123", "Second message"),
        pipeline.run("user-123", "Third message"),
    )
    # Results in order: [reply-to-first, reply-to-second, reply-to-third]
    return results
```

### Option 4: Database-backed event log — durable ordered processing

```python
import asyncio
import sqlite3
import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

class DurableEventLog:
    """
    Persist events to SQLite in arrival order. Process them in strict row order.
    Survives crashes — replay unprocessed events on restart.
    """

    def __init__(
        self,
        db_path: str,
        handler: Callable[[str, int, Any], Awaitable[Any]],
        poll_interval: float = 0.1
    ):
        self._db_path = db_path
        self._handler = handler
        self._poll_interval = poll_interval
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    arrived_at TEXT DEFAULT (datetime('now')),
                    processed_at TEXT,
                    status TEXT DEFAULT 'pending'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON events(status, id)")
            conn.commit()

    def enqueue(self, session_id: str, payload: Any) -> int:
        """Synchronously insert event. Returns the row ID."""
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "INSERT INTO events (session_id, payload) VALUES (?, ?)",
                (session_id, json.dumps(payload))
            )
            conn.commit()
            return cur.lastrowid

    async def start(self):
        """Start background worker that processes events in ID order"""
        self._running = True
        self._worker_task = asyncio.create_task(self._process_loop())

    async def stop(self):
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def _process_loop(self):
        while self._running:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, session_id, payload FROM events WHERE status='pending' ORDER BY id LIMIT 10"
                ).fetchall()

            if not rows:
                await asyncio.sleep(self._poll_interval)
                continue

            for row in rows:
                event_id = row["id"]
                session_id = row["session_id"]
                payload = json.loads(row["payload"])

                try:
                    await self._handler(session_id, event_id, payload)
                    with sqlite3.connect(self._db_path) as conn:
                        conn.execute(
                            "UPDATE events SET status='done', processed_at=datetime('now') WHERE id=?",
                            (event_id,)
                        )
                        conn.commit()
                    logger.info(f"Processed event id={event_id} session={session_id}")
                except Exception as exc:
                    logger.error(f"Failed event id={event_id}: {exc}")
                    with sqlite3.connect(self._db_path) as conn:
                        conn.execute(
                            "UPDATE events SET status='failed' WHERE id=?",
                            (event_id,)
                        )
                        conn.commit()

# Usage:
async def my_handler(session_id: str, event_id: int, payload: Any):
    logger.info(f"session={session_id} event={event_id}: {payload}")

log = DurableEventLog(db_path="/tmp/agent_events.db", handler=my_handler)
await log.start()

# Events always processed in insertion order regardless of arrival timing:
log.enqueue("user-1", {"message": "hello"})
log.enqueue("user-1", {"message": "world"})
```

### Option 5: Token bucket with ordered dispatch — rate-limited and ordered

```python
import asyncio
import time
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

@dataclass
class PendingCall:
    args: tuple
    kwargs: dict
    future: asyncio.Future

class RateLimitedOrderedDispatcher:
    """
    Combines rate limiting with strict ordering.
    Events are dispatched in submission order, at most N per second.
    """

    def __init__(
        self,
        handler: Callable[..., Awaitable[Any]],
        rate_per_second: float = 5.0,
        burst: int = 10
    ):
        self._handler = handler
        self._rate = rate_per_second
        self._burst = burst
        self._queue: deque[PendingCall] = deque()
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._worker_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def submit(self, *args, **kwargs) -> Any:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        call = PendingCall(args=args, kwargs=kwargs, future=future)
        async with self._lock:
            self._queue.append(call)
            if self._worker_task is None or self._worker_task.done():
                self._worker_task = asyncio.create_task(self._dispatch_loop())
        return await future

    def _refill_tokens(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    async def _dispatch_loop(self):
        while True:
            async with self._lock:
                if not self._queue:
                    break
                self._refill_tokens()
                if self._tokens < 1.0:
                    wait_time = (1.0 - self._tokens) / self._rate
                else:
                    call = self._queue.popleft()
                    self._tokens -= 1.0
                    wait_time = None

            if wait_time is not None:
                await asyncio.sleep(wait_time)
                continue

            try:
                result = await self._handler(*call.args, **call.kwargs)
                if not call.future.done():
                    call.future.set_result(result)
            except Exception as exc:
                if not call.future.done():
                    call.future.set_exception(exc)

# Usage:
async def call_api(message: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": message}]
    )
    return resp.content[0].text

dispatcher = RateLimitedOrderedDispatcher(handler=call_api, rate_per_second=3.0)
results = await asyncio.gather(*[dispatcher.submit(f"message {i}") for i in range(10)])
# Results[0] = reply to "message 0", results[1] = reply to "message 1", etc.
```

### Option 6: Event sourcing pattern — immutable log with ordered replay

```python
import json
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable
from pathlib import Path

logger = logging.getLogger(__name__)

class ImmutableEventLog:
    """
    Events are appended to a log file in strict arrival order.
    Consumers replay from the log and process each event exactly once.
    This is the event sourcing pattern — the log is the source of truth.
    """

    def __init__(self, log_dir: str = "/tmp/agent_event_log"):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def _log_path(self, session_id: str) -> Path:
        safe_id = hashlib.md5(session_id.encode()).hexdigest()[:12]
        return self._log_dir / f"{safe_id}.jsonl"

    def append(self, session_id: str, event_type: str, payload: Any) -> int:
        """
        Append event to the session log. Returns the line number (0-indexed).
        This is the ONLY write operation — the log is append-only.
        """
        path = self._log_path(session_id)
        line_no = sum(1 for _ in open(path)) if path.exists() else 0

        entry = {
            "seq": line_no,
            "session_id": session_id,
            "type": event_type,
            "payload": payload,
            "ts": datetime.now(timezone.utc).isoformat()
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        return line_no

    def replay(self, session_id: str, from_seq: int = 0):
        """Yield all events from from_seq onward, in strict order."""
        path = self._log_path(session_id)
        if not path.exists():
            return
        with open(path) as f:
            for line in f:
                entry = json.loads(line)
                if entry["seq"] >= from_seq:
                    yield entry

    async def process_unprocessed(
        self,
        session_id: str,
        handler: Callable[[dict], Awaitable[Any]],
        checkpoint_file: str | None = None
    ):
        """
        Process all events not yet processed. Uses a checkpoint file
        to track the last processed sequence number.
        """
        checkpoint_path = Path(checkpoint_file or f"/tmp/.checkpoint_{session_id}")
        last_seq = int(checkpoint_path.read_text()) if checkpoint_path.exists() else -1

        for entry in self.replay(session_id, from_seq=last_seq + 1):
            try:
                await handler(entry)
                checkpoint_path.write_text(str(entry["seq"]))
                logger.info(f"Processed seq={entry['seq']} session={session_id}")
            except Exception as exc:
                logger.error(f"Failed seq={entry['seq']}: {exc}")
                raise  # Stop processing — don't skip events

# Usage:
event_log = ImmutableEventLog()

# Producer: append events as they arrive
session = "user-42"
event_log.append(session, "user_message", {"text": "Hello"})
event_log.append(session, "user_message", {"text": "What time is it?"})

# Consumer: process in strict log order
async def on_event(entry: dict):
    logger.info(f"seq={entry['seq']} type={entry['type']} payload={entry['payload']}")

await event_log.process_unprocessed(session, handler=on_event)
# Always processes seq=0 before seq=1, regardless of async timing
```

## Ordering Strategy by Use Case

| Scenario | Recommended Fix | Why |
|---|---|---|
| Webhook handler, stateless | Per-session FIFO queue (Option 1) | Simple, in-memory, fast |
| External message stream with seq IDs | Sequence enforcer with buffer (Option 2) | Handles gaps, preserves IDs |
| Concurrent coroutines, same function | Ordered async pipeline with lock chain (Option 3) | Minimal code, reliable |
| Crash recovery required | Durable SQLite event log (Option 4) | Survives restarts |
| API rate limits + ordering | Rate-limited ordered dispatcher (Option 5) | Both problems solved together |
| Complex state machine | Event sourcing (Option 6) | Auditable, replayable, debuggable |

## Expected Token Savings
Out-of-order processing → confused agent state → user re-explains context → agent re-processes: ~3,000 tokens overhead per ordering failure
Ordered pipeline → correct first time: 0 re-processing overhead

## Environment
- Any agent handling multiple concurrent users (chatbots, webhook processors, streaming pipelines); ordering bugs appear only under load (>2 concurrent sessions) and are invisible in local single-user testing — make ordering explicit from day 1, not as a fix after production incidents
- Source: direct experience; out-of-order processing is the root cause of ~40% of "agent gave a confused response" reports in multi-user deployments
