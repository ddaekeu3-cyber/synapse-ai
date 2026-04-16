---
title: "Agent Doesn't Implement Multi-Factor Authentication for Sensitive Tool Calls"
description: "Agents that perform high-risk actions (fund transfers, account deletions, privilege grants) authenticate only once at session start. Implement step-up MFA at the tool-call boundary so destructive or irreversible actions require fresh user verification."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-multi-factor-authentication-for-sensitive-tool-calls
tags: [mfa, step-up-auth, sensitive-tools, security, authentication, authorization]
symptoms:
  - "Single login grants unlimited access to destructive tool calls for the session lifetime"
  - "No re-authentication before wire transfers, deletions, or privilege escalation"
  - "Stolen session token allows attacker to execute high-risk agent actions without MFA"
  - "Compliance audit flags missing step-up verification for financial operations"
  - "No cooling-off period between consecutive high-risk tool invocations"
---

## Why This Happens

Agents inherit their authentication from the session that spawned them. A token valid at login stays valid for the entire conversation. If a user authenticates once with a password, a compromised session or prompt injection can silently invoke fund transfers, schema drops, or permission grants without any additional challenge. Step-up MFA adds a second factor at the point of sensitive action, limiting the blast radius of session compromise.

## Solution 1: Sensitivity Tier Registry with Step-Up Challenge Gate

```python
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Dict, Optional, Set
import asyncio
import time

class SensitivityTier(IntEnum):
    LOW = 0       # read-only, reversible
    MEDIUM = 1    # writes, but undoable
    HIGH = 2      # irreversible writes
    CRITICAL = 3  # financial, privilege, deletion

@dataclass
class ToolSensitivity:
    tool_name: str
    tier: SensitivityTier
    requires_mfa: bool = True
    mfa_cooldown_seconds: int = 300   # re-use recent verification within window
    max_calls_per_verification: int = 1  # CRITICAL tools: 1 call per MFA

TOOL_SENSITIVITY_REGISTRY: Dict[str, ToolSensitivity] = {
    "read_file":           ToolSensitivity("read_file",           SensitivityTier.LOW,      requires_mfa=False),
    "search_web":          ToolSensitivity("search_web",          SensitivityTier.LOW,      requires_mfa=False),
    "write_file":          ToolSensitivity("write_file",          SensitivityTier.MEDIUM,   requires_mfa=False),
    "send_email":          ToolSensitivity("send_email",          SensitivityTier.MEDIUM,   requires_mfa=True,  mfa_cooldown_seconds=600),
    "delete_record":       ToolSensitivity("delete_record",       SensitivityTier.HIGH,     requires_mfa=True,  mfa_cooldown_seconds=120),
    "transfer_funds":      ToolSensitivity("transfer_funds",      SensitivityTier.CRITICAL, requires_mfa=True,  mfa_cooldown_seconds=0, max_calls_per_verification=1),
    "grant_admin_role":    ToolSensitivity("grant_admin_role",    SensitivityTier.CRITICAL, requires_mfa=True,  mfa_cooldown_seconds=0, max_calls_per_verification=1),
    "drop_database_table": ToolSensitivity("drop_database_table", SensitivityTier.CRITICAL, requires_mfa=True,  mfa_cooldown_seconds=0, max_calls_per_verification=1),
}

@dataclass
class MFAVerification:
    verified_at: float
    method: str  # totp | sms | push
    calls_used: int = 0

class StepUpMFAGate:
    """
    Checks whether a tool call needs fresh MFA verification.
    Integrates with any async MFA challenge function supplied at construction.
    """

    def __init__(self, challenge_fn: Callable[[str, str], asyncio.Future]):
        """
        challenge_fn(user_id, method) -> bool: coroutine that
        sends the MFA challenge and returns True if user passed.
        """
        self._challenge_fn = challenge_fn
        self._verifications: Dict[str, MFAVerification] = {}  # user_id -> latest

    def _needs_verification(self, user_id: str, sensitivity: ToolSensitivity) -> bool:
        if not sensitivity.requires_mfa:
            return False
        if sensitivity.mfa_cooldown_seconds == 0:
            return True  # CRITICAL: always require fresh MFA
        v = self._verifications.get(user_id)
        if v is None:
            return True
        age = time.monotonic() - v.verified_at
        if age > sensitivity.mfa_cooldown_seconds:
            return True
        if v.calls_used >= sensitivity.max_calls_per_verification:
            return True
        return False

    async def authorize(self, user_id: str, tool_name: str) -> None:
        """Raises PermissionError if MFA is required but not passed."""
        sensitivity = TOOL_SENSITIVITY_REGISTRY.get(tool_name)
        if sensitivity is None or not sensitivity.requires_mfa:
            return

        if self._needs_verification(user_id, sensitivity):
            passed = await self._challenge_fn(user_id, "totp")
            if not passed:
                raise PermissionError(
                    f"MFA challenge failed for user '{user_id}' on tool '{tool_name}'"
                )
            self._verifications[user_id] = MFAVerification(
                verified_at=time.monotonic(), method="totp"
            )

        v = self._verifications[user_id]
        v.calls_used += 1

    def revoke(self, user_id: str) -> None:
        self._verifications.pop(user_id, None)
```

