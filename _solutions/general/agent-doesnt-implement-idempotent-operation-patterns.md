---
layout: solution
title: "Agent Doesn't Implement Idempotent Operation Patterns"
category: general
description: "Agent operations that modify state — sending emails, creating records, charging payments — must be idempotent: safe to retry without causing duplicate effects. Without idempotency keys and deduplication logic, network retries and agent restarts cause double sends, double charges, and duplicate records."
tags: [reliability, idempotency, retry-safety, deduplication, state-management, distributed-systems]
---

## Problem

When an agent sends a message, creates a record, or triggers a side effect, network failures may leave the result ambiguous: did the operation succeed before the failure, or not? Without idempotency, retrying the operation causes duplicate effects. A customer gets charged twice, two support tickets are created, or the same email is sent multiple times. Idempotency keys ensure that repeating an operation is safe.

## Solutions

### Option 1: Idempotency Key Cache for Tool Calls

```python
import anthropic
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional

client = anthropic.Anthropic()

@dataclass
class IdempotencyRecord:
    key: str
    operation: str
    result: Any
    created_at: float
    ttl_seconds: float = 86400  # 24 hours

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

# In-memory idempotency store (use Redis in production)
_idempotency_store: dict[str, IdempotencyRecord] = {}

def make_idempotency_key(operation: str, params: dict) -> str:
    """Generate deterministic key from operation + params."""
    content = operation + str(sorted(params.items()))
    return hashlib.sha256(content.encode()).hexdigest()[:16]

def idempotent_execute(
    operation_name: str,
    params: dict,
    execute_fn,
    idempotency_key: Optional[str] = None,
    ttl_seconds: float = 86400
) -> tuple[Any, bool]:
    """
    Execute an operation exactly once, even if called multiple times.
    Returns (result, was_duplicate).
    """
    key = idempotency_key or make_idempotency_key(operation_name, params)

    # Check for existing result
    existing = _idempotency_store.get(key)
    if existing and not existing.is_expired:
        print(f"[Idempotent] DUPLICATE detected for '{operation_name}' (key: {key[:8]})")
        return existing.result, True

    # Execute fresh
    result = execute_fn(**params)

    # Store result
    _idempotency_store[key] = IdempotencyRecord(
        key=key,
        operation=operation_name,
        result=result,
        created_at=time.time(),
        ttl_seconds=ttl_seconds
    )
    print(f"[Idempotent] Executed '{operation_name}' (key: {key[:8]})")
    return result, False

# --- Simulated side-effect operations ---
_emails_sent: list[dict] = []
_records_created: list[dict] = []

def send_email(to: str, subject: str, body: str) -> dict:
    record = {"to": to, "subject": subject, "body": body[:50], "sent_at": time.time()}
    _emails_sent.append(record)
    return {"email_id": f"email_{len(_emails_sent)}", "status": "sent"}

def create_record(entity_type: str, data: dict) -> dict:
    record = {"type": entity_type, "data": data, "id": f"rec_{len(_records_created)+1}"}
    _records_created.append(record)
    return record

# --- Agent with idempotent tool execution ---
def agent_send_notification(user_id: str, message: str, request_id: str) -> dict:
    """Send user notification — idempotent by request_id."""
    result, duplicate = idempotent_execute(
        operation_name="send_notification",
        params={"to": f"user_{user_id}@example.com", "subject": "Agent Notification", "body": message},
        execute_fn=send_email,
        idempotency_key=f"notif_{user_id}_{request_id}"
    )
    return {**result, "duplicate": duplicate}

# Simulate retry scenario: same request called 3 times
request_id = "req_abc123"
for attempt in range(3):
    result = agent_send_notification("42", "Your task is complete.", request_id)
    print(f"Attempt {attempt+1}: {result}")

print(f"\nTotal emails actually sent: {len(_emails_sent)} (should be 1)")

# Expected Token Savings: Deduplication prevents agent from making redundant API calls
# Environment: ANTHROPIC_API_KEY not required for this example; use Redis TTL in production
```

### Option 2: Database-Backed Idempotency with SQLite

