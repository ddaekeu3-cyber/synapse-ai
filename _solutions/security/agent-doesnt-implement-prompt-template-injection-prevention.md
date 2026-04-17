---
title: "Agent Doesn't Implement Prompt Template Injection Prevention"
description: "Agents that interpolate user-controlled values directly into prompt templates allow attackers to inject template syntax, variable references, or formatting directives that alter the rendered prompt. Implement prompt template injection prevention that escapes user input before interpolation, validates template variable names against an allowlist, and sandboxes template rendering to prevent code execution."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-prompt-template-injection-prevention
tags: [template-injection, prompt-injection, input-sanitization, template-escaping, allowlist-validation, server-side-template]
symptoms:
  - "User input containing {variable} syntax alters the rendered prompt unexpectedly"
  - "Attacker passes __proto__ or __class__ as a template variable to probe the template engine"
  - "Template rendering executes Python expressions when Jinja2 is used without sandboxing"
  - "No validation that template variable names match an expected allowlist"
  - "Log entries show rendered prompts containing user-supplied format strings"
---

## Why This Happens

Prompt templates that use Python's `str.format(**user_data)` or f-string-equivalent interpolation execute any format string the user can influence. If the template is `"Answer as {persona}: {query}"` and the user controls `persona`, they can pass `persona="{query.__class__.__mro__}"` to extract internal object attributes. Even safer template engines like Jinja2 allow expression evaluation by default. Prevention requires escaping user values before they reach the template, validating that only known variable names are substituted, and using a sandboxed renderer that prevents attribute traversal.

## Solution 1: Template Variable Descriptor

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Pattern


class VariableType(str, Enum):
    TEXT = "text"           # free text, HTML-escaped before use
    IDENTIFIER = "identifier"  # alphanumeric + underscore only
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ENUM = "enum"           # must be one of allowed_values


@dataclass
class TemplateVariableDescriptor:
    name: str
    var_type: VariableType
    required: bool = True
    max_length: int = 4000
    allowed_values: list = field(default_factory=list)   # for ENUM type
    strip_template_chars: bool = True   # remove { } % < > from TEXT values

    def validate(self, value: object) -> tuple:
        """Returns (is_valid: bool, reason: str)."""
        if value is None:
            if self.required:
                return False, f"required variable '{self.name}' is missing"
            return True, ""

        if self.var_type == VariableType.INTEGER:
            if not isinstance(value, int):
                return False, f"'{self.name}' must be int"
            return True, ""

        if self.var_type == VariableType.BOOLEAN:
            if not isinstance(value, bool):
                return False, f"'{self.name}' must be bool"
            return True, ""

        str_val = str(value)

        if len(str_val) > self.max_length:
            return False, f"'{self.name}' exceeds max_length {self.max_length}"

        if self.var_type == VariableType.IDENTIFIER:
            if not re.fullmatch(r"[A-Za-z0-9_\-]+", str_val):
                return False, f"'{self.name}' contains non-identifier characters"

        if self.var_type == VariableType.ENUM:
            if str_val not in self.allowed_values:
                return False, f"'{self.name}' must be one of {self.allowed_values}"

        return True, ""
```

## Solution 2: Template Variable Sanitizer

```python
import re
from typing import Any, Dict, List


TEMPLATE_INJECTION_CHARS = re.compile(r"[{}\[\]<>%$`\\]")
DUNDER_PATTERN = re.compile(r"__\w+__")
FORMAT_SPEC = re.compile(r":\s*[^}]*")   # format spec like {:>10} inside {}


