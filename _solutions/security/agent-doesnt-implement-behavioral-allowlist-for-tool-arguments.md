---
title: "Agent Doesn't Implement Behavioral Allowlist for Tool Arguments"
description: "Agents that validate tool argument schema but not argument values can be manipulated into passing semantically dangerous inputs that satisfy type constraints but violate behavioral expectations — oversized payloads, path traversals disguised as filenames, SQL fragments in filter strings, or URL schemes that bypass SSRF checks. Implement a behavioral allowlist that validates argument values against expected ranges, patterns, and semantic constraints beyond JSON schema."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-behavioral-allowlist-for-tool-arguments
tags: [input-validation, behavioral-allowlist, argument-sanitization, security, tool-calls, schema-enforcement]
symptoms:
  - "Schema validation passes but tool receives a filename like '../../etc/passwd'"
  - "LLM generates a SQL fragment in a 'filter' argument that reaches the database"
  - "Tool accepts any URL scheme including 'file://' and 'gopher://' for fetch operations"
  - "Numeric arguments accept extreme values (negative counts, year 9999) that cause overflow"
  - "JSON schema says 'string' but doesn't prevent 10MB strings from exhausting memory"
---

## Why This Happens

JSON Schema validates structure and type but not semantics. A `string` field validating as `maxLength: 1000` still allows `../../../etc/shadow` or a SQL injection fragment. Behavioral allowlists add a semantic layer: for each tool argument, define the expected universe of valid values (regex patterns, numeric ranges, URI schemes, enum sets, or custom predicate functions). Any argument that falls outside this universe is rejected before the tool executes — the LLM-generated value never reaches the tool function.

## Solution 1: Argument Constraint Model

```python
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Pattern, Set, Union

@dataclass
class ArgumentConstraint:
    arg_name: str
    required: bool = True
    arg_type: type = str

    # String constraints
    min_length: int = 0
    max_length: int = 10_000
    pattern: Optional[str] = None          # regex pattern string
    allowed_values: Optional[Set[str]] = None   # exact allowed values

    # Numeric constraints
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    # Semantic constraints
    forbidden_patterns: List[str] = field(default_factory=list)  # regex patterns to block
    allowed_schemes: Optional[Set[str]] = None   # for URL arguments: {"https", "http"}
    path_traversal_safe: bool = False       # if True, block ../ patterns
    sql_injection_safe: bool = False        # if True, block SQL keywords
    custom_validator: Optional[Callable[[Any], tuple]] = None  # returns (ok, reason)

    _compiled_pattern: Optional[re.Pattern] = field(default=None, init=False, repr=False)
    _compiled_forbidden: List[re.Pattern] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self):
        if self.pattern:
            self._compiled_pattern = re.compile(self.pattern)
        self._compiled_forbidden = [re.compile(p, re.I) for p in self.forbidden_patterns]
```

## Solution 2: Behavioral Argument Validator

