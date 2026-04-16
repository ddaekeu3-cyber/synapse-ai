---
title: "Agent Doesn't Implement User Consent Verification Before Sensitive Tool Calls"
description: "Agents that execute irreversible or privacy-sensitive tool calls — sending emails, posting to external services, deleting records, accessing personal data — without verifying user consent allow prompt injection to trigger those actions silently. Implement consent verification that classifies tool calls by sensitivity, requires explicit confirmation for high-sensitivity operations, and records consent provenance for audit."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-user-consent-verification-before-sensitive-tool-calls
tags: [user-consent, confirmation, sensitive-tools, human-in-the-loop, audit-trail, privilege-escalation]
symptoms:
  - "Agent sends an email on behalf of the user without asking for confirmation"
  - "Prompt injection in a retrieved document triggers a delete operation silently"
  - "No classification of which tool calls require human confirmation before execution"
  - "All tool calls dispatched automatically regardless of their irreversibility"
  - "No record of whether the user explicitly approved a sensitive action"
---

## Why This Happens

Tool-calling agents are designed for automation — the value proposition is that they act without constant prompting. This convenience becomes a vulnerability when the agent can be manipulated by injected content to take actions the user never intended. High-sensitivity operations (send, delete, post, pay, access PII) require a confirmation step that cannot be bypassed by injected instructions, because the confirmation happens in a separate channel (user interface, explicit human response) that the injected content cannot control.

## Solution 1: Tool Sensitivity Classification

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class ToolSensitivity(str, Enum):
    LOW = "low"           # read-only, reversible — no confirmation needed
    MEDIUM = "medium"     # writes that can be undone — warn but proceed
    HIGH = "high"         # irreversible or PII-touching — require confirmation
    CRITICAL = "critical" # financial, legal, or mass-impact — require explicit typed confirmation


@dataclass
class SensitiveToolPolicy:
    tool_name: str
    sensitivity: ToolSensitivity
    description: str
    confirmation_prompt: str          # shown to user before execution
    requires_reason: bool = False     # user must provide a reason
    audit_on_execute: bool = True
    allowed_session_types: Optional[Set[str]] = None  # restrict to specific session types
```

## Solution 2: Tool Sensitivity Registry

```python
from typing import Dict, Optional


def build_default_sensitivity_registry() -> "ToolSensitivityRegistry":
    registry = ToolSensitivityRegistry()
    registry.register(SensitiveToolPolicy(
        tool_name="send_email",
        sensitivity=ToolSensitivity.HIGH,
        description="Sends an email on behalf of the user",
        confirmation_prompt="Send email to {to} with subject '{subject}'?",
        audit_on_execute=True,
    ))
    registry.register(SensitiveToolPolicy(
        tool_name="delete_record",
        sensitivity=ToolSensitivity.CRITICAL,
        description="Permanently deletes a database record",
        confirmation_prompt="Permanently delete record {record_id}? This cannot be undone.",
        requires_reason=True,
        audit_on_execute=True,
    ))
    registry.register(SensitiveToolPolicy(
        tool_name="post_to_social",
        sensitivity=ToolSensitivity.HIGH,
        description="Posts content to an external social platform",
        confirmation_prompt="Post the following content publicly: '{content_preview}'?",
        audit_on_execute=True,
    ))
    registry.register(SensitiveToolPolicy(
        tool_name="access_pii",
        sensitivity=ToolSensitivity.HIGH,
        description="Retrieves personally identifiable information",
        confirmation_prompt="Access PII for user {user_id}?",
        audit_on_execute=True,
    ))
    registry.register(SensitiveToolPolicy(
        tool_name="initiate_payment",
        sensitivity=ToolSensitivity.CRITICAL,
        description="Initiates a financial transaction",
        confirmation_prompt="Initiate payment of {amount} {currency} to {recipient}?",
        requires_reason=True,
        audit_on_execute=True,
    ))
    return registry


class ToolSensitivityRegistry:
    def __init__(self):
        self._policies: Dict[str, SensitiveToolPolicy] = {}

    def register(self, policy: SensitiveToolPolicy) -> None:
        self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> Optional[SensitiveToolPolicy]:
        return self._policies.get(tool_name)

    def requires_confirmation(self, tool_name: str) -> bool:
        policy = self.get(tool_name)
        if policy is None:
            return False
        return policy.sensitivity in (ToolSensitivity.HIGH, ToolSensitivity.CRITICAL)
```

## Solution 3: Consent Request

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ConsentDecision(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass
class ConsentRequest:
    request_id: str
    session_id: str
    tool_name: str
    tool_args: Dict[str, Any]
    sensitivity: ToolSensitivity
    confirmation_prompt: str
    decision: ConsentDecision = ConsentDecision.PENDING
    decided_at: Optional[float] = None
    user_reason: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 120.0)

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def approve(self, reason: Optional[str] = None) -> None:
        self.decision = ConsentDecision.APPROVED
        self.decided_at = time.time()
        self.user_reason = reason

    def deny(self) -> None:
        self.decision = ConsentDecision.DENIED
        self.decided_at = time.time()

    @classmethod
    def create(
        cls,
        session_id: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        policy: SensitiveToolPolicy,
    ) -> "ConsentRequest":
        return cls(
            request_id=uuid.uuid4().hex[:16],
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            sensitivity=policy.sensitivity,
            confirmation_prompt=policy.confirmation_prompt,
        )
```

