---
layout: solution
title: "Agent Doesn't Implement Soft Delete Instead of Hard Delete"
category: general
description: "Agent permanently deletes records when the user says 'delete', making recovery impossible and turning agentic mistakes into data loss incidents."
tags: [general, database, reliability, data-loss, production]
---

## Symptom

A user asks the agent to "delete the old test orders" and the agent executes `DELETE FROM orders WHERE status = 'test'`. Later the user realises that two real orders were tagged as 'test' by mistake. The data is gone — no audit trail, no recovery path. In an agentic context where Claude may misinterpret filters or run with overly broad conditions, hard deletes are a single point of irreversible failure.

## Root Cause

Agents are trained to be helpful and to act on user intent. "Delete X" maps naturally to a `DELETE` SQL statement. Without an explicit soft-delete pattern, the agent has no intermediate step between "record exists" and "record is permanently gone". The mismatch between user intent ("remove from view") and implementation ("destroy the data") is invisible until recovery is needed.

## Fix

### Option 1 — Add deleted_at column with filter in all queries

```python
import anthropic
import sqlite3
import json
from datetime import datetime, timezone

client = anthropic.Anthropic()

# Schema with soft-delete column
conn = sqlite3.connect(":memory:")
conn.execute("""
    CREATE TABLE orders (
        id         INTEGER PRIMARY KEY,
        customer   TEXT,
        amount     REAL,
        status     TEXT,
        deleted_at TEXT DEFAULT NULL   -- NULL = active, timestamp = soft-deleted
    )
""")
conn.executemany(
    "INSERT INTO orders (customer, amount, status) VALUES (?,?,?)",
    [("Alice", 100.0, "complete"), ("Bob", 50.0, "test"), ("Carol", 200.0, "complete")],
)
conn.commit()

def soft_delete(order_id: int, reason: str = "") -> dict:
    """Mark as deleted — data is preserved, never destroyed."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE orders SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
        (now, order_id),
    )
    conn.commit()
    rows_affected = conn.execute("SELECT changes()").fetchone()[0]
    return {
        "status":       "soft_deleted" if rows_affected else "not_found_or_already_deleted",
        "order_id":     order_id,
        "deleted_at":   now,
        "recoverable":  True,
        "reason":       reason,
    }

def list_orders(include_deleted: bool = False) -> list[dict]:
    """All queries default to excluding soft-deleted rows."""
    sql = "SELECT id, customer, amount, status, deleted_at FROM orders"
    if not include_deleted:
        sql += " WHERE deleted_at IS NULL"
    rows = conn.execute(sql).fetchall()
    return [{"id": r[0], "customer": r[1], "amount": r[2], "status": r[3], "deleted_at": r[4]} for r in rows]

def restore(order_id: int) -> dict:
    conn.execute("UPDATE orders SET deleted_at = NULL WHERE id = ?", (order_id,))
    conn.commit()
    return {"status": "restored", "order_id": order_id}

tools = [
    {"name": "delete_order",
     "description": "Soft-delete an order (recoverable). The data is preserved for 90 days.",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["order_id"]}},
    {"name": "list_orders",
     "description": "List active orders (soft-deleted orders are excluded by default).",
     "input_schema": {"type": "object", "properties": {"include_deleted": {"type": "boolean"}}}},
    {"name": "restore_order",
     "description": "Restore a previously soft-deleted order.",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"]}},
]

DISPATCH = {
    "delete_order":  lambda i: soft_delete(i["order_id"], i.get("reason", "")),
    "list_orders":   lambda i: list_orders(i.get("include_deleted", False)),
    "restore_order": lambda i: restore(i["order_id"]),
}

def agent_loop(user_msg: str):
    messages = [{"role": "user", "content": user_msg}]
    for _ in range(5):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages
        )
        if resp.stop_reason != "tool_use":
            print(f"[agent] {resp.content[0].text}")
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = DISPATCH[block.name](block.input)
                print(f"[tool] {block.name}({block.input}) → {result}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})

agent_loop("Delete order #2 — it was a test order.")
print(f"\nActive orders after delete: {list_orders()}")
print(f"All orders (incl deleted): {list_orders(include_deleted=True)}")
```

**Expected Token Savings:** Soft-delete prevents recovery incidents that consume dozens of turns of tokens (confirm deletion → check backup → restore → verify); one additional column saves hours of agent work.
**Environment:** Any agent with database write access; minimum viable implementation — one column, one filter.