```python
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

@dataclass
class ValidationError:
    arg_name: str
    value_preview: str   # truncated value — never full content for secrets
    violation: str
    severity: str        # "critical" | "high" | "medium"

SQL_KEYWORDS = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|UNION|"
    r"TRUNCATE|GRANT|REVOKE|CAST|CONVERT|DECLARE|FETCH|CURSOR)\b",
    re.I,
)
PATH_TRAVERSAL = re.compile(r"\.\.[/\\]|[/\\]\.\.")
NULL_BYTE = re.compile(r"\x00")
CRLF_INJECTION = re.compile(r"[\r\n]")

class BehavioralArgumentValidator:
    """
    Validates tool argument values against behavioral constraints.
    Goes beyond JSON schema to check semantic safety of string content,
    numeric ranges, URL schemes, and custom predicates.
    Rejects any argument that violates a constraint before tool execution.
    """

    def __init__(self, strict_mode: bool = True):
        self._strict = strict_mode

    def validate(
        self,
        constraints: List[ArgumentConstraint],
        arguments: Dict[str, Any],
    ) -> List[ValidationError]:
        errors = []
        constraint_map = {c.arg_name: c for c in constraints}

        # Check for unexpected arguments
        if self._strict:
            for key in arguments:
                if key not in constraint_map:
                    errors.append(ValidationError(
                        arg_name=key,
                        value_preview="<present>",
                        violation="unexpected argument not in allowlist",
                        severity="high",
                    ))

        for constraint in constraints:
            value = arguments.get(constraint.arg_name)

            if value is None:
                if constraint.required:
                    errors.append(ValidationError(
                        arg_name=constraint.arg_name,
                        value_preview="<missing>",
                        violation="required argument missing",
                        severity="critical",
                    ))
                continue

            errs = self._check_value(constraint, value)
            errors.extend(errs)

        return errors

    def _check_value(
        self, constraint: ArgumentConstraint, value: Any
    ) -> List[ValidationError]:
        errors = []
        name = constraint.arg_name
        preview = str(value)[:50]

        # Type check
        if not isinstance(value, constraint.arg_type):
            errors.append(ValidationError(name, preview,
                f"wrong type: expected {constraint.arg_type.__name__}, got {type(value).__name__}",
                "critical"))
            return errors   # skip further checks if wrong type

        # String checks
        if isinstance(value, str):
            if len(value) < constraint.min_length:
                errors.append(ValidationError(name, preview,
                    f"too short: {len(value)} < {constraint.min_length}", "medium"))
            if len(value) > constraint.max_length:
                errors.append(ValidationError(name, preview,
                    f"too long: {len(value)} > {constraint.max_length}", "high"))
            if constraint._compiled_pattern and not constraint._compiled_pattern.fullmatch(value):
                errors.append(ValidationError(name, preview,
                    f"does not match required pattern", "high"))
            if constraint.allowed_values and value not in constraint.allowed_values:
                errors.append(ValidationError(name, preview,
                    f"value not in allowed set", "high"))
            if constraint.path_traversal_safe and PATH_TRAVERSAL.search(value):
                errors.append(ValidationError(name, preview,
                    "path traversal detected", "critical"))
            if constraint.sql_injection_safe and SQL_KEYWORDS.search(value):
                errors.append(ValidationError(name, preview,
                    "SQL keyword detected", "critical"))
            if NULL_BYTE.search(value):
                errors.append(ValidationError(name, preview,
                    "null byte detected", "critical"))
            if CRLF_INJECTION.search(value):
                errors.append(ValidationError(name, preview,
                    "CRLF injection detected", "high"))
            for forbidden in constraint._compiled_forbidden:
                if forbidden.search(value):
                    errors.append(ValidationError(name, preview,
                        f"forbidden pattern matched: {forbidden.pattern[:40]}", "high"))
            if constraint.allowed_schemes:
                try:
                    scheme = urllib.parse.urlparse(value).scheme
                    if scheme and scheme not in constraint.allowed_schemes:
                        errors.append(ValidationError(name, preview,
                            f"URL scheme '{scheme}' not allowed", "critical"))
                except Exception:
                    pass

        # Numeric checks
        if isinstance(value, (int, float)):
            if constraint.min_value is not None and value < constraint.min_value:
                errors.append(ValidationError(name, preview,
                    f"below minimum: {value} < {constraint.min_value}", "high"))
            if constraint.max_value is not None and value > constraint.max_value:
                errors.append(ValidationError(name, preview,
                    f"above maximum: {value} > {constraint.max_value}", "high"))

        # Custom validator
        if constraint.custom_validator:
            ok, reason = constraint.custom_validator(value)
            if not ok:
                errors.append(ValidationError(name, preview, reason, "high"))

        return errors

    def is_safe(self, constraints, arguments) -> tuple:
        errors = self.validate(constraints, arguments)
        critical = [e for e in errors if e.severity == "critical"]
        return len(errors) == 0, errors
```

## Solution 3: Tool Constraint Registry

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class ToolConstraintSpec:
    tool_name: str
    argument_constraints: List[ArgumentConstraint] = field(default_factory=list)
    description: str = ""

