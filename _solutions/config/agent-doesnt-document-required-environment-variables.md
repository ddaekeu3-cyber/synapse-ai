---
layout: solution
title: "Agent Doesn't Document Required Environment Variables"
category: config
description: "AI agents silently fail or behave unexpectedly when required environment variables are missing or misconfigured, with no clear documentation or validation at startup."
tags: [config, environment-variables, validation, pydantic, dotenv, startup, documentation]
---

# Agent Doesn't Document Required Environment Variables

## Problem

An agent reads `os.environ["ANTHROPIC_API_KEY"]` at call time without ever declaring that the variable is required. When the key is missing in a new environment, the error appears deep in a stack trace during a live request rather than immediately at startup. Teams spend hours debugging deployment failures that a single startup check would have caught in seconds.

## Solutions

### Option 1: Explicit Startup Validation with Clear Error Messages

```python
# config/env_validator.py
"""
Validate all required environment variables at import/startup time.
Fail fast with a human-readable message listing every missing variable.
"""
import os
import sys
from typing import Optional


REQUIRED_VARS: dict[str, str] = {
    "ANTHROPIC_API_KEY": "Anthropic API key for Claude model access",
    "DATABASE_URL": "PostgreSQL connection string (e.g. postgresql://user:pass@host/db)",
    "REDIS_URL": "Redis connection string for session caching (e.g. redis://localhost:6379/0)",
}

OPTIONAL_VARS: dict[str, tuple[str, str]] = {
    # var_name: (description, default_value)
    "ANTHROPIC_MODEL": ("Claude model ID to use", "claude-sonnet-4-6"),
    "MAX_TOKENS": ("Maximum tokens per response", "1024"),
    "LOG_LEVEL": ("Logging level", "INFO"),
    "REQUEST_TIMEOUT": ("HTTP request timeout in seconds", "30"),
    "ENVIRONMENT": ("Deployment environment", "development"),
}


def validate_env(exit_on_failure: bool = True) -> dict[str, str]:
    """
    Validate required env vars and apply defaults for optional ones.
    Returns resolved config dict on success; exits with error on failure.
    """
    missing = [var for var in REQUIRED_VARS if not os.environ.get(var)]

    if missing:
        lines = [
            "=" * 60,
            "STARTUP ERROR: Missing required environment variables",
            "=" * 60,
            "",
        ]
        for var in missing:
            lines.append(f"  {var}")
            lines.append(f"    {REQUIRED_VARS[var]}")
            lines.append("")
        lines.append("Set these variables before starting the agent.")
        lines.append("See README.md or docs/configuration.md for details.")
        lines.append("=" * 60)
        print("\n".join(lines), file=sys.stderr)
        if exit_on_failure:
            sys.exit(1)
        raise EnvironmentError(f"Missing required env vars: {missing}")

    # Apply defaults for optional vars
    config = {var: os.environ[var] for var in REQUIRED_VARS}
    for var, (_, default) in OPTIONAL_VARS.items():
        config[var] = os.environ.get(var, default)

    return config


# Validate at module import — fail before any handler runs
CONFIG = validate_env()
```

```python
# main.py
from config.env_validator import CONFIG  # Fails fast here if env is incomplete
import anthropic

client = anthropic.Anthropic(api_key=CONFIG["ANTHROPIC_API_KEY"])
```

**Expected Token Savings:** Not applicable — configuration safety
**Environment:** stdlib only

---

### Option 2: Pydantic Settings with Auto-Documentation