---

### Option 2 — Trash bin with TTL and scheduled purge

```python
import anthropic
import sqlite3
import json
import time
from datetime import datetime, timezone

client = anthropic.Anthropic()

conn = sqlite3.connect(":memory:")
conn.execute("""
    CREATE TABLE documents (
        id         INTEGER PRIMARY KEY,
        title      TEXT,
        content    TEXT,
        owner_id   TEXT,
        deleted_at REAL DEFAULT NULL,   -- Unix timestamp, NULL = active
        delete_reason TEXT
    )
""")
conn.executemany(
    "INSERT INTO documents (title, content, owner_id) VALUES (?,?,?)",
    [("Q1 Report", "Revenue data...", "u1"),
     ("Draft Notes", "WIP content...", "u1"),
     ("Archive 2024", "Old records...", "u1")],
)
conn.commit()

TRASH_TTL_SECONDS = 30 * 24 * 3600  # 30 days

def move_to_trash(doc_id: int, owner_id: str, reason: str) -> dict:
    now = time.time()
    purge_at = datetime.fromtimestamp(now + TRASH_TTL_SECONDS, tz=timezone.utc).isoformat()
    conn.execute(
        "UPDATE documents SET deleted_at = ?, delete_reason = ? WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
        (now, reason, doc_id, owner_id),
    )
    conn.commit()
    changed = conn.execute("SELECT changes()").fetchone()[0]
    if not changed:
        return {"status": "not_found"}
    return {"status": "in_trash", "doc_id": doc_id, "purges_at": purge_at, "recoverable": True}

def list_trash(owner_id: str) -> list[dict]:
    """Show items in trash with time remaining before permanent deletion."""
    now = time.time()
    rows = conn.execute(
        "SELECT id, title, deleted_at, delete_reason FROM documents WHERE owner_id = ? AND deleted_at IS NOT NULL",
        (owner_id,),
    ).fetchall()
    result = []
    for r in rows:
        seconds_left = max(0, (r[2] + TRASH_TTL_SECONDS) - now)
        result.append({
            "id":            r[0],
            "title":         r[1],
            "reason":        r[3],
            "days_until_purge": int(seconds_left / 86400),
        })
    return result

def restore_from_trash(doc_id: int, owner_id: str) -> dict:
    conn.execute(
        "UPDATE documents SET deleted_at = NULL, delete_reason = NULL WHERE id = ? AND owner_id = ?",
        (doc_id, owner_id),
    )
    conn.commit()
    return {"status": "restored", "doc_id": doc_id}

def purge_expired(dry_run: bool = True) -> dict:
    """Permanently delete items past TTL — run on a schedule, not inline."""
    cutoff = time.time() - TRASH_TTL_SECONDS
    if dry_run:
        count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)
        ).fetchone()[0]
        return {"dry_run": True, "would_purge": count}
    conn.execute("DELETE FROM documents WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))
    conn.commit()
    purged = conn.execute("SELECT changes()").fetchone()[0]
    return {"purged": purged}

# Demo
result = move_to_trash(2, "u1", "no longer needed")
print(f"[trash] {result}")
print(f"[trash contents] {list_trash('u1')}")
print(f"[restore] {restore_from_trash(2, 'u1')}")
print(f"[active] {conn.execute('SELECT id, title FROM documents WHERE deleted_at IS NULL').fetchall()}")
```

**Expected Token Savings:** Trash-bin pattern gives users a 30-day recovery window; the agent can tell the user "you can restore within 30 days" rather than spending tokens investigating backup recovery options.
**Environment:** Document management agents, file system agents, CMS agents; any context where user-facing "delete" should mean "move to trash".

---

### Option 3 — Archive table: move rows, never delete them

