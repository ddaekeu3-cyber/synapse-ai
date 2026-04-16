---
title: "Agent Doesn't Implement Audit Logging for Auth Events"
description: "How to log authentication and authorization events—logins, token refreshes, permission denials, key rotations—for security monitoring and compliance."
categories: [auth]
difficulty: intermediate
---

Without an audit trail, you can't answer basic security questions: who accessed what, when was a token revoked, which requests were denied. Audit logging for auth events is a compliance requirement (SOC 2, HIPAA, PCI-DSS) and an essential forensic tool.

## Solution 1: Structured Auth Event Logger

Emit structured JSON log entries for every auth-relevant event with a consistent schema.

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()


class AuthEventType(str, Enum):
    LOGIN_SUCCESS = "login.success"
    LOGIN_FAILURE = "login.failure"
    TOKEN_ISSUED = "token.issued"
    TOKEN_REFRESHED = "token.refreshed"
    TOKEN_REVOKED = "token.revoked"
    TOKEN_EXPIRED = "token.expired"
    PERMISSION_GRANTED = "permission.granted"
    PERMISSION_DENIED = "permission.denied"
    API_KEY_USED = "api_key.used"
    API_KEY_ROTATED = "api_key.rotated"
    SESSION_CREATED = "session.created"
    SESSION_TERMINATED = "session.terminated"


@dataclass
class AuthEvent:
    event_type: AuthEventType
    user_id: str | None
    session_id: str | None
    resource: str | None
    outcome: str              # success | failure | blocked
    metadata: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    ip_address: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "user_id": self.user_id,
            "session_id": self.session_id,
            "resource": self.resource,
            "outcome": self.outcome,
            "ip_address": self.ip_address,
            "metadata": self.metadata,
        })


class AuthAuditLogger:
    def __init__(self, log_path: str = "/tmp/auth_audit.jsonl"):
        self._log_path = log_path

    def log(self, event: AuthEvent):
        with open(self._log_path, "a") as f:
            f.write(event.to_json() + "\n")
        # Also emit to stdout for log aggregators
        print(f"[AUDIT] {event.event_type.value} user={event.user_id} outcome={event.outcome}")

    def log_permission_denied(self, user_id: str, resource: str, reason: str, session_id: str = None):
        self.log(AuthEvent(
            event_type=AuthEventType.PERMISSION_DENIED,
            user_id=user_id,
            session_id=session_id,
            resource=resource,
            outcome="blocked",
            metadata={"reason": reason},
        ))

    def log_token_event(self, event_type: AuthEventType, user_id: str, token_id: str, **kwargs):
        self.log(AuthEvent(
            event_type=event_type,
            user_id=user_id,
            session_id=None,
            resource=None,
            outcome="success",
            metadata={"token_id": token_id, **kwargs},
        ))


audit = AuthAuditLogger()


async def check_permission_with_audit(user_id: str, resource: str, action: str) -> bool:
    # Simulated permission check
    allowed_resources = {"user_42": ["read:orders", "write:orders"], "user_99": ["read:orders"]}
    permission = f"{action}:{resource}"
    granted = permission in allowed_resources.get(user_id, [])

    if granted:
        audit.log(AuthEvent(
            event_type=AuthEventType.PERMISSION_GRANTED,
            user_id=user_id,
            session_id=None,
            resource=resource,
            outcome="success",
            metadata={"action": action},
        ))
    else:
        audit.log_permission_denied(user_id, resource, f"User lacks '{permission}'")

    return granted


async def main():
    # Login event
    audit.log(AuthEvent(
        event_type=AuthEventType.LOGIN_SUCCESS,
        user_id="user_42",
        session_id="sess_abc",
        resource=None,
        outcome="success",
        ip_address="192.168.1.10",
    ))

    # Permission checks
    await check_permission_with_audit("user_42", "orders", "write")  # Granted
    await check_permission_with_audit("user_99", "orders", "write")  # Denied
    await check_permission_with_audit("user_99", "orders", "read")   # Granted

    # Token events
    audit.log_token_event(AuthEventType.TOKEN_ISSUED, "user_42", "tok_xyz", expires_in=900)
    audit.log_token_event(AuthEventType.TOKEN_REVOKED, "user_42", "tok_xyz", reason="user_logout")

    print("\nAudit log written to /tmp/auth_audit.jsonl")


