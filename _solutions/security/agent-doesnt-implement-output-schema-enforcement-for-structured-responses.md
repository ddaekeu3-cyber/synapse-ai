---
title: "Agent Doesn't Implement Output Schema Enforcement for Structured Responses"
description: "Agents that expect structured JSON from LLMs but accept any output without validation forward malformed or incomplete data to downstream systems, causing runtime errors or silent data corruption. Implement output schema enforcement that validates every LLM-generated structured response against a defined schema before it leaves the agent, with typed coercion, required field checks, and repair-and-retry for recoverable failures."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-schema-enforcement-for-structured-responses
tags: [output-validation, schema-enforcement, structured-output, json-validation, type-coercion, llm-output-safety]
symptoms:
  - "LLM returns JSON missing required fields — downstream system receives None for critical keys"
  - "Agent passes raw LLM string output to json.loads() without try/except — crashes on malformed JSON"
  - "Numeric fields returned as strings by LLM cause type errors in downstream processing"
  - "No schema version tracking — schema changes break downstream consumers silently"
  - "LLM occasionally wraps JSON in markdown code fences — not stripped before parsing"
---

## Why This Happens

LLMs do not guarantee valid JSON even when instructed to produce it. They may wrap output in markdown fences, add explanatory text before or after the JSON block, omit optional fields, return numbers as strings, or hallucinate fields not in the schema. Accepting raw LLM output as structured data is a trust boundary violation — the LLM is an untrusted source and its output must be validated and coerced before use. Schema enforcement adds the validation layer and provides a repair-and-retry path for recoverable failures like missing optional fields or minor type mismatches.

## Solution 1: Output Field Schema

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional


class OutputFieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    LIST = "list"
    OBJECT = "object"
    NULLABLE_STRING = "nullable_string"


@dataclass
class OutputFieldSchema:
    name: str
    field_type: OutputFieldType
    required: bool = True
    default: Any = None
    min_length: Optional[int] = None      # for STRING
    max_length: Optional[int] = None      # for STRING
    min_value: Optional[float] = None     # for INTEGER/FLOAT
    max_value: Optional[float] = None
    allowed_values: List[Any] = field(default_factory=list)  # enum constraint
    nested_schema: Optional["OutputSchema"] = None            # for OBJECT


@dataclass
class OutputSchema:
    name: str
    version: str
    fields: List[OutputFieldSchema]
    allow_extra_fields: bool = False   # if False, extra keys are stripped
```

## Solution 2: JSON Extractor

```python
import json
import re
from typing import Optional, Tuple


class LLMOutputJSONExtractor:
    """
    Extracts JSON from LLM output that may contain markdown code fences,
    preamble text, or postamble explanations.
    """

    CODE_FENCE = re.compile(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", re.IGNORECASE)
    BARE_OBJECT = re.compile(r"(\{[\s\S]*\})", re.DOTALL)
    BARE_ARRAY = re.compile(r"(\[[\s\S]*\])", re.DOTALL)

    @classmethod
    def extract(cls, raw: str) -> Tuple[Optional[dict], str]:
        """
        Returns (parsed_object, extraction_method) or (None, "failed").
        """
        raw = raw.strip()

        # Try direct parse first
        try:
            return json.loads(raw), "direct"
        except json.JSONDecodeError:
            pass

        # Try extracting from code fence
        fence_match = cls.CODE_FENCE.search(raw)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip()), "code_fence"
            except json.JSONDecodeError:
                pass

        # Try extracting bare object
        obj_match = cls.BARE_OBJECT.search(raw)
        if obj_match:
            try:
                return json.loads(obj_match.group(1)), "bare_object"
            except json.JSONDecodeError:
                pass

        # Try extracting bare array
        arr_match = cls.BARE_ARRAY.search(raw)
        if arr_match:
            try:
                return json.loads(arr_match.group(1)), "bare_array"
            except json.JSONDecodeError:
                pass

        return None, "failed"
```

## Solution 3: Output Schema Validator

```python
from typing import Any, Dict, List, Tuple


