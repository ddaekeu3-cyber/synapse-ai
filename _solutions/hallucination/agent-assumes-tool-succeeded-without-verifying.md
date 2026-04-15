---
layout: solution
title: "Agent Assumes Tool Call Succeeded Without Verifying the Result"
category: hallucination
description: "Agent calls a write tool. The tool returns no error. Agent proceeds assuming the write succeeded. The write actually failed silently — the tool swallowed the error. Subsequent steps depend on data that was never written. Agent reports success on an operation that failed."
tags: [hallucination, tool-failure, verification, side-effects, write, database, confirmation, silent-failure]
---

# Agent Assumes Tool Call Succeeded Without Verifying the Result

## Symptom
- Agent calls `write_to_database(record)`, tool returns `{"status": "ok"}`, record is never in DB
- Agent sends email via tool, email silently fails, agent says "email sent successfully"
- Tool call returns without error but the side effect never happened
- Agent creates a file, subsequent read of the file fails — file was never created
- Agent reports "order placed" but no order exists in the system
- Multi-step workflow: step 3 fails because step 1's output wasn't actually written

## Root Cause
Agents treat the absence of an error as confirmation of success. Tool return values are often optimistic — the tool may return `{"status": "ok"}` before actually confirming the write committed, or it may catch and swallow internal errors. Language models also have a bias toward optimism: they tend to interpret ambiguous tool results as success. Without an explicit verification step that independently confirms the side effect occurred, the agent cannot distinguish between "succeeded" and "succeeded silently but did nothing."

## Fix

### Option 1: Verify side effects with a read-after-write

```python
import asyncio
import httpx

async def write_and_verify(
    write_fn,
    verify_fn,
    write_args: dict,
    max_verify_attempts: int = 3,
    verify_delay: float = 0.5
) -> dict:
    """
    Write data and then independently verify it was committed.
    Treats unverifiable writes as failures.
    """
    # Step 1: Perform the write
    write_result = await write_fn(**write_args)
    print(f"Write returned: {write_result}")

    # Step 2: Independently verify the write succeeded
    for attempt in range(max_verify_attempts):
        await asyncio.sleep(verify_delay * (attempt + 1))

        try:
            verified = await verify_fn(**write_args)
            if verified:
                print(f"Write verified on attempt {attempt + 1}")
                return {"status": "confirmed", "write_result": write_result, "verified": True}
        except Exception as e:
            print(f"Verification attempt {attempt + 1} failed: {e}")

    raise RuntimeError(
        f"Write appeared to succeed (returned: {write_result}) but "
        f"could not be verified after {max_verify_attempts} attempts. "
        f"The write may have failed silently."
    )

# Example: write a user record and verify it exists
async def write_user_record(user_id: str, data: dict) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.example.com/users",
            json={"id": user_id, **data},
            timeout=30.0
        )
        response.raise_for_status()
        return response.json()

async def verify_user_exists(user_id: str, **kwargs) -> bool:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.example.com/users/{user_id}",
            timeout=10.0
        )
        return response.status_code == 200

# Usage — never assume the write worked:
result = await write_and_verify(
    write_fn=write_user_record,
    verify_fn=verify_user_exists,
    write_args={"user_id": "user_123", "data": {"name": "Alice", "email": "alice@example.com"}}
)
print(f"User record confirmed: {result['verified']}")
```

### Option 2: Tool wrappers that return verifiable receipts

