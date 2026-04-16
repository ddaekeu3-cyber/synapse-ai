---
title: "Agent Doesn't Implement Sensitive Field Masking in Tool Arguments Before Logging"
description: "Agents that log raw tool call arguments expose secrets in log pipelines: API keys passed as tool parameters appear in plaintext in Datadog, Splunk, or CloudWatch, where they may be indexed, replicated to cold storage, and accessible to anyone with log read access. Implement sensitive field masking that detects and redacts credential-like values in tool arguments before they reach any log sink."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-sensitive-field-masking-in-tool-arguments-before-logging
tags: [log-masking, secret-redaction, credential-leakage, argument-sanitization, pii-masking, log-security]
symptoms:
  - "API keys appear in plaintext in log aggregation platforms"
  - "Tool arguments logged as raw JSON strings containing password fields"
  - "Bearer tokens passed to HTTP tool calls visible in application logs"
  - "No distinction between safe-to-log and unsafe-to-log argument fields"
  - "Compliance audit fails because PII and credentials exist in log storage"
---

## Why This Happens

Logging frameworks log what they receive. When a tool call dispatcher logs `{"tool": "http_request", "args": {"headers": {"Authorization": "Bearer sk-abc123"}}}`, the token lands in every downstream log sink with no transformation. Sensitive field masking must be applied as a distinct pre-log step — not inside the tool, not as an afterthought, but as a mandatory transform in the logging path. The challenge is that sensitive fields do not always have predictable names: a credential might be in `api_key`, `token`, `secret`, `password`, or a custom field. Masking must combine name-based detection with value-pattern detection.

## Solution 1: Sensitive Field Descriptor

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Pattern


class MaskingStrategy(str, Enum):
    FULL_REDACT = "full_redact"       # replace with "[REDACTED]"
    PARTIAL_MASK = "partial_mask"     # show first/last N chars: "sk-ab***123"
    HASH = "hash"                     # replace with sha256[:8] for correlation
    OMIT = "omit"                     # remove the field entirely from the log


@dataclass
class SensitiveFieldDescriptor:
    """Describes how to detect and mask a sensitive field."""
    name_patterns: List[str]          # regex patterns matched against field names
    value_patterns: List[str] = field(default_factory=list)  # regex matched against value
    strategy: MaskingStrategy = MaskingStrategy.FULL_REDACT
    partial_show_prefix: int = 4      # chars to show before mask (for PARTIAL_MASK)
    partial_show_suffix: int = 4      # chars to show after mask

    def matches_name(self, field_name: str) -> bool:
        for pat in self.name_patterns:
            if re.search(pat, field_name, re.IGNORECASE):
                return True
        return False

    def matches_value(self, value: str) -> bool:
        for pat in self.value_patterns:
            if re.search(pat, value, re.IGNORECASE):
                return True
        return False
```

## Solution 2: Built-In Sensitive Field Registry

```python
from typing import List


def default_sensitive_field_registry() -> List[SensitiveFieldDescriptor]:
    """
    Returns a default set of sensitive field descriptors covering common
    credential and PII field names and value patterns.
    """
    return [
        SensitiveFieldDescriptor(
            name_patterns=[r"password", r"passwd", r"pwd"],
            strategy=MaskingStrategy.FULL_REDACT,
        ),
        SensitiveFieldDescriptor(
            name_patterns=[r"api[_\-]?key", r"apikey"],
            value_patterns=[r"^[A-Za-z0-9\-_]{20,}$"],
            strategy=MaskingStrategy.PARTIAL_MASK,
            partial_show_prefix=4,
            partial_show_suffix=4,
        ),
        SensitiveFieldDescriptor(
            name_patterns=[r"secret", r"client[_\-]?secret"],
            strategy=MaskingStrategy.FULL_REDACT,
        ),
        SensitiveFieldDescriptor(
            name_patterns=[r"token", r"access[_\-]?token", r"refresh[_\-]?token"],
            value_patterns=[r"^Bearer\s", r"^[A-Za-z0-9\-_.]{30,}$"],
            strategy=MaskingStrategy.PARTIAL_MASK,
            partial_show_prefix=6,
            partial_show_suffix=4,
        ),
        SensitiveFieldDescriptor(
            name_patterns=[r"authorization", r"auth"],
            strategy=MaskingStrategy.PARTIAL_MASK,
            partial_show_prefix=7,
            partial_show_suffix=0,
        ),
        SensitiveFieldDescriptor(
            name_patterns=[r"credit[_\-]?card", r"card[_\-]?number", r"cvv", r"cvc"],
            strategy=MaskingStrategy.FULL_REDACT,
        ),
        SensitiveFieldDescriptor(
            name_patterns=[r"ssn", r"social[_\-]?security"],
            strategy=MaskingStrategy.FULL_REDACT,
        ),
        SensitiveFieldDescriptor(
            name_patterns=[r"private[_\-]?key", r"pem", r"rsa"],
            strategy=MaskingStrategy.OMIT,
        ),
    ]