```python
import anthropic
import sqlite3
import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Optional

client = anthropic.Anthropic()

DB_PATH = "/tmp/idempotency.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_records (
                key TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                params_hash TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                call_count INTEGER DEFAULT 1
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON idempotency_records(expires_at)")

init_db()

def get_or_execute(
    key: str,
    operation: str,
    params: dict,
    execute_fn,
    ttl_seconds: float = 3600
) -> tuple[Any, bool]:
    """
    Atomically check-and-set using SQLite — safe across process restarts.
    Returns (result, is_duplicate).
    """
    params_hash = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
    now = time.time()

    with sqlite3.connect(DB_PATH) as conn:
        # Try to fetch existing non-expired record
        row = conn.execute(
            "SELECT result, call_count FROM idempotency_records WHERE key=? AND expires_at > ?",
            (key, now)
        ).fetchone()

        if row:
            # Update hit count
            conn.execute("UPDATE idempotency_records SET call_count=call_count+1 WHERE key=?", (key,))
            return json.loads(row[0]), True

        # No record — execute and store
        try:
            result = execute_fn(**params)
            result_json = json.dumps(result)
            conn.execute(
                "INSERT OR REPLACE INTO idempotency_records VALUES (?,?,?,?,?,?,?)",
                (key, operation, params_hash, result_json, now, now + ttl_seconds, 1)
            )
            return result, False
        except Exception as e:
            # Don't store failed operations
            raise

def cleanup_expired(max_age: float = 86400 * 7):
    """Remove expired records to keep DB clean."""
    cutoff = time.time() - max_age
    with sqlite3.connect(DB_PATH) as conn:
        deleted = conn.execute(
            "DELETE FROM idempotency_records WHERE expires_at < ?", (cutoff,)
        ).rowcount
    return deleted

# --- Tool functions with idempotency ---
_ticket_counter = 0

def create_support_ticket(user_id: str, issue: str, priority: str) -> dict:
    global _ticket_counter
    _ticket_counter += 1
    return {
        "ticket_id": f"TKT-{_ticket_counter:04d}",
        "user_id": user_id,
        "issue": issue[:50],
        "priority": priority,
        "created_at": time.time()
    }

def agent_create_ticket_for_user(
    user_id: str,
    issue_description: str,
    session_id: str
) -> dict:
    """Create support ticket — exactly once per session."""
    # Key combines user + session to prevent duplicates within the same session
    idempotency_key = f"ticket_{user_id}_{session_id}"

    result, is_duplicate = get_or_execute(
        key=idempotency_key,
        operation="create_support_ticket",
        params={"user_id": user_id, "issue": issue_description, "priority": "medium"},
        execute_fn=create_support_ticket,
        ttl_seconds=3600
    )

    action = "DUPLICATE — returning existing" if is_duplicate else "CREATED new"
    print(f"[{action}] ticket {result['ticket_id']} for user {user_id}")
    return result

# Simulate agent retry (e.g., from task queue retry)
session = "sess_xyz789"
for attempt in range(3):
    ticket = agent_create_ticket_for_user("user_42", "Cannot log in to account", session)
    print(f"  Attempt {attempt+1}: ticket_id={ticket['ticket_id']}")

print(f"\nTotal tickets created: {_ticket_counter} (should be 1)")
print(f"Cleaned up {cleanup_expired()} expired records")

# Expected Token Savings: SQLite persistence survives process restarts; prevents duplicate operations
# Environment: ANTHROPIC_API_KEY not required; writes to /tmp/idempotency.db
```

### Option 3: Versioned State Machine with Idempotent Transitions

