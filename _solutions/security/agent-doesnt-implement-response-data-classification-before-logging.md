---
title: "Agent Doesn't Implement Response Data Classification Before Logging"
description: "Agents that log LLM responses and tool results verbatim write PII, secrets, financial data, and health information to log stores that are accessible to far more personnel than the original data warranted. Implement response data classification that detects sensitive data categories in outgoing content before it reaches the log sink, redacts or suppresses classified fields, and produces an audit trail of what was classified without exposing the underlying data."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-response-data-classification-before-logging
tags: [data-classification, pii-redaction, log-sanitization, sensitive-data, data-privacy, gdpr]
symptoms:
  - "LLM responses containing user SSNs appear in plaintext in centralized log stores"
  - "Tool results with API keys or passwords are logged verbatim"
  - "No distinction between public and sensitive fields in structured log events"
  - "GDPR right-to-erasure requests require scrubbing logs that should never have contained PII"
  - "Security audit finds credit card numbers in application logs"
---

## Why This Happens

Logging pipelines treat all text as equivalent — a response containing medical information and a response containing public news are both written to the same log destination with no filtering. The LLM may include sensitive user data in its response because the user provided it in the prompt; tool results may contain database rows with PII that the agent forwarded without inspection. Classification must happen at the egress point — between content generation and log sink — not as an afterthought.

## Solution 1: Data Classification Label

```python
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class DataSensitivityLevel(str, Enum):
    PUBLIC = "public"           # safe to log verbatim
    INTERNAL = "internal"       # log with context stripped
    CONFIDENTIAL = "confidential"  # log only metadata, not content
    RESTRICTED = "restricted"   # suppress entirely from logs


class PIICategory(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    DATE_OF_BIRTH = "dob"
    FULL_NAME = "full_name"
    ADDRESS = "address"
    IP_ADDRESS = "ip_address"
    API_KEY = "api_key"
    PASSWORD = "password"
    HEALTH = "health_data"
    FINANCIAL = "financial_data"


@dataclass
class ClassificationResult:
    sensitivity: DataSensitivityLevel
    detected_categories: List[PIICategory]
    confidence: float   # 0.0–1.0
    match_count: int
    sample_hint: Optional[str] = None   # non-sensitive prefix for debugging
```

## Solution 2: Pattern-Based Data Classifier

```python
import re
from typing import List, Tuple


CLASSIFICATION_PATTERNS: List[Tuple[PIICategory, str, DataSensitivityLevel]] = [
    (PIICategory.SSN,         r"\b\d{3}-\d{2}-\d{4}\b",                           DataSensitivityLevel.RESTRICTED),
    (PIICategory.CREDIT_CARD, r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13})\b", DataSensitivityLevel.RESTRICTED),
    (PIICategory.API_KEY,     r"(?:sk-|pk-|api[_-]?key[\s:=]+)[a-zA-Z0-9_\-]{20,}", DataSensitivityLevel.RESTRICTED),
    (PIICategory.PASSWORD,    r"(?:password|passwd|secret|token)[\s:=]+\S{6,}",    DataSensitivityLevel.RESTRICTED),
    (PIICategory.EMAIL,       r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b", DataSensitivityLevel.CONFIDENTIAL),
    (PIICategory.PHONE,       r"\b(?:\+1[\s\-]?)?\(?\d{3}\)?[\s\-]\d{3}[\s\-]\d{4}\b", DataSensitivityLevel.CONFIDENTIAL),
    (PIICategory.IP_ADDRESS,  r"\b(?:\d{1,3}\.){3}\d{1,3}\b",                     DataSensitivityLevel.INTERNAL),
    (PIICategory.DATE_OF_BIRTH, r"\b(?:dob|date.of.birth|born.on)[\s:]+\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}\b", DataSensitivityLevel.CONFIDENTIAL),
    (PIICategory.HEALTH,      r"\b(?:diagnosis|prescription|medical.record|patient.id|icd[\s\-]\d+)\b", DataSensitivityLevel.RESTRICTED),
    (PIICategory.FINANCIAL,   r"\b(?:account.number|routing.number|iban|swift.code|bank.account)\b", DataSensitivityLevel.RESTRICTED),
]

_COMPILED = [(cat, re.compile(pat, re.IGNORECASE), level) for cat, pat, level in CLASSIFICATION_PATTERNS]

_SEVERITY_RANK = {
    DataSensitivityLevel.PUBLIC: 0,
    DataSensitivityLevel.INTERNAL: 1,
    DataSensitivityLevel.CONFIDENTIAL: 2,
    DataSensitivityLevel.RESTRICTED: 3,
}


class PatternDataClassifier:
    """
    Scans text for known PII and sensitive data patterns.
    Returns the highest sensitivity level found across all matches.
    """

    def classify(self, text: str) -> ClassificationResult:
        if not text:
            return ClassificationResult(
                sensitivity=DataSensitivityLevel.PUBLIC,
                detected_categories=[],
                confidence=1.0,
                match_count=0,
            )

        categories: List[PIICategory] = []
        max_level = DataSensitivityLevel.PUBLIC
        total_matches = 0

        for category, pattern, level in _COMPILED:
            matches = pattern.findall(text)
            if matches:
                categories.append(category)
                total_matches += len(matches)
                if _SEVERITY_RANK[level] > _SEVERITY_RANK[max_level]:
                    max_level = level

        confidence = min(1.0, 0.7 + 0.1 * len(categories))
        hint = text[:40].replace("\n", " ") if max_level == DataSensitivityLevel.PUBLIC else None

        return ClassificationResult(
            sensitivity=max_level,
            detected_categories=categories,
            confidence=confidence,
            match_count=total_matches,
            sample_hint=hint,
        )
```