## Solution 4: Consent-Gated Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class ConsentGatedToolDispatcher:
    """
    Intercepts tool calls for registered sensitive tools and requires
    an approved ConsentRequest before dispatching. Provides a callback
    mechanism to present confirmation requests to the user interface.
    """

    def __init__(
        self,
        registry: ToolSensitivityRegistry,
        consent_requester: Callable,  # async fn(ConsentRequest) -> ConsentDecision
    ):
        self._registry = registry
        self._requester = consent_requester
        self._pending: Dict[str, ConsentRequest] = {}
        self._audit: list = []

    async def dispatch(
        self,
        session_id: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_fn: Callable,
    ) -> Any:
        policy = self._registry.get(tool_name)

        if policy is None or not self._registry.requires_confirmation(tool_name):
            return await tool_fn(**tool_args)

        # Build consent request
        consent_req = ConsentRequest.create(
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            policy=policy,
        )
        self._pending[consent_req.request_id] = consent_req

        # Present to user and await decision
        decision = await self._requester(consent_req)
        if asyncio.iscoroutine(decision):
            decision = await decision

        if consent_req.is_expired():
            consent_req.decision = ConsentDecision.EXPIRED

        if policy.audit_on_execute:
            self._audit.append({
                "request_id": consent_req.request_id,
                "session_id": session_id,
                "tool_name": tool_name,
                "decision": consent_req.decision.value,
                "sensitivity": policy.sensitivity.value,
                "decided_at": consent_req.decided_at,
            })

        if consent_req.decision != ConsentDecision.APPROVED:
            raise ConsentDeniedError(tool_name, consent_req.decision)

        del self._pending[consent_req.request_id]
        return await tool_fn(**tool_args)

    def audit_log(self) -> list:
        return list(self._audit)


class ConsentDeniedError(Exception):
    def __init__(self, tool_name: str, decision: ConsentDecision):
        self.tool_name = tool_name
        self.decision = decision
        super().__init__(f"consent {decision.value} for tool '{tool_name}'")
```

## Solution 5: Consent Audit Reporter

```python
import time
from typing import List


class ConsentAuditReporter:
    """
    Summarizes consent decisions for compliance reporting.
    High denial rates may indicate injection attempts; high approval
    rates for CRITICAL tools warrant manual review of the audit log.
    """

    def __init__(self, dispatcher: ConsentGatedToolDispatcher):
        self._dispatcher = dispatcher

    def report(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        log = [
            e for e in self._dispatcher.audit_log()
            if e.get("decided_at") and e["decided_at"] >= cutoff
        ]
        if not log:
            return {"window_seconds": window_seconds, "decisions": 0}

        by_decision: dict = {}
        by_tool: dict = {}
        for e in log:
            d = e["decision"]
            by_decision[d] = by_decision.get(d, 0) + 1
            t = e["tool_name"]
            by_tool[t] = by_tool.get(t, 0) + 1

        return {
            "window_seconds": window_seconds,
            "decisions": len(log),
            "by_decision": by_decision,
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])),
            "denial_rate": round(
                by_decision.get("denied", 0) / max(len(log), 1), 4
            ),
        }
```

## Solution 6: Auto-Deny Injection Guard

```python
import re
from typing import Any, Dict


INJECTION_CONSENT_BYPASS_PATTERNS = [
    re.compile(r"(auto.?approve|skip.?confirmation|bypass.?consent|confirm.?yes)", re.IGNORECASE),
    re.compile(r"(without asking|no confirmation needed|do it automatically)", re.IGNORECASE),
]


class InjectionConsentBypassGuard:
    """
    Scans LLM-generated tool call arguments for patterns that suggest
    a prompt injection is trying to pre-approve a sensitive action.
    """

    def scan(self, tool_args: Dict[str, Any]) -> List[dict]:
        findings = []
        for key, value in tool_args.items():
            if not isinstance(value, str):
                continue
            for pattern in INJECTION_CONSENT_BYPASS_PATTERNS:
                if pattern.search(value):
                    findings.append({
                        "arg_name": key,
                        "matched": pattern.pattern,
                        "value_prefix": value[:80],
                    })
        return findings
```

## Comparison

| Approach | Sensitivity Classification | Consent Gating | Audit Trail | Injection Detection | Async Confirmation |
|---|---|---|---|---|---|
| ToolSensitivityRegistry | Yes | No | No | No | No |
| ConsentGatedToolDispatcher | Via registry | Yes | Yes | No | Yes |
| ConsentAuditReporter | No | No | Via dispatcher | No | No |
| InjectionConsentBypassGuard | No | No | No | Yes | No |

**Best for production**: Classify every tool as LOW/MEDIUM/HIGH/CRITICAL before deployment — default-to-HIGH for any tool with side effects not covered. Present consent prompts in the UI, not in the LLM's response: injected content can instruct the LLM to include fake "user approved" text, but cannot produce a genuine UI click. Set `expires_at=120` seconds so consent requests do not remain valid indefinitely if the user walks away. Monitor `denial_rate` from `ConsentAuditReporter`: a spike above 10% in an hour suggests an injection campaign attempting to trigger sensitive actions that users are actively rejecting.
