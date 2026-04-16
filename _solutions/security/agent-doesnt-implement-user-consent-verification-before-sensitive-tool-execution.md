---
title: "Agent Doesn't Implement User Consent Verification Before Sensitive Tool Execution"
description: "Agents that invoke sensitive tools — file deletion, payment processing, email sending, account modification — without verifying that the user explicitly consented to that specific action in the current session are vulnerable to prompt injection and indirect authorization escalation: an attacker-controlled document can instruct the agent to take an action the user never requested. Implement user consent verification that requires an explicit confirmation signal before executing any tool in a configurable set of sensitive operations."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-user-consent-verification-before-sensitive-tool-execution
tags: [user-consent, sensitive-tool-gating, authorization, prompt-injection-defense, confirmation-required, action-verification]
symptoms:
  - "Agent executes file deletions or sends emails based on instructions in retrieved documents"
  - "No explicit user confirmation is required before state-changing operations"
  - "Prompt injection in a retrieved document can trigger payments or account changes"
  - "The agent treats 'the document says to do X' as equivalent to 'the user asked for X'"
  - "Audit log shows sensitive operations with no corresponding user confirmation event"
---

## Why This Happens

Agents receive instructions from multiple sources: user messages, system prompts, tool results, and retrieved documents. All of these end up in the model's context, and the model cannot reliably distinguish a user instruction from an injected instruction in a retrieved document. A consent verification gate does not rely on the model's judgment — it is a hard enforcement point in the tool dispatch layer that requires a recorded user confirmation before any sensitive tool call proceeds, regardless of what the model decided to do. This moves the authorization decision out of the model's context window and into auditable application logic.

## Solution 1: Sensitive Tool Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class SensitivityLevel(str, Enum):
    LOW = "low"           # read-only, no side effects
    MEDIUM = "medium"     # reversible state changes
    HIGH = "high"         # irreversible or high-impact changes
    CRITICAL = "critical" # financial, account, or destructive operations


@dataclass
class SensitiveToolPolicy:
    tool_name: str
    sensitivity: SensitivityLevel
    requires_consent: bool = True
    consent_message: str = ""
    timeout_seconds: float = 120.0     # how long consent remains valid
    max_consent_reuse: int = 1         # how many times consent can be used


def default_sensitive_tools() -> Dict[str, SensitiveToolPolicy]:
    return {
        "send_email": SensitiveToolPolicy(
            tool_name="send_email",
            sensitivity=SensitivityLevel.HIGH,
            consent_message="The agent wants to send an email. Do you approve?",
        ),
        "delete_file": SensitiveToolPolicy(
            tool_name="delete_file",
            sensitivity=SensitivityLevel.CRITICAL,
            consent_message="The agent wants to delete a file. This cannot be undone. Do you approve?",
        ),
        "process_payment": SensitiveToolPolicy(
            tool_name="process_payment",
            sensitivity=SensitivityLevel.CRITICAL,
            consent_message="The agent wants to process a payment. Do you approve?",
        ),
        "modify_user_account": SensitiveToolPolicy(
            tool_name="modify_user_account",
            sensitivity=SensitivityLevel.HIGH,
            consent_message="The agent wants to modify your account settings. Do you approve?",
        ),
        "execute_code": SensitiveToolPolicy(
            tool_name="execute_code",
            sensitivity=SensitivityLevel.HIGH,
            consent_message="The agent wants to execute code. Do you approve?",
        ),
    }
```

## Solution 2: Consent Record

```python
import time
import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass
class ConsentRecord:
    consent_id: str
    session_id: str
    tool_name: str
    user_confirmation: str   # the raw user message that constitutes consent
    granted_at: float
    expires_at: float
    times_used: int = 0
    max_uses: int = 1
    revoked: bool = False

    @classmethod
    def create(
        cls,
        session_id: str,
        tool_name: str,
        user_confirmation: str,
        timeout_seconds: float = 120.0,
        max_uses: int = 1,
    ) -> "ConsentRecord":
        now = time.time()
        return cls(
            consent_id=str(uuid.uuid4())[:12],
            session_id=session_id,
            tool_name=tool_name,
            user_confirmation=user_confirmation,
            granted_at=now,
            expires_at=now + timeout_seconds,
            max_uses=max_uses,
        )

    def is_valid(self) -> bool:
        if self.revoked:
            return False
        if time.time() > self.expires_at:
            return False
        if self.times_used >= self.max_uses:
            return False
        return True

    def consume(self) -> None:
        self.times_used += 1
```

## Solution 3: Consent Verifier

```python
import re
from typing import List


POSITIVE_CONSENT_PATTERNS = [
    r"\byes\b",
    r"\bapprove[d]?\b",
    r"\bconfirm[ed]?\b",
    r"\bgo ahead\b",
    r"\bproceed\b",
    r"\ballow[ed]?\b",
    r"\bok(ay)?\b",
    r"\bdo it\b",
    r"\bauthorize[d]?\b",
]