```

## Solution 3: Field Value Masker

```python
import hashlib
from typing import Any


class FieldValueMasker:
    """
    Applies a masking strategy to a single field value.
    """

    REDACTED_PLACEHOLDER = "[REDACTED]"

    @classmethod
    def mask(cls, value: Any, descriptor: SensitiveFieldDescriptor) -> Any:
        if not isinstance(value, str):
            if descriptor.strategy == MaskingStrategy.OMIT:
                return None   # sentinel: caller should drop the field
            return cls.REDACTED_PLACEHOLDER

        strategy = descriptor.strategy

        if strategy == MaskingStrategy.FULL_REDACT:
            return cls.REDACTED_PLACEHOLDER

        if strategy == MaskingStrategy.OMIT:
            return None   # sentinel

        if strategy == MaskingStrategy.HASH:
            digest = hashlib.sha256(value.encode()).hexdigest()[:8]
            return f"[HASH:{digest}]"

        if strategy == MaskingStrategy.PARTIAL_MASK:
            pre = descriptor.partial_show_prefix
            suf = descriptor.partial_show_suffix
            if len(value) <= pre + suf:
                return cls.REDACTED_PLACEHOLDER
            prefix = value[:pre]
            suffix = value[-suf:] if suf > 0 else ""
            stars = "*" * min(len(value) - pre - suf, 6)
            return f"{prefix}{stars}{suffix}"

        return cls.REDACTED_PLACEHOLDER
```

## Solution 4: Tool Argument Log Sanitizer

```python
import copy
from typing import Any, Dict, List


