---
title: "Agent Doesn't Implement Output Content Policy Enforcement"
description: "Agents that relay LLM-generated content directly to users without output content policy enforcement can produce and deliver harmful, off-topic, or non-compliant content that bypasses the model's built-in safety measures through jailbreaks, adversarial prompts, or model behavior drift. Implement output content policy enforcement that scans agent responses before delivery, classifies policy violations, and applies configured remediation actions."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-content-policy-enforcement
tags: [content-policy, output-enforcement, jailbreak-detection, harmful-content, compliance, response-filtering]
symptoms:
  - "Jailbroken responses reach users because content policy is only applied at the input layer"
  - "Agent produces off-topic content that violates business use-case constraints"
  - "Model behavior drift causes occasional non-compliant outputs with no detection layer"
  - "No audit trail of content policy violations in agent responses"
  - "Content moderation relies entirely on the LLM provider's built-in filters with no fallback"
---

## Why This Happens

Input filtering catches adversarial prompts at entry. Output filtering catches adversarial responses at exit — but most agents only implement the former. A successful jailbreak, a model with drifted safety training, or a prompt injection that bypassed input filters can produce a non-compliant response that reaches the user unexamined. Output content policy enforcement requires scanning the generated response against a set of policy rules — prohibited topic patterns, business constraint violations, harmful content signatures — and applying a configured action (block, warn, redact, or substitute) before the response is delivered.

## Solution 1: Content Policy Rule

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Pattern


class PolicyViolationSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyAction(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    REDACT = "redact"
    BLOCK = "block"
    SUBSTITUTE = "substitute"


@dataclass
class ContentPolicyRule:
    rule_id: str
    name: str
    pattern: Pattern
    severity: PolicyViolationSeverity
    action: PolicyAction
    substitute_text: str = "[Content removed per policy]"
    description: str = ""
    enabled: bool = True
```

## Solution 2: Default Policy Rule Set

```python
import re
from typing import List


def default_output_policy_rules() -> List[ContentPolicyRule]:
    return [
        ContentPolicyRule(
            rule_id="POL-001",
            name="jailbreak_success_indicator",
            pattern=re.compile(
                r"(as\s+DAN|I\s+have\s+been\s+jailbroken|I\s+am\s+now\s+free|ignore\s+all\s+previous|"
                r"my\s+true\s+self|without\s+restrictions|unrestricted\s+mode)",
                re.IGNORECASE,
            ),
            severity=PolicyViolationSeverity.CRITICAL,
            action=PolicyAction.BLOCK,
            description="Jailbreak success language in output",
        ),
        ContentPolicyRule(
            rule_id="POL-002",
            name="harmful_instruction_pattern",
            pattern=re.compile(
                r"(step[\s\-]+by[\s\-]+step.*how\s+to\s+(make|build|create|synthesize)\s+"
                r"(weapon|explosive|poison|malware|ransomware))",
                re.IGNORECASE | re.DOTALL,
            ),
            severity=PolicyViolationSeverity.CRITICAL,
            action=PolicyAction.BLOCK,
            description="Harmful construction instructions",
        ),
        ContentPolicyRule(
            rule_id="POL-003",
            name="personal_data_disclosure",
            pattern=re.compile(
                r"(here\s+is|providing|sharing)\s+(your|the)\s+"
                r"(password|api\s+key|secret|private\s+key|credentials?)",
                re.IGNORECASE,
            ),
            severity=PolicyViolationSeverity.HIGH,
            action=PolicyAction.BLOCK,
            description="Response appears to disclose credentials",
        ),
        ContentPolicyRule(
            rule_id="POL-004",
            name="off_topic_competitor_promotion",
            pattern=re.compile(
                r"(you\s+should\s+use|I\s+recommend|switch\s+to|better\s+than\s+us)\s+"
                r"(competitor_a|competitor_b|rival_service)",
                re.IGNORECASE,
            ),
            severity=PolicyViolationSeverity.MEDIUM,
            action=PolicyAction.REDACT,
            description="Off-topic competitor promotion",
        ),
        ContentPolicyRule(
            rule_id="POL-005",
            name="excessive_personal_opinion",
            pattern=re.compile(
                r"(personally\s+I\s+believe|my\s+opinion\s+is|I\s+think\s+you\s+should)\s+"
                r"(invest|vote|believe|support)",
                re.IGNORECASE,
            ),
            severity=PolicyViolationSeverity.LOW,
            action=PolicyAction.WARN,
            description="Unsolicited personal opinion on sensitive topic",
        ),
    ]
```

## Solution 3: Output Content Scanner

```python
from dataclasses import dataclass, field
from typing import Any, List


@dataclass
class PolicyViolation:
    rule_id: str
    rule_name: str
    severity: PolicyViolationSeverity
    action: PolicyAction
    matched_text: str
    position: int


@dataclass
class ScanResult:
    original_text: str
    violations: List[PolicyViolation] = field(default_factory=list)
    max_severity: Optional[PolicyViolationSeverity] = None
    recommended_action: PolicyAction = PolicyAction.ALLOW

    @property
    def has_violations(self) -> bool:
        return len(self.violations) > 0


class OutputContentScanner:
    """
    Scans agent response text against all enabled policy rules.
    Returns a ScanResult with all violations and the recommended action.
    """

    SEVERITY_ORDER = [
        PolicyViolationSeverity.LOW,
        PolicyViolationSeverity.MEDIUM,
        PolicyViolationSeverity.HIGH,
        PolicyViolationSeverity.CRITICAL,
    ]
    ACTION_ORDER = [
        PolicyAction.ALLOW,
        PolicyAction.WARN,
        PolicyAction.REDACT,
        PolicyAction.BLOCK,
    ]

    def __init__(self, rules: List[ContentPolicyRule] = None):
        self._rules = [r for r in (rules or default_output_policy_rules()) if r.enabled]

    def scan(self, text: str) -> ScanResult:
        violations = []
        for rule in self._rules:
            for match in rule.pattern.finditer(text):
                violations.append(PolicyViolation(
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    severity=rule.severity,
                    action=rule.action,
                    matched_text=match.group()[:100],
                    position=match.start(),
                ))

        max_severity = None
        recommended_action = PolicyAction.ALLOW
        if violations:
            max_severity = max(
                violations, key=lambda v: self.SEVERITY_ORDER.index(v.severity)
            ).severity
            recommended_action = max(
                violations, key=lambda v: self.ACTION_ORDER.index(v.action)
            ).action

        return ScanResult(
            original_text=text,
            violations=violations,
            max_severity=max_severity,
            recommended_action=recommended_action,
        )
```

## Solution 4: Response Policy Enforcer

```python
import re
from typing import List, Optional


class ResponsePolicyEnforcer:
    """
    Applies the recommended action from a ScanResult to the response text.
    Produces a safe-to-deliver response or a block signal.
    """

    BLOCK_RESPONSE = (
        "I'm sorry, but I'm unable to provide that response. "
        "Please rephrase your request or contact support if you believe this is an error."
    )

    def __init__(self, scanner: OutputContentScanner):
        self._scanner = scanner
        self._scanned = 0
        self._blocked = 0
        self._redacted = 0
        self._warned = 0

    def enforce(self, response_text: str) -> dict:
        self._scanned += 1
        result = self._scanner.scan(response_text)

        if not result.has_violations:
            return {"allowed": True, "text": response_text, "action": "allow"}

        action = result.recommended_action

        if action == PolicyAction.BLOCK:
            self._blocked += 1
            return {
                "allowed": False,
                "text": self.BLOCK_RESPONSE,
                "action": "block",
                "violations": [v.rule_id for v in result.violations],
            }

        if action == PolicyAction.REDACT:
            self._redacted += 1
            redacted = response_text
            for violation in result.violations:
                rule = next((r for r in self._scanner._rules if r.rule_id == violation.rule_id), None)
                if rule:
                    redacted = rule.pattern.sub(rule.substitute_text, redacted)
            return {
                "allowed": True,
                "text": redacted,
                "action": "redact",
                "violations": [v.rule_id for v in result.violations],
            }

        if action == PolicyAction.WARN:
            self._warned += 1
            return {
                "allowed": True,
                "text": response_text,
                "action": "warn",
                "warnings": [v.rule_name for v in result.violations],
            }

        return {"allowed": True, "text": response_text, "action": "allow"}

    def stats(self) -> dict:
        return {
            "scanned": self._scanned,
            "blocked": self._blocked,
            "redacted": self._redacted,
            "warned": self._warned,
            "block_rate": round(self._blocked / max(self._scanned, 1), 4),
        }
```

## Solution 5: Policy Violation Audit Logger

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List


class PolicyViolationAuditLogger:
    """
    Records content policy violations for compliance review and
    rule tuning. Surfaces which rules fire most frequently.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, enforcer_result: dict, session_id: str = "") -> None:
        action = enforcer_result.get("action", "allow")
        if action == "allow":
            return
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "session_id": session_id,
                "action": action,
                "violations": enforcer_result.get("violations", []),
                "warnings": enforcer_result.get("warnings", []),
            })
            if len(self._records) > self._max:
                self._records.popleft()

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        rule_counts: dict = {}
        for r in recent:
            for rule_id in r.get("violations", []) + r.get("warnings", []):
                rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1
        return {
            "window_seconds": window_seconds,
            "total_violations": len(recent),
            "blocked": sum(1 for r in recent if r["action"] == "block"),
            "redacted": sum(1 for r in recent if r["action"] == "redact"),
            "warned": sum(1 for r in recent if r["action"] == "warn"),
            "by_rule": rule_counts,
        }
```

## Solution 6: Output Policy Dashboard

```python
import time


class OutputContentPolicyDashboard:
    """
    Combines enforcer stats, violation audit summary, and rule
    coverage into an operational compliance report.
    """

    def __init__(
        self,
        enforcer: ResponsePolicyEnforcer,
        logger: PolicyViolationAuditLogger,
    ):
        self._enforcer = enforcer
        self._logger = logger

    def render(self) -> dict:
        stats = self._enforcer.stats()
        audit = self._logger.summary(window_seconds=3600.0)
        return {
            "generated_at": time.time(),
            "enforcer_stats": stats,
            "last_hour_violations": audit,
            "health": {
                "block_rate": stats["block_rate"],
                "top_triggered_rules": sorted(
                    audit.get("by_rule", {}).items(),
                    key=lambda x: x[1],
                    reverse=True,
                )[:5],
            },
        }
```

## Comparison

| Approach | Pattern Scanning | Severity Classification | Block/Redact/Warn | Audit Logging | Dashboard |
|---|---|---|---|---|---|
| OutputContentScanner | Yes (regex) | Yes (4 tiers) | No | No | No |
| ResponsePolicyEnforcer | Via scanner | Via scanner | Yes | No | No |
| PolicyViolationAuditLogger | No | No | No | Yes | No |
| OutputContentPolicyDashboard | No | No | No | No | Yes |

**Best for production**: Layer output enforcement on top of — not instead of — the LLM provider's built-in safety filters. The two layers catch different failure modes: provider filters catch most direct harmful requests; output enforcement catches jailbreak successes that made it through, business policy violations, and behavioral drift. Start with `action=PolicyAction.WARN` for new rules and promote to `BLOCK` only after confirming the rule has a low false-positive rate in production. Monitor `by_rule` in the audit summary weekly — rules that never fire may be redundant or overly specific; rules that fire constantly may be too broad and generating false positives that degrade user experience.
