---
title: "Agent Doesn't Implement Jinja2 Template Injection Prevention in Prompt Builders"
description: "Agents that build prompts using Jinja2 or similar template engines with unsanitized user input are vulnerable to server-side template injection (SSTI): an attacker submits '{{7*7}}' or '{{config}}' as a user message, causing the template to evaluate arbitrary expressions and potentially execute code or expose internal configuration. Implement template injection prevention that escapes all user-supplied content before template rendering and uses sandboxed template environments."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-jinja2-template-injection-prevention-in-prompt-builders
tags: [template-injection, ssti, jinja2, prompt-injection, template-security, sandbox-template]
symptoms:
  - "User input '{{7*7}}' in a message evaluates to '49' in the rendered prompt"
  - "Prompt builder uses Jinja2 render() with user content passed directly as context"
  - "Agent configuration or environment variables accessible via template expressions"
  - "No sandboxing on the template environment used for prompt assembly"
  - "Template syntax in tool arguments or user messages executed at render time"
---

## Why This Happens

Jinja2 templates are powerful — that is the problem. When user input is interpolated into a template using `{{ user_input }}` without escaping, and the input itself contains Jinja2 syntax, the template engine evaluates the user's expressions. In the worst case, Jinja2's default environment allows access to Python internals via `__class__.__mro__` chains, enabling remote code execution. Prevention requires either escaping template metacharacters in user input before rendering, or using Jinja2's `SandboxedEnvironment` which restricts attribute access and disables dangerous builtins.

## Solution 1: Template Metacharacter Escaper

```python
import re
from typing import Any


class Jinja2MetacharacterEscaper:
    """
    Escapes Jinja2 template metacharacters in user-supplied strings
    so they are rendered as literal text rather than evaluated.
    """

    # Characters that start Jinja2 expressions, statements, or comments
    _ESCAPE_MAP = {
        "{": "&#123;",
        "}": "&#125;",
        "%": "&#37;",
        "#": "&#35;",
    }

    # Pattern matching any Jinja2 construct
    _JINJA2_PATTERN = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)

    def escape(self, text: str) -> str:
        """
        Escapes Jinja2 delimiters using HTML entities.
        The resulting string will render as literal text in any Jinja2 template.
        """
        if not isinstance(text, str):
            return str(text)
        # Replace {{ and }} and {% %} with entity-encoded equivalents
        result = text.replace("{", "&#123;").replace("}", "&#125;")
        return result

    def contains_template_syntax(self, text: str) -> bool:
        """Returns True if the text contains Jinja2 template constructs."""
        return bool(self._JINJA2_PATTERN.search(text))

    def sanitize_context(self, context: dict) -> dict:
        """Recursively escapes all string values in a template context dict."""
        sanitized = {}
        for key, value in context.items():
            if isinstance(value, str):
                sanitized[key] = self.escape(value)
            elif isinstance(value, dict):
                sanitized[key] = self.sanitize_context(value)
            elif isinstance(value, list):
                sanitized[key] = [
                    self.escape(v) if isinstance(v, str) else v
                    for v in value
                ]
            else:
                sanitized[key] = value
        return sanitized
```

## Solution 2: Sandboxed Template Environment

```python
from typing import Any, Dict, Optional


class SandboxedPromptTemplateRenderer:
    """
    Renders prompt templates using Jinja2's SandboxedEnvironment,
    which restricts access to dangerous Python internals and builtins.
    All user-supplied context values are additionally escaped before rendering.
    """

    def __init__(self, escaper: Jinja2MetacharacterEscaper):
        self._escaper = escaper
        self._env = self._build_env()

    def _build_env(self):
        try:
            from jinja2.sandbox import SandboxedEnvironment
            env = SandboxedEnvironment(
                autoescape=True,      # HTML-escape all output by default
                undefined=self._strict_undefined(),
            )
            # Remove dangerous globals
            env.globals.clear()
            env.filters = {
                k: v for k, v in env.filters.items()
                if k in {"upper", "lower", "title", "strip", "trim", "truncate",
                         "replace", "e", "escape", "safe", "int", "float", "string"}
            }
            return env
        except ImportError:
            return None

    def _strict_undefined(self):
        try:
            from jinja2 import StrictUndefined
            return StrictUndefined
        except ImportError:
            return None

    def render(
        self,
        template_str: str,
        context: Dict[str, Any],
        user_keys: Optional[list] = None,
    ) -> str:
        """
        Renders a template with context.
        user_keys: list of context keys that came from user input (escaped).
        """
        # Escape all user-supplied values
        safe_context = dict(context)
        if user_keys:
            for key in user_keys:
                if key in safe_context and isinstance(safe_context[key], str):
                    safe_context[key] = self._escaper.escape(safe_context[key])
        else:
            safe_context = self._escaper.sanitize_context(context)

        if self._env is None:
            # Fallback: simple string substitution, no template engine
            return self._simple_render(template_str, safe_context)

        template = self._env.from_string(template_str)
        return template.render(**safe_context)

    @staticmethod
    def _simple_render(template: str, context: dict) -> str:
        """Safe fallback using str.format_map with escaped values."""
        import string

        class SafeDict(dict):
            def __missing__(self, key):
                return f"{{{key}}}"

        return template.format_map(SafeDict(context))
```

## Solution 3: Template Injection Detector

