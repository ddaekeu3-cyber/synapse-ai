---
title: "Agent Doesn't Implement Tool Schema Validation Before Dispatch"
description: "Agents that pass LLM-generated tool call arguments directly to tool functions without validating them against the declared schema allow malformed, out-of-range, or type-incorrect inputs to reach tool execution — causing unexpected behavior, data corruption, or injection vulnerabilities. Implement schema validation that checks every tool call argument set against the declared JSON Schema before dispatch, rejecting malformed calls before they reach the tool function."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-schema-validation-before-dispatch
tags: [schema-validation, tool-dispatch, input-validation, json-schema, llm-output-safety, argument-sanitization]
symptoms:
  - "Tool functions receive unexpected None values or wrong types from LLM-generated calls"
  - "LLM occasionally generates negative values for parameters that only accept positive integers"
  - "No validation between the LLM's tool_use block and the actual function call"
  - "Database tools receive SQL-injectable strings that bypass application-level sanitization"
  - "Tool call with missing required fields reaches execution and raises an unhandled exception"
---

## Why This Happens

LLMs are probabilistic — they sometimes omit required fields, provide wrong types, exceed allowed value ranges, or generate values that look plausible but fail semantic constraints. Tool schemas are defined for the LLM to guide generation, but the LLM is not guaranteed to follow them exactly. Schema validation must happen as an explicit gating step between the LLM's output and tool execution. Without it, every tool function must defensively handle malformed inputs, and when it doesn't, the failure mode is unpredictable.

## Solution 1: Tool Schema Registry

```python
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolSchema:
    tool_name: str
    description: str
    input_schema: Dict[str, Any]    # JSON Schema for the input object
    required_fields: List[str] = field(default_factory=list)
    allow_additional_properties: bool = False

    def __post_init__(self) -> None:
        # Normalize required into the schema if not already there
        if "required" not in self.input_schema and self.required_fields:
            self.input_schema["required"] = self.required_fields
        if "additionalProperties" not in self.input_schema:
            self.input_schema["additionalProperties"] = self.allow_additional_properties


class ToolSchemaRegistry:
    """
    Stores JSON Schema definitions for all registered tools.
    """

    def __init__(self):
        self._schemas: Dict[str, ToolSchema] = {}

    def register(self, schema: ToolSchema) -> None:
        self._schemas[schema.tool_name] = schema

    def get(self, tool_name: str) -> Optional[ToolSchema]:
        return self._schemas.get(tool_name)

    def all_tools(self) -> List[str]:
        return list(self._schemas.keys())
```

## Solution 2: JSON Schema Validator