asyncio.run(main())
```

## Solution 2: Agent Tool-Call Auth Audit

Intercept every tool call and log whether the agent was authorized to invoke it.

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

# Tool-level permission matrix
TOOL_PERMISSIONS: dict[str, list[str]] = {
    "read_orders":   ["role:viewer", "role:editor", "role:admin"],
    "write_orders":  ["role:editor", "role:admin"],
    "delete_orders": ["role:admin"],
    "send_email":    ["role:editor", "role:admin"],
}


@dataclass
class ToolCallAuditEntry:
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    user_id: str = ""
    user_roles: list[str] = field(default_factory=list)
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    authorized: bool = False
    denial_reason: str | None = None

    def to_log(self) -> str:
        return json.dumps({
            "entry_id": self.entry_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "session_id": self.session_id,
            "user_id": self.user_id,
            "user_roles": self.user_roles,
            "tool": self.tool_name,
            "args_keys": list(self.tool_args.keys()),  # Log arg names but not values (privacy)
            "authorized": self.authorized,
            "denial_reason": self.denial_reason,
        })


AUDIT_LOG: list[ToolCallAuditEntry] = []


def is_authorized(user_roles: list[str], tool_name: str) -> tuple[bool, str | None]:
    required = TOOL_PERMISSIONS.get(tool_name)
    if required is None:
        return False, f"Tool '{tool_name}' is not in the permission registry"
    if any(r in required for r in user_roles):
        return True, None
    return False, f"None of {user_roles} in {required}"


async def authorized_tool_call(
    tool_name: str,
    tool_args: dict,
    user_id: str,
    user_roles: list[str],
    session_id: str,
) -> str:
    authorized, reason = is_authorized(user_roles, tool_name)
    entry = ToolCallAuditEntry(
        session_id=session_id,
        user_id=user_id,
        user_roles=user_roles,
        tool_name=tool_name,
        tool_args=tool_args,
        authorized=authorized,
        denial_reason=reason,
    )
    AUDIT_LOG.append(entry)
    print(f"[AUDIT] {entry.to_log()}")

    if not authorized:
        return f"[FORBIDDEN] {reason}"

    return f"[{tool_name}] executed successfully"


async def agent_with_auth_audit(user_id: str, roles: list[str], query: str) -> str:
    session_id = str(uuid.uuid4())[:8]
    tools = [
        {
            "name": "read_orders",
            "description": "Read order data",
            "input_schema": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
        {
            "name": "delete_orders",
            "description": "Delete an order",
            "input_schema": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    ]
    messages = [{"role": "user", "content": query}]

    while True:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = await authorized_tool_call(
                    block.name, block.input, user_id, roles, session_id
                )
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": results})


async def main():
    # Viewer tries to delete — should be denied
    await agent_with_auth_audit("user_99", ["role:viewer"], "Delete order #12345")


asyncio.run(main())
```

## Solution 3: Tamper-Evident Audit Log with HMAC Chaining

Each log entry includes an HMAC over the previous entry's hash, creating a tamper-evident chain.

