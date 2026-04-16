---
title: "Agent Doesn't Implement Output Schema Validation Before Returning to Caller"
description: "Agents that return LLM-generated structured output to callers without schema validation allow malformed, incomplete, or type-mismatched responses to propagate downstream — causing JSON parse errors, null pointer exceptions, or silent data corruption in the caller. Implement output schema validation that checks LLM responses against a declared schema before returning, with repair attempts for minor violations and hard rejection for schema mismatches."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-schema-validation-before-returning-to-caller
tags: [output-validation, schema-validation, structured-output, llm-output, type-safety, response-integrity]
symptoms:
  - "Caller receives a dict missing required fields because the LLM omitted them"
  - "Integer fields contain string values — LLM wrote '\"count\": \"five\"' instead of '\"count\": 5'"
  - "No validation between LLM JSON output and the declared response schema"
  - "Downstream service crashes with KeyError or AttributeError on missing fields"
  - "LLM occasionally wraps the JSON in markdown code fences that break the parser"
---

## Why This Happens

LLMs produce text. Even when prompted to return JSON matching a schema, they occasionally wrap the output in markdown fences, swap field names, use string values for numeric fields, or omit optional-but-expected fields. Callers that directly deserialize LLM output and pass it to typed code will fail unpredictably. Output schema validation must sit between the LLM and the caller — parse the text, extract JSON, validate types and required fields, attempt minor repairs, and only pass validated output through.

## Solution 1: Output Schema Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Type


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    LIST = "list"
    DICT = "dict"
    ANY = "any"


@dataclass
class OutputFieldSpec:
    name: str
    field_type: FieldType
    required: bool = True
    nullable: bool = False
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    allowed_values: Optional[List[Any]] = None
    default: Any = None


@dataclass
class OutputSchema:
    name: str
    fields: List[OutputFieldSpec]
    allow_extra_fields: bool = False

    def field_map(self) -> Dict[str, OutputFieldSpec]:
        return {f.name: f for f in self.fields}
```

## Solution 2: JSON Extractor

```python
import json
import re
from typing import Optional


class LLMOutputJSONExtractor:
    """
    Extracts a JSON object from raw LLM output, handling common
    formatting issues: markdown fences, leading/trailing text,
    single-quoted keys, and trailing commas.
    """

    _FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
    _TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")

    def extract(self, raw_text: str) -> Optional[dict]:
        # Try direct parse first
        stripped = raw_text.strip()
        result = self._try_parse(stripped)
        if result is not None:
            return result

        # Try extracting from markdown fence
        fence_match = self._FENCE_RE.search(stripped)
        if fence_match:
            result = self._try_parse(fence_match.group(1))
            if result is not None:
                return result

        # Try finding first { ... } block
        brace_start = stripped.find("{")
        brace_end = stripped.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            result = self._try_parse(stripped[brace_start:brace_end + 1])
            if result is not None:
                return result

        return None

    def _try_parse(self, text: str) -> Optional[dict]:
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            # Fix trailing commas and retry
            fixed = self._TRAILING_COMMA_RE.sub(r"\1", text)
            try:
                parsed = json.loads(fixed)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
```

## Solution 3: Schema Validator and Repairer

```python
from typing import Any, Dict, List, Optional, Tuple


class OutputSchemaValidator:
    """
    Validates a parsed dict against an OutputSchema.
    Attempts lightweight repairs: coercing types, inserting defaults.
    Returns the validated (possibly repaired) dict and a list of violations.
    """

    TYPE_COERCIONS = {
        FieldType.INTEGER: int,
        FieldType.FLOAT: float,
        FieldType.STRING: str,
        FieldType.BOOLEAN: bool,
    }

    def validate(
        self,
        data: dict,
        schema: OutputSchema,
    ) -> Tuple[Optional[dict], List[str]]:
        violations: List[str] = []
        result = dict(data)
        field_map = schema.field_map()

        # Check required fields
        for spec in schema.fields:
            if spec.name not in result:
                if spec.required and spec.default is None and not spec.nullable:
                    violations.append(f"missing required field: '{spec.name}'")
                elif spec.default is not None:
                    result[spec.name] = spec.default

        # Validate and coerce present fields
        for spec in schema.fields:
            if spec.name not in result:
                continue
            value = result[spec.name]

            if value is None:
                if not spec.nullable:
                    violations.append(f"null value for non-nullable field '{spec.name}'")
                continue

            coerced = self._coerce(spec, value)
            if coerced is None:
                violations.append(
                    f"type mismatch for '{spec.name}': expected {spec.field_type.value}, got {type(value).__name__}"
                )
                continue
            result[spec.name] = coerced

            if spec.allowed_values and coerced not in spec.allowed_values:
                violations.append(f"'{spec.name}' value {coerced!r} not in allowed values")

            if spec.min_value is not None and isinstance(coerced, (int, float)):
                if coerced < spec.min_value:
                    violations.append(f"'{spec.name}' value {coerced} below minimum {spec.min_value}")

            if spec.max_length is not None and isinstance(coerced, (str, list)):
                if len(coerced) > spec.max_length:
                    violations.append(f"'{spec.name}' length {len(coerced)} exceeds max {spec.max_length}")

        # Strip extra fields unless allowed
        if not schema.allow_extra_fields:
            extra = [k for k in result if k not in field_map]
            for k in extra:
                del result[k]

        return (result if not violations else None), violations

    def _coerce(self, spec: OutputFieldSpec, value: Any) -> Any:
        expected_py_type = {
            FieldType.STRING: str,
            FieldType.INTEGER: (int, float),
            FieldType.FLOAT: (int, float),
            FieldType.BOOLEAN: bool,
            FieldType.LIST: list,
            FieldType.DICT: dict,
            FieldType.ANY: object,
        }.get(spec.field_type, object)

        if isinstance(value, expected_py_type if isinstance(expected_py_type, tuple) else (expected_py_type,)):
            if spec.field_type == FieldType.INTEGER:
                return int(value)
            if spec.field_type == FieldType.FLOAT:
                return float(value)
            return value

        coerce_fn = self.TYPE_COERCIONS.get(spec.field_type)
        if coerce_fn:
            try:
                return coerce_fn(value)
            except (ValueError, TypeError):
                return None
        return None
