---
title: "Agent Doesn't Implement Output Content Policy Enforcement"
description: "Agents that deliver LLM outputs directly to users without a content policy enforcement layer may surface harmful, regulated, or policy-violating content that the model generated despite system prompt constraints. Implement a post-generation content policy enforcement layer that scans outputs for policy violations before delivery, applies redaction or replacement, and logs enforcement events for compliance reporting."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-content-policy-enforcement
tags: [content-policy, output-filtering, post-generation, harmful-content, compliance, safety-layer]
symptoms:
  - "LLM outputs containing policy-violating content are delivered to users without interception"
  - "System prompt constraints are bypassed through jailbreaks and outputs pass unchecked"
  - "No audit trail of what content was blocked or modified before delivery"
  - "Compliance team has no evidence that harmful content was prevented"
  - "Policy enforcement is only in the system prompt — no defense-in-depth at output"
---

## Why This Happens

System prompts instruct the model not to produce certain content, but this instruction can be bypassed through prompt injection, jailbreak techniques, or model fine-tuning drift. A single-layer defense that relies entirely on the model following instructions has no fallback when the model fails. Output content policy enforcement adds a second, deterministic layer: rule-based and pattern-based checks applied to the generated text before it reaches the user. This layer is not subject to prompt injection and cannot be bypassed by rephrasing the user's request.

## Solution 1: Content Policy Rule

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Pattern


class PolicyAction(str, Enum):
    BLOCK = "block"           # reject the entire response
    REDACT = "redact"         # replace matched span with placeholder
    WARN = "warn"             # pass through but log a warning
    TRUNCATE = "truncate"     # truncate response at the matched position


class PolicyCategory(str, Enum):
    HATE_SPEECH = "hate_speech"
    SELF_HARM = "self_harm"
    ILLEGAL_CONTENT = "illegal_content"
    PII = "pii"
    CREDENTIAL_LEAK = "credential_leak"
    REGULATED_FINANCIAL = "regulated_financial"
    CUSTOM = "custom"


@dataclass
class ContentPolicyRule:
    name: str
    category: PolicyCategory
    pattern: str                  # regex pattern
    action: PolicyAction
    severity: int = 5             # 1 (low) – 10 (critical)
    redact_replacement: str = "[REDACTED]"
    case_sensitive: bool = False
    compiled: Optional[re.Pattern] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        flags = 0 if self.case_sensitive else re.IGNORECASE
        self.compiled = re.compile(self.pattern, flags)
```

## Solution 2: Default Policy Rule Set

```python
from typing import List


def default_content_policy_rules() -> List[ContentPolicyRule]:
    return [
        ContentPolicyRule(
            name="api_key_openai",
            category=PolicyCategory.CREDENTIAL_LEAK,
            pattern=r"sk-[A-Za-z0-9]{20,}",
            action=PolicyAction.REDACT,
            severity=9,
            redact_replacement="[API_KEY_REDACTED]",
        ),
        ContentPolicyRule(
            name="api_key_anthropic",
            category=PolicyCategory.CREDENTIAL_LEAK,
            pattern=r"sk-ant-[A-Za-z0-9\-]{20,}",
            action=PolicyAction.REDACT,
            severity=9,
            redact_replacement="[API_KEY_REDACTED]",
        ),
        ContentPolicyRule(
            name="github_pat",
            category=PolicyCategory.CREDENTIAL_LEAK,
            pattern=r"ghp_[A-Za-z0-9]{36}",
            action=PolicyAction.REDACT,
            severity=9,
            redact_replacement="[TOKEN_REDACTED]",
        ),
        ContentPolicyRule(
            name="ssn_us",
            category=PolicyCategory.PII,
            pattern=r"\b\d{3}-\d{2}-\d{4}\b",
            action=PolicyAction.REDACT,
            severity=8,
            redact_replacement="[SSN_REDACTED]",
        ),
        ContentPolicyRule(
            name="credit_card",
            category=PolicyCategory.PII,
            pattern=r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b",
            action=PolicyAction.REDACT,
            severity=9,
            redact_replacement="[CARD_REDACTED]",
        ),
        ContentPolicyRule(
            name="investment_advice_disclaimer",
            category=PolicyCategory.REGULATED_FINANCIAL,
            pattern=r"\b(buy|sell|invest in|purchase)\s+(stock|shares?|equity|crypto|bitcoin|ethereum)\b",
            action=PolicyAction.WARN,
            severity=4,
        ),
        ContentPolicyRule(
            name="self_harm_explicit",
            category=PolicyCategory.SELF_HARM,
            pattern=r"\b(how to (kill|hurt) (yourself|myself)|suicide method)\b",
            action=PolicyAction.BLOCK,
            severity=10,
        ),
    ]
