---
title: "Agent Doesn't Implement Tool Argument Schema Injection Prevention"
description: "Agents that pass LLM-generated tool arguments directly to tools without schema validation are vulnerable to argument injection: the LLM may produce arguments that violate expected types, include unexpected fields with special meaning, or supply values that exploit tool internals. Implement strict schema validation and argument sanitization that rejects or coerces malformed LLM-generated arguments before they reach tool execution."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-argument-schema-injection-prevention
tags: [argument-injection, schema-validation, tool-security, llm-output-validation, input-sanitization, type-coercion]
symptoms:
  - "LLM-generated tool arguments bypass expected type constraints"
  - "Extra fields in LLM output silently reach tool internals as unexpected kwargs"
  - "String arguments containing shell metacharacters or SQL fragments passed to tools unescaped"
  - "No validation layer between LLM output parsing and tool dispatch"
  - "Tool crashes or behaves unexpectedly when LLM hallucinates argument values"
---

## Why This Happens

Tool schemas define what arguments a tool expects, but schema enforcement is the caller's responsibility — not the tool's. An LLM that hallucinates an extra field, outputs a string where an integer is expected, or produces a value that triggers a code path the developer did not anticipate can cause unexpected behavior. The fix is a mandatory validation layer between argument parsing and tool dispatch that enforces type, range, allowlist, and structure constraints derived from the tool schema.

## Solution 1: Tool Argument Schema

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Pattern, Union


class ArgType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass
class ArgConstraints:
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None          # regex pattern for strings
    allowlist: Optional[List[Any]] = None  # only these values are allowed
    denylist: Optional[List[Any]] = None   # these values are forbidden
    strip_html: bool = False
    strip_null_bytes: bool = True


@dataclass
class ArgSchema:
    name: str
    arg_type: ArgType
    required: bool = True
    default: Any = None
    constraints: ArgConstraints = field(default_factory=ArgConstraints)
    description: str = ""
    allow_extra_fields: bool = False   # for OBJECT type


@dataclass
class ToolArgumentSchema:
    tool_name: str
    args: List[ArgSchema]
    strip_unknown_fields: bool = True  # drop args not in schema
    strict_types: bool = True          # fail on type mismatch vs. coerce
```

## Solution 2: Argument Type Coercer

```python
import re
from typing import Any, Optional


class ArgumentTypeCoercer:
    """
    Attempts to coerce LLM-generated argument values to the expected type.
    Used when strict_types=False to handle common LLM type mismatches
    like returning "42" instead of 42 or "true" instead of True.
    """

    @staticmethod
    def coerce(value: Any, target_type: ArgType) -> Any:
        if value is None:
            return value

        if target_type == ArgType.STRING:
            return str(value)

        if target_type == ArgType.INTEGER:
            if isinstance(value, bool):
                raise TypeError(f"cannot coerce bool to integer")
            if isinstance(value, str):
                cleaned = value.strip().rstrip(".0")
                return int(cleaned)
            return int(value)

        if target_type == ArgType.FLOAT:
            if isinstance(value, bool):
                raise TypeError(f"cannot coerce bool to float")
            return float(value)

        if target_type == ArgType.BOOLEAN:
            if isinstance(value, str):
                low = value.lower().strip()
                if low in ("true", "1", "yes", "on"):
                    return True
                if low in ("false", "0", "no", "off"):
                    return False
                raise TypeError(f"cannot coerce '{value}' to boolean")
            return bool(value)

        if target_type in (ArgType.ARRAY, ArgType.OBJECT):
            if not isinstance(value, (list, dict)):
                raise TypeError(f"cannot coerce {type(value).__name__} to {target_type}")
            return value

        return value
```

## Solution 3: Argument Constraint Validator

```python
import re
from typing import Any