```python
import anthropic
import sqlite3
import json
from datetime import datetime, timezone

client = anthropic.Anthropic()

conn = sqlite3.connect(":memory:")
conn.execute("""
    CREATE TABLE customers (
        id       INTEGER PRIMARY KEY,
        name     TEXT,
        email    TEXT,
        tier     TEXT
    )
""")
conn.execute("""
    CREATE TABLE customers_archive (
        id           INTEGER PRIMARY KEY,
        name         TEXT,
        email        TEXT,
        tier         TEXT,
        archived_at  TEXT NOT NULL,
        archive_reason TEXT
    )
""")
conn.executemany(
    "INSERT INTO customers (name, email, tier) VALUES (?,?,?)",
    [("Alice", "alice@example.com", "gold"),
     ("Bob",   "bob@example.com",   "test"),
     ("Carol", "carol@example.com", "silver")],
)
conn.commit()

def archive_customer(customer_id: int, reason: str) -> dict:
    """Move customer row to archive table — primary table stays clean."""
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not row:
        return {"status": "not_found", "customer_id": customer_id}

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO customers_archive (id, name, email, tier, archived_at, archive_reason) VALUES (?,?,?,?,?,?)",
        (*row, now, reason),
    )
    conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    conn.commit()
    return {"status": "archived", "customer_id": customer_id, "archived_at": now, "recoverable": True}

def unarchive_customer(customer_id: int) -> dict:
    row = conn.execute("SELECT id, name, email, tier FROM customers_archive WHERE id = ?", (customer_id,)).fetchone()
    if not row:
        return {"status": "not_in_archive"}
    conn.execute("INSERT INTO customers (id, name, email, tier) VALUES (?,?,?,?)", row)
    conn.execute("DELETE FROM customers_archive WHERE id = ?", (customer_id,))
    conn.commit()
    return {"status": "unarchived", "customer_id": customer_id}

def search_archive(query: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, email, tier, archived_at, archive_reason FROM customers_archive WHERE name LIKE ? OR email LIKE ?",
        (f"%{query}%", f"%{query}%"),
    ).fetchall()
    return [{"id": r[0], "name": r[1], "email": r[2], "tier": r[3], "archived_at": r[4], "reason": r[5]} for r in rows]

tools = [
    {"name": "archive_customer", "description": "Archive (soft-delete) a customer — recoverable.",
     "input_schema": {"type": "object", "properties": {"customer_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["customer_id", "reason"]}},
    {"name": "search_archive", "description": "Search archived customers.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]

DISPATCH = {
    "archive_customer": lambda i: archive_customer(i["customer_id"], i.get("reason", "")),
    "search_archive":   lambda i: search_archive(i["query"]),
}

def agent_loop(msg: str):
    messages = [{"role": "user", "content": msg}]
    for _ in range(5):
        resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages)
        if resp.stop_reason != "tool_use":
            print(f"[agent] {resp.content[0].text}")
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = DISPATCH[block.name](block.input)
                print(f"[tool] {block.name} → {result}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})

agent_loop("Remove Bob from the active customer list — he was a test account.")
print(f"\nActive customers: {conn.execute('SELECT name FROM customers').fetchall()}")
print(f"Archive: {search_archive('Bob')}")
```

**Expected Token Savings:** Separate archive table keeps primary table fast (no soft-delete filter overhead) while preserving all data; compliance-friendly since archived records are queryable for audits.
**Environment:** Multi-tenant SaaS with GDPR requirements; financial systems with audit log mandates.

---

### Option 4 — Confirmation gate before any destructive operation

