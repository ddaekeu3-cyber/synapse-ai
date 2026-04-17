---
title: "Agent Doesn't Implement Environment Variable Secret Detection at Startup"
description: "Agents that load environment variables at startup without validation may silently run with missing, empty, or malformed credentials — using default or empty values that expose internal systems, bypass authentication, or produce unpredictable behavior. Implement startup secret detection that validates required environment variables, detects patterns that look like secrets accidentally set to placeholder values, and fails fast with actionable error messages before the agent begins serving requests."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-environment-variable-secret-detection-at-startup
tags: [environment-variables, secret-detection, startup-validation, credential-validation, fail-fast, configuration-security]
symptoms:
  - "Agent starts with PLACEHOLDER or CHANGEME credentials without warning"
  - "Missing API key environment variable causes runtime failures instead of startup failure"
  - "Credentials set to empty string silently disable authentication"
  - "No validation that secret values meet minimum length or format requirements"
  - "Startup logs do not indicate which secrets were loaded or their validation status"
---

## Why This Happens

Environment variables are read with `os.getenv()` or `os.environ[]` throughout the codebase — in the database client constructor, the API client class, the webhook handler. Each call either falls back to a default or raises a KeyError at first use. There is no central startup gate that validates all required secrets before any client is initialized. Placeholder values like `your-api-key-here` or `CHANGEME` are accepted as valid strings, bypassing format checks that would catch them. A centralized startup validator reads all required secrets at process start, validates their format and presence, and aborts before the server begins accepting requests.

## Solution 1: Secret Specification

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional


class SecretSeverity(str, Enum):
    REQUIRED = "required"       # absence = startup failure
    RECOMMENDED = "recommended" # absence = warning only
    OPTIONAL = "optional"       # absence = info log only


@dataclass
class SecretSpec:
    env_var: str
    description: str
    severity: SecretSeverity = SecretSeverity.REQUIRED
    min_length: int = 8
    max_length: int = 2048
    pattern: Optional[str] = None         # regex the value must match
    placeholder_patterns: List[str] = field(default_factory=list)
    # Values that look like unset placeholders
    validator: Optional[Callable[[str], bool]] = None
    redact_in_logs: bool = True

    def __post_init__(self) -> None:
        if not self.placeholder_patterns:
            self.placeholder_patterns = [
                r"^(CHANGEME|your[_-].+|placeholder|example|test|dummy|fake|todo)",
                r"^xxx+$",
                r"^<.+>$",
                r"^\.\.\.+$",
            ]
```

## Solution 2: Default Secret Registry

```python
from typing import List


def default_agent_secret_specs() -> List[SecretSpec]:
    return [
        SecretSpec(
            env_var="ANTHROPIC_API_KEY",
            description="Anthropic API key for LLM calls",
            severity=SecretSeverity.REQUIRED,
            min_length=20,
            pattern=r"^sk-ant-",
        ),
        SecretSpec(
            env_var="OPENAI_API_KEY",
            description="OpenAI API key",
            severity=SecretSeverity.OPTIONAL,
            min_length=20,
            pattern=r"^sk-",
        ),
        SecretSpec(
            env_var="DATABASE_URL",
            description="Database connection string",
            severity=SecretSeverity.RECOMMENDED,
            min_length=10,
            redact_in_logs=True,
        ),
        SecretSpec(
            env_var="WEBHOOK_SECRET",
            description="Webhook HMAC signing secret",
            severity=SecretSeverity.RECOMMENDED,
            min_length=32,
        ),
        SecretSpec(
            env_var="JWT_SECRET_KEY",
            description="JWT signing key",
            severity=SecretSeverity.RECOMMENDED,
            min_length=32,
        ),
        SecretSpec(
            env_var="REDIS_URL",
            description="Redis connection URL",
            severity=SecretSeverity.OPTIONAL,
            min_length=5,
            redact_in_logs=True,
        ),
    ]
```

## Solution 3: Secret Validator

```python
import os
import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class SecretValidationResult:
    spec: SecretSpec
    present: bool
    value_length: Optional[int]
    issues: List[str]
    passed: bool

    def redacted_value(self) -> str:
        if not self.present:
            return "(not set)"
        val = os.environ.get(self.spec.env_var, "")
        if not self.spec.redact_in_logs or len(val) <= 8:
            return "(too short to redact)"
        return val[:4] + "****" + val[-4:]


class EnvironmentSecretValidator:
    """
    Validates all registered secret specs against the current environment.
    Returns a validation result for each spec.
    """

    def validate_all(self, specs: List[SecretSpec]) -> List[SecretValidationResult]:
        return [self.validate_one(spec) for spec in specs]

    def validate_one(self, spec: SecretSpec) -> SecretValidationResult:
        issues = []
        value = os.environ.get(spec.env_var)

        if value is None or value == "":
            return SecretValidationResult(
                spec=spec,
                present=False,
                value_length=None,
                issues=[f"Not set (env var '{spec.env_var}' is missing or empty)"],
                passed=(spec.severity == SecretSeverity.OPTIONAL),
            )

        # Length checks
        if len(value) < spec.min_length:
            issues.append(f"Too short: {len(value)} chars (minimum {spec.min_length})")
        if len(value) > spec.max_length:
            issues.append(f"Too long: {len(value)} chars (maximum {spec.max_length})")

        # Placeholder check
        for pat in spec.placeholder_patterns:
            if re.search(pat, value, re.IGNORECASE):
                issues.append(f"Looks like a placeholder value (matched '{pat}')")
                break

        # Pattern check
        if spec.pattern and not re.search(spec.pattern, value):
            issues.append(f"Does not match required format: '{spec.pattern}'")

        # Custom validator
        if spec.validator:
            try:
                if not spec.validator(value):
                    issues.append("Failed custom validation check")
            except Exception as exc:
                issues.append(f"Custom validator raised: {exc}")

        passed = len(issues) == 0
        return SecretValidationResult(
            spec=spec,
            present=True,
            value_length=len(value),
            issues=issues,
            passed=passed,
        )