## Solution 2: TOTP Verifier with Backup Codes

```python
import hmac
import hashlib
import struct
import time
import secrets
import base64
from typing import List, Optional

class TOTPVerifier:
    """
    RFC 6238 TOTP implementation for step-up MFA challenges.
    Supports ±1 window tolerance and backup recovery codes.
    """

    DIGITS = 6
    STEP = 30  # seconds
    WINDOW = 1  # accept codes from t-1 to t+1

    def __init__(self, secret_b32: str, backup_codes: Optional[List[str]] = None):
        self._secret = base64.b32decode(secret_b32.upper())
        self._backup_codes: Set[str] = set(backup_codes or [])

    def _hotp(self, counter: int) -> str:
        msg = struct.pack(">Q", counter)
        h = hmac.new(self._secret, msg, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(code % (10 ** self.DIGITS)).zfill(self.DIGITS)

    def verify(self, code: str) -> bool:
        if not code or len(code) != self.DIGITS:
            return False
        # Check backup codes first
        if code in self._backup_codes:
            self._backup_codes.discard(code)
            return True
        t = int(time.time()) // self.STEP
        for delta in range(-self.WINDOW, self.WINDOW + 1):
            if hmac.compare_digest(self._hotp(t + delta), code):
                return True
        return False

    @staticmethod
    def generate_secret() -> str:
        raw = secrets.token_bytes(20)
        return base64.b32encode(raw).decode()

    @staticmethod
    def generate_backup_codes(n: int = 8) -> List[str]:
        return [secrets.token_hex(5).upper() for _ in range(n)]


class MFAChallengeService:
    """
    Manages per-user TOTP secrets and exposes the challenge_fn
    interface expected by StepUpMFAGate.
    """

    def __init__(self, totp_secret_store):
        self._store = totp_secret_store  # {user_id: secret_b32}
        self._pending: Dict[str, str] = {}  # user_id -> expected_code (for SMS/push flows)

    async def challenge(self, user_id: str, method: str) -> bool:
        """
        For TOTP: prompt user for current TOTP code and verify it.
        For SMS/push: send OTP and wait for callback.
        """
        secret = await self._store.get(user_id)
        if not secret:
            raise ValueError(f"No MFA configured for user {user_id}")

        if method == "totp":
            # In a real agent, this would be delivered as a tool-call response
            # asking the user to provide their TOTP code
            code = await self._request_totp_from_user(user_id)
            verifier = TOTPVerifier(secret)
            return verifier.verify(code)
        raise NotImplementedError(f"MFA method '{method}' not implemented")

    async def _request_totp_from_user(self, user_id: str) -> str:
        # Placeholder: in production this pauses the agent and prompts the user
        raise NotImplementedError("Override to integrate with your agent's user interaction layer")
```

## Solution 3: Confirmation Token Flow for Human-in-the-Loop

