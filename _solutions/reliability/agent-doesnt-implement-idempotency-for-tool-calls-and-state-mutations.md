---
layout: solution
title: "Agent Doesn't Implement Idempotency for Tool Calls and State Mutations"
category: reliability
description: "Ensure that retried tool calls and state mutations never cause duplicate effects — using idempotency keys, deduplication stores, and at-most-once execution semantics."
tags: [reliability, idempotency, deduplication, retries, tool-calls, state-mutations, at-most-once]
---

## Problem

Agents retry tool calls when they time out or encounter transient errors. Without idempotency, a payment gets charged twice, a message gets sent twice, a database record gets inserted twice. The agent thinks it's being resilient — it's actually creating duplicate side effects. Every retry on a non-idempotent operation is a potential data corruption event.

```python
# Naive: retry without idempotency — the tool runs twice
async def send_email_tool(to: str, subject: str, body: str) -> str:
    for attempt in range(3):
        try:
            return email_service.send(to, subject, body)  # runs again on retry!
        except TimeoutError:
            continue
    raise Exception("Failed after 3 attempts")
```

## Solution Options

### Option 1: Idempotency Key Store with TTL

Generate a deterministic idempotency key from the tool call arguments. Store executed keys in a TTL-based store. Skip execution if the key already exists; return the cached result.

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass

@dataclass
class IdempotentResult:
    key: str
    result: str
    executed_at: float
    from_cache: bool

class IdempotencyStore:
    def __init__(self, ttl_seconds: float = 86400.0):  # 24h default
        self._store: dict[str, IdempotentResult] = {}
        self._ttl = ttl_seconds

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v.executed_at > self._ttl]
        for k in expired:
            del self._store[k]

    def get(self, key: str) -> IdempotentResult | None:
        self._evict_expired()
        result = self._store.get(key)
        if result and time.time() - result.executed_at <= self._ttl:
            return result
        return None

    def set(self, key: str, result: str) -> IdempotentResult:
        record = IdempotentResult(key=key, result=result, executed_at=time.time(), from_cache=False)
        self._store[key] = record
        return record

def make_idempotency_key(tool_name: str, tool_args: dict) -> str:
    canonical = json.dumps({"tool": tool_name, "args": tool_args}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]

idempotency_store = IdempotencyStore(ttl_seconds=3600)

def idempotent_tool_call(tool_name: str, tool_args: dict, executor: callable) -> IdempotentResult:
    key = make_idempotency_key(tool_name, tool_args)
    cached = idempotency_store.get(key)
    if cached:
        cached.from_cache = True
        print(f"[IDEMPOTENT] Cache hit: {tool_name}({key[:8]}...) — returning cached result")
        return cached
    print(f"[IDEMPOTENT] Executing: {tool_name}({key[:8]}...)")
    result_str = executor(tool_args)
    return idempotency_store.set(key, result_str)


client = anthropic.Anthropic()

# Simulate idempotent tool execution
def send_notification_executor(args: dict) -> str:
    print(f"  [TOOL] Actually sending notification to {args['email']}: {args['subject']}")
    return json.dumps({"status": "sent", "message_id": "msg_12345"})

# First call — executes
r1 = idempotent_tool_call("send_notification",
                           {"email": "user@example.com", "subject": "Welcome!"},
                           send_notification_executor)
print(f"Result 1: {r1.result} (from_cache={r1.from_cache})")

# Retry with same args — returns cached result, does NOT send again
r2 = idempotent_tool_call("send_notification",
                           {"email": "user@example.com", "subject": "Welcome!"},
                           send_notification_executor)
print(f"Result 2: {r2.result} (from_cache={r2.from_cache})")

# Different args — executes again (correct behavior)
r3 = idempotent_tool_call("send_notification",
                           {"email": "admin@example.com", "subject": "Alert!"},
                           send_notification_executor)
print(f"Result 3: {r3.result} (from_cache={r3.from_cache})")

# Expected Token Savings: Deduplication adds 0 tokens; prevents duplicate charges/emails/inserts on retry
# Environment: ANTHROPIC_API_KEY
```

### Option 2: Idempotency Keys with External API Header Injection

Many APIs support idempotency keys natively (Stripe, Twilio, etc.). Generate and inject idempotency keys into external API calls so the API provider handles deduplication.

```python
import anthropic
import hashlib
import json
import time
import uuid
from dataclasses import dataclass

@dataclass
class APICallRecord:
    idempotency_key: str
    endpoint: str
    payload: dict
    response: dict
    executed_at: float

