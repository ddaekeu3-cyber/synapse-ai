---
title: "Agent Doesn't Implement Tool Argument Injection Prevention"
description: "Agents that pass LLM-generated tool arguments directly to execution without sanitization are vulnerable to argument injection: the model outputs a shell command argument containing semicolons, a SQL fragment in a query parameter, or a path traversal sequence in a file argument. Implement tool argument injection prevention with per-argument type validators, allowlist-based sanitization, and injection pattern detection before any argument reaches the execution layer."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-tool-argument-injection-prevention
tags: [argument-injection, input-sanitization, shell-injection, sql-injection, path-traversal, argument-validation]
symptoms:
  - "Shell tool receives arguments containing semicolons or pipe characters from LLM output"
  - "Database query tool receives arguments with SQL fragments injected by a prompt injection attack"
  - "File read tool receives paths containing '../' sequences that escape the intended directory"
  - "No validation that LLM-generated argument values match the expected type and format"
  - "Tool arguments are passed to subprocess or database calls without any sanitization"
---

## Why This Happens

Tool argument schemas define types (string, integer, boolean) but not safe value constraints. An LLM instructed by a malicious document to call a shell tool with `{"command": "ls; rm -rf /"}` produces a valid-schema argument that is a dangerous injection. The execution layer receives the argument and passes it to the subprocess without inspecting the content. Prevention requires a second validation layer beyond JSON schema: injection pattern detection, allowlist matching for sensitive arguments, and contextual sanitization that strips or rejects dangerous characters before the argument reaches any execution primitive.

## Solution 1: Argument Injection Risk Profile

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Pattern


class ArgumentRiskLevel(str, Enum):
    LOW = "low"           # free-form text unlikely to be executable
    MEDIUM = "medium"     # structured values like URLs or emails
    HIGH = "high"         # values passed to system calls, SQL, or file paths
    CRITICAL = "critical" # values passed to shell execution


@dataclass
class ArgumentInjectionProfile:
    tool_name: str
    argument_name: str
    risk_level: ArgumentRiskLevel
    allowed_pattern: Optional[str] = None       # regex; value must match entirely
    blocked_patterns: List[str] = field(default_factory=list)  # any match = reject
    max_length: int = 2000
    strip_null_bytes: bool = True
```

## Solution 2: Injection Pattern Library

```python
from typing import Dict, List, Tuple
import re


SHELL_INJECTION_PATTERNS = [
    r"[;&|`$]",                          # shell metacharacters
    r"\$\(",                             # command substitution
    r"\b(rm|chmod|chown|sudo|su|curl|wget|nc|bash|sh|python|perl|ruby)\b",
    r">\s*/",                            # redirect to root path
    r"\.\./",                            # path traversal
]

SQL_INJECTION_PATTERNS = [
    r"('|\")\s*(;|--|\|\||&&)",         # quote followed by statement terminator
    r"\b(union|select|insert|update|delete|drop|truncate|exec|execute)\b",
    r"--\s",                            # SQL comment
    r"/\*.*?\*/",                       # block comment
    r"\bxp_\w+\b",                     # MSSQL extended procedures
    r"\b0x[0-9a-f]+\b",               # hex encoding
]

PATH_TRAVERSAL_PATTERNS = [
    r"\.\./",                           # Unix path traversal
    r"\.\.[/\\]",                       # Windows path traversal
    r"%2e%2e[%2f%5c]",                 # URL-encoded traversal
    r"[\x00-\x1f]",                    # control characters
]

LDAP_INJECTION_PATTERNS = [
    r"[*)(\\]",                        # LDAP metacharacters
    r"\x00",                           # null byte
]

INJECTION_PATTERN_SETS: Dict[str, List[str]] = {
    "shell": SHELL_INJECTION_PATTERNS,
    "sql": SQL_INJECTION_PATTERNS,
    "path": PATH_TRAVERSAL_PATTERNS,
    "ldap": LDAP_INJECTION_PATTERNS,
}
```

## Solution 3: Argument Injection Validator

```python
import re
from typing import Any, List, Optional


class ArgumentInjectionValidator:
    """
    Validates a single tool argument value against its injection profile.
    Returns a validation result with details on any detected pattern.
    """

    def validate(
        self,
        value: Any,
        profile: ArgumentInjectionProfile,
    ) -> dict:
        if not isinstance(value, str):
            return {"valid": True, "sanitized_value": value, "findings": []}

        findings = []

        # Strip null bytes if configured
        if profile.strip_null_bytes:
            value = value.replace("\x00", "")

        # Length check
        if len(value) > profile.max_length:
            return {
                "valid": False,
                "sanitized_value": None,
                "findings": [f"value exceeds max_length {profile.max_length}"],
            }

        # Allowlist pattern — value must match entirely
        if profile.allowed_pattern:
            if not re.fullmatch(profile.allowed_pattern, value):
                findings.append(
                    f"value does not match allowed pattern: {profile.allowed_pattern}"
                )
                return {"valid": False, "sanitized_value": None, "findings": findings}

        # Blocked patterns — any match = reject
        for pattern in profile.blocked_patterns:
            match = re.search(pattern, value, re.IGNORECASE | re.DOTALL)
            if match:
                findings.append(
                    f"blocked pattern matched: '{pattern}' at position {match.start()}"
                )

        valid = len(findings) == 0
        return {
            "valid": valid,
            "sanitized_value": value if valid else None,
            "findings": findings,
        }
```

## Solution 4: Tool Argument Sanitization Registry

```python
from typing import Dict, List, Optional


