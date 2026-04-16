---
title: "Agent Doesn't Implement PII Detection Before Sending Data to External Tools"
description: "Agents that forward user-provided content to external tools — web search, analytics, third-party APIs — without PII scanning transmit names, email addresses, phone numbers, and government IDs to external services where they may be logged, indexed, or retained in violation of privacy regulations. Implement PII detection that scans outbound tool arguments and either blocks, masks, or alerts before transmission."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-pii-detection-before-sending-data-to-external-tools
tags: [pii-detection, data-privacy, gdpr, outbound-scanning, tool-argument-masking, privacy-compliance]
symptoms:
  - "User emails and phone numbers appear in third-party analytics tool call logs"
  - "Search queries containing full names are sent to external search APIs"
  - "No scanning of tool arguments before they leave the agent boundary"
  - "Compliance audit reveals personal data transmitted to services without DPA agreements"
  - "Agent forwards SSNs and credit card numbers entered by users to external validation APIs"
---

## Why This Happens

Agents aggregate data from users and pass it to tools. The tool dispatch layer is a natural egress point for PII — it's where user-provided data becomes an external API call. Without a scan at this boundary, PII flows wherever the tool goes: into third-party logs, analytics platforms, and cached search results. PII detection at the tool argument level must handle both structured fields (named arguments like `email_address`) and unstructured text (search query strings or document content that may contain embedded PII). Detection requires a combination of field-name heuristics and value-pattern regex.

## Solution 1: PII Pattern Registry

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Pattern


