---
layout: solution
title: "Agent Doesn't Implement Webhook Delivery with Retries"
category: general
description: "When an AI agent completes a task, it fires a one-shot HTTP webhook with no retry logic. If the receiver is temporarily unavailable, the notification is silently lost and downstream systems never learn the task completed."
tags: [webhook, retry, asyncio, queue, reliability, backoff, delivery]
---

# Agent Doesn't Implement Webhook Delivery with Retries

## Problem

Agent tasks complete asynchronously. A single `httpx.post(webhook_url, ...)` call works fine 95% of the time, but receivers restart, experience blips, and occasionally return 5xx. Without retry logic, those completions are silently swallowed. Downstream pipelines stall, users don't get notified, and no one knows why until someone manually inspects logs hours later.

## Solutions

### Option 1: Synchronous Retry with Exponential Backoff

```python
# webhooks/sender.py
"""
Simple synchronous webhook sender with exponential backoff.
Drop-in replacement for a bare httpx.post() call.
"""
import time
import hashlib
import hmac
import json
import httpx
from dataclasses import dataclass


@dataclass
class WebhookResult:
    success: bool
    attempts: int
    final_status: int | None
    error: str = ""


def send_webhook(
    url: str,
    payload: dict,
    secret: str = "",
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    timeout: float = 10.0,
) -> WebhookResult:
    """
    Deliver a webhook with exponential backoff retries.
    Signs the payload with HMAC-SHA256 if a secret is provided.
    """
    body = json.dumps(payload, separators=(",", ":"))
    headers = {"Content-Type": "application/json"}

    if secret:
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    last_status = None
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        try:
            resp = httpx.post(url, content=body, headers=headers, timeout=timeout)
            last_status = resp.status_code

            if resp.status_code < 500:
                # 2xx = success; 4xx = permanent failure (don't retry)
                return WebhookResult(
                    success=200 <= resp.status_code < 300,
                    attempts=attempt,
                    final_status=resp.status_code,
                    error="" if resp.status_code < 300 else f"HTTP {resp.status_code}",
                )

            # 5xx = transient, retry
            last_error = f"HTTP {resp.status_code}"

        except httpx.TimeoutException:
            last_error = f"timeout after {timeout}s"
        except httpx.RequestError as e:
            last_error = str(e)

        if attempt < max_attempts:
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            # Add jitter: ±20%
            jitter = delay * 0.2 * (2 * __import__("random").random() - 1)
            time.sleep(delay + jitter)

    return WebhookResult(
        success=False,
        attempts=max_attempts,
        final_status=last_status,
        error=f"Exhausted {max_attempts} attempts. Last error: {last_error}",
    )


# ── Usage after agent task completes ──────────────────────────────────────────

def on_task_complete(task_id: str, result: dict, webhook_url: str):
    outcome = send_webhook(
        url=webhook_url,
        payload={"task_id": task_id, "status": "completed", "result": result},
        secret="my-webhook-secret",
    )
    if not outcome.success:
        # Log to dead-letter store for manual inspection
        import logging
        logging.error(
            "Webhook delivery failed after %d attempts: %s",
            outcome.attempts,
            outcome.error,
        )
```

**Expected Token Savings:** Not applicable — infrastructure reliability
**Environment:** `pip install httpx`

---

### Option 2: Async Webhook Queue with Persistent SQLite Backing