```

## Solution 4: Validated Output Gateway

```python
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class ValidationResult:
    valid: bool
    data: Optional[dict]
    violations: List[str]
    raw_text: str
    extraction_failed: bool = False


class ValidatedOutputGateway:
    """
    Combines JSON extraction and schema validation into a single
    gateway that sits between the LLM response and the caller.
    """

    def __init__(
        self,
        extractor: LLMOutputJSONExtractor,
        validator: OutputSchemaValidator,
    ):
        self._extractor = extractor
        self._validator = validator

    def process(self, raw_text: str, schema: OutputSchema) -> ValidationResult:
        parsed = self._extractor.extract(raw_text)

        if parsed is None:
            return ValidationResult(
                valid=False,
                data=None,
                violations=["could not extract JSON from LLM output"],
                raw_text=raw_text,
                extraction_failed=True,
            )

        validated, violations = self._validator.validate(parsed, schema)
        return ValidationResult(
            valid=validated is not None,
            data=validated,
            violations=violations,
            raw_text=raw_text,
        )
```

## Solution 5: Retry-Backed Schema Enforcer

```python
from typing import Any, Callable, Optional


class RetryBackedSchemaEnforcer:
    """
    Calls the LLM up to max_retries times, feeding validation errors
    back into the prompt on each failure to guide the model toward
    a schema-conformant response.
    """

    def __init__(
        self,
        gateway: ValidatedOutputGateway,
        max_retries: int = 2,
    ):
        self._gateway = gateway
        self._max_retries = max_retries

    async def enforce(
        self,
        prompt: str,
        schema: OutputSchema,
        llm_fn: Callable[[str], str],
    ) -> ValidationResult:
        current_prompt = prompt
        last_result: Optional[ValidationResult] = None

        for attempt in range(self._max_retries + 1):
            raw = await llm_fn(current_prompt)
            result = self._gateway.process(raw, schema)

            if result.valid:
                return result

            last_result = result
            if attempt < self._max_retries:
                error_summary = "; ".join(result.violations[:3])
                current_prompt = (
                    f"{prompt}\n\n"
                    f"Previous response was invalid: {error_summary}. "
                    f"Return valid JSON matching the schema exactly."
                )

        return last_result
```

## Solution 6: Output Validation Audit Logger

```python
import time
from typing import List


class OutputValidationAuditLogger:
    """
    Records validation outcomes for monitoring schema compliance
    rates and identifying which fields are most often violated.
    """

    def __init__(self, max_records: int = 5000):
        self._records: List[dict] = []
        self._max = max_records

    def record(self, result: ValidationResult, schema_name: str = "") -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "schema": schema_name,
            "valid": result.valid,
            "extraction_failed": result.extraction_failed,
            "violation_count": len(result.violations),
            "violations": result.violations[:5],
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "validations": 0}
        valid_count = sum(1 for r in recent if r["valid"])
        field_violations: dict = {}
        for r in recent:
            for v in r.get("violations", []):
                field_violations[v] = field_violations.get(v, 0) + 1
        return {
            "window_seconds": window_seconds,
            "validations": len(recent),
            "valid_rate": round(valid_count / len(recent), 4),
            "extraction_failures": sum(1 for r in recent if r["extraction_failed"]),
            "top_violations": sorted(field_violations.items(), key=lambda x: -x[1])[:5],
        }
```

## Comparison

| Approach | JSON Extraction | Type Coercion | Required Field Check | Retry on Failure | Audit |
|---|---|---|---|---|---|
| LLMOutputJSONExtractor | Yes (fence+brace) | No | No | No | No |
| OutputSchemaValidator | No | Yes | Yes | No | No |
| ValidatedOutputGateway | Via extractor | Via validator | Via validator | No | No |
| RetryBackedSchemaEnforcer | Via gateway | Via gateway | Via gateway | Yes | No |
| OutputValidationAuditLogger | No | No | No | No | Yes |

**Best for production**: Run `ValidatedOutputGateway.process()` on every structured LLM response before returning to the caller — never trust raw LLM JSON directly. Use `RetryBackedSchemaEnforcer` with `max_retries=1` for responses where schema compliance is business-critical (e.g., function call arguments, API payloads); the second attempt with violation feedback fixes the majority of LLM formatting mistakes. Monitor `valid_rate` from `OutputValidationAuditLogger`: a rate below 0.95 indicates the output prompt needs tighter schema instructions or examples. Track `extraction_failures` separately — they indicate the model is producing prose instead of JSON and the prompt framing needs adjustment.