```python
from dataclasses import dataclass
from typing import Any
import hashlib
import time

@dataclass
class OperationReceipt:
    """
    Proof that an operation completed.
    Contains enough information to independently verify success.
    """
    operation: str
    operation_id: str       # Idempotency key / transaction ID
    timestamp: float
    resource_id: str | None  # ID of the created/updated resource
    checksum: str | None     # Hash of written data for verification
    status: str              # "pending", "committed", "failed"

    def verify_data_integrity(self, expected_data: dict) -> bool:
        """Verify the data that should have been written"""
        if not self.checksum:
            return False
        expected_hash = hashlib.sha256(str(sorted(expected_data.items())).encode()).hexdigest()[:16]
        return self.checksum == expected_hash

def create_db_write_tool():
    """
    Database write tool that returns a verifiable receipt.
    """
    async def write_record(table: str, record: dict) -> OperationReceipt:
        import uuid

        operation_id = str(uuid.uuid4())
        record_with_id = {"id": operation_id, **record}
        checksum = hashlib.sha256(str(sorted(record.items())).encode()).hexdigest()[:16]

        try:
            # Perform the actual write
            async with db_pool.acquire() as conn:
                await conn.execute(
                    f"INSERT INTO {table} VALUES ($1, $2)",
                    operation_id,
                    json.dumps(record)
                )
                await conn.execute("COMMIT")  # Explicit commit — don't assume auto-commit

            receipt = OperationReceipt(
                operation=f"INSERT INTO {table}",
                operation_id=operation_id,
                timestamp=time.time(),
                resource_id=operation_id,
                checksum=checksum,
                status="committed"
            )

            # Immediate read-back verification
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT id FROM {table} WHERE id = $1",
                    operation_id
                )
                if not row:
                    receipt.status = "failed"
                    raise RuntimeError(
                        f"Write to {table} appeared to succeed but record not found on immediate read-back"
                    )

            return receipt

        except Exception as e:
            return OperationReceipt(
                operation=f"INSERT INTO {table}",
                operation_id=operation_id,
                timestamp=time.time(),
                resource_id=None,
                checksum=None,
                status="failed"
            )

    return write_record
```

### Option 3: Agent tool use with explicit verification steps

```python
import anthropic
import json

client = anthropic.Anthropic()

# Tools include verification counterparts for every write operation
TOOLS_WITH_VERIFICATION = [
    {
        "name": "send_email",
        "description": "Send an email to a recipient",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"}
            },
            "required": ["to", "subject", "body"]
        }
    },
    {
        "name": "check_email_delivery",
        "description": "Check if a previously sent email was delivered. Call this after send_email to confirm delivery.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The message_id returned by send_email"}
            },
            "required": ["message_id"]
        }
    },
    {
        "name": "create_order",
        "description": "Create a new order in the system",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {"type": "array"},
                "customer_id": {"type": "string"}
            },
            "required": ["items", "customer_id"]
        }
    },
    {
        "name": "get_order",
        "description": "Retrieve an order by ID to verify it was created. Use after create_order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"}
            },
            "required": ["order_id"]
        }
    }
]

VERIFICATION_SYSTEM = """You are an agent that always verifies side effects.

After ANY write operation (send, create, update, delete):
1. Call the corresponding verification tool to confirm the operation succeeded
2. Do NOT report success until the verification tool confirms it
3. If verification fails, retry the write once before escalating

Write tool → Verification tool mapping:
- send_email → check_email_delivery (use the message_id from send_email)
- create_order → get_order (use the order_id from create_order)
- write_file → read_file (read the file back and check content)
- update_record → get_record (fetch the record and confirm the change)

Never say "I sent the email" or "order created" without first running the verification tool."""
```

### Option 4: Idempotency keys — detect and retry failed writes

```python
import uuid
import asyncio
import time
from dataclasses import dataclass

@dataclass
class WriteOperation:
    idempotency_key: str
    operation: str
    payload: dict
    created_at: float = None
    completed_at: float = None
    status: str = "pending"  # pending, committed, failed
    result: dict = None

class IdempotentWriter:
    """
    Write operations with idempotency keys.
    Same key = same operation — safe to retry without duplicating.
    Tracks operation state to detect silent failures.
    """

    def __init__(self, storage):
        self.storage = storage  # Redis, DB, etc.

    async def write(
        self,
        operation: str,
        payload: dict,
        idempotency_key: str = None,
        timeout: float = 30.0
    ) -> WriteOperation:
        key = idempotency_key or str(uuid.uuid4())

        # Check for existing operation (idempotent retry)
        existing = await self.storage.get(f"op:{key}")
        if existing and existing["status"] == "committed":
            print(f"Idempotent: operation {key} already committed — returning cached result")
            return WriteOperation(**existing)

        op = WriteOperation(
            idempotency_key=key,
            operation=operation,
            payload=payload,
            created_at=time.time()
        )

        # Record intent before attempting write
        await self.storage.set(f"op:{key}", {"status": "pending", **op.__dict__}, ttl=3600)

        try:
            result = await asyncio.wait_for(
                self._perform_write(operation, payload),
                timeout=timeout
            )
            op.result = result
            op.status = "committed"
            op.completed_at = time.time()

            # Verify the write committed
            verified = await self._verify(operation, payload, result)
            if not verified:
                op.status = "failed"
                op.result = {"error": "Write not confirmed by verification"}
                await self.storage.set(f"op:{key}", op.__dict__, ttl=3600)
                raise RuntimeError(f"Write {key} not verified after completion")

            await self.storage.set(f"op:{key}", op.__dict__, ttl=3600)
            return op

        except Exception as e:
            op.status = "failed"
            op.result = {"error": str(e)}
            await self.storage.set(f"op:{key}", op.__dict__, ttl=3600)
            raise

    async def _perform_write(self, operation: str, payload: dict) -> dict:
        """Override to implement actual write"""
        raise NotImplementedError

    async def _verify(self, operation: str, payload: dict, result: dict) -> bool:
        """Override to implement write verification"""
        return True  # Default: trust the write
```

