---
title: "Agent Doesn't Implement Tool Argument Type Validation Before Execution"
description: "Agents that pass LLM-generated tool arguments directly to tool functions without type validation are vulnerable to type confusion attacks and silent data corruption: the model may generate a string where an integer is expected, a negative number for a quantity field, or an arbitrary path string for a file parameter — all of which the tool processes without complaint until an unexpected side effect occurs. Implement strict type validation of tool arguments before execution using schema-based validators that reject malformed arguments before they reach tool code."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-argument-type-validation-before-execution
tags: [argument-validation, type-checking, schema-validation, input-sanitization, tool-security, llm-output-validation]
symptoms:
  - "Tool functions receive wrong types from LLM-generated arguments without error"
  - "Negative or out-of-range numeric arguments reach business logic without validation"
  - "Path traversal strings reach file tools because argument format is not validated"
  - "Tool crashes with TypeError deep in business logic rather than at the argument boundary"
  - "No argument schema is defined — tools accept any dict from the model"
---

## Why This Happens

LLMs generate tool arguments as text that is parsed into a dict. The model may generate syntactically valid JSON that is semantically wrong: `"count": "five"` instead of `"count": 5`, `"path": "../../../etc/passwd"` instead of a relative path, or `"amount": -100` instead of a positive payment amount. Without a validation layer between the model output and the tool function, these values reach application logic that may process them without type checking, causing silent corruption, unexpected behavior, or security vulnerabilities. Schema-based validation at the tool dispatch boundary rejects invalid arguments before any tool code executes.

## Solution 1: Argument Schema

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union


class ArgType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    LIST = "list"
    DICT = "dict"
    ENUM = "enum"
    PATH = "path"              # string with path traversal prevention
    URL = "url"                # string matching URL pattern
    EMAIL = "email"


@dataclass
class ArgSchema:
    name: str
    arg_type: ArgType
    required: bool = True
    min_value: Optional[float] = None       # for numeric types
    max_value: Optional[float] = None
    min_length: Optional[int] = None        # for string/list types
    max_length: Optional[int] = None
    pattern: Optional[str] = None          # regex for string validation
    allowed_values: Optional[List[Any]] = None   # for enum type
    item_type: Optional[ArgType] = None    # for list type
    default: Optional[Any] = None
    description: str = ""


@dataclass
class ToolArgumentSchema:
    tool_name: str
    args: List[ArgSchema] = field(default_factory=list)
    allow_extra_fields: bool = False

    def arg(self, name: str) -> Optional[ArgSchema]:
        return next((a for a in self.args if a.name == name), None)
```

## Solution 2: Type Coercer

```python
from typing import Any, Optional


class TypeCoercer:
    """
    Attempts to coerce LLM-generated values to the declared type.
    Raises ValueError when coercion is not possible.
    """

    @staticmethod
    def coerce(value: Any, arg_type: ArgType) -> Any:
        if value is None:
            return None

        if arg_type == ArgType.INTEGER:
            if isinstance(value, bool):
                raise ValueError(f"expected integer, got boolean")
            try:
                coerced = int(value)
                if isinstance(value, float) and value != coerced:
                    raise ValueError(f"float {value} has fractional part, cannot coerce to integer")
                return coerced
            except (TypeError, ValueError):
                raise ValueError(f"cannot coerce '{value}' to integer")

        if arg_type == ArgType.FLOAT:
            try:
                return float(value)
            except (TypeError, ValueError):
                raise ValueError(f"cannot coerce '{value}' to float")

        if arg_type == ArgType.BOOLEAN:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                if value.lower() in ("true", "1", "yes"):
                    return True
                if value.lower() in ("false", "0", "no"):
                    return False
            raise ValueError(f"cannot coerce '{value}' to boolean")

        if arg_type == ArgType.STRING:
            return str(value)

        if arg_type == ArgType.LIST:
            if isinstance(value, list):
                return value
            raise ValueError(f"expected list, got {type(value).__name__}")

        if arg_type == ArgType.DICT:
            if isinstance(value, dict):
                return value
            raise ValueError(f"expected dict, got {type(value).__name__}")

        return value
