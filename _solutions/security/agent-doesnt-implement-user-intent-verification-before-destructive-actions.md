---
title: "Agent Doesn't Implement User Intent Verification Before Destructive Actions"
description: "Agents that execute destructive tool calls — deleting files, dropping tables, sending bulk messages, or cancelling orders — without verifying user intent risk catastrophic irreversible actions from ambiguous instructions or prompt injection. Implement a user intent verification layer that classifies action destructiveness, requires explicit confirmation for high-risk operations, and maintains an audit trail of every destructive action authorization."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-user-intent-verification-before-destructive-actions
tags: [intent-verification, destructive-actions, confirmation-gate, action-safety, prompt-injection-defense, authorization]
symptoms:
  - "Agent deletes files or records based on an ambiguous instruction without confirmation"
  - "Prompt injection causes the agent to execute a destructive operation the user never requested"
  - "No distinction between reversible and irreversible tool calls in the execution layer"
  - "Bulk send or mass-delete operations run without any rate or scope confirmation"
  - "Audit log shows destructive actions with no record of user authorization"
---

## Why This Happens

Agents are designed to be helpful and act on instructions, which makes them prone to executing destructive actions when an instruction is ambiguous or when a prompt injection attack is present. A delete operation triggered by "clean up the old records" is semantically plausible but potentially catastrophic if "old" is broader than intended. Without a verification gate, the agent proceeds with maximum scope. Implementing intent verification requires classifying tool calls by destructiveness level, computing scope estimates (how many records, which files), presenting a confirmation request for high-risk operations, and refusing to execute until explicit authorization is recorded.

## Solution 1: Action Destructiveness Classifier

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Pattern
import re


class DestructivenessLevel(str, Enum):
    SAFE = "safe"               # read-only; no side effects
    LOW = "low"                 # reversible write (update, create)
    MEDIUM = "medium"           # potentially irreversible (send message, publish)
    HIGH = "high"               # irreversible or high-blast-radius (delete, drop)
    CRITICAL = "critical"       # catastrophic if wrong (bulk delete, purge, drop table)


@dataclass
class DestructivenessRule:
    tool_name_pattern: str          # regex matched against tool name
    level: DestructivenessLevel
    scope_arg: Optional[str] = None  # argument that indicates blast radius
    keywords: List[str] = field(default_factory=list)  # arg keywords that escalate level


