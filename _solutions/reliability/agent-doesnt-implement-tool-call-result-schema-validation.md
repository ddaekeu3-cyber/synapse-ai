---
title: "Agent Doesn't Implement Tool Call Result Schema Validation"
description: "Agents that pass raw tool results directly to the LLM context without schema validation silently inject malformed, truncated, or type-mismatched data into reasoning. When an external API changes its response shape, the agent continues operating on corrupt data with no error, producing confident but wrong answers. Implement result schema validation that catches structural deviations at the tool boundary before they propagate into the context."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-call-result-schema-validation
tags: [schema-validation, tool-results, api-contract, data-integrity, defensive-parsing, result-validation]
symptoms:
  - "API response shape changed silently and agent started producing wrong answers"
  - "Tool results with missing required fields are passed to the LLM without error"
  - "Type mismatches in tool results (string where int expected) go undetected"
  - "No alarm when a tool returns an empty result that should always contain data"
  - "Debugging wrong answers requires tracing back through raw tool outputs manually"
---

## Why This Happens

Tool call results are external data crossing a trust boundary. When the upstream API evolves — adding required fields, renaming keys, changing value types, or returning error payloads with a success HTTP status — an agent without schema validation silently consumes the degraded data. The LLM receives a partial or structurally wrong context and produces plausible-sounding but incorrect answers with no indication that the upstream data was malformed. Schema validation at the tool result boundary converts silent data corruption into an explicit, handleable error before any content reaches the context.

## Solution 1: Result Schema Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type


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
    nullable: bool = False
    min_value: Optional[float] = None       # for numeric types
    max_value: Optional[float] = None
    min_length: Optional[int] = None        # for string/list types
    max_length: Optional[int] = None
    allowed_values: Optional[List[Any]] = None
    nested_schema: Optional["ResultSchema"] = None   # for dict/list of dicts


@dataclass
class ResultSchema:
    tool_name: str
    fields: List[FieldSpec]
    allow_extra_fields: bool = True         # tolerate unknown keys by default
    non_empty_required: bool = False        # result dict must not be empty
```

## Solution 2: Result Schema Validator

```python
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ValidationViolation:
    field_path: str
    violation_type: str    # "missing_required" | "type_mismatch" | "out_of_range" | "empty_result"
    expected: str
    actual: str


class ResultSchemaValidator:
    """
    Validates a tool result dict against a ResultSchema.
    Returns a list of violations; an empty list means the result is valid.
    """

    def validate(
        self, result: Any, schema: ResultSchema
    ) -> List[ValidationViolation]:
        violations: List[ValidationViolation] = []

        if not isinstance(result, dict):
            violations.append(ValidationViolation(
                field_path="<root>",
                violation_type="type_mismatch",
                expected="dict",
                actual=type(result).__name__,
            ))
            return violations

        if schema.non_empty_required and not result:
            violations.append(ValidationViolation(
                field_path="<root>",
                violation_type="empty_result",
                expected="non-empty dict",
                actual="{}",
            ))

        for spec in schema.fields:
            self._validate_field(result, spec, spec.name, violations)

        return violations

    def _validate_field(
        self,
        obj: dict,
        spec: FieldSpec,
        path: str,
        violations: List[ValidationViolation],
    ) -> None:
        if spec.name not in obj:
            if spec.required:
                violations.append(ValidationViolation(
                    field_path=path,
                    violation_type="missing_required",
                    expected=spec.field_type.value,
                    actual="missing",
                ))
            return

        value = obj[spec.name]

        if value is None:
            if not spec.nullable:
                violations.append(ValidationViolation(
                    field_path=path,
                    violation_type="type_mismatch",
                    expected=spec.field_type.value,
                    actual="null",
                ))
            return

        # Type check
        type_ok = self._check_type(value, spec.field_type)
        if not type_ok:
            violations.append(ValidationViolation(
                field_path=path,
                violation_type="type_mismatch",
                expected=spec.field_type.value,
                actual=type(value).__name__,
            ))
            return

        # Range / length checks
        if spec.field_type in (FieldType.INTEGER, FieldType.FLOAT):
            if spec.min_value is not None and value < spec.min_value:
                violations.append(ValidationViolation(
                    field_path=path,
                    violation_type="out_of_range",
                    expected=f">= {spec.min_value}",
                    actual=str(value),
                ))
            if spec.max_value is not None and value > spec.max_value:
                violations.append(ValidationViolation(
                    field_path=path,
                    violation_type="out_of_range",
                    expected=f"<= {spec.max_value}",
                    actual=str(value),
                ))

        if spec.field_type in (FieldType.STRING, FieldType.LIST):
            if spec.min_length is not None and len(value) < spec.min_length:
                violations.append(ValidationViolation(
                    field_path=path,
                    violation_type="out_of_range",
                    expected=f"len >= {spec.min_length}",
                    actual=f"len={len(value)}",
                ))

        if spec.allowed_values is not None and value not in spec.allowed_values:
            violations.append(ValidationViolation(
                field_path=path,
                violation_type="out_of_range",
                expected=f"one of {spec.allowed_values}",
                actual=str(value),
            ))

        # Recurse into nested dict schema
        if spec.field_type == FieldType.DICT and spec.nested_schema and isinstance(value, dict):
            for nested_spec in spec.nested_schema.fields:
                self._validate_field(value, nested_spec, f"{path}.{nested_spec.name}", violations)

    @staticmethod
    def _check_type(value: Any, field_type: FieldType) -> bool:
        mapping = {
            FieldType.STRING: str,
            FieldType.INTEGER: int,
            FieldType.FLOAT: (int, float),
            FieldType.BOOLEAN: bool,
            FieldType.LIST: list,
            FieldType.DICT: dict,
        }
        if field_type == FieldType.ANY:
            return True
        expected_type = mapping.get(field_type)
        if expected_type is None:
            return True
        # bool is a subclass of int in Python — treat separately
        if field_type == FieldType.INTEGER and isinstance(value, bool):
            return False
        return isinstance(value, expected_type)
