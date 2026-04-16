---
title: "Agent Doesn't Implement Input Validation Pipeline for Structured Data"
description: "Agents that pass structured inputs — JSON bodies, tool arguments, form fields — directly to downstream logic without a validation pipeline allow malformed types, missing required fields, oversized values, and injection payloads to reach model context or tool execution. Implement a composable validation pipeline with schema enforcement, constraint checking, normalization, and rejection logging at the agent's entry boundary."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-input-validation-pipeline-for-structured-data
tags: [input-validation, schema-enforcement, data-sanitization, pipeline, injection-prevention, boundary-security]
symptoms:
  - "TypeError or KeyError inside tool execution because a required field was absent"
  - "SQL injection or path traversal reaches a tool because string fields were not checked"
  - "Oversized string fields bloat the context window and inflate token cost"
  - "Numeric fields outside valid ranges cause downstream arithmetic errors"
  - "No audit trail of which requests were rejected and why"
---

## Why This Happens

Validation is skipped because frameworks like FastAPI or Pydantic give a false sense of safety: they parse the structure but don't enforce semantic constraints (value ranges, string patterns, cross-field dependencies). Tool call arguments in particular arrive as untyped JSON dictionaries that the agent passes directly to tool functions. A validation pipeline adds a mandatory gate before any tool receives arguments: each field is type-checked, range-checked, pattern-matched, cross-field-validated, and normalized before the downstream code ever sees it.

## Solution 1: Field Constraint

```python
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Pattern, Set, Union


@dataclass
class FieldConstraint:
    """
    Declares constraints for a single field in a structured input.
    """
    name: str
    required: bool = True
    expected_type: type = str
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    min_value: Optional[Union[int, float]] = None
    max_value: Optional[Union[int, float]] = None
    pattern: Optional[str] = None          # regex pattern
    allowed_values: Optional[Set] = None
    strip_whitespace: bool = True
    custom_validators: List[Callable[[Any], Optional[str]]] = field(
        default_factory=list
    )   # return error message or None

    def __post_init__(self):
        self._compiled_pattern = re.compile(self.pattern) if self.pattern else None
```

## Solution 2: Single-Field Validator

```python
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class FieldValidationResult:
    field_name: str
    valid: bool
    normalized_value: Any
    errors: List[str]


class FieldValidator:
    """
    Applies a FieldConstraint to a single value.
    Returns a normalized value and a list of validation errors.
    """

    def validate(
        self, constraint: FieldConstraint, value: Any
    ) -> FieldValidationResult:
        errors: List[str] = []
        normalized = value

        # Presence check
        if value is None or value == "":
            if constraint.required:
                errors.append(f"'{constraint.name}' is required")
            return FieldValidationResult(
                field_name=constraint.name,
                valid=not errors,
                normalized_value=None,
                errors=errors,
            )

        # Type coercion / check
        try:
            if constraint.expected_type == int and not isinstance(value, bool):
                normalized = int(value)
            elif constraint.expected_type == float:
                normalized = float(value)
            elif constraint.expected_type == str:
                normalized = str(value)
                if constraint.strip_whitespace:
                    normalized = normalized.strip()
            elif not isinstance(value, constraint.expected_type):
                errors.append(
                    f"'{constraint.name}' expected {constraint.expected_type.__name__}, "
                    f"got {type(value).__name__}"
                )
        except (ValueError, TypeError):
            errors.append(
                f"'{constraint.name}' cannot be coerced to {constraint.expected_type.__name__}"
            )

        if errors:
            return FieldValidationResult(
                field_name=constraint.name,
                valid=False,
                normalized_value=None,
                errors=errors,
            )

        # Length checks
        if constraint.min_length is not None and hasattr(normalized, "__len__"):
            if len(normalized) < constraint.min_length:
                errors.append(
                    f"'{constraint.name}' too short: {len(normalized)} < {constraint.min_length}"
                )
        if constraint.max_length is not None and hasattr(normalized, "__len__"):
            if len(normalized) > constraint.max_length:
                errors.append(
                    f"'{constraint.name}' too long: {len(normalized)} > {constraint.max_length}"
                )

        # Range checks
        if constraint.min_value is not None and isinstance(normalized, (int, float)):
            if normalized < constraint.min_value:
                errors.append(
                    f"'{constraint.name}' below minimum: {normalized} < {constraint.min_value}"
                )
        if constraint.max_value is not None and isinstance(normalized, (int, float)):
            if normalized > constraint.max_value:
                errors.append(
                    f"'{constraint.name}' above maximum: {normalized} > {constraint.max_value}"
                )

        # Pattern check
        if constraint._compiled_pattern and isinstance(normalized, str):
            if not constraint._compiled_pattern.fullmatch(normalized):
                errors.append(
                    f"'{constraint.name}' does not match pattern '{constraint.pattern}'"
                )

        # Allowed values
        if constraint.allowed_values is not None and normalized not in constraint.allowed_values:
            errors.append(
                f"'{constraint.name}' value '{normalized}' not in allowed set"
            )

        # Custom validators
        for fn in constraint.custom_validators:
            msg = fn(normalized)
            if msg:
                errors.append(f"'{constraint.name}': {msg}")

        return FieldValidationResult(
            field_name=constraint.name,
            valid=not errors,
            normalized_value=normalized if not errors else value,
            errors=errors,
        )
```

