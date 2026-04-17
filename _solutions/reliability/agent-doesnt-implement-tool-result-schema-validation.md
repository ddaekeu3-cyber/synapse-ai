---
title: "Agent Doesn't Implement Tool Result Schema Validation"
description: "Agents that inject raw tool results into the LLM context without schema validation are vulnerable to silent data corruption: a tool that returns a malformed response causes the agent to reason over garbage, producing confident but wrong answers. Implement tool result schema validation using JSON Schema or dataclass validators that reject malformed results before they enter the reasoning context, with fallback to a structured error message."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-result-schema-validation
tags: [schema-validation, tool-results, data-integrity, json-schema, context-injection, defensive-parsing]
symptoms:
  - "Agent confidently answers based on a tool result that returned HTML instead of JSON"
  - "Missing required fields in tool responses cause KeyError downstream rather than a graceful error"
  - "No distinction between a tool that returned an empty result and one that returned a malformed result"
  - "Tool result type changes after an API update go undetected until the agent produces wrong answers"
  - "LLM receives None or an exception traceback as a tool result with no error handling"
---

## Why This Happens

Tools are external integrations that can return unexpected data at any time: the upstream API changes its response format, the network returns an HTTP error body, or a deserialization bug introduces None where a string is expected. Without a validation layer between tool execution and context injection, any of these failure modes silently corrupts the agent's reasoning context. Schema validation enforces a contract on tool results before they reach the LLM — converting silent corruption into a detectable, handleable error that can be surfaced to the model as a structured tool error rather than injected as garbage.

## Solution 1: Tool Result Schema

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Type, Union


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    LIST = "list"
    DICT = "dict"
    ANY = "any"


@dataclass
class FieldSchema:
    name: str
    field_type: FieldType
    required: bool = True
    nullable: bool = False
    min_length: Optional[int] = None     # for strings and lists
    max_length: Optional[int] = None
    allowed_values: Optional[List[Any]] = None


@dataclass
class ToolResultSchema:
    tool_name: str
    fields: List[FieldSchema] = field(default_factory=list)
    allow_extra_fields: bool = True
    description: str = ""
```

## Solution 2: Schema Validator

```python
from typing import Any, Dict, List, Tuple


class ToolResultSchemaValidator:
    """
    Validates a tool result dict against a ToolResultSchema.
    Returns a list of validation errors (empty = valid).
    """

    TYPE_MAP = {
        FieldType.STRING: str,
        FieldType.INTEGER: int,
        FieldType.FLOAT: (int, float),
        FieldType.BOOLEAN: bool,
        FieldType.LIST: list,
        FieldType.DICT: dict,
    }

    def validate(
        self,
        result: Any,
        schema: ToolResultSchema,
    ) -> List[str]:
        errors = []

        if not isinstance(result, dict):
            return [f"expected dict, got {type(result).__name__}"]

        for field_schema in schema.fields:
            name = field_schema.name

            if name not in result:
                if field_schema.required:
                    errors.append(f"missing required field: '{name}'")
                continue

            value = result[name]

            if value is None:
                if not field_schema.nullable:
                    errors.append(f"field '{name}' is null but not nullable")
                continue

            if field_schema.field_type != FieldType.ANY:
                expected_type = self.TYPE_MAP.get(field_schema.field_type)
                if expected_type and not isinstance(value, expected_type):
                    errors.append(
                        f"field '{name}': expected {field_schema.field_type.value}, "
                        f"got {type(value).__name__}"
                    )
                    continue

            if field_schema.allowed_values is not None:
                if value not in field_schema.allowed_values:
                    errors.append(
                        f"field '{name}': value {value!r} not in allowed values"
                    )

            if isinstance(value, (str, list)):
                if field_schema.min_length is not None and len(value) < field_schema.min_length:
                    errors.append(
                        f"field '{name}': length {len(value)} below minimum {field_schema.min_length}"
                    )
                if field_schema.max_length is not None and len(value) > field_schema.max_length:
                    errors.append(
                        f"field '{name}': length {len(value)} exceeds maximum {field_schema.max_length}"
                    )

        return errors
```

## Solution 3: Validation Result

```python
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class ToolResultValidationOutcome:
    tool_name: str
    valid: bool
    errors: List[str]
    original_result: Any
    sanitized_result: Optional[Any]   # None if validation failed
    validated_at: float = field(default_factory=time.time)

    def as_error_message(self) -> str:
        return (
            f"Tool '{self.tool_name}' returned an invalid result. "
            f"Errors: {'; '.join(self.errors)}. "
            f"The tool result cannot be used."
        )
```

## Solution 4: Validating Tool Result Interceptor

```python
import json
import time
from typing import Any, Callable, Dict, Optional


