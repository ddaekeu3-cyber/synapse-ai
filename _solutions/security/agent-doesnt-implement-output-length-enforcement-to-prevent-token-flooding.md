---
title: "Agent Doesn't Implement Output Length Enforcement to Prevent Token Flooding"
description: "Agents that place no ceiling on LLM output length are vulnerable to token flooding: a crafted prompt causes the model to generate extremely long outputs, exhausting downstream token budgets, inflating costs, and potentially smuggling encoded payloads inside verbose responses. Implement output length enforcement that truncates, summarizes, or rejects responses exceeding configurable token and character limits before they are forwarded downstream."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-length-enforcement-to-prevent-token-flooding
tags: [output-length, token-flooding, response-truncation, cost-protection, output-validation, token-budget]
symptoms:
  - "Single crafted request generates a 50,000-token response, exhausting the context budget"
  - "Downstream components receive multi-megabyte LLM outputs they cannot process"
  - "No per-request or per-session token ceiling enforced on model output"
  - "Verbose model outputs inflate API costs with no measurable benefit"
  - "Encoded payloads hidden inside extremely long model-generated text bypass downstream filters"
---

## Why This Happens

LLM output length is controlled by `max_tokens` at the API level, but many agents set this to a high ceiling (or the model default) for every request regardless of task type. A summarization task that legitimately needs 200 tokens and a code generation task that needs 2,000 tokens are given the same ceiling. An attacker who can influence the prompt can cause the model to produce padded, repetitive, or encoded output that fills the ceiling. Output length enforcement applies a post-generation gate that truncates or rejects responses exceeding task-appropriate limits, independent of the `max_tokens` API parameter.

## Solution 1: Output Length Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class LengthViolationAction(str, Enum):
    TRUNCATE = "truncate"       # cut at the limit
    SUMMARIZE = "summarize"     # placeholder: caller summarizes before forwarding
    REJECT = "reject"           # raise an error, do not forward
    WARN = "warn"               # forward but emit a warning


@dataclass
class OutputLengthPolicy:
    max_chars: int = 20_000
    max_tokens_estimate: int = 5_000    # rough estimate at 4 chars/token
    action: LengthViolationAction = LengthViolationAction.TRUNCATE
    truncation_suffix: str = "\n\n[OUTPUT TRUNCATED — length limit enforced]"
    per_task_overrides: Dict[str, int] = field(default_factory=dict)

    def effective_max_chars(self, task_type: str = "") -> int:
        return self.per_task_overrides.get(task_type, self.max_chars)
```

## Solution 2: Output Length Enforcer

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class EnforcementResult:
    text: str
    original_length: int
    enforced_length: int
    violated: bool
    action_taken: str
    task_type: str = ""


class OutputLengthEnforcer:
    """
    Applies a length policy to a raw LLM output string.
    Returns an EnforcementResult with the safe text and audit metadata.
    """

    def __init__(self, policy: OutputLengthPolicy):
        self._policy = policy

    def enforce(self, text: str, task_type: str = "") -> EnforcementResult:
        original_len = len(text)
        limit = self._policy.effective_max_chars(task_type)

        if original_len <= limit:
            return EnforcementResult(
                text=text,
                original_length=original_len,
                enforced_length=original_len,
                violated=False,
                action_taken="none",
                task_type=task_type,
            )

        action = self._policy.action

        if action == LengthViolationAction.TRUNCATE:
            suffix = self._policy.truncation_suffix
            safe_text = text[:limit - len(suffix)] + suffix
            return EnforcementResult(
                text=safe_text,
                original_length=original_len,
                enforced_length=len(safe_text),
                violated=True,
                action_taken="truncate",
                task_type=task_type,
            )

        if action == LengthViolationAction.REJECT:
            raise OutputTooLongError(original_len, limit, task_type)

        if action == LengthViolationAction.WARN:
            return EnforcementResult(
                text=text,
                original_length=original_len,
                enforced_length=original_len,
                violated=True,
                action_taken="warn",
                task_type=task_type,
            )

        # SUMMARIZE: return as-is with flag for caller to handle
        return EnforcementResult(
            text=text,
            original_length=original_len,
            enforced_length=original_len,
            violated=True,
            action_taken="summarize_required",
            task_type=task_type,
        )


class OutputTooLongError(Exception):
    def __init__(self, actual: int, limit: int, task_type: str):
        super().__init__(
            f"LLM output length {actual} chars exceeds limit {limit} for task '{task_type}'"
        )
        self.actual = actual
        self.limit = limit
        self.task_type = task_type
```

## Solution 3: Repetition Density Detector

```python
import re
from typing import Tuple


class RepetitionDensityDetector:
    """
    Detects outputs with abnormally high repetition — a common characteristic
    of token-flooding responses. Uses paragraph-level deduplication ratio
    as a proxy for padding attacks.
    """

    def __init__(self, repetition_threshold: float = 0.40):
        self._threshold = repetition_threshold

    def _paragraphs(self, text: str) -> list:
        return [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]

    def analyze(self, text: str) -> Tuple[float, bool]:
        """
        Returns (repetition_ratio, is_suspicious).
        repetition_ratio = duplicate paragraphs / total paragraphs.
        """
        paras = self._paragraphs(text)
        if len(paras) <= 2:
            return 0.0, False
        unique = len(set(paras))
        ratio = 1.0 - unique / len(paras)
        return round(ratio, 4), ratio >= self._threshold
```

## Solution 4: Session Token Budget Tracker

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Optional