class TemplateVariableSanitizer:
    """
    Sanitizes user-supplied values before they are interpolated into
    prompt templates. Removes template syntax, dunder attributes, and
    format specifiers from TEXT-type values.
    """

    @staticmethod
    def sanitize_text(value: str) -> str:
        # Remove dunder references
        value = DUNDER_PATTERN.sub("[removed]", value)
        # Remove template injection characters
        value = TEMPLATE_INJECTION_CHARS.sub("", value)
        # Normalize whitespace
        value = re.sub(r"\s+", " ", value).strip()
        return value

    @staticmethod
    def sanitize_identifier(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_\-]", "", value)

    @classmethod
    def sanitize(
        cls,
        values: Dict[str, Any],
        descriptors: List[TemplateVariableDescriptor],
    ) -> Dict[str, Any]:
        desc_map = {d.name: d for d in descriptors}
        result: Dict[str, Any] = {}

        for name, value in values.items():
            desc = desc_map.get(name)
            if desc is None:
                # Unknown variable — skip entirely
                continue

            if value is None:
                result[name] = value
                continue

            if desc.var_type == VariableType.TEXT:
                cleaned = cls.sanitize_text(str(value))
                if desc.strip_template_chars:
                    result[name] = cleaned
                else:
                    result[name] = str(value)
            elif desc.var_type == VariableType.IDENTIFIER:
                result[name] = cls.sanitize_identifier(str(value))
            elif desc.var_type in (VariableType.INTEGER, VariableType.BOOLEAN):
                result[name] = value
            elif desc.var_type == VariableType.ENUM:
                result[name] = str(value) if str(value) in desc.allowed_values else ""
            else:
                result[name] = cls.sanitize_text(str(value))

        return result
```

## Solution 3: Safe Template Renderer

```python
import string
from typing import Any, Dict, List, Optional, Tuple


class SafeTemplateRenderer:
    """
    Renders prompt templates using Python's string.Template ($ syntax)
    rather than str.format(), preventing format-string attacks.
    Validates that only allowlisted variable names appear in the template
    and that all required variables are supplied.
    """

    def __init__(
        self,
        template_str: str,
        descriptors: List[TemplateVariableDescriptor],
        sanitizer: TemplateVariableSanitizer,
    ):
        self._raw = template_str
        self._template = string.Template(template_str)
        self._descriptors = descriptors
        self._sanitizer = sanitizer
        self._allowed_names = {d.name for d in descriptors}

    def _extract_template_vars(self) -> list:
        """Extract variable names referenced in the template."""
        import re
        return re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", self._raw)

    def validate_template(self) -> Tuple[bool, List[str]]:
        """Check that template only references allowlisted variables."""
        issues = []
        for var_name in self._extract_template_vars():
            if var_name not in self._allowed_names:
                issues.append(f"template references unknown variable: '{var_name}'")
        return len(issues) == 0, issues

    def render(self, raw_values: Dict[str, Any]) -> Tuple[str, List[str]]:
        """
        Returns (rendered_prompt, list_of_warnings).
        Raises ValueError if required variables are missing or invalid.
        """
        warnings: List[str] = []

        # Validate values against descriptors
        for desc in self._descriptors:
            ok, reason = desc.validate(raw_values.get(desc.name))
            if not ok:
                raise ValueError(f"Template variable validation failed: {reason}")

        # Sanitize
        safe_values = self._sanitizer.sanitize(raw_values, self._descriptors)

        # Check for values that were sanitized significantly
        for name, original in raw_values.items():
            if name in safe_values and str(safe_values[name]) != str(original):
                warnings.append(f"variable '{name}' was sanitized before interpolation")

        try:
            rendered = self._template.safe_substitute(
                {k: str(v) if v is not None else "" for k, v in safe_values.items()}
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Template rendering failed: {exc}") from exc

        return rendered, warnings
```

## Solution 4: Template Injection Audit Logger

```python
import time
from typing import List


class TemplateInjectionAuditLogger:
    """
    Records template rendering events where sanitization was applied
    or validation failed, for security review and pattern analysis.
    """

    def __init__(self, max_records: int = 20000):
        self._max = max_records
        self._records: List[dict] = []

    def record_sanitized(
        self,
        template_id: str,
        warnings: List[str],
        session_id: str = "",
    ) -> None:
        if not warnings:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "event": "sanitized",
            "template_id": template_id,
            "warnings": warnings,
            "session_id": session_id,
        })

    def record_blocked(
        self,
        template_id: str,
        reason: str,
        session_id: str = "",
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "event": "blocked",
            "template_id": template_id,
            "reason": reason,
            "session_id": session_id,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "total_events": len(recent),
            "sanitized": sum(1 for r in recent if r["event"] == "sanitized"),
            "blocked": sum(1 for r in recent if r["event"] == "blocked"),
            "by_template": {
                tid: sum(1 for r in recent if r["template_id"] == tid)
                for tid in {r["template_id"] for r in recent}
            },
        }