```

## Solution 4: Startup Gate

```python
import sys
from typing import List, Optional


class SecretStartupGate:
    """
    Runs all secret validations at agent startup.
    Fails fast (raises SystemExit) if any REQUIRED secret is invalid.
    Emits warnings for RECOMMENDED secrets and info for OPTIONAL.
    """

    def __init__(
        self,
        validator: EnvironmentSecretValidator,
        specs: List[SecretSpec],
        logger: Optional[object] = None,
    ):
        self._validator = validator
        self._specs = specs
        self._logger = logger

    def _log(self, level: str, message: str) -> None:
        if self._logger:
            getattr(self._logger, level, print)(message)
        else:
            print(f"[{level.upper()}] {message}")

    def check(self) -> List[SecretValidationResult]:
        results = self._validator.validate_all(self._specs)
        failures = []
        warnings = []

        for result in results:
            spec = result.spec
            if result.passed:
                self._log(
                    "info",
                    f"[SECRET OK] {spec.env_var} = {result.redacted_value()}",
                )
                continue

            msg = f"[SECRET {'MISSING' if not result.present else 'INVALID'}] {spec.env_var}: " + "; ".join(result.issues)

            if spec.severity == SecretSeverity.REQUIRED:
                self._log("error", msg)
                failures.append(result)
            elif spec.severity == SecretSeverity.RECOMMENDED:
                self._log("warning", msg)
                warnings.append(result)
            else:
                self._log("info", f"[SECRET OPTIONAL] {spec.env_var} not set — skipping")

        if failures:
            self._log(
                "error",
                f"Startup aborted: {len(failures)} required secret(s) failed validation. "
                "Fix the above errors and restart.",
            )
            raise SystemExit(1)

        return results
```

## Solution 5: Secret Rotation Watcher

```python
import os
import time
from threading import Lock, Thread
from typing import Callable, List, Optional


class SecretRotationWatcher:
    """
    Periodically re-reads environment-backed secrets (or file-backed secrets)
    and calls a callback when a value changes — enabling zero-downtime rotation.
    Only applies to secrets that can change without restart (e.g., from a secrets manager).
    """

    def __init__(
        self,
        env_vars: List[str],
        on_change: Callable[[str, str], None],
        poll_interval_seconds: float = 60.0,
    ):
        self._vars = env_vars
        self._on_change = on_change
        self._interval = poll_interval_seconds
        self._lock = Lock()
        self._last_values = {v: os.environ.get(v, "") for v in env_vars}
        self._running = False
        self._thread: Optional[Thread] = None

    def _check(self) -> None:
        for var in self._vars:
            current = os.environ.get(var, "")
            with self._lock:
                prev = self._last_values.get(var, "")
                if current != prev:
                    self._last_values[var] = current
                    try:
                        self._on_change(var, current)
                    except Exception:
                        pass

    def start(self) -> None:
        self._running = True
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while self._running:
            time.sleep(self._interval)
            self._check()

    def stop(self) -> None:
        self._running = False
```

## Solution 6: Startup Secret Audit Report

```python
import time
from typing import List


class StartupSecretAuditReport:
    """
    Produces a structured audit report of the startup secret validation
    for compliance and operational records.
    """

    def generate(self, results: List[SecretValidationResult]) -> dict:
        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed]
        required_failed = [r for r in failed if r.spec.severity == SecretSeverity.REQUIRED]

        return {
            "generated_at": time.time(),
            "total_secrets_checked": len(results),
            "passed": len(passed),
            "failed": len(failed),
            "required_failures": len(required_failed),
            "startup_safe": len(required_failed) == 0,
            "details": [
                {
                    "env_var": r.spec.env_var,
                    "severity": r.spec.severity.value,
                    "present": r.present,
                    "passed": r.passed,
                    "issues": r.issues,
                }
                for r in results
            ],
        }
```

## Comparison

| Approach | Presence Check | Format Validation | Placeholder Detection | Fail Fast | Rotation Watch |
|---|---|---|---|---|---|
| EnvironmentSecretValidator | Yes | Yes (regex+custom) | Yes (pattern list) | No | No |
| SecretStartupGate | Via validator | Via validator | Via validator | Yes (SystemExit) | No |
| SecretRotationWatcher | No | No | No | No | Yes |
| StartupSecretAuditReport | No | No | No | No | No (audit only) |

**Best for production**: Call `SecretStartupGate.check()` as the very first operation in your application entry point — before any client is initialized, before any port is bound. This guarantees the process never enters a partially-initialized state with missing credentials. Treat `RECOMMENDED` secrets as `REQUIRED` in production environments by setting `severity=SecretSeverity.REQUIRED` via environment-specific config overrides. Export `StartupSecretAuditReport` to your structured log on every startup: a history of audit reports makes it easy to identify when a credential was first detected as misconfigured relative to a deployment timestamp.
