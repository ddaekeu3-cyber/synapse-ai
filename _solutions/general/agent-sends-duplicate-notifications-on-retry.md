---
layout: solution
title: "Agent Sends Duplicate Notifications on Retry — Idempotency Failures"
category: general
description: "Agent sends a Slack message, hits a network error, retries, sends the message again. User receives two identical notifications. Or the agent emails a report, the process restarts, it emails the same report again. Side-effecting actions without idempotency protection produce duplicates every time there's a failure."
tags: [general, idempotency, notifications, retry, deduplication, slack, email, webhook, side-effects, at-most-once]
---

# Agent Sends Duplicate Notifications on Retry — Idempotency Failures

## Symptom
- User receives the same Slack message twice after an agent timeout and retry
- Email report is sent 3 times — once per retry attempt
- Webhook fires multiple times for the same event after transient network errors
- Database record is inserted twice — agent retried after a write that actually succeeded
- Payment is charged twice — agent retried an API that already processed the request
- Agent checkpoint shows task complete but notifications were already sent 2 sessions ago

## Root Cause
Side-effecting actions (send message, send email, write record, fire webhook, charge payment) are not naturally idempotent — calling them twice produces two effects. When agents retry on failure, they often can't tell whether the previous attempt succeeded before failing. Without idempotency keys or completion checks, every retry attempt repeats the side effect. The fix is to make every side effect idempotent: either use the API's built-in idempotency key support, or track completion state before acting.

## Fix

### Option 1: Idempotency key on every external API call

```python
import hashlib
import json
import time
import httpx
from typing import Any

def make_idempotency_key(
    action: str,
    payload: dict,
    scope: str = ""
) -> str:
    """
    Generate a stable idempotency key for an action.
    Same action + payload always produces the same key.
    Key is scoped to prevent cross-agent collisions.
    """
    canonical = json.dumps(
        {"action": action, "payload": payload, "scope": scope},
        sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]

async def send_notification_idempotent(
    channel: str,
    message: str,
    task_id: str,
    webhook_url: str
) -> dict:
    """
    Send a notification with an idempotency key.
    If retried with the same key, the server de-duplicates.
    """
    # Key based on content + task context — same message for same task = same key
    idempotency_key = make_idempotency_key(
        action="send_notification",
        payload={"channel": channel, "message": message},
        scope=task_id
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            webhook_url,
            json={"channel": channel, "text": message},
            headers={
                "Idempotency-Key": idempotency_key,
                "Content-Type": "application/json"
            },
            timeout=30.0
        )
        response.raise_for_status()
        return {"status": "sent", "idempotency_key": idempotency_key, "code": response.status_code}

# Stripe-style idempotency (works for payments, API calls):
async def charge_payment_idempotent(
    customer_id: str,
    amount_cents: int,
    currency: str,
    task_id: str,
    stripe_api_key: str
) -> dict:
    """Send a payment request — safe to retry with same key"""
    idempotency_key = make_idempotency_key(
        action="charge_customer",
        payload={"customer_id": customer_id, "amount": amount_cents, "currency": currency},
        scope=task_id
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.stripe.com/v1/payment_intents",
            data={
                "amount": amount_cents,
                "currency": currency,
                "customer": customer_id,
            },
            headers={
                "Authorization": f"Bearer {stripe_api_key}",
                "Idempotency-Key": idempotency_key
            }
        )
        return response.json()
```

### Option 2: Local completion ledger — track what has been sent