@dataclass
class SessionTokenUsage:
    session_id: str
    output_chars_total: int = 0
    output_chars_ceiling: int = 200_000
    request_count: int = 0
    violations: int = 0
    started_at: float = field(default_factory=time.time)


class SessionTokenBudgetTracker:
    """
    Tracks cumulative output length per session and blocks requests
    once the session's output budget is exhausted.
    """

    def __init__(self, default_ceiling_chars: int = 200_000):
        self._default_ceiling = default_ceiling_chars
        self._sessions: Dict[str, SessionTokenUsage] = {}
        self._lock = Lock()

    def _get_session(self, session_id: str) -> SessionTokenUsage:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionTokenUsage(
                session_id=session_id,
                output_chars_ceiling=self._default_ceiling,
            )
        return self._sessions[session_id]

    def charge(self, session_id: str, chars: int) -> bool:
        """Returns True if budget remains, False if exhausted."""
        with self._lock:
            usage = self._get_session(session_id)
            usage.output_chars_total += chars
            usage.request_count += 1
            if usage.output_chars_total > usage.output_chars_ceiling:
                usage.violations += 1
                return False
            return True

    def remaining(self, session_id: str) -> int:
        with self._lock:
            usage = self._get_session(session_id)
            return max(0, usage.output_chars_ceiling - usage.output_chars_total)

    def summary(self, session_id: str) -> dict:
        with self._lock:
            usage = self._get_session(session_id)
            return {
                "session_id": session_id,
                "output_chars_total": usage.output_chars_total,
                "ceiling": usage.output_chars_ceiling,
                "remaining": max(0, usage.output_chars_ceiling - usage.output_chars_total),
                "request_count": usage.request_count,
                "violations": usage.violations,
            }
```

## Solution 5: Output Length Gate

```python
import time
from typing import Optional


class OutputLengthGate:
    """
    Combines per-response enforcement, repetition detection, and
    session budget tracking into a single gate that all LLM outputs
    must pass before being forwarded downstream.
    """

    def __init__(
        self,
        enforcer: OutputLengthEnforcer,
        repetition_detector: RepetitionDensityDetector,
        budget_tracker: SessionTokenBudgetTracker,
    ):
        self._enforcer = enforcer
        self._repetition = repetition_detector
        self._budget = budget_tracker

    def gate(
        self,
        text: str,
        session_id: str,
        task_type: str = "",
    ) -> dict:
        # Step 1: repetition check
        rep_ratio, is_repetitive = self._repetition.analyze(text)

        # Step 2: per-response length enforcement
        result = self._enforcer.enforce(text, task_type)

        # Step 3: session budget
        budget_ok = self._budget.charge(session_id, result.enforced_length)

        blocked = result.violated and result.action_taken == "reject"
        blocked = blocked or not budget_ok

        return {
            "safe_text": result.text if not blocked else "",
            "blocked": blocked,
            "block_reason": (
                "session_budget_exhausted" if not budget_ok
                else ("output_too_long" if result.violated and result.action_taken == "reject" else None)
            ),
            "original_length": result.original_length,
            "enforced_length": result.enforced_length,
            "violated": result.violated,
            "action_taken": result.action_taken,
            "repetition_ratio": rep_ratio,
            "is_repetitive": is_repetitive,
            "session_remaining_chars": self._budget.remaining(session_id),
            "timestamp": time.time(),
        }
```

## Solution 6: Output Flooding Audit Logger

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class OutputFloodingAuditLogger:
    """
    Records gate assessments that triggered length violations or repetition flags.
    Surfaces sessions with systematic flooding patterns.
    """

    def __init__(self, max_records: int = 10_000):
        self._max = max_records
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, gate_result: dict, session_id: str) -> None:
        if not gate_result.get("violated") and not gate_result.get("is_repetitive"):
            return
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "session_id": session_id,
                "original_length": gate_result.get("original_length"),
                "enforced_length": gate_result.get("enforced_length"),
                "action_taken": gate_result.get("action_taken"),
                "repetition_ratio": gate_result.get("repetition_ratio"),
                "blocked": gate_result.get("blocked", False),
            })
            if len(self._records) > self._max:
                self._records.popleft()

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "flagged_responses": len(recent),
            "blocked_responses": sum(1 for r in recent if r["blocked"]),
            "repetitive_responses": sum(1 for r in recent if r.get("repetition_ratio", 0) >= 0.4),
            "unique_sessions": len({r["session_id"] for r in recent}),
        }
```

## Comparison

| Approach | Per-Response Limit | Repetition Detection | Session Budget | Blocking | Audit |
|---|---|---|---|---|---|
| OutputLengthEnforcer | Yes (truncate/reject) | No | No | Via reject | No |
| RepetitionDensityDetector | No | Yes (paragraph dedup) | No | No | No |
| SessionTokenBudgetTracker | No | No | Yes | Yes | No |
| OutputLengthGate | Via enforcer | Via detector | Via tracker | Yes | No |
| OutputFloodingAuditLogger | No | No | No | No | Yes |

**Best for production**: Set `max_chars` per task type rather than a single global ceiling — summarization tasks rarely need more than 2,000 chars while code generation may legitimately need 15,000. Use `LengthViolationAction.TRUNCATE` by default and `REJECT` only for tool-call outputs where truncation would produce malformed JSON. Always run `RepetitionDensityDetector` in parallel with length enforcement — repetitive padding is the attack signature, not just raw length. Alert via `OutputFloodingAuditLogger` when a single session accounts for more than 3 violations within an hour.