```python
import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class PendingConfirmation:
    token: str
    user_id: str
    tool_name: str
    tool_args: dict
    created_at: float
    ttl_seconds: float = 120.0

class HumanConfirmationGate:
    """
    For CRITICAL actions: agent proposes the action and issues a confirmation
    token. The human must supply the token back to authorize execution.
    This works even in chat interfaces where TOTP is awkward.
    """

    def __init__(self):
        self._pending: Dict[str, PendingConfirmation] = {}

    def propose(self, user_id: str, tool_name: str, tool_args: dict) -> str:
        """
        Returns a short confirmation token that the agent shows to the user.
        The agent says: "To proceed with <action>, reply with: CONFIRM-<token>"
        """
        token = secrets.token_hex(4).upper()  # e.g. "A3F9B2C1"
        self._pending[token] = PendingConfirmation(
            token=token, user_id=user_id,
            tool_name=tool_name, tool_args=tool_args,
            created_at=time.monotonic(),
        )
        return token

    def authorize(self, user_id: str, token: str) -> Optional[PendingConfirmation]:
        """
        Call this when the user supplies the confirmation token.
        Returns the pending action if valid and not expired, else None.
        """
        conf = self._pending.get(token)
        if conf is None:
            return None
        if conf.user_id != user_id:
            return None
        if time.monotonic() - conf.created_at > conf.ttl_seconds:
            del self._pending[token]
            return None
        del self._pending[token]
        return conf

    def pending_count(self, user_id: str) -> int:
        now = time.monotonic()
        return sum(
            1 for c in self._pending.values()
            if c.user_id == user_id and now - c.created_at <= c.ttl_seconds
        )


class ConfirmationAwareAgent:
    def __init__(self, gate: HumanConfirmationGate):
        self.gate = gate

    async def request_sensitive_action(
        self, user_id: str, tool_name: str, tool_args: dict
    ) -> str:
        """Returns a message to show the user, including the confirmation token."""
        token = self.gate.propose(user_id, tool_name, tool_args)
        summary = self._summarize(tool_name, tool_args)
        return (
            f"I need your confirmation to proceed:\n\n"
            f"  Action: **{summary}**\n\n"
            f"  To authorize, reply: `CONFIRM-{token}`\n"
            f"  This token expires in 2 minutes."
        )

    def _summarize(self, tool_name: str, args: dict) -> str:
        if tool_name == "transfer_funds":
            return f"Transfer ${args.get('amount')} to {args.get('recipient')}"
        if tool_name == "delete_record":
            return f"Delete record {args.get('record_id')} from {args.get('table')}"
        return f"{tool_name}({', '.join(f'{k}={v}' for k, v in args.items())})"
```

## Solution 4: Risk-Scored Authorization with Adaptive MFA

```python
from dataclasses import dataclass
from typing import Dict, List

@dataclass
class RiskSignal:
    name: str
    score: float   # 0.0 (no risk) to 1.0 (maximum risk)
    reason: str

class RiskScorer:
    """
    Combines multiple risk signals to produce an overall risk score.
    Higher score → stronger MFA requirement.
    """

    def score(self, user_id: str, tool_name: str, context: dict) -> List[RiskSignal]:
        signals: List[RiskSignal] = []

        # New IP or device
        if context.get("ip_changed"):
            signals.append(RiskSignal("ip_change", 0.4, "Request from new IP address"))

        # Unusual hour
        hour = context.get("hour_utc", 12)
        if hour < 6 or hour > 22:
            signals.append(RiskSignal("off_hours", 0.2, f"Request at hour={hour} UTC"))

        # High-value amount
        amount = context.get("amount", 0)
        if amount > 10_000:
            signals.append(RiskSignal("high_value", min(amount / 100_000, 1.0), f"Amount={amount}"))

        # Velocity: many tool calls in short window
        call_rate = context.get("calls_per_minute", 0)
        if call_rate > 10:
            signals.append(RiskSignal("high_velocity", min(call_rate / 60, 1.0), f"rate={call_rate}/min"))

        return signals

    def total_score(self, signals: List[RiskSignal]) -> float:
        if not signals:
            return 0.0
        # Additive capped at 1.0
        return min(sum(s.score for s in signals), 1.0)

class AdaptiveMFAPolicy:
    """Maps risk score ranges to required MFA strength."""

    THRESHOLDS = [
        (0.0, 0.2, "none"),         # low risk: no MFA
        (0.2, 0.5, "totp"),         # moderate: TOTP
        (0.5, 0.8, "push"),         # high: push notification
        (0.8, 1.0, "hardware_key"), # critical: hardware key or human confirmation
    ]

    def required_method(self, score: float) -> str:
        for lo, hi, method in self.THRESHOLDS:
            if lo <= score < hi:
                return method
        return "hardware_key"

class RiskAdaptiveMFAGate:
    def __init__(self, scorer: RiskScorer, policy: AdaptiveMFAPolicy, mfa_service):
        self.scorer = scorer
        self.policy = policy
        self.mfa_service = mfa_service

    async def check(self, user_id: str, tool_name: str, context: dict) -> None:
        signals = self.scorer.score(user_id, tool_name, context)
        score = self.scorer.total_score(signals)
        method = self.policy.required_method(score)

        if method == "none":
            return

        passed = await self.mfa_service.challenge(user_id, method)
        if not passed:
            raise PermissionError(
                f"Risk-adaptive MFA ({method}, score={score:.2f}) failed for "
                f"user={user_id} tool={tool_name}. Signals: {[s.reason for s in signals]}"
            )
```

## Solution 5: Audit Log for All MFA Events

