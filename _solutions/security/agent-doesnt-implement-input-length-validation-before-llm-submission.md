---
title: "Agent Doesn't Implement Input Length Validation Before LLM Submission"
description: "Agents that forward user input to the LLM without length validation enable prompt stuffing attacks, accidental context exhaustion, and runaway token costs. A single user submitting a 500KB document as a chat message can exhaust the context window, trigger maximum-token billing, or push system prompt instructions out of the effective context. Implement input length validation that enforces per-field byte and token limits, truncates oversized inputs gracefully, and detects anomalous length patterns."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-input-length-validation-before-llm-submission
tags: [input-validation, length-limits, prompt-stuffing, context-exhaustion, token-limits, input-sanitization]
symptoms:
  - "User submits a 200KB string as a message and the agent forwards it to the LLM verbatim"
  - "Context window exhausted by user input before system prompt instructions are processed"
  - "No maximum length check on any input field — tool arguments, messages, or file content"
  - "Monthly token costs spike when one user submits oversized inputs repeatedly"
  - "Prompt injection via padding: user adds thousands of spaces before an injected instruction"
---

## Why This Happens

Input validation is applied at the API schema level (type checking, required fields) but not at the content level (byte length, token count, anomalous patterns). The LLM API accepts any string up to its context limit, so oversized inputs are silently accepted and billed. Token counting requires a tokenizer call — an extra dependency that many teams skip — so teams use character length as a proxy. Input length validation must be enforced before the LLM call: rejecting or truncating oversized inputs at the edge is cheaper and safer than billing for them.

## Solution 1: Input Length Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class TruncationStrategy(str, Enum):
    REJECT = "reject"               # return error to caller
    TRUNCATE_END = "truncate_end"   # keep first N chars/tokens
    TRUNCATE_MIDDLE = "truncate_middle"  # keep start + end, drop middle
    SUMMARIZE_HINT = "summarize_hint"    # truncate + append a note


@dataclass
class InputLengthPolicy:
    field_name: str
    max_chars: int
    max_tokens: Optional[int] = None        # if set, enforce token limit too
    min_chars: int = 0
    truncation: TruncationStrategy = TruncationStrategy.REJECT
    truncation_note: str = "[content truncated due to length limit]"
    anomaly_multiplier: float = 10.0        # flag if > N × median length
    allow_empty: bool = False
```

## Solution 2: Token Estimator

```python
import re
from typing import Optional