# Local record of issued idempotency keys (for logging and audit)
ISSUED_KEYS: dict[str, APICallRecord] = {}

def generate_api_idempotency_key(endpoint: str, payload: dict, session_id: str = "") -> str:
    """Generate a stable idempotency key for an API call."""
    canonical = json.dumps({
        "session": session_id,
        "endpoint": endpoint,
        "payload": payload,
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]

def idempotent_api_call(
    endpoint: str,
    payload: dict,
    session_id: str = "",
    max_retries: int = 3,
) -> dict:
    """Call an API with an idempotency key, with retry on transient errors."""
    ikey = generate_api_idempotency_key(endpoint, payload, session_id)

    # Check if we already have a record for this key
    if ikey in ISSUED_KEYS:
        record = ISSUED_KEYS[ikey]
        print(f"[IDEMPOTENCY] Returning existing result for key={ikey[:12]}...")
        return record.response

    for attempt in range(max_retries):
        try:
            # Simulate API call with idempotency key in header
            simulated_response = _simulate_api_call(endpoint, payload, ikey)
            record = APICallRecord(
                idempotency_key=ikey,
                endpoint=endpoint,
                payload=payload,
                response=simulated_response,
                executed_at=time.time(),
            )
            ISSUED_KEYS[ikey] = record
            print(f"[IDEMPOTENCY] Executed {endpoint} (attempt {attempt+1}) key={ikey[:12]}...")
            return simulated_response
        except ConnectionError as e:
            if attempt == max_retries - 1:
                raise
            print(f"[RETRY] attempt {attempt+1} failed: {e}")
    return {}

def _simulate_api_call(endpoint: str, payload: dict, idempotency_key: str) -> dict:
    """Simulates a payment API that respects idempotency keys."""
    print(f"  [API] POST {endpoint} Idempotency-Key: {idempotency_key[:12]}... payload={payload}")
    return {
        "status": "success",
        "transaction_id": f"txn_{idempotency_key[:8]}",
        "amount": payload.get("amount"),
        "idempotency_key": idempotency_key,
    }


client = anthropic.Anthropic()

# Simulate an agent that calls a payment tool twice (e.g., due to timeout retry)
TOOLS = [{
    "name": "charge_payment",
    "description": "Charge a customer's payment method",
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "amount_cents": {"type": "integer"},
            "description": {"type": "string"},
        },
        "required": ["customer_id", "amount_cents"],
    },
}]

r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    tools=TOOLS,
    messages=[{"role": "user", "content": "Charge customer cust_123 $49.99 for Pro subscription"}],
)

if r.stop_reason == "tool_use":
    tool_use = next(b for b in r.content if b.type == "tool_use")
    session_id = "session_abc"

    # First call
    result1 = idempotent_api_call("POST /payments/charge", tool_use.input, session_id)
    print(f"First call: {result1}")

    # Simulated retry (e.g., due to timeout)
    result2 = idempotent_api_call("POST /payments/charge", tool_use.input, session_id)
    print(f"Retry result: {result2}")

    print(f"\nSame transaction_id: {result1['transaction_id'] == result2['transaction_id']}")

# Expected Token Savings: Idempotency key injection adds 0 tokens; prevents double-charges worth real money
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Write-Ahead Log for At-Most-Once State Mutations

Before mutating state, write the intended operation to a WAL. On execution, mark the WAL entry as complete. On retry, check WAL first — skip execution if already completed.

