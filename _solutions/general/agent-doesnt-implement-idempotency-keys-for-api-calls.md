---
layout: solution
title: "Agent Doesn't Implement Idempotency Keys for API Calls"
category: general
description: "Agent retries failed API calls without idempotency keys — a network timeout on a payment charge or order creation triggers a retry that creates a duplicate record, charges the user twice, or sends the same email twice."
tags: [reliability, idempotency, retries, api, safety]
---

## Symptom

An agent submits a payment charge that times out at 28 seconds. It retries. The first request had already succeeded server-side — now the user is charged twice:

```
[agent] POST /v1/charges {"amount": 9900, "currency": "usd"} → timeout (30s)
[agent] Retry 1: POST /v1/charges {"amount": 9900, "currency": "usd"} → 200 OK (charge_id: ch_002)
[payment-api] charge ch_001 succeeded (from first request)
[payment-api] charge ch_002 succeeded (from retry)
[user] Two charges on their credit card statement
```

## Root Cause

The agent retries on failure without distinguishing *network failure* (server never received the request) from *response failure* (server processed the request but the response was lost). Without an idempotency key, the server has no way to deduplicate:

```python
import anthropic
import httpx

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: retry without idempotency key
def charge_user(amount: int, currency: str) -> dict:
    for attempt in range(3):
        try:
            resp = httpx.post(
                "https://api.stripe.com/v1/charges",
                data={"amount": amount, "currency": currency},
                timeout=30.0
            )
            return resp.json()
        except httpx.TimeoutException:
            continue  # ← Retry blindly — may create duplicate charge
    raise RuntimeError("Charge failed after 3 attempts")
```

---

## Fix

### Option 1 — Generate a UUID idempotency key per logical operation

Assign a unique key to each logical operation before the first attempt. Reuse the same key on every retry. The server deduplicates using the key.

```python
import anthropic
import httpx
import uuid
import time

client = anthropic.Anthropic(api_key="sk-live-...")


def charge_user(amount: int, currency: str, idempotency_key: str | None = None) -> dict:
    """
    Charge a user. Pass the same idempotency_key on retries to prevent duplicates.
    If omitted, generates a new key (safe for first call only).
    """
    key = idempotency_key or str(uuid.uuid4())

    for attempt in range(3):
        try:
            resp = httpx.post(
                "https://api.stripe.com/v1/charges",
                data={"amount": amount, "currency": currency},
                headers={
                    "Authorization": "Bearer sk-live-...",
                    "Idempotency-Key": key,  # ← Same key on every retry
                },
                timeout=30.0
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (409, 422):
                raise  # Conflict/validation — don't retry
            raise

    raise RuntimeError("Charge failed")


# Generate the key ONCE per user action, reuse across all retries
key = str(uuid.uuid4())
result = charge_user(9900, "usd", idempotency_key=key)
print(f"Charge result: {result}")

# Expected Token Savings: no duplicate-charge debugging sessions, no refund flows
# Environment: agents calling payment APIs, order APIs, email APIs, or any non-idempotent endpoint
```

---

### Option 2 — Deterministic idempotency key from request content

Derive the key from a hash of the request parameters so the same logical operation always produces the same key, even across process restarts.

