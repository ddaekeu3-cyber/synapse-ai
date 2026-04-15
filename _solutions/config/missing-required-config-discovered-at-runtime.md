---
layout: solution
title: "Missing Required Config Discovered at Runtime — Agent Fails Mid-Task"
category: config
description: "Agent starts successfully but fails mid-task when it first tries to use an unconfigured feature. Missing DATABASE_URL discovered only when first DB query runs, not at startup."
tags: [config, validation, startup, fail-fast, environment-variables, runtime-error]
---

# Missing Required Config Discovered at Runtime — Agent Fails Mid-Task

## Symptom
- Agent starts without errors but fails 30 minutes into a task
- `KeyError: 'REDIS_URL'` raised only when Redis is first needed
- Partially completed work lost when config error discovered mid-task
- Config error appears in different places depending on execution path
- Works in some code paths, fails in others (config checked lazily)

## Root Cause
Lazy configuration: config values read only when first used. If usage happens late in execution, missing config is discovered after significant work is done. A fail-fast approach — validate all config at startup before any work begins — prevents this.

## Fix

### Option 1: Validate all config at startup before any work

```python
import os, sys

# Define all required config in one place
REQUIRED_CONFIG = {
    "ANTHROPIC_API_KEY": "Anthropic API key for LLM calls",
    "DATABASE_URL": "PostgreSQL connection string",
    "REDIS_URL": "Redis connection for task queue",
    "SLACK_WEBHOOK_URL": "Slack webhook for notifications",
}

OPTIONAL_CONFIG = {
    "MAX_WORKERS": ("10", "Maximum concurrent workers"),
    "LOG_LEVEL": ("INFO", "Logging verbosity"),
    "TASK_TIMEOUT": ("300", "Task timeout in seconds"),
}

def validate_config() -> dict:
    """Validate all config at startup. Exit immediately if anything is missing."""
    errors = []
    config = {}

    for key, description in REQUIRED_CONFIG.items():
        value = os.environ.get(key)
        if not value:
            errors.append(f"  Missing: {key} — {description}")
        else:
            config[key] = value

    if errors:
        print("FATAL: Required configuration missing:")
        for error in errors:
            print(error)
        print("\nSet these environment variables before running the agent.")
        sys.exit(1)

    for key, (default, description) in OPTIONAL_CONFIG.items():
        config[key] = os.environ.get(key, default)

    return config

# Call FIRST before any other initialization
CONFIG = validate_config()
print(f"Configuration validated. Running with {len(CONFIG)} settings.")
```

### Option 2: Pydantic Settings — type-safe config validation

```python
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import sys

class AgentConfig(BaseSettings):
    # Required — no default value means it MUST be set
    anthropic_api_key: str
    database_url: str
    redis_url: str

    # Optional — has a default
    max_workers: int = 10
    log_level: str = "INFO"
    task_timeout: int = 300
    slack_webhook_url: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v):
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            raise ValueError(f"log_level must be one of {valid}, got '{v}'")
        return v.upper()

    @field_validator("max_workers")
    @classmethod
    def validate_max_workers(cls, v):
        if v < 1 or v > 1000:
            raise ValueError(f"max_workers must be between 1 and 1000, got {v}")
        return v

try:
    config = AgentConfig()
except Exception as e:
    print(f"Configuration error: {e}")
    sys.exit(1)
```

### Option 3: Test all connections at startup

```python
import asyncio, sys
import anthropic, redis.asyncio as redis
import asyncpg

async def test_all_connections(config: dict) -> None:
    """Verify all external services are reachable at startup"""
    errors = []

    # Test Anthropic API
    try:
        client = anthropic.Anthropic(api_key=config["ANTHROPIC_API_KEY"])
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}]
        )
        print("✓ Anthropic API: connected")
    except Exception as e:
        errors.append(f"✗ Anthropic API: {e}")

    # Test database
    try:
        conn = await asyncpg.connect(config["DATABASE_URL"])
        await conn.fetchval("SELECT 1")
        await conn.close()
        print("✓ Database: connected")
    except Exception as e:
        errors.append(f"✗ Database: {e}")

    # Test Redis
    try:
        r = redis.from_url(config["REDIS_URL"])
        await r.ping()
        await r.aclose()
        print("✓ Redis: connected")
    except Exception as e:
        errors.append(f"✗ Redis: {e}")

    if errors:
        print("\nStartup connection tests failed:")
        for error in errors:
            print(f"  {error}")
        sys.exit(1)

    print("All connections verified. Starting agent...")

# Run at startup
asyncio.run(test_all_connections(CONFIG))
```

### Option 4: Config schema with documentation

```python
from dataclasses import dataclass, field

@dataclass
class ConfigSpec:
    name: str
    required: bool
    description: str
    example: str
    default: str = None

CONFIG_SCHEMA = [
    ConfigSpec("ANTHROPIC_API_KEY", True, "Anthropic API key", "sk-ant-api03-..."),
    ConfigSpec("DATABASE_URL", True, "PostgreSQL URL", "postgresql://user:pass@localhost/db"),
    ConfigSpec("REDIS_URL", True, "Redis URL", "redis://localhost:6379"),
    ConfigSpec("MAX_WORKERS", False, "Concurrent workers", "20", default="10"),
    ConfigSpec("LOG_LEVEL", False, "Log verbosity", "DEBUG", default="INFO"),
]

def print_config_help():
    """Print configuration documentation"""
    print("Required environment variables:")
    for spec in CONFIG_SCHEMA:
        marker = "[REQUIRED]" if spec.required else f"[optional, default: {spec.default}]"
        print(f"  {spec.name} {marker}")
        print(f"    {spec.description}")
        print(f"    Example: {spec.name}={spec.example}")

def load_and_validate():
    missing = []
    config = {}
    for spec in CONFIG_SCHEMA:
        value = os.environ.get(spec.name, spec.default)
        if value is None and spec.required:
            missing.append(spec)
        else:
            config[spec.name] = value
    if missing:
        print("Missing required configuration:\n")
        for spec in missing:
            print(f"  {spec.name}: {spec.description}")
            print(f"  Example: export {spec.name}={spec.example}\n")
        sys.exit(1)
    return config
```

### Option 5: .env file template generation

```python
def generate_env_template(output_path: str = ".env.example"):
    """Generate .env.example from config schema"""
    lines = ["# Agent Configuration\n# Copy to .env and fill in values\n"]

    for spec in CONFIG_SCHEMA:
        lines.append(f"# {spec.description}")
        if not spec.required:
            lines.append(f"# Optional. Default: {spec.default}")
        value = spec.default if spec.default else spec.example
        prefix = "# " if not spec.required else ""
        lines.append(f"{prefix}{spec.name}={value}\n")

    with open(output_path, "w") as f:
        f.write("\n".join(lines))

    print(f"Generated {output_path}")

# Run once to create template
generate_env_template()
```

## Startup Validation Checklist

| What to check | When | How |
|---|---|---|
| Required env vars present | First | `os.environ[key]` or pydantic |
| Env var format valid | First | Regex, URL parse, etc. |
| External services reachable | After env validation | Test connections |
| Permissions sufficient | After connections | Test a real operation |
| Disk space available | Before large tasks | `shutil.disk_usage()` |

## Expected Token Savings
Mid-task config failure + restart + redo work: ~20,000 tokens
Startup validation catches it before any work: 0 wasted

## Environment
- All production agent deployments with multiple external service dependencies
- Source: direct experience; fail-fast config validation is a universal best practice