class ActionDestructivenessClassifier:
    """
    Classifies a tool call's destructiveness level using registered rules.
    Escalates level when scope-indicating arguments suggest large blast radius.
    """

    def __init__(self, rules: List[DestructivenessRule]):
        self._rules = rules

    def classify(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> DestructivenessLevel:
        for rule in self._rules:
            if re.search(rule.tool_name_pattern, tool_name, re.IGNORECASE):
                level = rule.level
                # Escalate if scope arg suggests broad impact
                if rule.scope_arg and rule.scope_arg in arguments:
                    scope_val = str(arguments[rule.scope_arg])
                    if scope_val in ("*", "all", "None", "") or re.search(r"\ball\b", scope_val, re.I):
                        level = DestructivenessLevel.CRITICAL
                # Escalate on dangerous keywords in any arg value
                for kw in rule.keywords:
                    for v in arguments.values():
                        if kw.lower() in str(v).lower():
                            if level.value < DestructivenessLevel.HIGH.value:
                                level = DestructivenessLevel.HIGH
                return level
        return DestructivenessLevel.SAFE
```

## Solution 2: Intent Verification Request

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class IntentVerificationRequest:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    tool_name: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    destructiveness: DestructivenessLevel = DestructivenessLevel.SAFE
    scope_description: str = ""         # human-readable impact summary
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    authorized: Optional[bool] = None   # None = pending, True = approved, False = denied
    authorized_at: Optional[float] = None
    authorization_token: str = ""       # echoed back by user to confirm


@dataclass
class IntentVerificationResult:
    request: IntentVerificationRequest
    proceed: bool
    reason: str
```

## Solution 3: Intent Scope Estimator

```python
from typing import Any, Dict, Optional


class IntentScopeEstimator:
    """
    Produces a human-readable description of the blast radius of a
    proposed destructive action based on tool arguments.
    """

    def estimate(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> str:
        parts = []

        # Record count hints
        for key in ("ids", "record_ids", "keys", "items"):
            if key in arguments and isinstance(arguments[key], list):
                n = len(arguments[key])
                parts.append(f"{n} record(s)")

        # Wildcard or broad scope hints
        for key, val in arguments.items():
            sval = str(val)
            if sval in ("*", "all", "None", ""):
                parts.append(f"all {key}")
            elif re.search(r"\ball\b|\bevery\b", sval, re.I):
                parts.append(f"all matching '{key}'")

        # Path hints
        for key in ("path", "file", "directory", "folder"):
            if key in arguments:
                parts.append(f"path: {arguments[key]}")

        if not parts:
            args_summary = ", ".join(f"{k}={v}" for k, v in list(arguments.items())[:3])
            parts.append(args_summary or "unknown scope")

        return f"Tool '{tool_name}' affecting: {'; '.join(parts)}"
```

## Solution 4: Intent Verification Gate

```python
import time
from typing import Callable, Optional


CONFIRMATION_THRESHOLD = DestructivenessLevel.MEDIUM


class IntentVerificationGate:
    """
    Intercepts tool calls at or above the confirmation threshold and
    requests user authorization before allowing execution to proceed.
    High/critical actions require an explicit echoed token.
    """

    def __init__(
        self,
        classifier: ActionDestructivenessClassifier,
        scope_estimator: IntentScopeEstimator,
        confirmation_threshold: DestructivenessLevel = CONFIRMATION_THRESHOLD,
        ask_user_fn: Optional[Callable[[IntentVerificationRequest], bool]] = None,
    ):
        self._classifier = classifier
        self._estimator = scope_estimator
        self._threshold = confirmation_threshold
        self._ask_user = ask_user_fn
        self._pending: dict = {}
        self._audit: list = []

    def _level_value(self, level: DestructivenessLevel) -> int:
        order = {
            DestructivenessLevel.SAFE: 0,
            DestructivenessLevel.LOW: 1,
            DestructivenessLevel.MEDIUM: 2,
            DestructivenessLevel.HIGH: 3,
            DestructivenessLevel.CRITICAL: 4,
        }
        return order[level]

    def evaluate(
        self,
        tool_name: str,
        arguments: dict,
        session_id: str = "",
    ) -> IntentVerificationResult:
        level = self._classifier.classify(tool_name, arguments)

        if self._level_value(level) < self._level_value(self._threshold):
            return IntentVerificationResult(
                request=IntentVerificationRequest(
                    tool_name=tool_name,
                    arguments=arguments,
                    destructiveness=level,
                    session_id=session_id,
                    authorized=True,
                ),
                proceed=True,
                reason="below_confirmation_threshold",
            )

        scope = self._estimator.estimate(tool_name, arguments)
        req = IntentVerificationRequest(
            tool_name=tool_name,
            arguments=arguments,
            destructiveness=level,
            scope_description=scope,
            session_id=session_id,
        )

        authorized = False
        if self._ask_user:
            authorized = self._ask_user(req)
        req.authorized = authorized
        req.authorized_at = time.time()

        self._audit.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "level": level.value,
            "scope": scope,
            "authorized": authorized,
            "session_id": session_id,
        })

        return IntentVerificationResult(
            request=req,
            proceed=authorized,
            reason="user_authorized" if authorized else "user_denied",
        )

    def audit_log(self, limit: int = 100) -> list:
        return self._audit[-limit:]
```

## Solution 5: Verification-Gated Tool Dispatcher

```python
from typing import Any, Callable, Dict


class VerificationGatedToolDispatcher:
    """
    Wraps a tool execution function with the intent verification gate.
    Raises PermissionError if the user denies a destructive action.
    """

    def __init__(
        self,
        gate: IntentVerificationGate,
        execute_fn: Callable[[str, Dict[str, Any]], Any],
    ):
        self._gate = gate
        self._execute = execute_fn
        self._blocked_count = 0
        self._allowed_count = 0

    async def dispatch(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        session_id: str = "",
    ) -> Any:
        verification = self._gate.evaluate(tool_name, arguments, session_id)

        if not verification.proceed:
            self._blocked_count += 1
            raise PermissionError(
                f"Destructive action '{tool_name}' was not authorized by the user. "
                f"Scope: {verification.request.scope_description}"
            )

        self._allowed_count += 1
        return await self._execute(tool_name, arguments)

    def stats(self) -> dict:
        total = self._allowed_count + self._blocked_count
        return {
            "total_dispatched": total,
            "allowed": self._allowed_count,
            "blocked": self._blocked_count,
            "block_rate": round(self._blocked_count / max(total, 1), 4),
        }
```

## Solution 6: Intent Verification Audit Reporter

```python
import time
from typing import List


class IntentVerificationAuditReporter:
    """
    Aggregates audit log entries from the gate and produces
    security reports on destructive action authorization patterns.
    """

    def __init__(self, gate: IntentVerificationGate):
        self._gate = gate

    def report(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._gate._audit if e["ts"] >= cutoff]
        by_level: dict = {}
        for e in recent:
            lv = e["level"]
            by_level.setdefault(lv, {"count": 0, "authorized": 0, "denied": 0})
            by_level[lv]["count"] += 1
            if e["authorized"]:
                by_level[lv]["authorized"] += 1
            else:
                by_level[lv]["denied"] += 1

        return {
            "window_seconds": window_seconds,
            "total_gated_actions": len(recent),
            "authorized": sum(1 for e in recent if e["authorized"]),
            "denied": sum(1 for e in recent if not e["authorized"]),
            "by_level": by_level,
        }
```

## Comparison

| Approach | Destructiveness Classification | Scope Estimation | User Confirmation | Audit | Dispatch Gating |
|---|---|---|---|---|---|
| ActionDestructivenessClassifier | Yes (rules + escalation) | No | No | No | No |
| IntentScopeEstimator | No | Yes (arg analysis) | No | No | No |
| IntentVerificationGate | Via classifier | Via estimator | Yes | Yes | No |
| VerificationGatedToolDispatcher | Via gate | Via gate | Via gate | No | Yes |
| IntentVerificationAuditReporter | No | No | No | Via gate | No |

**Best for production**: Set `confirmation_threshold=HIGH` for automated pipelines where user interaction is not possible — allow LOW and MEDIUM actions silently, but block HIGH/CRITICAL and surface them as structured responses asking the user for explicit confirmation. For CRITICAL actions (bulk delete, drop table), require the user to echo back the scope description as the authorization token — this forces a human to read and acknowledge what will be destroyed. Log every gated action with `session_id` and `scope_description` — a spike in denied HIGH/CRITICAL actions from a single session indicates a prompt injection attack and should trigger a session security alert.
