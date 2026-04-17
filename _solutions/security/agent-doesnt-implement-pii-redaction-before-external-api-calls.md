---
title: "Agent Doesn't Implement PII Redaction Before External API Calls"
description: "Agents that forward user messages containing personally identifiable information to external APIs — web search engines, analytics services, third-party tools — transmit PII to services outside the trust boundary without user consent or data processing agreements. Implement PII redaction that detects and replaces PII in tool arguments before they are sent to external APIs, substituting placeholder tokens that can be restored in the response."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-pii-redaction-before-external-api-calls
tags: [pii-redaction, data-minimization, external-api-privacy, gdpr, personal-data, tool-argument-sanitization]
symptoms:
  - "User's email address appears in web search queries sent to external search APIs"
  - "Full name and date of birth forwarded to third-party enrichment services"
  - "Phone numbers in user messages transmitted to external tool APIs without redaction"
  - "No distinction between internal APIs (trusted) and external APIs (untrusted) in tool routing"
  - "GDPR audit finds personal data sent to processors without data processing agreements"
---

## Why This Happens

When users include personal information in their messages — "look up flights for John Smith traveling from London on March 15" — agents that directly forward message content to tool arguments transmit that PII to external services. Web search APIs, weather services, flight lookup tools, and analytics endpoints all log request parameters. Without redaction, the agent becomes an inadvertent PII exfiltration channel. Redaction requires detecting PII patterns in tool arguments, replacing them with placeholder tokens before the API call, and optionally restoring original values in the response if needed for user-facing output.

## Solution 1: PII Pattern Registry

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Pattern


