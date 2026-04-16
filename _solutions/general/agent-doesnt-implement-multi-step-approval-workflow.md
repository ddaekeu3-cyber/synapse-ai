---
layout: solution
title: "Agent Doesn't Implement Multi-Step Approval Workflow"
category: general
description: "AI agents that execute high-stakes actions without human checkpoints create unrecoverable failures. Multi-step approval workflows gate irreversible operations behind explicit human confirmation."
tags: [approval, workflow, human-in-the-loop, safety, gating, sqlite, async]
---

# Agent Doesn't Implement Multi-Step Approval Workflow

## Problem

AI agents with autonomous tool execution can trigger irreversible side effects — sending emails, deleting records, charging payments — without human review. A single hallucinated parameter or misunderstood intent can cause damage that no retry can fix.

Multi-step approval workflows insert explicit human gates before high-stakes operations, with full audit trails and time-limited windows.

---

## Option 1: Simple Confirm-Before-Execute Pattern

```python
import anthropic

DANGEROUS_ACTIONS = {"delete_user", "send_bulk_email", "process_refund", "deploy_to_production"}

def describe_action(tool_name: str, tool_input: dict) -> str:
    """Produce a human-readable description of a pending action."""
    descriptions = {
        "delete_user": "Permanently delete user account for user_id={user_id}",
        "send_bulk_email": "Send email to {recipient_count} recipients with subject: {subject}",
        "process_refund": "Process refund of ${amount} to {customer_email}",
        "deploy_to_production": "Deploy version {version} to production environment {environment}",
    }
    template = descriptions.get(tool_name, f"Execute {tool_name} with {tool_input}")
    try:
        return template.format(**tool_input)
    except KeyError:
        return f"Execute {tool_name} with: {tool_input}"


def request_approval(tool_name: str, tool_input: dict) -> bool:
    """Prompt user for explicit confirmation before dangerous actions."""
    description = describe_action(tool_name, tool_input)
    print(f"\n⚠️  APPROVAL REQUIRED")
    print(f"   Action: {description}")
    print(f"   This action may be irreversible.")
    answer = input("   Type 'yes' to approve, anything else to cancel: ").strip().lower()
    return answer == "yes"


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a tool, requiring approval for dangerous actions."""
    if tool_name in DANGEROUS_ACTIONS:
        approved = request_approval(tool_name, tool_input)
        if not approved:
            return f"Action '{tool_name}' was cancelled by user."

    # Simulate tool execution
    return f"Successfully executed {tool_name} with {tool_input}"


def run_agent_with_approval(user_request: str):
    client = anthropic.Anthropic()

    tools = [
        {
            "name": "delete_user",
            "description": "Permanently delete a user account",
            "input_schema": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
        {
            "name": "get_user_info",
            "description": "Retrieve user information",
            "input_schema": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    ]

    messages = [{"role": "user", "content": user_request}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            print(f"\nAgent: {response.content[0].text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    run_agent_with_approval("Please delete user account U-12345 from the system.")
# Expected Token Savings: None — approval gates add latency, not token efficiency
# Environment: pip install anthropic; interactive terminal required
```

---

## Option 2: Risk-Level Tiered Approval