class ValidatingToolResultInterceptor:
    """
    Intercepts tool execution results, validates them against registered schemas,
    and returns either the validated result or a structured error message
    safe for injection into the LLM context.
    """

    def __init__(
        self,
        validator: ToolResultSchemaValidator,
        schemas: Dict[str, ToolResultSchema],
    ):
        self._validator = validator
        self._schemas = schemas
        self._validation_failures = 0
        self._total_validated = 0

    def register_schema(self, schema: ToolResultSchema) -> None:
        self._schemas[schema.tool_name] = schema

    def intercept(
        self,
        tool_name: str,
        raw_result: Any,
    ) -> ToolResultValidationOutcome:
        self._total_validated += 1
        schema = self._schemas.get(tool_name)

        if schema is None:
            # No schema registered — pass through with a warning
            return ToolResultValidationOutcome(
                tool_name=tool_name,
                valid=True,
                errors=[],
                original_result=raw_result,
                sanitized_result=raw_result,
            )

        # Attempt JSON parsing if result is a string
        parsed_result = raw_result
        if isinstance(raw_result, str):
            try:
                parsed_result = json.loads(raw_result)
            except (json.JSONDecodeError, ValueError):
                self._validation_failures += 1
                return ToolResultValidationOutcome(
                    tool_name=tool_name,
                    valid=False,
                    errors=["result is not valid JSON"],
                    original_result=raw_result,
                    sanitized_result=None,
                )

        errors = self._validator.validate(parsed_result, schema)

        if errors:
            self._validation_failures += 1
            return ToolResultValidationOutcome(
                tool_name=tool_name,
                valid=False,
                errors=errors,
                original_result=raw_result,
                sanitized_result=None,
            )

        return ToolResultValidationOutcome(
            tool_name=tool_name,
            valid=True,
            errors=[],
            original_result=raw_result,
            sanitized_result=parsed_result,
        )

    def failure_rate(self) -> float:
        return round(self._validation_failures / max(self._total_validated, 1), 4)

    def stats(self) -> dict:
        return {
            "total_validated": self._total_validated,
            "failures": self._validation_failures,
            "failure_rate": self.failure_rate(),
        }
```

## Solution 5: Schema Drift Detector

```python
import time
from collections import defaultdict
from typing import Dict, List


class ToolResultSchemaDriftDetector:
    """
    Tracks unexpected fields and missing fields over time to detect
    when a tool's API response format has silently changed.
    """

    def __init__(self):
        self._unexpected_fields: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._missing_fields: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._sample_count: Dict[str, int] = defaultdict(int)

    def observe(
        self,
        tool_name: str,
        result: dict,
        schema: ToolResultSchema,
    ) -> None:
        if not isinstance(result, dict):
            return
        self._sample_count[tool_name] += 1
        schema_fields = {f.name for f in schema.fields}
        result_fields = set(result.keys())

        for field_name in result_fields - schema_fields:
            self._unexpected_fields[tool_name][field_name] += 1

        for field_name in schema_fields - result_fields:
            required_field = any(f.name == field_name and f.required for f in schema.fields)
            if required_field:
                self._missing_fields[tool_name][field_name] += 1

    def drift_report(self, min_occurrence_rate: float = 0.05) -> dict:
        report = {}
        for tool_name in set(self._unexpected_fields) | set(self._missing_fields):
            sample_count = self._sample_count.get(tool_name, 1)
            unexpected = {
                field: count for field, count in self._unexpected_fields[tool_name].items()
                if count / sample_count >= min_occurrence_rate
            }
            missing = {
                field: count for field, count in self._missing_fields[tool_name].items()
                if count / sample_count >= min_occurrence_rate
            }
            if unexpected or missing:
                report[tool_name] = {
                    "sample_count": sample_count,
                    "unexpected_fields": unexpected,
                    "missing_fields": missing,
                }
        return report
```

## Solution 6: Schema Validation Dashboard

```python
import time


class ToolResultValidationDashboard:
    """
    Combines interceptor stats and schema drift detection
    into a single operational report.
    """

    def __init__(
        self,
        interceptor: ValidatingToolResultInterceptor,
        drift_detector: ToolResultSchemaDriftDetector,
    ):
        self._interceptor = interceptor
        self._drift_detector = drift_detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "validation_stats": self._interceptor.stats(),
            "registered_schemas": list(self._interceptor._schemas.keys()),
            "schema_drift": self._drift_detector.drift_report(),
        }
```

## Comparison

| Approach | Field Type Checking | Required Field Check | JSON Parsing | Drift Detection | Dashboard |
|---|---|---|---|---|---|
| ToolResultSchemaValidator | Yes (type + length) | Yes | No | No | No |
| ValidatingToolResultInterceptor | Via validator | Via validator | Yes (str→dict) | No | Stats |
| ToolResultSchemaDriftDetector | No | No | No | Yes (field tracking) | No |
| ToolResultValidationDashboard | No | No | No | Via detector | Yes |

**Best for production**: Register schemas for all tools that interact with external APIs — internal pure-Python tools are lower risk. Use `allow_extra_fields=True` by default to avoid breaking on API additions; let the drift detector surface new fields passively. When validation fails, inject `outcome.as_error_message()` into the tool result slot instead of the raw result — this gives the model clear signal that the tool failed without exposing malformed data to the reasoning context. Monitor `failure_rate()` per tool: a sudden spike indicates an upstream API change that requires schema update.
