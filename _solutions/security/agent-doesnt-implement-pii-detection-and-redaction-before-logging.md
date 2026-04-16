---
title: "Agent Doesn't Implement PII Detection and Redaction Before Logging"
description: "Agents that log raw user inputs, LLM prompts, and tool arguments write personally identifiable information — email addresses, phone numbers, credit card numbers, SSNs — into log storage that has broader access than production systems. Implement PII detection and redaction that scans log payloads before they are written, replaces identified PII with type-labeled tokens, and maintains a reversible mapping for authorized incident investigation."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-pii-detection-and-redaction-before-logging
tags: [pii-detection, pii-redaction, log-hygiene, data-privacy, gdpr, sensitive-data]
symptoms:
  - "Log aggregation platform contains user email addresses from agent session transcripts"
  - "Tool call arguments logged verbatim include credit card numbers passed by users"
  - "Compliance audit finds SSNs and dates of birth in application logs"
  - "Log access is broader than production database access but logs contain the same PII"
  - "No mechanism to purge a specific user's PII from logs without deleting entire log files"
---

## Why This Happens

Logging frameworks record whatever is passed to them. Agents log inputs and outputs for debugging, but those strings contain whatever the user typed — which may include PII they embedded in their query. Without a redaction layer between the application and the log sink, PII flows directly into logs. Redaction must happen synchronously before the log write, not asynchronously, to prevent any window where raw PII enters storage.

## Solution 1: PII Pattern Library

```python
import re
from dataclasses import dataclass
from typing import List, Pattern


@dataclass
class PIIPattern:
    pii_type: str
    pattern: re.Pattern
    token_label: str   # replacement label, e.g. "[EMAIL]"
    confidence: str    # "high" | "medium"


PII_PATTERNS: List[PIIPattern] = [
    PIIPattern("email", re.compile(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b"
    ), "[EMAIL]", "high"),
    PIIPattern("phone_us", re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    ), "[PHONE]", "high"),
    PIIPattern("ssn", re.compile(
        r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"
    ), "[SSN]", "high"),
    PIIPattern("credit_card", re.compile(
        r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13}|6(?:011|5\d{2})\d{12})\b"
    ), "[CREDIT_CARD]", "high"),
    PIIPattern("ipv4", re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
    ), "[IP_ADDRESS]", "medium"),
    PIIPattern("date_of_birth", re.compile(
        r"\b(?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])[-/](?:19|20)\d{2}\b"
    ), "[DATE_OF_BIRTH]", "medium"),
    PIIPattern("passport", re.compile(
        r"\b[A-Z]{1,2}\d{6,9}\b"
    ), "[PASSPORT]", "medium"),
]
```

## Solution 2: PII Detector

```python
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PIIFinding:
    pii_type: str
    token_label: str
    original_value: str
    start: int
    end: int
    confidence: str


class PIIDetector:
    """
    Scans text for PII patterns and returns a list of findings
    with positions for targeted redaction.
    """

    def __init__(self, patterns: List[PIIPattern] = None):
        self._patterns = patterns or PII_PATTERNS

    def detect(self, text: str) -> List[PIIFinding]:
        findings = []
        for pii_pattern in self._patterns:
            for m in pii_pattern.pattern.finditer(text):
                findings.append(PIIFinding(
                    pii_type=pii_pattern.pii_type,
                    token_label=pii_pattern.token_label,
                    original_value=m.group(),
                    start=m.start(),
                    end=m.end(),
                    confidence=pii_pattern.confidence,
                ))
        # Sort by position, deduplicate overlapping findings
        findings.sort(key=lambda f: f.start)
        return self._deduplicate(findings)

    def _deduplicate(self, findings: List[PIIFinding]) -> List[PIIFinding]:
        result = []
        last_end = -1
        for f in findings:
            if f.start >= last_end:
                result.append(f)
                last_end = f.end
        return result

    def contains_pii(self, text: str) -> bool:
        return bool(self.detect(text))
```

## Solution 3: PII Redactor

```python
import hashlib
import uuid
from typing import Dict, Optional, Tuple


class PIIRedactor:
    """
    Replaces PII findings in text with type-labeled tokens.
    Optionally maintains a reversible mapping keyed by a redaction session ID
    for authorized incident investigation.
    Supports deterministic replacement (same value -> same token) for log correlation.
    """

    def __init__(
        self,
        detector: PIIDetector,
        deterministic: bool = True,
        store_mapping: bool = False,
    ):
        self._detector = detector
        self._deterministic = deterministic
        self._store_mapping = store_mapping
        self._mappings: Dict[str, Dict[str, str]] = {}   # session_id -> {token -> original}

    def redact(
        self,
        text: str,
        session_id: Optional[str] = None,
    ) -> Tuple[str, List[PIIFinding]]:
        """Returns (redacted_text, list_of_findings)."""
        findings = self._detector.detect(text)
        if not findings:
            return text, []

        result = []
        last_end = 0
        mapping: Dict[str, str] = {}

        for finding in findings:
            result.append(text[last_end:finding.start])
            token = self._make_token(finding)
            result.append(token)
            if self._store_mapping:
                mapping[token] = finding.original_value
            last_end = finding.end

        result.append(text[last_end:])
        redacted = "".join(result)

        if self._store_mapping and session_id and mapping:
            self._mappings.setdefault(session_id, {}).update(mapping)

        return redacted, findings

    def _make_token(self, finding: PIIFinding) -> str:
        if self._deterministic:
            short_hash = hashlib.sha256(finding.original_value.encode()).hexdigest()[:6]
            return f"{finding.token_label[:-1]}:{short_hash}]"
        return finding.token_label

    def reveal(self, session_id: str, redacted_text: str) -> str:
        """Reverse redaction for authorized investigation."""
        mapping = self._mappings.get(session_id, {})
        result = redacted_text
        for token, original in mapping.items():
            result = result.replace(token, original)
        return result

    def purge_session(self, session_id: str) -> int:
        mapping = self._mappings.pop(session_id, {})
        return len(mapping)
```

