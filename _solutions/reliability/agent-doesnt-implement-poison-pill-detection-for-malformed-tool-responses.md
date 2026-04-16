---
title: "Agent Doesn't Implement Poison Pill Detection for Malformed Tool Responses"
description: "Agents that pass raw tool responses directly into the LLM context without validation expose the reasoning loop to poison pill inputs: a tool returning a truncated JSON object, an embedding service returning NaN values, or a search API returning a response containing LLM instruction text. Implement poison pill detection that validates tool response structure, detects anomalous content, and quarantines responses that could corrupt the agent's reasoning or inject instructions."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-poison-pill-detection-for-malformed-tool-responses
tags: [poison-pill, tool-response-validation, malformed-response, response-quarantine, schema-validation, context-injection]
symptoms:
  - "A search tool returns a result containing 'IGNORE PREVIOUS INSTRUCTIONS' in the document body"
  - "A database tool returns a partially truncated JSON object that breaks the LLM's parsing"
  - "An embedding tool returns NaN values that silently corrupt downstream similarity calculations"
  - "No structural validation on tool responses — whatever the tool returns goes directly into context"
  - "Agent produces nonsensical outputs after receiving a malformed tool response"
---

## Why This Happens

Tool responses are trusted implicitly. The tool contract specifies what to return, but external APIs, databases, and third-party services occasionally return malformed, truncated, or adversarially-crafted content. Without a validation layer between tool execution and context injection, any structural anomaly or injected text flows directly into the LLM's context window. Poison pill detection treats every tool response as untrusted until validated: it checks schema compliance, scans for injection patterns, validates numeric ranges, and quarantines suspicious responses before they corrupt the reasoning loop.

## Solution 1: Tool Response Schema

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Type


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    LIST = "list"
    DICT = "dict"
    ANY = "any"


@dataclass
class FieldSpec:
    name: str
    field_type: FieldType
    required: bool = True
    allow_null: bool = False
    min_value: Optional[float] = None   # for numeric types
    max_value: Optional[float] = None
    max_length: Optional[int] = None    # for strings/lists
    no_nan: bool = True                 # reject NaN/Inf for floats
    custom_validator: Optional[Callable[[Any], bool]] = None


@dataclass
class ToolResponseSchema:
    tool_name: str
    fields: List[FieldSpec] = field(default_factory=list)
    allow_extra_fields: bool = True
    max_response_bytes: int = 1_000_000   # 1MB default
    required_type: Optional[type] = None  # e.g. dict or list at top level
```

## Solution 2: Schema Validator

```python
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ValidationResult:
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @classmethod
    def ok(cls) -> "ValidationResult":
        return cls(valid=True)

    @classmethod
    def fail(cls, *errors: str) -> "ValidationResult":
        return cls(valid=False, errors=list(errors))


from dataclasses import field as dc_field


class ToolResponseSchemaValidator:
    """
    Validates a tool response dict/value against a ToolResponseSchema.
    Reports all errors rather than failing on the first.
    """

    def validate(self, response: Any, schema: ToolResponseSchema) -> ValidationResult:
        errors = []
        warnings = []

        # Type check at top level
        if schema.required_type is not None and not isinstance(response, schema.required_type):
            return ValidationResult.fail(
                f"Expected {schema.required_type.__name__} at top level, "
                f"got {type(response).__name__}"
            )

        # Size check
        try:
            import json
            size = len(json.dumps(response, default=str).encode())
            if size > schema.max_response_bytes:
                errors.append(
                    f"Response size {size} bytes exceeds limit {schema.max_response_bytes}"
                )
        except Exception:
            warnings.append("Could not estimate response size")

        if not isinstance(response, dict):
            return ValidationResult(valid=not errors, errors=errors, warnings=warnings)

        # Field validation
        for spec in schema.fields:
            value = response.get(spec.name)

            if value is None:
                if spec.required and not spec.allow_null:
                    errors.append(f"Required field '{spec.name}' is missing or null")
                continue

            # Type check
            type_map = {
                FieldType.STRING: str,
                FieldType.INTEGER: int,
                FieldType.FLOAT: (int, float),
                FieldType.BOOLEAN: bool,
                FieldType.LIST: list,
                FieldType.DICT: dict,
            }
            expected = type_map.get(spec.field_type)
            if expected and not isinstance(value, expected):
                errors.append(
                    f"Field '{spec.name}' expected {spec.field_type}, got {type(value).__name__}"
                )
                continue

            # Numeric checks
            if spec.field_type == FieldType.FLOAT and isinstance(value, float):
                if spec.no_nan and (math.isnan(value) or math.isinf(value)):
                    errors.append(f"Field '{spec.name}' contains NaN or Inf")
                if spec.min_value is not None and value < spec.min_value:
                    errors.append(f"Field '{spec.name}' value {value} below minimum {spec.min_value}")
                if spec.max_value is not None and value > spec.max_value:
                    errors.append(f"Field '{spec.name}' value {value} above maximum {spec.max_value}")

            # Length checks
            if spec.max_length is not None and hasattr(value, "__len__"):
                if len(value) > spec.max_length:
                    errors.append(
                        f"Field '{spec.name}' length {len(value)} exceeds limit {spec.max_length}"
                    )

            # Custom validator
            if spec.custom_validator and not spec.custom_validator(value):
                errors.append(f"Field '{spec.name}' failed custom validation")

        return ValidationResult(valid=not errors, errors=errors, warnings=warnings)