```python
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

AUDIT_SECRET = os.environ.get("AUDIT_SECRET", "dev-secret-change-in-prod")
CHAIN_LOG = Path("/tmp/auth_audit_chain.jsonl")


@dataclass
class ChainedEntry:
    event_type: str
    user_id: str
    outcome: str
    metadata: dict
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    prev_hash: str = ""
    entry_hash: str = ""
    hmac_sig: str = ""

    def compute_hash(self) -> str:
        payload = json.dumps({
            "entry_id": self.entry_id,
            "event_type": self.event_type,
            "user_id": self.user_id,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "prev_hash": self.prev_hash,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def sign(self, secret: str) -> str:
        return hmac.new(
            secret.encode(), self.entry_hash.encode(), hashlib.sha256
        ).hexdigest()

    def to_record(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "event_type": self.event_type,
            "user_id": self.user_id,
            "outcome": self.outcome,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
            "hmac_sig": self.hmac_sig,
        }


class TamperEvidentLogger:
    def __init__(self, log_path: Path = CHAIN_LOG, secret: str = AUDIT_SECRET):
        self._log_path = log_path
        self._secret = secret
        self._last_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self._log_path.exists():
            return ""
        lines = self._log_path.read_text().strip().splitlines()
        if not lines:
            return ""
        try:
            last = json.loads(lines[-1])
            return last.get("entry_hash", "")
        except Exception:
            return ""

    def log(self, event_type: str, user_id: str, outcome: str, **metadata):
        entry = ChainedEntry(
            event_type=event_type,
            user_id=user_id,
            outcome=outcome,
            metadata=metadata,
            prev_hash=self._last_hash,
        )
        entry.entry_hash = entry.compute_hash()
        entry.hmac_sig = entry.sign(self._secret)

        with self._log_path.open("a") as f:
            f.write(json.dumps(entry.to_record()) + "\n")

        self._last_hash = entry.entry_hash
        return entry

    def verify_chain(self) -> tuple[bool, str]:
        if not self._log_path.exists():
            return True, "No log file"

        lines = self._log_path.read_text().strip().splitlines()
        prev_hash = ""

        for i, line in enumerate(lines):
            record = json.loads(line)

            # Recompute hash
            expected_hash = ChainedEntry(
                entry_id=record["entry_id"],
                event_type=record["event_type"],
                user_id=record["user_id"],
                outcome=record["outcome"],
                timestamp=record["timestamp"],
                prev_hash=record["prev_hash"],
                metadata={},
            ).compute_hash()

            if expected_hash != record["entry_hash"]:
                return False, f"Hash mismatch at entry {i} ({record['entry_id']})"

            if record["prev_hash"] != prev_hash:
                return False, f"Chain broken at entry {i}"

            prev_hash = record["entry_hash"]

        return True, f"Chain intact ({len(lines)} entries)"


def main():
    logger = TamperEvidentLogger()

    logger.log("login.success", "user_42", "success", ip="10.0.0.1")
    logger.log("permission.denied", "user_99", "blocked", resource="delete_orders")
    logger.log("token.revoked", "user_42", "success", reason="logout")

    valid, message = logger.verify_chain()
    print(f"Chain verification: {message}")


main()
```

## Solution 4: Real-Time Auth Anomaly Alerting

Detect suspicious auth patterns (multiple failures, unusual times, new locations) and emit alerts.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

FAILURE_THRESHOLD = 5        # Failures within window = alert
WINDOW_SECONDS = 300         # 5-minute window


@dataclass
class AuthAttempt:
    user_id: str
    success: bool
    ip_address: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class AuthAlert:
    alert_type: str
    user_id: str
    severity: str
    details: str
    timestamp: float = field(default_factory=time.time)


class AnomalyDetector:
    def __init__(self):
        self._attempts: dict[str, list[AuthAttempt]] = defaultdict(list)
        self._known_ips: dict[str, set[str]] = defaultdict(set)

    def record(self, attempt: AuthAttempt) -> list[AuthAlert]:
        self._attempts[attempt.user_id].append(attempt)
        alerts = []

        # Prune old attempts outside window
        cutoff = time.time() - WINDOW_SECONDS
        self._attempts[attempt.user_id] = [
            a for a in self._attempts[attempt.user_id] if a.timestamp > cutoff
        ]

        recent = self._attempts[attempt.user_id]
        recent_failures = [a for a in recent if not a.success]

        # Brute-force detection
        if len(recent_failures) >= FAILURE_THRESHOLD:
            alerts.append(AuthAlert(
                alert_type="brute_force",
                user_id=attempt.user_id,
                severity="HIGH",
                details=f"{len(recent_failures)} failures in {WINDOW_SECONDS}s window",
            ))

        # New IP detection
        if attempt.success and attempt.ip_address not in self._known_ips[attempt.user_id]:
            if self._known_ips[attempt.user_id]:  # Not first login
                alerts.append(AuthAlert(
                    alert_type="new_ip_login",
                    user_id=attempt.user_id,
                    severity="MEDIUM",
                    details=f"First login from IP {attempt.ip_address}",
                ))
            self._known_ips[attempt.user_id].add(attempt.ip_address)

        return alerts