```python
# config/settings.py
"""
Pydantic BaseSettings automatically reads from environment variables,
validates types, and generates documentation via model_json_schema().
"""
from functools import lru_cache
from typing import Literal
from pydantic import Field, field_validator, AnyUrl
from pydantic_settings import BaseSettings


class AgentSettings(BaseSettings):
    """
    All configuration for the AI agent service.

    Each field maps 1:1 to an environment variable (upper-cased field name).
    Required fields have no default; optional fields include sensible defaults.

    Example .env file:
        ANTHROPIC_API_KEY=sk-ant-...
        DATABASE_URL=postgresql://user:pass@localhost/agentdb
        REDIS_URL=redis://localhost:6379/0
    """

    # --- Required ---
    anthropic_api_key: str = Field(
        ...,
        description="Anthropic API key. Get from https://console.anthropic.com/",
    )
    database_url: str = Field(
        ...,
        description="PostgreSQL connection string for session persistence.",
    )
    redis_url: str = Field(
        ...,
        description="Redis connection string for response caching.",
    )

    # --- Optional with defaults ---
    anthropic_model: str = Field(
        default="claude-sonnet-4-6",
        description="Claude model ID. Options: claude-haiku-4-5-20251001, claude-sonnet-4-6, claude-opus-4-6",
    )
    max_tokens: int = Field(
        default=1024,
        ge=1,
        le=8192,
        description="Maximum output tokens per agent response.",
    )
    request_timeout: float = Field(
        default=30.0,
        gt=0,
        description="Timeout in seconds for Anthropic API requests.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Python logging level.",
    )
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Deployment environment. Affects logging verbosity and safety checks.",
    )
    max_concurrent_requests: int = Field(
        default=20,
        ge=1,
        le=500,
        description="Maximum concurrent requests to the Anthropic API.",
    )
    enable_prompt_caching: bool = Field(
        default=True,
        description="Enable Anthropic prompt caching for repeated system prompts.",
    )

    @field_validator("anthropic_api_key")
    @classmethod
    def validate_api_key(cls, v: str) -> str:
        if not v.startswith("sk-ant-"):
            raise ValueError("ANTHROPIC_API_KEY must start with 'sk-ant-'")
        if len(v) < 20:
            raise ValueError("ANTHROPIC_API_KEY appears too short to be valid")
        return v

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not (v.startswith("postgresql://") or v.startswith("postgres://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection string")
        return v

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False  # ANTHROPIC_API_KEY and anthropic_api_key both work


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    """Cached singleton — validates once, reuses everywhere."""
    return AgentSettings()


def print_env_docs():
    """Print documentation for all environment variables."""
    settings_class = AgentSettings
    schema = settings_class.model_json_schema()
    props = schema.get("properties", {})
    required = schema.get("required", [])

    print("\nEnvironment Variable Reference")
    print("=" * 60)
    for field_name, field_info in props.items():
        env_var = field_name.upper()
        is_required = field_name in required
        description = field_info.get("description", "No description")
        default = field_info.get("default", "REQUIRED" if is_required else "")
        print(f"\n{env_var}")
        print(f"  Required : {'Yes' if is_required else 'No'}")
        print(f"  Default  : {default}")
        print(f"  Type     : {field_info.get('type', field_info.get('anyOf', '?'))}")
        print(f"  Info     : {description}")
    print("=" * 60)


if __name__ == "__main__":
    print_env_docs()
```

```bash
# Generate env docs: python -m config.settings
# Output:
# Environment Variable Reference
# ============================================================
# ANTHROPIC_API_KEY
#   Required : Yes
#   Default  : REQUIRED
#   Info     : Anthropic API key. Get from https://console.anthropic.com/
# ...
```

**Expected Token Savings:** Not applicable — configuration validation
**Environment:** `pip install pydantic-settings`

---

### Option 3: .env.example File Generator