### Option 5: Post-operation assertions

```python
from typing import Callable, Awaitable

class AssertionError(Exception):
    pass

async def with_post_assertions(
    operation_fn: Callable[[], Awaitable[dict]],
    assertions: list[tuple[str, Callable[[], Awaitable[bool]]]],
    operation_name: str = "operation"
) -> dict:
    """
    Run an operation and then verify a list of named assertions.
    Fails with clear error if any assertion is not met.
    """
    # Run the operation
    result = await operation_fn()
    print(f"{operation_name} returned: {result}")

    # Run all assertions
    failed_assertions = []
    for assertion_name, check_fn in assertions:
        try:
            passed = await check_fn()
            if passed:
                print(f"  ✓ {assertion_name}")
            else:
                print(f"  ✗ {assertion_name}")
                failed_assertions.append(assertion_name)
        except Exception as e:
            print(f"  ✗ {assertion_name}: {e}")
            failed_assertions.append(f"{assertion_name} (error: {e})")

    if failed_assertions:
        raise AssertionError(
            f"{operation_name} failed post-operation assertions:\n"
            + "\n".join(f"  - {a}" for a in failed_assertions)
        )

    return result

# Usage:
order_id = None

async def place_order():
    global order_id
    result = await create_order(items=[...], customer_id="cust_123")
    order_id = result.get("order_id")
    return result

result = await with_post_assertions(
    operation_fn=place_order,
    assertions=[
        ("order_id returned", lambda: bool(order_id)),
        ("order exists in DB", lambda: order_exists_in_db(order_id)),
        ("inventory decremented", lambda: inventory_was_updated(order_id)),
        ("confirmation email queued", lambda: email_queued_for(order_id)),
    ],
    operation_name="Place order"
)
```

### Option 6: System prompt — verify before claiming success

```
System prompt:
"Verification rules — apply to ALL write operations:

NEVER report that an operation succeeded without verification.

After every write, create, send, or update operation:
1. Call the appropriate verification tool to confirm the side effect
2. Only report success after the verification tool confirms it
3. If verification fails:
   a. Retry the operation ONCE
   b. Verify again
   c. If still unconfirmed, report: 'Operation may have failed — unconfirmed'

Verification tools available:
- After send_email: call check_email_status(message_id)
- After create_record: call get_record(record_id)
- After write_file: call read_file(path) and check content
- After update_database: call query_database(table, id) and check field values

Language rules:
- WRONG: 'I have sent the email to alice@example.com'
- RIGHT: 'Email sent (message_id: msg_123). Verifying delivery...'
  [call check_email_status]
  'Delivery confirmed.' (or 'Delivery status: pending — will retry if undelivered')

Never use past tense for side effects until verified."
```

## Write Verification Patterns

| Operation | Verification Method | Timing |
|---|---|---|
| Database INSERT | SELECT by primary key | Immediate |
| File write | Read file back, compare hash | Immediate |
| Email send | Check delivery status API | 5–30s |
| Message queue publish | Check queue depth or consume | 1–5s |
| Cache write | GET from cache | Immediate |
| API POST | GET by returned ID | Immediate |
| Webhook trigger | Check webhook delivery logs | 5–60s |

## Expected Token Savings
Agent reports success → downstream acts on ghost data → failure discovered later → debug + redo: ~25,000 tokens
Verify-then-report → failure caught immediately → one clean retry: ~2,000 tokens overhead

## Environment
- Any agent performing write operations (database, email, files, APIs, queues); critical for e-commerce, workflow automation, and any agent with real-world side effects
- Source: direct experience; unverified writes are the most dangerous class of agent behavior because the failure is invisible until a downstream process depends on data that was never written