```python
import anthropic
from enum import Enum
from dataclasses import dataclass

class RiskLevel(Enum):
    LOW = "low"          # Auto-approve
    MEDIUM = "medium"    # Single approval
    HIGH = "high"        # Manager approval required
    CRITICAL = "critical" # Two-person rule

@dataclass
class ActionPolicy:
    tool_name: str
    risk_level: RiskLevel
    description_template: str
    requires_reason: bool = False

POLICIES: dict[str, ActionPolicy] = {
    "read_data":        ActionPolicy("read_data",        RiskLevel.LOW,      "Read {table} data"),
    "update_record":    ActionPolicy("update_record",    RiskLevel.MEDIUM,   "Update {table} record {id}"),
    "delete_record":    ActionPolicy("delete_record",    RiskLevel.HIGH,     "Delete {table} record {id}", requires_reason=True),
    "drop_table":       ActionPolicy("drop_table",       RiskLevel.CRITICAL, "DROP TABLE {table}", requires_reason=True),
    "send_email":       ActionPolicy("send_email",       RiskLevel.MEDIUM,   "Send email to {to}"),
    "send_bulk_email":  ActionPolicy("send_bulk_email",  RiskLevel.HIGH,     "Send bulk email to {count} recipients", requires_reason=True),
    "charge_card":      ActionPolicy("charge_card",      RiskLevel.HIGH,     "Charge ${amount} to card ending {last4}", requires_reason=True),
    "refund":           ActionPolicy("refund",           RiskLevel.MEDIUM,   "Refund ${amount} to {email}"),
}

def get_approval(policy: ActionPolicy, tool_input: dict) -> tuple[bool, str]:
    """
    Returns (approved, reason).
    Applies approval logic based on risk level.
    """
    try:
        description = policy.description_template.format(**tool_input)
    except KeyError:
        description = f"{policy.tool_name} with {tool_input}"

    if policy.risk_level == RiskLevel.LOW:
        return True, "auto-approved (low risk)"

    print(f"\n{'='*60}")
    print(f"ACTION PENDING APPROVAL [{policy.risk_level.value.upper()}]")
    print(f"  {description}")

    reason = ""
    if policy.requires_reason:
        reason = input("  Provide business justification: ").strip()
        if not reason:
            return False, "cancelled: justification required"

    if policy.risk_level == RiskLevel.CRITICAL:
        print("  ⚠️  CRITICAL action requires TWO approvals.")
        first = input("  First approver (type 'approve'): ").strip()
        second = input("  Second approver (type 'approve'): ").strip()
        if first != "approve" or second != "approve":
            return False, "cancelled: two-person rule not satisfied"
        return True, f"two-person approved | reason: {reason}"

    answer = input(f"  Approve? (yes/no): ").strip().lower()
    if answer == "yes":
        return True, f"approved | reason: {reason}"
    return False, "cancelled by approver"


def execute_with_policy(tool_name: str, tool_input: dict) -> str:
    policy = POLICIES.get(tool_name)
    if policy is None:
        return f"Unknown tool: {tool_name}"

    approved, decision = get_approval(policy, tool_input)
    if not approved:
        return f"Action blocked: {decision}"

    print(f"  ✓ Proceeding: {decision}")
    return f"Executed {tool_name} successfully. Audit: {decision}"


def run_tiered_approval_agent(request: str):
    client = anthropic.Anthropic()

    tools = [
        {
            "name": name,
            "description": policy.description_template,
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": True},
        }
        for name, policy in POLICIES.items()
    ]

    messages = [{"role": "user", "content": request}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"\nAgent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_with_policy(block.name, block.input)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    run_tiered_approval_agent("Delete the user record for ID 99 and drop the temp_sessions table.")
# Expected Token Savings: None — approval latency is intentional, token cost unchanged
# Environment: pip install anthropic; interactive terminal required
```

---

## Option 3: Async Approval with Time-Limited Windows

