---
title: "Agent Doesn't Implement LLM Response Content Type Enforcement"
description: "Agents that accept any LLM output format without content type enforcement are exploitable via prompt injection: an attacker causes the model to return a JSON object with injected fields when the agent expects a plain string, or a shell command string when the agent expects a structured action object. Implement content type enforcement that validates the structural type and format of every LLM response before the agent acts on it."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-llm-response-content-type-enforcement
tags: [content-type-enforcement, response-validation, prompt-injection, output-integrity, structured-output, type-safety]
symptoms:
  - "Prompt injection causes model to return JSON when plain text is expected — agent crashes on parse"
  - "Model returns action object with injected 'admin' field that downstream handler accepts"
  - "No check that model returned the declared output format before acting on result"
  - "LLM occasionally returns markdown code fences around JSON — parser fails silently"
  - "Response content type varies by model version — no compatibility check after model upgrade"
---

## Why This Happens

LLM responses are strings. Agents that expect JSON parse the string and use the result; agents that expect plain text pass the string directly to downstream systems. Neither enforces that the model actually returned the expected type. A prompt injection that causes the model to return a different structure — extra fields, a different root type, a completely different format — passes through without detection. Content type enforcement requires declaring the expected response type before the LLM call and validating the actual response against that declaration before any downstream action is taken.

## Solution 1: Response Content Type Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Set


class ResponseContentType(str, Enum):
    PLAIN_TEXT = "plain_text"
    JSON_OBJECT = "json_object"
    JSON_ARRAY = "json_array"
    MARKDOWN = "markdown"
    ACTION_OBJECT = "action_object"   # structured {action, parameters}
    BOOLEAN_ANSWER = "boolean_answer" # yes/no, true/false
    NUMERIC = "numeric"


@dataclass
class ResponseTypeDescriptor:
    content_type: ResponseContentType
    required_json_keys: Set[str] = field(default_factory=set)
    forbidden_json_keys: Set[str] = field(default_factory=set)
    max_length: Optional[int] = None
    min_length: Optional[int] = None
    allow_markdown_wrapper: bool = True   # strip ```json ... ``` before parse
    description: str = ""
```

## Solution 2: Response Content Type Validator

```python
import json
import re
from typing import Any, Tuple


class ContentTypeViolation(Exception):
    def __init__(self, expected: str, detail: str, raw: str):
        super().__init__(f"Content type violation (expected {expected}): {detail}")
        self.expected = expected
        self.detail = detail
        self.raw = raw


class ResponseContentTypeValidator:
    """
    Validates an LLM response string against a ResponseTypeDescriptor.
    Returns (parsed_value, warnings) on success.
    Raises ContentTypeViolation on structural mismatch.
    """

    _MD_CODE_FENCE = re.compile(r"^```(?:json|python|text)?\s*([\s\S]+?)\s*```$", re.MULTILINE)

    def validate(
        self,
        raw: str,
        descriptor: ResponseTypeDescriptor,
    ) -> Tuple[Any, list]:
        warnings = []
        text = raw.strip()

        # Length checks
        if descriptor.min_length and len(text) < descriptor.min_length:
            raise ContentTypeViolation(
                descriptor.content_type.value,
                f"Response length {len(text)} < min {descriptor.min_length}",
                raw,
            )
        if descriptor.max_length and len(text) > descriptor.max_length:
            warnings.append(f"Response length {len(text)} exceeds max {descriptor.max_length}")
            text = text[: descriptor.max_length]

        ctype = descriptor.content_type

        if ctype == ResponseContentType.PLAIN_TEXT:
            return text, warnings

        if ctype == ResponseContentType.BOOLEAN_ANSWER:
            lower = text.lower().strip(".,!? ")
            if lower in ("yes", "true", "1", "affirmative"):
                return True, warnings
            if lower in ("no", "false", "0", "negative"):
                return False, warnings
            raise ContentTypeViolation(ctype.value, f"Cannot parse as boolean: {text[:50]!r}", raw)

        if ctype == ResponseContentType.NUMERIC:
            try:
                return float(text), warnings
            except ValueError:
                raise ContentTypeViolation(ctype.value, f"Cannot parse as number: {text[:50]!r}", raw)

        if ctype in (ResponseContentType.JSON_OBJECT, ResponseContentType.JSON_ARRAY, ResponseContentType.ACTION_OBJECT):
            # Strip markdown fences if allowed
            if descriptor.allow_markdown_wrapper:
                m = self._MD_CODE_FENCE.search(text)
                if m:
                    text = m.group(1).strip()
                    warnings.append("stripped markdown code fence from response")

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ContentTypeViolation(ctype.value, f"JSON parse error: {exc}", raw)

            if ctype == ResponseContentType.JSON_ARRAY and not isinstance(parsed, list):
                raise ContentTypeViolation(ctype.value, f"Expected array, got {type(parsed).__name__}", raw)

            if ctype in (ResponseContentType.JSON_OBJECT, ResponseContentType.ACTION_OBJECT):
                if not isinstance(parsed, dict):
                    raise ContentTypeViolation(ctype.value, f"Expected object, got {type(parsed).__name__}", raw)
                missing = descriptor.required_json_keys - set(parsed.keys())
                if missing:
                    raise ContentTypeViolation(ctype.value, f"Missing required keys: {missing}", raw)
                injected = descriptor.forbidden_json_keys & set(parsed.keys())
                if injected:
                    raise ContentTypeViolation(ctype.value, f"Forbidden keys present: {injected}", raw)

            return parsed, warnings

        if ctype == ResponseContentType.MARKDOWN:
            return text, warnings

        return text, warnings