```python
# webhooks/async_queue.py
"""
Async webhook delivery queue backed by SQLite.
- Survives process restarts (pending deliveries are re-attempted on startup).
- Workers pick up items from the queue; exponential backoff per item.
- Dead-letter after max_attempts.
"""
import asyncio
import json
import sqlite3
import time
import hashlib
import hmac
from pathlib import Path
import aiohttp


DB_PATH = Path("webhook_queue.db")


def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webhook_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            payload TEXT NOT NULL,
            secret TEXT DEFAULT '',
            attempts INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 5,
            next_attempt_at REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            last_error TEXT DEFAULT '',
            created_at REAL NOT NULL,
            completed_at REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_next ON webhook_jobs(status, next_attempt_at)")
    conn.commit()
    return conn


_db = _init_db()
_lock = asyncio.Lock()


def enqueue(url: str, payload: dict, secret: str = "", max_attempts: int = 5) -> int:
    """Add a webhook job to the queue. Thread-safe."""
    cursor = _db.execute(
        "INSERT INTO webhook_jobs (url, payload, secret, max_attempts, next_attempt_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (url, json.dumps(payload), secret, max_attempts, time.time(), time.time()),
    )
    _db.commit()
    return cursor.lastrowid


async def _deliver(session: aiohttp.ClientSession, job: dict) -> bool:
    """Attempt one delivery. Returns True on success."""
    body = job["payload"]
    headers = {"Content-Type": "application/json"}
    if job["secret"]:
        sig = hmac.new(job["secret"].encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={sig}"
    try:
        async with session.post(
            job["url"],
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


async def _worker(worker_id: int):
    """Background worker: polls for due jobs and attempts delivery."""
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            now = time.time()
            async with _lock:
                rows = _db.execute(
                    "SELECT id, url, payload, secret, attempts, max_attempts "
                    "FROM webhook_jobs "
                    "WHERE status = 'pending' AND next_attempt_at <= ? "
                    "ORDER BY next_attempt_at LIMIT 10",
                    (now,),
                ).fetchall()

            for row in rows:
                job_id, url, payload, secret, attempts, max_attempts = row
                job = {"url": url, "payload": payload, "secret": secret}
                success = await _deliver(session, job)
                new_attempts = attempts + 1

                async with _lock:
                    if success:
                        _db.execute(
                            "UPDATE webhook_jobs SET status='delivered', attempts=?, completed_at=? WHERE id=?",
                            (new_attempts, time.time(), job_id),
                        )
                    elif new_attempts >= max_attempts:
                        _db.execute(
                            "UPDATE webhook_jobs SET status='dead', attempts=? WHERE id=?",
                            (new_attempts, job_id),
                        )
                    else:
                        delay = min(2 ** new_attempts, 300)  # cap at 5 min
                        next_at = time.time() + delay
                        _db.execute(
                            "UPDATE webhook_jobs SET attempts=?, next_attempt_at=?, last_error='retry' WHERE id=?",
                            (new_attempts, next_at, job_id),
                        )
                    _db.commit()

            await asyncio.sleep(2)  # poll interval


async def start_workers(n: int = 3):
    """Start N background delivery workers."""
    for i in range(n):
        asyncio.create_task(_worker(i))


# ── Usage ─────────────────────────────────────────────────────────────────────

async def main():
    await start_workers(n=2)
    # Enqueue a webhook after task completion
    job_id = enqueue(
        url="https://example.com/hooks/task-complete",
        payload={"task_id": "abc123", "status": "done"},
        secret="secret123",
    )
    print(f"Queued webhook job {job_id}")
    await asyncio.sleep(30)  # workers run in background


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Not applicable — reliability infrastructure
**Environment:** `pip install aiohttp`

---

### Option 3: FastAPI Background Task with In-Process Queue

```python
# webhooks/fastapi_webhook.py
"""
FastAPI-native webhook delivery using BackgroundTasks + asyncio.Queue.
Suitable when you don't want an external queue broker but need async delivery.
"""
import asyncio
import json
import time
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Optional
import aiohttp
from fastapi import FastAPI, BackgroundTasks

logger = logging.getLogger(__name__)


@dataclass
class WebhookJob:
    url: str
    payload: dict
    secret: str = ""
    max_attempts: int = 5
    attempt: int = 0
    next_retry_at: float = field(default_factory=time.time)


