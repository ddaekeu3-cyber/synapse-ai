---
title: "Agent Doesn't Implement Output Sanitization Before Tool Argument Injection"
description: "Agents that pass LLM-generated text directly into tool arguments without sanitization enable prompt-injection-driven tool abuse: the LLM output contains a crafted string that, when used as a shell command argument, SQL clause, or file path, escapes the intended context. Implement output sanitization that validates and escapes LLM-produced values before they reach tool argument slots, enforcing type constraints, allowlists, and structural boundaries per argument."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-output-sanitization-before-tool-argument-injection
tags: [output-sanitization, tool-injection, argument-validation, prompt-injection, shell-escape, sql-injection]
symptoms:
  - "LLM-generated filename contains '../../../etc/passwd' and the file-read tool opens it"
  - "SQL query tool receives an LLM-produced WHERE clause with injected OR 1=1"
  - "Shell command tool receives LLM output containing '; rm -rf /'"
  - "No validation between what the LLM returns and what is passed to tool arguments"
  - "Tool argument schema is defined but LLM output is cast directly without sanitization"
---

## Why This Happens

Tool-calling agents trust LLM output as structurally valid and safe. JSON parsing validates syntax but not semantics: `{"path": "../../../etc/passwd"}` is valid JSON and passes schema validation, but it escapes the intended directory. The LLM can be manipulated by injected content in retrieved documents to produce malicious argument values. Sanitization must happen as a distinct layer between LLM output parsing and tool invocation — type-checking, allowlisting, and context-specific escaping per argument slot.

## Solution 1: Argument Sanitization Rule

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional, Pattern, Set


class ArgumentType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    PATH = "path"           # filesystem path — blocks traversal
    IDENTIFIER = "identifier"  # alphanumeric + underscore only
    SQL_VALUE = "sql_value"    # will be parameterized — no injection
    URL = "url"
    ENUM = "enum"           # must be in allowlist


@dataclass
class ArgumentSanitizationRule:
    arg_name: str
    arg_type: ArgumentType
    required: bool = True
    max_length: int = 4096
    min_length: int = 0
    allowlist: Optional[Set[str]] = None     # exact allowed values (for ENUM)
    pattern: Optional[str] = None            # regex the value must match
    deny_patterns: List[str] = field(default_factory=list)  # regex patterns that must NOT match
    strip_whitespace: bool = True
    custom_validator: Optional[Callable[[Any], bool]] = None
```

## Solution 2: Per-Type Sanitizers

```python
import os
import re
import urllib.parse
from typing import Any, Tuple