```python
import json
import hashlib
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

@dataclass
class CompletionRecord:
    action_id: str
    action_type: str
    completed_at: float
    result: dict
    task_id: str

class CompletionLedger:
    """
    Tracks which side-effecting actions have been completed.
    Before taking any side effect, check the ledger — skip if already done.
    """

    def __init__(self, ledger_path: str = "/data/completion_ledger.json"):
        self.ledger_path = Path(ledger_path)
        self._records: dict[str, CompletionRecord] = self._load()

    def _load(self) -> dict:
        if not self.ledger_path.exists():
            return {}
        raw = json.loads(self.ledger_path.read_text())
        return {k: CompletionRecord(**v) for k, v in raw.items()}

    def _save(self):
        data = {k: v.__dict__ for k, v in self._records.items()}
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.ledger_path)

    def action_id(self, action_type: str, payload: dict, task_id: str) -> str:
        """Generate stable action ID"""
        canonical = json.dumps({"type": action_type, "payload": payload, "task": task_id}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def is_done(self, action_id: str) -> Optional[CompletionRecord]:
        """Return completion record if action was already done, else None"""
        return self._records.get(action_id)

    def mark_done(self, action_id: str, action_type: str, task_id: str, result: dict):
        """Record that an action completed successfully"""
        self._records[action_id] = CompletionRecord(
            action_id=action_id,
            action_type=action_type,
            completed_at=time.time(),
            result=result,
            task_id=task_id
        )
        self._save()

    def cleanup_old(self, max_age_days: int = 7):
        """Remove old records to prevent unbounded growth"""
        cutoff = time.time() - max_age_days * 86400
        before = len(self._records)
        self._records = {k: v for k, v in self._records.items() if v.completed_at > cutoff}
        removed = before - len(self._records)
        if removed:
            self._save()
            print(f"Ledger cleanup: removed {removed} old records")

ledger = CompletionLedger()

async def send_slack_message_once(
    channel: str,
    message: str,
    task_id: str,
    slack_token: str
) -> dict:
    """
    Send a Slack message exactly once, regardless of how many times called.
    Safe to call on every retry attempt.
    """
    payload = {"channel": channel, "message": message}
    action_id = ledger.action_id("slack_message", payload, task_id)

    # Check if already sent
    existing = ledger.is_done(action_id)
    if existing:
        print(f"Slack message already sent (action_id={action_id}) — skipping duplicate")
        return {"status": "already_sent", "original_result": existing.result}

    # Send the message
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://slack.com/api/chat.postMessage",
            json={"channel": channel, "text": message},
            headers={"Authorization": f"Bearer {slack_token}"},
            timeout=30.0
        )
        result = response.json()

    if result.get("ok"):
        # Mark as done AFTER confirming success
        ledger.mark_done(action_id, "slack_message", task_id, result)
        print(f"Slack message sent and recorded (action_id={action_id})")
    else:
        print(f"Slack message failed — not recording (will retry): {result}")

    return result

async def send_email_report_once(
    to_address: str,
    subject: str,
    body: str,
    task_id: str
) -> dict:
    """Send an email report exactly once"""
    payload = {"to": to_address, "subject": subject}
    action_id = ledger.action_id("email_report", payload, task_id)

    if ledger.is_done(action_id):
        print(f"Email already sent to {to_address} — skipping")
        return {"status": "already_sent"}

    result = await _send_email(to_address, subject, body)
    if result.get("success"):
        ledger.mark_done(action_id, "email_report", task_id, result)
    return result

async def _send_email(to: str, subject: str, body: str) -> dict:
    # Placeholder for actual email sending
    return {"success": True, "message_id": f"msg_{int(time.time())}"}
```

### Option 3: Check-before-act pattern — verify before sending

```python
import httpx
from typing import Optional

async def check_message_already_sent(
    channel_id: str,
    expected_text: str,
    slack_token: str,
    lookback_minutes: int = 60
) -> Optional[dict]:
    """
    Check Slack history to see if we already sent this message recently.
    Prevents duplicates even if the completion ledger is lost.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://slack.com/api/conversations.history",
            params={
                "channel": channel_id,
                "limit": 20,
                "oldest": str(time.time() - lookback_minutes * 60)
            },
            headers={"Authorization": f"Bearer {slack_token}"}
        )
        history = response.json()

    if not history.get("ok"):
        return None  # Can't check — proceed with caution

    for message in history.get("messages", []):
        if message.get("text", "").strip() == expected_text.strip():
            return message  # Found duplicate

    return None

async def send_slack_with_dedup_check(
    channel_id: str,
    message: str,
    task_id: str,
    slack_token: str
) -> dict:
    """
    Two-layer deduplication:
    1. Check completion ledger (fast, local)
    2. Check Slack history (slower, but survives ledger loss)
    """
    payload = {"channel": channel_id, "message": message}
    action_id = ledger.action_id("slack_message", payload, task_id)

    # Layer 1: local ledger check
    if ledger.is_done(action_id):
        print("Already sent (ledger) — skipping")
        return {"status": "already_sent", "source": "ledger"}

    # Layer 2: API history check (for cases where ledger was reset)
    existing = await check_message_already_sent(channel_id, message, slack_token)
    if existing:
        print(f"Already sent (API history check) — recording in ledger")
        ledger.mark_done(action_id, "slack_message", task_id, existing)
        return {"status": "already_sent", "source": "api_check"}

    # Safe to send
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://slack.com/api/chat.postMessage",
            json={"channel": channel_id, "text": message},
            headers={"Authorization": f"Bearer {slack_token}"},
            timeout=30.0
        )
    result = response.json()
    if result.get("ok"):
        ledger.mark_done(action_id, "slack_message", task_id, result)
    return result
```