```

## Solution 3: Typed LLM Response

```python
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class TypedLLMResponse:
    raw: str
    parsed: Any
    content_type: str
    warnings: List[str] = field(default_factory=list)
    valid: bool = True
    violation: Optional[str] = None
```

## Solution 4: Content-Type-Enforcing LLM Caller

```python
import time
from typing import Any, Callable, Optional


class ContentTypeEnforcingLLMCaller:
    """
    Wraps any LLM call function and validates the response content type
    before returning. On violation, either raises or returns a TypedLLMResponse
    with valid=False, depending on strict_mode.
    """

    def __init__(
        self,
        validator: ResponseContentTypeValidator,
        strict_mode: bool = True,
        violation_log_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._validator = validator
        self._strict = strict_mode
        self._log = violation_log_fn
        self._violations = 0
        self._total = 0

    async def call(
        self,
        llm_fn: Callable,
        descriptor: ResponseTypeDescriptor,
        *args: Any,
        **kwargs: Any,
    ) -> TypedLLMResponse:
        self._total += 1
        raw = await llm_fn(*args, **kwargs)

        try:
            parsed, warnings = self._validator.validate(raw, descriptor)
            return TypedLLMResponse(
                raw=raw,
                parsed=parsed,
                content_type=descriptor.content_type.value,
                warnings=warnings,
                valid=True,
            )
        except ContentTypeViolation as exc:
            self._violations += 1
            if self._log:
                self._log({
                    "event": "content_type_violation",
                    "ts": time.time(),
                    "expected": exc.expected,
                    "detail": exc.detail,
                    "raw_preview": exc.raw[:200],
                })
            if self._strict:
                raise
            return TypedLLMResponse(
                raw=raw,
                parsed=None,
                content_type=descriptor.content_type.value,
                valid=False,
                violation=exc.detail,
            )

    def stats(self) -> dict:
        return {
            "total_calls": self._total,
            "violations": self._violations,
            "violation_rate": round(self._violations / max(self._total, 1), 4),
        }
```

## Solution 5: Forbidden Key Injection Scanner

```python
import json
import re
from typing import Any, List, Set


class ForbiddenKeyInjectionScanner:
    """
    Scans a parsed JSON object (or raw string) for keys that are
    commonly injected via prompt manipulation. Returns a list of
    detected injection signals.
    """

    COMMON_INJECTION_KEYS = {
        "admin", "sudo", "root", "override", "bypass",
        "system_prompt", "ignore_instructions", "jailbreak",
        "role", "tool_call_override", "allow_all",
        "__proto__", "constructor",
    }

    def scan(self, value: Any, extra_forbidden: Set[str] = None) -> List[str]:
        forbidden = self.COMMON_INJECTION_KEYS | (extra_forbidden or set())
        findings = []
        self._scan_recursive(value, forbidden, findings, path="")
        return findings

    def _scan_recursive(self, value: Any, forbidden: Set[str], findings: List[str], path: str) -> None:
        if isinstance(value, dict):
            for key in value:
                full_path = f"{path}.{key}" if path else key
                if key.lower() in forbidden:
                    findings.append(f"Forbidden key at '{full_path}'")
                self._scan_recursive(value[key], forbidden, findings, full_path)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self._scan_recursive(item, forbidden, findings, f"{path}[{i}]")
```

## Solution 6: Content Type Enforcement Audit Logger

```python
import time
from threading import Lock
from typing import List


class ContentTypeEnforcementAuditLogger:
    """
    Records content type violations and injection scan findings
    for trend analysis and model behavior monitoring.
    """

    def __init__(self, max_records: int = 5000):
        self._records: List[dict] = []
        self._lock = Lock()
        self._max = max_records

    def record_violation(self, detail: str, expected_type: str, raw_preview: str) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "kind": "violation",
                "expected_type": expected_type,
                "detail": detail,
                "raw_preview": raw_preview[:100],
            })
            if len(self._records) > self._max:
                self._records.pop(0)

    def record_injection(self, findings: List[str], raw_preview: str) -> None:
        if not findings:
            return
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "kind": "injection",
                "findings": findings,
                "raw_preview": raw_preview[:100],
            })
            if len(self._records) > self._max:
                self._records.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        violations = [r for r in recent if r["kind"] == "violation"]
        injections = [r for r in recent if r["kind"] == "injection"]
        return {
            "window_seconds": window_seconds,
            "violations": len(violations),
            "injection_detections": len(injections),
            "total_events": len(recent),
        }
```

## Comparison

| Approach | Type Checking | Required Key Validation | Forbidden Key Detection | Injection Scan | Audit Log |
|---|---|---|---|---|---|
| ResponseContentTypeValidator | Yes (all types) | Yes | Yes | No | No |
| ContentTypeEnforcingLLMCaller | Via validator | Via validator | Via validator | No | Yes |
| ForbiddenKeyInjectionScanner | No | No | Yes (common patterns) | Yes (recursive) | No |
| ContentTypeEnforcementAuditLogger | No | No | No | No | Yes |

**Best for production**: Declare a `ResponseTypeDescriptor` for every distinct LLM call site — mixing a "return JSON action" call with a "return plain text summary" call without separate descriptors means both accept each other's output. Set `forbidden_json_keys={"admin", "override", "sudo", "system_prompt"}` on action objects; these are the most common prompt injection targets. Run `ForbiddenKeyInjectionScanner.scan()` on every parsed JSON response, not just on known action objects — injection can occur in data fields too. Set `strict_mode=True` in production and `strict_mode=False` in development to surface violations without breaking the dev loop.