```python
import anthropic
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

class WALStatus(Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class WALEntry:
    operation_id: str
    operation_type: str
    payload: dict
    status: WALStatus
    result: str | None
    created_at: float
    completed_at: float | None

WAL_DB = Path("wal.db")

def init_wal() -> None:
    conn = sqlite3.connect(str(WAL_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wal (
            operation_id TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            created_at REAL NOT NULL,
            completed_at REAL
        )
    """)
    conn.commit()
    conn.close()

init_wal()

def wal_write(operation_id: str, operation_type: str, payload: dict) -> WALEntry:
    conn = sqlite3.connect(str(WAL_DB))
    conn.execute(
        "INSERT OR IGNORE INTO wal (operation_id, operation_type, payload_json, status, created_at) "
        "VALUES (?, ?, ?, 'pending', ?)",
        (operation_id, operation_type, json.dumps(payload), time.time()),
    )
    conn.commit()
    conn.close()
    return WALEntry(operation_id, operation_type, payload, WALStatus.PENDING, None, time.time(), None)

def wal_get(operation_id: str) -> WALEntry | None:
    conn = sqlite3.connect(str(WAL_DB))
    row = conn.execute(
        "SELECT operation_id, operation_type, payload_json, status, result, created_at, completed_at "
        "FROM wal WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return WALEntry(row[0], row[1], json.loads(row[2]), WALStatus(row[3]), row[4], row[5], row[6])

def wal_complete(operation_id: str, result: str) -> None:
    conn = sqlite3.connect(str(WAL_DB))
    conn.execute(
        "UPDATE wal SET status='completed', result=?, completed_at=? WHERE operation_id=?",
        (result, time.time(), operation_id),
    )
    conn.commit()
    conn.close()

def wal_fail(operation_id: str, error: str) -> None:
    conn = sqlite3.connect(str(WAL_DB))
    conn.execute(
        "UPDATE wal SET status='failed', result=?, completed_at=? WHERE operation_id=?",
        (error, time.time(), operation_id),
    )
    conn.commit()
    conn.close()

def idempotent_mutate(operation_id: str, operation_type: str, payload: dict, executor: callable) -> str:
    # Check WAL first
    existing = wal_get(operation_id)
    if existing and existing.status == WALStatus.COMPLETED:
        print(f"[WAL] Already completed: {operation_id[:8]} — returning cached result")
        return existing.result

    # Write intent to WAL
    wal_write(operation_id, operation_type, payload)
    print(f"[WAL] Executing: {operation_type} {operation_id[:8]}")
    try:
        result = executor(payload)
        wal_complete(operation_id, result)
        return result
    except Exception as e:
        wal_fail(operation_id, str(e))
        raise


client = anthropic.Anthropic()

def database_insert_executor(payload: dict) -> str:
    print(f"  [DB] INSERT INTO orders VALUES({payload})")
    return json.dumps({"order_id": f"ord_{payload.get('product_id', 'unknown')}_001", "status": "created"})

# First attempt — executes
op_id = hashlib.sha256(json.dumps({"customer": "user_123", "product": "premium"}, sort_keys=True).encode()).hexdigest()[:16] if False else str(uuid.uuid4())[:16]

import hashlib
op_id = hashlib.sha256(json.dumps({"customer": "user_123", "product": "premium"}, sort_keys=True).encode()).hexdigest()[:16]

r1 = idempotent_mutate(op_id, "create_order", {"customer_id": "user_123", "product_id": "premium"}, database_insert_executor)
print(f"Result 1: {r1}")

# Retry — reads from WAL, skips execution
r2 = idempotent_mutate(op_id, "create_order", {"customer_id": "user_123", "product_id": "premium"}, database_insert_executor)
print(f"Result 2 (from WAL): {r2}")

# Expected Token Savings: WAL adds 0 tokens; SQLite ensures durability across process restarts
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Conditional Execution with Pre-Check Assertions

Before executing a state mutation, run a pre-check to verify the precondition still holds. If the state has already been mutated (by a prior retry), return the existing state without executing again.

```python
import anthropic
import json
import time
from dataclasses import dataclass
from typing import Callable

@dataclass
class ConditionalExecutionResult:
    executed: bool
    result: str
    precondition_met: bool
    state_before: dict

# Simulated state database
STATE_DB: dict[str, dict] = {
    "user_123": {"subscription": "free", "payment_status": "none", "updated_at": 0},
    "order_456": {"status": "pending", "charged": False, "charged_at": None},
}

def check_precondition(entity_id: str, entity_type: str, expected_state: dict) -> tuple[bool, dict]:
    current = STATE_DB.get(entity_id, {})
    for key, expected_value in expected_state.items():
        if current.get(key) != expected_value:
            return False, current
    return True, current

def conditional_execute(
    entity_id: str,
    entity_type: str,
    precondition: dict,      # what state must be true to execute
    postcondition: dict,     # what state to check if already done
    executor: Callable[[str], dict],
) -> ConditionalExecutionResult:
    # Check if already in post-state (already executed)
    post_met, current_state = check_precondition(entity_id, entity_type, postcondition)
    if post_met:
        print(f"[CONDITIONAL] {entity_id}: already in post-state — skipping execution")
        return ConditionalExecutionResult(
            executed=False,
            result=json.dumps(current_state),
            precondition_met=True,
            state_before=current_state,
        )

    # Check precondition (safe to execute)
    pre_met, current_state = check_precondition(entity_id, entity_type, precondition)
    if not pre_met:
        print(f"[CONDITIONAL] {entity_id}: precondition not met — cannot execute")
        return ConditionalExecutionResult(
            executed=False,
            result=json.dumps({"error": "precondition_not_met", "current_state": current_state}),
            precondition_met=False,
            state_before=current_state,
        )

    print(f"[CONDITIONAL] {entity_id}: precondition met — executing")
    new_state = executor(entity_id)
    STATE_DB[entity_id].update(new_state)
    STATE_DB[entity_id]["updated_at"] = time.time()
    return ConditionalExecutionResult(
        executed=True,
        result=json.dumps(new_state),
        precondition_met=True,
        state_before=current_state,
    )