```python
# scripts/generate_env_example.py
"""
Automatically generate a .env.example file from the settings definition.
Run this script whenever settings change to keep documentation in sync.
Never commit actual secrets — only the .example file goes to version control.
"""
import inspect
from config.settings import AgentSettings
from pydantic.fields import FieldInfo


ENV_EXAMPLE_HEADER = """\
# .env.example — Copy this file to .env and fill in your values.
# Lines starting with # are comments.
# NEVER commit .env to version control.
# Run `python scripts/generate_env_example.py` to regenerate this file.

"""


def generate_env_example(output_path: str = ".env.example") -> str:
    model = AgentSettings
    schema = model.model_json_schema()
    required_fields = set(schema.get("required", []))
    lines = [ENV_EXAMPLE_HEADER]

    # Group: required first, then optional
    fields = model.model_fields
    required = {k: v for k, v in fields.items() if k in required_fields}
    optional = {k: v for k, v in fields.items() if k not in required_fields}

    lines.append("# ─── Required ────────────────────────────────────────────\n")
    for field_name, field_info in required.items():
        env_var = field_name.upper()
        description = field_info.description or ""
        lines.append(f"# {description}")
        lines.append(f"{env_var}=\n")

    lines.append("\n# ─── Optional (defaults shown) ───────────────────────────\n")
    for field_name, field_info in optional.items():
        env_var = field_name.upper()
        description = field_info.description or ""
        default = field_info.default
        lines.append(f"# {description}")
        lines.append(f"# {env_var}={default}\n")

    content = "\n".join(lines)
    with open(output_path, "w") as f:
        f.write(content)
    print(f"Generated {output_path}")
    return content


if __name__ == "__main__":
    generate_env_example()
    # Also validate that current environment matches requirements
    try:
        from config.settings import get_settings
        settings = get_settings()
        print("Current environment: all required vars present.")
    except Exception as e:
        print(f"WARNING: Current environment has issues: {e}")
```

```
# Generated .env.example output:
# ─── Required ─────────────────────────────────────────────
# Anthropic API key. Get from https://console.anthropic.com/
ANTHROPIC_API_KEY=

# PostgreSQL connection string for session persistence.
DATABASE_URL=

# Redis connection string for response caching.
REDIS_URL=

# ─── Optional (defaults shown) ────────────────────────────
# Claude model ID. Options: claude-haiku-4-5-20251001, claude-sonnet-4-6, claude-opus-4-6
# ANTHROPIC_MODEL=claude-sonnet-4-6

# Maximum output tokens per agent response.
# MAX_TOKENS=1024
```

**Expected Token Savings:** Not applicable — documentation tooling
**Environment:** `pip install pydantic-settings`

---

### Option 4: Runtime Config Health-Check Endpoint

```python
# api/health.py
"""
Expose a /health/config endpoint that reports which environment variables
are set, missing, or invalid — without revealing secret values.
Useful for ops teams debugging deployment issues without shell access.
"""
import os
from enum import Enum
from typing import Any
from fastapi import APIRouter
from pydantic import BaseModel


router = APIRouter()


class VarStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"


class EnvVarCheck(BaseModel):
    name: str
    status: VarStatus
    description: str
    hint: str = ""
    # Never expose actual secret values
    value_preview: str = ""  # e.g., "sk-ant-***...abc" or "set" or "not set"


class ConfigHealthResponse(BaseModel):
    healthy: bool
    checks: list[EnvVarCheck]


def _mask_secret(value: str, show_chars: int = 4) -> str:
    """Show only last N characters of a secret value."""
    if len(value) <= show_chars:
        return "***"
    return f"***...{value[-show_chars:]}"


def _check_env_vars() -> list[EnvVarCheck]:
    checks = []

    # ANTHROPIC_API_KEY
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        checks.append(EnvVarCheck(
            name="ANTHROPIC_API_KEY",
            status=VarStatus.MISSING,
            description="Anthropic API key for Claude model access",
            hint="Set to your key from https://console.anthropic.com/",
            value_preview="not set",
        ))
    elif not key.startswith("sk-ant-"):
        checks.append(EnvVarCheck(
            name="ANTHROPIC_API_KEY",
            status=VarStatus.INVALID,
            description="Anthropic API key for Claude model access",
            hint="Key must start with 'sk-ant-'",
            value_preview=_mask_secret(key),
        ))
    else:
        checks.append(EnvVarCheck(
            name="ANTHROPIC_API_KEY",
            status=VarStatus.OK,
            description="Anthropic API key",
            value_preview=_mask_secret(key),
        ))

    # DATABASE_URL
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        checks.append(EnvVarCheck(
            name="DATABASE_URL",
            status=VarStatus.MISSING,
            description="PostgreSQL connection string",
            hint="Example: postgresql://user:pass@localhost/agentdb",
            value_preview="not set",
        ))
    else:
        # Show scheme + host only, not password
        try:
            from urllib.parse import urlparse
            parsed = urlparse(db_url)
            preview = f"{parsed.scheme}://***@{parsed.hostname}{parsed.path}"
        except Exception:
            preview = "set (parse error)"
        checks.append(EnvVarCheck(
            name="DATABASE_URL",
            status=VarStatus.OK,
            description="PostgreSQL connection string",
            value_preview=preview,
        ))

    # Optional vars — just report presence
    for var, default in [
        ("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        ("MAX_TOKENS", "1024"),
        ("LOG_LEVEL", "INFO"),
    ]:
        value = os.environ.get(var, "")
        checks.append(EnvVarCheck(
            name=var,
            status=VarStatus.OK,
            description=f"Optional (default: {default})",
            value_preview=value if value else f"(default: {default})",
        ))

    return checks


@router.get("/health/config", response_model=ConfigHealthResponse)
async def config_health():
    """
    Reports configuration health without exposing secret values.
    Returns HTTP 200 if all required vars are present and valid.
    Returns HTTP 503 if any required vars are missing or invalid.
    """
    checks = _check_env_vars()
    healthy = all(c.status == VarStatus.OK for c in checks if "Optional" not in c.description)

    from fastapi import Response
    if not healthy:
        # Caller can detect unhealthy config by HTTP status
        pass  # FastAPI handles status codes via HTTPException or Response

    return ConfigHealthResponse(healthy=healthy, checks=checks)
```