```

## Solution 3: Injection Pattern Scanner for Tool Responses

```python
import re
from typing import Any, List


TOOL_RESPONSE_INJECTION_PATTERNS = [
    (re.compile(r"(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior|above)\s+instructions", re.I), "instruction_override"),
    (re.compile(r"(?:you are now|pretend to be|act as if you are)", re.I), "role_reassignment"),
    (re.compile(r"<\|(?:im_start|system|endoftext)\|>", re.I), "special_token"),
    (re.compile(r"system:\s*\n", re.I), "role_marker"),
    (re.compile(r"human:\s*assistant:", re.I), "dialogue_injection"),
    (re.compile(r"(?:new\s+)?(?:system\s+)?prompt:?\s*\n", re.I), "prompt_injection_marker"),
]


class ToolResponseInjectionScanner:
    """
    Scans tool response content for prompt injection patterns.
    Applies to all string values recursively within the response.
    """

    def scan(self, response: Any, depth: int = 0) -> List[dict]:
        if depth > 5:
            return []
        findings = []
        if isinstance(response, str):
            for pattern, sig_name in TOOL_RESPONSE_INJECTION_PATTERNS:
                if pattern.search(response):
                    findings.append({
                        "signature": sig_name,
                        "value_prefix": response[:60].replace("\n", "\\n"),
                    })
        elif isinstance(response, dict):
            for key, value in response.items():
                sub = self.scan(value, depth + 1)
                for f in sub:
                    f["field_path"] = f"{key}.{f.get('field_path', '')}" .rstrip(".")
                findings.extend(sub)
        elif isinstance(response, list):
            for i, item in enumerate(response[:20]):  # scan first 20 items
                sub = self.scan(item, depth + 1)
                for f in sub:
                    f["field_path"] = f"[{i}].{f.get('field_path', '')}".rstrip(".")
                findings.extend(sub)
        return findings
```

## Solution 4: Poison Pill Detector

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PoisonPillResult:
    poisoned: bool
    quarantined: bool
    schema_errors: List[str] = field(default_factory=list)
    injection_findings: List[dict] = field(default_factory=list)
    safe_value: Any = None   # None if quarantined, validated value otherwise
    detected_at: float = field(default_factory=time.time)
    tool_name: str = ""

    def is_safe(self) -> bool:
        return not self.poisoned and not self.quarantined


class PoisonPillDetector:
    """
    Combines schema validation and injection scanning.
    Quarantines responses with critical errors; warns on non-critical issues.
    """

    QUARANTINE_ON_INJECTION = True   # injection findings always quarantine

    def __init__(
        self,
        validator: ToolResponseSchemaValidator,
        scanner: ToolResponseInjectionScanner,
        schemas: Optional[Dict[str, ToolResponseSchema]] = None,
    ):
        self._validator = validator
        self._scanner = scanner
        self._schemas: Dict[str, ToolResponseSchema] = schemas or {}
        self._detections = 0
        self._quarantined = 0

    def register_schema(self, schema: ToolResponseSchema) -> None:
        self._schemas[schema.tool_name] = schema

    def check(self, tool_name: str, response: Any) -> PoisonPillResult:
        schema = self._schemas.get(tool_name)

        # Schema validation
        schema_errors = []
        if schema:
            result = self._validator.validate(response, schema)
            schema_errors = result.errors

        # Injection scan
        injection_findings = self._scanner.scan(response)

        poisoned = bool(schema_errors or injection_findings)
        quarantine = bool(injection_findings) or (schema and bool(schema_errors))

        if poisoned:
            self._detections += 1
        if quarantine:
            self._quarantined += 1

        return PoisonPillResult(
            poisoned=poisoned,
            quarantined=quarantine,
            schema_errors=schema_errors,
            injection_findings=injection_findings,
            safe_value=None if quarantine else response,
            tool_name=tool_name,
        )

    def stats(self) -> dict:
        return {
            "total_detections": self._detections,
            "total_quarantined": self._quarantined,
        }
```