async def analyze_alert_with_llm(alert: AuthAlert) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Security alert: type={alert.alert_type}, user={alert.user_id}, "
                    f"severity={alert.severity}, details={alert.details}\n\n"
                    f"Recommend ONE immediate action (1 sentence)."
                ),
            }
        ],
    )
    return resp.content[0].text.strip()


async def main():
    detector = AnomalyDetector()

    # Simulate brute-force attack
    for i in range(6):
        attempt = AuthAttempt("user_42", success=False, ip_address="1.2.3.4")
        alerts = detector.record(attempt)
        for alert in alerts:
            recommendation = await analyze_alert_with_llm(alert)
            print(f"[ALERT:{alert.severity}] {alert.alert_type} on {alert.user_id}")
            print(f"  Details: {alert.details}")
            print(f"  Action: {recommendation}\n")

    # Simulate login from new IP
    detector.record(AuthAttempt("user_99", success=True, ip_address="10.0.0.1"))
    alerts = detector.record(AuthAttempt("user_99", success=True, ip_address="5.6.7.8"))
    for alert in alerts:
        recommendation = await analyze_alert_with_llm(alert)
        print(f"[ALERT:{alert.severity}] {alert.alert_type} on {alert.user_id}")
        print(f"  {alert.details}")
        print(f"  Action: {recommendation}")


asyncio.run(main())
```

## Solution 5: Compliance Report Generator

Aggregate audit logs and generate compliance-ready summaries (SOC 2, GDPR, PCI-DSS).

```python
import asyncio
import json
import time
from pathlib import Path
from collections import defaultdict
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


def load_audit_log(log_path: str = "/tmp/auth_audit.jsonl") -> list[dict]:
    p = Path(log_path)
    if not p.exists():
        return []
    entries = []
    for line in p.read_text().splitlines():
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return entries


def aggregate_events(entries: list[dict]) -> dict:
    stats = defaultdict(int)
    by_user: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    denied_resources: list[str] = []

    for e in entries:
        event_type = e.get("event_type", "unknown")
        user_id = e.get("user_id", "unknown")
        stats[event_type] += 1
        by_user[user_id][event_type] += 1
        if event_type == "permission.denied":
            denied_resources.append(e.get("resource", "unknown"))

    return {
        "total_events": len(entries),
        "by_event_type": dict(stats),
        "active_users": len(by_user),
        "top_denied_resources": list(set(denied_resources))[:10],
        "login_failures": stats.get("login.failure", 0),
        "token_revocations": stats.get("token.revoked", 0),
    }


async def generate_compliance_report(period_days: int = 30) -> str:
    entries = load_audit_log()
    cutoff = time.time() - (period_days * 86400)
    period_entries = [e for e in entries if e.get("timestamp", 0) > cutoff]

    agg = aggregate_events(period_entries)
    summary = json.dumps(agg, indent=2)

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Generate a concise compliance report for the last {period_days} days "
                    f"based on these auth audit statistics:\n\n{summary}\n\n"
                    f"Cover: access control effectiveness, notable incidents, "
                    f"recommendations. Format as a structured report."
                ),
            }
        ],
    )
    return resp.content[0].text


async def main():
    report = await generate_compliance_report(period_days=30)
    print("=== Auth Compliance Report ===")
    print(report)


asyncio.run(main())
```

## Solution 6: Distributed Auth Audit with Correlation IDs

Correlate auth events across multiple services using a shared request/session correlation ID.

```python
import asyncio
import json
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()