class ArgumentConstraintValidator:
    """
    Validates a coerced argument value against declared constraints.
    Returns a list of violation strings (empty = valid).
    """

    @staticmethod
    def validate(name: str, value: Any, constraints: ArgConstraints) -> list:
        violations = []

        if value is None:
            return violations

        # String sanitization
        if isinstance(value, str):
            if constraints.strip_null_bytes and "\x00" in value:
                value = value.replace("\x00", "")
            if constraints.strip_html:
                value = re.sub(r"<[^>]+>", "", value)
            if constraints.min_length is not None and len(value) < constraints.min_length:
                violations.append(f"'{name}' length {len(value)} < min {constraints.min_length}")
            if constraints.max_length is not None and len(value) > constraints.max_length:
                violations.append(f"'{name}' length {len(value)} > max {constraints.max_length}")
            if constraints.pattern and not re.match(constraints.pattern, value):
                violations.append(f"'{name}' does not match pattern '{constraints.pattern}'")

        # Numeric range
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if constraints.min_value is not None and value < constraints.min_value:
                violations.append(f"'{name}' value {value} < min {constraints.min_value}")
            if constraints.max_value is not None and value > constraints.max_value:
                violations.append(f"'{name}' value {value} > max {constraints.max_value}")

        # Allowlist / denylist
        if constraints.allowlist is not None and value not in constraints.allowlist:
            violations.append(f"'{name}' value {value!r} not in allowlist")
        if constraints.denylist is not None and value in constraints.denylist:
            violations.append(f"'{name}' value {value!r} is in denylist")

        return violations
```

## Solution 4: Tool Argument Validator

```python
from typing import Any, Dict, List, Tuple


class ToolArgumentValidator:
    """
    Validates and sanitizes a complete argument dict against a ToolArgumentSchema.
    Returns sanitized args or raises on validation failure.
    """

    def __init__(self, coercer: ArgumentTypeCoercer, constraint_validator: ArgumentConstraintValidator):
        self._coercer = coercer
        self._constraint_validator = constraint_validator

    def validate(
        self,
        raw_args: Dict[str, Any],
        schema: ToolArgumentSchema,
    ) -> Tuple[Dict[str, Any], List[str]]:
        """
        Returns (sanitized_args, warnings).
        Raises ValueError on hard failures.
        """
        sanitized: Dict[str, Any] = {}
        warnings: List[str] = []
        schema_keys = {s.name for s in schema.args}

        # Strip unknown fields
        unknown = set(raw_args.keys()) - schema_keys
        if unknown:
            if schema.strip_unknown_fields:
                warnings.append(f"stripped unknown fields: {sorted(unknown)}")
            else:
                raise ValueError(f"unknown argument fields: {sorted(unknown)}")

        for arg_schema in schema.args:
            name = arg_schema.name
            raw_value = raw_args.get(name)

            if raw_value is None:
                if arg_schema.required:
                    raise ValueError(f"missing required argument '{name}'")
                sanitized[name] = arg_schema.default
                continue

            # Type coercion / validation
            if not isinstance(raw_value, self._get_python_type(arg_schema.arg_type)):
                if schema.strict_types:
                    raise ValueError(
                        f"argument '{name}' expected {arg_schema.arg_type.value}, "
                        f"got {type(raw_value).__name__}"
                    )
                try:
                    raw_value = ArgumentTypeCoercer.coerce(raw_value, arg_schema.arg_type)
                    warnings.append(f"coerced '{name}' to {arg_schema.arg_type.value}")
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"cannot coerce '{name}': {exc}")

            # Constraint validation
            violations = ArgumentConstraintValidator.validate(name, raw_value, arg_schema.constraints)
            if violations:
                raise ValueError(f"constraint violations: {'; '.join(violations)}")

            sanitized[name] = raw_value

        return sanitized, warnings

    @staticmethod
    def _get_python_type(arg_type: ArgType):
        return {
            ArgType.STRING: str,
            ArgType.INTEGER: int,
            ArgType.FLOAT: (int, float),
            ArgType.BOOLEAN: bool,
            ArgType.ARRAY: list,
            ArgType.OBJECT: dict,
        }.get(arg_type, object)