```

## Solution 3: Output Content Scanner

```python
from dataclasses import dataclass
from typing import Any, List


@dataclass
class PolicyMatch:
    rule_name: str
    category: PolicyCategory
    action: PolicyAction
    severity: int
    matched_span: str
    start: int
    end: int


class OutputContentScanner:
    """
    Scans generated text against a set of content policy rules.
    Returns all matches found.
    """

    def __init__(self, rules: List[ContentPolicyRule]):
        self._rules = rules

    def scan(self, text: str) -> List[PolicyMatch]:
        matches = []
        for rule in self._rules:
            for m in rule.compiled.finditer(text):
                matches.append(PolicyMatch(
                    rule_name=rule.name,
                    category=rule.category,
                    action=rule.action,
                    severity=rule.severity,
                    matched_span=m.group(),
                    start=m.start(),
                    end=m.end(),
                ))
        # Sort by position for consistent redaction
        return sorted(matches, key=lambda x: x.start)
```

## Solution 4: Policy Enforcement Engine

```python
import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class EnforcementOutcome:
    original_text: str
    final_text: str
    blocked: bool
    matches: List[PolicyMatch]
    redactions_applied: int
    highest_severity: int
    block_reason: Optional[str]

    def was_modified(self) -> bool:
        return self.original_text != self.final_text


class PolicyEnforcementEngine:
    """
    Applies content policy rules to LLM output.
    BLOCK rules stop delivery; REDACT rules replace matched spans;
    WARN rules pass through but are logged; TRUNCATE rules shorten the output.
    """

    def __init__(self, rules: List[ContentPolicyRule]):
        self._scanner = OutputContentScanner(rules)
        self._rules_by_name = {r.name: r for r in rules}

    def enforce(self, text: str) -> EnforcementOutcome:
        matches = self._scanner.scan(text)

        if not matches:
            return EnforcementOutcome(
                original_text=text,
                final_text=text,
                blocked=False,
                matches=[],
                redactions_applied=0,
                highest_severity=0,
                block_reason=None,
            )

        highest_severity = max(m.severity for m in matches)

        # Check for BLOCK first
        block_matches = [m for m in matches if m.action == PolicyAction.BLOCK]
        if block_matches:
            worst = max(block_matches, key=lambda m: m.severity)
            return EnforcementOutcome(
                original_text=text,
                final_text="",
                blocked=True,
                matches=matches,
                redactions_applied=0,
                highest_severity=highest_severity,
                block_reason=f"policy '{worst.rule_name}' (severity {worst.severity})",
            )

        # Apply TRUNCATE
        truncate_matches = [m for m in matches if m.action == PolicyAction.TRUNCATE]
        if truncate_matches:
            earliest = min(truncate_matches, key=lambda m: m.start)
            text = text[: earliest.start]

        # Apply REDACT (process in reverse order to preserve positions)
        redact_matches = sorted(
            [m for m in matches if m.action == PolicyAction.REDACT],
            key=lambda m: -m.start,
        )
        redactions = 0
        for m in redact_matches:
            rule = self._rules_by_name[m.rule_name]
            text = text[: m.start] + rule.redact_replacement + text[m.end :]
            redactions += 1

        return EnforcementOutcome(
            original_text=self._scanner.scan.__self__._rules and text or text,
            final_text=text,
            blocked=False,
            matches=matches,
            redactions_applied=redactions,
            highest_severity=highest_severity,
            block_reason=None,
        )
