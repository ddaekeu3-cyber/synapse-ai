---
title: "Agent Doesn't Implement Prompt Length Limiting to Prevent Context Flooding"
description: "Agents that accept arbitrary-length user inputs without capping them are vulnerable to context flooding attacks: a malicious user submits a prompt containing 50,000 tokens of carefully crafted content that pushes the system prompt, tool results, and conversation history out of the context window — effectively erasing the agent's instructions and memory. Implement prompt length limiting that enforces per-input and per-session token budgets, rejects or truncates inputs that exceed them, and alerts on sustained flooding attempts."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-prompt-length-limiting-to-prevent-context-flooding
tags: [context-flooding, prompt-length, token-budget, input-limiting, denial-of-service, context-window-attack]
symptoms:
  - "A user submits a 50,000-token message that pushes the system prompt out of the context window"
  - "No per-input token limit — any length of user message is accepted and forwarded to the LLM"
  - "Context window fills with user-supplied content, displacing tool results and agent instructions"
  - "Single-session flooding consumes the entire monthly token budget in minutes"
  - "No per-session cumulative token limit — a user can send large messages repeatedly"
---

## Why This Happens

LLM APIs accept inputs up to their context window size and charge for every token. Without a length limit at the application layer, a user can submit messages of any length — limited only by HTTP request size limits, which are typically several megabytes. A 100,000-token message in a 128,000-token context window leaves only 28,000 tokens for system prompt, tool schemas, history, and response. Flooding attacks do not need to be a single message: a user who sends ten 10,000-token messages in succession fills the context over multiple turns. Per-input and per-session limits are both necessary.

## Solution 1: Input Length Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class LengthLimitAction(str, Enum):
    REJECT = "reject"         # return error, do not process
    TRUNCATE = "truncate"     # silently truncate to limit
    WARN_AND_TRUNCATE = "warn_and_truncate"   # truncate and inform user


@dataclass
class InputLengthPolicy:
    max_chars_per_message: int = 20_000        # ~5,000 tokens
    max_tokens_per_message_estimate: int = 5_000
    max_chars_per_session: int = 200_000       # ~50,000 tokens per session
    max_messages_per_session: int = 100
    action_on_message_exceeded: LengthLimitAction = LengthLimitAction.REJECT
    action_on_session_exceeded: LengthLimitAction = LengthLimitAction.REJECT
    chars_per_token_estimate: float = 4.0

    def token_estimate(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token_estimate))

    def message_exceeds_limit(self, text: str) -> bool:
        return len(text) > self.max_chars_per_message
```

## Solution 2: Session Input Budget Tracker

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List


@dataclass
class SessionInputStats:
    session_id: str
    user_id: str
    total_chars: int = 0
    message_count: int = 0
    rejected_count: int = 0
    truncated_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_message_at: float = field(default_factory=time.time)

    def record_message(self, chars: int, rejected: bool = False, truncated: bool = False) -> None:
        self.total_chars += chars
        self.message_count += 1
        self.last_message_at = time.time()
        if rejected:
            self.rejected_count += 1
        if truncated:
            self.truncated_count += 1


class SessionInputBudgetTracker:
    """
    Tracks cumulative input usage per session to enforce per-session limits.
    """

    def __init__(self, max_sessions: int = 10000, session_ttl_seconds: float = 3600.0):
        self._sessions: Dict[str, SessionInputStats] = {}
        self._lock = Lock()
        self._max = max_sessions
        self._ttl = session_ttl_seconds

    def get_or_create(self, session_id: str, user_id: str = "") -> SessionInputStats:
        with self._lock:
            if session_id not in self._sessions:
                self._evict()
                self._sessions[session_id] = SessionInputStats(
                    session_id=session_id, user_id=user_id
                )
            return self._sessions[session_id]

    def session_exceeds_limit(
        self, stats: SessionInputStats, policy: InputLengthPolicy
    ) -> bool:
        return (
            stats.total_chars > policy.max_chars_per_session
            or stats.message_count >= policy.max_messages_per_session
        )

    def _evict(self) -> None:
        if len(self._sessions) < self._max:
            return
        cutoff = time.time() - self._ttl
        stale = [
            sid for sid, s in self._sessions.items()
            if s.last_message_at < cutoff
        ]
        for sid in stale[:max(1, len(stale))]:
            del self._sessions[sid]

    def all_stats(self) -> list:
        with self._lock:
            return list(self._sessions.values())
```

## Solution 3: Input Length Enforcer

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class LengthCheckResult:
    allowed: bool
    action_taken: Optional[LengthLimitAction]
    original_length: int
    effective_length: int
    effective_text: str
    rejection_reason: str = ""
    was_truncated: bool = False


