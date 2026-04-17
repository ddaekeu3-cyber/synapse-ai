---
title: "Agent Doesn't Implement Output Length Limit Enforcement"
description: "Agents that forward raw LLM output to users or downstream systems without length limits allow adversarially triggered verbose generation to exhaust client buffers, breach API response size limits, and consume excessive output tokens. Implement output length limit enforcement that truncates, summarizes, or rejects responses exceeding configured size thresholds before delivery."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-length-limit-enforcement
tags: [output-length, response-size, token-limit, verbose-generation, buffer-overflow, output-enforcement]
symptoms:
  - "LLM produces a 50,000-token response when asked to repeat a phrase — no ceiling enforced"
  - "Downstream API returns 413 Payload Too Large because the agent's response was not bounded"
  - "Client-side buffer overflows or JavaScript heap exhaustion from extremely large responses"
  - "No distinction between soft limit (warn and summarize) and hard limit (truncate and reject)"
  - "Output token costs spike due to adversarially long generations with no budget guard"
---

## Why This Happens

LLMs will generate as many tokens as the `max_tokens` parameter allows, and some adversarial prompts deliberately trigger maximum-length outputs. Even without adversarial intent, LLMs sometimes produce runaway repetitions or excessively verbose explanations. Agents that pipe raw LLM output directly to the response layer have no opportunity to intercept oversized responses. Output length enforcement requires measuring response size at the character and token level, applying configurable soft and hard limits, and choosing between truncation, summarization, and rejection based on the violation severity and the response type.

## Solution 1: Output Length Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class LimitAction(str, Enum):
    PASS = "pass"             # within limits — return as-is
    WARN = "warn"             # over soft limit — return with warning annotation
    TRUNCATE = "truncate"     # over hard limit — truncate and append notice
    SUMMARIZE = "summarize"   # over soft limit — request summarization
    REJECT = "reject"         # over reject limit — do not deliver


@dataclass
class OutputLengthPolicy:
    policy_id: str
    soft_limit_chars: int = 8000       # warn/summarize threshold
    hard_limit_chars: int = 20000      # truncate threshold
    reject_limit_chars: int = 100000   # reject threshold (possible attack)
    soft_action: LimitAction = LimitAction.WARN
    hard_action: LimitAction = LimitAction.TRUNCATE
    truncation_notice: str = "\n\n[Response truncated — output exceeded the configured length limit.]"
    measure_tokens: bool = True
    chars_per_token: float = 4.0

    def token_estimate(self, char_count: int) -> int:
        return int(char_count / self.chars_per_token)
```

## Solution 2: Output Length Measurement

```python
from dataclasses import dataclass


@dataclass
class OutputMeasurement:
    char_count: int
    token_estimate: int
    line_count: int
    word_count: int
    has_code_blocks: bool
    has_tables: bool

    def exceeds(self, limit_chars: int) -> bool:
        return self.char_count > limit_chars


class OutputLengthMeasurer:
    """
    Measures various size dimensions of a response for enforcement decisions.
    """

    import re
    CODE_BLOCK = re.compile(r"```[\s\S]*?```", re.DOTALL)
    TABLE_ROW = re.compile(r"^\|.+\|$", re.MULTILINE)

    @classmethod
    def measure(cls, text: str, chars_per_token: float = 4.0) -> OutputMeasurement:
        char_count = len(text)
        return OutputMeasurement(
            char_count=char_count,
            token_estimate=int(char_count / chars_per_token),
            line_count=text.count("\n") + 1,
            word_count=len(text.split()),
            has_code_blocks=bool(cls.CODE_BLOCK.search(text)),
            has_tables=bool(cls.TABLE_ROW.search(text)),
        )
```

## Solution 3: Output Truncator

```python
import re
from typing import Tuple


class OutputTruncator:
    """
    Truncates text to a character limit while preserving sentence and
    paragraph boundaries where possible. Appends a truncation notice.
    """

    SENTENCE_END = re.compile(r"[.!?]\s+")
    PARAGRAPH_END = re.compile(r"\n\n+")

    @classmethod
    def truncate(
        cls,
        text: str,
        max_chars: int,
        notice: str = "\n\n[Response truncated.]",
    ) -> Tuple[str, int]:
        """
        Returns (truncated_text, original_char_count).
        Tries to break at paragraph > sentence > word boundary.
        """
        original_len = len(text)
        if original_len <= max_chars:
            return text, original_len

        available = max_chars - len(notice)
        if available <= 0:
            return notice.strip(), original_len

        candidate = text[:available]

        # Try paragraph break
        para_match = list(cls.PARAGRAPH_END.finditer(candidate))
        if para_match:
            cut = para_match[-1].end()
            return text[:cut].rstrip() + notice, original_len

        # Try sentence break
        sent_match = list(cls.SENTENCE_END.finditer(candidate))
        if sent_match:
            cut = sent_match[-1].end()
            return text[:cut].rstrip() + notice, original_len

        # Hard cut at word boundary
        last_space = candidate.rfind(" ")
        if last_space > available // 2:
            return text[:last_space] + notice, original_len

        return candidate + notice, original_len
```

## Solution 4: Output Length Enforcer

```python
import time
from typing import Callable, Optional