```python
import anthropic
import hashlib
import json
import httpx
import time

client = anthropic.Anthropic(api_key="sk-live-...")


def make_idempotency_key(operation: str, params: dict) -> str:
    """
    Derive a deterministic idempotency key from the operation name and params.
    Same logical request always produces the same key — survives process restart.
    """
    canonical = json.dumps({"op": operation, **params}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def send_email(to: str, subject: str, body: str) -> dict:
    """Send email with idempotency key — safe to retry, won't send twice."""
    key = make_idempotency_key("send_email", {"to": to, "subject": subject})

    for attempt in range(4):
        try:
            resp = httpx.post(
                "https://api.sendgrid.com/v3/mail/send",
                json={"to": [{"email": to}], "subject": subject, "content": [{"type": "text/plain", "value": body}]},
                headers={
                    "Authorization": "Bearer SG.live-...",
                    "X-Idempotency-Key": key,
                },
                timeout=20.0
            )
            if resp.status_code == 202:
                return {"status": "sent", "key": key}
            resp.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError):
            wait = min(2 ** attempt, 30)
            print(f"[email] Attempt {attempt + 1} failed — retrying in {wait}s (key={key})")
            time.sleep(wait)

    raise RuntimeError(f"Email send failed after 4 attempts (key={key})")


# Even if the process restarts mid-retry, the same key is derived
result = send_email("alice@example.com", "Your order is confirmed", "Order #12345 has been placed.")
print(result)

# Expected Token Savings: deterministic keys survive restarts → no re-send investigation needed
# Environment: email agents, notification agents, webhook-dispatch agents
```

---

### Option 3 — Store pending operations in a local journal before executing

Before calling any non-idempotent API, write the operation to a local journal with a pre-assigned key. On startup, replay un-acknowledged operations using their original keys.

```python
import anthropic
import json
import uuid
import time
import httpx
from pathlib import Path
from dataclasses import dataclass, asdict

client = anthropic.Anthropic(api_key="sk-live-...")

JOURNAL_PATH = Path("/tmp/agent-operations-journal.jsonl")


@dataclass
class PendingOperation:
    op_id: str
    operation: str
    params: dict
    idempotency_key: str
    status: str  # "pending" | "succeeded" | "failed"
    created_at: float
    completed_at: float | None = None


class IdempotentExecutor:
    """Journal operations before executing; replay on restart using stored keys."""

    def __init__(self, journal_path: Path = JOURNAL_PATH):
        self._journal = journal_path
        self._ops: dict[str, PendingOperation] = {}
        self._load_journal()

    def _load_journal(self) -> None:
        if not self._journal.exists():
            return
        for line in self._journal.read_text().splitlines():
            if line.strip():
                data = json.loads(line)
                op = PendingOperation(**data)
                self._ops[op.op_id] = op

    def _append_journal(self, op: PendingOperation) -> None:
        with self._journal.open("a") as f:
            f.write(json.dumps(asdict(op)) + "\n")

    def _update_status(self, op_id: str, status: str) -> None:
        op = self._ops[op_id]
        op.status = status
        op.completed_at = time.time()
        # Rewrite journal (in prod: use SQLite or Redis for atomic updates)
        entries = [json.dumps(asdict(o)) for o in self._ops.values()]
        self._journal.write_text("\n".join(entries) + "\n")

    def execute(self, operation: str, params: dict, executor_fn) -> dict:
        """
        Execute an operation with guaranteed idempotency.
        On restart, pending operations are replayed with their original keys.
        """
        # Check if already succeeded
        for op in self._ops.values():
            if op.operation == operation and op.params == params and op.status == "succeeded":
                print(f"[journal] Already succeeded: {operation} (key={op.idempotency_key})")
                return {"status": "already_done", "key": op.idempotency_key}

        # Create new pending operation
        op_id = str(uuid.uuid4())
        key = str(uuid.uuid4())
        op = PendingOperation(op_id, operation, params, key, "pending", time.time())
        self._ops[op_id] = op
        self._append_journal(op)

        # Execute with retries using the stored key
        for attempt in range(3):
            try:
                result = executor_fn(params, key)
                self._update_status(op_id, "succeeded")
                print(f"[journal] Succeeded: {operation} (key={key})")
                return result
            except Exception as e:
                print(f"[journal] Attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(2)

        self._update_status(op_id, "failed")
        raise RuntimeError(f"Operation {operation} failed (key={key})")

    def replay_pending(self) -> None:
        """Replay any operations that were pending when the process last died."""
        pending = [op for op in self._ops.values() if op.status == "pending"]
        if pending:
            print(f"[journal] Replaying {len(pending)} pending operation(s)")
        for op in pending:
            print(f"[journal] Replaying {op.operation} with key={op.idempotency_key}")
            # In production: call executor_fn with op.idempotency_key


executor = IdempotentExecutor()
executor.replay_pending()

def fake_charge(params: dict, key: str) -> dict:
    return {"charged": True, "amount": params["amount"], "key": key}

result = executor.execute("charge", {"amount": 9900, "currency": "usd"}, fake_charge)
print(result)

# Expected Token Savings: journal prevents re-charging on restart → no charge-reversal workflow
# Environment: agents running financial operations; long-running pipelines prone to crashes
```