class InputLengthEnforcer:
    """
    Applies per-message and per-session length limits.
    Returns a LengthCheckResult indicating whether the input was allowed,
    truncated, or rejected.
    """

    def __init__(
        self,
        policy: InputLengthPolicy,
        tracker: SessionInputBudgetTracker,
        audit_logger: "InputLengthAuditLogger",
    ):
        self._policy = policy
        self._tracker = tracker
        self._logger = audit_logger

    def check(
        self,
        text: str,
        session_id: str,
        user_id: str = "",
    ) -> LengthCheckResult:
        original_len = len(text)
        stats = self._tracker.get_or_create(session_id, user_id)

        # Check per-session limit first
        if self._tracker.session_exceeds_limit(stats, self._policy):
            result = LengthCheckResult(
                allowed=False,
                action_taken=self._policy.action_on_session_exceeded,
                original_length=original_len,
                effective_length=0,
                effective_text="",
                rejection_reason=f"session cumulative limit exceeded ({stats.total_chars} chars, {stats.message_count} messages)",
            )
            stats.record_message(0, rejected=True)
            self._logger.record(result, session_id, user_id, "session_limit")
            return result

        # Check per-message limit
        if self._policy.message_exceeds_limit(text):
            action = self._policy.action_on_message_exceeded
            if action == LengthLimitAction.REJECT:
                result = LengthCheckResult(
                    allowed=False,
                    action_taken=action,
                    original_length=original_len,
                    effective_length=0,
                    effective_text="",
                    rejection_reason=f"message exceeds {self._policy.max_chars_per_message} char limit",
                )
                stats.record_message(0, rejected=True)
                self._logger.record(result, session_id, user_id, "message_limit")
                return result
            else:
                # Truncate
                truncated = text[:self._policy.max_chars_per_message]
                result = LengthCheckResult(
                    allowed=True,
                    action_taken=action,
                    original_length=original_len,
                    effective_length=len(truncated),
                    effective_text=truncated,
                    was_truncated=True,
                )
                stats.record_message(len(truncated), truncated=True)
                self._logger.record(result, session_id, user_id, "message_truncated")
                return result

        result = LengthCheckResult(
            allowed=True,
            action_taken=None,
            original_length=original_len,
            effective_length=original_len,
            effective_text=text,
        )
        stats.record_message(original_len)
        return result
```

## Solution 4: Flooding Pattern Detector

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, Tuple


class FloodingPatternDetector:
    """
    Detects sustained context flooding by tracking message volume and
    size per session in a sliding window. Alerts on sessions that
    submit many large messages in a short period.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        large_message_threshold_chars: int = 10_000,
        flooding_trigger_count: int = 5,    # N large messages in window
    ):
        self._window = window_seconds
        self._threshold = large_message_threshold_chars
        self._trigger = flooding_trigger_count
        self._events: Dict[str, Deque[Tuple[float, int]]] = {}
        self._lock = Lock()

    def record(self, session_id: str, message_chars: int) -> bool:
        """Returns True if a flooding pattern is detected."""
        now = time.time()
        with self._lock:
            if session_id not in self._events:
                self._events[session_id] = deque()
            dq = self._events[session_id]
            cutoff = now - self._window
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            if message_chars >= self._threshold:
                dq.append((now, message_chars))
            return len(dq) >= self._trigger

    def flooded_sessions(self) -> list:
        now = time.time()
        with self._lock:
            cutoff = now - self._window
            return [
                sid for sid, dq in self._events.items()
                if sum(1 for ts, _ in dq if ts >= cutoff) >= self._trigger
            ]
```

## Solution 5: Input Length Audit Logger

```python
import time
from typing import List


class InputLengthAuditLogger:
    """
    Records all length enforcement actions for security and billing analysis.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        result: LengthCheckResult,
        session_id: str,
        user_id: str,
        trigger: str,
    ) -> None:
        if result.action_taken is None and not result.was_truncated:
            return  # only log enforcement events
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "user_id": user_id,
            "trigger": trigger,
            "allowed": result.allowed,
            "action": result.action_taken.value if result.action_taken else None,
            "original_length": result.original_length,
            "effective_length": result.effective_length,
            "truncated": result.was_truncated,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "events": 0}
        rejected = [r for r in recent if not r["allowed"]]
        truncated = [r for r in recent if r["truncated"]]
        return {
            "window_seconds": window_seconds,
            "events": len(recent),
            "rejected": len(rejected),
            "truncated": len(truncated),
            "avg_original_chars": round(
                sum(r["original_length"] for r in recent) / len(recent), 0
            ),
        }
```

## Solution 6: Length Limit Dashboard

```python
import time


class InputLengthLimitDashboard:
    def __init__(
        self,
        enforcer: InputLengthEnforcer,
        flood_detector: FloodingPatternDetector,
        audit_logger: InputLengthAuditLogger,
    ):
        self._enforcer = enforcer
        self._flood = flood_detector
        self._logger = audit_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "enforcement_summary_1h": self._logger.summary(3600.0),
            "active_flooding_sessions": self._flood.flooded_sessions(),
            "policy": {
                "max_chars_per_message": self._enforcer._policy.max_chars_per_message,
                "max_chars_per_session": self._enforcer._policy.max_chars_per_session,
                "action_on_message_exceeded": self._enforcer._policy.action_on_message_exceeded.value,
            },
        }
```

## Comparison

| Approach | Per-Message Limit | Per-Session Limit | Flooding Detection | Truncation Support | Audit Log |
|---|---|---|---|---|---|
| InputLengthPolicy | Yes | Yes | No | Yes | No |
| SessionInputBudgetTracker | No | Yes (cumulative) | No | No | No |
| InputLengthEnforcer | Via policy | Via tracker | No | Yes | Via logger |
| FloodingPatternDetector | No | No | Yes (sliding window) | No | No |
| InputLengthAuditLogger | No | No | No | No | Yes |

**Best for production**: Set `action_on_message_exceeded=REJECT` rather than `TRUNCATE` for user-facing agents — silently truncating user input can cause confusing behavior where the agent answers a different question than the user asked. Reserve `TRUNCATE` for internal pipelines where the cost of rejection is high and partial context is acceptable. Set `max_chars_per_session` based on your monthly token budget divided by expected concurrent sessions and average session duration — this prevents a single session from consuming a disproportionate share of resources. Alert when `FloodingPatternDetector.flooded_sessions()` is non-empty: five large messages in 60 seconds from a single session is almost certainly automated, not a human user, and warrants rate limiting at the API gateway layer rather than just at the input enforcer.