# ContextVar propagates the correlation ID through async call chains
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")
session_id_var: ContextVar[str] = ContextVar("session_id", default="")


@dataclass
class DistributedAuthEvent:
    service: str
    event_type: str
    user_id: str
    outcome: str
    correlation_id: str = field(default_factory=lambda: correlation_id_var.get())
    session_id: str = field(default_factory=lambda: session_id_var.get())
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def to_log(self) -> str:
        return json.dumps({
            "service": self.service,
            "event_type": self.event_type,
            "user_id": self.user_id,
            "outcome": self.outcome,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "event_id": self.event_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "metadata": self.metadata,
        })


DISTRIBUTED_LOG: list[str] = []


def audit_log(event: DistributedAuthEvent):
    DISTRIBUTED_LOG.append(event.to_log())
    print(f"[{event.service}] {event.event_type} corr={event.correlation_id[:8]} user={event.user_id}")


async def auth_service_login(user_id: str, password: str) -> bool:
    success = user_id.startswith("user_")  # Simulated check
    audit_log(DistributedAuthEvent(
        service="auth-service",
        event_type="login.attempt",
        user_id=user_id,
        outcome="success" if success else "failure",
        metadata={"method": "password"},
    ))
    return success


async def token_service_issue(user_id: str) -> str:
    token = f"tok_{uuid.uuid4().hex[:12]}"
    audit_log(DistributedAuthEvent(
        service="token-service",
        event_type="token.issued",
        user_id=user_id,
        outcome="success",
        metadata={"token_id": token, "expires_in": 900},
    ))
    return token


async def api_gateway_authorize(user_id: str, resource: str) -> bool:
    allowed = resource.startswith("orders")
    audit_log(DistributedAuthEvent(
        service="api-gateway",
        event_type="permission.granted" if allowed else "permission.denied",
        user_id=user_id,
        outcome="success" if allowed else "blocked",
        metadata={"resource": resource},
    ))
    return allowed


async def handle_request(user_id: str, resource: str):
    # Set correlation ID for this request — propagates to all async calls
    corr_id = str(uuid.uuid4())
    sess_id = f"sess_{user_id}"
    correlation_id_var.set(corr_id)
    session_id_var.set(sess_id)

    print(f"\n--- Request {corr_id[:8]} ---")
    logged_in = await auth_service_login(user_id, "password123")
    if not logged_in:
        return

    token = await token_service_issue(user_id)
    authorized = await api_gateway_authorize(user_id, resource)

    print(f"  Result: {'authorized' if authorized else 'denied'}")


async def main():
    await handle_request("user_42", "orders/list")
    await handle_request("user_42", "admin/settings")  # Will be denied

    # Show all correlated events for first request
    first_corr = json.loads(DISTRIBUTED_LOG[0])["correlation_id"]
    print(f"\n[Correlated events for {first_corr[:8]}]")
    for entry in DISTRIBUTED_LOG:
        e = json.loads(entry)
        if e["correlation_id"] == first_corr:
            print(f"  {e['service']}: {e['event_type']} → {e['outcome']}")


asyncio.run(main())
```

## Comparison

| Solution | Tamper resistance | Real-time alerts | Compliance reports | Multi-service | Best for |
|---|---|---|---|---|---|
| **Structured event logger** | None | No | Manual | No | Basic audit trail |
| **Tool-call auth audit** | None | No | No | No | Agent permission logging |
| **HMAC chain log** | High | No | No | No | Compliance & forensics |
| **Anomaly alerting** | None | Yes | No | No | Security monitoring |
| **Compliance reporter** | None | No | Yes | No | SOC 2 / GDPR audits |
| **Distributed with corr IDs** | None | No | No | Yes | Microservice architectures |

Start with **structured event logger** (Solution 1) for immediate visibility. Add **HMAC chain log** (Solution 3) when tamper resistance is required for compliance. Use **distributed correlation IDs** (Solution 6) when auth events span multiple services.