class PIICategory(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    NAME = "name"
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    IP_ADDRESS = "ip_address"
    DATE_OF_BIRTH = "date_of_birth"
    PASSPORT = "passport"
    ADDRESS = "address"
    NATIONAL_ID = "national_id"


@dataclass
class PIIPattern:
    category: PIICategory
    pattern: str          # regex
    placeholder_prefix: str  # e.g. "[EMAIL_" -> "[EMAIL_1]"
    severity: float = 0.8   # 0.0–1.0

    def compiled(self) -> re.Pattern:
        return re.compile(self.pattern, re.IGNORECASE)


DEFAULT_PII_PATTERNS: List[PIIPattern] = [
    PIIPattern(
        category=PIICategory.EMAIL,
        pattern=r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
        placeholder_prefix="[EMAIL_",
        severity=0.95,
    ),
    PIIPattern(
        category=PIICategory.PHONE,
        pattern=r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
        placeholder_prefix="[PHONE_",
        severity=0.90,
    ),
    PIIPattern(
        category=PIICategory.SSN,
        pattern=r"\b\d{3}-\d{2}-\d{4}\b",
        placeholder_prefix="[SSN_",
        severity=1.0,
    ),
    PIIPattern(
        category=PIICategory.CREDIT_CARD,
        pattern=r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b",
        placeholder_prefix="[CC_",
        severity=1.0,
    ),
    PIIPattern(
        category=PIICategory.IP_ADDRESS,
        pattern=r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
        placeholder_prefix="[IP_",
        severity=0.70,
    ),
    PIIPattern(
        category=PIICategory.DATE_OF_BIRTH,
        pattern=r"\b(?:born|dob|date of birth)[:\s]+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        placeholder_prefix="[DOB_",
        severity=0.85,
    ),
    PIIPattern(
        category=PIICategory.PASSPORT,
        pattern=r"\b[A-Z]{1,2}\d{6,9}\b",
        placeholder_prefix="[PASSPORT_",
        severity=0.80,
    ),
]
```

## Solution 2: PII Redactor

```python
import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class RedactionResult:
    original: str
    redacted: str
    replacements: Dict[str, str]   # placeholder -> original value
    detections: List[dict]         # [{category, placeholder, original_prefix}]
    pii_found: bool


class PIIRedactor:
    """
    Detects and replaces PII in text using pattern matching.
    Maintains a replacement map so originals can be restored if needed.
    """

    def __init__(self, patterns: List[PIIPattern]):
        self._patterns = patterns
        self._compiled = [(p, p.compiled()) for p in patterns]

    def redact(self, text: str) -> RedactionResult:
        replacements: Dict[str, str] = {}
        detections: List[dict] = []
        result = text
        counter: Dict[str, int] = {}

        for pattern, compiled in self._compiled:
            cat = pattern.category.value
            matches = list(compiled.finditer(result))
            for match in reversed(matches):   # reverse to preserve offsets
                original_value = match.group(0)
                counter[cat] = counter.get(cat, 0) + 1
                placeholder = f"{pattern.placeholder_prefix}{counter[cat]}]"
                replacements[placeholder] = original_value
                detections.append({
                    "category": cat,
                    "placeholder": placeholder,
                    "original_prefix": original_value[:4] + "***",
                    "severity": pattern.severity,
                })
                result = result[:match.start()] + placeholder + result[match.end():]

        return RedactionResult(
            original=text,
            redacted=result,
            replacements=replacements,
            detections=detections,
            pii_found=len(detections) > 0,
        )

    def restore(self, redacted_text: str, replacements: Dict[str, str]) -> str:
        """Restore original values in a response that contains placeholders."""
        result = redacted_text
        for placeholder, original in replacements.items():
            result = result.replace(placeholder, original)
        return result
```

## Solution 3: External API Trust Classifier

```python
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Set


class APITrustLevel(str, Enum):
    INTERNAL = "internal"       # same org, no redaction needed
    TRUSTED_PARTNER = "trusted_partner"  # DPA in place, minimal redaction
    EXTERNAL = "external"       # full PII redaction required
    UNTRUSTED = "untrusted"     # block PII entirely, consider blocking call


@dataclass
class APITrustPolicy:
    base_url_prefix: str
    trust_level: APITrustLevel
    allowed_pii_categories: Set[str] = None   # None = no PII allowed
    description: str = ""

    def __post_init__(self):
        if self.allowed_pii_categories is None:
            self.allowed_pii_categories = set()


class ExternalAPITrustClassifier:
    """
    Classifies outbound API calls by trust level based on URL prefix.
    Determines whether and how aggressively to redact PII in arguments.
    """

    def __init__(self, policies: List[APITrustPolicy]):
        self._policies = sorted(policies, key=lambda p: -len(p.base_url_prefix))

    def classify(self, url: str) -> APITrustPolicy:
        for policy in self._policies:
            if url.startswith(policy.base_url_prefix):
                return policy
        # Default: external with no PII allowed
        return APITrustPolicy(
            base_url_prefix="",
            trust_level=APITrustLevel.EXTERNAL,
        )
```

## Solution 4: PII-Safe Tool Argument Sanitizer

```python
import copy
from typing import Any, Dict, List, Optional, Tuple


class PIISafeToolArgumentSanitizer:
    """
    Sanitizes tool arguments before external API calls.
    Recursively traverses argument dicts, redacting PII in string values.
    Returns sanitized args and a restoration map for response post-processing.
    """

    def __init__(
        self,
        redactor: PIIRedactor,
        trust_classifier: ExternalAPITrustClassifier,
    ):
        self._redactor = redactor
        self._classifier = trust_classifier

    def sanitize(
        self,
        tool_name: str,
        args: Dict[str, Any],
        endpoint_url: str = "",
    ) -> Tuple[Dict[str, Any], Dict[str, str], List[dict]]:
        """
        Returns (sanitized_args, restoration_map, all_detections).
        """
        policy = self._classifier.classify(endpoint_url)
        if policy.trust_level == APITrustLevel.INTERNAL:
            return args, {}, []

        sanitized = copy.deepcopy(args)
        all_replacements: Dict[str, str] = {}
        all_detections: List[dict] = []

        self._sanitize_recursive(sanitized, all_replacements, all_detections, policy)
        return sanitized, all_replacements, all_detections

    def _sanitize_recursive(
        self,
        obj: Any,
        replacements: Dict[str, str],
        detections: List[dict],
        policy: APITrustPolicy,
    ) -> None:
        if isinstance(obj, dict):
            for key in list(obj.keys()):
                val = obj[key]
                if isinstance(val, str):
                    result = self._redactor.redact(val)
                    # Filter by trust policy
                    if result.pii_found:
                        filtered_detections = [
                            d for d in result.detections
                            if d["category"] not in policy.allowed_pii_categories
                        ]
                        if filtered_detections:
                            obj[key] = result.redacted
                            replacements.update(result.replacements)
                            detections.extend(filtered_detections)
                elif isinstance(val, (dict, list)):
                    self._sanitize_recursive(val, replacements, detections, policy)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                if isinstance(item, str):
                    result = self._redactor.redact(item)
                    if result.pii_found:
                        obj[i] = result.redacted
                        replacements.update(result.replacements)
                        detections.extend(result.detections)
                elif isinstance(item, (dict, list)):
                    self._sanitize_recursive(item, replacements, detections, policy)
```

## Solution 5: PII Redaction Audit Logger

```python
import time
from typing import List


class PIIRedactionAuditLogger:
    """
    Records PII detection events for compliance reporting.
    """

    def __init__(self, max_records: int = 100000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        tool_name: str,
        endpoint_url: str,
        detections: List[dict],
        session_id: str = "",
    ) -> None:
        if not detections:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        categories = list({d["category"] for d in detections})
        self._records.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "endpoint_url": endpoint_url[:100],
            "pii_categories": categories,
            "detection_count": len(detections),
            "session_id": session_id,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "redactions": 0}
        by_category: dict = {}
        for r in recent:
            for cat in r["pii_categories"]:
                by_category[cat] = by_category.get(cat, 0) + 1
        return {
            "window_seconds": window_seconds,
            "redactions": len(recent),
            "by_category": by_category,
            "by_tool": {
                tool: sum(1 for r in recent if r["tool_name"] == tool)
                for tool in {r["tool_name"] for r in recent}
            },
        }
```

## Solution 6: PII Redaction Dashboard

```python
import time


class PIIRedactionDashboard:
    """
    Combines redaction audit summary with trust policy inventory.
    """

    def __init__(
        self,
        audit_logger: PIIRedactionAuditLogger,
        trust_classifier: ExternalAPITrustClassifier,
    ):
        self._audit = audit_logger
        self._classifier = trust_classifier

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "trust_policies": [
                {
                    "prefix": p.base_url_prefix or "(default)",
                    "trust_level": p.trust_level.value,
                    "allowed_pii": list(p.allowed_pii_categories),
                }
                for p in self._classifier._policies
            ],
            "last_hour_redactions": self._audit.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Pattern Detection | Recursive Sanitization | Trust Classification | Restoration Map | Audit Log |
|---|---|---|---|---|---|
| PIIRedactor | Yes (8 categories) | No | No | Yes | No |
| ExternalAPITrustClassifier | No | No | Yes (4 levels) | No | No |
| PIISafeToolArgumentSanitizer | Via redactor | Yes | Via classifier | Yes | No |
| PIIRedactionAuditLogger | No | No | No | No | Yes |
| PIIRedactionDashboard | No | No | Via classifier | No | Via logger |

**Best for production**: Classify all external tool endpoints explicitly in `ExternalAPITrustClassifier` — defaulting to EXTERNAL trust for unknown URLs is correct and conservative. Use the restoration map to post-process responses: if the external API echoes back the query (e.g. "Results for [EMAIL_1]"), restore the original for the user-facing response while keeping logs redacted. Never log the `original` field from `RedactionResult` — the audit log should contain only categories and prefixes, not the actual PII values. Run `PIIRedactionAuditLogger.summary()` in GDPR compliance reports to demonstrate that PII is detected and redacted before external transmission.