class ToolArgumentSanitizationRegistry:
    """
    Stores injection profiles for all registered tool arguments.
    Returns a permissive default for unknown arguments rather than
    blocking unknown tools (fail-open with logging).
    """

    def __init__(self):
        self._profiles: Dict[str, Dict[str, ArgumentInjectionProfile]] = {}

    def register(self, profile: ArgumentInjectionProfile) -> None:
        if profile.tool_name not in self._profiles:
            self._profiles[profile.tool_name] = {}
        self._profiles[profile.tool_name][profile.argument_name] = profile

    def get(
        self, tool_name: str, argument_name: str
    ) -> Optional[ArgumentInjectionProfile]:
        return self._profiles.get(tool_name, {}).get(argument_name)

    def register_shell_tool(self, tool_name: str, command_arg: str = "command") -> None:
        self.register(ArgumentInjectionProfile(
            tool_name=tool_name,
            argument_name=command_arg,
            risk_level=ArgumentRiskLevel.CRITICAL,
            blocked_patterns=SHELL_INJECTION_PATTERNS,
            max_length=500,
        ))

    def register_sql_tool(self, tool_name: str, query_arg: str = "query") -> None:
        self.register(ArgumentInjectionProfile(
            tool_name=tool_name,
            argument_name=query_arg,
            risk_level=ArgumentRiskLevel.HIGH,
            blocked_patterns=SQL_INJECTION_PATTERNS,
            max_length=2000,
        ))

    def register_file_tool(self, tool_name: str, path_arg: str = "path") -> None:
        self.register(ArgumentInjectionProfile(
            tool_name=tool_name,
            argument_name=path_arg,
            risk_level=ArgumentRiskLevel.HIGH,
            blocked_patterns=PATH_TRAVERSAL_PATTERNS,
            allowed_pattern=r"[a-zA-Z0-9_\-./]+",
            max_length=500,
        ))
```

## Solution 5: Injection-Safe Tool Call Gateway

```python
import time
from typing import Any, Callable, Dict, List


class InjectionSafeToolCallGateway:
    """
    Validates all arguments against their injection profiles before
    allowing a tool call to proceed. Logs violations for audit.
    """

    def __init__(
        self,
        registry: ToolArgumentSanitizationRegistry,
        validator: ArgumentInjectionValidator,
    ):
        self._registry = registry
        self._validator = validator
        self._violation_log: List[dict] = []
        self._blocked_calls = 0
        self._allowed_calls = 0

    async def dispatch(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        execute_fn: Callable,
    ) -> Any:
        violations = []
        for arg_name, arg_value in arguments.items():
            profile = self._registry.get(tool_name, arg_name)
            if profile is None:
                continue
            result = self._validator.validate(arg_value, profile)
            if not result["valid"]:
                violations.append({
                    "argument": arg_name,
                    "findings": result["findings"],
                })

        if violations:
            self._blocked_calls += 1
            self._violation_log.append({
                "ts": time.time(),
                "tool_name": tool_name,
                "violations": violations,
            })
            raise PermissionError(
                f"Tool call '{tool_name}' blocked: injection patterns detected in "
                f"argument(s): {[v['argument'] for v in violations]}"
            )

        self._allowed_calls += 1
        return await execute_fn(tool_name, arguments)

    def violation_summary(self) -> dict:
        total = self._allowed_calls + self._blocked_calls
        return {
            "total_calls": total,
            "allowed": self._allowed_calls,
            "blocked": self._blocked_calls,
            "block_rate": round(self._blocked_calls / max(total, 1), 4),
            "recent_violations": self._violation_log[-10:],
        }
```

## Solution 6: Injection Pattern Coverage Auditor

```python
from typing import Any, Dict, List


class InjectionPatternCoverageAuditor:
    """
    Scans tool schemas to identify arguments that have no injection
    profile registered, surfacing coverage gaps before deployment.
    """

    def __init__(self, registry: ToolArgumentSanitizationRegistry):
        self._registry = registry

    def audit(
        self,
        tool_schemas: List[Dict[str, Any]],
    ) -> List[dict]:
        """
        tool_schemas: list of dicts with 'name' and 'parameters' (JSON schema).
        Returns uncovered high-risk argument candidates.
        """
        gaps = []
        HIGH_RISK_ARG_NAMES = {
            "command", "query", "sql", "path", "file", "url",
            "script", "expression", "template", "filter",
        }
        for schema in tool_schemas:
            tool_name = schema.get("name", "")
            params = schema.get("parameters", {}).get("properties", {})
            for arg_name in params:
                if arg_name.lower() in HIGH_RISK_ARG_NAMES:
                    profile = self._registry.get(tool_name, arg_name)
                    if profile is None:
                        gaps.append({
                            "tool_name": tool_name,
                            "argument": arg_name,
                            "recommendation": (
                                f"Register an injection profile for '{tool_name}.{arg_name}'"
                            ),
                        })
        return gaps
```

## Comparison

| Approach | Pattern Detection | Allowlist Matching | Per-Arg Profiles | Dispatch Gating | Coverage Audit |
|---|---|---|---|---|---|
| ArgumentInjectionValidator | Yes (regex) | Yes | Via profile | No | No |
| ToolArgumentSanitizationRegistry | No | No | Yes | No | No |
| InjectionSafeToolCallGateway | Via validator | Via validator | Via registry | Yes | No |
| InjectionPatternCoverageAuditor | No | No | Via registry | No | Yes |

**Best for production**: Run `InjectionPatternCoverageAuditor.audit()` against all tool schemas in CI — any high-risk argument without a profile should fail the build. Register shell tools with `register_shell_tool()` and file tools with `register_file_tool()` using the built-in blocked-pattern sets; these cover the most common injection vectors out of the box. Log every blocked call with full violation details — a spike in injection attempts from a single session indicates an active prompt injection attack and the session should be terminated immediately. Never rely solely on JSON schema validation for security; schema types catch type errors but not injection payloads.
