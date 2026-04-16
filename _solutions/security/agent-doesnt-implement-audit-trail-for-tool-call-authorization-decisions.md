---
title: "Agent Doesn't Implement Audit Trail for Tool Call Authorization Decisions"
description: "Agents that execute tools without recording the authorization decision that permitted each call cannot satisfy compliance audits or forensic investigations: there is no record of which policy allowed a sensitive action, which session triggered it, or whether the authorization was granted by user consent or system policy. Implement a structured audit trail that records every authorization decision — permit and deny — with the policy that applied, the caller identity, and the full tool call context."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-audit-trail-for-tool-call-authorization-decisions
tags: [audit-trail, authorization-logging, compliance, tool-call-audit, forensics, access-control-logging]
symptoms:
  - "No record exists of which policy permitted a sensitive tool call"
  - "Compliance audit cannot determine who authorized a file deletion or payment"
  - "Forensic investigation after an incident has no tool call authorization history"
  - "Permit and deny decisions are not logged — only errors are recorded"
  - "Authorization decisions are embedded in code with no structured log output"
---

## Why This Happens

Authorization checks are often written as inline conditionals: `if user.can_call(tool): execute()`. The decision is made and discarded — no structured record is emitted. When a compliance auditor asks "who authorized this payment?" or an incident responder asks "which tool calls occurred during this session?", the answer requires reconstructing decisions from scattered application logs. A structured authorization audit trail treats each permit/deny decision as a first-class event with a consistent schema: who, what, when, which policy, and what outcome. This makes compliance reporting a query rather than a forensic investigation.

## Solution 1: Authorization Decision Record

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class AuthDecision(str, Enum):
    PERMIT = "permit"
    DENY = "deny"
    ESCALATE = "escalate"   # requires additional verification


class AuthPolicySource(str, Enum):
    USER_CONSENT = "user_consent"
    ROLE_POLICY = "role_policy"
    SYSTEM_POLICY = "system_policy"
    DEFAULT_DENY = "default_deny"
    CIRCUIT_BREAKER = "circuit_breaker"
    RATE_LIMIT = "rate_limit"


@dataclass
class AuthorizationDecisionRecord:
    decision_id: str
    session_id: str
    caller_id: str
    tool_name: str
    decision: AuthDecision
    policy_source: AuthPolicySource
    policy_name: str
    timestamp: float
    args_summary: Dict[str, Any]    # sanitized — no secrets
    reason: str = ""
    consent_id: str = ""
    role: str = ""
    request_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        session_id: str,
        caller_id: str,
        tool_name: str,
        decision: AuthDecision,
        policy_source: AuthPolicySource,
        policy_name: str,
        args_summary: Dict[str, Any],
        reason: str = "",
        **kwargs,
    ) -> "AuthorizationDecisionRecord":
        return cls(
            decision_id=str(uuid.uuid4())[:12],
            session_id=session_id,
            caller_id=caller_id,
            tool_name=tool_name,
            decision=decision,
            policy_source=policy_source,
            policy_name=policy_name,
            timestamp=time.time(),
            args_summary=args_summary,
            reason=reason,
            **kwargs,
        )
```

## Solution 2: Args Summarizer

```python
import re
from typing import Any, Dict


SECRET_FIELDS = re.compile(
    r"password|secret|token|key|auth|credential|api_key|private",
    re.IGNORECASE,
)


class ArgsSummarizer:
    """
    Produces a sanitized summary of tool arguments for audit logging.
    Removes or masks sensitive fields before recording.
    """

    def summarize(self, args: Dict[str, Any], max_value_len: int = 100) -> Dict[str, Any]:
        result = {}
        for key, value in args.items():
            if SECRET_FIELDS.search(key):
                result[key] = "[REDACTED]"
            elif isinstance(value, str) and len(value) > max_value_len:
                result[key] = value[:max_value_len] + f"...[{len(value) - max_value_len} chars omitted]"
            elif isinstance(value, (dict, list)):
                result[key] = f"[{type(value).__name__} len={len(value)}]"
            else:
                result[key] = value
        return result
```

## Solution 3: Authorization Audit Logger

```python
import json
import threading
import time
from collections import deque
from typing import Callable, Deque, List, Optional