---

### Option 4 — LLM-generated operation ID injected into tool arguments

When the model generates tool calls for non-idempotent operations, require it to include an `operation_id` field. The framework maps this to an idempotency key.

```python
import anthropic
import uuid
import json
import time

client = anthropic.Anthropic(api_key="sk-live-...")

# Track operation_id → result mappings in memory (use Redis/DB in production)
_completed_ops: dict[str, dict] = {}


def execute_tool_call(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call, deduplicating by operation_id if present."""
    op_id = tool_input.get("operation_id")

    if op_id and op_id in _completed_ops:
        print(f"[dedup] Returning cached result for operation_id={op_id}")
        return json.dumps(_completed_ops[op_id])

    # Simulate tool execution
    time.sleep(0.1)
    result = {"tool": tool_name, "input": tool_input, "status": "success", "timestamp": time.time()}

    if op_id:
        _completed_ops[op_id] = result

    return json.dumps(result)


tools = [
    {
        "name": "send_notification",
        "description": "Send a notification to a user. Include operation_id to prevent duplicate sends.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "message": {"type": "string"},
                "operation_id": {
                    "type": "string",
                    "description": "Unique ID for this operation. Reuse on retry to prevent duplicates. Generate a UUID if not provided."
                }
            },
            "required": ["user_id", "message", "operation_id"]
        }
    }
]

SYSTEM = """When calling send_notification or any write tool, always include an operation_id.
Generate a UUID (e.g., 'op-<random>') for new operations.
If retrying a failed call, reuse the SAME operation_id.
Never call a write tool without an operation_id."""


def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for tu in tool_uses:
            result = execute_tool_call(tu.name, tu.input)
            print(f"[tool] {tu.name}({tu.input.get('operation_id', 'NO_ID')}): {result[:60]}")
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


result = run_agent("Send a welcome notification to user u_123")
print(result)

# Expected Token Savings: LLM-injected IDs prevent duplicates without extra round-trips
# Environment: agentic pipelines where models invoke write tools
```

---

### Option 5 — Per-session idempotency scope with TTL

Scope idempotency keys to a session ID + operation fingerprint. Expire them after a TTL so the same user can retry the same operation in a new session.

