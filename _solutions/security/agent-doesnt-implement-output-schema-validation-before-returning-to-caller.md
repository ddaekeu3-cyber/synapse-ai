---
title: "Agent Doesn't Implement Output Schema Validation Before Returning to Caller"
description: "Agents that return LLM-generated structured output without schema validation pass malformed JSON, wrong field types, or injected extra keys directly to downstream systems: a billing service receives a negative amount, an email tool receives a recipient list with injected addresses, a database write receives a null primary key. Implement output schema validation that checks every structured response against a declared schema before it leaves the agent boundary."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-schema-validation-before-returning-to-caller
tags: [output-validation, schema-enforcement, structured-output, type-safety, injection-prevention, output-integrity]
symptoms:
  - "LLM returns negative numeric values that downstream billing system accepts without complaint"
  - "Structured output contains extra keys not in the declared schema — injected by prompt manipulation"
  - "Agent returns null for a required field and caller crashes with NullPointerException"
  - "No validation between LLM output parsing and downstream system consumption"
  - "Schema drift: LLM begins returning a renamed field and no alert fires for weeks"
---

## Why This Happens

LLMs are probabilistic. Even with structured output prompting or JSON mode, models occasionally return wrong field types, omit required fields, include undeclared extra fields, or produce values outside declared ranges. When an agent pipes LLM output directly to downstream systems without validation, schema violations become runtime failures or — worse — silent data corruption. Output schema validation must happen at the agent boundary, not inside the downstream consumer, because the agent is the trust boundary: it knows what it asked for and must verify what it got.

## Solution 1: Output Schema Field Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional, Set


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    LIST = "list"
    DICT = "dict"
    NULL = "null"


@dataclass
class OutputFieldDescriptor:
    name: str
    field_type: FieldType
    required: bool = True
    nullable: bool = False
    min_value: Optional[float] = None      # for numeric types
    max_value: Optional[float] = None
    min_length: Optional[int] = None       # for string/list
    max_length: Optional[int] = None
    allowed_values: Optional[Set[Any]] = None  # enum constraint
    custom_validator: Optional[Callable[[Any], bool]] = None
    description: str = ""
```

## Solution 2: Output Schema

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set


@dataclass
class OutputSchema:
    schema_name: str
    fields: List[OutputFieldDescriptor]
    allow_extra_fields: bool = False   # if False, extra keys are a violation
    version: str = "1.0"

    def field_map(self) -> Dict[str, OutputFieldDescriptor]:
        return {f.name: f for f in self.fields}

    def required_fields(self) -> Set[str]:
        return {f.name for f in self.fields if f.required}
```

## Solution 3: Output Schema Validator