```python
import asyncio
import uuid
import anthropic
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"

@dataclass
class ApprovalRequest:
    request_id: str
    tool_name: str
    tool_input: dict
    created_at: datetime
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision_by: str | None = None
    decision_at: datetime | None = None

class ApprovalQueue:
    """Async approval queue with time-limited windows."""

    def __init__(self, timeout_seconds: int = 300):
        self.timeout = timeout_seconds
        self._pending: dict[str, ApprovalRequest] = {}
        self._events: dict[str, asyncio.Event] = {}

    def submit(self, tool_name: str, tool_input: dict) -> ApprovalRequest:
        req_id = str(uuid.uuid4())[:8]
        now = datetime.utcnow()
        request = ApprovalRequest(
            request_id=req_id,
            tool_name=tool_name,
            tool_input=tool_input,
            created_at=now,
            expires_at=now + timedelta(seconds=self.timeout),
        )
        self._pending[req_id] = request
        self._events[req_id] = asyncio.Event()
        return request

    async def wait_for_decision(self, request_id: str) -> ApprovalRequest:
        request = self._pending[request_id]
        event = self._events[request_id]

        try:
            await asyncio.wait_for(event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            request.status = ApprovalStatus.EXPIRED

        return request

    def decide(self, request_id: str, approved: bool, approver: str):
        if request_id not in self._pending:
            raise KeyError(f"Unknown request: {request_id}")
        request = self._pending[request_id]
        if request.status != ApprovalStatus.PENDING:
            raise ValueError(f"Request {request_id} is not pending")
        request.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        request.decision_by = approver
        request.decision_at = datetime.utcnow()
        self._events[request_id].set()

    def list_pending(self) -> list[ApprovalRequest]:
        return [r for r in self._pending.values() if r.status == ApprovalStatus.PENDING]


# Global queue for demo
approval_queue = ApprovalQueue(timeout_seconds=30)

async def auto_approver_simulation():
    """Simulates an approver reviewing and acting on pending requests."""
    await asyncio.sleep(2)  # Simulate review delay
    pending = approval_queue.list_pending()
    for req in pending:
        print(f"\n[Approver] Reviewing: {req.tool_name}({req.tool_input})")
        # Auto-approve for demo; real implementation would show UI/send notification
        approval_queue.decide(req.request_id, approved=True, approver="auto-demo-approver")
        print(f"[Approver] Approved request {req.request_id}")


async def execute_with_async_approval(tool_name: str, tool_input: dict) -> str:
    HIGH_RISK = {"delete_record", "send_bulk_email", "process_payment"}

    if tool_name not in HIGH_RISK:
        return f"Auto-executed {tool_name}: {tool_input}"

    request = approval_queue.submit(tool_name, tool_input)
    print(f"\n[Agent] Submitted approval request {request.request_id} for {tool_name}")
    print(f"[Agent] Waiting up to {approval_queue.timeout}s for approval...")

    decision = await approval_queue.wait_for_decision(request.request_id)

    if decision.status == ApprovalStatus.APPROVED:
        return f"Executed {tool_name} after approval by {decision.decision_by}"
    elif decision.status == ApprovalStatus.REJECTED:
        return f"Action {tool_name} rejected by {decision.decision_by}"
    else:
        return f"Action {tool_name} expired without decision — cancelled"


async def run_async_approval_agent(request: str):
    client = anthropic.AsyncAnthropic()
    tools = [
        {
            "name": "delete_record",
            "description": "Delete a database record",
            "input_schema": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "id": {"type": "string"},
                },
                "required": ["table", "id"],
            },
        },
        {
            "name": "get_record",
            "description": "Read a database record",
            "input_schema": {
                "type": "object",
                "properties": {"table": {"type": "string"}, "id": {"type": "string"}},
                "required": ["table", "id"],
            },
        },
    ]

    messages = [{"role": "user", "content": request}]

    # Run approver simulation concurrently
    approver_task = asyncio.create_task(auto_approver_simulation())

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"\nAgent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await execute_with_async_approval(block.name, block.input)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})

    approver_task.cancel()


if __name__ == "__main__":
    asyncio.run(run_async_approval_agent("Delete record ID 42 from the users table."))
# Expected Token Savings: None — approval windows add human latency by design
# Environment: pip install anthropic; asyncio is stdlib
```

---

## Option 4: SQLite-Persisted Approval Audit Trail