```

## Solution 3: Argument Validator

```python
import re
from typing import Any, Dict, List, Optional


class ArgumentValidationError(Exception):
    def __init__(self, tool_name: str, errors: List[str]):
        super().__init__(
            f"argument validation failed for '{tool_name}': " + "; ".join(errors)
        )
        self.tool_name = tool_name
        self.errors = errors


class ArgumentValidator:
    """
    Validates a tool argument dict against a ToolArgumentSchema.
    Returns the coerced and validated argument dict on success.
    Raises ArgumentValidationError on any violation.
    """

    PATH_TRAVERSAL_PATTERN = re.compile(r"\.\./|\.\.\\|%2e%2e|%252e")
    URL_PATTERN = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)
    EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

    def __init__(self, coercer: TypeCoercer = None):
        self._coercer = coercer or TypeCoercer()

    def validate(
        self,
        schema: ToolArgumentSchema,
        raw_args: Dict[str, Any],
    ) -> Dict[str, Any]:
        errors = []
        validated = {}

        for arg_schema in schema.args:
            name = arg_schema.name
            value = raw_args.get(name, arg_schema.default)

            if value is None and arg_schema.required:
                errors.append(f"required argument '{name}' is missing")
                continue

            if value is None:
                continue

            try:
                value = self._coercer.coerce(value, arg_schema.arg_type)
            except ValueError as exc:
                errors.append(f"'{name}': {exc}")
                continue

            field_errors = self._validate_constraints(name, value, arg_schema)
            errors.extend(field_errors)
            if not field_errors:
                validated[name] = value

        if not schema.allow_extra_fields:
            declared_names = {a.name for a in schema.args}
            extra = set(raw_args.keys()) - declared_names
            if extra:
                errors.append(f"unexpected fields: {sorted(extra)}")

        if errors:
            raise ArgumentValidationError(schema.tool_name, errors)

        return validated

    def _validate_constraints(
        self,
        name: str,
        value: Any,
        schema: ArgSchema,
    ) -> List[str]:
        errors = []

        if schema.min_value is not None and isinstance(value, (int, float)):
            if value < schema.min_value:
                errors.append(f"'{name}' value {value} < min {schema.min_value}")

        if schema.max_value is not None and isinstance(value, (int, float)):
            if value > schema.max_value:
                errors.append(f"'{name}' value {value} > max {schema.max_value}")

        if schema.min_length is not None and hasattr(value, "__len__"):
            if len(value) < schema.min_length:
                errors.append(f"'{name}' length {len(value)} < min_length {schema.min_length}")

        if schema.max_length is not None and hasattr(value, "__len__"):
            if len(value) > schema.max_length:
                errors.append(f"'{name}' length {len(value)} > max_length {schema.max_length}")

        if schema.pattern and isinstance(value, str):
            if not re.match(schema.pattern, value):
                errors.append(f"'{name}' does not match required pattern")

        if schema.allowed_values is not None and value not in schema.allowed_values:
            errors.append(f"'{name}' value '{value}' not in allowed values {schema.allowed_values}")

        if schema.arg_type == ArgType.PATH and isinstance(value, str):
            if self.PATH_TRAVERSAL_PATTERN.search(value):
                errors.append(f"'{name}' contains path traversal sequence")

        if schema.arg_type == ArgType.URL and isinstance(value, str):
            if not self.URL_PATTERN.match(value):
                errors.append(f"'{name}' is not a valid URL")

        if schema.arg_type == ArgType.EMAIL and isinstance(value, str):
            if not self.EMAIL_PATTERN.match(value):
                errors.append(f"'{name}' is not a valid email address")

        return errors