```python
import re
from dataclasses import dataclass
from typing import List


@dataclass
class InjectionDetectionResult:
    contains_injection: bool
    patterns_found: List[str]
    risk_level: str   # "low" | "medium" | "high"


class TemplateInjectionDetector:
    """
    Scans user input for template injection patterns.
    Does not rely on rendering — detects syntactically before escaping.
    """

    _PATTERNS = [
        (r"\{\{.*?\}\}", "jinja2_expression", "high"),
        (r"\{%.*?%\}", "jinja2_statement", "high"),
        (r"\{#.*?#\}", "jinja2_comment", "low"),
        (r"\$\{.*?\}", "dollar_brace_expression", "medium"),  # OGNL/EL
        (r"<%.*?%>", "server_page_tag", "medium"),
        (r"#\{.*?\}", "velocity_expression", "medium"),
        (r"__class__", "python_introspection", "high"),
        (r"__mro__", "python_introspection", "high"),
        (r"__builtins__", "python_builtins_access", "high"),
    ]

    def detect(self, text: str) -> InjectionDetectionResult:
        found = []
        max_risk = "low"
        risk_order = {"low": 0, "medium": 1, "high": 2}

        for pattern, name, risk in self._PATTERNS:
            if re.search(pattern, text, re.DOTALL | re.IGNORECASE):
                found.append(name)
                if risk_order.get(risk, 0) > risk_order.get(max_risk, 0):
                    max_risk = risk

        return InjectionDetectionResult(
            contains_injection=len(found) > 0,
            patterns_found=found,
            risk_level=max_risk if found else "none",
        )
```

## Solution 4: Safe Prompt Builder

```python
import time
from typing import Any, Callable, Dict, List, Optional


class TemplateInjectionAttemptError(Exception):
    def __init__(self, patterns: List[str]):
        super().__init__(f"template injection detected: {', '.join(patterns)}")
        self.patterns = patterns


class SafePromptBuilder:
    """
    Builds prompts from templates with full SSTI prevention:
    1. Detects injection patterns in user inputs
    2. Escapes metacharacters before rendering
    3. Uses sandboxed template environment
    """

    def __init__(
        self,
        renderer: SandboxedPromptTemplateRenderer,
        detector: TemplateInjectionDetector,
        block_on_high_risk: bool = True,
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._renderer = renderer
        self._detector = detector
        self._block_on_high = block_on_high_risk
        self._audit = audit_fn or (lambda _: None)

    def build(
        self,
        template: str,
        context: Dict[str, Any],
        user_keys: Optional[List[str]] = None,
    ) -> str:
        user_keys = user_keys or []

        for key in user_keys:
            value = context.get(key, "")
            if not isinstance(value, str):
                continue
            result = self._detector.detect(value)
            if result.contains_injection:
                self._audit({
                    "ts": time.time(),
                    "event": "template_injection_detected",
                    "key": key,
                    "risk_level": result.risk_level,
                    "patterns": result.patterns_found,
                })
                if self._block_on_high and result.risk_level == "high":
                    raise TemplateInjectionAttemptError(result.patterns_found)

        return self._renderer.render(template, context, user_keys=user_keys)
```

## Solution 5: Template Injection Audit Log

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class TemplateInjectionAuditLog:
    """
    Records template injection detection events for security investigation.
    """

    def __init__(self, max_records: int = 10_000):
        self._records: Deque[dict] = deque(maxlen=max_records)
        self._lock = Lock()

    def record(self, event: dict) -> None:
        with self._lock:
            self._records.append({**event, "logged_at": time.time()})

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.get("logged_at", 0) >= cutoff]
        if not recent:
            return {"detections": 0}

        by_risk: dict = {}
        for r in recent:
            risk = r.get("risk_level", "unknown")
            by_risk[risk] = by_risk.get(risk, 0) + 1

        return {
            "window_seconds": window_seconds,
            "detections": len(recent),
            "by_risk_level": by_risk,
            "high_risk_count": by_risk.get("high", 0),
        }
```

## Solution 6: Template Security Dashboard

```python
import time


class TemplateSecurityDashboard:
    """
    Renders injection detection statistics, environment configuration,
    and risk distribution for security monitoring.
    """

    def __init__(
        self,
        audit_log: TemplateInjectionAuditLog,
        detector: TemplateInjectionDetector,
    ):
        self._log = audit_log
        self._detector = detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "protection": {
                "sandboxed_environment": True,
                "metacharacter_escaping": True,
                "injection_pattern_count": len(self._detector._PATTERNS),
                "block_on_high_risk": True,
            },
            "detections_1h": self._log.summary(3600.0),
            "detections_24h": self._log.summary(86400.0),
        }
```

## Comparison

| Approach | Metacharacter Escaping | Sandboxed Environment | Pattern Detection | Request Blocking | Audit |
|---|---|---|---|---|---|
| Jinja2MetacharacterEscaper | Yes | No | No | No | No |
| SandboxedPromptTemplateRenderer | Via escaper | Yes (Jinja2 sandbox) | No | No | No |
| TemplateInjectionDetector | No | No | Yes (9 patterns) | No | No |
| SafePromptBuilder | Via renderer | Via renderer | Via detector | Yes (high risk) | Yes |
| TemplateInjectionAuditLog | No | No | No | No | Yes |

**Best for production**: Use `SandboxedEnvironment` with `autoescape=True` as the baseline — this prevents the most severe SSTI payloads even without explicit escaping. Always pass user-controlled values through `Jinja2MetacharacterEscaper.escape()` as a defense-in-depth layer. Block on high-risk patterns (`__class__`, `__mro__`, Jinja2 expressions) immediately; log and monitor medium-risk patterns. Alert when `high_risk_count` from the audit log exceeds 5 in an hour — this is active SSTI probing, not accidental template syntax from users.
