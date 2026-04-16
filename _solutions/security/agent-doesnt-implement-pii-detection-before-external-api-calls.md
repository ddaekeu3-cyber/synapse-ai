---
title: "Agent Doesn't Implement PII Detection Before External API Calls"
description: "Agents that forward user-supplied content to external APIs without PII scanning risk transmitting names, email addresses, phone numbers, and national ID numbers to third-party services that may store or log the data. Implement PII detection that scans tool arguments before external API calls and either blocks the call, redacts the PII, or requires explicit consent before transmission."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-pii-detection-before-external-api-calls
tags: [pii-detection, data-privacy, external-api, gdpr, pii-redaction, data-minimization]
symptoms:
  - "User emails and phone numbers passed verbatim to third-party enrichment APIs"
  - "Full names and addresses forwarded to external search tools without scrubbing"
  - "National ID numbers included in API call arguments destined for external services"
  - "No distinction between internal tool calls (safe to include PII) and external calls (must redact)"
  - "Compliance audit finds PII in outbound API call logs to third-party vendors"
---

## Why This Happens

Agents receive PII from users through conversation and pass it along to tools — a lookup tool gets a name and phone number, a search tool gets an address, a notification tool gets an email. Internal tools that operate on data the user explicitly provided are generally acceptable; external API calls to third-party services are not, because the agent cannot control what those services do with the data. Without a PII detection layer, there is no gate between what the user typed and what leaves the system boundary. PII detection requires pattern matching (email regex, phone number formats, national ID patterns) plus name entity recognition heuristics before any outbound call.

## Solution 1: PII Pattern Library

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Pattern


