---
layout: solution
title: "Agent Retries Already-Completed Task — Duplicate Execution"
category: concurrency
description: "Agent sends a payment, times out waiting for confirmation, retries, and sends the payment twice. Or inserts a database row, gets a network error on the response, retries, and creates a duplicate record."
tags: [idempotency, retry, duplicate, deduplication, exactly-once, concurrency]
---

# Agent Retries Already-Completed Task — Duplicate Execution

## Symptom
- Customer charged twice — agent retried a payment that already succeeded
- Duplicate rows in database — agent retried an insert after a network timeout
- Email sent twice — retry triggered after SMTP connection dropped post-send
- API endpoint called twice with identical side effects
- Agent has no way to know if a previous attempt succeeded before the response was lost

## Root Cause
Network timeouts happen after the server processes the request but before the response reaches the client. The agent sees a timeout, assumes failure, and retries — but the operation already completed. Without idempotency keys or deduplication, retries cause duplicate side effects.

## Fix

### Option 1: Idempotency keys on every mutating request

```python
import uuid
import httpx

def generate_idempotency_key(operation: str, payload: dict) -> str:
    """
    Generate a stable key for this specific operation attempt.
    Same operation + same payload = same key = safe to retry.
    """
    import hashlib, json
    content = f"{operation}:{json.dumps(payload, sort_keys=True)}"
    return hashlib.sha256(content.encode()).hexdigest()[:32]

async def call_with_idempotency(
    url: str,
    payload: dict,
    operation_name: str,
    max_retries: int = 3
) -> dict:
    """
    Make a mutating API call with an idempotency key.
    Safe to retry — server deduplicates by key.
    """
    idempotency_key = generate_idempotency_key(operation_name, payload)

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Idempotency-Key": idempotency_key,
                        "X-Operation": operation_name,
                    },
                    timeout=30
                )
                resp.raise_for_status()
                return resp.json()
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if attempt < max_retries - 1:
                print(f"Attempt {attempt + 1} failed: {e}. Retrying with same idempotency key...")
                # Same key = server will return the original result if already processed
            else:
                raise

# Usage: safe to call multiple times
result = await call_with_idempotency(
    "https://api.stripe.com/v1/charges",
    {"amount": 2000, "currency": "usd", "customer": "cus_123"},
    operation_name="charge_customer_order_789"
)
```

### Option 2: Server-side deduplication table

```python
import sqlite3
import hashlib
import json
from datetime import datetime, timedelta

class DeduplicationStore:
    """
    Store completed operation results for deduplication.
    When a retry arrives, return the stored result instead of re-executing.
    """

    def __init__(self, db_path: str = "dedup.db", ttl_hours: int = 24):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.ttl = timedelta(hours=ttl_hours)
        self._init_db()

    def _init_db(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS dedup_results (
                operation_key TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        self.db.commit()

    def get(self, key: str) -> dict | None:
        """Return stored result if it exists and hasn't expired"""
        row = self.db.execute(
            "SELECT result, expires_at FROM dedup_results WHERE operation_key = ?",
            (key,)
        ).fetchone()
        if not row:
            return None
        result_json, expires_at = row
        if datetime.utcnow().isoformat() > expires_at:
            self.db.execute("DELETE FROM dedup_results WHERE operation_key = ?", (key,))
            return None
        return json.loads(result_json)

    def save(self, key: str, result: dict):
        """Save result for future deduplication"""
        now = datetime.utcnow()
        self.db.execute("""
            INSERT OR REPLACE INTO dedup_results (operation_key, result, created_at, expires_at)
            VALUES (?, ?, ?, ?)
        """, (key, json.dumps(result), now.isoformat(), (now + self.ttl).isoformat()))
        self.db.commit()

dedup = DeduplicationStore()

async def idempotent_execute(operation_key: str, execute_fn) -> dict:
    """Execute operation once — return stored result on retry"""
    cached = dedup.get(operation_key)
    if cached:
        print(f"Dedup hit for {operation_key} — returning stored result")
        return cached

    result = await execute_fn()
    dedup.save(operation_key, result)
    return result

# Usage:
result = await idempotent_execute(
    operation_key=f"send_email:order_789:user_456",
    execute_fn=lambda: send_email(user_id=456, order_id=789)
)
```

### Option 3: Check-then-act pattern for database operations

```python
import asyncpg

async def upsert_instead_of_insert(conn: asyncpg.Connection, record: dict) -> dict:
    """
    Use UPSERT so retries don't create duplicates.
    ON CONFLICT DO NOTHING or DO UPDATE depending on use case.
    """
    # WRONG — duplicate on retry:
    # await conn.execute("INSERT INTO orders VALUES ($1, $2)", order_id, data)

    # RIGHT — idempotent via ON CONFLICT:
    row = await conn.fetchrow("""
        INSERT INTO orders (order_id, customer_id, amount, status, created_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (order_id) DO UPDATE
            SET status = EXCLUDED.status
        RETURNING *
    """, record["order_id"], record["customer_id"], record["amount"], record["status"])

    return dict(row)

async def check_before_send_email(conn: asyncpg.Connection, user_id: int, template: str) -> bool:
    """
    Check if email was already sent before sending.
    Returns True if sent now, False if already sent previously.
    """
    # Atomic check-and-mark using INSERT ON CONFLICT
    result = await conn.fetchrow("""
        INSERT INTO sent_emails (user_id, template, sent_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (user_id, template) DO NOTHING
        RETURNING sent_at
    """, user_id, template)

    if result is None:
        print(f"Email {template} to user {user_id} already sent — skipping")
        return False

    await actually_send_email(user_id, template)
    return True
```