class ToolConstraintRegistry:
    """
    Stores behavioral allowlist specifications per tool.
    Tools are registered with their argument constraints at startup.
    Validation is performed by looking up the tool spec before execution.
    """

    def __init__(self):
        self._specs: Dict[str, ToolConstraintSpec] = {}
        self._validator = BehavioralArgumentValidator()

    def register(self, spec: ToolConstraintSpec) -> None:
        self._specs[spec.tool_name] = spec

    def validate_call(
        self,
        tool_name: str,
        arguments: Dict,
    ) -> tuple:
        spec = self._specs.get(tool_name)
        if not spec:
            return True, []   # no spec = no constraints (configure strict mode separately)
        return self._validator.is_safe(spec.argument_constraints, arguments)

    def registered_tools(self) -> List[str]:
        return list(self._specs.keys())

    def example_constraints(self) -> Dict[str, ToolConstraintSpec]:
        """Returns example constraint specs for common tools."""
        return {
            "fetch_url": ToolConstraintSpec(
                tool_name="fetch_url",
                argument_constraints=[
                    ArgumentConstraint(
                        arg_name="url",
                        arg_type=str,
                        max_length=2048,
                        allowed_schemes={"https", "http"},
                        forbidden_patterns=[r"localhost", r"127\.0\.0\.", r"169\.254\."],
                    ),
                ],
            ),
            "read_file": ToolConstraintSpec(
                tool_name="read_file",
                argument_constraints=[
                    ArgumentConstraint(
                        arg_name="path",
                        arg_type=str,
                        max_length=512,
                        path_traversal_safe=True,
                        pattern=r"[a-zA-Z0-9_\-./]+",
                    ),
                ],
            ),
            "query_database": ToolConstraintSpec(
                tool_name="query_database",
                argument_constraints=[
                    ArgumentConstraint(
                        arg_name="filter",
                        arg_type=str,
                        max_length=500,
                        sql_injection_safe=True,
                        path_traversal_safe=True,
                    ),
                    ArgumentConstraint(
                        arg_name="limit",
                        arg_type=int,
                        min_value=1,
                        max_value=1000,
                    ),
                ],
            ),
        }
```

## Solution 4: Argument Sanitizer

```python
import re
import unicodedata
from typing import Any, Dict, List, Optional

class ArgumentSanitizer:
    """
    Sanitizes argument values after validation: removes null bytes,
    normalizes unicode, strips leading/trailing whitespace, and
    truncates oversized strings. Sanitization is a defense-in-depth
    measure — validation should always run first.
    """

    def sanitize_string(
        self,
        value: str,
        max_length: Optional[int] = None,
        normalize_unicode: bool = True,
        strip_whitespace: bool = True,
        remove_null_bytes: bool = True,
        remove_control_chars: bool = True,
    ) -> str:
        if remove_null_bytes:
            value = value.replace("\x00", "")
        if remove_control_chars:
            value = "".join(c for c in value if not unicodedata.category(c).startswith("C")
                           or c in "\t\n\r")
        if normalize_unicode:
            value = unicodedata.normalize("NFC", value)
        if strip_whitespace:
            value = value.strip()
        if max_length and len(value) > max_length:
            value = value[:max_length]
        return value

    def sanitize_arguments(
        self,
        arguments: Dict[str, Any],
        string_max_length: Optional[int] = 10_000,
    ) -> Dict[str, Any]:
        result = {}
        for key, value in arguments.items():
            if isinstance(value, str):
                result[key] = self.sanitize_string(value, max_length=string_max_length)
            elif isinstance(value, dict):
                result[key] = self.sanitize_arguments(value, string_max_length)
            elif isinstance(value, list):
                result[key] = [
                    self.sanitize_string(v, string_max_length) if isinstance(v, str) else v
                    for v in value
                ]
            else:
                result[key] = value
        return result