```python
import re
from typing import Any, Dict, List, Optional, Tuple


class JSONSchemaValidator:
    """
    Minimal JSON Schema validator covering the subset used in tool schemas:
    type, required, properties, enum, minimum, maximum, minLength, maxLength,
    pattern, items (for arrays), and additionalProperties.
    Use jsonschema library in production for full compliance.
    """

    def validate(
        self,
        instance: Any,
        schema: Dict[str, Any],
        path: str = "",
    ) -> List[str]:
        """Returns a list of validation error messages. Empty = valid."""
        errors = []
        schema_type = schema.get("type")

        # Type check
        if schema_type:
            errors.extend(self._check_type(instance, schema_type, path))
            if errors:
                return errors

        # Object validation
        if schema_type == "object" or isinstance(instance, dict):
            errors.extend(self._validate_object(instance, schema, path))

        # Array validation
        if schema_type == "array" or isinstance(instance, list):
            errors.extend(self._validate_array(instance, schema, path))

        # String validation
        if schema_type == "string" or isinstance(instance, str):
            errors.extend(self._validate_string(instance, schema, path))

        # Number validation
        if schema_type in ("number", "integer") or isinstance(instance, (int, float)):
            errors.extend(self._validate_number(instance, schema, path))

        # Enum
        if "enum" in schema and instance not in schema["enum"]:
            errors.append(f"{path}: value {instance!r} not in enum {schema['enum']}")

        return errors

    def _check_type(self, instance: Any, expected: str, path: str) -> List[str]:
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
            "null": type(None),
        }
        expected_type = type_map.get(expected)
        if expected_type and not isinstance(instance, expected_type):
            if not (expected == "integer" and isinstance(instance, bool)):
                return [f"{path}: expected {expected}, got {type(instance).__name__}"]
        return []

    def _validate_object(self, obj: Any, schema: dict, path: str) -> List[str]:
        if not isinstance(obj, dict):
            return []
        errors = []
        for req in schema.get("required", []):
            if req not in obj:
                errors.append(f"{path}.{req}: required field missing")
        if not schema.get("additionalProperties", True):
            allowed = set(schema.get("properties", {}).keys())
            for key in obj:
                if key not in allowed:
                    errors.append(f"{path}.{key}: additional property not allowed")
        for prop, subschema in schema.get("properties", {}).items():
            if prop in obj:
                errors.extend(self.validate(obj[prop], subschema, f"{path}.{prop}"))
        return errors

    def _validate_array(self, arr: Any, schema: dict, path: str) -> List[str]:
        if not isinstance(arr, list):
            return []
        errors = []
        if "minItems" in schema and len(arr) < schema["minItems"]:
            errors.append(f"{path}: array length {len(arr)} < minItems {schema['minItems']}")
        if "maxItems" in schema and len(arr) > schema["maxItems"]:
            errors.append(f"{path}: array length {len(arr)} > maxItems {schema['maxItems']}")
        item_schema = schema.get("items", {})
        for i, item in enumerate(arr):
            errors.extend(self.validate(item, item_schema, f"{path}[{i}]"))
        return errors

    def _validate_string(self, s: Any, schema: dict, path: str) -> List[str]:
        if not isinstance(s, str):
            return []
        errors = []
        if "minLength" in schema and len(s) < schema["minLength"]:
            errors.append(f"{path}: string length {len(s)} < minLength {schema['minLength']}")
        if "maxLength" in schema and len(s) > schema["maxLength"]:
            errors.append(f"{path}: string length {len(s)} > maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], s):
            errors.append(f"{path}: string does not match pattern {schema['pattern']!r}")
        return errors

    def _validate_number(self, n: Any, schema: dict, path: str) -> List[str]:
        if not isinstance(n, (int, float)):
            return []
        errors = []
        if "minimum" in schema and n < schema["minimum"]:
            errors.append(f"{path}: {n} < minimum {schema['minimum']}")
        if "maximum" in schema and n > schema["maximum"]:
            errors.append(f"{path}: {n} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and n <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {n} <= exclusiveMinimum {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and n >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {n} >= exclusiveMaximum {schema['exclusiveMaximum']}")
        return errors
```

## Solution 3: Schema-Validated Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, List


class SchemaValidatedToolDispatcher:
    """
    Validates tool call arguments against the registered schema before dispatch.
    Raises SchemaValidationError on violation; logs all validations for audit.
    """

    def __init__(
        self,
        schema_registry: ToolSchemaRegistry,
        validator: JSONSchemaValidator,
    ):
        self._registry = schema_registry
        self._validator = validator
        self._validation_log: list = []

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        arguments: Dict[str, Any],
    ) -> Any:
        schema = self._registry.get(tool_name)
        if schema is None:
            # No schema registered — dispatch with warning
            self._log(tool_name, arguments, errors=[], schema_found=False)
            return await tool_fn(**arguments)

        errors = self._validator.validate(
            arguments,
            schema.input_schema,
            path=tool_name,
        )
        self._log(tool_name, arguments, errors=errors, schema_found=True)

        if errors:
            raise SchemaValidationError(tool_name=tool_name, errors=errors)

        return await tool_fn(**arguments)

    def _log(
        self,
        tool_name: str,
        arguments: dict,
        errors: List[str],
        schema_found: bool,
    ) -> None:
        self._validation_log.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "schema_found": schema_found,
            "valid": len(errors) == 0,
            "error_count": len(errors),
        })
        if len(self._validation_log) > 10000:
            self._validation_log.pop(0)

    def validation_stats(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._validation_log if r["ts"] >= cutoff]
        invalid = [r for r in recent if not r["valid"]]
        no_schema = [r for r in recent if not r["schema_found"]]
        return {
            "window_seconds": window_seconds,
            "total_dispatches": len(recent),
            "invalid_calls_blocked": len(invalid),
            "calls_without_schema": len(no_schema),
            "validation_failure_rate": round(len(invalid) / max(len(recent), 1), 4),
        }