```

## Solution 4: Validated Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class ValidatedToolDispatcher:
    """
    Validates tool arguments against registered schemas before dispatch.
    Rejects calls with invalid arguments before any tool code executes.
    """

    def __init__(
        self,
        validator: ArgumentValidator,
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._validator = validator
        self._schemas: Dict[str, ToolArgumentSchema] = {}
        self._audit = audit_fn or (lambda ev: None)
        self._validated = 0
        self._rejected = 0

    def register_schema(self, schema: ToolArgumentSchema) -> None:
        self._schemas[schema.tool_name] = schema

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        raw_args: Dict[str, Any],
    ) -> Any:
        schema = self._schemas.get(tool_name)

        if schema is None:
            # No schema registered — pass through with warning
            self._audit({
                "event": "no_schema_registered",
                "tool_name": tool_name,
                "timestamp": time.time(),
            })
            return await tool_fn(**raw_args)

        try:
            validated_args = self._validator.validate(schema, raw_args)
            self._validated += 1
        except ArgumentValidationError as exc:
            self._rejected += 1
            self._audit({
                "event": "argument_validation_failed",
                "tool_name": tool_name,
                "errors": exc.errors,
                "timestamp": time.time(),
            })
            raise

        return await tool_fn(**validated_args)

    def stats(self) -> dict:
        return {
            "validated": self._validated,
            "rejected": self._rejected,
            "rejection_rate": round(self._rejected / max(self._validated + self._rejected, 1), 4),
        }
```

## Solution 5: Schema Registry

```python
from typing import Dict, List, Optional


class ToolSchemaRegistry:
    """
    Central registry of all tool argument schemas.
    Supports bulk registration and schema lookup.
    """

    def __init__(self):
        self._schemas: Dict[str, ToolArgumentSchema] = {}

    def register(self, schema: ToolArgumentSchema) -> None:
        self._schemas[schema.tool_name] = schema

    def register_many(self, schemas: List[ToolArgumentSchema]) -> None:
        for schema in schemas:
            self.register(schema)

    def get(self, tool_name: str) -> Optional[ToolArgumentSchema]:
        return self._schemas.get(tool_name)

    def all_tool_names(self) -> List[str]:
        return sorted(self._schemas.keys())

    def coverage_report(self, registered_tool_names: List[str]) -> dict:
        covered = [t for t in registered_tool_names if t in self._schemas]
        uncovered = [t for t in registered_tool_names if t not in self._schemas]
        return {
            "total_tools": len(registered_tool_names),
            "schema_coverage": len(covered),
            "coverage_pct": round(len(covered) / max(len(registered_tool_names), 1) * 100, 1),
            "uncovered_tools": uncovered,
        }
```

## Solution 6: Validation Audit Dashboard

```python
import time


class ArgumentValidationDashboard:
    """
    Combines dispatcher stats and schema coverage into a security operations view.
    """

    def __init__(
        self,
        dispatcher: ValidatedToolDispatcher,
        registry: ToolSchemaRegistry,
    ):
        self._dispatcher = dispatcher
        self._registry = registry

    def render(self, all_tool_names: list = None) -> dict:
        report = {
            "generated_at": time.time(),
            "dispatcher_stats": self._dispatcher.stats(),
            "registered_schemas": len(self._registry.all_tool_names()),
        }
        if all_tool_names:
            report["coverage"] = self._registry.coverage_report(all_tool_names)
        return report
```

## Comparison

| Approach | Type Coercion | Constraint Checks | Path Traversal Prevention | Schema Registry | Audit |
|---|---|---|---|---|---|
| TypeCoercer | Yes (6 types) | No | No | No | No |
| ArgumentValidator | Via coercer | Yes (min/max/pattern) | Yes | No | No |
| ValidatedToolDispatcher | Via validator | Via validator | Via validator | No | Yes |
| ToolSchemaRegistry | No | No | No | Yes | No |
| ArgumentValidationDashboard | No | No | No | Via registry | Yes |

**Best for production**: Register schemas for every tool before deployment and run `coverage_report()` as a CI check — any tool without a schema is a gap in the security perimeter. Use `ArgType.PATH` for all file path arguments to automatically block path traversal attempts. Alert when `rejection_rate` exceeds 1% for any tool: this pattern indicates either a misbehaving model that consistently generates invalid arguments (requiring prompt engineering) or an active adversarial input trying to find argument formats that bypass validation.