## Solution 3: Structured Input Schema

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CrossFieldRule:
    """Validates a condition that spans multiple fields."""
    description: str
    check: Any   # Callable[[Dict[str, Any]], Optional[str]]


@dataclass
class StructuredInputSchema:
    """
    Declares the full validation contract for a structured input.
    Fields, cross-field rules, and unknown-field policy.
    """
    name: str
    fields: List[FieldConstraint]
    cross_field_rules: List[CrossFieldRule] = field(default_factory=list)
    reject_unknown_fields: bool = True
    max_total_fields: int = 50

    def field_names(self) -> set:
        return {f.name for f in self.fields}


@dataclass
class SchemaValidationResult:
    valid: bool
    normalized: Dict[str, Any]
    field_errors: Dict[str, List[str]]
    cross_field_errors: List[str]
    unknown_fields: List[str]

    @property
    def all_errors(self) -> List[str]:
        errs = []
        for field_name, msgs in self.field_errors.items():
            errs.extend(msgs)
        errs.extend(self.cross_field_errors)
        return errs
```

## Solution 4: Validation Pipeline Stage

```python
from typing import Any, Dict, List


class SchemaValidationStage:
    """
    One stage in the validation pipeline: applies a StructuredInputSchema
    to a raw dict and produces a SchemaValidationResult.
    """

    def __init__(self, field_validator: FieldValidator):
        self._fv = field_validator

    def validate(
        self, raw: Dict[str, Any], schema: StructuredInputSchema
    ) -> SchemaValidationResult:
        if len(raw) > schema.max_total_fields:
            return SchemaValidationResult(
                valid=False,
                normalized={},
                field_errors={"__root__": [f"too many fields: {len(raw)} > {schema.max_total_fields}"]},
                cross_field_errors=[],
                unknown_fields=[],
            )

        # Unknown fields
        known = schema.field_names()
        unknown = [k for k in raw if k not in known]
        field_errors: Dict[str, List[str]] = {}
        normalized: Dict[str, Any] = {}

        for constraint in schema.fields:
            value = raw.get(constraint.name)
            result = self._fv.validate(constraint, value)
            if result.errors:
                field_errors[constraint.name] = result.errors
            else:
                normalized[constraint.name] = result.normalized_value

        # Cross-field rules (only if individual fields passed)
        cross_errors: List[str] = []
        if not field_errors:
            for rule in schema.cross_field_rules:
                msg = rule.check(normalized)
                if msg:
                    cross_errors.append(msg)

        if schema.reject_unknown_fields and unknown:
            field_errors["__unknown__"] = [f"unexpected fields: {unknown}"]

        valid = not field_errors and not cross_errors
        return SchemaValidationResult(
            valid=valid,
            normalized=normalized,
            field_errors=field_errors,
            cross_field_errors=cross_errors,
            unknown_fields=unknown,
        )