class OutputSchemaValidator:
    """
    Validates and coerces a parsed JSON object against an OutputSchema.
    Performs type coercion for common LLM mismatches (e.g. "42" -> 42).
    Returns a cleaned object and a list of validation issues.
    """

    def validate(
        self,
        data: Dict[str, Any],
        schema: OutputSchema,
    ) -> Tuple[Dict[str, Any], List[str]]:
        issues: List[str] = []
        result: Dict[str, Any] = {}

        for field_schema in schema.fields:
            name = field_schema.name
            if name not in data:
                if field_schema.required:
                    issues.append(f"required field '{name}' is missing")
                else:
                    result[name] = field_schema.default
                continue

            value = data[name]
            coerced, issue = self._coerce(value, field_schema)
            if issue:
                issues.append(issue)
            else:
                result[name] = coerced

        if not schema.allow_extra_fields:
            schema_names = {f.name for f in schema.fields}
            stripped = [k for k in data if k not in schema_names]
            if stripped:
                issues.append(f"extra fields stripped: {stripped}")

        return result, issues

    def _coerce(self, value: Any, fs: OutputFieldSchema) -> Tuple[Any, str]:
        ft = fs.field_type
        try:
            if ft == OutputFieldType.STRING:
                coerced = str(value) if not isinstance(value, str) else value
                if fs.min_length and len(coerced) < fs.min_length:
                    return None, f"'{fs.name}' too short (min {fs.min_length})"
                if fs.max_length and len(coerced) > fs.max_length:
                    coerced = coerced[:fs.max_length]
                if fs.allowed_values and coerced not in fs.allowed_values:
                    return None, f"'{fs.name}' not in allowed values"
                return coerced, ""
            elif ft == OutputFieldType.INTEGER:
                coerced = int(float(value)) if not isinstance(value, int) else value
                if fs.min_value is not None and coerced < fs.min_value:
                    return None, f"'{fs.name}' below minimum {fs.min_value}"
                if fs.max_value is not None and coerced > fs.max_value:
                    return None, f"'{fs.name}' above maximum {fs.max_value}"
                return coerced, ""
            elif ft == OutputFieldType.FLOAT:
                coerced = float(value)
                return coerced, ""
            elif ft == OutputFieldType.BOOLEAN:
                if isinstance(value, str):
                    coerced = value.lower() in ("true", "yes", "1")
                else:
                    coerced = bool(value)
                return coerced, ""
            elif ft == OutputFieldType.NULLABLE_STRING:
                return (str(value) if value is not None else None), ""
            elif ft == OutputFieldType.LIST:
                return (value if isinstance(value, list) else [value]), ""
            elif ft == OutputFieldType.OBJECT:
                if not isinstance(value, dict):
                    return None, f"'{fs.name}' must be an object"
                if fs.nested_schema:
                    nested, nested_issues = OutputSchemaValidator().validate(value, fs.nested_schema)
                    if nested_issues:
                        return None, f"nested '{fs.name}': {'; '.join(nested_issues)}"
                    return nested, ""
                return value, ""
        except (ValueError, TypeError) as exc:
            return None, f"'{fs.name}' coercion failed: {exc}"
        return value, ""
```

## Solution 4: Schema-Enforcing Output Parser

```python
from typing import Any, Callable, Dict, Optional


