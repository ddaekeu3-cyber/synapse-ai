---
title: "Agent Doesn't Implement Tool Output Schema Validation"
description: "Agents that inject tool results directly into the LLM context without schema validation are vulnerable to malformed or adversarially crafted responses from external APIs: an API that returns unexpected field types, injected markup, or oversized payloads can corrupt the agent's reasoning or trigger downstream errors. Implement tool output schema validation that verifies result structure and types before the result reaches the LLM context."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-output-schema-validation
tags: [schema-validation, tool-output, output-integrity, api-response-validation, type-checking, malformed-response]
symptoms:
  - "Unexpected field types from external APIs cause downstream type errors in tool result processing"
  - "Oversized string fields in tool results fill the context window and crowd out system instructions"
  - "Tool results containing unexpected keys are injected into context verbatim without sanitization"
  - "An API returning HTML error pages instead of JSON causes the agent to reason about error markup"
  - "No validation layer between raw API response and LLM context injection"
---

## Why This Happens

Tool implementations call external APIs and return raw results. The agent framework takes those results and serializes them into the LLM context. When an external API returns an unexpected structure — due to a backend error, a version change, or an adversarial response — the raw payload lands in the context without any check. Schema validation adds a gate between the tool's raw return value and context injection: it verifies that required fields are present, field types match expectations, string lengths are within bounds, and no unexpected fields have been added. Validation failures are handled as tool errors rather than being passed to the LLM as apparent valid results.

## Solution 1: Output Field Schema

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Type


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    LIST = "list"
    DICT = "dict"
    NULL = "null"
    ANY = "any"


@dataclass
class OutputFieldSchema:
    name: str
    field_type: FieldType
    required: bool = True
    max_length: Optional[int] = None       # for strings
    max_items: Optional[int] = None        # for lists
    allowed_values: Optional[List[Any]] = None
    nested_schema: Optional[List["OutputFieldSchema"]] = None
    description: str = ""

    def python_type(self) -> Optional[Type]:
        mapping = {
            FieldType.STRING: str,
            FieldType.INTEGER: int,
            FieldType.FLOAT: float,
            FieldType.BOOLEAN: bool,
            FieldType.LIST: list,
            FieldType.DICT: dict,
        }
        return mapping.get(self.field_type)
```

## Solution 2: Tool Output Schema

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ToolOutputSchema:
    tool_name: str
    fields: List[OutputFieldSchema]
    allow_extra_fields: bool = False
    max_total_chars: int = 50_000
    description: str = ""

    def field_map(self) -> dict:
        return {f.name: f for f in self.fields}
```

## Solution 3: Schema Validator

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ValidationError:
    field_path: str
    error_type: str
    message: str


@dataclass
class ValidationResult:
    valid: bool
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    sanitized_output: Any = None

    def add_error(self, path: str, error_type: str, message: str) -> None:
        self.errors.append(ValidationError(path, error_type, message))
        self.valid = False


class ToolOutputSchemaValidator:
    """
    Validates a tool result against its declared output schema.
    Returns a ValidationResult with errors and a sanitized copy
    of the output with oversized fields truncated.
    """

    def validate(self, result: Any, schema: ToolOutputSchema) -> ValidationResult:
        vr = ValidationResult(valid=True)

        # Total size check
        total_chars = len(str(result))
        if total_chars > schema.max_total_chars:
            vr.warnings.append(
                f"Output size {total_chars} chars exceeds limit {schema.max_total_chars} — truncation applied"
            )

        if not isinstance(result, dict):
            vr.add_error("$", "type_error", f"Expected dict, got {type(result).__name__}")
            vr.sanitized_output = {}
            return vr

        sanitized = {}
        field_map = schema.field_map()

        # Check required fields
        for fname, fschema in field_map.items():
            if fschema.required and fname not in result:
                vr.add_error(fname, "missing_required", f"Required field '{fname}' is absent")

        # Validate present fields
        for key, value in result.items():
            if key not in field_map:
                if not schema.allow_extra_fields:
                    vr.warnings.append(f"Unexpected field '{key}' dropped")
                    continue
                sanitized[key] = value
                continue

            fschema = field_map[key]
            validated_value = self._validate_field(key, value, fschema, vr)
            if validated_value is not None:
                sanitized[key] = validated_value

        vr.sanitized_output = sanitized
        return vr

    def _validate_field(
        self, path: str, value: Any, schema: OutputFieldSchema, vr: ValidationResult
    ) -> Any:
        if schema.field_type == FieldType.ANY:
            return value

        expected_type = schema.python_type()
        if expected_type and not isinstance(value, expected_type):
            # Attempt coercion for common cases
            try:
                if schema.field_type == FieldType.STRING:
                    value = str(value)
                elif schema.field_type == FieldType.INTEGER:
                    value = int(value)
                elif schema.field_type == FieldType.FLOAT:
                    value = float(value)
                else:
                    vr.add_error(path, "type_error",
                                 f"Expected {schema.field_type.value}, got {type(value).__name__}")
                    return None
            except (ValueError, TypeError):
                vr.add_error(path, "type_error",
                             f"Cannot coerce {type(value).__name__} to {schema.field_type.value}")
                return None

        if schema.field_type == FieldType.STRING and schema.max_length and len(value) > schema.max_length:
            vr.warnings.append(f"Field '{path}' truncated from {len(value)} to {schema.max_length} chars")
            value = value[: schema.max_length]

        if schema.field_type == FieldType.LIST and schema.max_items and len(value) > schema.max_items:
            vr.warnings.append(f"Field '{path}' list truncated from {len(value)} to {schema.max_items} items")
            value = value[: schema.max_items]

        if schema.allowed_values and value not in schema.allowed_values:
            vr.add_error(path, "invalid_value",
                         f"Value '{value}' not in allowed set {schema.allowed_values}")
            return None

        return value