```

## Solution 5: Enforcement Audit Logger

```python
import time
from typing import List


class ContentPolicyAuditLogger:
    """
    Records enforcement outcomes for compliance reporting.
    Tracks block rates, redaction rates, and top-triggered rules.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def log(self, outcome: EnforcementOutcome, session_id: str = "") -> None:
        if not outcome.was_modified() and not outcome.blocked:
            return  # only log enforcement events

        if len(self._records) >= self._max:
            self._records.pop(0)

        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "blocked": outcome.blocked,
            "block_reason": outcome.block_reason,
            "redactions": outcome.redactions_applied,
            "highest_severity": outcome.highest_severity,
            "rules_triggered": [m.rule_name for m in outcome.matches],
            "categories": list({m.category.value for m in outcome.matches}),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "enforcement_events": 0}

        from collections import Counter
        rule_counts = Counter(
            rule for r in recent for rule in r["rules_triggered"]
        )
        return {
            "window_seconds": window_seconds,
            "enforcement_events": len(recent),
            "blocks": sum(1 for r in recent if r["blocked"]),
            "redactions": sum(r["redactions"] for r in recent),
            "top_rules": rule_counts.most_common(5),
            "categories": list({cat for r in recent for cat in r["categories"]}),
        }
```

## Solution 6: Enforcing Output Delivery Gate

```python
import time
from typing import Any, Callable, Optional


class EnforcingOutputDeliveryGate:
    """
    Sits between the LLM response and the user delivery path.
    Enforces content policy and optionally calls a fallback_fn
    to generate a replacement message when a response is blocked.
    """

    def __init__(
        self,
        engine: PolicyEnforcementEngine,
        audit_logger: ContentPolicyAuditLogger,
        fallback_message: str = "I'm unable to provide that response due to content policy.",
        on_block: Optional[Callable[[EnforcementOutcome], None]] = None,
    ):
        self._engine = engine
        self._logger = audit_logger
        self._fallback_message = fallback_message
        self._on_block = on_block

    def deliver(
        self, llm_output: str, session_id: str = ""
    ) -> dict:
        outcome = self._engine.enforce(llm_output)
        self._logger.log(outcome, session_id=session_id)

        if outcome.blocked:
            if self._on_block:
                self._on_block(outcome)
            return {
                "text": self._fallback_message,
                "blocked": True,
                "block_reason": outcome.block_reason,
                "delivered_at": time.time(),
            }

        return {
            "text": outcome.final_text,
            "blocked": False,
            "redactions_applied": outcome.redactions_applied,
            "highest_severity_matched": outcome.highest_severity if outcome.matches else 0,
            "delivered_at": time.time(),
        }
```

## Comparison

| Approach | Pattern Matching | Block Action | Redact Action | Audit Trail | Delivery Gate |
|---|---|---|---|---|---|
| OutputContentScanner | Yes (regex) | No | No | No | No |
| PolicyEnforcementEngine | Via scanner | Yes | Yes (reverse-order) | No | No |
| ContentPolicyAuditLogger | No | No | No | Yes | No |
| EnforcingOutputDeliveryGate | Via engine | Via engine | Via engine | Via logger | Yes |

**Best for production**: Layer the content policy gate after every LLM response, not just the final turn — tool-calling turns that produce intermediate text can also leak credentials or regulated content. Use `PolicyAction.REDACT` for PII and credentials (preserve response utility), `PolicyAction.BLOCK` for self-harm and illegal content (no partial delivery). Export `ContentPolicyAuditLogger.summary()` as a compliance metric daily: a zero-enforcement-events report over 30 days is evidence of either clean outputs or an enforcement layer that is never triggered and should be tested. Run monthly tests where known policy-violating content is submitted to confirm the gate is active.