class SchemaValidationError(Exception):
    def __init__(self, tool_name: str, errors: List[str]):
        super().__init__(
            f"tool '{tool_name}' call rejected — schema validation failed: {errors}"
        )
        self.tool_name = tool_name
        self.errors = errors
```

## Solution 4: Argument Coercer

```python
from typing import Any, Dict, Optional


class ToolArgumentCoercer:
    """
    Attempts to coerce LLM-generated arguments to their expected types
    before validation. Handles common cases like numeric strings,
    boolean strings, and None-instead-of-empty-list.
    """

    def coerce(self, arguments: Dict[str, Any], schema: ToolSchema) -> Dict[str, Any]:
        properties = schema.input_schema.get("properties", {})
        result = dict(arguments)
        for field_name, field_schema in properties.items():
            if field_name not in result:
                continue
            result[field_name] = self._coerce_value(
                result[field_name], field_schema
            )
        return result

    def _coerce_value(self, value: Any, schema: dict) -> Any:
        expected_type = schema.get("type")
        if value is None:
            if expected_type == "array":
                return []
            if expected_type == "string":
                return ""
            return value
        if expected_type == "integer" and isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                pass
        if expected_type == "number" and isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
        if expected_type == "boolean" and isinstance(value, str):
            if value.lower() in ("true", "1", "yes"):
                return True
            if value.lower() in ("false", "0", "no"):
                return False
        return value
```

## Solution 5: Schema Coverage Auditor

```python
from typing import List


class SchemaCoverageAuditor:
    """
    Audits which registered tools have schemas and which do not.
    Identifies tools that are dispatched without validation coverage.
    """

    def __init__(
        self,
        schema_registry: ToolSchemaRegistry,
        dispatcher: SchemaValidatedToolDispatcher,
    ):
        self._registry = schema_registry
        self._dispatcher = dispatcher

    def coverage_report(self) -> dict:
        stats = self._dispatcher.validation_stats()
        registered_tools = set(self._registry.all_tools())
        return {
            "registered_tools_with_schema": len(registered_tools),
            "dispatches_without_schema": stats["calls_without_schema"],
            "tool_names": sorted(registered_tools),
            "validation_failure_rate": stats["validation_failure_rate"],
        }
```

## Solution 6: Schema Validation Dashboard

```python
import time


class SchemaValidationDashboard:
    """
    Combines validation stats and coverage audit into a single report.
    """

    def __init__(
        self,
        dispatcher: SchemaValidatedToolDispatcher,
        auditor: SchemaCoverageAuditor,
    ):
        self._dispatcher = dispatcher
        self._auditor = auditor

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "validation_stats": self._dispatcher.validation_stats(window_seconds),
            "coverage": self._auditor.coverage_report(),
        }
```

## Comparison

| Approach | Schema Storage | Type Checking | Constraint Checking | Coercion | Audit |
|---|---|---|---|---|---|
| ToolSchemaRegistry | Yes | No | No | No | No |
| JSONSchemaValidator | Via schema | Yes | Yes (min/max/pattern) | No | No |
| SchemaValidatedToolDispatcher | Via registry | Via validator | Via validator | No | Yes |
| ToolArgumentCoercer | No | No | No | Yes | No |
| SchemaCoverageAuditor | Via registry | No | No | No | Yes |
| SchemaValidationDashboard | No | No | No | No | Yes |

**Best for production**: Register a schema for every tool at startup — `calls_without_schema` should be zero in production. Use `ToolArgumentCoercer.coerce()` before validation, not instead of it: coercion handles the common case of a numeric string, validation catches the remaining edge cases. When `SchemaValidationError` is raised, return it to the LLM as a `tool_result` with `is_error=true` and the validation errors as the content — the LLM can then self-correct and retry with valid arguments. Set `allow_additional_properties=False` on all schemas to prevent the LLM from hallucinating extra parameters that are silently ignored but could indicate prompt injection attempts probing the tool interface.