class PIICategory(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    DATE_OF_BIRTH = "date_of_birth"
    FULL_NAME = "full_name"
    IP_ADDRESS = "ip_address"
    PASSPORT = "passport"
    NATIONAL_ID = "national_id"
    MEDICAL_RECORD = "medical_record"


@dataclass
class PIIPattern:
    category: PIICategory
    name_patterns: List[str]        # regex matched against field names
    value_pattern: Optional[str]    # regex matched against field values
    sensitivity: str = "high"       # "high" | "medium"
    _compiled_value: re.Pattern = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        if self.value_pattern:
            self._compiled_value = re.compile(self.value_pattern, re.IGNORECASE)

    def matches_name(self, field_name: str) -> bool:
        for pat in self.name_patterns:
            if re.search(pat, field_name, re.IGNORECASE):
                return True
        return False

    def matches_value(self, value: str) -> bool:
        if self._compiled_value:
            return bool(self._compiled_value.search(value))
        return False


def default_pii_patterns() -> List[PIIPattern]:
    return [
        PIIPattern(
            category=PIICategory.EMAIL,
            name_patterns=[r"email", r"e-?mail"],
            value_pattern=r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        ),
        PIIPattern(
            category=PIICategory.PHONE,
            name_patterns=[r"phone", r"mobile", r"tel(ephone)?", r"cell"],
            value_pattern=r"(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}",
        ),
        PIIPattern(
            category=PIICategory.SSN,
            name_patterns=[r"ssn", r"social[_\-\s]?security"],
            value_pattern=r"\b\d{3}-\d{2}-\d{4}\b",
            sensitivity="high",
        ),
        PIIPattern(
            category=PIICategory.CREDIT_CARD,
            name_patterns=[r"credit[_\-]?card", r"card[_\-]?number", r"cc[_\-]?num"],
            value_pattern=r"\b(?:\d[ -]?){13,16}\b",
        ),
        PIIPattern(
            category=PIICategory.IP_ADDRESS,
            name_patterns=[r"ip[_\-]?(addr(ess)?)?", r"client[_\-]?ip"],
            value_pattern=r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
            sensitivity="medium",
        ),
        PIIPattern(
            category=PIICategory.DATE_OF_BIRTH,
            name_patterns=[r"dob", r"birth[_\-]?date", r"date[_\-]?of[_\-]?birth"],
            value_pattern=r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
            sensitivity="medium",
        ),
    ]
```

## Solution 2: PII Scanner

```python
import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class PIIFinding:
    field_path: str
    category: PIICategory
    sensitivity: str
    match_type: str     # "field_name" | "value_pattern" | "both"
    value_preview: str  # first 8 chars + "..."


class PIIScanner:
    """
    Scans a dict (tool arguments) for PII findings using field-name
    heuristics and value-pattern regex. Returns all findings without
    modifying the original data.
    """

    def __init__(self, patterns: List[PIIPattern]):
        self._patterns = patterns

    def scan(self, args: Dict[str, Any], path_prefix: str = "") -> List[PIIFinding]:
        findings = []
        for key, value in args.items():
            full_path = f"{path_prefix}.{key}" if path_prefix else key
            findings.extend(self._check_field(full_path, key, value))
            if isinstance(value, dict):
                findings.extend(self.scan(value, full_path))
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        findings.extend(self.scan(item, f"{full_path}[{i}]"))
                    elif isinstance(item, str):
                        findings.extend(self._check_field(f"{full_path}[{i}]", "", item))
        return findings

    def _check_field(self, path: str, name: str, value: Any) -> List[PIIFinding]:
        if not isinstance(value, str):
            return []
        results = []
        for pattern in self._patterns:
            name_match = name and pattern.matches_name(name)
            value_match = pattern.matches_value(value)
            if name_match or value_match:
                results.append(PIIFinding(
                    field_path=path,
                    category=pattern.category,
                    sensitivity=pattern.sensitivity,
                    match_type=(
                        "both" if name_match and value_match
                        else "field_name" if name_match
                        else "value_pattern"
                    ),
                    value_preview=value[:8] + "..." if len(value) > 8 else value,
                ))
        return results
```

## Solution 3: PII Redactor

```python
import copy
import re
from typing import Any, Dict, List


class PIIRedactor:
    """
    Produces a redacted copy of tool arguments with PII values
    replaced by category-labeled placeholders.
    """

    @staticmethod
    def redact(
        args: Dict[str, Any],
        findings: List[PIIFinding],
    ) -> Dict[str, Any]:
        redacted = copy.deepcopy(args)
        for finding in findings:
            PIIRedactor._apply_redaction(redacted, finding.field_path, finding.category)
        return redacted

    @staticmethod
    def _apply_redaction(
        data: Any,
        path: str,
        category: PIICategory,
    ) -> None:
        parts = path.split(".", 1)
        key = parts[0]
        rest = parts[1] if len(parts) > 1 else None

        # Handle list index
        list_match = re.match(r"^(.*)\[(\d+)\]$", key)
        if list_match:
            field = list_match.group(1)
            idx = int(list_match.group(2))
            if field and isinstance(data.get(field), list):
                if rest:
                    PIIRedactor._apply_redaction(data[field][idx], rest, category)
                else:
                    data[field][idx] = f"[{category.value.upper()}_REDACTED]"
            return

        if rest and isinstance(data, dict) and key in data:
            PIIRedactor._apply_redaction(data[key], rest, category)
        elif isinstance(data, dict) and key in data:
            if isinstance(data[key], str):
                data[key] = f"[{category.value.upper()}_REDACTED]"
```

## Solution 4: Outbound PII Guard

```python
import asyncio
from typing import Any, Callable, Dict, List


class OutboundPIIGuard:
    """
    Intercepts tool call arguments before dispatch.
    Configurable policy: BLOCK (refuse call), REDACT (mask PII and proceed),
    or ALERT (log finding and proceed unmodified).
    """

    def __init__(
        self,
        scanner: PIIScanner,
        redactor: PIIRedactor,
        policy: str = "redact",   # "block" | "redact" | "alert"
        block_on_sensitivity: str = "high",
    ):
        self._scanner = scanner
        self._redactor = redactor
        self._policy = policy
        self._block_sensitivity = block_on_sensitivity
        self._blocked = 0
        self._redacted = 0
        self._alerted = 0
        self._clean = 0

    async def guard(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
    ) -> dict:
        findings = self._scanner.scan(args)

        if not findings:
            self._clean += 1
            result = await tool_fn(tool_name, args)
            return {"result": result, "pii_action": "none", "findings": []}

        high_findings = [f for f in findings if f.sensitivity == "high"]

        if self._policy == "block" and high_findings:
            self._blocked += 1
            raise PIIBlockedError(tool_name, [f.category.value for f in high_findings])

        if self._policy in ("redact", "block"):
            redacted_args = self._redactor.redact(args, findings)
            self._redacted += 1
            result = await tool_fn(tool_name, redacted_args)
            return {
                "result": result,
                "pii_action": "redacted",
                "findings": [f.category.value for f in findings],
            }

        # alert-only
        self._alerted += 1
        result = await tool_fn(tool_name, args)
        return {
            "result": result,
            "pii_action": "alerted",
            "findings": [f.category.value for f in findings],
        }

    def stats(self) -> dict:
        return {
            "clean_calls": self._clean,
            "blocked_calls": self._blocked,
            "redacted_calls": self._redacted,
            "alerted_calls": self._alerted,
        }


class PIIBlockedError(Exception):
    def __init__(self, tool_name: str, categories: List[str]):
        super().__init__(
            f"Tool call '{tool_name}' blocked: PII categories detected: {categories}"
        )
        self.tool_name = tool_name
        self.categories = categories
```

## Solution 5: PII Transmission Audit Log

```python
import time
from collections import Counter
from typing import List


class PIITransmissionAuditLog:
    """
    Records PII detection events at the tool boundary for compliance
    reporting and privacy impact assessment.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        tool_name: str,
        findings: List[PIIFinding],
        action: str,
        session_id: str = "",
    ) -> None:
        if not findings:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "categories": [f.category.value for f in findings],
            "high_sensitivity_count": sum(1 for f in findings if f.sensitivity == "high"),
            "action": action,
            "session_id": session_id,
        })

    def summary(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "pii_events": 0}
        cat_counts: Counter = Counter()
        for r in recent:
            for c in r["categories"]:
                cat_counts[c] += 1
        return {
            "window_seconds": window_seconds,
            "pii_events": len(recent),
            "high_sensitivity_events": sum(r["high_sensitivity_count"] for r in recent),
            "top_categories": cat_counts.most_common(5),
            "top_tools": Counter(r["tool_name"] for r in recent).most_common(5),
            "blocked_count": sum(1 for r in recent if r["action"] == "blocked"),
        }
```

## Solution 6: PII Detection Dashboard

```python
import time


class PIIDetectionDashboard:
    """
    Combines guard stats, audit log summary, and pattern registry
    into a privacy compliance health report.
    """

    def __init__(
        self,
        guard: OutboundPIIGuard,
        audit_log: PIITransmissionAuditLog,
        patterns: List[PIIPattern],
    ):
        self._guard = guard
        self._audit = audit_log
        self._patterns = patterns

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "policy": self._guard._policy,
            "pattern_count": len(self._patterns),
            "guard_stats": self._guard.stats(),
            "audit_24h": self._audit.summary(window_seconds=86400.0),
            "audit_7d": self._audit.summary(window_seconds=604800.0),
        }
```

## Comparison

| Approach | Field-Name Detection | Value-Pattern Detection | Redaction | Blocking | Audit |
|---|---|---|---|---|---|
| PIIPattern / default registry | Yes (regex) | Yes (regex) | No | No | No |
| PIIScanner | Yes | Yes | No | No | No |
| PIIRedactor | No | No | Yes (placeholder) | No | No |
| OutboundPIIGuard | Via scanner | Via scanner | Via redactor | Yes | No |
| PIITransmissionAuditLog | No | No | No | No | Yes |
| PIIDetectionDashboard | No | No | No | No | Yes |

**Best for production**: Apply PII scanning only at the tool egress boundary — scanning every intermediate in-memory operation is wasteful. Use `policy="redact"` for most tools and `policy="block"` for tools that call external analytics or advertising platforms where PII must never appear even transiently. Audit `top_tools` in the log summary: a tool that repeatedly receives PII is a data flow design problem — the caller should strip PII before calling the tool rather than relying on the guard to redact it every time. Treat `high_sensitivity_events` as a compliance metric: an unexplained spike indicates a new user flow that passes raw PII to tools.