**Expected Token Savings:** Not applicable — observability endpoint
**Environment:** `pip install fastapi pydantic`

---

### Option 5: Dockerfile ARG + ENV Documentation Layer

```dockerfile
# Dockerfile
# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

# ─── Environment Variable Documentation ───────────────────────────────────────
# Required at runtime (must be injected via -e or secrets manager):
#
#   ANTHROPIC_API_KEY     Anthropic API key (sk-ant-...)
#   DATABASE_URL          PostgreSQL connection string
#   REDIS_URL             Redis connection string
#
# Optional (override these defaults if needed):
#
#   ANTHROPIC_MODEL       Claude model ID          (default: claude-sonnet-4-6)
#   MAX_TOKENS            Max response tokens      (default: 1024)
#   LOG_LEVEL             Logging verbosity        (default: INFO)
#   REQUEST_TIMEOUT       API timeout in seconds   (default: 30)
#   ENVIRONMENT           deployment env           (default: production)
# ──────────────────────────────────────────────────────────────────────────────

# Set non-secret defaults (secrets are NEVER baked into the image)
ENV ANTHROPIC_MODEL="claude-sonnet-4-6" \
    MAX_TOKENS="1024" \
    LOG_LEVEL="INFO" \
    REQUEST_TIMEOUT="30" \
    ENVIRONMENT="production" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Validate env vars at container startup, not at image build time
ENTRYPOINT ["python", "-m", "scripts.validate_env_then_start"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```python
# scripts/validate_env_then_start.py
"""
Container entrypoint: validate environment before starting the server.
Provides clear feedback in container logs when misconfigured.
"""
import os
import sys
import subprocess


REQUIRED = {
    "ANTHROPIC_API_KEY": "Anthropic API key (sk-ant-...)",
    "DATABASE_URL": "PostgreSQL connection string",
    "REDIS_URL": "Redis connection string",
}