class WebhookDispatcher:
    def __init__(self, workers: int = 3):
        self._queue: asyncio.Queue[WebhookJob] = asyncio.Queue(maxsize=1000)
        self._dead_letters: list[WebhookJob] = []
        self._session: Optional[aiohttp.ClientSession] = None
        self._workers = workers

    async def start(self):
        self._session = aiohttp.ClientSession()
        for _ in range(self._workers):
            asyncio.create_task(self._worker())

    async def stop(self):
        if self._session:
            await self._session.close()

    def enqueue(self, job: WebhookJob):
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            logger.error("Webhook queue full — dropping job for %s", job.url)

    async def _deliver_once(self, job: WebhookJob) -> bool:
        body = json.dumps(job.payload)
        headers = {"Content-Type": "application/json"}
        if job.secret:
            sig = hmac.new(job.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            headers["X-Webhook-Signature"] = f"sha256={sig}"
        try:
            async with self._session.post(
                job.url, data=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                if resp.status < 500:
                    logger.warning("Webhook %s returned %d — not retrying", job.url, resp.status)
                    return True  # Treat client errors as terminal
                return False
        except Exception as e:
            logger.warning("Webhook delivery error for %s: %s", job.url, e)
            return False

    async def _worker(self):
        while True:
            job = await self._queue.get()
            # Honor scheduled retry time
            wait = job.next_retry_at - time.time()
            if wait > 0:
                await asyncio.sleep(wait)

            success = await self._deliver_once(job)
            if success:
                logger.info("Webhook delivered to %s (attempt %d)", job.url, job.attempt + 1)
            else:
                job.attempt += 1
                if job.attempt >= job.max_attempts:
                    logger.error("Webhook dead-lettered for %s after %d attempts", job.url, job.attempt)
                    self._dead_letters.append(job)
                else:
                    delay = min(2 ** job.attempt, 300)
                    job.next_retry_at = time.time() + delay
                    await self._queue.put(job)

            self._queue.task_done()

    def dead_letter_count(self) -> int:
        return len(self._dead_letters)


# ── FastAPI integration ───────────────────────────────────────────────────────

app = FastAPI()
dispatcher = WebhookDispatcher(workers=2)


@app.on_event("startup")
async def startup():
    await dispatcher.start()


@app.on_event("shutdown")
async def shutdown():
    await dispatcher.stop()


@app.post("/api/agent/run")
async def run_agent(request: dict, background_tasks: BackgroundTasks):
    """Run agent task and dispatch webhook on completion."""
    # ... run agent task here ...
    result = {"output": "task done"}

    webhook_url = request.get("webhook_url")
    if webhook_url:
        dispatcher.enqueue(WebhookJob(
            url=webhook_url,
            payload={"task_id": request.get("task_id"), "result": result},
            secret=request.get("webhook_secret", ""),
        ))

    return {"status": "accepted", "result": result}
```

**Expected Token Savings:** Not applicable — async delivery infrastructure
**Environment:** `pip install fastapi aiohttp uvicorn`

---

### Option 4: Webhook Delivery with Idempotency Keys

```python
# webhooks/idempotent_sender.py
"""
Webhook delivery with idempotency keys.
Prevents duplicate deliveries when retrying — critical for side-effectful receivers
like payment systems, database writes, or email senders.
"""
import asyncio
import hashlib
import json
import time
import sqlite3
from pathlib import Path
import aiohttp


DELIVERY_LOG_DB = Path("webhook_delivery_log.db")
_log_conn = sqlite3.connect(str(DELIVERY_LOG_DB))
_log_conn.execute("""
    CREATE TABLE IF NOT EXISTS delivery_log (
        idempotency_key TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        delivered_at REAL NOT NULL,
        status_code INTEGER NOT NULL
    )
""")
_log_conn.commit()


def _make_idempotency_key(url: str, payload: dict) -> str:
    """Deterministic key from URL + payload content."""
    canonical = json.dumps({"url": url, "payload": payload}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def deliver_once(
    url: str,
    payload: dict,
    idempotency_key: str | None = None,
    max_attempts: int = 5,
) -> dict:
    """
    Deliver a webhook exactly once.
    If already delivered (same idempotency_key), return cached result immediately.
    """
    key = idempotency_key or _make_idempotency_key(url, payload)

    # Check delivery log first
    existing = _log_conn.execute(
        "SELECT delivered_at, status_code FROM delivery_log WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if existing:
        return {"status": "already_delivered", "delivered_at": existing[0], "status_code": existing[1]}

    body = json.dumps(payload)
    headers = {
        "Content-Type": "application/json",
        "X-Idempotency-Key": key,
    }

    async with aiohttp.ClientSession() as session:
        for attempt in range(1, max_attempts + 1):
            try:
                async with session.post(
                    url, data=body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if 200 <= resp.status < 300:
                        _log_conn.execute(
                            "INSERT OR IGNORE INTO delivery_log VALUES (?, ?, ?, ?)",
                            (key, url, time.time(), resp.status),
                        )
                        _log_conn.commit()
                        return {"status": "delivered", "attempt": attempt, "status_code": resp.status}
                    if resp.status < 500:
                        return {"status": "failed", "status_code": resp.status, "permanent": True}
            except Exception as e:
                pass

            if attempt < max_attempts:
                await asyncio.sleep(min(2 ** attempt, 60))

    return {"status": "failed", "attempts": max_attempts}


# ── Example: safe to call multiple times for the same task completion ─────────

async def notify_task_complete(task_id: str, result: dict, webhook_url: str):
    outcome = await deliver_once(
        url=webhook_url,
        payload={"task_id": task_id, "status": "completed", "result": result},
        idempotency_key=f"task-complete-{task_id}",  # Stable key per task
    )
    if outcome["status"] == "already_delivered":
        print(f"Webhook for task {task_id} already delivered — skipping duplicate")
    elif outcome["status"] == "delivered":
        print(f"Webhook delivered on attempt {outcome['attempt']}")
    else:
        print(f"Webhook delivery failed: {outcome}")
```

**Expected Token Savings:** Not applicable — delivery reliability
**Environment:** `pip install aiohttp`

---

### Option 5: Webhook Fan-Out to Multiple Subscribers

```python
# webhooks/fanout.py
"""
Deliver the same event to multiple webhook subscribers concurrently.
Each subscriber gets independent retry tracking — one slow/down subscriber
does not block delivery to others.
"""
import asyncio
import json
import time
import hashlib
import hmac
from dataclasses import dataclass, field
import aiohttp


@dataclass
class Subscriber:
    name: str
    url: str
    secret: str = ""
    max_attempts: int = 5
    timeout: float = 10.0
    # Tracks delivery state per event
    _attempts: int = field(default=0, repr=False)
    _delivered: bool = field(default=False, repr=False)


@dataclass
class FanOutResult:
    event_id: str
    total: int
    delivered: int
    failed: int
    details: list[dict]


async def _deliver_to_subscriber(
    session: aiohttp.ClientSession,
    subscriber: Subscriber,
    event_id: str,
    body: str,
) -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-Event-ID": event_id,
    }
    if subscriber.secret:
        sig = hmac.new(subscriber.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    for attempt in range(1, subscriber.max_attempts + 1):
        try:
            async with session.post(
                subscriber.url, data=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=subscriber.timeout),
            ) as resp:
                if 200 <= resp.status < 300:
                    return {"subscriber": subscriber.name, "status": "delivered", "attempts": attempt}
                if resp.status < 500:
                    return {"subscriber": subscriber.name, "status": "failed", "reason": f"HTTP {resp.status}"}
        except Exception as e:
            pass
        if attempt < subscriber.max_attempts:
            await asyncio.sleep(min(2 ** attempt, 60))

    return {"subscriber": subscriber.name, "status": "dead", "attempts": subscriber.max_attempts}


async def fan_out(
    event_type: str,
    payload: dict,
    subscribers: list[Subscriber],
    event_id: str | None = None,
) -> FanOutResult:
    """Deliver an event to all subscribers concurrently."""
    if event_id is None:
        import uuid
        event_id = str(uuid.uuid4())

    envelope = {"event_id": event_id, "event_type": event_type, "payload": payload, "timestamp": time.time()}
    body = json.dumps(envelope)

    async with aiohttp.ClientSession() as session:
        tasks = [_deliver_to_subscriber(session, sub, event_id, body) for sub in subscribers]
        details = await asyncio.gather(*tasks)

    delivered = sum(1 for d in details if d["status"] == "delivered")
    return FanOutResult(
        event_id=event_id,
        total=len(subscribers),
        delivered=delivered,
        failed=len(details) - delivered,
        details=details,
    )


# ── Usage ─────────────────────────────────────────────────────────────────────

SUBSCRIBERS = [
    Subscriber("analytics", "https://analytics.internal/events", secret="s1"),
    Subscriber("notifications", "https://notify.internal/hooks", secret="s2"),
    Subscriber("audit-log", "https://audit.internal/webhook", secret="s3"),
]


async def on_agent_task_complete(task_id: str, result: dict):
    report = await fan_out(
        event_type="agent.task.completed",
        payload={"task_id": task_id, "result": result},
        subscribers=SUBSCRIBERS,
    )
    print(f"Fan-out: {report.delivered}/{report.total} delivered for event {report.event_id}")
    for d in report.details:
        if d["status"] != "delivered":
            print(f"  FAILED: {d['subscriber']} — {d.get('reason', 'exhausted retries')}")
```

**Expected Token Savings:** Not applicable — event delivery infrastructure
**Environment:** `pip install aiohttp`

---

### Option 6: Dead-Letter Queue with Admin Replay

```python
# webhooks/dlq.py
"""
Full delivery pipeline: attempt delivery, move failures to a dead-letter queue,
expose an admin endpoint to inspect and replay dead-lettered webhooks.
"""
import asyncio
import json
import sqlite3
import time
import hashlib
import hmac
from pathlib import Path
import aiohttp
from fastapi import FastAPI, HTTPException


DB = sqlite3.connect(str(Path("webhook_dlq.db")), check_same_thread=False)
DB.execute("""
    CREATE TABLE IF NOT EXISTS dlq (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        url TEXT NOT NULL,
        payload TEXT NOT NULL,
        secret TEXT DEFAULT '',
        failure_reason TEXT,
        attempts INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        dead_at REAL NOT NULL,
        replayed INTEGER DEFAULT 0
    )
""")
DB.commit()

app = FastAPI()


async def _attempt_delivery(url: str, payload: dict, secret: str = "", max_attempts: int = 5) -> tuple[bool, str]:
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    if secret:
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={sig}"
    async with aiohttp.ClientSession() as session:
        for attempt in range(1, max_attempts + 1):
            try:
                async with session.post(url, data=body, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if 200 <= resp.status < 300:
                        return True, ""
                    if resp.status < 500:
                        return False, f"HTTP {resp.status} (permanent)"
                    reason = f"HTTP {resp.status}"
            except Exception as e:
                reason = str(e)
            if attempt < max_attempts:
                await asyncio.sleep(min(2 ** attempt, 60))
    return False, reason


async def deliver_or_dlq(event_type: str, url: str, payload: dict, secret: str = ""):
    success, reason = await _attempt_delivery(url, payload, secret)
    if not success:
        DB.execute(
            "INSERT INTO dlq (event_type, url, payload, secret, failure_reason, attempts, created_at, dead_at) "
            "VALUES (?, ?, ?, ?, ?, 5, ?, ?)",
            (event_type, url, json.dumps(payload), secret, reason, time.time(), time.time()),
        )
        DB.commit()
        print(f"Webhook dead-lettered: {event_type} -> {url}: {reason}")


@app.get("/admin/dlq")
def list_dlq(limit: int = 50):
    rows = DB.execute(
        "SELECT id, event_type, url, failure_reason, attempts, dead_at, replayed "
        "FROM dlq ORDER BY dead_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {"id": r[0], "event_type": r[1], "url": r[2], "reason": r[3],
         "attempts": r[4], "dead_at": r[5], "replayed": bool(r[6])}
        for r in rows
    ]


@app.post("/admin/dlq/{item_id}/replay")
async def replay_dlq_item(item_id: int):
    row = DB.execute(
        "SELECT url, payload, secret FROM dlq WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="DLQ item not found")
    url, payload_str, secret = row
    payload = json.loads(payload_str)
    success, reason = await _attempt_delivery(url, payload, secret, max_attempts=3)
    if success:
        DB.execute("UPDATE dlq SET replayed = 1 WHERE id = ?", (item_id,))
        DB.commit()
        return {"status": "replayed", "success": True}
    return {"status": "replay_failed", "reason": reason}
```

**Expected Token Savings:** Not applicable — operational infrastructure
**Environment:** `pip install fastapi aiohttp uvicorn`

---

## Comparison Table

| Option | Storage | Survives Restart | Idempotent | Fan-Out | Admin Replay | Complexity |
|--------|---------|------------------|------------|---------|--------------|------------|
| 1: Sync backoff | None | No | No | No | No | Minimal |
| 2: Async SQLite queue | SQLite | Yes | No | No | No | Medium |
| 3: FastAPI queue | In-memory | No | No | No | No | Low |
| 4: Idempotency keys | SQLite | Yes | Yes | No | No | Medium |
| 5: Fan-out | None | No | No | Yes | No | Medium |
| 6: DLQ + replay | SQLite | Yes | No | No | Yes | High |