### Option 4: Distributed lock for critical one-time operations

```python
import redis
import contextlib

class DistributedLock:
    """Redis-based lock to prevent concurrent duplicate execution"""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    @contextlib.asynccontextmanager
    async def lock(self, key: str, ttl_seconds: int = 60):
        """
        Acquire exclusive lock for an operation.
        Raises if already locked (another agent is running it).
        """
        lock_key = f"lock:{key}"
        acquired = self.redis.set(lock_key, "1", nx=True, ex=ttl_seconds)

        if not acquired:
            raise RuntimeError(
                f"Operation '{key}' is already running or was recently completed. "
                f"Lock expires in {self.redis.ttl(lock_key)}s."
            )
        try:
            yield
        finally:
            self.redis.delete(lock_key)

lock = DistributedLock(redis.Redis())

async def process_payment_once(payment_id: str, amount: int):
    """Process payment — exactly once, even under concurrent retries"""
    async with lock.lock(f"payment:{payment_id}", ttl_seconds=120):
        # Check if already completed (for post-lock verification)
        if await payment_already_processed(payment_id):
            print(f"Payment {payment_id} already processed — skipping")
            return

        result = await charge_card(payment_id, amount)
        await mark_payment_complete(payment_id, result)
        return result
```

### Option 5: Versioned state machine — reject out-of-order retries

```python
from enum import Enum

class OrderStatus(Enum):
    CREATED = "created"
    PAYMENT_PENDING = "payment_pending"
    PAYMENT_COMPLETE = "payment_complete"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"

VALID_TRANSITIONS = {
    OrderStatus.CREATED: [OrderStatus.PAYMENT_PENDING, OrderStatus.CANCELLED],
    OrderStatus.PAYMENT_PENDING: [OrderStatus.PAYMENT_COMPLETE, OrderStatus.CANCELLED],
    OrderStatus.PAYMENT_COMPLETE: [OrderStatus.FULFILLED],
}

async def transition_order(order_id: str, to_status: OrderStatus, conn) -> dict:
    """
    Advance order to next state atomically.
    Rejects transitions that are already done (idempotent retries work correctly).
    """
    order = await conn.fetchrow("SELECT * FROM orders WHERE id = $1 FOR UPDATE", order_id)
    current = OrderStatus(order["status"])

    if current == to_status:
        print(f"Order {order_id} already in {to_status.value} — retry is a no-op")
        return dict(order)

    allowed = VALID_TRANSITIONS.get(current, [])
    if to_status not in allowed:
        raise ValueError(
            f"Invalid transition for order {order_id}: "
            f"{current.value} → {to_status.value} (allowed: {[s.value for s in allowed]})"
        )

    updated = await conn.fetchrow("""
        UPDATE orders SET status = $1, updated_at = NOW()
        WHERE id = $2 AND status = $3
        RETURNING *
    """, to_status.value, order_id, current.value)

    if not updated:
        raise RuntimeError(f"Concurrent modification — order {order_id} changed during transition")

    return dict(updated)
```

### Option 6: System prompt for idempotency awareness

```
System prompt:
"Retry safety rules:

1. BEFORE retrying any operation that has side effects (payment, email, database write,
   API call), ask: 'Could this operation have already succeeded before I got the timeout/error?'

2. If the answer is 'yes': do NOT retry blindly. Instead:
   a. Check the resource state (query the database, call a status endpoint)
   b. Only retry if the state confirms the operation did NOT complete
   c. OR use an idempotency key so the server handles deduplication

3. Operations safe to retry without checking:
   - GET requests (read-only)
   - Operations with explicit idempotency keys
   - Operations protected by ON CONFLICT DO NOTHING

4. Operations NOT safe to retry without state check:
   - Payment charges
   - Email sends
   - Database inserts without ON CONFLICT
   - Webhook triggers
   - Message queue publishes"
```

## Idempotency by Operation Type

| Operation | Safe to retry? | Strategy |
|---|---|---|
| GET request | Yes | Always safe — read-only |
| INSERT without key | No | Add ON CONFLICT or check-first |
| UPSERT (ON CONFLICT) | Yes | Idempotent by design |
| Payment charge | No | Idempotency-Key header required |
| Send email | No | Check sent_emails table first |
| Publish to queue | Depends | Use dedup key if consumer deduplicates |
| DELETE by ID | Yes | Deleting nonexistent record is a no-op |

## Expected Token Savings
Debugging duplicate charge with support team + rollback: ~50,000 tokens
Idempotency key prevents duplicate at the call site: 0 wasted

## Environment
- Any agent calling payment APIs, sending emails, or writing to databases under retry logic
- Source: direct experience; non-idempotent retries cause the most severe production incidents in agent systems