```python
import anthropic
import json

client = anthropic.Anthropic()

# Staged deletion: first generate a preview, then require explicit confirmation
_pending_deletes: dict[str, dict] = {}  # token → deletion plan

import uuid
import time

def plan_deletion(target: str, filter_description: str) -> dict:
    """Stage a deletion and return a preview + confirmation token."""
    token = str(uuid.uuid4())[:8]
    plan = {
        "token":              token,
        "target":             target,
        "filter":             filter_description,
        "estimated_rows":     3,  # in production: run SELECT COUNT(*) with same filter
        "expires_in_seconds": 60,
        "expires_at":         time.time() + 60,
        "status":             "pending_confirmation",
        "warning":            "This operation is soft-deletable for 30 days.",
    }
    _pending_deletes[token] = plan
    return plan

def confirm_deletion(token: str) -> dict:
    """Execute the staged deletion after user confirms the preview."""
    plan = _pending_deletes.pop(token, None)
    if not plan:
        return {"status": "invalid_or_expired_token"}
    if time.time() > plan["expires_at"]:
        return {"status": "token_expired", "message": "Confirmation window has passed. Re-plan the deletion."}
    # Execute soft delete here
    print(f"[soft-delete] executing: {plan['filter']} on {plan['target']}")
    return {"status": "soft_deleted", "rows_affected": plan["estimated_rows"], "recoverable": True}

def cancel_deletion(token: str) -> dict:
    _pending_deletes.pop(token, None)
    return {"status": "cancelled"}

tools = [
    {"name": "plan_deletion",
     "description": "Stage a deletion and return a preview. User must call confirm_deletion to proceed.",
     "input_schema": {"type": "object", "properties": {"target": {"type": "string"}, "filter_description": {"type": "string"}}, "required": ["target", "filter_description"]}},
    {"name": "confirm_deletion",
     "description": "Execute a previously planned deletion using its confirmation token.",
     "input_schema": {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]}},
    {"name": "cancel_deletion",
     "description": "Cancel a pending deletion.",
     "input_schema": {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]}},
]

DISPATCH = {
    "plan_deletion":    lambda i: plan_deletion(i["target"], i["filter_description"]),
    "confirm_deletion": lambda i: confirm_deletion(i["token"]),
    "cancel_deletion":  lambda i: cancel_deletion(i["token"]),
}

def agent_loop(msg: str):
    messages = [{"role": "user", "content": msg}]
    for _ in range(8):
        resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages)
        if resp.stop_reason != "tool_use":
            print(f"[agent] {resp.content[0].text}")
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = DISPATCH[block.name](block.input)
                print(f"[tool] {block.name} → {result}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})

agent_loop("Delete all test orders from last month and confirm the plan with me before proceeding.")
```

**Expected Token Savings:** Two-step plan+confirm gate prevents large accidental deletions; the agent naturally presents the plan to the user, who confirms with a token — no extra prompt engineering needed.
**Environment:** High-stakes deletions affecting many rows; financial data; customer records; any operation where scope could be wider than intended.

---

### Option 5 — Event sourcing: append-only log, never mutate

```python
import anthropic
import json
import time
from typing import Any

client = anthropic.Anthropic()

# Append-only event store — records are never deleted
_events: list[dict] = []
_event_id = 0

def append_event(event_type: str, entity_id: str, data: dict) -> dict:
    global _event_id
    _event_id += 1
    event = {
        "event_id":   _event_id,
        "event_type": event_type,
        "entity_id":  entity_id,
        "data":       data,
        "timestamp":  time.time(),
    }
    _events.append(event)
    return event

def get_current_state(entity_id: str) -> dict | None:
    """Replay events to get current state — deletion is just another event type."""
    state = None
    for ev in _events:
        if ev["entity_id"] != entity_id:
            continue
        if ev["event_type"] == "created":
            state = dict(ev["data"])
        elif ev["event_type"] == "updated":
            if state:
                state.update(ev["data"])
        elif ev["event_type"] == "deleted":
            state = None   # logically deleted, but event is still in the log
    return state

def get_history(entity_id: str) -> list[dict]:
    return [e for e in _events if e["entity_id"] == entity_id]

# Seed some data
append_event("created", "order-1", {"customer": "Alice", "amount": 100, "status": "complete"})
append_event("created", "order-2", {"customer": "Bob", "amount": 50,  "status": "test"})
append_event("updated", "order-1", {"status": "shipped"})

def delete_order(order_id: str, reason: str) -> dict:
    state = get_current_state(order_id)
    if state is None:
        return {"status": "not_found_or_already_deleted"}
    event = append_event("deleted", order_id, {"reason": reason})
    return {"status": "deleted", "event_id": event["event_id"], "recoverable": True,
            "note": "History is preserved. Query get_history to see all events."}

def restore_order(order_id: str) -> dict:
    history = get_history(order_id)
    last_event = history[-1] if history else None
    if not last_event or last_event["event_type"] != "deleted":
        return {"status": "order_is_not_deleted"}
    event = append_event("restored", order_id, {"reason": "user restore"})
    return {"status": "restored", "event_id": event["event_id"]}

# Demo
print(f"order-2 state: {get_current_state('order-2')}")
print(f"delete: {delete_order('order-2', 'test account')}")
print(f"order-2 state after delete: {get_current_state('order-2')}")
print(f"history: {json.dumps(get_history('order-2'), indent=2)}")
print(f"restore: {restore_order('order-2')}")
print(f"order-2 state after restore: {get_current_state('order-2')}")
```