class SchemaEnforcingOutputParser:
    """
    Combines JSON extraction and schema validation into a single pipeline.
    Provides a repair-and-retry path: if validation fails, a repair prompt
    is constructed and the LLM is called again with the validation errors.
    """

    def __init__(
        self,
        extractor: LLMOutputJSONExtractor,
        validator: OutputSchemaValidator,
        schema: OutputSchema,
        max_repair_attempts: int = 1,
    ):
        self._extractor = extractor
        self._validator = validator
        self._schema = schema
        self._max_repair = max_repair_attempts

    async def parse(
        self,
        raw_output: str,
        repair_fn: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """
        Returns validated, coerced data dict.
        Raises ValueError if parsing fails after repair attempts.
        """
        data, method = self._extractor.extract(raw_output)
        if data is None:
            raise ValueError(f"Could not extract JSON from LLM output (tried all methods)")

        cleaned, issues = self._validator.validate(data, self._schema)
        critical = [i for i in issues if "required field" in i or "coercion failed" in i]

        if not critical:
            return cleaned  # warnings only — proceed

        if repair_fn and self._max_repair > 0:
            repair_prompt = (
                f"Your previous response had validation errors:\n"
                + "\n".join(f"- {i}" for i in critical)
                + f"\n\nPlease return a valid JSON object conforming to schema '{self._schema.name}'. "
                  f"Output only JSON with no additional text."
            )
            repaired_raw = await repair_fn(repair_prompt)
            repaired_data, _ = self._extractor.extract(repaired_raw)
            if repaired_data:
                repaired_cleaned, repaired_issues = self._validator.validate(repaired_data, self._schema)
                if not [i for i in repaired_issues if "required field" in i]:
                    return repaired_cleaned

        raise ValueError(
            f"Schema validation failed for '{self._schema.name}': {'; '.join(critical)}"
        )
```

## Solution 5: Output Validation Audit Logger

```python
import time
from typing import List


class OutputValidationAuditLogger:
    """
    Records schema validation outcomes for quality monitoring.
    Surfaces which schemas and which field types fail most often.
    """

    def __init__(self, max_records: int = 20000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        schema_name: str,
        issues: List[str],
        extraction_method: str,
        repaired: bool = False,
        session_id: str = "",
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "schema_name": schema_name,
            "issue_count": len(issues),
            "issues": issues[:5],
            "extraction_method": extraction_method,
            "repaired": repaired,
            "session_id": session_id,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "validations": 0}
        failures = [r for r in recent if r["issue_count"] > 0]
        repaired = [r for r in recent if r["repaired"]]
        by_schema: dict = {}
        for r in recent:
            by_schema[r["schema_name"]] = by_schema.get(r["schema_name"], 0) + (1 if r["issue_count"] > 0 else 0)
        return {
            "window_seconds": window_seconds,
            "validations": len(recent),
            "failures": len(failures),
            "failure_rate": round(len(failures) / max(len(recent), 1), 4),
            "repaired": len(repaired),
            "by_schema": by_schema,
        }
```

## Solution 6: Schema Enforcement Dashboard

```python
import time


class SchemaEnforcementDashboard:
    """
    Combines validation audit summary with schema registry inventory.
    """

    def __init__(
        self,
        audit_logger: OutputValidationAuditLogger,
        schemas: List[OutputSchema],
    ):
        self._audit = audit_logger
        self._schemas = schemas

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "registered_schemas": [
                {"name": s.name, "version": s.version, "field_count": len(s.fields)}
                for s in self._schemas
            ],
            "validation_audit_last_hour": self._audit.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | JSON Extraction | Schema Validation | Type Coercion | Repair Retry | Audit Log |
|---|---|---|---|---|---|
| LLMOutputJSONExtractor | Yes (4 strategies) | No | No | No | No |
| OutputSchemaValidator | No | Yes | Yes (6 types) | No | No |
| SchemaEnforcingOutputParser | Via extractor | Via validator | Via validator | Yes | No |
| OutputValidationAuditLogger | No | No | No | No | Yes |
| SchemaEnforcementDashboard | No | No | No | No | Via logger |

**Best for production**: Always strip markdown fences before JSON parsing — the LLM will produce them even when told not to, especially for long JSON objects. Set `allow_extra_fields=False` to prevent hallucinated keys from propagating downstream; the stripped fields are logged as warnings rather than errors. Use `max_repair_attempts=1` — more than one repair loop adds latency without proportional improvement; if the second attempt also fails, the schema prompt needs to be improved rather than repaired at runtime. Monitor `failure_rate` by schema name: a schema with >10% failure rate needs a clearer system prompt with a concrete output example.