class PIICategory(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    NATIONAL_ID = "national_id"
    CREDIT_CARD = "credit_card"
    IP_ADDRESS = "ip_address"
    DATE_OF_BIRTH = "date_of_birth"
    POSTAL_ADDRESS = "postal_address"
    PASSPORT = "passport"


@dataclass
class PIIPattern:
    category: PIICategory
    pattern: Pattern
    redact_strategy: str = "full"   # "full" | "partial"
    partial_keep_prefix: int = 0
    partial_keep_suffix: int = 0
    description: str = ""


PII_PATTERNS: List[PIIPattern] = [
    PIIPattern(
        category=PIICategory.EMAIL,
        pattern=re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        redact_strategy="partial",
        partial_keep_prefix=2,
        partial_keep_suffix=4,
        description="Email address",
    ),
    PIIPattern(
        category=PIICategory.PHONE,
        pattern=re.compile(r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
        redact_strategy="partial",
        partial_keep_prefix=0,
        partial_keep_suffix=4,
        description="North American phone number",
    ),
    PIIPattern(
        category=PIICategory.NATIONAL_ID,
        pattern=re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b"),   # SSN format
        redact_strategy="full",
        description="Social Security Number",
    ),
    PIIPattern(
        category=PIICategory.CREDIT_CARD,
        pattern=re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b"),
        redact_strategy="partial",
        partial_keep_prefix=4,
        partial_keep_suffix=4,
        description="Credit card number",
    ),
    PIIPattern(
        category=PIICategory.IP_ADDRESS,
        pattern=re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"),
        redact_strategy="partial",
        partial_keep_prefix=0,
        partial_keep_suffix=3,
        description="IPv4 address",
    ),
    PIIPattern(
        category=PIICategory.DATE_OF_BIRTH,
        pattern=re.compile(r"\b(0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])[-/](19|20)\d{2}\b"),
        redact_strategy="full",
        description="Date of birth",
    ),
    PIIPattern(
        category=PIICategory.PASSPORT,
        pattern=re.compile(r"\b[A-Z]{1,2}[0-9]{6,9}\b"),
        redact_strategy="full",
        description="Passport number",
    ),
]
```

## Solution 2: PII Scanner

```python
import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class PIIFinding:
    path: str
    category: PIICategory
    pattern_description: str
    original_length: int


class PIIScanner:
    """
    Recursively scans dict/list/string structures for PII patterns.
    Returns a redacted deep copy and a list of findings.
    """

    REDACTED = "[PII_REDACTED]"

    def __init__(self, patterns: List[PIIPattern] = None):
        self._patterns = patterns or PII_PATTERNS

    def scan_and_redact(self, data: Any) -> tuple[Any, List[PIIFinding]]:
        findings: List[PIIFinding] = []
        redacted = self._process(copy.deepcopy(data), "$", findings)
        return redacted, findings

    def _process(self, obj: Any, path: str, findings: List[PIIFinding]) -> Any:
        if isinstance(obj, str):
            return self._redact_string(obj, path, findings)
        if isinstance(obj, dict):
            return {k: self._process(v, f"{path}.{k}", findings) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._process(item, f"{path}[{i}]", findings) for i, item in enumerate(obj)]
        return obj

    def _redact_string(self, value: str, path: str, findings: List[PIIFinding]) -> str:
        result = value
        for pdef in self._patterns:
            def replace_match(m, pdef=pdef):
                matched = m.group()
                findings.append(PIIFinding(
                    path=path,
                    category=pdef.category,
                    pattern_description=pdef.description,
                    original_length=len(matched),
                ))
                if pdef.redact_strategy == "partial":
                    pre = pdef.partial_keep_prefix
                    suf = pdef.partial_keep_suffix
                    if len(matched) <= pre + suf:
                        return self.REDACTED
                    tail = matched[-suf:] if suf > 0 else ""
                    return f"{matched[:pre]}***{tail}"
                return self.REDACTED

            result = pdef.pattern.sub(replace_match, result)
        return result
```

## Solution 3: External API PII Gate

```python
import time
from typing import Any, Callable, Dict, List, Optional, Set


class ExternalAPIPIIGate:
    """
    Gates tool calls destined for external APIs. Scans arguments for PII
    and either blocks, redacts, or passes through based on policy.
    """

    EXTERNAL_TOOL_TAGS = {"external", "third_party", "outbound"}

    def __init__(
        self,
        scanner: PIIScanner,
        external_tool_names: Set[str] = None,
        policy: str = "redact",   # "block" | "redact" | "warn"
        blocked_categories: List[PIICategory] = None,
    ):
        self._scanner = scanner
        self._external_tools = external_tool_names or set()
        self._policy = policy
        self._blocked_categories = set(blocked_categories or [
            PIICategory.NATIONAL_ID, PIICategory.CREDIT_CARD, PIICategory.PASSPORT
        ])
        self._pii_detections = 0
        self._blocked_calls = 0

    def is_external(self, tool_name: str) -> bool:
        return tool_name in self._external_tools

    def process(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> dict:
        if not self.is_external(tool_name):
            return {"allowed": True, "args": args, "pii_found": False}

        redacted_args, findings = self._scanner.scan_and_redact(args)

        if not findings:
            return {"allowed": True, "args": args, "pii_found": False}

        self._pii_detections += 1
        categories_found = {f.category for f in findings}
        hard_blocked = categories_found & self._blocked_categories

        if hard_blocked or self._policy == "block":
            self._blocked_calls += 1
            return {
                "allowed": False,
                "args": None,
                "pii_found": True,
                "categories": [c.value for c in categories_found],
                "reason": f"PII detected in external tool call: {[c.value for c in hard_blocked or categories_found]}",
            }

        if self._policy == "redact":
            return {
                "allowed": True,
                "args": redacted_args,
                "pii_found": True,
                "categories": [c.value for c in categories_found],
                "redacted_count": len(findings),
            }

        return {
            "allowed": True,
            "args": args,
            "pii_found": True,
            "warning": f"PII detected but policy is 'warn': {[c.value for c in categories_found]}",
        }

    def stats(self) -> dict:
        return {
            "pii_detections": self._pii_detections,
            "blocked_calls": self._blocked_calls,
            "external_tools_registered": len(self._external_tools),
        }
```

## Solution 4: PII Audit Logger

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List


class PIIAuditLogger:
    """
    Records PII detection events for compliance auditing.
    Provides category frequency summaries for privacy reporting.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, tool_name: str, gate_result: dict, session_id: str = "") -> None:
        if not gate_result.get("pii_found"):
            return
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "tool_name": tool_name,
                "session_id": session_id,
                "allowed": gate_result.get("allowed"),
                "categories": gate_result.get("categories", []),
                "redacted_count": gate_result.get("redacted_count", 0),
            })
            if len(self._records) > self._max:
                self._records.popleft()

    def summary(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        category_counts: dict = {}
        for r in recent:
            for cat in r.get("categories", []):
                category_counts[cat] = category_counts.get(cat, 0) + 1
        return {
            "window_seconds": window_seconds,
            "pii_events": len(recent),
            "blocked": sum(1 for r in recent if not r["allowed"]),
            "redacted": sum(1 for r in recent if r["allowed"] and r.get("redacted_count", 0) > 0),
            "by_category": category_counts,
        }
```

## Solution 5: Consent-Gated PII Transmitter

```python
from typing import Any, Callable, Dict, Optional, Set


class ConsentGatedPIITransmitter:
    """
    For cases where PII transmission is necessary (e.g., KYC workflows),
    requires explicit per-session consent before allowing PII to pass
    through the gate to external APIs.
    """

    def __init__(self, gate: ExternalAPIPIIGate):
        self._gate = gate
        self._consented_sessions: Set[str] = set()

    def grant_consent(self, session_id: str) -> None:
        self._consented_sessions.add(session_id)

    def revoke_consent(self, session_id: str) -> None:
        self._consented_sessions.discard(session_id)

    def process(
        self,
        tool_name: str,
        args: Dict[str, Any],
        session_id: str = "",
    ) -> dict:
        result = self._gate.process(tool_name, args)
        if result.get("pii_found") and not result.get("allowed"):
            if session_id in self._consented_sessions:
                return {"allowed": True, "args": args, "pii_found": True, "consent_granted": True}
        return result
```

## Solution 6: PII Detection Dashboard

```python
import time


class PIIDetectionDashboard:
    """
    Combines gate stats, audit summary, and consent status into
    an operational privacy compliance report.
    """

    def __init__(
        self,
        gate: ExternalAPIPIIGate,
        logger: PIIAuditLogger,
    ):
        self._gate = gate
        self._logger = logger

    def render(self) -> dict:
        gate_stats = self._gate.stats()
        audit = self._logger.summary(window_seconds=86400.0)
        return {
            "generated_at": time.time(),
            "gate": gate_stats,
            "last_24h": audit,
            "policy": self._gate._policy,
            "hard_blocked_categories": [c.value for c in self._gate._blocked_categories],
        }
```

## Comparison

| Approach | Pattern Detection | Recursive Deep Scan | External Tool Gate | Consent Override | Audit Log |
|---|---|---|---|---|---|
| PIIScanner | Yes (regex) | Yes | No | No | No |
| ExternalAPIPIIGate | Via scanner | Via scanner | Yes | No | No |
| ConsentGatedPIITransmitter | Via gate | Via gate | Via gate | Yes | No |
| PIIAuditLogger | No | No | No | No | Yes |
| PIIDetectionDashboard | No | No | No | No | No |

**Best for production**: Start with `policy="redact"` so legitimate workflows are not broken while PII is sanitized. Move high-risk categories (`NATIONAL_ID`, `CREDIT_CARD`, `PASSPORT`) to `blocked_categories` immediately — these should never appear in external API calls under any policy. Use `ConsentGatedPIITransmitter` for KYC and identity-verification tools where the business purpose requires transmitting PII: the consent record creates an audit trail. Run `PIIAuditLogger.summary(window_seconds=86400)` daily in a compliance report — consistent high volumes in `by_category` for a specific tool indicate that tool's prompt or argument construction is unnecessarily including PII.
