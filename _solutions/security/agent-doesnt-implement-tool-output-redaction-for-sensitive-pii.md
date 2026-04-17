---
title: "Agent Doesn't Implement Tool Output Redaction for Sensitive PII"
description: "Agents that inject raw tool outputs containing PII into the LLM context risk exposing personal data in logs, conversation history, and LLM training pipelines. Implement PII detection and redaction on tool outputs before context injection, replacing sensitive fields with typed placeholders that preserve the semantic structure while removing the actual values."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-output-redaction-for-sensitive-pii
tags: [pii-redaction, data-privacy, gdpr, tool-output-sanitization, sensitive-data, context-privacy]
symptoms:
  - "Social security numbers from database queries appear in conversation history"
  - "Credit card numbers retrieved by payment tools are injected into LLM context"
  - "Email addresses and phone numbers from CRM lookups persist in session logs"
  - "No distinction between PII fields and non-sensitive fields in tool outputs"
  - "LLM training data contains real user PII from injected tool results"
---

## Why This Happens

Tools that query databases, CRMs, or payment systems return records with PII fields alongside non-sensitive fields. When the full record is injected into the LLM context, PII travels with it — into the context window, into conversation logs, into any LLM training pipeline that learns from conversations. PII redaction must be applied before context injection, replacing sensitive values with typed placeholders (e.g., `[SSN_REDACTED]`, `[EMAIL_REDACTED]`) that allow the LLM to reason about the structure of the data without seeing the actual values.

## Solution 1: PII Pattern Registry

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Pattern


class PIICategory(str, Enum):
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    EMAIL = "email"
    PHONE = "phone"
    DATE_OF_BIRTH = "date_of_birth"
    PASSPORT = "passport"
    IP_ADDRESS = "ip_address"
    BANK_ACCOUNT = "bank_account"
    DRIVERS_LICENSE = "drivers_license"
    NAME = "name"               # requires NER, use cautiously


class RedactionStrategy(str, Enum):
    PLACEHOLDER = "placeholder"   # replace with [EMAIL_REDACTED]
    MASK = "mask"                 # show partial: j***@example.com
    HASH = "hash"                 # replace with hash for correlation
    OMIT = "omit"                 # remove field entirely


@dataclass
class PIIPattern:
    category: PIICategory
    name_patterns: List[str]      # regex patterns for field names
    value_patterns: List[str]     # regex patterns for field values
    strategy: RedactionStrategy = RedactionStrategy.PLACEHOLDER
    placeholder: str = ""
    confidence: float = 1.0       # 0.0-1.0, lower = more false positives

    def __post_init__(self):
        if not self.placeholder:
            self.placeholder = f"[{self.category.value.upper()}_REDACTED]"

    def name_compiled(self) -> List[re.Pattern]:
        return [re.compile(p, re.IGNORECASE) for p in self.name_patterns]

    def value_compiled(self) -> List[re.Pattern]:
        return [re.compile(p) for p in self.value_patterns]