```python
import anthropic
import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

client = anthropic.Anthropic()

class OrderState(str, Enum):
    CREATED = "created"
    PROCESSING = "processing"
    PAYMENT_REQUESTED = "payment_requested"
    PAYMENT_CONFIRMED = "payment_confirmed"
    FULFILLED = "fulfilled"
    FAILED = "failed"

@dataclass
class Order:
    order_id: str
    state: OrderState
    version: int      # Monotonic version number
    user_id: str
    amount_cents: int
    history: list[dict] = field(default_factory=list)

    def transition_to(self, new_state: OrderState, metadata: dict = None) -> bool:
        """
        Idempotent state transition: same (from, to) pair is safe to apply multiple times.
        Returns True if transition was applied, False if already in target state.
        """
        if self.state == new_state:
            print(f"[StateMachine] NOOP: order {self.order_id} already in {new_state.value}")
            return False  # Already in target state — idempotent

        # Validate allowed transitions
        allowed = {
            OrderState.CREATED: [OrderState.PROCESSING, OrderState.FAILED],
            OrderState.PROCESSING: [OrderState.PAYMENT_REQUESTED, OrderState.FAILED],
            OrderState.PAYMENT_REQUESTED: [OrderState.PAYMENT_CONFIRMED, OrderState.FAILED],
            OrderState.PAYMENT_CONFIRMED: [OrderState.FULFILLED, OrderState.FAILED],
        }
        if new_state not in allowed.get(self.state, []):
            raise ValueError(f"Invalid transition: {self.state} → {new_state}")

        prev_state = self.state
        self.state = new_state
        self.version += 1
        self.history.append({
            "from": prev_state.value,
            "to": new_state.value,
            "version": self.version,
            "timestamp": time.time(),
            "metadata": metadata or {}
        })
        print(f"[StateMachine] {self.order_id}: {prev_state.value} → {new_state.value} (v{self.version})")
        return True

# Order store
_orders: dict[str, Order] = {}
_payment_calls: list[str] = []

def get_or_create_order(order_id: str, user_id: str, amount_cents: int) -> Order:
    if order_id not in _orders:
        _orders[order_id] = Order(
            order_id=order_id, state=OrderState.CREATED,
            version=1, user_id=user_id, amount_cents=amount_cents
        )
    return _orders[order_id]

def process_payment(order_id: str, amount_cents: int) -> dict:
    """Simulate payment — would use Stripe idempotency_key in production."""
    _payment_calls.append(order_id)
    return {"payment_id": f"pay_{len(_payment_calls)}", "status": "confirmed"}

def agent_process_order(order_id: str, user_id: str, amount_cents: int) -> dict:
    """
    Idempotent order processing pipeline.
    Safe to call multiple times — picks up where it left off.
    """
    order = get_or_create_order(order_id, user_id, amount_cents)

    # Each step is idempotent: skip if already past this state
    if order.state == OrderState.CREATED:
        order.transition_to(OrderState.PROCESSING)

    if order.state == OrderState.PROCESSING:
        order.transition_to(OrderState.PAYMENT_REQUESTED, {"amount": amount_cents})

    if order.state == OrderState.PAYMENT_REQUESTED:
        # Payment call — idempotent by order_id
        payment = process_payment(order_id, amount_cents)
        order.transition_to(OrderState.PAYMENT_CONFIRMED, payment)

    if order.state == OrderState.PAYMENT_CONFIRMED:
        order.transition_to(OrderState.FULFILLED, {"shipped": True})

    return {
        "order_id": order.order_id,
        "state": order.state.value,
        "version": order.version,
        "payment_calls_total": len(_payment_calls)
    }

# Simulate retry: call 3 times for same order
order_id = "ord_20240315_001"
for attempt in range(3):
    result = agent_process_order(order_id, "user_42", 9999)
    print(f"  Attempt {attempt+1}: state={result['state']}, payment_calls={result['payment_calls_total']}")

print(f"\nPayment called {len(_payment_calls)} time(s) (should be 1)")

# Expected Token Savings: State machine prevents redundant LLM calls for already-completed steps
# Environment: ANTHROPIC_API_KEY not required
```

### Option 4: Idempotent Agent Tool Wrapper

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Any

client = anthropic.Anthropic()

@dataclass
class ToolCallRecord:
    call_id: str
    tool_name: str
    input_hash: str
    result: Any
    executed_at: float
    ttl: float

_tool_cache: dict[str, ToolCallRecord] = {}

def idempotent_tool(tool_name: str, ttl_seconds: float = 300):
    """
    Decorator that makes any tool function idempotent.
    Same tool + same inputs within TTL returns cached result.
    """
    def decorator(fn: Callable) -> Callable:
        def wrapper(**kwargs) -> Any:
            input_hash = hashlib.sha256(
                json.dumps({k: str(v) for k, v in sorted(kwargs.items())}).encode()
            ).hexdigest()[:12]
            cache_key = f"{tool_name}:{input_hash}"
            now = time.time()

            cached = _tool_cache.get(cache_key)
            if cached and (now - cached.executed_at) < cached.ttl:
                print(f"  [Tool:{tool_name}] CACHED (age: {now - cached.executed_at:.1f}s)")
                return cached.result

            print(f"  [Tool:{tool_name}] EXECUTING")
            result = fn(**kwargs)
            _tool_cache[cache_key] = ToolCallRecord(
                call_id=cache_key,
                tool_name=tool_name,
                input_hash=input_hash,
                result=result,
                executed_at=now,
                ttl=ttl_seconds
            )
            return result
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator

# Define idempotent tools
_send_count = 0
_lookup_count = 0

@idempotent_tool("send_message", ttl_seconds=3600)
def send_message(channel: str, text: str) -> dict:
    global _send_count
    _send_count += 1
    return {"message_id": f"msg_{_send_count}", "channel": channel, "delivered": True}

@idempotent_tool("lookup_user", ttl_seconds=300)
def lookup_user(user_id: str) -> dict:
    global _lookup_count
    _lookup_count += 1
    return {"user_id": user_id, "name": "Alice", "email": f"{user_id}@example.com"}