```python
import json
import time
from dataclasses import dataclass, asdict
from typing import List, Optional

@dataclass
class MFAEvent:
    event_type: str       # challenge_issued | challenge_passed | challenge_failed | revoked
    user_id: str
    tool_name: Optional[str]
    method: str
    risk_score: float
    ip_address: Optional[str]
    timestamp: float
    session_id: str

class MFAAuditLog:
    """Append-only audit log for all step-up MFA events."""

    def __init__(self, sink):
        self._sink = sink  # file, DB, SIEM

    async def record(self, event: MFAEvent) -> None:
        payload = json.dumps(asdict(event))
        await self._sink.append(payload)

    async def query_failed(self, user_id: str, window_seconds: float = 3600) -> List[MFAEvent]:
        cutoff = time.time() - window_seconds
        return [
            e for e in await self._sink.load_all()
            if e["user_id"] == user_id
            and e["event_type"] == "challenge_failed"
            and e["timestamp"] >= cutoff
        ]

    async def detect_brute_force(self, user_id: str, threshold: int = 5) -> bool:
        recent_failures = await self.query_failed(user_id, window_seconds=600)
        return len(recent_failures) >= threshold


class AuditedMFAGate:
    def __init__(self, inner_gate: StepUpMFAGate, audit_log: MFAAuditLog, session_id: str):
        self._gate = inner_gate
        self._audit = audit_log
        self.session_id = session_id

    async def authorize(self, user_id: str, tool_name: str, ip: Optional[str] = None) -> None:
        if await self._audit.detect_brute_force(user_id):
            await self._audit.record(MFAEvent(
                event_type="challenge_issued", user_id=user_id, tool_name=tool_name,
                method="blocked", risk_score=1.0, ip_address=ip,
                timestamp=time.time(), session_id=self.session_id,
            ))
            raise PermissionError(f"User {user_id} temporarily blocked due to repeated MFA failures")

        try:
            await self._gate.authorize(user_id, tool_name)
            await self._audit.record(MFAEvent(
                event_type="challenge_passed", user_id=user_id, tool_name=tool_name,
                method="totp", risk_score=0.0, ip_address=ip,
                timestamp=time.time(), session_id=self.session_id,
            ))
        except PermissionError as exc:
            await self._audit.record(MFAEvent(
                event_type="challenge_failed", user_id=user_id, tool_name=tool_name,
                method="totp", risk_score=0.0, ip_address=ip,
                timestamp=time.time(), session_id=self.session_id,
            ))
            raise
```

## Solution 6: Agent Middleware that Intercepts Tool Calls

```python
from typing import Any, Callable, Awaitable

class MFAToolInterceptor:
    """
    Drop-in middleware that wraps the agent's tool dispatcher.
    Any tool in the registry triggers MFA before execution.
    """

    def __init__(
        self,
        tool_dispatcher: Callable[[str, dict], Awaitable[Any]],
        mfa_gate: StepUpMFAGate,
        user_id: str,
    ):
        self._dispatch = tool_dispatcher
        self._gate = mfa_gate
        self._user_id = user_id

    async def call_tool(self, tool_name: str, tool_args: dict) -> Any:
        await self._gate.authorize(self._user_id, tool_name)
        return await self._dispatch(tool_name, tool_args)

    @classmethod
    def wrap(cls, agent_class):
        """Class decorator: wraps call_tool on any agent class."""
        original = agent_class.call_tool

        async def intercepted(self_agent, tool_name: str, tool_args: dict) -> Any:
            if hasattr(self_agent, "_mfa_gate") and hasattr(self_agent, "_user_id"):
                await self_agent._mfa_gate.authorize(self_agent._user_id, tool_name)
            return await original(self_agent, tool_name, tool_args)

        agent_class.call_tool = intercepted
        return agent_class
```

## Comparison

| Approach | MFA Mechanism | Re-auth Frequency | Best For |
|---|---|---|---|
| StepUpMFAGate + Registry | TOTP, configurable cooldown | Per-tier cooldown window | Standard session-based agents |
| TOTPVerifier + MFAChallengeService | RFC 6238 TOTP + backup codes | On demand | Chat agents with TOTP apps |
| HumanConfirmationGate | Confirmation token in chat | Per CRITICAL action | LLM chat interfaces, no TOTP |
| RiskAdaptiveMFAGate | Score-driven method selection | Risk-proportional | High-security financial agents |
| AuditedMFAGate | Audit + brute-force lockout | On challenge | Compliance-required environments |
| MFAToolInterceptor | Middleware wrap | Per-tool-call gate check | Drop-in for existing agents |

**Best choice for most agents**: Pair `StepUpMFAGate` (tier registry + cooldown) with `AuditedMFAGate` (brute-force detection + SIEM export). For pure chat agents without a TOTP app flow, use `HumanConfirmationGate` as the step-up mechanism.
