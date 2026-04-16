---
title: "Agent Doesn't Implement Output Schema Validation Before Delivery"
description: "Agents that return LLM-generated structured output without schema validation deliver malformed JSON, missing required fields, and type mismatches to downstream consumers — causing silent data corruption, client crashes, and hard-to-trace integration bugs. Implement output schema validation that catches structural errors before the response leaves the agent boundary."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-schema-validation-before-delivery
tags: [output-validation, schema-enforcement, structured-output, llm-output-safety, json-validation, contract-enforcement]
symptoms:
  - "Downstream services crash on None values where strings were expected"
  - "LLM occasionally returns Markdown-wrapped JSON that fails to parse"
  - "Required fields missing from structured outputs with no error raised"
  - "Type mismatches slip through — integers returned as strings, arrays as dicts"
  - "No contract between what the LLM was asked to produce and what is actually delivered"
---

## Why This Happens

LLMs are probabilistic. Even with careful prompting, structured output can deviate: extra keys appear, required keys are omitted, numeric fields arrive as strings, or the entire payload is wrapped in a Markdown code fence. Without a validation gate at the agent's output boundary, these deviations propagate downstream where they cause errors far from the source. Output schema validation treats LLM responses as untrusted data — the same way you'd validate API input — and enforces the contract before delivery.

## Solution 1: Output Field Descriptor

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Set


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    LIST = "list"
    DICT = "dict"
    NULLABLE_STRING = "nullable_string"
    ENUM_STRING = "enum_string"


@dataclass
class OutputFieldDescriptor:
    name: str
    field_type: FieldType
    required: bool = True
    min_length: Optional[int] = None       # for strings
    max_length: Optional[int] = None
    min_value: Optional[float] = None      # for numbers
    max_value: Optional[float] = None
    allowed_values: Optional[Set[str]] = None  # for ENUM_STRING
    pattern: Optional[str] = None         # regex for strings
    nested_schema: Optional["OutputSchema"] = None  # for DICT fields

    def type_matches(self, value: Any) -> bool:
        if self.field_type == FieldType.STRING:
            return isinstance(value, str)
        if self.field_type == FieldType.NULLABLE_STRING:
            return value is None or isinstance(value, str)
        if self.field_type == FieldType.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        if self.field_type == FieldType.FLOAT:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if self.field_type == FieldType.BOOLEAN:
            return isinstance(value, bool)
        if self.field_type == FieldType.LIST:
            return isinstance(value, list)
        if self.field_type == FieldType.DICT:
            return isinstance(value, dict)
        if self.field_type == FieldType.ENUM_STRING:
            return isinstance(value, str) and (
                self.allowed_values is None or value in self.allowed_values
            )
        return False
```

## Solution 2: Output Schema

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ValidationViolation:
    field_path: str
    violation_type: str
    expected: str
    actual: str


@dataclass
class OutputSchema:
    name: str
    fields: List[OutputFieldDescriptor] = field(default_factory=list)
    allow_extra_fields: bool = False

    def validate(self, data: Dict[str, Any]) -> List[ValidationViolation]:
        violations = []
        known_keys = {f.name for f in self.fields}

        for descriptor in self.fields:
            if descriptor.name not in data:
                if descriptor.required:
                    violations.append(ValidationViolation(
                        field_path=descriptor.name,
                        violation_type="missing_required_field",
                        expected=descriptor.field_type.value,
                        actual="absent",
                    ))
                continue

            value = data[descriptor.name]
            if not descriptor.type_matches(value):
                violations.append(ValidationViolation(
                    field_path=descriptor.name,
                    violation_type="type_mismatch",
                    expected=descriptor.field_type.value,
                    actual=type(value).__name__,
                ))
                continue

            if isinstance(value, str):
                if descriptor.min_length and len(value) < descriptor.min_length:
                    violations.append(ValidationViolation(
                        field_path=descriptor.name,
                        violation_type="string_too_short",
                        expected=f">={descriptor.min_length}",
                        actual=str(len(value)),
                    ))
                if descriptor.max_length and len(value) > descriptor.max_length:
                    violations.append(ValidationViolation(
                        field_path=descriptor.name,
                        violation_type="string_too_long",
                        expected=f"<={descriptor.max_length}",
                        actual=str(len(value)),
                    ))
                if descriptor.pattern and not __import__("re").match(descriptor.pattern, value):
                    violations.append(ValidationViolation(
                        field_path=descriptor.name,
                        violation_type="pattern_mismatch",
                        expected=descriptor.pattern,
                        actual=value[:50],
                    ))

        if not self.allow_extra_fields:
            for key in data:
                if key not in known_keys:
                    violations.append(ValidationViolation(
                        field_path=key,
                        violation_type="unexpected_field",
                        expected="absent",
                        actual=type(data[key]).__name__,
                    ))

        return violations
```

## Solution 3: LLM Output Extractor