```python
from typing import Any, Dict, List


class SchemaViolation:
    def __init__(self, field_name: str, violation_type: str, detail: str):
        self.field_name = field_name
        self.violation_type = violation_type
        self.detail = detail

    def __repr__(self) -> str:
        return f"SchemaViolation({self.field_name!r}: {self.violation_type} — {self.detail})"


class OutputSchemaValidator:
    """
    Validates a parsed output dict against an OutputSchema.
    Returns a list of SchemaViolation objects (empty = valid).
    """

    _TYPE_MAP = {
        FieldType.STRING: str,
        FieldType.INTEGER: int,
        FieldType.FLOAT: (int, float),
        FieldType.BOOLEAN: bool,
        FieldType.LIST: list,
        FieldType.DICT: dict,
    }

    def validate(
        self,
        output: Dict[str, Any],
        schema: OutputSchema,
    ) -> List[SchemaViolation]:
        violations = []
        field_map = schema.field_map()

        # Check required fields
        for field_name in schema.required_fields():
            if field_name not in output:
                violations.append(SchemaViolation(
                    field_name, "missing_required",
                    f"Required field '{field_name}' absent from output",
                ))

        # Check extra fields
        if not schema.allow_extra_fields:
            for key in output:
                if key not in field_map:
                    violations.append(SchemaViolation(
                        key, "extra_field",
                        f"Field '{key}' not declared in schema '{schema.schema_name}'",
                    ))

        # Validate present fields
        for key, value in output.items():
            if key not in field_map:
                continue
            desc = field_map[key]

            if value is None:
                if not desc.nullable:
                    violations.append(SchemaViolation(
                        key, "null_not_allowed",
                        f"Field '{key}' is null but nullable=False",
                    ))
                continue

            # Type check
            expected = self._TYPE_MAP.get(desc.field_type)
            if expected and not isinstance(value, expected):
                violations.append(SchemaViolation(
                    key, "wrong_type",
                    f"Expected {desc.field_type.value}, got {type(value).__name__}",
                ))
                continue

            # Range checks
            if desc.min_value is not None and isinstance(value, (int, float)):
                if value < desc.min_value:
                    violations.append(SchemaViolation(
                        key, "below_minimum",
                        f"Value {value} < min {desc.min_value}",
                    ))
            if desc.max_value is not None and isinstance(value, (int, float)):
                if value > desc.max_value:
                    violations.append(SchemaViolation(
                        key, "above_maximum",
                        f"Value {value} > max {desc.max_value}",
                    ))

            # Length checks
            if desc.min_length is not None and hasattr(value, "__len__"):
                if len(value) < desc.min_length:
                    violations.append(SchemaViolation(
                        key, "too_short",
                        f"Length {len(value)} < min {desc.min_length}",
                    ))
            if desc.max_length is not None and hasattr(value, "__len__"):
                if len(value) > desc.max_length:
                    violations.append(SchemaViolation(
                        key, "too_long",
                        f"Length {len(value)} > max {desc.max_length}",
                    ))

            # Enum constraint
            if desc.allowed_values is not None and value not in desc.allowed_values:
                violations.append(SchemaViolation(
                    key, "invalid_enum_value",
                    f"Value {value!r} not in allowed set {desc.allowed_values}",
                ))

            # Custom validator
            if desc.custom_validator is not None:
                try:
                    if not desc.custom_validator(value):
                        violations.append(SchemaViolation(
                            key, "custom_validation_failed",
                            f"Custom validator returned False for value {value!r}",
                        ))
                except Exception as exc:
                    violations.append(SchemaViolation(
                        key, "custom_validator_error",
                        f"Custom validator raised: {exc}",
                    ))

        return violations
```

## Solution 4: Validated Output Gate

```python
import json
import time
from typing import Any, Callable, Dict, Optional


class OutputSchemaViolationError(Exception):
    def __init__(self, schema_name: str, violations: list):
        super().__init__(
            f"Output failed schema '{schema_name}' with {len(violations)} violation(s): "
            + "; ".join(str(v) for v in violations[:3])
        )
        self.schema_name = schema_name
        self.violations = violations


class ValidatedOutputGate:
    """
    Validates structured agent output before returning to caller.
    Raises OutputSchemaViolationError on violation, or calls a
    custom handler that can repair or log the violation.
    """

    def __init__(
        self,
        validator: OutputSchemaValidator,
        on_violation: Optional[Callable[[list, dict], dict]] = None,
    ):
        self._validator = validator
        self._on_violation = on_violation
        self._total_validated = 0
        self._total_violations = 0

    def validate_and_pass(
        self,
        output: Dict[str, Any],
        schema: OutputSchema,
    ) -> Dict[str, Any]:
        violations = self._validator.validate(output, schema)
        self._total_validated += 1

        if violations:
            self._total_violations += 1
            if self._on_violation:
                return self._on_violation(violations, output)
            raise OutputSchemaViolationError(schema.schema_name, violations)

        return output

    def stats(self) -> dict:
        return {
            "total_validated": self._total_validated,
            "total_violations": self._total_violations,
            "violation_rate": round(
                self._total_violations / max(self._total_validated, 1), 4
            ),
        }
```

## Solution 5: Output Violation Audit Logger