```python
import sqlite3
import uuid
import json
import anthropic
from datetime import datetime

class ApprovalAuditDB:
    """Persist all approval requests and decisions to SQLite."""

    def __init__(self, db_path: str = "approvals.db"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS approval_requests (
                request_id TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                tool_input TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                submitted_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT,
                rejection_reason TEXT,
                agent_session_id TEXT
            );

            CREATE TABLE IF NOT EXISTS approval_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_data TEXT,
                occurred_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (request_id) REFERENCES approval_requests(request_id)
            );
        """)
        self.conn.commit()

    def submit(self, tool_name: str, tool_input: dict, risk_level: str, session_id: str) -> str:
        req_id = str(uuid.uuid4())[:12]
        self.conn.execute(
            """INSERT INTO approval_requests
               (request_id, tool_name, tool_input, risk_level, submitted_at, agent_session_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (req_id, tool_name, json.dumps(tool_input), risk_level, datetime.utcnow().isoformat(), session_id),
        )
        self.conn.execute(
            "INSERT INTO approval_events (request_id, event_type) VALUES (?, ?)",
            (req_id, "submitted"),
        )
        self.conn.commit()
        return req_id

    def approve(self, request_id: str, approver: str):
        self.conn.execute(
            """UPDATE approval_requests
               SET status='approved', decided_at=?, decided_by=?
               WHERE request_id=? AND status='pending'""",
            (datetime.utcnow().isoformat(), approver, request_id),
        )
        self.conn.execute(
            "INSERT INTO approval_events (request_id, event_type, event_data) VALUES (?, ?, ?)",
            (request_id, "approved", json.dumps({"approver": approver})),
        )
        self.conn.commit()

    def reject(self, request_id: str, approver: str, reason: str):
        self.conn.execute(
            """UPDATE approval_requests
               SET status='rejected', decided_at=?, decided_by=?, rejection_reason=?
               WHERE request_id=? AND status='pending'""",
            (datetime.utcnow().isoformat(), approver, reason, request_id),
        )
        self.conn.execute(
            "INSERT INTO approval_events (request_id, event_type, event_data) VALUES (?, ?, ?)",
            (request_id, "rejected", json.dumps({"approver": approver, "reason": reason})),
        )
        self.conn.commit()

    def get_status(self, request_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM approval_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.conn.execute("SELECT * FROM approval_requests").description]
        return dict(zip(cols, row))

    def audit_report(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT request_id, tool_name, risk_level, status, decided_by FROM approval_requests ORDER BY submitted_at"
        ).fetchall()
        return [
            {"request_id": r[0], "tool_name": r[1], "risk": r[2], "status": r[3], "by": r[4]}
            for r in rows
        ]


def run_agent_with_audit(user_request: str, session_id: str = "demo-session"):
    db = ApprovalAuditDB(db_path=":memory:")
    client = anthropic.Anthropic()

    RISK_MAP = {
        "delete_user": "critical",
        "update_user": "medium",
        "get_user": "low",
    }

    tools = [
        {"name": name, "description": f"Tool: {name}", "input_schema": {"type": "object", "additionalProperties": True}}
        for name in RISK_MAP
    ]

    messages = [{"role": "user", "content": user_request}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []

            for block in response.content:
                if block.type == "tool_use":
                    risk = RISK_MAP.get(block.name, "medium")
                    req_id = db.submit(block.name, block.input, risk, session_id)
                    print(f"\n[Audit] Request {req_id}: {block.name} (risk={risk})")

                    if risk in ("low", "medium"):
                        db.approve(req_id, approver="auto-policy")
                        result = f"Executed {block.name} (auto-approved, risk={risk})"
                    else:
                        # Simulate manual approval for demo
                        db.approve(req_id, approver="admin@example.com")
                        result = f"Executed {block.name} (manually approved by admin)"

                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": results})

    print("\nAudit Report:")
    for entry in db.audit_report():
        print(f"  {entry}")


if __name__ == "__main__":
    run_agent_with_audit("Get user info for ID 5, then delete user ID 5.")
# Expected Token Savings: None — audit trail has negligible overhead
# Environment: pip install anthropic; sqlite3 is stdlib
```

---

## Option 5: Webhook-Based External Approval

```python
import asyncio
import json
import uuid
import anthropic
from dataclasses import dataclass
from datetime import datetime

@dataclass
class WebhookApprovalRequest:
    request_id: str
    tool_name: str
    tool_input: dict
    webhook_url: str
    callback_token: str
    created_at: str

class WebhookApprovalGateway:
    """
    Send approval requests to an external system via webhook.
    In production: Slack, PagerDuty, Jira, or a custom portal.
    Here we simulate the webhook roundtrip locally.
    """

    def __init__(self, webhook_url: str = "https://hooks.example.com/approvals"):
        self.webhook_url = webhook_url
        self._pending: dict[str, asyncio.Future] = {}

    async def request_approval(self, tool_name: str, tool_input: dict) -> bool:
        req_id = str(uuid.uuid4())[:8]
        token = str(uuid.uuid4()).replace("-", "")

        payload = WebhookApprovalRequest(
            request_id=req_id,
            tool_name=tool_name,
            tool_input=tool_input,
            webhook_url=self.webhook_url,
            callback_token=token,
            created_at=datetime.utcnow().isoformat(),
        )

        loop = asyncio.get_event_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[req_id] = future

        # In production: POST payload to webhook_url
        # Here: simulate external system approving after delay
        print(f"\n[Webhook] Sent approval request {req_id} for {tool_name}")
        print(f"[Webhook] Payload: {json.dumps(payload.__dict__, default=str)[:120]}...")
        asyncio.create_task(self._simulate_external_approval(req_id, delay=1.5))

        try:
            approved = await asyncio.wait_for(future, timeout=60.0)
        except asyncio.TimeoutError:
            del self._pending[req_id]
            print(f"[Webhook] Request {req_id} timed out")
            return False

        return approved

    async def _simulate_external_approval(self, request_id: str, delay: float):
        """Simulates external system sending callback."""
        await asyncio.sleep(delay)
        self.receive_callback(request_id, approved=True, approver="webhook-system")

    def receive_callback(self, request_id: str, approved: bool, approver: str):
        """Called when external system sends back a decision."""
        if request_id in self._pending:
            future = self._pending.pop(request_id)
            if not future.done():
                future.set_result(approved)
            print(f"[Webhook] Received callback: {request_id} → {'APPROVED' if approved else 'REJECTED'} by {approver}")


gateway = WebhookApprovalGateway()
HIGH_RISK_TOOLS = {"delete_user", "mass_update", "system_restart"}


async def execute_with_webhook_approval(tool_name: str, tool_input: dict) -> str:
    if tool_name in HIGH_RISK_TOOLS:
        approved = await gateway.request_approval(tool_name, tool_input)
        if not approved:
            return f"Action {tool_name} was not approved via webhook"

    return f"Executed {tool_name} with {tool_input}"


async def run_webhook_approval_agent(request: str):
    client = anthropic.AsyncAnthropic()
    tools = [
        {
            "name": name,
            "description": f"Tool: {name}",
            "input_schema": {"type": "object", "additionalProperties": True},
        }
        for name in ["delete_user", "get_user", "update_user"]
    ]

    messages = [{"role": "user", "content": request}]

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            # Process tool calls concurrently where safe
            tasks = {}
            for block in response.content:
                if block.type == "tool_use":
                    task = asyncio.create_task(execute_with_webhook_approval(block.name, block.input))
                    tasks[block.id] = task

            results = []
            for tool_id, task in tasks.items():
                result = await task
                results.append({"type": "tool_result", "tool_use_id": tool_id, "content": result})

            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    asyncio.run(run_webhook_approval_agent("Delete user with ID 88."))
# Expected Token Savings: None — webhook round-trip adds latency, not token savings
# Environment: pip install anthropic; asyncio is stdlib
```

