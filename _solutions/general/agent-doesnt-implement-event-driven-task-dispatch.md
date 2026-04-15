---
layout: solution
title: "Agent Doesn't Implement Event-Driven Task Dispatch"
category: general
description: "Agents that poll for work or process tasks synchronously waste resources and cannot scale. Event-driven dispatch — where tasks arrive via queues, webhooks, or pub/sub channels — lets agents react to work only when it arrives, enabling parallel processing and backpressure."
tags: [general, event-driven, queue, dispatch, asyncio, pub-sub, webhook, scalability]
---

## Problem

Agents that use tight polling loops (`while True: check_for_work()`) waste CPU and API quota when idle, and block on long tasks. Synchronous pipelines stall when one step is slow. Without event-driven dispatch, agents cannot scale horizontally, cannot prioritize urgent tasks, and cannot handle bursty workloads gracefully. An event-driven architecture decouples work producers from consumers and enables reactive, non-blocking task handling.

## Solutions

### Option 1: asyncio.Queue Dispatcher with Priority Lanes

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum

client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    HIGH = 0
    NORMAL = 1
    LOW = 2

@dataclass(order=True)
class Task:
    priority: Priority
    payload: dict = field(compare=False)
    enqueued_at: float = field(default_factory=time.time, compare=False)

class EventDispatcher:
    def __init__(self, num_workers: int = 3):
        self._queue: asyncio.PriorityQueue[Task] = asyncio.PriorityQueue()
        self._num_workers = num_workers
        self._processed = 0

    async def enqueue(self, payload: dict, priority: Priority = Priority.NORMAL):
        task = Task(priority=priority, payload=payload)
        await self._queue.put(task)

    async def _worker(self, worker_id: int):
        while True:
            task = await self._queue.get()
            try:
                latency = time.time() - task.enqueued_at
                result = await self._process(task)
                self._processed += 1
                print(f"  [W{worker_id}] P{task.priority.name} | latency={latency:.2f}s | {result[:50]}")
            except Exception as e:
                print(f"  [W{worker_id}] ERROR: {e}")
            finally:
                self._queue.task_done()

    async def _process(self, task: Task) -> str:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": task.payload.get("prompt", "OK")}],
        )
        return resp.content[0].text

    async def run(self, events: list[tuple[dict, Priority]]):
        workers = [asyncio.create_task(self._worker(i)) for i in range(self._num_workers)]
        for payload, priority in events:
            await self.enqueue(payload, priority)
        await self._queue.join()
        for w in workers:
            w.cancel()
        print(f"\nProcessed {self._processed} tasks")

async def main():
    dispatcher = EventDispatcher(num_workers=2)
    events = [
        ({"prompt": "Low priority: what is 1+1?"}, Priority.LOW),
        ({"prompt": "HIGH: urgent alert summarize this NOW"}, Priority.HIGH),
        ({"prompt": "Normal: describe a cat"}, Priority.NORMAL),
        ({"prompt": "HIGH: critical failure report"}, Priority.HIGH),
        ({"prompt": "Low: fun fact about penguins"}, Priority.LOW),
        ({"prompt": "Normal: list 3 colors"}, Priority.NORMAL),
    ]
    await dispatcher.run(events)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: workers process only on arrival; no idle polling consuming API quota
# Environment: multi-tenant agents; HIGH priority tasks jump queue ahead of LOW regardless of arrival order
```

### Option 2: SQLite-Backed Persistent Event Queue with At-Least-Once Delivery

```python
import anthropic
import sqlite3
import time
import uuid
from pathlib import Path
from threading import Thread, Event