```python
import anthropic
import hashlib
import json
import time
from collections import OrderedDict

client = anthropic.Anthropic(api_key="sk-live-...")

TTL_SECONDS = 3600  # Keys valid for 1 hour


class SessionScopedIdempotency:
    """Idempotency store scoped by session; keys expire after TTL."""

    def __init__(self, ttl: float = TTL_SECONDS, max_size: int = 10_000):
        self._store: OrderedDict[str, tuple[dict, float]] = OrderedDict()
        self._ttl = ttl
        self._max = max_size

    def _key(self, session_id: str, operation: str, params: dict) -> str:
        raw = json.dumps({"session": session_id, "op": operation, **params}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, session_id: str, operation: str, params: dict) -> dict | None:
        k = self._key(session_id, operation, params)
        if k in self._store:
            result, stored_at = self._store[k]
            if time.monotonic() - stored_at < self._ttl:
                return result
            del self._store[k]
        return None

    def put(self, session_id: str, operation: str, params: dict, result: dict) -> None:
        k = self._key(session_id, operation, params)
        self._store[k] = (result, time.monotonic())
        self._store.move_to_end(k)
        if len(self._store) > self._max:
            self._store.popitem(last=False)

    def execute(self, session_id: str, operation: str, params: dict, fn) -> dict:
        cached = self.get(session_id, operation, params)
        if cached is not None:
            print(f"[idem] Dedup: {operation} in session={session_id}")
            return cached

        result = fn(params)
        self.put(session_id, operation, params, result)
        return result


idem = SessionScopedIdempotency()


def fake_create_order(params: dict) -> dict:
    return {"order_id": f"ord_{int(time.time())}", **params}


session = "sess_abc123"

# First call — executes
r1 = idem.execute(session, "create_order", {"item": "book", "qty": 1}, fake_create_order)
print(f"First:  {r1}")

# Retry (same session + params) — returns cached result, no duplicate
r2 = idem.execute(session, "create_order", {"item": "book", "qty": 1}, fake_create_order)
print(f"Retry:  {r2}")

print(f"Same order_id: {r1['order_id'] == r2['order_id']}")

# Expected Token Savings: session-scoped dedup prevents duplicate orders without external DB
# Environment: chatbot agents; session-based workflows with retry buttons in UI
```

---

### Option 6 — Claude generates the idempotency strategy for a given API

Use Claude to analyse an API spec and recommend where idempotency keys are required, then generate the wrapper code automatically.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def analyse_api_for_idempotency(api_spec: str) -> str:
    """Ask Claude to identify which endpoints need idempotency keys and generate wrappers."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system="""You are an API reliability expert.
Given an API spec, identify:
1. Which endpoints are non-idempotent (POST/PUT that create resources or trigger side effects).
2. Which already support idempotency keys (header or param).
3. Which need a client-side deduplication wrapper.

Then generate Python wrapper functions for any non-idempotent endpoints that lack built-in idempotency support.
Each wrapper must:
- Accept an optional `idempotency_key: str` parameter.
- If key is None, generate a UUID internally.
- Log the key with every attempt.
- Cache the result on success and return the cached result on retry with the same key.""",
        messages=[{
            "role": "user",
            "content": f"Analyse this API and generate idempotency wrappers:\n\n{api_spec}"
        }]
    )
    return response.content[0].text.strip()


api_spec = """
POST /v1/invoices        — Creates a new invoice (supports Idempotency-Key header)
POST /v1/webhooks        — Registers a webhook URL (no idempotency support)
POST /v1/emails/send     — Sends a transactional email (no idempotency support)
GET  /v1/invoices/{id}   — Retrieves invoice (read-only, safe)
DELETE /v1/invoices/{id} — Deletes invoice (already idempotent by REST convention)
"""

analysis = analyse_api_for_idempotency(api_spec)
print(analysis)

# Expected Token Savings: generated wrappers prevent duplicates across entire API surface
# Environment: agents integrating new third-party APIs; automated API client generation pipelines
```

---

## Comparison

| Option | Key Strategy | Survives Restart | External Store | TTL Support | Complexity |
|--------|-------------|-----------------|----------------|-------------|------------|
| 1 | UUID per operation | No | No | No | Low |
| 2 | Deterministic hash | Yes | No | No | Low |
| 3 | Journal + replay | Yes | No (file) | No | Medium |
| 4 | LLM-injected op ID | No | In-memory | No | Low |
| 5 | Session-scoped cache | No | In-memory | Yes | Low |
| 6 | Auto-generated wrappers | Depends | Depends | Depends | Medium |

**Recommended starting point:** Option 1 for any new API integration — generate a UUID before the first attempt and pass it as a header on every retry. Takes 3 lines of code and prevents an entire class of duplicate-operation bugs. Upgrade to Option 2 (deterministic key) for agents that restart frequently and need replay safety without external state.