```

## Solution 5: Template Registry

```python
from typing import Dict, Optional


class SafeTemplateRegistry:
    """
    Stores named SafeTemplateRenderer instances.
    Ensures templates are validated at registration time, not at render time.
    """

    def __init__(
        self,
        sanitizer: TemplateVariableSanitizer,
        audit_logger: TemplateInjectionAuditLogger,
    ):
        self._sanitizer = sanitizer
        self._audit = audit_logger
        self._templates: Dict[str, SafeTemplateRenderer] = {}

    def register(
        self,
        template_id: str,
        template_str: str,
        descriptors: List[TemplateVariableDescriptor],
    ) -> None:
        renderer = SafeTemplateRenderer(template_str, descriptors, self._sanitizer)
        ok, issues = renderer.validate_template()
        if not ok:
            raise ValueError(
                f"Template '{template_id}' failed validation: {'; '.join(issues)}"
            )
        self._templates[template_id] = renderer

    def render(
        self,
        template_id: str,
        values: Dict[str, Any],
        session_id: str = "",
    ) -> str:
        renderer = self._templates.get(template_id)
        if renderer is None:
            raise KeyError(f"Unknown template: '{template_id}'")
        try:
            rendered, warnings = renderer.render(values)
            if warnings:
                self._audit.record_sanitized(template_id, warnings, session_id)
            return rendered
        except ValueError as exc:
            self._audit.record_blocked(template_id, str(exc), session_id)
            raise
```

## Solution 6: Injection Prevention Dashboard

```python
import time


class TemplateInjectionPreventionDashboard:
    """
    Combines registry health, audit summary, and template inventory
    into a single operational view.
    """

    def __init__(
        self,
        registry: SafeTemplateRegistry,
        audit_logger: TemplateInjectionAuditLogger,
    ):
        self._registry = registry
        self._audit = audit_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "registered_templates": list(self._registry._templates.keys()),
            "template_count": len(self._registry._templates),
            "last_hour_audit": self._audit.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Char Escaping | Allowlist Validation | Safe Renderer | Audit Log | Registry |
|---|---|---|---|---|---|
| TemplateVariableDescriptor | No | Yes (per-variable) | No | No | No |
| TemplateVariableSanitizer | Yes (regex strip) | Via descriptors | No | No | No |
| SafeTemplateRenderer | Via sanitizer | Yes (template vars) | Yes (string.Template) | No | No |
| TemplateInjectionAuditLogger | No | No | No | Yes | No |
| SafeTemplateRegistry | Via renderer | Via renderer | Via renderer | Via logger | Yes |

**Best for production**: Use `string.Template` with `$variable` syntax instead of `str.format()` — `safe_substitute()` leaves unknown variables unreplaced rather than raising, which is safer under adversarial input. Validate every template at registration time with `validate_template()` so injection bugs are caught at deploy time, not at runtime. Register all templates at startup with explicit `TemplateVariableDescriptor` lists — any template that accepts `TEXT` type must have `strip_template_chars=True`. Monitor `blocked` counts in `TemplateInjectionAuditLogger.summary()`: a spike from a single session indicates a systematic probing attempt.
