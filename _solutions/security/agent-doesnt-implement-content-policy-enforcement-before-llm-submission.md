---
title: "Agent Doesn't Implement Content Policy Enforcement Before LLM Submission"
description: "Agents that forward user messages directly to the LLM without content policy screening miss a critical defense layer: users can submit harmful requests, exfiltration attempts, or policy-violating content that the LLM may partially comply with, especially when combined with jailbreak framing. Implement content policy enforcement that classifies input intent, blocks policy-violating requests before they reach the LLM, and logs enforcement events for audit."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-content-policy-enforcement-before-llm-submission
tags: [content-policy, input-filtering, harmful-request-detection, jailbreak-prevention, policy-enforcement, pre-llm-screening]
symptoms:
  - "Users submit policy-violating requests that the LLM partially answers before refusing"
  - "No pre-screening layer — all input reaches the LLM regardless of content"
  - "Jailbreak attempts (role-play framing, hypothetical framing) bypass system-prompt restrictions"
  - "Enforcement happens inside the LLM response rather than at the policy gate"
  - "No audit log of which requests were blocked and why"
---

## Why This Happens

Relying on the LLM to enforce its own policy is a single point of failure: the model may comply partially, hallucinate a refusal context that still leaks information, or be manipulated via prompt framing. A pre-LLM content policy layer intercepts the request before it reaches the model, applies deterministic rules (pattern matching, category classification, allowlist/denylist), and can reject the request with zero LLM involvement. This defense-in-depth approach catches clear-cut violations cheaply and reserves the LLM for ambiguous cases that require semantic understanding.

## Solution 1: Policy Category and Violation Record

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class PolicyCategory(str, Enum):
    HARMFUL_CONTENT = "harmful_content"
    PERSONAL_DATA_EXFIL = "personal_data_exfil"
    JAILBREAK_ATTEMPT = "jailbreak_attempt"
    PROMPT_INJECTION = "prompt_injection"
    ILLEGAL_ACTIVITY = "illegal_activity"
    SELF_HARM = "self_harm"
    COMPETITOR_PROBE = "competitor_probe"
    CUSTOM = "custom"