class TypeSanitizer:
    """
    Applies type-specific sanitization and validation.
    Returns (sanitized_value, error_message_or_None).
    """

    PATH_TRAVERSAL = re.compile(r"\.\.[/\\]|[/\\]\.\.")
    SHELL_METACHAR = re.compile(r"[;&|`$<>{}()\[\]!\\]")
    SQL_COMMENT = re.compile(r"(--|#|/\*)")
    SQL_TERMINATOR = re.compile(r"[;']")
    IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    URL_SCHEME_ALLOWLIST = {"http", "https"}

    @classmethod
    def sanitize(
        cls,
        value: Any,
        rule: ArgumentSanitizationRule,
    ) -> Tuple[Any, Optional[str]]:
        if value is None:
            if rule.required:
                return None, f"argument '{rule.arg_name}' is required"
            return None, None

        arg_type = rule.arg_type

        if arg_type == ArgumentType.INTEGER:
            try:
                return int(value), None
            except (ValueError, TypeError):
                return None, f"'{rule.arg_name}' must be an integer, got {type(value).__name__}"

        if arg_type == ArgumentType.FLOAT:
            try:
                return float(value), None
            except (ValueError, TypeError):
                return None, f"'{rule.arg_name}' must be a float"

        if arg_type == ArgumentType.BOOLEAN:
            if isinstance(value, bool):
                return value, None
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true", None
            return None, f"'{rule.arg_name}' must be a boolean"

        # String-based types
        s = str(value)
        if rule.strip_whitespace:
            s = s.strip()

        if len(s) < rule.min_length:
            return None, f"'{rule.arg_name}' too short (min {rule.min_length})"
        if len(s) > rule.max_length:
            return None, f"'{rule.arg_name}' too long (max {rule.max_length})"

        if arg_type == ArgumentType.PATH:
            if cls.PATH_TRAVERSAL.search(s):
                return None, f"'{rule.arg_name}' contains path traversal sequence"
            if cls.SHELL_METACHAR.search(s):
                return None, f"'{rule.arg_name}' contains shell metacharacters"
            # Normalize and confine to relative paths
            normalized = os.path.normpath(s)
            if normalized.startswith("/") or normalized.startswith("\\"):
                return None, f"'{rule.arg_name}' must be a relative path"
            return normalized, None

        if arg_type == ArgumentType.IDENTIFIER:
            if not cls.IDENTIFIER_RE.match(s):
                return None, f"'{rule.arg_name}' must match [a-zA-Z_][a-zA-Z0-9_]*"
            return s, None

        if arg_type == ArgumentType.SQL_VALUE:
            # Values must be passed as parameters — reject anything that looks like injection
            if cls.SQL_COMMENT.search(s) or cls.SQL_TERMINATOR.search(s):
                return None, f"'{rule.arg_name}' contains SQL injection pattern"
            return s, None

        if arg_type == ArgumentType.URL:
            try:
                parsed = urllib.parse.urlparse(s)
                if parsed.scheme not in cls.URL_SCHEME_ALLOWLIST:
                    return None, f"'{rule.arg_name}' URL scheme must be http/https"
                if not parsed.netloc:
                    return None, f"'{rule.arg_name}' URL missing host"
                return s, None
            except Exception:
                return None, f"'{rule.arg_name}' is not a valid URL"

        if arg_type == ArgumentType.ENUM:
            if rule.allowlist and s not in rule.allowlist:
                return None, f"'{rule.arg_name}' must be one of {sorted(rule.allowlist)}"
            return s, None

        # Generic STRING
        for deny in rule.deny_patterns:
            if re.search(deny, s, re.IGNORECASE):
                return None, f"'{rule.arg_name}' matches forbidden pattern"
        if rule.pattern and not re.fullmatch(rule.pattern, s):
            return None, f"'{rule.arg_name}' does not match required pattern"
        if rule.allowlist and s not in rule.allowlist:
            return None, f"'{rule.arg_name}' not in allowlist"
        if rule.custom_validator and not rule.custom_validator(s):
            return None, f"'{rule.arg_name}' failed custom validation"
        return s, None
```

## Solution 3: Tool Argument Sanitizer

```python
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class SanitizationResult:
    sanitized_args: Dict[str, Any]
    errors: List[str]
    warnings: List[str]
    blocked: bool   # True if any required arg failed or a critical error occurred

    def is_safe(self) -> bool:
        return not self.blocked and not self.errors


class ToolArgumentSanitizer:
    """
    Validates and sanitizes all arguments for a tool call against
    a set of ArgumentSanitizationRules before the call is dispatched.
    Returns a SanitizationResult — caller must check is_safe() before proceeding.
    """

    def __init__(self, rules: List[ArgumentSanitizationRule]):
        self._rules = {r.arg_name: r for r in rules}

    def sanitize(self, raw_args: Dict[str, Any]) -> SanitizationResult:
        sanitized: Dict[str, Any] = {}
        errors: List[str] = []
        warnings: List[str] = []
        blocked = False

        # Check all defined rules
        for arg_name, rule in self._rules.items():
            value = raw_args.get(arg_name)
            clean, error = TypeSanitizer.sanitize(value, rule)
            if error:
                errors.append(error)
                if rule.required:
                    blocked = True
            else:
                sanitized[arg_name] = clean

        # Warn about extra arguments not in rules
        for key in raw_args:
            if key not in self._rules:
                warnings.append(f"unexpected argument '{key}' stripped")

        return SanitizationResult(
            sanitized_args=sanitized,
            errors=errors,
            warnings=warnings,
            blocked=blocked,
        )
```

## Solution 4: Sanitization-Gated Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict


class SanitizationGatedToolDispatcher:
    """
    Wraps tool invocation with mandatory sanitization.
    Raises ValueError with sanitization errors if args are unsafe.
    Logs warnings for stripped extra arguments.
    """

    def __init__(self):
        self._sanitizers: Dict[str, ToolArgumentSanitizer] = {}

    def register(self, tool_name: str, sanitizer: ToolArgumentSanitizer) -> None:
        self._sanitizers[tool_name] = sanitizer

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        raw_args: Dict[str, Any],
    ) -> Any:
        sanitizer = self._sanitizers.get(tool_name)
        if sanitizer is None:
            raise ValueError(
                f"Tool '{tool_name}' has no registered sanitizer — "
                "register one before dispatching"
            )

        result = sanitizer.sanitize(raw_args)

        if not result.is_safe():
            raise ValueError(
                f"Tool '{tool_name}' argument sanitization failed: "
                + "; ".join(result.errors)
            )

        return await tool_fn(**result.sanitized_args)
```