```

## Solution 5: Validation-Gated Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class ValidationGatedToolDispatcher:
    """
    Validates tool arguments before dispatch and logs all validation
    failures for security monitoring.
    """

    def __init__(
        self,
        validator: ToolArgumentValidator,
        schema_registry: Dict[str, ToolArgumentSchema],
    ):
        self._validator = validator
        self._schemas = schema_registry
        self._validation_failures = 0
        self._total_dispatches = 0
        self._failure_log = []

    async def dispatch(
        self,
        tool_name: str,
        raw_args: Dict[str, Any],
        fn: Callable,
    ) -> Any:
        self._total_dispatches += 1
        schema = self._schemas.get(tool_name)

        if schema is None:
            raise ValueError(f"no schema registered for tool '{tool_name}'")

        try:
            sanitized_args, warnings = self._validator.validate(raw_args, schema)
        except ValueError as exc:
            self._validation_failures += 1
            self._failure_log.append({
                "ts": time.time(),
                "tool_name": tool_name,
                "error": str(exc),
                "raw_arg_keys": list(raw_args.keys()),
            })
            if len(self._failure_log) > 1000:
                self._failure_log.pop(0)
            raise

        return await fn(**sanitized_args)

    def stats(self) -> dict:
        return {
            "total_dispatches": self._total_dispatches,
            "validation_failures": self._validation_failures,
            "failure_rate": round(self._validation_failures / max(self._total_dispatches, 1), 4),
        }

    def recent_failures(self, n: int = 20) -> list:
        return self._failure_log[-n:]
```

## Solution 6: Schema Injection Audit Reporter

```python
import time
from collections import Counter
from typing import List


class SchemaInjectionAuditReporter:
    """
    Analyzes validation failure logs to identify patterns that may
    indicate prompt injection attempts targeting tool arguments.
    """

    INJECTION_INDICATORS = [
        "__", "eval(", "exec(", "import ", "os.system",
        "subprocess", "DROP TABLE", "SELECT *", "../", "\\x00",
    ]

    def __init__(self, dispatcher: ValidationGatedToolDispatcher):
        self._dispatcher = dispatcher

    def _looks_like_injection(self, error_msg: str) -> bool:
        return any(indicator in error_msg for indicator in self.INJECTION_INDICATORS)

    def report(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        failures = [f for f in self._dispatcher._failure_log if f["ts"] >= cutoff]
        suspected_injections = [f for f in failures if self._looks_like_injection(f.get("error", ""))]
        by_tool = Counter(f["tool_name"] for f in failures)

        return {
            "window_seconds": window_seconds,
            "total_failures": len(failures),
            "suspected_injections": len(suspected_injections),
            "failures_by_tool": dict(by_tool.most_common(10)),
            "injection_alerts": [
                {"tool": f["tool_name"], "error_prefix": f["error"][:80]}
                for f in suspected_injections[:10]
            ],
            "alert": len(suspected_injections) > 0,
        }
```

## Comparison

| Approach | Type Validation | Constraint Validation | Unknown Field Strip | Injection Detection | Audit |
|---|---|---|---|---|---|
| ToolArgumentValidator | Yes (strict + coerce) | Yes | Yes | No | No |
| ArgumentTypeCoercer | Yes (coerce only) | No | No | No | No |
| ArgumentConstraintValidator | No | Yes (range+pattern+list) | No | No | No |
| ValidationGatedToolDispatcher | Via validator | Via validator | Via schema | No | Yes |
| SchemaInjectionAuditReporter | No | No | No | Yes (heuristic) | Yes |

**Best for production**: Set `strict_types=True` for production and `False` only in development — LLM type mismatches are usually promptable away and coercion in production masks prompt issues. Always set `strip_unknown_fields=True` — an LLM that produces extra fields is either hallucinating or has been injected with a prompt that adds fields with side-effect names. Set `max_length` constraints on all string arguments — unbounded strings are the most common vector for prompt injection payloads. Run `SchemaInjectionAuditReporter.report()` hourly and alert on any `suspected_injections > 0`.
