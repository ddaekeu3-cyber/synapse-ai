---
layout: solution
title: "Agent Doesn't Implement Idempotency — Retry Causes Duplicate Side Effects"
category: general
description: "An agent sends a payment, creates a record, or dispatches a notification. The network request times out. The agent retries. The downstream service receives the request twice — the customer is charged twice, two records are created, or two emails are sent. Without idempotency keys, any retry loop becomes a source of duplicate side effects. The fix is to assign a stable idempotency key to every state-mutating operation."
tags: [reliability, idempotency, retry, duplicates, payments, side-effects, at-most-once, at-least-once]
---

# Agent Doesn't Implement Idempotency — Retry Causes Duplicate Side Effects

## Symptom
- Customer charged twice after network timeout on payment call
- Two welcome emails sent to the same user on retry
- Database ends up with duplicate order records
- Retry after rate limit causes double-booking
- Agent crashes mid-task, restarts, and re-sends all notifications from the beginning
- Webhook handler processes the same event twice because the first ack was lost

## Root Cause
State-mutating operations (create, charge, send, book) are not idempotent by default — calling them twice has double the effect. When agents retry failed operations, they must ensure the second call is a no-op if the first one succeeded. The standard solution is an idempotency key: a stable, deterministic ID derived from the operation's inputs. If the server has already processed a request with that key, it returns the cached result instead of executing again.

## Fix

### Option 1: Idempotency key in every tool call — deterministic key from inputs

```python
import anthropic
import hashlib
import json
import time
import logging
from functools import wraps

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def make_idempotency_key(*args, **kwargs) -> str:
    """
    Create a deterministic idempotency key from the operation's inputs.
    Same inputs → same key → same result on retry.
    """
    payload = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
    return "idem_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


# In-memory store simulating a server-side idempotency cache:
_idempotency_store: dict[str, dict] = {}

def idempotent_call(operation_name: str, key: str, fn, *args, **kwargs) -> dict:
    """
    Execute fn(*args, **kwargs) exactly once for a given idempotency key.
    Subsequent calls with the same key return the cached result.
    """
    if key in _idempotency_store:
        cached = _idempotency_store[key]
        logger.info(f"[idempotent] Cache hit for {operation_name}: key={key}")
        return {**cached, "was_cached": True}

    logger.info(f"[idempotent] Executing {operation_name}: key={key}")
    result = fn(*args, **kwargs)
    _idempotency_store[key] = {**result, "executed_at": time.time(), "was_cached": False}
    return _idempotency_store[key]


# Tool implementations wrapped with idempotency:
def _charge_payment(customer_id: str, amount: float, currency: str) -> dict:
    """Actual payment charge — called at most once per idempotency key."""
    logger.info(f"Charging {customer_id}: {amount} {currency}")
    return {
        "status": "charged",
        "transaction_id": f"txn_{int(time.time())}",
        "customer_id": customer_id,
        "amount": amount
    }

def charge_payment(customer_id: str, amount: float, currency: str = "USD") -> dict:
    """Idempotent payment charge."""
    key = make_idempotency_key("charge_payment", customer_id=customer_id, amount=amount, currency=currency)
    return idempotent_call("charge_payment", key, _charge_payment, customer_id, amount, currency)


def _send_email(to: str, subject: str, body: str) -> dict:
    logger.info(f"Sending email to {to}: {subject}")
    return {"status": "sent", "message_id": f"msg_{int(time.time())}"}

def send_email(to: str, subject: str, body: str) -> dict:
    """Idempotent email send — same recipient + subject = same send."""
    key = make_idempotency_key("send_email", to=to, subject=subject)
    return idempotent_call("send_email", key, _send_email, to, subject, body)


TOOLS = [
    {
        "name": "charge_payment",
        "description": "Charge a customer. Idempotent — safe to retry.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "amount": {"type": "number"},
                "currency": {"type": "string"}
            },
            "required": ["customer_id", "amount"]
        }
    },
    {
        "name": "send_email",
        "description": "Send an email. Idempotent — safe to retry.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"}
            },
            "required": ["to", "subject", "body"]
        }
    }
]


def handle_tool(name: str, inputs: dict) -> str:
    if name == "charge_payment":
        result = charge_payment(**inputs)
    elif name == "send_email":
        result = send_email(**inputs)
    else:
        result = {"error": f"Unknown tool: {name}"}
    return json.dumps(result)


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return response.content[0].text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = handle_tool(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


# Safe to retry — second charge call returns cached result:
r1 = charge_payment("cust_123", 99.99)
r2 = charge_payment("cust_123", 99.99)  # retry after timeout
assert r1["transaction_id"] == r2["transaction_id"]  # same result, no double charge
print(f"r1: {r1}")
print(f"r2 (retry): {r2}")
```