## Solution 3: Log-Safe Redactor

```python
import re
from typing import Optional


class LogSafeRedactor:
    """
    Replaces detected sensitive patterns with type-labeled placeholders.
    Preserves text structure so log messages remain readable.
    """

    REDACTION_LABELS = {
        PIICategory.SSN: "[REDACTED:SSN]",
        PIICategory.CREDIT_CARD: "[REDACTED:CC]",
        PIICategory.API_KEY: "[REDACTED:API_KEY]",
        PIICategory.PASSWORD: "[REDACTED:PASSWORD]",
        PIICategory.EMAIL: "[REDACTED:EMAIL]",
        PIICategory.PHONE: "[REDACTED:PHONE]",
        PIICategory.IP_ADDRESS: "[REDACTED:IP]",
        PIICategory.DATE_OF_BIRTH: "[REDACTED:DOB]",
        PIICategory.HEALTH: "[REDACTED:HEALTH]",
        PIICategory.FINANCIAL: "[REDACTED:FINANCIAL]",
    }

    def redact(self, text: str, categories: Optional[List[PIICategory]] = None) -> str:
        """
        Redacts all detected patterns or only specified categories.
        """
        result = text
        target_patterns = [
            (cat, pattern, level)
            for cat, pattern, level in _COMPILED
            if categories is None or cat in categories
        ]
        for category, pattern, _ in target_patterns:
            label = self.REDACTION_LABELS.get(category, "[REDACTED]")
            result = pattern.sub(label, result)
        return result

    def redact_classified(self, text: str, classification: ClassificationResult) -> str:
        if classification.sensitivity == DataSensitivityLevel.PUBLIC:
            return text
        if classification.sensitivity == DataSensitivityLevel.RESTRICTED:
            return f"[CONTENT SUPPRESSED: {', '.join(c.value for c in classification.detected_categories)}]"
        return self.redact(text, classification.detected_categories)
```

## Solution 4: Classification-Gated Log Emitter

```python
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class ClassifiedLogEvent:
    event_type: str
    sensitivity: str
    detected_categories: List[str]
    content_redacted: bool
    content_suppressed: bool
    safe_content: Optional[str]
    metadata: Dict[str, Any] = field(default_factory=dict)
    emitted_at: float = field(default_factory=time.time)


class ClassificationGatedLogEmitter:
    """
    Classifies content before logging.
    RESTRICTED content is suppressed entirely (logged as metadata only).
    CONFIDENTIAL content is fully redacted before logging.
    INTERNAL content has sensitive fields redacted.
    PUBLIC content is logged verbatim.
    """

    def __init__(
        self,
        classifier: PatternDataClassifier,
        redactor: LogSafeRedactor,
        log_sink: Optional[Callable[[ClassifiedLogEvent], None]] = None,
        log_restricted_metadata: bool = True,
    ) -> None:
        self._classifier = classifier
        self._redactor = redactor
        self._sink = log_sink
        self._log_restricted = log_restricted_metadata
        self._events: List[ClassifiedLogEvent] = []

    def emit(
        self,
        event_type: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ClassifiedLogEvent:
        classification = self._classifier.classify(content)
        suppressed = (
            classification.sensitivity == DataSensitivityLevel.RESTRICTED
            and not self._log_restricted
        )

        if suppressed:
            safe_content = None
        else:
            safe_content = self._redactor.redact_classified(content, classification)

        event = ClassifiedLogEvent(
            event_type=event_type,
            sensitivity=classification.sensitivity.value,
            detected_categories=[c.value for c in classification.detected_categories],
            content_redacted=bool(classification.detected_categories) and not suppressed,
            content_suppressed=suppressed,
            safe_content=safe_content,
            metadata=metadata or {},
        )
        self._events.append(event)
        if self._sink:
            try:
                self._sink(event)
            except Exception:
                pass
        return event

    def recent_events(self, limit: int = 100) -> List[ClassifiedLogEvent]:
        return self._events[-limit:]
```