class ToolArgumentLogSanitizer:
    """
    Recursively walks a tool argument dict and masks any field
    whose name or value matches a sensitive field descriptor.
    Returns a deep copy — the original args are never mutated.
    """

    def __init__(self, descriptors: List[SensitiveFieldDescriptor]):
        self._descriptors = descriptors
        self._masker = FieldValueMasker()

    def sanitize(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._sanitize_dict(copy.deepcopy(args))

    def _sanitize_dict(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        for key, value in obj.items():
            masked_value = self._check_field(key, value)
            if masked_value is None:
                # OMIT strategy — skip field entirely
                continue
            result[key] = masked_value
        return result

    def _check_field(self, key: str, value: Any) -> Any:
        for descriptor in self._descriptors:
            if descriptor.matches_name(key):
                masked = FieldValueMasker.mask(value, descriptor)
                return masked  # None signals OMIT
            if isinstance(value, str) and descriptor.matches_value(value):
                masked = FieldValueMasker.mask(value, descriptor)
                return masked
        # Recurse into nested dicts/lists
        if isinstance(value, dict):
            return self._sanitize_dict(value)
        if isinstance(value, list):
            return [self._check_field(f"[{i}]", v) for i, v in enumerate(value)]
        return value
```

## Solution 5: Sanitized Tool Call Logger

```python
import json
import time
from typing import Any, Callable, Dict, Optional


class SanitizedToolCallLogger:
    """
    Produces log records for tool calls with all sensitive fields masked.
    Accepts a write_fn (e.g. structlog, stdlib logging) to decouple
    masking from the actual log sink.
    """

    def __init__(
        self,
        sanitizer: ToolArgumentLogSanitizer,
        write_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._sanitizer = sanitizer
        self._write = write_fn or self._default_write
        self._logged_calls = 0
        self._masked_fields_total = 0

    @staticmethod
    def _default_write(record: dict) -> None:
        print(json.dumps(record))

    def log_call(
        self,
        tool_name: str,
        raw_args: Dict[str, Any],
        outcome: str = "ok",
        latency_ms: float = 0.0,
        error: Optional[str] = None,
    ) -> None:
        safe_args = self._sanitizer.sanitize(raw_args)
        self._logged_calls += 1

        record = {
            "event": "tool_call",
            "tool_name": tool_name,
            "args": safe_args,
            "outcome": outcome,
            "latency_ms": latency_ms,
            "timestamp": time.time(),
        }
        if error:
            record["error"] = error

        self._write(record)

    def stats(self) -> dict:
        return {"logged_calls": self._logged_calls}
```

## Solution 6: Masking Coverage Auditor

```python
import re
from typing import Any, Dict, List


CREDENTIAL_VALUE_HEURISTICS = [
    re.compile(r"^sk-[A-Za-z0-9]{20,}$"),           # OpenAI-style keys
    re.compile(r"^Bearer\s+[A-Za-z0-9\-_.]{20,}$"),  # Bearer tokens
    re.compile(r"^[A-Za-z0-9+/]{40,}={0,2}$"),       # Base64 secrets
    re.compile(r"^ghp_[A-Za-z0-9]{36}$"),             # GitHub PATs
    re.compile(r"^[0-9]{13,16}$"),                    # Credit card numbers
]


class MaskingCoverageAuditor:
    """
    Scans a set of raw tool argument samples to detect fields that look
    like credentials but are not covered by any sensitive field descriptor.
    Use this during development to discover gaps in the descriptor set.
    """

    def __init__(self, descriptors: List[SensitiveFieldDescriptor]):
        self._descriptors = descriptors

    def _is_covered(self, field_name: str, value: Any) -> bool:
        for desc in self._descriptors:
            if desc.matches_name(field_name):
                return True
            if isinstance(value, str) and desc.matches_value(value):
                return True
        return False

    def _looks_like_credential(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        for heuristic in CREDENTIAL_VALUE_HEURISTICS:
            if heuristic.match(value):
                return True
        return False

    def audit(self, samples: List[Dict[str, Any]]) -> List[dict]:
        gaps = []
        for sample in samples:
            for key, value in sample.items():
                if self._looks_like_credential(value) and not self._is_covered(key, value):
                    gaps.append({
                        "field_name": key,
                        "value_prefix": str(value)[:10] + "...",
                        "recommendation": f"Add a SensitiveFieldDescriptor covering name '{key}'",
                    })
        return gaps
```

## Comparison

| Approach | Name-Based Detection | Value-Pattern Detection | Deep Nesting | Audit Gap Detection | Log Integration |
|---|---|---|---|---|---|
| SensitiveFieldDescriptor | Yes (regex) | Yes (regex) | No | No | No |
| FieldValueMasker | No | No | No | No | No |
| ToolArgumentLogSanitizer | Via descriptors | Via descriptors | Yes (recursive) | No | No |
| SanitizedToolCallLogger | Via sanitizer | Via sanitizer | Via sanitizer | No | Yes |
| MaskingCoverageAuditor | Via descriptors | Via descriptors | No | Yes | No |

**Best for production**: Run `MaskingCoverageAuditor.audit()` against a sample of real tool call logs in a staging environment before deploying — this surfaces fields the default descriptor set does not cover. Never log `raw_args` directly; always pass through `ToolArgumentLogSanitizer.sanitize()` first and make this a lint/review requirement. Use `MaskingStrategy.PARTIAL_MASK` for API keys (rather than full redact) so that key rotation incidents can be correlated across logs without exposing the full secret. Set `MaskingStrategy.OMIT` for private keys and PEM data — these should never appear in any log record, even masked.