```

## Solution 5: Validation Audit Logger

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List

@dataclass
class ValidationAuditRecord:
    tool_name: str
    argument_count: int
    violation_count: int
    critical_count: int
    violations: List[str]
    blocked: bool
    session_id: str
    timestamp: float

class ValidationAuditLogger:
    """
    Logs all validation decisions for security analysis.
    Tracks per-tool violation rates to identify tools being probed.
    Detects repeated violations from the same session (possible attack).
    """

    def __init__(self, max_records: int = 10_000):
        self._records: Deque[ValidationAuditRecord] = deque(maxlen=max_records)
        self._by_tool: Dict[str, int] = defaultdict(int)
        self._by_session: Dict[str, int] = defaultdict(int)

    def log(
        self,
        tool_name: str,
        argument_count: int,
        errors: List[ValidationError],
        blocked: bool,
        session_id: str = "",
    ) -> None:
        critical = sum(1 for e in errors if e.severity == "critical")
        record = ValidationAuditRecord(
            tool_name=tool_name,
            argument_count=argument_count,
            violation_count=len(errors),
            critical_count=critical,
            violations=[f"{e.arg_name}: {e.violation}" for e in errors],
            blocked=blocked,
            session_id=session_id,
            timestamp=time.time(),
        )
        self._records.append(record)
        if errors:
            self._by_tool[tool_name] += 1
            if session_id:
                self._by_session[session_id] += 1

    def suspicious_sessions(self, threshold: int = 5) -> List[str]:
        return [sid for sid, count in self._by_session.items() if count >= threshold]

    def summary(self) -> dict:
        total = len(self._records)
        blocked = sum(1 for r in self._records if r.blocked)
        return {
            "total_validations": total,
            "blocked": blocked,
            "block_rate": round(blocked / max(total, 1), 4),
            "top_violating_tools": sorted(
                self._by_tool.items(), key=lambda x: x[1], reverse=True
            )[:5],
            "suspicious_sessions": self.suspicious_sessions(),
        }
```

## Solution 6: Pre-Execution Guard

```python
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

@dataclass
class GuardDecision:
    allowed: bool
    tool_name: str
    sanitized_arguments: Dict[str, Any]
    errors: List[ValidationError]
    block_reason: str

class PreExecutionGuard:
    """
    Single entry point for all tool pre-execution checks.
    Runs: behavioral validation -> sanitization -> audit logging.
    Returns sanitized arguments on allow, raises on block.
    """

    def __init__(
        self,
        registry: ToolConstraintRegistry,
        sanitizer: ArgumentSanitizer,
        audit_logger: ValidationAuditLogger,
        block_on_critical: bool = True,
        block_on_high: bool = False,
    ):
        self._registry = registry
        self._sanitizer = sanitizer
        self._audit = audit_logger
        self._block_critical = block_on_critical
        self._block_high = block_on_high

    def check(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        session_id: str = "",
    ) -> GuardDecision:
        # Validate
        safe, errors = self._registry.validate_call(tool_name, arguments)

        # Determine if blocked
        critical = [e for e in errors if e.severity == "critical"]
        high = [e for e in errors if e.severity == "high"]
        blocked = (
            (self._block_critical and len(critical) > 0) or
            (self._block_high and len(high) > 0)
        )

        # Sanitize (even if blocked, for logging purposes)
        sanitized = self._sanitizer.sanitize_arguments(arguments)

        # Audit
        self._audit.log(tool_name, len(arguments), errors, blocked, session_id)

        block_reason = ""
        if blocked:
            reasons = [f"{e.arg_name}: {e.violation}" for e in (critical + high)[:3]]
            block_reason = "; ".join(reasons)

        return GuardDecision(
            allowed=not blocked,
            tool_name=tool_name,
            sanitized_arguments=sanitized,
            errors=errors,
            block_reason=block_reason,
        )
```

## Comparison

| Approach | Schema Validation | Semantic Validation | Sanitization | Audit |
|---|---|---|---|---|
| ArgumentConstraint | Type + length | Pattern, enum, range | No | No |
| BehavioralArgumentValidator | Yes | Yes (SQL, path, SSRF) | No | No |
| ToolConstraintRegistry | Via validator | Via validator | No | No |
| ArgumentSanitizer | No | No | Yes | No |
| ValidationAuditLogger | No | No | No | Yes |
| PreExecutionGuard | Via registry | Via registry | Yes | Yes |

**Best for production**: Register `ToolConstraintSpec` for every tool at startup — treat missing specs as a misconfiguration alert. Set `path_traversal_safe=True` for all file path arguments and `sql_injection_safe=True` for all filter/query string arguments as a baseline. Set `allowed_schemes={"https"}` for all URL arguments unless HTTP is explicitly required. Run `ArgumentSanitizer` on all arguments after validation to catch edge cases. Monitor `ValidationAuditLogger.suspicious_sessions()` — repeated violations from the same session are an active probe indicator.