```

## Solution 4: Schema Registry

```python
from typing import Dict, Optional


class ToolOutputSchemaRegistry:
    """
    Stores output schemas per tool name. Tools without a registered
    schema pass through with a warning (permissive mode) or are
    blocked (strict mode).
    """

    def __init__(self, strict_mode: bool = False):
        self._schemas: Dict[str, ToolOutputSchema] = {}
        self._strict = strict_mode

    def register(self, schema: ToolOutputSchema) -> None:
        self._schemas[schema.tool_name] = schema

    def get(self, tool_name: str) -> Optional[ToolOutputSchema]:
        return self._schemas.get(tool_name)

    def is_strict(self) -> bool:
        return self._strict

    def registered_tools(self) -> list:
        return list(self._schemas.keys())
```

## Solution 5: Validation-Gated Result Injector

```python
import time
from typing import Any, Dict, List, Optional


class ValidationGatedResultInjector:
    """
    Validates tool outputs against registered schemas before
    injecting them into the LLM context. Blocks invalid results
    in strict mode; passes with warnings in permissive mode.
    """

    def __init__(
        self,
        registry: ToolOutputSchemaRegistry,
        validator: ToolOutputSchemaValidator,
    ):
        self._registry = registry
        self._validator = validator
        self._validated = 0
        self._blocked = 0
        self._warned = 0
        self._unscheduled = 0

    def process(self, tool_name: str, raw_result: Any) -> dict:
        schema = self._registry.get(tool_name)

        if schema is None:
            self._unscheduled += 1
            if self._registry.is_strict():
                self._blocked += 1
                return {
                    "allowed": False,
                    "result": None,
                    "reason": f"No schema registered for tool '{tool_name}' (strict mode)",
                }
            return {"allowed": True, "result": raw_result, "warnings": ["no_schema"]}

        vr = self._validator.validate(raw_result, schema)
        self._validated += 1

        if not vr.valid:
            self._blocked += 1
            return {
                "allowed": False,
                "result": None,
                "errors": [{"path": e.field_path, "message": e.message} for e in vr.errors],
                "reason": "Schema validation failed",
            }

        if vr.warnings:
            self._warned += 1

        return {
            "allowed": True,
            "result": vr.sanitized_output,
            "warnings": vr.warnings,
        }

    def stats(self) -> dict:
        return {
            "validated": self._validated,
            "blocked": self._blocked,
            "warned": self._warned,
            "unschema_d": self._unscheduled,
            "block_rate": round(self._blocked / max(self._validated, 1), 4),
        }
```

## Solution 6: Schema Validation Dashboard

```python
import time


class ToolOutputSchemaValidationDashboard:
    """
    Combines injector stats, registry coverage, and validation
    health into a single operational report.
    """

    def __init__(
        self,
        injector: ValidationGatedResultInjector,
        registry: ToolOutputSchemaRegistry,
    ):
        self._injector = injector
        self._registry = registry

    def render(self) -> dict:
        stats = self._injector.stats()
        return {
            "generated_at": time.time(),
            "registry": {
                "registered_tools": self._registry.registered_tools(),
                "strict_mode": self._registry.is_strict(),
            },
            "validation_stats": stats,
            "health": {
                "block_rate": stats["block_rate"],
                "unschema_d_results": stats["unschema_d"],
            },
        }
```

## Comparison

| Approach | Field Type Check | Length Enforcement | Required Field Check | Extra Field Drop | Strict/Permissive Mode |
|---|---|---|---|---|---|
| ToolOutputSchemaValidator | Yes (with coercion) | Yes (truncate) | Yes | Via schema flag | No |
| ToolOutputSchemaRegistry | No | No | No | No | Yes |
| ValidationGatedResultInjector | Via validator | Via validator | Via validator | Via validator | Via registry |
| ToolOutputSchemaValidationDashboard | No | No | No | No | No |

**Best for production**: Start with `strict_mode=False` and `allow_extra_fields=True` to collect a baseline of warnings before enforcing schemas — this avoids blocking valid traffic during the initial schema definition period. Set `max_total_chars=50_000` as a hard ceiling regardless of field-level limits: a single oversized API response should never fill the context window. Use `max_length=2000` for description fields and `max_items=50` for list fields as conservative defaults. Promote to `strict_mode=True` after one week of permissive monitoring confirms that legitimate tool results pass validation without errors.