```

## Solution 3: Schema Registry

```python
from typing import Dict, Optional


class ToolResultSchemaRegistry:
    """
    Stores ResultSchema definitions keyed by tool name.
    Tools without a registered schema pass through without validation.
    """

    def __init__(self):
        self._schemas: Dict[str, ResultSchema] = {}

    def register(self, schema: ResultSchema) -> None:
        self._schemas[schema.tool_name] = schema

    def get(self, tool_name: str) -> Optional[ResultSchema]:
        return self._schemas.get(tool_name)

    def registered_tools(self) -> List[str]:
        return list(self._schemas.keys())
```

## Solution 4: Validating Tool Result Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class SchemaValidationError(Exception):
    def __init__(self, tool_name: str, violations: List[ValidationViolation]):
        super().__init__(
            f"Tool '{tool_name}' result failed schema validation: "
            + "; ".join(f"{v.field_path} {v.violation_type}" for v in violations)
        )
        self.tool_name = tool_name
        self.violations = violations


class ValidatingToolResultDispatcher:
    """
    Wraps tool execution with result schema validation.
    Raises SchemaValidationError on violations; passes through results
    from tools with no registered schema.
    """

    def __init__(
        self,
        registry: ToolResultSchemaRegistry,
        validator: ResultSchemaValidator,
        strict: bool = True,    # False = log violations but don't raise
    ):
        self._registry = registry
        self._validator = validator
        self._strict = strict
        self._violation_log: List[dict] = []

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = await tool_fn(*args, **kwargs)

        schema = self._registry.get(tool_name)
        if schema is None:
            return result

        violations = self._validator.validate(result, schema)
        if violations:
            self._violation_log.append({
                "ts": time.time(),
                "tool_name": tool_name,
                "violations": [
                    {"path": v.field_path, "type": v.violation_type,
                     "expected": v.expected, "actual": v.actual}
                    for v in violations
                ],
            })
            if self._strict:
                raise SchemaValidationError(tool_name, violations)

        return result

    def violation_summary(self, last_n: int = 50) -> List[dict]:
        return self._violation_log[-last_n:]
```

## Solution 5: Schema Drift Detector

```python
import time
from collections import defaultdict
from typing import Dict, List


class SchemaDriftDetector:
    """
    Tracks which tools are producing schema violations over time.
    A rising violation rate for a specific tool signals an upstream
    API contract change requiring schema or tool update.
    """

    def __init__(self):
        self._lock = __import__("threading").Lock()
        self._windows: Dict[str, List[float]] = defaultdict(list)

    def record_violation(self, tool_name: str) -> None:
        with self._lock:
            self._windows[tool_name].append(time.time())

    def record_success(self, tool_name: str) -> None:
        pass  # successes not tracked — only violation rate matters

    def violation_rate(
        self, tool_name: str, window_seconds: float = 600.0
    ) -> float:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [t for t in self._windows.get(tool_name, []) if t >= cutoff]
        return len(recent)

    def drifted_tools(
        self, threshold_violations: int = 5, window_seconds: float = 600.0
    ) -> List[str]:
        return [
            tool for tool in self._windows
            if self.violation_rate(tool, window_seconds) >= threshold_violations
        ]
```

## Solution 6: Validation Coverage Auditor

```python
from typing import List


class SchemaValidationCoverageAuditor:
    """
    Reports which tools have schema coverage and which do not.
    Helps teams identify gaps before a schema violation causes a silent failure.
    """

    def __init__(
        self,
        registry: ToolResultSchemaRegistry,
        known_tool_names: List[str],
    ):
        self._registry = registry
        self._known = known_tool_names

    def audit(self) -> dict:
        covered = set(self._registry.registered_tools())
        all_tools = set(self._known)
        uncovered = all_tools - covered
        coverage_pct = len(covered & all_tools) / max(len(all_tools), 1) * 100

        return {
            "total_tools": len(all_tools),
            "covered": len(covered & all_tools),
            "uncovered": sorted(uncovered),
            "coverage_pct": round(coverage_pct, 1),
            "recommendation": (
                "All tools covered" if not uncovered
                else f"Add ResultSchema for: {', '.join(sorted(uncovered))}"
            ),
        }
```

## Comparison

| Approach | Field Type Check | Required Field Check | Range Validation | Drift Detection | Coverage Audit |
|---|---|---|---|---|---|
| ResultSchemaValidator | Yes | Yes | Yes | No | No |
| ToolResultSchemaRegistry | No | No | No | No | No |
| ValidatingToolResultDispatcher | Via validator | Via validator | Via validator | No | No |
| SchemaDriftDetector | No | No | No | Yes (rate) | No |
| SchemaValidationCoverageAuditor | No | No | No | No | Yes |

**Best for production**: Register schemas for every external tool on day one — use `SchemaValidationCoverageAuditor` as a startup check that fails the deploy if coverage drops below 100%. Run in `strict=False` for the first week after a schema change to observe violations without breaking production, then flip to `strict=True` once the schema matches observed traffic. Monitor `SchemaDriftDetector.drifted_tools()` in a daily job: a tool that starts producing violations after a period of silence almost always means the upstream API changed its response shape without notice.