client = anthropic.Anthropic()

def charge_order_executor(order_id: str) -> dict:
    print(f"  [PAYMENT] Charging order {order_id}")
    return {"status": "paid", "charged": True, "charged_at": time.time()}

# First execution — order is pending, not yet charged
r1 = conditional_execute(
    "order_456",
    "order",
    precondition={"status": "pending", "charged": False},  # must be in this state
    postcondition={"charged": True},                        # if already in this state, skip
    executor=charge_order_executor,
)
print(f"First: executed={r1.executed} result={r1.result[:80]}\n")

# Retry — order is now "charged=True", postcondition met — skips execution
r2 = conditional_execute(
    "order_456",
    "order",
    precondition={"status": "pending", "charged": False},
    postcondition={"charged": True},
    executor=charge_order_executor,
)
print(f"Retry: executed={r2.executed} result={r2.result[:80]}")

# Expected Token Savings: Pre-check adds 0 tokens; prevents double-charges by inspecting state before acting
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Async Idempotency Middleware for Tool Execution Pipeline

Wrap an async tool execution pipeline with idempotency middleware. Each tool call passes through the middleware which checks, executes, and caches atomically.

```python
import anthropic
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Callable, Awaitable

@dataclass
class ToolCallRecord:
    key: str
    tool_name: str
    result: str
    executed_at: float
    execution_time_ms: float

class AsyncIdempotencyMiddleware:
    def __init__(self, ttl_seconds: float = 3600.0):
        self._cache: dict[str, ToolCallRecord] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ttl = ttl_seconds

    def _make_key(self, tool_name: str, tool_input: dict) -> str:
        canonical = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:20]

    def _is_valid(self, record: ToolCallRecord) -> bool:
        return time.time() - record.executed_at < self._ttl

    async def execute(
        self,
        tool_name: str,
        tool_input: dict,
        executor: Callable[[dict], Awaitable[str]],
    ) -> tuple[str, bool]:  # (result, from_cache)
        key = self._make_key(tool_name, tool_input)

        # Fast path: check cache without lock
        if key in self._cache and self._is_valid(self._cache[key]):
            print(f"[MIDDLEWARE] Cache hit: {tool_name} key={key[:10]}")
            return self._cache[key].result, True

        # Slow path: acquire per-key lock to prevent concurrent duplicate execution
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        async with self._locks[key]:
            # Double-check after acquiring lock
            if key in self._cache and self._is_valid(self._cache[key]):
                return self._cache[key].result, True

            t0 = time.monotonic()
            result = await executor(tool_input)
            elapsed_ms = (time.monotonic() - t0) * 1000

            self._cache[key] = ToolCallRecord(
                key=key, tool_name=tool_name,
                result=result, executed_at=time.time(),
                execution_time_ms=elapsed_ms,
            )
            print(f"[MIDDLEWARE] Executed: {tool_name} key={key[:10]} in {elapsed_ms:.0f}ms")
            return result, False

middleware = AsyncIdempotencyMiddleware()
client = anthropic.AsyncAnthropic()

async def search_tool(args: dict) -> str:
    await asyncio.sleep(0.1)  # simulate API latency
    return json.dumps({"results": [f"Result for {args['query']}"], "count": 1})

async def email_tool(args: dict) -> str:
    print(f"  [EMAIL] Sending to {args['to']}: {args['subject']}")
    return json.dumps({"sent": True, "message_id": "msg_001"})

async def main():
    # Simulate 3 concurrent agents all calling the same search
    search_args = {"query": "Python async programming"}
    tasks = [
        middleware.execute("search", search_args, search_tool),
        middleware.execute("search", search_args, search_tool),
        middleware.execute("search", search_args, search_tool),
    ]
    results = await asyncio.gather(*tasks)
    for i, (result, from_cache) in enumerate(results):
        print(f"  Agent {i+1}: from_cache={from_cache}")

    # Email — should execute once even if called twice
    print("\nSending email:")
    r1 = await middleware.execute("send_email", {"to": "user@example.com", "subject": "Hello"}, email_tool)
    r2 = await middleware.execute("send_email", {"to": "user@example.com", "subject": "Hello"}, email_tool)
    print(f"Sent once: {r1[1]=} (from_cache) | {r2[1]=} (from_cache)")

asyncio.run(main())

# Expected Token Savings: Per-key locking prevents thundering-herd duplicate executions; 0 token overhead
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Event Sourcing with Idempotent Event Application

Model all state changes as events in an append-only log. Apply events idempotently — replaying the same event twice produces the same final state.

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

@dataclass
class Event:
    event_id: str
    event_type: str
    aggregate_id: str
    payload: dict
    timestamp: float
    version: int  # monotonically increasing per aggregate

EVENT_LOG: list[Event] = []
APPLIED_EVENT_IDS: set[str] = set()

def append_event(event_type: str, aggregate_id: str, payload: dict) -> Event:
    existing_versions = [e.version for e in EVENT_LOG if e.aggregate_id == aggregate_id]
    version = max(existing_versions, default=0) + 1
    event = Event(
        event_id=str(uuid.uuid4())[:8],
        event_type=event_type,
        aggregate_id=aggregate_id,
        payload=payload,
        timestamp=time.time(),
        version=version,
    )
    EVENT_LOG.append(event)
    return event

def apply_event_idempotent(event: Event, state: dict) -> dict:
    """Apply event only if not already applied. Returns updated state."""
    if event.event_id in APPLIED_EVENT_IDS:
        print(f"[EVENT] Skipping already-applied event: {event.event_id} ({event.event_type})")
        return state

    new_state = dict(state)
    if event.event_type == "subscription_upgraded":
        new_state["tier"] = event.payload["new_tier"]
        new_state["upgraded_at"] = event.timestamp
    elif event.event_type == "payment_charged":
        new_state["balance"] = new_state.get("balance", 0) - event.payload["amount_cents"]
        new_state["last_payment"] = event.timestamp
    elif event.event_type == "notification_sent":
        sent = new_state.get("notifications_sent", [])
        sent.append(event.payload["notification_type"])
        new_state["notifications_sent"] = sent
    else:
        new_state.update(event.payload)

    APPLIED_EVENT_IDS.add(event.event_id)
    print(f"[EVENT] Applied: {event.event_type} v{event.version} → {new_state}")
    return new_state

def rebuild_aggregate_state(aggregate_id: str) -> dict:
    """Rebuild state by replaying all events — idempotent."""
    events = sorted(
        [e for e in EVENT_LOG if e.aggregate_id == aggregate_id],
        key=lambda e: e.version,
    )
    state: dict = {}
    seen: set[str] = set()
    for event in events:
        if event.event_id not in seen:
            state = apply_event_idempotent(event, state)
            seen.add(event.event_id)
    return state


client = anthropic.Anthropic()

# Emit events (each event represents a tool call result)
user_id = "user_123"
e1 = append_event("subscription_upgraded", user_id, {"new_tier": "pro", "amount_cents": 4999})
e2 = append_event("payment_charged", user_id, {"amount_cents": 4999, "description": "Pro plan"})
e3 = append_event("notification_sent", user_id, {"notification_type": "upgrade_confirmation"})

# Build state
state = rebuild_aggregate_state(user_id)
print(f"\nFinal state: {state}\n")

# Simulate replay (e.g., crash recovery) — events applied again idempotently
print("=== Replaying events (crash recovery simulation) ===")
APPLIED_EVENT_IDS.clear()  # reset applied set to simulate fresh process
state2 = rebuild_aggregate_state(user_id)
print(f"\nReplayed state: {state2}")
print(f"States match: {state == state2}")

# Expected Token Savings: Event sourcing adds 0 tokens; enables crash recovery and audit trail simultaneously
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Storage | Granularity | Crash Safe | Best For |
|--------|---------|------------|-----------|----------|
| 1. Key Store with TTL | In-memory dict | Per-call | No | Simple retry deduplication |
| 2. API Header Keys | External API + local log | Per-API-call | Partial | Payment APIs (Stripe, Twilio) |
| 3. Write-Ahead Log | SQLite | Per-operation | Yes | Database mutations, orders |
| 4. Conditional Pre-Check | In-memory state | Per-entity | Depends on state | State machine transitions |
| 5. Async Middleware | In-memory + locks | Per-tool-call | No | High-concurrency tool pipelines |
| 6. Event Sourcing | Append-only log | Per-event | Yes | Audit trails, complex state |

**Recommended**: Option 2 (API headers) when targeting APIs with native idempotency support. Option 3 (WAL) for local state mutations. Option 6 (event sourcing) for systems that need full audit trails and crash recovery.