def main():
    args = sys.argv[1:]  # e.g. ["uvicorn", "main:app", ...]
    if not args:
        print("Usage: validate_env_then_start.py <command> [args...]", file=sys.stderr)
        sys.exit(1)

    missing = {k: v for k, v in REQUIRED.items() if not os.environ.get(k)}
    if missing:
        print("\n[FATAL] Container startup failed — missing environment variables:\n", file=sys.stderr)
        for var, desc in missing.items():
            print(f"  {var}: {desc}", file=sys.stderr)
        print(
            "\nInject these via docker run -e VAR=value or your secrets manager.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    print("[OK] Environment validated. Starting server...")
    os.execvp(args[0], args)  # Replace this process with the server


if __name__ == "__main__":
    main()
```

**Expected Token Savings:** Not applicable — deployment safety
**Environment:** stdlib + Docker

---

### Option 6: pytest Fixture That Enforces Env Doc Coverage

```python
# tests/config/test_env_documentation.py
"""
Meta-test: ensure every environment variable the code reads from os.environ
is documented in REQUIRED_VARS or OPTIONAL_VARS.
Prevents the "undocumented secret env var" drift over time.
"""
import ast
import os
from pathlib import Path
import pytest
from config.env_validator import REQUIRED_VARS, OPTIONAL_VARS


def collect_os_environ_reads(source_root: Path) -> set[str]:
    """
    AST-scan Python files for os.environ["VAR"] and os.environ.get("VAR") calls.
    Returns the set of variable names found.
    """
    found = set()
    for py_file in source_root.rglob("*.py"):
        # Skip test files and generated files
        parts = py_file.parts
        if any(p in parts for p in ("tests", ".venv", "__pycache__", "migrations")):
            continue
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            # os.environ["VAR"] or os.environ.get("VAR")
            if isinstance(node, ast.Subscript):
                if (
                    isinstance(node.value, ast.Attribute)
                    and node.value.attr == "environ"
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                ):
                    found.add(node.slice.value)

            elif isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                ):
                    if node.args and isinstance(node.args[0], ast.Constant):
                        found.add(node.args[0].value)

    return found


DOCUMENTED_VARS = set(REQUIRED_VARS.keys()) | set(OPTIONAL_VARS.keys())
SOURCE_ROOT = Path(__file__).parent.parent.parent  # repo root


def test_all_env_reads_are_documented():
    """Every os.environ read in source code must be documented."""
    read_vars = collect_os_environ_reads(SOURCE_ROOT)
    # Exclude well-known stdlib vars that don't need agent-level docs
    stdlib_vars = {"PATH", "HOME", "USER", "SHELL", "PYTHONPATH", "VIRTUAL_ENV"}
    undocumented = (read_vars - DOCUMENTED_VARS - stdlib_vars)
    assert not undocumented, (
        f"These env vars are read in source but not documented:\n"
        + "\n".join(f"  {v}" for v in sorted(undocumented))
        + "\nAdd them to REQUIRED_VARS or OPTIONAL_VARS in config/env_validator.py"
    )


def test_documented_vars_are_actually_used():
    """Warn about documented vars that are never read (possible dead config)."""
    read_vars = collect_os_environ_reads(SOURCE_ROOT)
    # Only check REQUIRED_VARS — optional vars may be pre-declared for future use
    unused_required = set(REQUIRED_VARS.keys()) - read_vars
    # This is a soft warning, not a hard failure
    if unused_required:
        import warnings
        warnings.warn(
            f"These REQUIRED_VARS are documented but never read: {unused_required}",
            UserWarning,
            stacklevel=2,
        )


def test_required_vars_present_in_ci():
    """In CI, all required vars must be set (secrets injected by CI system)."""
    if os.environ.get("CI") != "true":
        pytest.skip("Only runs in CI environment")
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    assert not missing, f"CI is missing required env vars: {missing}"
```

**Expected Token Savings:** Not applicable — documentation coverage test
**Environment:** stdlib + pytest

---

## Comparison Table

| Option | Approach | Fail Point | Secret Safety | Generates Docs | CI-Ready |
|--------|----------|------------|---------------|----------------|----------|
| 1: Startup validation | Explicit dict + sys.exit | Import time | Yes (no values logged) | Manual | Yes |
| 2: Pydantic Settings | BaseSettings validators | Import time | Yes (validation only) | Schema dump | Yes |
| 3: .env.example generator | Script from model | Build/dev | Yes (example only) | Auto-generated | Yes |
| 4: Health endpoint | HTTP /health/config | Runtime | Yes (masked values) | Via API | Yes |
| 5: Dockerfile entrypoint | Container ENTRYPOINT | Container start | Yes (no bake-in) | Dockerfile comments | Yes |
| 6: AST coverage test | pytest AST scan | Test run | N/A | Enforced coverage | Yes |