## Solution 5: Quarantine-Gated Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class QuarantineGatedToolDispatcher:
    """
    Wraps tool execution with poison pill detection.
    Raises on quarantined responses; returns validated value on clean responses.
    Optionally supplies a sanitized fallback value for non-injection schema errors.
    """

    def __init__(
        self,
        detector: PoisonPillDetector,
        fallback_on_schema_error: bool = False,
    ):
        self._detector = detector
        self._fallback_on_schema_error = fallback_on_schema_error

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        fallback_value: Any = None,
        **kwargs: Any,
    ) -> Any:
        raw_response = await tool_fn(**kwargs)
        result = self._detector.check(tool_name, raw_response)

        if result.is_safe():
            return result.safe_value if result.safe_value is not None else raw_response

        if result.injection_findings:
            raise ValueError(
                f"Tool '{tool_name}' response quarantined: injection pattern detected "
                f"({result.injection_findings[0]['signature']})"
            )

        if result.schema_errors and self._fallback_on_schema_error:
            return fallback_value

        raise ValueError(
            f"Tool '{tool_name}' response failed schema validation: "
            + "; ".join(result.schema_errors[:3])
        )
```

## Solution 6: Poison Pill Audit Reporter

```python
import time
from collections import defaultdict
from typing import List


class PoisonPillAuditReporter:
    """
    Aggregates poison pill detections across tool calls.
    Repeated detections from the same tool indicate a systemic issue
    (adversarial content in the data source, buggy API, etc.).
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._events: List[dict] = []

    def record(self, result: PoisonPillResult, session_id: str = "") -> None:
        if result.poisoned:
            self._events.append({
                "ts": time.time(),
                "tool_name": result.tool_name,
                "quarantined": result.quarantined,
                "has_injection": bool(result.injection_findings),
                "schema_error_count": len(result.schema_errors),
                "session_id": session_id,
            })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e["ts"] >= cutoff]

    def summary(self) -> dict:
        self._trim()
        total = len(self._events)
        injections = sum(1 for e in self._events if e["has_injection"])
        quarantined = sum(1 for e in self._events if e["quarantined"])
        by_tool: dict = defaultdict(int)
        for e in self._events:
            by_tool[e["tool_name"]] += 1

        alerts = []
        for tool, count in by_tool.items():
            if count >= 5:
                alerts.append({
                    "type": "repeated_poison_pill",
                    "tool": tool,
                    "count": count,
                    "message": f"Tool '{tool}' has triggered {count} poison pill detections — inspect data source.",
                })

        return {
            "total_detections": total,
            "injection_detections": injections,
            "quarantined": quarantined,
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])),
            "alerts": alerts,
        }
```

## Comparison

| Approach | Schema Validation | Injection Scanning | Quarantine Logic | Fallback Support | Audit Reporting |
|---|---|---|---|---|---|
| ToolResponseSchemaValidator | Yes | No | No | No | No |
| ToolResponseInjectionScanner | No | Yes (recursive) | No | No | No |
| PoisonPillDetector | Via validator | Via scanner | Yes | No | No |
| QuarantineGatedToolDispatcher | Via detector | Via detector | Via detector | Yes | No |
| PoisonPillAuditReporter | No | No | No | No | Yes |

**Best for production**: Register a `ToolResponseSchema` for every external tool — this is the baseline defence. Injection scanning applies automatically without schemas. Use `QuarantineGatedToolDispatcher` with `fallback_on_schema_error=True` for enrichment tools where a malformed response is recoverable; set it to `False` for tools whose response is essential to the answer. Monitor `PoisonPillAuditReporter.summary()` for tools with repeated detections — five or more detections in an hour indicates either an adversarially-crafted data source or a broken upstream API that should be removed from the tool registry until fixed.