## Solution 4: Redacting Log Filter

```python
import logging
from typing import Any, Dict, List, Optional


class PIIRedactingLogFilter(logging.Filter):
    """
    Python logging Filter that redacts PII from log record messages
    and all string arguments before the record is emitted.
    Attach to any logger or handler.
    """

    def __init__(self, redactor: PIIRedactor, session_id_attr: str = "session_id"):
        super().__init__()
        self._redactor = redactor
        self._session_id_attr = session_id_attr

    def filter(self, record: logging.LogRecord) -> bool:
        session_id = getattr(record, self._session_id_attr, None)

        if isinstance(record.msg, str):
            record.msg, _ = self._redactor.redact(record.msg, session_id)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (self._redactor.redact(v, session_id)[0] if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._redactor.redact(v, session_id)[0] if isinstance(v, str) else v
                    for v in record.args
                )

        return True
```

## Solution 5: Structured Log Payload Redactor

```python
import json
from typing import Any, Dict, List, Optional


class StructuredLogPayloadRedactor:
    """
    Recursively redacts PII from structured log payloads (dicts/lists)
    before they are serialized to JSON.
    Skips keys in the safe_keys allowlist (e.g., numeric IDs, timestamps).
    """

    SAFE_KEYS = frozenset({
        "timestamp", "level", "service", "version",
        "duration_ms", "status_code", "request_id",
    })

    def __init__(self, redactor: PIIRedactor):
        self._redactor = redactor

    def redact_payload(
        self,
        payload: Any,
        session_id: Optional[str] = None,
        _depth: int = 0,
    ) -> Any:
        if _depth > 10:
            return payload

        if isinstance(payload, str):
            return self._redactor.redact(payload, session_id)[0]

        if isinstance(payload, dict):
            return {
                k: (
                    payload[k] if k in self.SAFE_KEYS
                    else self.redact_payload(v, session_id, _depth + 1)
                )
                for k, v in payload.items()
            }

        if isinstance(payload, list):
            return [self.redact_payload(item, session_id, _depth + 1) for item in payload]

        return payload
```

## Solution 6: PII Redaction Audit Reporter

```python
import time
from collections import defaultdict
from typing import Dict, List


class PIIRedactionAuditReporter:
    """
    Tracks PII redaction events to measure how much PII is flowing
    through the logging pipeline and which PII types appear most often.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._events: List[dict] = []

    def record(self, findings: List[PIIFinding], source: str = "") -> None:
        ts = time.time()
        for finding in findings:
            self._events.append({
                "ts": ts,
                "pii_type": finding.pii_type,
                "confidence": finding.confidence,
                "source": source,
            })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e["ts"] >= cutoff]

    def summary(self) -> dict:
        self._trim()
        by_type: Dict[str, int] = defaultdict(int)
        by_source: Dict[str, int] = defaultdict(int)
        for e in self._events:
            by_type[e["pii_type"]] += 1
            if e["source"]:
                by_source[e["source"]] += 1
        return {
            "window_seconds": self._window,
            "total_pii_redactions": len(self._events),
            "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
            "by_source": dict(sorted(by_source.items(), key=lambda x: -x[1])),
            "high_confidence_count": sum(1 for e in self._events if e["confidence"] == "high"),
        }
```

## Comparison

| Approach | Pattern Detection | In-Place Redaction | Reversible Mapping | Log Integration | Audit Trail |
|---|---|---|---|---|---|
| PIIDetector | Yes (multi-pattern) | No | No | No | No |
| PIIRedactor | Via detector | Yes | Optional | No | No |
| PIIRedactingLogFilter | Via redactor | Yes | Via redactor | Yes (logging.Filter) | No |
| StructuredLogPayloadRedactor | Via redactor | Yes (recursive) | Via redactor | Manual | No |
| PIIRedactionAuditReporter | No | No | No | No | Yes |

**Best for production**: Attach `PIIRedactingLogFilter` to your root logger and to every log handler that writes to external storage (Elasticsearch, Splunk, CloudWatch). Enable `store_mapping=True` only in a dedicated incident-investigation environment, not in production log pipelines — storing the mapping reintroduces the PII you are trying to protect. Use `PIIRedactionAuditReporter` to monitor redaction volume: a sudden spike in email or SSN redactions may indicate users are pasting sensitive data into queries, which warrants a UX intervention to warn them before they submit.