```python
import time
from threading import Lock
from typing import Any, Dict, List


class OutputViolationAuditLogger:
    """
    Records output schema violations for trend analysis.
    Surfaces which violation types are most common and whether
    violation rate is increasing after model or prompt changes.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[dict] = []
        self._lock = Lock()
        self._max = max_records

    def record(
        self,
        schema_name: str,
        violations: list,
        raw_output: Dict[str, Any],
    ) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "schema_name": schema_name,
                "violation_count": len(violations),
                "violation_types": [v.violation_type for v in violations],
                "fields": [v.field_name for v in violations],
            })
            if len(self._records) > self._max:
                self._records.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]

        type_counts: dict = {}
        field_counts: dict = {}
        for r in recent:
            for vtype in r["violation_types"]:
                type_counts[vtype] = type_counts.get(vtype, 0) + 1
            for field in r["fields"]:
                field_counts[field] = field_counts.get(field, 0) + 1

        return {
            "window_seconds": window_seconds,
            "violations": len(recent),
            "by_type": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
            "by_field": dict(sorted(field_counts.items(), key=lambda x: -x[1])),
        }
```

## Solution 6: Auto-Repair Output Sanitizer

```python
from typing import Any, Dict, List


class AutoRepairOutputSanitizer:
    """
    Attempts to repair common output schema violations automatically:
    - Casts string integers to int
    - Strips extra fields when allow_extra_fields=False
    - Replaces null required fields with a declared fallback
    """

    def repair(
        self,
        output: Dict[str, Any],
        schema: OutputSchema,
        violations: List[SchemaViolation],
    ) -> Dict[str, Any]:
        repaired = dict(output)
        field_map = schema.field_map()

        for v in violations:
            if v.violation_type == "extra_field":
                repaired.pop(v.field_name, None)

            elif v.violation_type == "wrong_type":
                desc = field_map.get(v.field_name)
                val = repaired.get(v.field_name)
                if desc and val is not None:
                    try:
                        if desc.field_type == FieldType.INTEGER:
                            repaired[v.field_name] = int(val)
                        elif desc.field_type == FieldType.FLOAT:
                            repaired[v.field_name] = float(val)
                        elif desc.field_type == FieldType.STRING:
                            repaired[v.field_name] = str(val)
                        elif desc.field_type == FieldType.BOOLEAN:
                            repaired[v.field_name] = bool(val)
                    except (ValueError, TypeError):
                        pass

            elif v.violation_type in ("below_minimum", "above_maximum"):
                desc = field_map.get(v.field_name)
                val = repaired.get(v.field_name)
                if desc and val is not None:
                    if desc.min_value is not None:
                        val = max(val, desc.min_value)
                    if desc.max_value is not None:
                        val = min(val, desc.max_value)
                    repaired[v.field_name] = val

        return repaired
```

## Comparison

| Approach | Type Checking | Range Validation | Extra Field Detection | Auto-Repair | Audit Log |
|---|---|---|---|---|---|
| OutputSchemaValidator | Yes | Yes | Yes | No | No |
| ValidatedOutputGate | Via validator | Via validator | Via validator | Via handler | No |
| AutoRepairOutputSanitizer | No | Yes (clamp) | Yes (strip) | Yes | No |
| OutputViolationAuditLogger | No | No | No | No | Yes |

**Best for production**: Set `allow_extra_fields=False` on all schemas — extra fields from LLM output are the primary injection vector (a prompt manipulation may cause the model to add an `admin: true` field that a downstream consumer silently accepts). Never auto-repair in production without logging the original violation; use `AutoRepairOutputSanitizer` only as a fallback that always writes to `OutputViolationAuditLogger` first. Monitor `violation_rate` via `ValidatedOutputGate.stats()`: a spike after a model upgrade or prompt change means the new configuration produces structurally incompatible output. Alert on `by_type["missing_required"]` — a missing required field is almost always a prompt regression, not model noise.