def run_agent_with_idempotent_tools(task: str, user_id: str, channel: str) -> str:
    """Agent that uses idempotent tools — safe to restart mid-execution."""
    # These calls are safe to retry: same inputs → same result, no duplicate side effects
    user = lookup_user(user_id=user_id)
    user2 = lookup_user(user_id=user_id)  # Duplicate — returns cached

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": f"Write a 1-sentence completion message for: {task}"}]
    )
    message_text = response.content[0].text.strip()

    result1 = send_message(channel=channel, text=f"Hi {user['name']}: {message_text}")
    result2 = send_message(channel=channel, text=f"Hi {user['name']}: {message_text}")  # Duplicate

    return f"Delivered: {result1['message_id']} (send_count={_send_count}, lookup_count={_lookup_count})"

# Simulate agent restart / retry
for attempt in range(3):
    print(f"\n--- Agent attempt {attempt+1} ---")
    result = run_agent_with_idempotent_tools("summarize the Q3 report", "user_99", "#notifications")
    print(f"Result: {result}")

print(f"\nActual sends: {_send_count} (should be 1)")
print(f"Actual lookups: {_lookup_count} (should be 1)")

# Expected Token Savings: Tool caching saves N-1 tool round-trips on retries
# Environment: ANTHROPIC_API_KEY required for agent call
```

### Option 5: Distributed Idempotency with Conditional Writes

```python
import anthropic
import hashlib
import time
import json
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

# Simulates a distributed KV store (Redis/DynamoDB in production)
_kv_store: dict[str, dict] = {}
_kv_lock_holders: dict[str, str] = {}

def kv_set_if_not_exists(key: str, value: dict, ttl: float) -> bool:
    """Atomic conditional write — only succeeds if key doesn't exist."""
    if key in _kv_store and time.time() < _kv_store[key].get("expires_at", 0):
        return False  # Key exists and not expired
    _kv_store[key] = {**value, "expires_at": time.time() + ttl}
    return True

def kv_get(key: str) -> Optional[dict]:
    entry = _kv_store.get(key)
    if entry and time.time() < entry.get("expires_at", 0):
        return entry
    return None

@dataclass
class DistributedIdempotencyResult:
    operation_id: str
    result: dict
    is_duplicate: bool
    lock_acquired: bool

def distributed_idempotent_execute(
    operation_id: str,
    operation_type: str,
    params: dict,
    execute_fn,
    ttl_seconds: float = 3600
) -> DistributedIdempotencyResult:
    """
    Distributed idempotency using check-and-set semantics.
    Suitable for multi-instance agent deployments.
    """
    result_key = f"result:{operation_id}"
    lock_key = f"lock:{operation_id}"

    # Check if result already exists
    existing = kv_get(result_key)
    if existing:
        return DistributedIdempotencyResult(
            operation_id=operation_id,
            result=existing.get("result", {}),
            is_duplicate=True,
            lock_acquired=False
        )

    # Try to acquire lock (prevents concurrent execution of same operation)
    instance_id = f"agent_{int(time.time()*1000) % 10000}"
    lock_acquired = kv_set_if_not_exists(lock_key, {"holder": instance_id}, ttl=30)

    if not lock_acquired:
        # Another instance is executing — wait briefly and check for result
        time.sleep(0.1)
        existing = kv_get(result_key)
        if existing:
            return DistributedIdempotencyResult(
                operation_id=operation_id,
                result=existing.get("result", {}),
                is_duplicate=True,
                lock_acquired=False
            )
        raise RuntimeError(f"Lock contention on operation {operation_id}")

    try:
        result = execute_fn(**params)
        # Store result atomically
        kv_set_if_not_exists(result_key, {"result": result, "operation": operation_type}, ttl_seconds)
        print(f"[DistributedIdempotent] EXECUTED {operation_type} ({operation_id[:8]})")
        return DistributedIdempotencyResult(operation_id, result, False, True)
    finally:
        # Release lock
        if _kv_store.get(lock_key, {}).get("holder") == instance_id:
            del _kv_store[lock_key]

# Tool
_charges: list[dict] = []

def charge_payment(amount_cents: int, currency: str, user_id: str) -> dict:
    charge_id = f"ch_{len(_charges)+1:04d}"
    _charges.append({"id": charge_id, "amount": amount_cents, "currency": currency, "user": user_id})
    return {"charge_id": charge_id, "status": "succeeded", "amount": amount_cents}

# Simulate 3 agents all trying to charge the same payment simultaneously
operation_id = hashlib.sha256(f"payment_user42_order99".encode()).hexdigest()[:16]