---

## Option 6: State-Machine Approval Workflow with SQLite

```python
import sqlite3
import json
import uuid
import anthropic
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

class WorkflowState(Enum):
    DRAFT = "draft"
    PENDING_L1 = "pending_l1"     # Team lead approval
    PENDING_L2 = "pending_l2"     # Manager approval
    PENDING_L3 = "pending_l3"     # Executive approval
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    ROLLED_BACK = "rolled_back"

RISK_TO_INITIAL_STATE = {
    "low":      WorkflowState.APPROVED,     # Auto-approved
    "medium":   WorkflowState.PENDING_L1,
    "high":     WorkflowState.PENDING_L2,
    "critical": WorkflowState.PENDING_L3,
}

APPROVAL_CHAIN = {
    WorkflowState.PENDING_L1: ("team_lead", WorkflowState.APPROVED),
    WorkflowState.PENDING_L2: ("manager",   WorkflowState.PENDING_L1),
    WorkflowState.PENDING_L3: ("executive", WorkflowState.PENDING_L2),
}

@dataclass
class WorkflowTicket:
    ticket_id: str
    tool_name: str
    tool_input: dict
    risk_level: str
    state: WorkflowState

class ApprovalStateMachine:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS workflows (
                ticket_id TEXT PRIMARY KEY,
                tool_name TEXT,
                tool_input TEXT,
                risk_level TEXT,
                state TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT,
                from_state TEXT,
                to_state TEXT,
                actor TEXT,
                note TEXT,
                occurred_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def create_ticket(self, tool_name: str, tool_input: dict, risk_level: str) -> WorkflowTicket:
        ticket_id = str(uuid.uuid4())[:10]
        initial_state = RISK_TO_INITIAL_STATE.get(risk_level, WorkflowState.PENDING_L1)
        now = datetime.utcnow().isoformat()

        self.conn.execute(
            "INSERT INTO workflows VALUES (?,?,?,?,?,?,?)",
            (ticket_id, tool_name, json.dumps(tool_input), risk_level, initial_state.value, now, now),
        )
        self._record_transition(ticket_id, "none", initial_state.value, "system", "ticket created")
        self.conn.commit()

        return WorkflowTicket(ticket_id, tool_name, tool_input, risk_level, initial_state)

    def _record_transition(self, ticket_id, from_s, to_s, actor, note):
        self.conn.execute(
            "INSERT INTO workflow_transitions (ticket_id, from_state, to_state, actor, note) VALUES (?,?,?,?,?)",
            (ticket_id, from_s, to_s, actor, note),
        )

    def advance(self, ticket_id: str, actor: str, approved: bool, note: str = "") -> WorkflowState:
        row = self.conn.execute(
            "SELECT state FROM workflows WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        current = WorkflowState(row[0])

        if not approved:
            next_state = WorkflowState.REJECTED
        elif current in APPROVAL_CHAIN:
            _, next_state = APPROVAL_CHAIN[current]
        else:
            next_state = WorkflowState.APPROVED

        self.conn.execute(
            "UPDATE workflows SET state=?, updated_at=? WHERE ticket_id=?",
            (next_state.value, datetime.utcnow().isoformat(), ticket_id),
        )
        self._record_transition(ticket_id, current.value, next_state.value, actor, note)
        self.conn.commit()
        return next_state

    def mark_executed(self, ticket_id: str):
        self.conn.execute(
            "UPDATE workflows SET state=? WHERE ticket_id=?",
            (WorkflowState.EXECUTED.value, ticket_id),
        )
        self._record_transition(ticket_id, WorkflowState.APPROVED.value, WorkflowState.EXECUTED.value, "system", "executed")
        self.conn.commit()

    def get_audit_trail(self, ticket_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT from_state, to_state, actor, note, occurred_at FROM workflow_transitions WHERE ticket_id=? ORDER BY id",
            (ticket_id,),
        ).fetchall()
        return [{"from": r[0], "to": r[1], "actor": r[2], "note": r[3], "at": r[4]} for r in rows]


TOOL_RISK = {
    "read_data": "low",
    "update_record": "medium",
    "delete_record": "high",
    "wipe_database": "critical",
}


def simulate_approval_chain(sm: ApprovalStateMachine, ticket: WorkflowTicket) -> bool:
    """Simulate all approvers in the chain (auto-approve for demo)."""
    max_steps = 5
    for _ in range(max_steps):
        if ticket.state == WorkflowState.APPROVED:
            return True
        if ticket.state == WorkflowState.REJECTED:
            return False
        if ticket.state not in APPROVAL_CHAIN:
            return ticket.state == WorkflowState.APPROVED

        required_actor, _ = APPROVAL_CHAIN[ticket.state]
        print(f"  [{required_actor}] Approving ticket {ticket.ticket_id} (state={ticket.state.value})")
        ticket.state = sm.advance(ticket.ticket_id, actor=required_actor, approved=True, note="auto-approved in demo")

    return False


def run_state_machine_agent(user_request: str):
    sm = ApprovalStateMachine()
    client = anthropic.Anthropic()

    tools = [
        {"name": name, "description": f"Tool: {name}", "input_schema": {"type": "object", "additionalProperties": True}}
        for name in TOOL_RISK
    ]

    messages = [{"role": "user", "content": user_request}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []

            for block in response.content:
                if block.type == "tool_use":
                    risk = TOOL_RISK.get(block.name, "medium")
                    ticket = sm.create_ticket(block.name, block.input, risk)
                    print(f"\n[Workflow] Ticket {ticket.ticket_id} created: {block.name} risk={risk} state={ticket.state.value}")

                    approved = simulate_approval_chain(sm, ticket)

                    if approved:
                        sm.mark_executed(ticket.ticket_id)
                        result = f"Executed {block.name} after full approval chain"
                    else:
                        result = f"Action {block.name} rejected by approval chain"

                    trail = sm.get_audit_trail(ticket.ticket_id)
                    print(f"  Audit trail: {[t['from'] + '→' + t['to'] for t in trail]}")

                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    run_state_machine_agent("Read some data, then delete a record, then wipe the database.")
# Expected Token Savings: None — state machine overhead is O(1) per tool call
# Environment: pip install anthropic; sqlite3, uuid, json are stdlib
```

---

## Comparison

| Option | Approval Mechanism | Persistence | Async | Audit Trail | Best For |
|--------|-------------------|-------------|-------|-------------|----------|
| 1 | Terminal confirm | None | No | None | Simple scripts, local tools |
| 2 | Risk-level tiered | None | No | Console only | Batch operations with varied risk |
| 3 | Async time-limited | In-memory | Yes | None | Slack/notification integrations |
| 4 | SQLite full audit | SQLite | No | Full events log | Compliance-required environments |
| 5 | Webhook external | None | Yes | Webhook system | Existing approval portals (Jira, etc.) |
| 6 | State machine chain | SQLite | No | Full transitions | Multi-level approval hierarchies |