class AuthorizationAuditLogger:
    """
    Records authorization decision records to an in-memory ring buffer
    and forwards to a configurable write function (file, SIEM, database).
    """

    def __init__(
        self,
        max_records: int = 100000,
        write_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._max = max_records
        self._records: Deque[AuthorizationDecisionRecord] = deque()
        self._lock = threading.Lock()
        self._write = write_fn or self._default_write
        self._permits = 0
        self._denies = 0

    @staticmethod
    def _default_write(record: dict) -> None:
        print(json.dumps(record))

    def log(self, record: AuthorizationDecisionRecord) -> None:
        event = {
            "event": "authorization_decision",
            "decision_id": record.decision_id,
            "session_id": record.session_id,
            "caller_id": record.caller_id,
            "tool_name": record.tool_name,
            "decision": record.decision.value,
            "policy_source": record.policy_source.value,
            "policy_name": record.policy_name,
            "reason": record.reason,
            "args_summary": record.args_summary,
            "timestamp": record.timestamp,
            "consent_id": record.consent_id,
            "role": record.role,
        }
        self._write(event)

        with self._lock:
            self._records.append(record)
            if len(self._records) > self._max:
                self._records.popleft()
            if record.decision == AuthDecision.PERMIT:
                self._permits += 1
            else:
                self._denies += 1

    def query_by_session(self, session_id: str) -> List[AuthorizationDecisionRecord]:
        with self._lock:
            return [r for r in self._records if r.session_id == session_id]

    def query_by_tool(
        self,
        tool_name: str,
        decision: Optional[AuthDecision] = None,
        window_seconds: float = 3600.0,
    ) -> List[AuthorizationDecisionRecord]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [
                r for r in self._records
                if r.tool_name == tool_name
                and r.timestamp >= cutoff
                and (decision is None or r.decision == decision)
            ]

    def stats(self) -> dict:
        total = self._permits + self._denies
        return {
            "total_decisions": total,
            "permits": self._permits,
            "denies": self._denies,
            "deny_rate": round(self._denies / max(total, 1), 4),
        }
```

## Solution 4: Audited Authorization Gate

```python
import time
from typing import Any, Callable, Dict, Optional


class AuditedAuthorizationGate:
    """
    Combines authorization policy evaluation with structured audit logging.
    Every tool call through this gate produces an authorization decision record.
    """

    def __init__(
        self,
        logger: AuthorizationAuditLogger,
        summarizer: ArgsSummarizer,
        policy_fn: Callable,    # fn(tool_name, caller_id, role, args) -> (decision, policy_source, policy_name, reason)
    ):
        self._logger = logger
        self._summarizer = summarizer
        self._policy = policy_fn

    async def gate(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
        session_id: str = "",
        caller_id: str = "",
        role: str = "",
        consent_id: str = "",
    ) -> Any:
        decision, policy_source, policy_name, reason = self._policy(
            tool_name, caller_id, role, args
        )

        record = AuthorizationDecisionRecord.create(
            session_id=session_id,
            caller_id=caller_id,
            tool_name=tool_name,
            decision=decision,
            policy_source=policy_source,
            policy_name=policy_name,
            args_summary=self._summarizer.summarize(args),
            reason=reason,
            consent_id=consent_id,
            role=role,
        )
        self._logger.log(record)

        if decision == AuthDecision.DENY:
            raise AuthorizationDeniedError(tool_name, reason, record.decision_id)

        if decision == AuthDecision.ESCALATE:
            raise AuthorizationEscalateError(tool_name, reason, record.decision_id)

        return await tool_fn(**args)


class AuthorizationDeniedError(Exception):
    def __init__(self, tool_name: str, reason: str, decision_id: str):
        super().__init__(f"authorization denied for '{tool_name}': {reason} (id={decision_id})")
        self.tool_name = tool_name
        self.reason = reason
        self.decision_id = decision_id


class AuthorizationEscalateError(Exception):
    def __init__(self, tool_name: str, reason: str, decision_id: str):
        super().__init__(f"authorization escalation required for '{tool_name}' (id={decision_id})")
        self.tool_name = tool_name
        self.reason = reason
        self.decision_id = decision_id
```

## Solution 5: Compliance Report Generator

```python
import time
from collections import Counter
from typing import List


class ComplianceReportGenerator:
    """
    Produces compliance-ready summaries of authorization decisions
    for a specified time window, grouped by tool, policy, and decision.
    """

    def __init__(self, logger: AuthorizationAuditLogger):
        self._logger = logger

    def report(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._logger._lock:
            recent = [r for r in self._logger._records if r.timestamp >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "decisions": 0}

        by_tool = Counter(r.tool_name for r in recent)
        by_policy = Counter(r.policy_name for r in recent)
        by_decision = Counter(r.decision.value for r in recent)
        by_session = Counter(r.session_id for r in recent)
        denied = [r for r in recent if r.decision == AuthDecision.DENY]
        critical_tools = {"process_payment", "delete_file", "send_email", "modify_user_account"}
        critical_calls = [r for r in recent if r.tool_name in critical_tools]

        return {
            "window_seconds": window_seconds,
            "decisions": len(recent),
            "by_decision": dict(by_decision),
            "top_tools": dict(by_tool.most_common(10)),
            "top_policies": dict(by_policy.most_common(10)),
            "unique_sessions": len(set(r.session_id for r in recent)),
            "deny_events": len(denied),
            "critical_tool_calls": len(critical_calls),
            "critical_tool_breakdown": dict(Counter(r.tool_name for r in critical_calls)),
        }
```

## Solution 6: Authorization Audit Dashboard

```python
import time


class AuthorizationAuditDashboard:
    """
    Combines live authorization stats and compliance report into
    a single security operations view.
    """

    def __init__(
        self,
        logger: AuthorizationAuditLogger,
        report_generator: ComplianceReportGenerator,
    ):
        self._logger = logger
        self._report = report_generator

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "live_stats": self._logger.stats(),
            "last_1h_report": self._report.report(window_seconds=3600.0),
            "last_24h_summary": {
                "decisions": self._report.report(window_seconds=86400.0).get("decisions", 0),
                "critical_calls": self._report.report(window_seconds=86400.0).get("critical_tool_calls", 0),
            },
        }
```

## Comparison

| Approach | Structured Record | Sanitized Args | Policy Attribution | Compliance Query | Dashboard |
|---|---|---|---|---|---|
| AuthorizationDecisionRecord | Yes (full schema) | Via summarizer | Yes (policy_name) | No | No |
| AuthorizationAuditLogger | Yes | No | No | Yes (by session/tool) | No |
| AuditedAuthorizationGate | Via record | Via summarizer | Via policy_fn | No | No |
| ComplianceReportGenerator | No | No | No | Yes (window) | No |
| AuthorizationAuditDashboard | No | No | No | No | Yes |

**Best for production**: Emit authorization decision records to a write-once SIEM or append-only log store — mutable in-process records do not satisfy compliance requirements because they can be altered after the fact. Include `policy_name` in every record so that when a policy changes, the audit trail shows the exact policy version that governed each historical decision. Index records by `session_id` for incident response: a single session query should return the complete sequence of permit/deny decisions that occurred, enabling rapid forensic reconstruction without full log scanning.