DB = Path("/tmp/agent_events.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS event_queue (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT DEFAULT 'pending',  -- pending | processing | done | failed
            priority INTEGER DEFAULT 1,
            enqueued_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            attempts INTEGER DEFAULT 0,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_status_priority ON event_queue(status, priority, enqueued_at);
    """)
    con.commit()
    con.close()

def publish_event(event_type: str, payload: dict, priority: int = 1) -> str:
    event_id = str(uuid.uuid4())
    con = sqlite3.connect(DB)
    import json
    con.execute("""
        INSERT INTO event_queue (id, event_type, payload, priority, enqueued_at)
        VALUES (?, ?, ?, ?, ?)
    """, (event_id, event_type, json.dumps(payload), priority, time.time()))
    con.commit()
    con.close()
    return event_id

def claim_next_event(worker_id: str) -> dict | None:
    """Claim the highest-priority pending event atomically."""
    import json
    con = sqlite3.connect(DB, isolation_level="EXCLUSIVE")
    row = con.execute("""
        SELECT id, event_type, payload FROM event_queue
        WHERE status = 'pending' AND attempts < 3
        ORDER BY priority ASC, enqueued_at ASC
        LIMIT 1
    """).fetchone()
    if row:
        event_id, event_type, payload_str = row
        con.execute("""
            UPDATE event_queue SET status='processing', started_at=?, attempts=attempts+1
            WHERE id=?
        """, (time.time(), event_id))
        con.commit()
        con.close()
        return {"id": event_id, "type": event_type, "payload": json.loads(payload_str)}
    con.close()
    return None

def complete_event(event_id: str, success: bool, error: str = ""):
    con = sqlite3.connect(DB)
    status = "done" if success else "failed"
    con.execute("""
        UPDATE event_queue SET status=?, completed_at=?, error=?
        WHERE id=?
    """, (status, time.time(), error, event_id))
    con.commit()
    con.close()

def process_event(event: dict) -> str:
    payload = event["payload"]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": payload.get("prompt", "OK")}],
    )
    return resp.content[0].text

def worker_loop(worker_id: str, stop_event: Event, poll_interval: float = 0.5):
    while not stop_event.is_set():
        event = claim_next_event(worker_id)
        if event:
            try:
                result = process_event(event)
                complete_event(event["id"], success=True)
                print(f"  [{worker_id}] DONE {event['type']}: {result.strip()[:40]}")
            except Exception as e:
                complete_event(event["id"], success=False, error=str(e))
                print(f"  [{worker_id}] FAIL: {e}")
        else:
            stop_event.wait(timeout=poll_interval)

if __name__ == "__main__":
    init_db()

    # Publish events
    publish_event("summarize", {"prompt": "Summarize: The cat sat on the mat."}, priority=1)
    publish_event("classify", {"prompt": "Is 'hello' formal or informal?"}, priority=0)
    publish_event("translate", {"prompt": "Translate to Spanish: Good morning."}, priority=1)

    stop = Event()
    workers = [
        Thread(target=worker_loop, args=(f"W{i}", stop), daemon=True)
        for i in range(2)
    ]
    for w in workers:
        w.start()
    time.sleep(5)
    stop.set()
    for w in workers:
        w.join(timeout=2)
    print("Queue processed.")

# Expected Token Savings: persistent queue survives crashes; at-least-once ensures no lost events after restart
# Environment: multi-process agents; SQLite EXCLUSIVE prevents double-processing across workers
```

### Option 3: Webhook Receiver with Async Event Fan-Out

```python
import anthropic
import asyncio
import hashlib
import hmac
import json
import time
from aiohttp import web

client = anthropic.AsyncAnthropic()
WEBHOOK_SECRET = b"my-secret-key"

# Event routing table: event_type -> list of handler coroutines
_handlers: dict[str, list] = {}

def on_event(event_type: str):
    """Decorator to register a handler for an event type."""
    def decorator(fn):
        _handlers.setdefault(event_type, []).append(fn)
        return fn
    return decorator

async def dispatch_event(event_type: str, payload: dict):
    handlers = _handlers.get(event_type, [])
    if not handlers:
        print(f"  [WARN] No handler for event: {event_type}")
        return
    # Fan-out: run all handlers concurrently
    results = await asyncio.gather(
        *[h(payload) for h in handlers],
        return_exceptions=True,
    )
    for h, r in zip(handlers, results):
        if isinstance(r, Exception):
            print(f"  [ERROR] handler {h.__name__}: {r}")

def verify_signature(body: bytes, sig_header: str) -> bool:
    expected = hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", sig_header)

@on_event("message.received")
async def handle_new_message(payload: dict):
    text = payload.get("text", "")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Reply to: {text}"}],
    )
    print(f"  [reply] {resp.content[0].text.strip()[:60]}")

@on_event("message.received")
async def handle_classify_message(payload: dict):
    text = payload.get("text", "")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{"role": "user", "content": f"Is this urgent? yes/no: {text}"}],
    )
    print(f"  [classify] urgent={resp.content[0].text.strip()}")

@on_event("user.signup")
async def handle_signup(payload: dict):
    name = payload.get("name", "User")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Write a brief welcome message for {name}."}],
    )
    print(f"  [welcome] {resp.content[0].text.strip()[:60]}")

async def webhook_handler(request: web.Request) -> web.Response:
    body = await request.read()
    sig = request.headers.get("X-Signature", "")
    if not verify_signature(body, sig):
        return web.json_response({"error": "invalid signature"}, status=401)
    event = json.loads(body)
    asyncio.create_task(dispatch_event(event["type"], event.get("payload", {})))
    return web.json_response({"status": "accepted"})

async def main():
    # Simulate webhook events without HTTP server
    events = [
        ("message.received", {"text": "I need urgent help with my account!"}),
        ("user.signup", {"name": "Alice"}),
        ("message.received", {"text": "What are your business hours?"}),
    ]
    for event_type, payload in events:
        print(f"\n[EVENT] {event_type}")
        await dispatch_event(event_type, payload)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: fan-out runs handlers in parallel; shared event avoids duplicate API calls for same data
# Environment: webhook-driven agents (Slack, GitHub, payment); multiple handlers per event without sequential coupling
```

### Option 4: Event Sourcing — Replay Agent State from Event Log

```python
import anthropic
import json
import sqlite3
import time
import uuid
from pathlib import Path

DB = Path("/tmp/agent_event_log.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS event_log (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            aggregate_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            data TEXT NOT NULL,
            occurred_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_aggregate ON event_log(aggregate_id, seq);
    """)
    con.commit()
    con.close()