### Option 2: Session-scoped idempotency — stable keys from session + operation

```python
import anthropic
import hashlib
import time
import json
import uuid

client = anthropic.Anthropic()

class IdempotentAgentSession:
    """
    An agent session where every tool call is automatically idempotent.
    Keys are derived from session_id + tool_name + inputs.
    Even if the agent loop restarts, the same calls return cached results.
    """
    def __init__(self, session_id: str | None = None):
        self.session_id = session_id or str(uuid.uuid4())
        self._cache: dict[str, dict] = {}   # in production: Redis/DB
        self._call_sequence = 0

    def _make_key(self, tool_name: str, inputs: dict) -> str:
        """
        Key includes session_id so different sessions don't share cache.
        Key includes inputs so different parameters get different keys.
        """
        payload = json.dumps({
            "session": self.session_id,
            "tool": tool_name,
            "inputs": inputs
        }, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def call_tool(self, tool_name: str, inputs: dict, tool_fn) -> dict:
        """Call a tool idempotently within this session."""
        key = self._make_key(tool_name, inputs)

        if key in self._cache:
            cached = self._cache[key]
            print(f"  [cache hit] {tool_name}({inputs}) → cached at {cached['_cached_at']:.0f}")
            return cached

        self._call_sequence += 1
        print(f"  [execute #{self._call_sequence}] {tool_name}({inputs})")

        result = tool_fn(inputs)
        self._cache[key] = {**result, "_cached_at": time.time(), "_call_seq": self._call_sequence}
        return self._cache[key]

    def run(self, user_message: str, tools: list[dict], tool_implementations: dict) -> str:
        messages = [{"role": "user", "content": user_message}]

        while True:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                tools=tools,
                messages=messages
            )

            if response.stop_reason == "end_turn":
                return response.content[0].text

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    fn = tool_implementations.get(block.name)
                    if fn:
                        result = self.call_tool(block.name, block.input, fn)
                    else:
                        result = {"error": f"Unknown: {block.name}"}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})


# Usage:
session = IdempotentAgentSession(session_id="order-flow-abc123")
# If this session restarts mid-flight, all already-completed tool calls
# return their cached results without re-executing.
```

### Option 3: External idempotency key for third-party APIs (Stripe, etc.)