class TokenEstimator:
    """
    Lightweight token estimator that avoids importing a full tokenizer.
    Uses character-based heuristics: ~4 chars/token for English prose,
    ~2 chars/token for code (shorter tokens), ~6 chars/token for structured data.
    For production, replace estimate() with a real tokenizer call.
    """

    # Rough per-content-type chars-per-token ratios
    _RATIOS = {
        "prose": 4.0,
        "code": 2.5,
        "json": 3.5,
        "default": 4.0,
    }

    @classmethod
    def estimate(cls, text: str, content_type: str = "default") -> int:
        ratio = cls._RATIOS.get(content_type, cls._RATIOS["default"])
        return max(1, int(len(text) / ratio))

    @classmethod
    def content_type_from_text(cls, text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return "json"
        if re.search(r"\bdef \b|\bclass \b|\bimport \b|function\s*\(", stripped):
            return "code"
        return "prose"
```

## Solution 3: Input Length Validator

```python
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class LengthValidationResult:
    field_name: str
    original_length_chars: int
    validated_value: Any
    truncated: bool
    rejected: bool
    error: Optional[str]
    estimated_tokens: int
    anomalous: bool = False

    def is_ok(self) -> bool:
        return not self.rejected


class InputLengthValidator:
    """
    Validates and optionally truncates string inputs according to policy.
    Tracks median length per field to detect anomalous submissions.
    """

    def __init__(self):
        self._policies: Dict[str, InputLengthPolicy] = {}
        self._length_history: Dict[str, List[int]] = {}

    def register(self, policy: InputLengthPolicy) -> None:
        self._policies[policy.field_name] = policy
        self._length_history[policy.field_name] = []

    def validate(self, field_name: str, value: str) -> LengthValidationResult:
        policy = self._policies.get(
            field_name,
            InputLengthPolicy(field_name=field_name, max_chars=32_768),
        )
        original_len = len(value)
        ctype = TokenEstimator.content_type_from_text(value)
        token_est = TokenEstimator.estimate(value, ctype)

        # Track history for anomaly detection
        history = self._length_history.setdefault(field_name, [])
        history.append(original_len)
        if len(history) > 500:
            history.pop(0)

        anomalous = False
        if len(history) >= 10:
            median = sorted(history)[len(history) // 2]
            if median > 0 and original_len > median * policy.anomaly_multiplier:
                anomalous = True

        # Min length check
        if not policy.allow_empty and len(value.strip()) < policy.min_chars:
            return LengthValidationResult(
                field_name=field_name,
                original_length_chars=original_len,
                validated_value=value,
                truncated=False,
                rejected=True,
                error=f"'{field_name}' is too short (min {policy.min_chars} chars)",
                estimated_tokens=token_est,
            )

        # Token limit check
        if policy.max_tokens and token_est > policy.max_tokens:
            return self._handle_oversize(
                policy, value, original_len, token_est, anomalous,
                reason=f"exceeds token limit ({token_est} > {policy.max_tokens})"
            )

        # Char limit check
        if original_len > policy.max_chars:
            return self._handle_oversize(
                policy, value, original_len, token_est, anomalous,
                reason=f"exceeds char limit ({original_len} > {policy.max_chars})"
            )

        return LengthValidationResult(
            field_name=field_name,
            original_length_chars=original_len,
            validated_value=value,
            truncated=False,
            rejected=False,
            error=None,
            estimated_tokens=token_est,
            anomalous=anomalous,
        )

    def _handle_oversize(
        self,
        policy: InputLengthPolicy,
        value: str,
        original_len: int,
        token_est: int,
        anomalous: bool,
        reason: str,
    ) -> LengthValidationResult:
        if policy.truncation == TruncationStrategy.REJECT:
            return LengthValidationResult(
                field_name=policy.field_name,
                original_length_chars=original_len,
                validated_value=value,
                truncated=False,
                rejected=True,
                error=f"'{policy.field_name}' {reason}",
                estimated_tokens=token_est,
                anomalous=anomalous,
            )

        limit = policy.max_chars
        if policy.truncation == TruncationStrategy.TRUNCATE_END:
            truncated_val = value[:limit]
        elif policy.truncation == TruncationStrategy.TRUNCATE_MIDDLE:
            half = limit // 2
            truncated_val = value[:half] + "\n…\n" + value[-(limit - half):]
        else:  # SUMMARIZE_HINT
            truncated_val = value[:limit] + "\n" + policy.truncation_note

        return LengthValidationResult(
            field_name=policy.field_name,
            original_length_chars=original_len,
            validated_value=truncated_val,
            truncated=True,
            rejected=False,
            error=None,
            estimated_tokens=TokenEstimator.estimate(truncated_val),
            anomalous=anomalous,
        )
```

## Solution 4: Request-Level Length Guard

```python
from typing import Any, Dict, List, Optional


class RequestLengthGuard:
    """
    Validates all string fields in a request dict against registered policies.
    Returns cleaned args and a list of validation results.
    Rejects the entire request if any field is rejected.
    """

    def __init__(self, validator: InputLengthValidator):
        self._validator = validator

    def guard(
        self, request_fields: Dict[str, Any]
    ) -> tuple:  # (cleaned_fields, results, blocked)
        cleaned: Dict[str, Any] = {}
        results: List[LengthValidationResult] = []
        blocked = False

        for field_name, value in request_fields.items():
            if not isinstance(value, str):
                cleaned[field_name] = value
                continue
            result = self._validator.validate(field_name, value)
            results.append(result)
            if result.rejected:
                blocked = True
            else:
                cleaned[field_name] = result.validated_value

        return cleaned, results, blocked
```

## Solution 5: Anomalous Input Detector

```python
import time
from collections import deque
from typing import Deque, List


class AnomalousInputDetector:
    """
    Tracks anomalous-length inputs across requests.
    Fires alerts when a user is repeatedly submitting oversized inputs,
    which may indicate automation or a prompt stuffing attempt.
    """

    def __init__(self, window_seconds: float = 300.0, spike_threshold: int = 5):
        self._window = window_seconds
        self._spike_threshold = spike_threshold
        self._events: Deque[dict] = deque()

    def record(self, user_id: str, field_name: str, original_len: int, anomalous: bool) -> None:
        if anomalous:
            self._events.append({
                "ts": time.time(),
                "user_id": user_id,
                "field_name": field_name,
                "length": original_len,
            })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        while self._events and self._events[0]["ts"] < cutoff:
            self._events.popleft()

    def check_user(self, user_id: str) -> dict:
        self._trim()
        user_events = [e for e in self._events if e["user_id"] == user_id]
        is_suspicious = len(user_events) >= self._spike_threshold
        return {
            "user_id": user_id,
            "anomalous_submissions": len(user_events),
            "suspicious": is_suspicious,
            "recommendation": (
                "Consider temporary rate limit or manual review." if is_suspicious else None
            ),
        }

    def top_offenders(self, n: int = 10) -> List[dict]:
        self._trim()
        counts: Dict[str, int] = {}
        for e in self._events:
            counts[e["user_id"]] = counts.get(e["user_id"], 0) + 1
        return [
            {"user_id": uid, "count": c}
            for uid, c in sorted(counts.items(), key=lambda x: -x[1])[:n]
        ]
```

## Solution 6: Length Validation Audit Logger

```python
import time
from typing import List


class LengthValidationAuditLogger:
    """
    Records validation events for security audit.
    Exposes summary metrics for monitoring dashboards.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._events: List[dict] = []

    def record(self, user_id: str, results: List[LengthValidationResult], blocked: bool) -> None:
        self._events.append({
            "ts": time.time(),
            "user_id": user_id,
            "blocked": blocked,
            "anomalous_fields": [r.field_name for r in results if r.anomalous],
            "truncated_fields": [r.field_name for r in results if r.truncated],
            "rejected_fields": [r.field_name for r in results if r.rejected],
            "total_input_tokens": sum(r.estimated_tokens for r in results),
        })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e["ts"] >= cutoff]

    def summary(self) -> dict:
        self._trim()
        total = len(self._events)
        blocked = sum(1 for e in self._events if e["blocked"])
        anomalous = sum(1 for e in self._events if e["anomalous_fields"])
        return {
            "total_requests": total,
            "blocked_requests": blocked,
            "block_rate": round(blocked / max(total, 1), 4),
            "anomalous_requests": anomalous,
            "anomaly_rate": round(anomalous / max(total, 1), 4),
        }
```

## Comparison

| Approach | Per-Field Limits | Token Estimation | Truncation | Anomaly Detection | Audit Log |
|---|---|---|---|---|---|
| InputLengthValidator | Yes | Yes (heuristic) | Yes (3 strategies) | Yes (median-based) | No |
| RequestLengthGuard | Via validator | Via validator | Via validator | Via validator | No |
| AnomalousInputDetector | No | No | No | Yes (per-user) | No |
| LengthValidationAuditLogger | No | No | No | No | Yes |

**Best for production**: Register separate `InputLengthPolicy` entries for each input field with appropriate limits — chat messages (8K chars), document uploads (100K chars with TRUNCATE_MIDDLE), tool arguments (4K chars). Use `TruncationStrategy.TRUNCATE_MIDDLE` for document content so the LLM sees the beginning and end (most relevant for documents) rather than just the beginning. Set `anomaly_multiplier=5.0` to flag inputs 5× larger than a user's typical submissions — this catches both accidental paste errors and intentional stuffing. Monitor `LengthValidationAuditLogger.summary()` block rate: sustained rates above 2% indicate either overly aggressive limits or an active attack.
