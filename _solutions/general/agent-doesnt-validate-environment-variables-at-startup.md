---
layout: solution
title: "Agent Doesn't Validate Environment Variables at Startup"
category: general
description: "Agent crashes deep inside a task when a required environment variable is missing, losing all progress and leaving the user with a cryptic error."
tags: [startup, configuration, environment, fail-fast, reliability]
---

## Symptom

The agent starts successfully but crashes mid-task with errors like `KeyError: 'ANTHROPIC_API_KEY'`, `NoneType has no attribute 'split'`, or `Invalid API key` after already completing several expensive steps. Each restart fails at a different point depending on which variable is accessed first.

## Root Cause

The agent reads environment variables lazily — only when first needed. A missing `ANTHROPIC_API_KEY` isn't discovered until the first API call. A missing `DATABASE_URL` isn't caught until the first DB query. By then, partial work may have been committed, side effects triggered, and context window filled. The agent has no startup gate that validates configuration before any work begins.

## Fix

### Option 1: Synchronous startup validator with explicit error messages

```python
import os
import sys
import anthropic

REQUIRED_ENV_VARS = {
    "ANTHROPIC_API_KEY": "Anthropic API key for model calls",
    "DATABASE_URL": "PostgreSQL connection string",
    "REDIS_URL": "Redis URL for task queue",
    "APP_SECRET_KEY": "Secret key for session signing",
}

OPTIONAL_ENV_VARS = {
    "LOG_LEVEL": ("INFO", "Logging verbosity: DEBUG, INFO, WARNING, ERROR"),
    "MAX_RETRIES": ("3", "Number of API retry attempts"),
    "REQUEST_TIMEOUT": ("30", "HTTP request timeout in seconds"),
    "MODEL": ("claude-sonnet-4-6", "Claude model to use"),
}


def validate_environment() -> dict:
    """Validate all required env vars before starting. Exit on failure."""
    errors = []
    config = {}

    # Check required vars
    for var, description in REQUIRED_ENV_VARS.items():
        value = os.environ.get(var)
        if not value:
            errors.append(f"  ✗ {var}: {description}")
        elif not value.strip():
            errors.append(f"  ✗ {var}: set but empty ({description})")
        else:
            config[var] = value

    if errors:
        print("STARTUP ERROR: Missing required environment variables:", file=sys.stderr)
        for error in errors:
            print(error, file=sys.stderr)
        print("\nSet these variables and restart.", file=sys.stderr)
        sys.exit(1)

    # Apply defaults for optional vars
    for var, (default, description) in OPTIONAL_ENV_VARS.items():
        config[var] = os.environ.get(var, default)

    print(f"✓ Environment validated ({len(config)} variables loaded)")
    return config


def run_agent(user_message: str) -> str:
    config = validate_environment()  # Fail fast here, before any work

    client = anthropic.Anthropic(api_key=config["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=config["MODEL"],
        max_tokens=1024,
        system="You are a helpful assistant.",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


if __name__ == "__main__":
    result = run_agent("What is the capital of France?")
    print(result)
```

**Expected Token Savings:** Indirect — prevents wasted tokens from partial runs that fail mid-task and must restart.
**Environment:** Any deployment; essential for containerized and serverless environments.

---

### Option 2: Pydantic settings model with type coercion and validation

```python
import anthropic
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class AgentConfig(BaseSettings):
    # Required — no default means pydantic raises ValidationError if missing
    anthropic_api_key: str = Field(..., description="Anthropic API key")
    database_url: str = Field(..., description="PostgreSQL connection URL")

    # Optional with defaults and type coercion
    model: str = Field("claude-sonnet-4-6", description="Claude model ID")
    max_tokens: int = Field(2048, ge=1, le=8192)
    temperature: float = Field(0.0, ge=0.0, le=1.0)
    max_retries: int = Field(3, ge=0, le=10)
    log_level: str = Field("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    environment: str = Field("production", pattern="^(development|staging|production)$")

    @field_validator("anthropic_api_key")
    @classmethod
    def api_key_format(cls, v: str) -> str:
        if not v.startswith("sk-"):
            raise ValueError("ANTHROPIC_API_KEY must start with 'sk-'")
        if len(v) < 20:
            raise ValueError("ANTHROPIC_API_KEY appears too short to be valid")
        return v

    @field_validator("database_url")
    @classmethod
    def database_url_format(cls, v: str) -> str:
        valid_schemes = ("postgresql://", "postgres://", "sqlite:///")
        if not any(v.startswith(s) for s in valid_schemes):
            raise ValueError(f"DATABASE_URL must start with one of: {valid_schemes}")
        return v

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "case_sensitive": False}


# Module-level instantiation — crashes at import time if env is invalid
# This is intentional: fail before any request handler is registered
try:
    config = AgentConfig()
except Exception as e:
    import sys
    print(f"FATAL: Invalid configuration\n{e}", file=sys.stderr)
    sys.exit(1)

client = anthropic.Anthropic(api_key=config.anthropic_api_key)


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Eliminates incomplete API calls from misconfigured agents.
**Environment:** Python 3.9+; requires `pydantic-settings` (`pip install pydantic-settings`).

---

### Option 3: Layered config with .env file, environment, and defaults

```python
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv


class Config:
    """Layered configuration: .env < environment variables < explicit overrides."""

    def __init__(self, env_file: str = ".env", override: dict | None = None):
        # Load .env first (lowest priority — env vars override .env)
        env_path = Path(env_file)
        if env_path.exists():
            load_dotenv(env_path)
            print(f"Loaded config from {env_path}")
        else:
            print(f"No {env_file} found — using environment variables only")

        # Apply any explicit overrides (highest priority)
        if override:
            for key, value in override.items():
                os.environ[key] = str(value)

        self._validate()

    def _validate(self):
        missing = []
        invalid = []

        # Required string vars
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")

        self.database_url = os.environ.get("DATABASE_URL", "")
        if not self.database_url:
            missing.append("DATABASE_URL")

        # Required int with validation
        try:
            self.port = int(os.environ.get("PORT", "8080"))
            if not (1024 <= self.port <= 65535):
                invalid.append(f"PORT={self.port} (must be 1024–65535)")
        except ValueError:
            invalid.append(f"PORT={os.environ.get('PORT')} (must be an integer)")

        # Optional with defaults
        self.model = os.environ.get("MODEL", "claude-sonnet-4-6")
        self.debug = os.environ.get("DEBUG", "false").lower() == "true"
        self.max_retries = int(os.environ.get("MAX_RETRIES", "3"))

        errors = []
        if missing:
            errors.append(f"Missing required variables: {', '.join(missing)}")
        if invalid:
            errors.append(f"Invalid values: {'; '.join(invalid)}")

        if errors:
            for e in errors:
                print(f"CONFIG ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    def __repr__(self) -> str:
        # Redact secrets in logs
        masked_key = self.anthropic_api_key[:8] + "..." if self.anthropic_api_key else "MISSING"
        return (
            f"Config(model={self.model}, port={self.port}, "
            f"api_key={masked_key}, debug={self.debug})"
        )


config = Config()
print(f"Starting with {config}")
client = anthropic.Anthropic(api_key=config.anthropic_api_key)


def run_agent(prompt: str) -> str:
    response = client.messages.create(
        model=config.model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Prevents all tokens spent on doomed runs.
**Environment:** Requires `python-dotenv` (`pip install python-dotenv`).

---

### Option 4: Async startup with connection probing

```python
import asyncio
import os
import sys

import anthropic


async def probe_anthropic_api(api_key: str) -> tuple[bool, str]:
    """Make a minimal API call to confirm the key is valid and the service is reachable."""
    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        # Use the cheapest possible call: 1 input token, 1 output token
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True, "OK"
    except anthropic.AuthenticationError:
        return False, "API key rejected (AuthenticationError)"
    except anthropic.APIConnectionError as e:
        return False, f"Cannot reach Anthropic API: {e}"
    except anthropic.RateLimitError:
        # Rate limited = key is valid but we're over quota
        return True, "Rate limited (key valid)"
    except Exception as e:
        return False, f"Unexpected error: {type(e).__name__}: {e}"


async def startup_checks() -> dict:
    """Run all startup validations concurrently."""
    required = {
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        "DATABASE_URL": os.environ.get("DATABASE_URL", ""),
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"FATAL: Missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    # Probe API connectivity concurrently with other checks
    api_ok, api_msg = await probe_anthropic_api(required["ANTHROPIC_API_KEY"])

    if not api_ok:
        print(f"FATAL: Anthropic API check failed: {api_msg}", file=sys.stderr)
        sys.exit(1)

    print(f"✓ Anthropic API: {api_msg}")
    return {
        "anthropic_api_key": required["ANTHROPIC_API_KEY"],
        "model": os.environ.get("MODEL", "claude-sonnet-4-6"),
    }


async def main():
    config = await startup_checks()
    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])

    response = await client.messages.create(
        model=config["model"],
        max_tokens=512,
        messages=[{"role": "user", "content": "Hello, are you working?"}],
    )
    print(response.content[0].text)


asyncio.run(main())
```

**Expected Token Savings:** 1 token probe call prevents multi-thousand-token task runs with invalid credentials.
**Environment:** Python 3.11+; async startup; suitable for FastAPI lifespan handlers.

---

### Option 5: Startup validator as a decorator

```python
import functools
import os
import sys
from typing import Any, Callable

import anthropic


def requires_env(*var_names: str, optional: dict[str, str] | None = None):
    """
    Decorator that validates env vars before the decorated function runs.
    Use on main(), app factory, or any entry point.

    Usage:
        @requires_env("ANTHROPIC_API_KEY", "DATABASE_URL",
                      optional={"MODEL": "claude-sonnet-4-6"})
        def main():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            missing = [v for v in var_names if not os.environ.get(v)]
            if missing:
                print(
                    f"FATAL [{func.__name__}]: Missing env vars: {', '.join(missing)}",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Inject optional defaults
            if optional:
                for var, default in optional.items():
                    os.environ.setdefault(var, default)

            return func(*args, **kwargs)
        return wrapper
    return decorator


@requires_env(
    "ANTHROPIC_API_KEY",
    "APP_SECRET",
    optional={
        "MODEL": "claude-sonnet-4-6",
        "MAX_TOKENS": "2048",
        "LOG_LEVEL": "INFO",
    },
)
def run_agent(user_message: str) -> str:
    client = anthropic.Anthropic()  # Reads ANTHROPIC_API_KEY automatically
    response = client.messages.create(
        model=os.environ["MODEL"],
        max_tokens=int(os.environ["MAX_TOKENS"]),
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


@requires_env("ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", optional={"CURRENCY": "usd"})
def run_billing_agent(prompt: str) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


if __name__ == "__main__":
    print(run_agent("Summarize the key points of fail-fast design."))
```

**Expected Token Savings:** Zero wasted tokens from crashed partial runs.
**Environment:** Python 3.10+ (uses `dict[str, str]` type hint); works with any entry point pattern.

---

### Option 6: Schema-driven validator with JSON config manifest

```python
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Literal

import anthropic


@dataclass
class EnvVarSpec:
    name: str
    required: bool
    description: str
    default: str | None = None
    validator: str | None = None  # "url", "api_key", "int", "bool", "port"
    secret: bool = False


ENV_SCHEMA: list[EnvVarSpec] = [
    EnvVarSpec("ANTHROPIC_API_KEY", required=True, description="Anthropic API key",
               validator="api_key", secret=True),
    EnvVarSpec("DATABASE_URL", required=True, description="Postgres connection URL",
               validator="url"),
    EnvVarSpec("REDIS_URL", required=False, description="Redis URL for caching",
               validator="url", default="redis://localhost:6379"),
    EnvVarSpec("PORT", required=False, description="HTTP server port",
               validator="port", default="8080"),
    EnvVarSpec("DEBUG", required=False, description="Enable debug mode",
               validator="bool", default="false"),
    EnvVarSpec("MODEL", required=False, description="Claude model to use",
               default="claude-sonnet-4-6"),
]


def validate_value(spec: EnvVarSpec, value: str) -> str | None:
    """Returns an error message or None if valid."""
    if spec.validator == "api_key":
        if not value.startswith("sk-"):
            return "must start with 'sk-'"
        if len(value) < 20:
            return "too short to be a valid API key"
    elif spec.validator == "url":
        if "://" not in value:
            return "must be a valid URL with scheme (e.g., postgresql://...)"
    elif spec.validator == "int":
        try:
            int(value)
        except ValueError:
            return f"must be an integer, got '{value}'"
    elif spec.validator == "bool":
        if value.lower() not in ("true", "false", "1", "0", "yes", "no"):
            return f"must be a boolean (true/false), got '{value}'"
    elif spec.validator == "port":
        try:
            port = int(value)
            if not (1 <= port <= 65535):
                return f"port must be 1–65535, got {port}"
        except ValueError:
            return f"port must be an integer, got '{value}'"
    return None


def load_and_validate_config() -> dict[str, str]:
    config: dict[str, str] = {}
    errors: list[str] = []

    for spec in ENV_SCHEMA:
        raw = os.environ.get(spec.name)

        if raw is None or raw.strip() == "":
            if spec.required:
                errors.append(f"  ✗ {spec.name} (required): {spec.description}")
                continue
            elif spec.default is not None:
                raw = spec.default
            else:
                continue  # Optional, no default, skip

        if spec.validator:
            error = validate_value(spec, raw)
            if error:
                errors.append(f"  ✗ {spec.name}: {error}")
                continue

        config[spec.name] = raw

    if errors:
        print("STARTUP FAILED — configuration errors:", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)

    # Log config summary (redact secrets)
    summary = {
        k: ("***" if any(s.name == k and s.secret for s in ENV_SCHEMA) else v)
        for k, v in config.items()
    }
    print(f"✓ Config loaded: {json.dumps(summary, indent=2)}")
    return config


config = load_and_validate_config()
client = anthropic.Anthropic(api_key=config["ANTHROPIC_API_KEY"])


def run_agent(prompt: str) -> str:
    response = client.messages.create(
        model=config.get("MODEL", "claude-sonnet-4-6"),
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


if __name__ == "__main__":
    print(run_agent("Explain fail-fast configuration validation."))
```

**Expected Token Savings:** Eliminates all wasted tokens from runs that would fail on missing config.
**Environment:** Python 3.10+; zero additional dependencies; schema can be loaded from JSON file for team-wide documentation.

---

| Option | Approach | Validation Depth | Best For |
|--------|----------|-----------------|----------|
| 1 | Explicit dict check + sys.exit | Basic presence | Simple scripts |
| 2 | Pydantic BaseSettings | Full type + format | FastAPI/Django apps |
| 3 | Layered .env + override | Multi-source | 12-factor apps |
| 4 | Async + API probe | Connectivity check | Async microservices |
| 5 | Decorator | Per-function | Library code |
| 6 | Schema-driven manifest | Typed + documented | Team codebases |