```

## Solution 5: Validation Rejection Logger

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ValidationRejectionEvent:
    schema_name: str
    source: str               # "tool_call" | "webhook" | "user_input"
    errors: List[str]
    raw_field_count: int
    timestamp: float = field(default_factory=time.time)


class ValidationRejectionLogger:
    """
    Append-only log of validation rejections.
    Surfaces: which schemas reject most often, which fields fail most,
    and whether rejection rate is trending up (possible attack pattern).
    """

    def __init__(self, max_events: int = 10_000):
        self._events: List[ValidationRejectionEvent] = []
        self._max = max_events

    def log(self, event: ValidationRejectionEvent) -> None:
        if len(self._events) >= self._max:
            self._events.pop(0)
        self._events.append(event)

    def recent(self, hours: float = 1.0) -> List[ValidationRejectionEvent]:
        cutoff = time.time() - hours * 3600
        return [e for e in self._events if e.timestamp >= cutoff]

    def summary(self) -> dict:
        recent = self.recent(1.0)
        schema_counts: Dict[str, int] = {}
        field_counts: Dict[str, int] = {}
        for e in recent:
            schema_counts[e.schema_name] = schema_counts.get(e.schema_name, 0) + 1
            for err in e.errors:
                key = err.split(":")[0]
                field_counts[key] = field_counts.get(key, 0) + 1
        return {
            "rejections_last_hour": len(recent),
            "by_schema": schema_counts,
            "top_failing_fields": sorted(field_counts, key=field_counts.get, reverse=True)[:5],
        }
```

## Solution 6: Input Validation Gateway

```python
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ValidationOutcome:
    accepted: bool
    normalized: Optional[Dict[str, Any]]
    errors: Optional[List[str]]
    schema_name: str


class InputValidationGateway:
    """
    Single entry point for all structured input validation.
    Combines schema validation, rejection logging, and a clean
    accept/reject outcome for callers.
    """

    def __init__(
        self,
        stage: SchemaValidationStage,
        logger: ValidationRejectionLogger,
    ):
        self._stage = stage
        self._logger = logger

    def validate(
        self,
        raw: Dict[str, Any],
        schema: StructuredInputSchema,
        source: str = "unknown",
    ) -> ValidationOutcome:
        result = self._stage.validate(raw, schema)

        if not result.valid:
            self._logger.log(
                ValidationRejectionEvent(
                    schema_name=schema.name,
                    source=source,
                    errors=result.all_errors,
                    raw_field_count=len(raw),
                )
            )
            return ValidationOutcome(
                accepted=False,
                normalized=None,
                errors=result.all_errors,
                schema_name=schema.name,
            )

        return ValidationOutcome(
            accepted=True,
            normalized=result.normalized,
            errors=None,
            schema_name=schema.name,
        )
```

## Comparison

| Approach | Field Constraints | Type Coercion | Cross-Field Rules | Rejection Logging |
|---|---|---|---|---|
| FieldValidator | Yes | Yes | No | No |
| SchemaValidationStage | Via field validator | Via field validator | Yes | No |
| ValidationRejectionLogger | No | No | No | Yes |
| InputValidationGateway | Via stage | Via stage | Via stage | Yes |

**Best for production**: Define a `StructuredInputSchema` for every tool in your tool registry — not just user-facing endpoints. Tool call arguments are the most common source of type errors in agent pipelines and are almost never validated. Use `CrossFieldRule` for logical constraints like `end_date > start_date` or `limit <= max_limit`. Route all structured inputs through `InputValidationGateway.validate()` before any downstream processing. Review `ValidationRejectionLogger.summary()` hourly — a spike in rejections for a specific field often indicates a prompt injection attempt or a client sending malformed data after an API change.