### Option 4: Idempotent task execution wrapper — wrap entire tasks

```python
import asyncio
import json
import hashlib
import time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Any

class IdempotentTaskRunner:
    """
    Wrapper that makes any task idempotent.
    If the task has completed before (by ID), returns the cached result.
    If in progress, waits for it to complete.
    """

    def __init__(self, state_dir: str = "/data/task_states"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _state_path(self, task_id: str) -> Path:
        return self.state_dir / f"{task_id}.json"

    def get_state(self, task_id: str) -> dict | None:
        path = self._state_path(task_id)
        if path.exists():
            return json.loads(path.read_text())
        return None

    def _write_state(self, task_id: str, state: dict):
        path = self._state_path(task_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(path)

    async def run_once(
        self,
        task_id: str,
        task_fn: Callable,
        *args,
        **kwargs
    ) -> Any:
        """
        Run task_fn exactly once for this task_id.
        On retry, returns the cached result from the first successful run.
        """
        state = self.get_state(task_id)

        if state and state.get("status") == "completed":
            print(f"Task {task_id} already completed — returning cached result")
            return state["result"]

        if state and state.get("status") == "in_progress":
            started_at = state.get("started_at", 0)
            elapsed = time.time() - started_at
            if elapsed < 300:  # 5 minute timeout
                print(f"Task {task_id} already in progress ({elapsed:.0f}s ago) — waiting")
                # Wait for completion with polling
                for _ in range(30):
                    await asyncio.sleep(10)
                    state = self.get_state(task_id)
                    if state and state.get("status") == "completed":
                        return state["result"]
                print(f"Timeout waiting for task {task_id} — taking over")
            else:
                print(f"Task {task_id} was in progress but timed out — retrying")

        # Mark as in progress
        self._write_state(task_id, {
            "status": "in_progress",
            "started_at": time.time(),
            "attempts": (state or {}).get("attempts", 0) + 1
        })

        try:
            result = await task_fn(*args, **kwargs)
            self._write_state(task_id, {
                "status": "completed",
                "completed_at": time.time(),
                "result": result
            })
            print(f"Task {task_id} completed and recorded")
            return result
        except Exception as e:
            self._write_state(task_id, {
                "status": "failed",
                "failed_at": time.time(),
                "error": str(e)
            })
            raise

runner = IdempotentTaskRunner()

# Usage — safe to call multiple times with same task_id:
async def send_weekly_report(recipients: list[str], report_data: dict) -> dict:
    """Sends a weekly report — wrapped to be idempotent"""
    task_id = f"weekly_report_{report_data['week']}_{report_data['year']}"

    return await runner.run_once(
        task_id=task_id,
        task_fn=_actually_send_report,
        recipients=recipients,
        report_data=report_data
    )

async def _actually_send_report(recipients: list, report_data: dict) -> dict:
    # Actual sending logic here
    return {"sent_to": recipients, "report_week": report_data.get("week")}
```

### Option 5: At-most-once delivery with atomic flag