class ConsentVerifier:
    """
    Determines whether a user message constitutes explicit consent
    for a named tool execution. Returns False for ambiguous messages.
    """

    def __init__(self):
        self._patterns = [
            re.compile(p, re.IGNORECASE) for p in POSITIVE_CONSENT_PATTERNS
        ]

    def is_explicit_consent(self, user_message: str, tool_name: str) -> bool:
        """
        Returns True only if the message contains an unambiguous
        affirmative signal. Ambiguous messages return False.
        """
        normalized = user_message.strip().lower()
        if not normalized:
            return False

        matches = sum(1 for p in self._patterns if p.search(normalized))
        return matches >= 1

    def extract_denial(self, user_message: str) -> bool:
        denial_patterns = [r"\bno\b", r"\bcancel\b", r"\bstop\b", r"\bdon't\b", r"\bdo not\b"]
        for pat in denial_patterns:
            if re.search(pat, user_message, re.IGNORECASE):
                return True
        return False
```

## Solution 4: Consent Store

```python
import threading
import time
from typing import Dict, List, Optional


class ConsentStore:
    """
    Stores consent records by session and tool name.
    Provides lookup and consumption of valid consent.
    """

    def __init__(self):
        self._records: Dict[str, List[ConsentRecord]] = {}
        self._lock = threading.Lock()

    def grant(self, record: ConsentRecord) -> None:
        key = self._key(record.session_id, record.tool_name)
        with self._lock:
            self._records.setdefault(key, []).append(record)

    def consume(self, session_id: str, tool_name: str) -> Optional[ConsentRecord]:
        """Returns and consumes a valid consent record if one exists."""
        key = self._key(session_id, tool_name)
        with self._lock:
            for record in reversed(self._records.get(key, [])):
                if record.is_valid():
                    record.consume()
                    return record
        return None

    def has_valid_consent(self, session_id: str, tool_name: str) -> bool:
        key = self._key(session_id, tool_name)
        with self._lock:
            return any(r.is_valid() for r in self._records.get(key, []))

    def revoke_all(self, session_id: str) -> int:
        revoked = 0
        with self._lock:
            for key, records in self._records.items():
                if key.startswith(f"{session_id}:"):
                    for r in records:
                        if not r.revoked:
                            r.revoked = True
                            revoked += 1
        return revoked

    @staticmethod
    def _key(session_id: str, tool_name: str) -> str:
        return f"{session_id}:{tool_name}"
```

## Solution 5: Consent-Gated Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class ConsentGatedToolDispatcher:
    """
    Enforces consent verification before sensitive tool execution.
    Raises ConsentRequiredError when no valid consent exists,
    and ConsentDeniedError when the user explicitly denied.
    """

    def __init__(
        self,
        policies: Dict[str, SensitiveToolPolicy],
        consent_store: ConsentStore,
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._policies = policies
        self._store = consent_store
        self._audit = audit_fn or (lambda ev: None)
        self._gated_calls = 0
        self._consent_granted = 0
        self._consent_denied = 0

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
        session_id: str = "",
    ) -> Any:
        policy = self._policies.get(tool_name)

        if policy is None or not policy.requires_consent:
            return await tool_fn(**args)

        self._gated_calls += 1

        if not self._store.has_valid_consent(session_id, tool_name):
            self._audit({
                "event": "consent_required",
                "tool_name": tool_name,
                "session_id": session_id,
                "consent_message": policy.consent_message,
                "timestamp": time.time(),
            })
            raise ConsentRequiredError(tool_name, policy.consent_message)

        consent = self._store.consume(session_id, tool_name)
        self._consent_granted += 1
        self._audit({
            "event": "consent_consumed",
            "tool_name": tool_name,
            "session_id": session_id,
            "consent_id": consent.consent_id if consent else "",
            "timestamp": time.time(),
        })

        return await tool_fn(**args)

    def stats(self) -> dict:
        return {
            "gated_calls": self._gated_calls,
            "consent_granted": self._consent_granted,
            "consent_denied": self._consent_denied,
        }


class ConsentRequiredError(Exception):
    def __init__(self, tool_name: str, message: str):
        super().__init__(f"consent required for '{tool_name}': {message}")
        self.tool_name = tool_name
        self.consent_message = message


class ConsentDeniedError(Exception):
    def __init__(self, tool_name: str):
        super().__init__(f"user denied consent for '{tool_name}'")
        self.tool_name = tool_name
```

## Solution 6: Consent Audit Dashboard

```python
import time


class ConsentAuditDashboard:
    """
    Combines dispatcher stats and consent store state for security auditing.
    """

    def __init__(
        self,
        dispatcher: ConsentGatedToolDispatcher,
        store: ConsentStore,
    ):
        self._dispatcher = dispatcher
        self._store = store

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "dispatcher_stats": self._dispatcher.stats(),
        }
```

## Comparison

| Approach | Policy Registry | Consent Detection | Consent Storage | Hard Gate | Audit |
|---|---|---|---|---|---|
| SensitiveToolPolicy | Yes | No | No | No | No |
| ConsentVerifier | No | Yes (regex) | No | No | No |
| ConsentStore | No | No | Yes (session-scoped) | No | No |
| ConsentGatedToolDispatcher | Via policies | No | Via store | Yes | Yes |
| ConsentAuditDashboard | No | No | No | No | Yes |

**Best for production**: Do not rely on the model to request consent — place `ConsentGatedToolDispatcher` as a hard enforcement layer that the model's tool dispatch cannot bypass. Set `max_consent_reuse=1` for CRITICAL tools (each execution requires a fresh confirmation) and `max_consent_reuse=3` with `timeout_seconds=300` for HIGH tools in workflows where a user approves a batch of related actions. Log every `consent_required` event: a pattern of the agent requesting consent for a sensitive tool in a session where the user never explicitly requested that action is a strong signal of prompt injection.