```python
import json
import re
from typing import Any, Optional, Tuple


class LLMOutputExtractor:
    """
    Extracts JSON from LLM responses that may be wrapped in Markdown code
    fences, prefixed with explanation text, or have trailing commentary.
    """

    FENCE_PATTERN = re.compile(
        r"```(?:json)?\s*\n?([\s\S]+?)\n?```", re.IGNORECASE
    )
    INLINE_JSON_PATTERN = re.compile(r"\{[\s\S]+\}", re.DOTALL)

    @classmethod
    def extract(cls, raw_output: str) -> Tuple[Optional[dict], Optional[str]]:
        """
        Returns (parsed_dict, error_message).
        error_message is None on success.
        """
        # Try direct parse first
        try:
            return json.loads(raw_output.strip()), None
        except json.JSONDecodeError:
            pass

        # Try extracting from code fence
        fence_match = cls.FENCE_PATTERN.search(raw_output)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip()), None
            except json.JSONDecodeError:
                pass

        # Try finding bare JSON object
        inline_match = cls.INLINE_JSON_PATTERN.search(raw_output)
        if inline_match:
            try:
                return json.loads(inline_match.group(0)), None
            except json.JSONDecodeError:
                pass

        return None, f"No valid JSON found in output (length={len(raw_output)})"
```

## Solution 4: Output Validation Gate

```python
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class OutputValidationResult:
    valid: bool
    data: Optional[Dict[str, Any]]
    violations: List[ValidationViolation]
    extraction_error: Optional[str]
    schema_name: str
    validated_at: float


class OutputValidationGate:
    """
    Extracts and validates structured output against a registered schema.
    Returns a structured result with violations or the validated payload.
    """

    def __init__(
        self,
        extractor: LLMOutputExtractor,
        schemas: Dict[str, OutputSchema],
    ):
        self._extractor = extractor
        self._schemas = schemas
        self._pass_count = 0
        self._fail_count = 0

    def validate(
        self,
        raw_output: str,
        schema_name: str,
    ) -> OutputValidationResult:
        schema = self._schemas.get(schema_name)
        if schema is None:
            raise KeyError(f"Schema '{schema_name}' not registered")

        data, extraction_error = self._extractor.extract(raw_output)

        if extraction_error or data is None:
            self._fail_count += 1
            return OutputValidationResult(
                valid=False,
                data=None,
                violations=[],
                extraction_error=extraction_error,
                schema_name=schema_name,
                validated_at=time.time(),
            )

        violations = schema.validate(data)
        valid = len(violations) == 0

        if valid:
            self._pass_count += 1
        else:
            self._fail_count += 1

        return OutputValidationResult(
            valid=valid,
            data=data if valid else None,
            violations=violations,
            extraction_error=None,
            schema_name=schema_name,
            validated_at=time.time(),
        )

    def pass_rate(self) -> float:
        total = self._pass_count + self._fail_count
        return round(self._pass_count / max(total, 1), 4)
```

## Solution 5: Validation Failure Logger

```python
import time
from collections import Counter
from typing import List


class OutputValidationFailureLogger:
    """
    Records output validation failures with violation details.
    Surfaces most common violation types and affected schemas.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, result: OutputValidationResult, session_id: str = "") -> None:
        if result.valid:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "schema_name": result.schema_name,
            "extraction_error": result.extraction_error,
            "violation_types": [v.violation_type for v in result.violations],
            "violation_fields": [v.field_path for v in result.violations],
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "failures": 0}
        violation_counts: Counter = Counter()
        for r in recent:
            for vt in r["violation_types"]:
                violation_counts[vt] += 1
        return {
            "window_seconds": window_seconds,
            "failures": len(recent),
            "extraction_errors": sum(1 for r in recent if r["extraction_error"]),
            "top_violation_types": violation_counts.most_common(5),
            "affected_schemas": list({r["schema_name"] for r in recent}),
        }
```

## Solution 6: Output Validation Dashboard

```python
import time


class OutputValidationDashboard:
    """
    Combines gate pass rate, failure log summary, and schema registry
    into a single view for output quality monitoring.
    """

    def __init__(
        self,
        gate: OutputValidationGate,
        logger: OutputValidationFailureLogger,
    ):
        self._gate = gate
        self._logger = logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "registered_schemas": list(self._gate._schemas.keys()),
            "pass_rate": self._gate.pass_rate(),
            "pass_count": self._gate._pass_count,
            "fail_count": self._gate._fail_count,
            "failure_summary_1h": self._logger.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | JSON Extraction | Schema Validation | Type Checking | Failure Logging | Dashboard |
|---|---|---|---|---|---|
| LLMOutputExtractor | Yes (fence/bare) | No | No | No | No |
| OutputSchema / OutputFieldDescriptor | No | Yes | Yes | No | No |
| OutputValidationGate | Via extractor | Via schema | Via schema | No | No |
| OutputValidationFailureLogger | No | No | No | Yes | No |
| OutputValidationDashboard | No | No | No | No | Yes |

**Best for production**: Always run `LLMOutputExtractor` before schema validation — Markdown-fenced JSON is the single most common deviation in structured output prompts. Set `allow_extra_fields=False` to catch when the model hallucinates keys not in your schema; extra fields are a signal that the prompt's output specification is being misread. Monitor `pass_rate` per schema name: a schema with pass rate below 0.90 means either the prompt needs tightening or the schema constraints are too strict. Use `NULLABLE_STRING` for any field the model might omit in error cases rather than marking it required — required fields that the model frequently omits will show up as `missing_required_field` violations in the log.