## Solution 5: Classification Audit Reporter

```python
import time
from collections import defaultdict
from typing import List


class ClassificationAuditReporter:
    """
    Aggregates classification outcomes to identify which data categories
    appear most often and whether suppression rates are healthy.
    """

    def __init__(self, emitter: ClassificationGatedLogEmitter) -> None:
        self._emitter = emitter

    def report(self, last_n: int = 1000) -> dict:
        events = self._emitter.recent_events(last_n)
        total = len(events)
        by_sensitivity: dict = defaultdict(int)
        by_category: dict = defaultdict(int)
        suppressed = 0
        redacted = 0

        for e in events:
            by_sensitivity[e.sensitivity] += 1
            for cat in e.detected_categories:
                by_category[cat] += 1
            if e.content_suppressed:
                suppressed += 1
            elif e.content_redacted:
                redacted += 1

        return {
            "generated_at": time.time(),
            "total_events": total,
            "suppressed": suppressed,
            "redacted": redacted,
            "clean": total - suppressed - redacted,
            "suppression_rate": round(suppressed / max(total, 1), 4),
            "redaction_rate": round(redacted / max(total, 1), 4),
            "by_sensitivity": dict(by_sensitivity),
            "top_categories": dict(
                sorted(by_category.items(), key=lambda x: -x[1])[:5]
            ),
        }
```

## Solution 6: Data Classification Dashboard

```python
import time


class DataClassificationDashboard:
    """
    Combines classification stats, audit report, and policy
    compliance indicators into a privacy operations view.
    """

    def __init__(
        self,
        emitter: ClassificationGatedLogEmitter,
        reporter: ClassificationAuditReporter,
    ) -> None:
        self._emitter = emitter
        self._reporter = reporter

    def render(self) -> dict:
        audit = self._reporter.report()
        alerts = []

        if audit["suppression_rate"] > 0.10:
            alerts.append({
                "type": "high_suppression_rate",
                "rate": audit["suppression_rate"],
                "message": "More than 10% of log events contain RESTRICTED data — review data flows.",
            })
        if audit["redaction_rate"] > 0.30:
            alerts.append({
                "type": "high_redaction_rate",
                "rate": audit["redaction_rate"],
                "message": "High PII volume in logs — check whether sensitive data is necessary in responses.",
            })

        return {
            "generated_at": time.time(),
            "audit": audit,
            "active_alerts": alerts,
        }
```

## Comparison

| Approach | Pattern Detection | Redaction | Log Suppression | Audit Trail | Dashboard |
|---|---|---|---|---|---|
| PatternDataClassifier | Yes (10 categories) | No | No | No | No |
| LogSafeRedactor | No | Yes (labeled placeholders) | Partial (RESTRICTED) | No | No |
| ClassificationGatedLogEmitter | Via classifier | Via redactor | Yes | Partial | No |
| ClassificationAuditReporter | No | No | No | Yes | No |
| DataClassificationDashboard | No | No | No | Via reporter | Yes |

**Best for production**: Classify every LLM response and tool result at the egress point before any log sink receives it — never rely on upstream data sanitization. Treat `RESTRICTED` content as fully suppressed from application logs; emit only metadata (event type, timestamp, session ID, category labels) to a separate compliance log with stricter ACLs. Run `ClassificationAuditReporter` weekly and investigate any category appearing in more than 5% of events — legitimate agents rarely need to log SSNs or credit card numbers, and their presence usually indicates a prompt design problem rather than a logging problem.