for i in range(3):
    try:
        result = distributed_idempotent_execute(
            operation_id=operation_id,
            operation_type="charge_payment",
            params={"amount_cents": 4999, "currency": "USD", "user_id": "user_42"},
            execute_fn=charge_payment
        )
        status = "DUPLICATE" if result.is_duplicate else "NEW"
        print(f"Agent {i+1}: [{status}] charge_id={result.result.get('charge_id')}")
    except RuntimeError as e:
        print(f"Agent {i+1}: LOCK_CONTENTION — {e}")

print(f"\nTotal charges made: {len(_charges)} (should be 1)")

# Expected Token Savings: Prevents duplicate charges — financial cost saved >> token cost
# Environment: ANTHROPIC_API_KEY not required; use Redis SETNX in production
```

### Option 6: LLM-Aware Idempotency with Semantic Deduplication

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class SemanticIdempotencyRecord:
    canonical_request: str
    result: str
    created_at: float
    request_count: int = 1
    ttl: float = 1800

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl

# Store by canonical hash
_semantic_cache: dict[str, SemanticIdempotencyRecord] = {}

def canonicalize_request(prompt: str) -> str:
    """Use LLM to extract canonical form of a request for deduplication."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": f"""Extract the canonical intent from this request as a short key.
Strip filler words, normalize numbers, lowercase.
Request: "{prompt}"
Canonical key (10-30 chars):"""}]
    )
    return response.content[0].text.strip().lower()[:40]

def semantic_idempotent_call(prompt: str, system: str = "") -> dict:
    """
    Deduplicate semantically similar requests.
    'What time is it?' and 'Tell me the current time please' → same canonical key.
    """
    # Fast hash check first
    fast_key = hashlib.sha256(prompt.lower().strip().encode()).hexdigest()[:12]
    existing = _semantic_cache.get(fast_key)
    if existing and not existing.is_expired:
        existing.request_count += 1
        print(f"[SemanticDedup] EXACT match (count: {existing.request_count})")
        return {"result": existing.result, "source": "exact_cache", "count": existing.request_count}

    # Semantic canonicalization
    canonical = canonicalize_request(prompt)
    canonical_key = hashlib.sha256(canonical.encode()).hexdigest()[:12]

    existing_canonical = _semantic_cache.get(canonical_key)
    if existing_canonical and not existing_canonical.is_expired:
        existing_canonical.request_count += 1
        # Also cache under fast key for future exact matches
        _semantic_cache[fast_key] = existing_canonical
        print(f"[SemanticDedup] SEMANTIC match: '{canonical}' (count: {existing_canonical.request_count})")
        return {"result": existing_canonical.result, "source": "semantic_cache",
                "canonical": canonical, "count": existing_canonical.request_count}

    # No match — execute
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    result = response.content[0].text
    record = SemanticIdempotencyRecord(canonical_request=canonical, result=result, created_at=time.time())
    _semantic_cache[fast_key] = record
    _semantic_cache[canonical_key] = record
    print(f"[SemanticDedup] NEW execution: '{canonical}'")
    return {"result": result, "source": "live", "canonical": canonical, "count": 1}

# Test semantic deduplication
test_prompts = [
    "What are the top 3 Python web frameworks?",
    "List the 3 most popular Python web frameworks",  # Semantically same
    "Can you tell me the top three Python web frameworks?",  # Semantically same
    "What is the capital of France?",   # Different topic
    "What is France's capital city?",   # Semantically same as above
]

for prompt in test_prompts:
    result = semantic_idempotent_call(prompt, system="You are a concise assistant.")
    print(f"  [{result['source']}] {prompt[:50]} → {result['result'][:60]}\n")

# Expected Token Savings: Semantic dedup collapses N similar requests to 1 LLM call
# Environment: ANTHROPIC_API_KEY required; canonicalization adds ~30 tokens overhead
```

## Comparison

| Option | Storage | Cross-Process Safe | Handles Concurrent Calls | Best Use Case |
|--------|---------|-------------------|--------------------------|---------------|
| In-Memory Key Cache | RAM | No | Partially | Single-process agents, short-lived ops |
| SQLite-Persisted | Disk | Yes | Yes (file lock) | Single-machine, persistent across restarts |
| Versioned State Machine | RAM/DB | With DB | Yes | Complex multi-step workflows |
| Decorator-Based Tool Cache | RAM | No | No | Simple tool-level deduplication |
| Distributed Conditional Write | External KV | Yes | Yes (lock) | Multi-instance agent deployments |
| Semantic Deduplication | RAM | No | Partially | LLM calls with paraphrased inputs |