```python
import anthropic
import hashlib
import json
import os
import time

client = anthropic.Anthropic()

# Many payment and financial APIs accept an idempotency key header.
# Passing a stable key guarantees at-most-once execution server-side.

def charge_stripe(
    customer_id: str,
    amount_cents: int,
    currency: str = "usd",
    idempotency_key: str | None = None
) -> dict:
    """
    Charge via Stripe with idempotency key.
    Stripe deduplicates requests with the same key for 24 hours.
    """
    if idempotency_key is None:
        # Derive key from operation parameters if not provided:
        payload = f"stripe_charge:{customer_id}:{amount_cents}:{currency}"
        idempotency_key = hashlib.sha256(payload.encode()).hexdigest()

    # In production: use stripe library with idempotency_key kwarg:
    # import stripe
    # charge = stripe.PaymentIntent.create(
    #     amount=amount_cents,
    #     currency=currency,
    #     customer=customer_id,
    #     idempotency_key=idempotency_key
    # )

    # Simulated:
    print(f"Stripe charge: customer={customer_id}, amount={amount_cents}, idempotency_key={idempotency_key}")
    return {
        "status": "succeeded",
        "id": f"pi_{idempotency_key[:12]}",
        "amount": amount_cents,
        "idempotency_key": idempotency_key
    }


def send_sendgrid_email(
    to: str,
    subject: str,
    body: str,
    idempotency_key: str | None = None
) -> dict:
    """
    Send email with deduplication key.
    """
    if idempotency_key is None:
        payload = f"email:{to}:{subject}"
        idempotency_key = hashlib.sha256(payload.encode()).hexdigest()

    # In production: include X-Idempotency-Key header in HTTP request
    print(f"SendGrid email: to={to}, key={idempotency_key}")
    return {"status": "sent", "message_id": f"msg_{idempotency_key[:12]}"}


TOOLS = [
    {
        "name": "charge_customer",
        "description": "Charge a customer via Stripe. Idempotent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "amount_cents": {"type": "integer"},
                "reason": {"type": "string"}
            },
            "required": ["customer_id", "amount_cents"]
        }
    }
]


def handle_charge(inputs: dict) -> str:
    # Generate idempotency key from the semantic intent, not wall clock time:
    key_source = f"agent_charge:{inputs['customer_id']}:{inputs['amount_cents']}:{inputs.get('reason', '')}"
    idem_key = hashlib.sha256(key_source.encode()).hexdigest()
    result = charge_stripe(inputs["customer_id"], inputs["amount_cents"], idempotency_key=idem_key)
    return json.dumps(result)
```

### Option 4: Idempotency with SQLite — durable across process restarts

```python
import anthropic
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager

client = anthropic.Anthropic()

class DurableIdempotencyStore:
    """
    SQLite-backed idempotency store.
    Persists across process restarts — safe for long-running agent tasks.
    """
    def __init__(self, db_path: str = "/tmp/agent_idempotency.db", ttl_seconds: int = 86400):
        self.db_path = db_path
        self.ttl = ttl_seconds
        self._init()

    def _init(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS idempotency_cache (
                    key TEXT PRIMARY KEY,
                    result TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT result, created_at FROM idempotency_cache WHERE key = ?",
                (key,)
            ).fetchone()
            if row:
                result, created_at = row
                if time.time() - created_at < self.ttl:
                    return json.loads(result)
                # Expired — delete:
                conn.execute("DELETE FROM idempotency_cache WHERE key = ?", (key,))
        return None

    def set(self, key: str, result: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO idempotency_cache (key, result, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(result), time.time())
            )

    def execute_once(self, key: str, fn, *args, **kwargs) -> dict:
        """Execute fn at most once. Returns cached result on retry."""
        cached = self.get(key)
        if cached is not None:
            print(f"  [durable-cache hit] {key}")
            return {**cached, "_from_cache": True}
        result = fn(*args, **kwargs)
        self.set(key, result)
        return {**result, "_from_cache": False}


# Usage:
store = DurableIdempotencyStore()

def create_subscription(user_id: str, plan: str) -> dict:
    key = hashlib.sha256(f"create_subscription:{user_id}:{plan}".encode()).hexdigest()
    return store.execute_once(
        key,
        lambda: {
            "subscription_id": f"sub_{user_id}_{int(time.time())}",
            "user_id": user_id,
            "plan": plan,
            "status": "active"
        }
    )

# First call: executes
r1 = create_subscription("user_123", "pro")
# Retry after crash: returns cached result
r2 = create_subscription("user_123", "pro")
assert r1["subscription_id"] == r2["subscription_id"]
```

### Option 5: Webhook handler idempotency — deduplicate received events