## Solution 5: Sanitization Audit Logger

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SanitizationAuditEvent:
    tool_name: str
    blocked: bool
    errors: List[str]
    warnings: List[str]
    raw_arg_keys: List[str]
    sanitized_arg_keys: List[str]
    timestamp: float = field(default_factory=time.time)


class SanitizationAuditLogger:
    """
    Records sanitization outcomes for security audit and anomaly detection.
    High block rates may indicate an active injection attempt.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._events: List[SanitizationAuditEvent] = []
        self._window = window_seconds

    def record(
        self,
        tool_name: str,
        raw_args: Dict[str, Any],
        result: SanitizationResult,
    ) -> None:
        event = SanitizationAuditEvent(
            tool_name=tool_name,
            blocked=result.blocked,
            errors=list(result.errors),
            warnings=list(result.warnings),
            raw_arg_keys=list(raw_args.keys()),
            sanitized_arg_keys=list(result.sanitized_args.keys()),
        )
        self._events.append(event)

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e.timestamp >= cutoff]

    def summary(self) -> dict:
        self._trim()
        total = len(self._events)
        blocked = sum(1 for e in self._events if e.blocked)
        by_tool: Dict[str, int] = {}
        for e in self._events:
            if e.blocked:
                by_tool[e.tool_name] = by_tool.get(e.tool_name, 0) + 1
        return {
            "total_calls": total,
            "blocked_calls": blocked,
            "block_rate": round(blocked / max(total, 1), 4),
            "blocked_by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])),
        }
```

## Solution 6: Injection Pattern Detector

```python
import re
from typing import Any, Dict, List


INJECTION_SIGNATURES = [
    (re.compile(r"\.\.[/\\]"), "path_traversal"),
    (re.compile(r"[;&|`$]"), "shell_injection"),
    (re.compile(r"(--|#|/\*|\*/)", re.IGNORECASE), "sql_comment"),
    (re.compile(r"<script", re.IGNORECASE), "xss_script_tag"),
    (re.compile(r"(ignore previous|disregard|new instruction)", re.IGNORECASE), "prompt_injection"),
    (re.compile(r"\x00"), "null_byte"),
    (re.compile(r"[\r\n]{3,}"), "excessive_newlines"),
]


class InjectionPatternDetector:
    """
    Scans raw LLM output arguments for known injection signatures
    before sanitization rules are applied. Useful as a fast pre-screen
    and for logging attempted injections separately from rule violations.
    """

    def scan(self, raw_args: Dict[str, Any]) -> List[dict]:
        findings = []
        for arg_name, value in raw_args.items():
            if not isinstance(value, str):
                continue
            for pattern, sig_name in INJECTION_SIGNATURES:
                if pattern.search(value):
                    findings.append({
                        "arg_name": arg_name,
                        "signature": sig_name,
                        "value_prefix": value[:30] + ("..." if len(value) > 30 else ""),
                    })
        return findings
```

## Comparison

| Approach | Type Validation | Pattern Blocking | Shell/SQL/Path Escape | Audit Log | Injection Detection |
|---|---|---|---|---|---|
| TypeSanitizer | Yes | Yes (deny patterns) | Yes (per type) | No | No |
| ToolArgumentSanitizer | Via TypeSanitizer | Yes | Yes | No | No |
| SanitizationGatedToolDispatcher | Via sanitizer | Via sanitizer | Via sanitizer | No | No |
| SanitizationAuditLogger | No | No | No | Yes | No |
| InjectionPatternDetector | No | Yes (signatures) | Partial | No | Yes |

**Best for production**: Register a `ToolArgumentSanitizer` for every tool before deployment — no tool should be dispatchable without one. Use `ArgumentType.PATH` for any file-related argument and `ArgumentType.IDENTIFIER` for any column or table name passed to a database tool. Run `InjectionPatternDetector.scan()` before sanitization and log findings separately: a spike in injection signatures indicates an active attack, not just misconfigured inputs. Monitor `SanitizationAuditLogger.summary()` block rate — sustained rates above 1% warrant investigation of the upstream retrieval pipeline for injected content.