class OutputLengthDecision:
    def __init__(
        self,
        action: LimitAction,
        output: str,
        original_chars: int,
        final_chars: int,
        policy_id: str,
    ):
        self.action = action
        self.output = output
        self.original_chars = original_chars
        self.final_chars = final_chars
        self.policy_id = policy_id
        self.truncated = final_chars < original_chars
        self.rejected = action == LimitAction.REJECT


class OutputLengthEnforcer:
    """
    Applies an OutputLengthPolicy to LLM response text.
    Optionally invokes a summarization function for SUMMARIZE action.
    """

    def __init__(
        self,
        policy: OutputLengthPolicy,
        measurer: OutputLengthMeasurer,
        truncator: OutputTruncator,
        summarize_fn: Optional[Callable] = None,
    ):
        self._policy = policy
        self._measurer = measurer
        self._truncator = truncator
        self._summarize_fn = summarize_fn
        self._enforcement_log: list = []

    async def enforce(self, text: str) -> OutputLengthDecision:
        measurement = self._measurer.measure(text, self._policy.chars_per_token)
        chars = measurement.char_count

        if chars > self._policy.reject_limit_chars:
            action = LimitAction.REJECT
            final_output = "[Response rejected: output length exceeded security threshold.]"
        elif chars > self._policy.hard_limit_chars:
            action = self._policy.hard_action
            if action == LimitAction.TRUNCATE:
                final_output, _ = self._truncator.truncate(
                    text, self._policy.hard_limit_chars, self._policy.truncation_notice
                )
            else:
                final_output = text
        elif chars > self._policy.soft_limit_chars:
            action = self._policy.soft_action
            if action == LimitAction.SUMMARIZE and self._summarize_fn:
                try:
                    final_output = await self._summarize_fn(text)
                except Exception:
                    final_output, _ = self._truncator.truncate(
                        text, self._policy.soft_limit_chars, self._policy.truncation_notice
                    )
            elif action == LimitAction.WARN:
                final_output = text + f"\n\n⚠ Response is long ({chars:,} chars)."
            else:
                final_output = text
        else:
            action = LimitAction.PASS
            final_output = text

        decision = OutputLengthDecision(
            action=action,
            output=final_output,
            original_chars=chars,
            final_chars=len(final_output),
            policy_id=self._policy.policy_id,
        )
        self._enforcement_log.append({
            "ts": time.time(),
            "action": action.value,
            "original_chars": chars,
            "policy_id": self._policy.policy_id,
        })
        if len(self._enforcement_log) > 50000:
            self._enforcement_log = self._enforcement_log[-25000:]
        return decision
```

## Solution 5: Output Length Audit Logger

```python
import time
from typing import List


class OutputLengthAuditLogger:
    """
    Aggregates enforcement decisions for security monitoring and policy tuning.
    """

    def __init__(self, enforcer: OutputLengthEnforcer):
        self._enforcer = enforcer

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._enforcer._enforcement_log if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "enforced": 0}

        by_action: dict = {}
        for r in recent:
            by_action[r["action"]] = by_action.get(r["action"], 0) + 1

        large = [r for r in recent if r["original_chars"] > self._enforcer._policy.soft_limit_chars]
        return {
            "window_seconds": window_seconds,
            "total_responses": len(recent),
            "by_action": by_action,
            "over_soft_limit": len(large),
            "rejection_rate": round(by_action.get("reject", 0) / max(len(recent), 1), 5),
            "mean_chars": round(sum(r["original_chars"] for r in recent) / len(recent), 0),
        }
```

## Solution 6: Output Length Enforcement Dashboard

```python
import time


class OutputLengthEnforcementDashboard:
    """
    Combines enforcement stats and policy configuration into a single view.
    """

    def __init__(
        self,
        enforcer: OutputLengthEnforcer,
        audit_logger: OutputLengthAuditLogger,
    ):
        self._enforcer = enforcer
        self._audit = audit_logger

    def render(self) -> dict:
        p = self._enforcer._policy
        return {
            "generated_at": time.time(),
            "policy": {
                "policy_id": p.policy_id,
                "soft_limit_chars": p.soft_limit_chars,
                "hard_limit_chars": p.hard_limit_chars,
                "reject_limit_chars": p.reject_limit_chars,
                "soft_action": p.soft_action.value,
                "hard_action": p.hard_action.value,
            },
            "last_hour": self._audit.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Measurement | Soft Limit | Hard Limit | Reject Limit | Audit Log |
|---|---|---|---|---|---|
| OutputLengthMeasurer | Yes (4 dimensions) | No | No | No | No |
| OutputTruncator | No | No | Yes (boundary-aware) | No | No |
| OutputLengthEnforcer | Via measurer | Yes | Yes | Yes | Yes |
| OutputLengthAuditLogger | No | No | No | No | Yes |
| OutputLengthEnforcementDashboard | No | No | No | No | Via logger |

**Best for production**: Set `reject_limit_chars=100000` as the absolute ceiling — no legitimate agent response needs to be 25,000+ words. Use `TRUNCATE` for the hard limit rather than `REJECT` — users are better served by a partial answer than an error. Monitor `rejection_rate` — above 0.1% means either the limit is too low or the system prompt is triggering verbose generation patterns that should be fixed upstream. Apply different policies per feature: a code generation tool may legitimately produce 10,000-character outputs, while a QA bot should rarely exceed 2,000 characters.