def append_event(aggregate_id: str, event_type: str, data: dict) -> int:
    event_id = str(uuid.uuid4())
    con = sqlite3.connect(DB)
    cur = con.execute("""
        INSERT INTO event_log (event_id, aggregate_id, event_type, data, occurred_at)
        VALUES (?, ?, ?, ?, ?)
    """, (event_id, aggregate_id, event_type, json.dumps(data), time.time()))
    seq = cur.lastrowid
    con.commit()
    con.close()
    return seq

def replay_state(aggregate_id: str) -> dict:
    """Reconstruct current state by replaying all events for this aggregate."""
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT event_type, data FROM event_log
        WHERE aggregate_id = ? ORDER BY seq ASC
    """, (aggregate_id,)).fetchall()
    con.close()

    state = {"id": aggregate_id, "messages": [], "status": "new"}
    for event_type, data_str in rows:
        data = json.loads(data_str)
        if event_type == "task.created":
            state.update({"prompt": data["prompt"], "status": "pending"})
        elif event_type == "task.started":
            state["status"] = "processing"
        elif event_type == "task.completed":
            state.update({"result": data["result"], "status": "done"})
        elif event_type == "task.failed":
            state.update({"error": data["error"], "status": "failed"})
        elif event_type == "message.appended":
            state["messages"].append(data["message"])
    return state

def process_task(task_id: str):
    state = replay_state(task_id)
    if state["status"] != "pending":
        print(f"  Task {task_id} in state '{state['status']}', skipping")
        return

    append_event(task_id, "task.started", {})
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": state["prompt"]}],
        )
        result = resp.content[0].text
        append_event(task_id, "task.completed", {"result": result})
        print(f"  DONE [{task_id}]: {result.strip()[:50]}")
    except Exception as e:
        append_event(task_id, "task.failed", {"error": str(e)})
        print(f"  FAIL [{task_id}]: {e}")

if __name__ == "__main__":
    init_db()

    # Create tasks via events
    t1 = "task-001"
    t2 = "task-002"
    append_event(t1, "task.created", {"prompt": "What is event sourcing in 10 words?"})
    append_event(t2, "task.created", {"prompt": "Name the planets of the solar system."})

    # Process
    process_task(t1)
    process_task(t2)

    # Replay state — survives restarts
    print("\nReplayed states:")
    print("  t1:", replay_state(t1)["status"], "|", replay_state(t1).get("result", "")[:40])
    print("  t2:", replay_state(t2)["status"], "|", replay_state(t2).get("result", "")[:40])

# Expected Token Savings: no re-processing of completed tasks; replay reconstructs state without re-calling Claude
# Environment: durable agents; event log enables point-in-time replay for debugging and audit
```

### Option 5: Pub/Sub with Topic Subscriptions and Dead Letter Queue

```python
import anthropic
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class Message:
    topic: str
    payload: dict
    msg_id: int = field(default_factory=lambda: int(time.time() * 1000))
    attempts: int = 0
    max_attempts: int = 3

class PubSubBus:
    def __init__(self):
        self._subscribers: dict[str, list] = defaultdict(list)
        self._dlq: list[tuple[Message, str]] = []  # (msg, error)
        self._msg_count = 0

    def subscribe(self, topic: str, handler):
        self._subscribers[topic].append(handler)

    async def publish(self, topic: str, payload: dict):
        self._msg_count += 1
        msg = Message(topic=topic, payload=payload)
        handlers = self._subscribers.get(topic, [])
        if not handlers:
            print(f"  [PubSub] No subscribers for topic: {topic}")
            return
        await asyncio.gather(*[self._deliver(h, msg) for h in handlers])

    async def _deliver(self, handler, msg: Message):
        for attempt in range(msg.max_attempts):
            try:
                await handler(msg.payload)
                return
            except Exception as e:
                msg.attempts += 1
                if attempt + 1 >= msg.max_attempts:
                    self._dlq.append((msg, str(e)))
                    print(f"  [DLQ] {msg.topic} after {msg.attempts} attempts: {e}")
                else:
                    await asyncio.sleep(0.5 * (2 ** attempt))

    def drain_dlq(self) -> list[tuple[Message, str]]:
        items = list(self._dlq)
        self._dlq.clear()
        return items

bus = PubSubBus()

@bus.subscribe.__func__
async def _(): pass  # placeholder — use bus.subscribe() below

async def handle_summarize(payload: dict):
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Summarize in 10 words: {payload['text']}"}],
    )
    print(f"  [summarize] {resp.content[0].text.strip()[:60]}")

async def handle_sentiment(payload: dict):
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{"role": "user", "content": f"Sentiment (positive/negative/neutral): {payload['text']}"}],
    )
    print(f"  [sentiment] {resp.content[0].text.strip()}")

async def handle_alert(payload: dict):
    if payload.get("simulate_failure"):
        raise RuntimeError("Simulated handler failure")
    print(f"  [alert] Notifying admin: {payload.get('message', '')[:40]}")

async def main():
    bus.subscribe("text.received", handle_summarize)
    bus.subscribe("text.received", handle_sentiment)
    bus.subscribe("alert.triggered", handle_alert)

    await bus.publish("text.received", {"text": "The new product launch exceeded all sales targets."})
    await bus.publish("text.received", {"text": "Server response times are degrading rapidly."})
    await bus.publish("alert.triggered", {"message": "CPU > 90%", "simulate_failure": True})

    dlq = bus.drain_dlq()
    if dlq:
        print(f"\nDead letter queue: {len(dlq)} message(s)")
        for msg, err in dlq:
            print(f"  Topic={msg.topic} | Error={err}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: multiple subscribers share one message payload; fan-out avoids redundant publishing
# Environment: event-driven agents with multiple downstream consumers; DLQ prevents silent event loss
```

### Option 6: Backpressure-Aware Event Ingestion with Rate Gate

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class RateGate:
    """Allows at most `rate` events per `window` seconds into the agent."""
    rate: int
    window: float
    _timestamps: list[float] = None

    def __post_init__(self):
        self._timestamps = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.time()
            cutoff = now - self.window
            self._timestamps = [t for t in self._timestamps if t >= cutoff]
            if len(self._timestamps) >= self.rate:
                wait = self._timestamps[0] + self.window - now
                await asyncio.sleep(max(0.01, wait))
                now = time.time()
                self._timestamps = [t for t in self._timestamps if t >= now - self.window]
            self._timestamps.append(now)

class BackpressureEventIngester:
    def __init__(self, max_queue: int = 20, rate: int = 5, window: float = 10.0):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._gate = RateGate(rate=rate, window=window)
        self._dropped = 0
        self._processed = 0

    async def ingest(self, event: dict) -> bool:
        """Returns True if accepted, False if dropped (queue full)."""
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            return False

    async def _worker(self):
        while True:
            event = await self._queue.get()
            await self._gate.acquire()
            try:
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=32,
                    messages=[{"role": "user", "content": event.get("prompt", "OK")}],
                )
                self._processed += 1
                print(f"  [processed] {resp.content[0].text.strip()[:40]}")
            finally:
                self._queue.task_done()

    async def run(self, events: list[dict]):
        worker = asyncio.create_task(self._worker())
        for e in events:
            accepted = await self.ingest(e)
            if not accepted:
                print(f"  [DROPPED] backpressure: {e.get('prompt', '')[:30]}")
            await asyncio.sleep(0.05)
        await self._queue.join()
        worker.cancel()
        print(f"\nProcessed: {self._processed} | Dropped: {self._dropped}")

async def main():
    ingester = BackpressureEventIngester(max_queue=5, rate=3, window=5.0)
    events = [{"prompt": f"What is {i} squared?"} for i in range(10)]
    await ingester.run(events)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: dropped events prevent runaway spending; rate gate enforces API quota compliance
# Environment: high-volume event sources (webhooks, IoT); backpressure prevents queue explosion under load
```

## Comparison

| Option | Dispatch Model | Persistence | Priority | Backpressure | Best For |
|--------|--------------|-------------|---------|-------------|---------|
| 1 — asyncio PriorityQueue | In-process async | No | Yes | Queue size | Single-process prioritized dispatch |
| 2 — SQLite persistent queue | Multi-process | Yes | Yes | Retry limit | Durable at-least-once delivery |
| 3 — Webhook + fan-out | HTTP + async | No | No | None | Webhook-driven multi-handler fan-out |
| 4 — Event sourcing | SQLite append-only | Yes | No | None | Audit trail + crash recovery |
| 5 — Pub/Sub + DLQ | In-process async | No | No | None | Topic-based multi-consumer routing |
| 6 — Backpressure gate | In-process async | No | No | Queue cap + rate | High-volume sources with quota limits |