```python
import sqlite3
from contextlib import contextmanager
from pathlib import Path

class AtMostOnceDelivery:
    """
    Database-backed at-most-once delivery guarantee.
    Uses a unique constraint to prevent duplicate actions.
    Even under concurrent retries, only one attempt succeeds.
    """

    def __init__(self, db_path: str = "/data/delivery.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    action_key TEXT PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    delivered_at REAL NOT NULL,
                    result TEXT,
                    task_id TEXT
                )
            """)
            conn.commit()

    @contextmanager
    def claim(self, action_key: str, action_type: str, task_id: str = ""):
        """
        Try to claim an action. Yields True if claim succeeded (first time).
        Yields False if already claimed (duplicate — skip the action).
        Atomically prevents duplicate execution even under concurrent retries.
        """
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO deliveries (action_key, action_type, delivered_at, task_id) VALUES (?, ?, ?, ?)",
                    (action_key, action_type, __import__("time").time(), task_id)
                )
                conn.commit()
                claimed = True
            except sqlite3.IntegrityError:
                # PRIMARY KEY conflict — already claimed
                claimed = False

        if claimed:
            try:
                yield True  # Caller should perform the action
            except Exception:
                # Action failed — release the claim so it can be retried
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("DELETE FROM deliveries WHERE action_key = ?", (action_key,))
                    conn.commit()
                raise
        else:
            yield False  # Already delivered — skip

delivery = AtMostOnceDelivery()

async def send_notification_at_most_once(
    user_id: str,
    notification_type: str,
    message: str,
    task_id: str
) -> dict:
    """Send notification at most once — safe under concurrent retries"""
    action_key = f"{notification_type}:{user_id}:{task_id}"

    with delivery.claim(action_key, notification_type, task_id) as claimed:
        if not claimed:
            print(f"Notification already sent (action_key={action_key}) — skipping")
            return {"status": "skipped", "reason": "already_delivered"}

        # This only runs once, even if called concurrently
        result = await _deliver_notification(user_id, message)
        print(f"Notification delivered to {user_id}")
        return result

async def _deliver_notification(user_id: str, message: str) -> dict:
    return {"delivered": True, "user_id": user_id}
```

### Option 6: System prompt instruction — teach the agent about idempotency

```python
IDEMPOTENCY_SYSTEM_PROMPT = """## Notification and Side-Effect Rules

Before sending any notification, email, message, or triggering any external action:

1. CHECK if this action was already performed in this task
   - Look for prior tool calls with the same recipient + message content
   - If you already called send_notification or send_email for this recipient in this task, DO NOT call it again

2. USE the task_id as part of any idempotency key
   - Pass task_id to all notification tools — they use it to deduplicate

3. PREFER "send if not already sent" tools over plain "send" tools
   - Use send_slack_message_once() not send_slack_message()
   - Use send_email_once() not send_email()

4. On retry, assume side effects from previous attempts may have succeeded
   - A failed tool call does NOT mean the action wasn't executed
   - A network timeout means the action MAY have succeeded

5. NEVER send the same message to the same recipient twice in one task
   - Once you've sent a completion notification, your job is done
   - Verify before sending: "Have I already notified this user in this task?"

The cost of a missed notification is far lower than the cost of a duplicate charge or spam."""

def build_idempotency_aware_system(base_prompt: str) -> str:
    return f"{base_prompt}\n\n{IDEMPOTENCY_SYSTEM_PROMPT}"
```

## Idempotency Failure Patterns

| Action | Failure Mode | Fix |
|---|---|---|
| Send Slack message | Timeout + retry = duplicate message | Completion ledger + idempotency key |
| Send email | Process restart = email sent twice | At-most-once delivery with DB constraint |
| Charge payment | Stripe timeout = double charge | Stripe idempotency key header |
| Write database record | Network error = duplicate insert | Unique constraint + upsert |
| Call webhook | Connection reset = double fire | Idempotency key in webhook header |
| Trigger deployment | Agent restart = deploy twice | Task state tracking before triggering |

## Expected Token Savings
Duplicate notification → user reports → agent investigates → apologizes → fixes: ~12,000 tokens
At-most-once delivery → no duplicates → no investigation: 0 cleanup overhead

## Environment
- Any agent that sends notifications, emails, webhooks, charges payments, or writes to external systems; critical for batch-processing agents and any agent that retries on failure — idempotency is required for all side-effecting actions in production agents
- Source: direct experience; duplicate notifications are the most common user-facing correctness bug in retry-enabled agents, and the most damaging to user trust