def default_pii_patterns() -> List[PIIPattern]:
    return [
        PIIPattern(
            category=PIICategory.SSN,
            name_patterns=[r"\bssn\b", r"social.?security"],
            value_patterns=[r"\b\d{3}-\d{2}-\d{4}\b", r"\b\d{9}\b"],
            strategy=RedactionStrategy.PLACEHOLDER,
        ),
        PIIPattern(
            category=PIICategory.CREDIT_CARD,
            name_patterns=[r"card.?number", r"credit.?card", r"cc.?num"],
            value_patterns=[r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"],
            strategy=RedactionStrategy.MASK,
            placeholder="****-****-****-[LAST4]",
        ),
        PIIPattern(
            category=PIICategory.EMAIL,
            name_patterns=[r"\bemail\b", r"e.?mail.?address"],
            value_patterns=[r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b"],
            strategy=RedactionStrategy.PLACEHOLDER,
        ),
        PIIPattern(
            category=PIICategory.PHONE,
            name_patterns=[r"\bphone\b", r"mobile", r"tel\b", r"cell"],
            value_patterns=[r"\b(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b"],
            strategy=RedactionStrategy.PLACEHOLDER,
        ),
        PIIPattern(
            category=PIICategory.DATE_OF_BIRTH,
            name_patterns=[r"dob", r"date.?of.?birth", r"birthdate", r"birthday"],
            value_patterns=[
                r"\b\d{4}-\d{2}-\d{2}\b",
                r"\b\d{2}/\d{2}/\d{4}\b",
            ],
            strategy=RedactionStrategy.PLACEHOLDER,
        ),
        PIIPattern(
            category=PIICategory.IP_ADDRESS,
            name_patterns=[r"ip.?address", r"\bip\b"],
            value_patterns=[r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"],
            strategy=RedactionStrategy.MASK,
            placeholder="[IP_REDACTED]",
        ),
        PIIPattern(
            category=PIICategory.BANK_ACCOUNT,
            name_patterns=[r"account.?number", r"bank.?account", r"routing"],
            value_patterns=[r"\b\d{8,17}\b"],
            strategy=RedactionStrategy.PLACEHOLDER,
        ),
    ]
```

## Solution 2: PII Value Redactor

```python
import hashlib
import re
from typing import Any


class PIIValueRedactor:
    """
    Applies the configured redaction strategy to a detected PII value.
    """

    @staticmethod
    def redact(value: str, pattern: PIIPattern) -> str:
        if pattern.strategy == RedactionStrategy.PLACEHOLDER:
            return pattern.placeholder

        if pattern.strategy == RedactionStrategy.OMIT:
            return ""  # caller removes the field

        if pattern.strategy == RedactionStrategy.HASH:
            digest = hashlib.sha256(value.encode()).hexdigest()[:8]
            return f"[HASH:{digest}]"

        if pattern.strategy == RedactionStrategy.MASK:
            if pattern.category == PIICategory.CREDIT_CARD:
                digits = re.sub(r"[^\d]", "", value)
                last4 = digits[-4:] if len(digits) >= 4 else digits
                return f"****-****-****-{last4}"
            if pattern.category == PIICategory.EMAIL:
                parts = value.split("@")
                if len(parts) == 2:
                    local = parts[0]
                    masked = local[0] + "***" if len(local) > 1 else "***"
                    return f"{masked}@{parts[1]}"
            # Generic masking: show first char, mask rest
            return value[0] + "*" * (len(value) - 1) if value else pattern.placeholder

        return pattern.placeholder
```

## Solution 3: Tool Output PII Detector

```python
import re
from typing import Any, Dict, List, Tuple


class ToolOutputPIIDetector:
    """
    Scans tool output fields for PII by matching field names and values
    against registered PII patterns. Returns detected (field, pattern) pairs.
    """

    def __init__(self, patterns: List[PIIPattern]):
        self._patterns = patterns
        self._compiled_name: List[Tuple[PIIPattern, List[re.Pattern]]] = [
            (p, p.name_compiled()) for p in patterns
        ]
        self._compiled_value: List[Tuple[PIIPattern, List[re.Pattern]]] = [
            (p, p.value_compiled()) for p in patterns
        ]

    def _matches_name(self, field_name: str) -> List[PIIPattern]:
        matched = []
        for pattern, compiled in self._compiled_name:
            if any(rx.search(field_name) for rx in compiled):
                matched.append(pattern)
        return matched

    def _matches_value(self, value: str) -> List[PIIPattern]:
        matched = []
        for pattern, compiled in self._compiled_value:
            if any(rx.search(value) for rx in compiled):
                matched.append(pattern)
        return matched

    def detect_in_dict(self, obj: dict) -> Dict[str, List[PIIPattern]]:
        """Returns {field_name: [matching_patterns]} for all fields with detected PII."""
        detections: Dict[str, List[PIIPattern]] = {}
        for key, value in obj.items():
            found = []
            found.extend(self._matches_name(str(key)))
            if isinstance(value, str):
                found.extend(m for m in self._matches_value(value) if m not in found)
            if found:
                detections[key] = found
        return detections
```

## Solution 4: Tool Output PII Redactor

```python
import copy
from typing import Any, Dict, List


class ToolOutputPIIRedactor:
    """
    Applies PII redaction to tool output dicts and lists recursively.
    Returns a deep copy — original tool output is never mutated.
    Records a redaction report for audit purposes.
    """

    def __init__(
        self,
        detector: ToolOutputPIIDetector,
        value_redactor: PIIValueRedactor,
    ):
        self._detector = detector
        self._value_redactor = value_redactor

    def redact(self, tool_output: Any) -> Tuple[Any, List[dict]]:
        """Returns (redacted_output, redaction_report)."""
        output = copy.deepcopy(tool_output)
        report: List[dict] = []
        self._redact_recursive(output, report, path="")
        return output, report

    def _redact_recursive(self, obj: Any, report: List[dict], path: str) -> None:
        if isinstance(obj, dict):
            detections = self._detector.detect_in_dict(obj)
            for field, patterns in detections.items():
                pattern = patterns[0]  # use highest-priority pattern
                if isinstance(obj[field], str):
                    original_preview = obj[field][:20] + "..." if len(obj[field]) > 20 else obj[field]
                    if pattern.strategy == RedactionStrategy.OMIT:
                        del obj[field]
                    else:
                        obj[field] = self._value_redactor.redact(obj[field], pattern)
                    report.append({
                        "field_path": f"{path}.{field}" if path else field,
                        "category": pattern.category.value,
                        "strategy": pattern.strategy.value,
                        "original_preview": original_preview,
                    })
            # Recurse into remaining fields
            for key in list(obj.keys()):
                if isinstance(obj[key], (dict, list)):
                    child_path = f"{path}.{key}" if path else key
                    self._redact_recursive(obj[key], report, child_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                self._redact_recursive(item, report, f"{path}[{i}]")
```

## Solution 5: PII Redaction Tool Interceptor

```python
import time
from typing import Any, Callable, Dict, List, Optional


class PIIRedactionToolInterceptor:
    """
    Intercepts tool outputs and applies PII redaction before they are
    injected into the LLM context. Records redaction events for audit.
    """

    def __init__(
        self,
        redactor: ToolOutputPIIRedactor,
        audit_fn: Optional[Callable[[dict], None]] = None,
        redaction_enabled: bool = True,
    ):
        self._redactor = redactor
        self._audit_fn = audit_fn
        self._enabled = redaction_enabled
        self._stats = {"total_outputs": 0, "outputs_with_pii": 0, "fields_redacted": 0}

    def intercept(self, tool_name: str, raw_output: Any) -> Any:
        self._stats["total_outputs"] += 1
        if not self._enabled:
            return raw_output

        redacted, report = self._redactor.redact(raw_output)

        if report:
            self._stats["outputs_with_pii"] += 1
            self._stats["fields_redacted"] += len(report)
            if self._audit_fn:
                self._audit_fn({
                    "ts": time.time(),
                    "tool_name": tool_name,
                    "fields_redacted": len(report),
                    "categories": list({r["category"] for r in report}),
                    "report": report,
                })

        return redacted

    def stats(self) -> dict:
        return dict(self._stats)
```

## Solution 6: PII Redaction Coverage Auditor

```python
import re
from typing import Any, Dict, List


UNMATCHED_PII_HEURISTICS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "possible_ssn"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b"), "possible_email"),
    (re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"), "possible_credit_card"),
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "possible_ip"),
]


class PIIRedactionCoverageAuditor:
    """
    Scans redacted outputs for PII patterns that survived redaction —
    indicating gaps in the pattern registry. Run in staging to find
    PII categories not yet covered.
    """

    def audit(self, redacted_output: Any) -> List[dict]:
        gaps = []
        text = str(redacted_output)
        for pattern, label in UNMATCHED_PII_HEURISTICS:
            matches = pattern.findall(text)
            if matches:
                gaps.append({
                    "heuristic": label,
                    "samples": [m[:30] for m in matches[:3]],
                    "count": len(matches),
                    "recommendation": f"Add a PIIPattern covering '{label}' values",
                })
        return gaps
```

## Comparison

| Approach | Name-Based Detection | Value-Based Detection | Recursive Redaction | Audit Logging | Coverage Audit |
|---|---|---|---|---|---|
| PIIPattern Registry | Yes (regex names) | Yes (regex values) | No | No | No |
| ToolOutputPIIDetector | Via patterns | Via patterns | No | No | No |
| ToolOutputPIIRedactor | Via detector | Via detector | Yes | Via report | No |
| PIIRedactionToolInterceptor | Via redactor | Via redactor | Via redactor | Yes (callback) | No |
| PIIRedactionCoverageAuditor | No | No | No | No | Yes |

**Best for production**: Apply PII redaction as a mandatory interceptor in the tool dispatch pipeline — not opt-in per tool. Run `PIIRedactionCoverageAuditor.audit()` against a sample of tool outputs in staging before each deployment to catch new PII fields introduced by API schema changes. Use `RedactionStrategy.PLACEHOLDER` for SSNs and full account numbers (never show any part); use `RedactionStrategy.MASK` for credit cards (show last 4 for user confirmation flows) and emails (show domain for debugging). Log all redaction events to a separate audit trail with WORM (write-once) storage to satisfy GDPR Article 30 processing records requirements.