```python
import anthropic
import hashlib
import time
import json
from collections import OrderedDict

client = anthropic.Anthropic()

class WebhookDeduplicator:
    """
    Deduplicates incoming webhooks by event ID.
    Prevents processing the same event twice (at-least-once delivery).
    """
    def __init__(self, max_size: int = 10_000, ttl_seconds: int = 3600):
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds

    def is_duplicate(self, event_id: str) -> bool:
        now = time.time()
        # Clean up expired entries:
        expired = [k for k, t in self._seen.items() if now - t > self._ttl]
        for k in expired:
            del self._seen[k]

        if event_id in self._seen:
            return True

        # Register as seen:
        self._seen[event_id] = now
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)  # evict oldest

        return False

    def process_if_new(self, event_id: str, fn, *args, **kwargs):
        """Process event only if not seen before."""
        if self.is_duplicate(event_id):
            print(f"  [webhook-dedup] Skipping duplicate event: {event_id}")
            return None
        return fn(*args, **kwargs)


deduplicator = WebhookDeduplicator()

def handle_webhook(payload: dict) -> dict | None:
    """Process incoming webhook — idempotent via event_id deduplication."""
    event_id = payload.get("event_id") or hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()

    return deduplicator.process_if_new(
        event_id,
        process_event,
        payload
    )


def process_event(payload: dict) -> dict:
    """Actual event processing — called at most once per event."""
    print(f"Processing event: {payload.get('type')} for {payload.get('user_id')}")
    # ... call LLM, update DB, send notification ...
    return {"processed": True, "event_id": payload.get("event_id")}


# Simulate at-least-once delivery (same event delivered twice):
event = {"event_id": "evt_12345", "type": "payment.succeeded", "user_id": "user_123"}
r1 = handle_webhook(event)   # → processes
r2 = handle_webhook(event)   # → skipped (duplicate)
assert r1 is not None
assert r2 is None
```

### Option 6: Conditional operation — check-then-act with state verification

```python
import anthropic
import json
import time

client = anthropic.Anthropic()

# For operations where idempotency keys aren't supported by the downstream service,
# use check-then-act: verify the desired state before attempting to create it.

def ensure_subscription_exists(user_id: str, plan: str) -> dict:
    """
    Idempotent subscription creation via check-then-act.
    If the subscription already exists, returns it without creating a new one.
    """
    # Step 1: Check if already in desired state:
    existing = get_subscription(user_id)
    if existing and existing.get("plan") == plan and existing.get("status") == "active":
        return {**existing, "created": False, "reason": "already_exists"}

    # Step 2: Create only if not already there:
    new_sub = create_subscription_api(user_id, plan)
    return {**new_sub, "created": True}


def get_subscription(user_id: str) -> dict | None:
    """Check existing subscription (read-only, always safe to call)."""
    # Simulated: returns None if no subscription exists
    return None

def create_subscription_api(user_id: str, plan: str) -> dict:
    """Create subscription in external system."""
    return {
        "subscription_id": f"sub_{user_id}_{int(time.time())}",
        "user_id": user_id,
        "plan": plan,
        "status": "active"
    }


TOOLS = [
    {
        "name": "ensure_subscription",
        "description": "Create a subscription if it doesn't already exist. Idempotent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "plan": {"type": "string", "enum": ["free", "pro", "enterprise"]}
            },
            "required": ["user_id", "plan"]
        }
    }
]


def handle_tool(name: str, inputs: dict) -> str:
    if name == "ensure_subscription":
        result = ensure_subscription_exists(inputs["user_id"], inputs["plan"])
        return json.dumps(result)
    return json.dumps({"error": f"Unknown tool: {name}"})


def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            tools=TOOLS,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return response.content[0].text
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": handle_tool(block.name, block.input)
                })
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
```

## Idempotency Strategy by Operation Type

| Operation | Strategy | Key Source |
|---|---|---|
| Payment charge | API idempotency key header | customer_id + amount + reason |
| Email send | Deduplication by recipient + subject | to + subject |
| Record creation | Check-then-act | resource identifier |
| Webhook processing | Event ID deduplication | event_id from payload |
| Multi-step task | Session-scoped cache | session_id + tool_name + inputs |
| Long-running job | Durable SQLite/Redis cache | job_id + operation |

## Expected Token Savings
No direct token savings. However, idempotency failures in financial or user-communication contexts cause refunds, support costs, and user trust loss that far exceed engineering effort. The deterministic key approach (Option 1) adds zero latency on the first call and microseconds on cache hits.

## Environment
- Any agent with write/mutate operations (payments, emails, records, bookings); critical for agents with retry logic or running in at-least-once delivery environments; mandatory for webhook handlers; the SQLite store (Option 4) is appropriate when durability across process restarts is needed; in-memory stores (Options 1, 2) are sufficient for single-session idempotency; use the third-party API idempotency key approach (Option 3) whenever the downstream service supports it