**Expected Token Savings:** Event sourcing makes the entire history auditable — every agent action (including deletions) is stored and inspectable; no recovery tokens wasted on "what did the agent do?" investigations.
**Environment:** Financial systems, compliance-heavy applications, multi-agent workflows where causality tracing is required.

---

### Option 6 — Permission check: require elevated scope for hard deletes

```python
import anthropic
import json

client = anthropic.Anthropic()

AGENT_PERMISSIONS = {
    "standard": ["soft_delete", "list", "restore"],
    "admin":    ["soft_delete", "list", "restore", "hard_delete", "purge_trash"],
}

def get_agent_permissions(agent_token: str) -> list[str]:
    """In production: validate JWT or check ACL table."""
    if agent_token == "admin-token":
        return AGENT_PERMISSIONS["admin"]
    return AGENT_PERMISSIONS["standard"]

_records = {1: {"name": "Alice", "active": True}, 2: {"name": "Bob", "active": True}}
_trash:   dict[int, dict] = {}

def soft_delete_record(record_id: int, agent_token: str) -> dict:
    perms = get_agent_permissions(agent_token)
    if "soft_delete" not in perms:
        return {"status": "forbidden", "required_permission": "soft_delete"}
    record = _records.pop(record_id, None)
    if not record:
        return {"status": "not_found"}
    _trash[record_id] = record
    return {"status": "soft_deleted", "id": record_id, "recoverable": True}

def hard_delete_record(record_id: int, agent_token: str) -> dict:
    perms = get_agent_permissions(agent_token)
    if "hard_delete" not in perms:
        return {
            "status":   "forbidden",
            "required": "hard_delete",
            "message":  "Standard agents can only soft-delete. Request admin escalation for permanent deletion.",
        }
    _records.pop(record_id, None)
    _trash.pop(record_id, None)
    return {"status": "permanently_deleted", "id": record_id, "recoverable": False}

# Standard agent — cannot hard delete
print(soft_delete_record(1, "standard-token"))   # allowed
print(hard_delete_record(2, "standard-token"))   # forbidden
print(hard_delete_record(2, "admin-token"))      # allowed for admin

tools = [
    {"name": "delete_record",
     "description": "Delete a record. Standard agents perform soft-delete only. Hard deletes require admin token.",
     "input_schema": {"type": "object", "properties": {
         "record_id":   {"type": "integer"},
         "agent_token": {"type": "string"},
         "permanent":   {"type": "boolean", "description": "If true, permanently delete (requires admin token)."},
     }, "required": ["record_id", "agent_token"]}},
]

def dispatch_delete(inp: dict) -> dict:
    if inp.get("permanent"):
        return hard_delete_record(inp["record_id"], inp["agent_token"])
    return soft_delete_record(inp["record_id"], inp["agent_token"])

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    tools=tools,
    messages=[{"role": "user", "content": "Delete record 2 using my standard-token."}],
)
print(f"\n[agent response] stop_reason: {response.stop_reason}")
for block in response.content:
    if block.type == "tool_use":
        result = dispatch_delete(block.input)
        print(f"[tool] delete_record → {result}")
```

**Expected Token Savings:** Permission boundary prevents the agent from performing irreversible operations even if prompted; hard-delete requires explicit escalation, creating a natural audit checkpoint.
**Environment:** Multi-tenant agents with role-based access; any agent where the calling user's permission level should constrain what the agent can do.

---

## Comparison

| Option | Recovery Window | Query Overhead | Audit Trail | Storage Overhead | Best For |
|---|---|---|---|---|---|
| 1. deleted_at column | Indefinite | Filter on every query | Partial (timestamp) | Minimal | Quickest implementation; most common pattern |
| 2. Trash bin + TTL | TTL (e.g. 30 days) | Filter on every query | Partial + TTL | Minimal | User-facing apps (Google Drive model) |
| 3. Archive table | Indefinite | None on primary table | Yes (separate table) | Separate table | High-traffic tables; compliance archives |
| 4. Plan + confirm gate | N/A (prevent accidents) | None | Via plan log | None | Bulk operations; high-stakes deletions |
| 5. Event sourcing | Indefinite (full history) | Replay required | Complete (full log) | High (all events) | Audit-critical systems; financial data |
| 6. Permission scope | N/A (prevent hard-delete) | None | Via permission check | None | Multi-role agents; least-privilege enforcement |