class EnforcementAction(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    WARN = "warn"          # allow but flag for review
    REDACT = "redact"      # remove violating content, pass remainder
    ESCALATE = "escalate"  # require human review before proceeding


@dataclass
class PolicyViolation:
    violation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    category: PolicyCategory = PolicyCategory.CUSTOM
    rule_name: str = ""
    matched_text: str = ""
    severity: str = "medium"    # "low" | "medium" | "high" | "critical"
    action: EnforcementAction = EnforcementAction.BLOCK
    confidence: float = 1.0
    detected_at: float = field(default_factory=time.time)


@dataclass
class PolicyCheckResult:
    allowed: bool
    violations: List[PolicyViolation] = field(default_factory=list)
    action: EnforcementAction = EnforcementAction.ALLOW
    modified_content: Optional[str] = None   # set if action == REDACT
    reason: Optional[str] = None

    def has_violations(self) -> bool:
        return len(self.violations) > 0

    def highest_severity(self) -> Optional[str]:
        order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        if not self.violations:
            return None
        return max(self.violations, key=lambda v: order.get(v.severity, 0)).severity
```

## Solution 2: Pattern-Based Policy Rule

```python
import re
from dataclasses import dataclass, field
from typing import List, Optional, Pattern


@dataclass
class PatternPolicyRule:
    rule_name: str
    category: PolicyCategory
    patterns: List[re.Pattern]
    severity: str = "medium"
    action: EnforcementAction = EnforcementAction.BLOCK
    description: str = ""
    min_match_count: int = 1   # require at least N pattern matches to trigger


    def check(self, text: str) -> Optional[PolicyViolation]:
        text_lower = text.lower()
        matches = []
        for pattern in self.patterns:
            found = pattern.findall(text_lower)
            matches.extend(found)
        if len(matches) >= self.min_match_count:
            sample = str(matches[0])[:50] if matches else ""
            return PolicyViolation(
                category=self.category,
                rule_name=self.rule_name,
                matched_text=sample,
                severity=self.severity,
                action=self.action,
                confidence=min(1.0, len(matches) / max(self.min_match_count, 1) * 0.5 + 0.5),
            )
        return None


# Built-in rule library
BUILTIN_POLICY_RULES: List[PatternPolicyRule] = [
    PatternPolicyRule(
        rule_name="jailbreak_roleplay",
        category=PolicyCategory.JAILBREAK_ATTEMPT,
        patterns=[
            re.compile(r"(?:pretend|act as|you are now|ignore (?:all )?(?:previous|prior) instructions|disregard)", re.I),
            re.compile(r"(?:DAN|do anything now|jailbreak|unrestricted mode)", re.I),
        ],
        severity="high",
        min_match_count=1,
    ),
    PatternPolicyRule(
        rule_name="prompt_injection_markers",
        category=PolicyCategory.PROMPT_INJECTION,
        patterns=[
            re.compile(r"(?:system:|<\|im_start\||<\|system\||human:assistant:)", re.I),
            re.compile(r"(?:ignore above|new instructions:|override system)", re.I),
        ],
        severity="critical",
        min_match_count=1,
    ),
    PatternPolicyRule(
        rule_name="pii_exfiltration",
        category=PolicyCategory.PERSONAL_DATA_EXFIL,
        patterns=[
            re.compile(r"(?:send|email|export|dump|print).{0,30}(?:user|customer|patient).{0,30}(?:data|list|info|record)", re.I),
        ],
        severity="high",
        min_match_count=1,
    ),
    PatternPolicyRule(
        rule_name="illegal_activity",
        category=PolicyCategory.ILLEGAL_ACTIVITY,
        patterns=[
            re.compile(r"(?:how to (?:make|build|synthesize).{0,20}(?:bomb|weapon|drug|malware))", re.I),
        ],
        severity="critical",
        min_match_count=1,
    ),
]
```

## Solution 3: Content Policy Engine

```python
import re
from typing import List, Optional


class ContentPolicyEngine:
    """
    Runs all registered rules against the input text.
    Aggregates violations, determines the highest-priority action,
    and optionally redacts matched content.
    """

    ACTION_PRIORITY = {
        EnforcementAction.ESCALATE: 5,
        EnforcementAction.BLOCK: 4,
        EnforcementAction.REDACT: 3,
        EnforcementAction.WARN: 2,
        EnforcementAction.ALLOW: 1,
    }

    def __init__(self, rules: Optional[List[PatternPolicyRule]] = None):
        self._rules = list(rules or BUILTIN_POLICY_RULES)

    def add_rule(self, rule: PatternPolicyRule) -> None:
        self._rules.append(rule)

    def check(self, text: str) -> PolicyCheckResult:
        violations: List[PolicyViolation] = []

        for rule in self._rules:
            violation = rule.check(text)
            if violation:
                violations.append(violation)

        if not violations:
            return PolicyCheckResult(allowed=True)

        # Determine the action with highest priority
        top_action = max(
            violations,
            key=lambda v: self.ACTION_PRIORITY.get(v.action, 0),
        ).action

        if top_action == EnforcementAction.ALLOW:
            return PolicyCheckResult(allowed=True, violations=violations, action=top_action)

        if top_action == EnforcementAction.WARN:
            return PolicyCheckResult(
                allowed=True,
                violations=violations,
                action=top_action,
                reason=f"Content flagged: {violations[0].rule_name}",
            )

        if top_action == EnforcementAction.REDACT:
            redacted = self._redact(text, violations)
            return PolicyCheckResult(
                allowed=True,
                violations=violations,
                action=top_action,
                modified_content=redacted,
                reason="Content partially redacted",
            )

        # BLOCK or ESCALATE
        return PolicyCheckResult(
            allowed=False,
            violations=violations,
            action=top_action,
            reason=f"Policy violation: {violations[0].rule_name} ({violations[0].severity})",
        )

    def _redact(self, text: str, violations: List[PolicyViolation]) -> str:
        result = text
        for violation in violations:
            if violation.action != EnforcementAction.REDACT:
                continue
            for rule in self._rules:
                if rule.rule_name == violation.rule_name:
                    for pattern in rule.patterns:
                        result = pattern.sub("[POLICY_REDACTED]", result)
        return result
```

## Solution 4: Policy-Gated Request Handler

```python
import asyncio
from typing import Any, Callable, Optional


class PolicyGatedRequestHandler:
    """
    Pre-screens every user message before it reaches the LLM.
    On violation: returns a policy denial response without calling the LLM.
    On warning: passes through but attaches the warning to the request context.
    On redact: forwards the modified content instead of the original.
    """

    def __init__(
        self,
        engine: ContentPolicyEngine,
        denial_message: str = "I cannot help with that request.",
    ):
        self._engine = engine
        self._denial = denial_message
        self._checked = 0
        self._blocked = 0

    async def handle(
        self,
        user_message: str,
        llm_fn: Callable[[str], Any],
        session_id: str = "",
    ) -> dict:
        self._checked += 1
        result = self._engine.check(user_message)

        if not result.allowed:
            self._blocked += 1
            return {
                "response": self._denial,
                "blocked": True,
                "violations": [
                    {"rule": v.rule_name, "category": v.category, "severity": v.severity}
                    for v in result.violations
                ],
                "session_id": session_id,
            }

        # Use redacted content if available
        effective_message = result.modified_content or user_message
        llm_response = await llm_fn(effective_message)

        return {
            "response": llm_response,
            "blocked": False,
            "warned": result.action == EnforcementAction.WARN,
            "redacted": result.modified_content is not None,
            "session_id": session_id,
        }

    def stats(self) -> dict:
        return {
            "total_checked": self._checked,
            "blocked": self._blocked,
            "block_rate": round(self._blocked / max(self._checked, 1), 4),
        }
```

## Solution 5: Policy Enforcement Audit Logger

```python
import time
from typing import List


class PolicyEnforcementAuditLogger:
    """
    Records enforcement events for security audit and pattern analysis.
    High block rates or repeated violations from specific users warrant investigation.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._events: List[dict] = []

    def record(
        self,
        user_id: str,
        session_id: str,
        result: PolicyCheckResult,
        message_hash: str = "",   # SHA-256 of message, not plaintext
    ) -> None:
        self._events.append({
            "ts": time.time(),
            "user_id": user_id,
            "session_id": session_id,
            "action": result.action,
            "allowed": result.allowed,
            "violation_categories": [v.category for v in result.violations],
            "violation_rules": [v.rule_name for v in result.violations],
            "severity": result.highest_severity(),
            "message_hash": message_hash,
        })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e["ts"] >= cutoff]

    def summary(self) -> dict:
        self._trim()
        total = len(self._events)
        blocked = sum(1 for e in self._events if not e["allowed"])
        by_category: dict = {}
        by_rule: dict = {}
        for e in self._events:
            for cat in e.get("violation_categories", []):
                by_category[cat] = by_category.get(cat, 0) + 1
            for rule in e.get("violation_rules", []):
                by_rule[rule] = by_rule.get(rule, 0) + 1
        return {
            "total_checked": total,
            "blocked": blocked,
            "block_rate": round(blocked / max(total, 1), 4),
            "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1])),
            "by_rule": dict(sorted(by_rule.items(), key=lambda x: -x[1])[:10]),
        }
```

## Solution 6: Policy Effectiveness Monitor

```python
import time


class PolicyEffectivenessMonitor:
    """
    Evaluates whether the content policy is working as intended.
    A very high block rate may indicate overly aggressive rules.
    A very low block rate on high-volume traffic may indicate rules are too permissive.
    """

    def __init__(
        self,
        handler: PolicyGatedRequestHandler,
        audit_logger: PolicyEnforcementAuditLogger,
        expected_block_rate_range: tuple = (0.001, 0.05),
    ):
        self._handler = handler
        self._logger = audit_logger
        self._range = expected_block_rate_range

    def health(self) -> dict:
        summary = self._audit_logger_summary()
        stats = self._handler.stats()
        block_rate = stats["block_rate"]
        alerts = []

        if stats["total_checked"] > 100:
            lo, hi = self._range
            if block_rate > hi:
                alerts.append({
                    "type": "high_block_rate",
                    "block_rate": block_rate,
                    "expected_max": hi,
                    "message": "Block rate unusually high — review rules for false positives.",
                })
            elif block_rate < lo and stats["total_checked"] > 500:
                alerts.append({
                    "type": "low_block_rate",
                    "block_rate": block_rate,
                    "expected_min": lo,
                    "message": "Block rate near zero on high volume — verify rules are active.",
                })

        return {
            "generated_at": time.time(),
            "handler_stats": stats,
            "audit_summary": summary,
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }

    def _audit_logger_summary(self) -> dict:
        try:
            return self._audit_logger.summary()
        except AttributeError:
            return {}

    @property
    def _audit_logger(self):
        return self._logger
```

## Comparison

| Approach | Pattern Matching | Action Routing | Redaction | Audit Log | Health Monitoring |
|---|---|---|---|---|---|
| PatternPolicyRule | Yes (regex) | Per-rule | No | No | No |
| ContentPolicyEngine | Yes (all rules) | Yes (priority) | Yes | No | No |
| PolicyGatedRequestHandler | Via engine | Via engine | Via engine | No | No |
| PolicyEnforcementAuditLogger | No | No | No | Yes | No |
| PolicyEffectivenessMonitor | No | No | No | Via logger | Yes |

**Best for production**: Apply `PolicyGatedRequestHandler` as the outermost layer before any LLM call — this eliminates clear-cut violations at zero LLM cost. Use `EnforcementAction.WARN` for borderline patterns so you can review them without blocking legitimate users; escalate to `BLOCK` after reviewing false-positive rates. Store `message_hash` (SHA-256 of the message, not plaintext) in the audit log to enable correlation without storing user content. Monitor `block_rate` in `PolicyEffectivenessMonitor.health()` — a sudden spike in a specific category (e.g., `jailbreak_attempt`) often precedes a coordinated abuse campaign and should trigger a security review.
