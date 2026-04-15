---
layout: solution
title: "Environment Variable Typo Causes Silent None — Agent Uses Wrong Config Value"
category: config
description: "Agent uses os.environ.get('ANTHROPIC_API_KEEY') — typo returns None silently. No error until the None reaches an API call. Agent behaves incorrectly with no obvious cause."
tags: [config, environment-variables, typo, silent-failure, none, debugging]
---

# Environment Variable Typo Causes Silent None — Agent Uses Wrong Config Value

## Symptom
- `os.environ.get("ANTHROPIC_API_KEEY")` returns `None` — no error raised
- Agent starts but fails later with `AuthenticationError` or `NoneType has no attribute`
- Config value appears set but agent ignores it
- Works on one machine, fails on another (different variable name typo)
- `print(os.environ.get("MY_VAR"))` → `None` even though `MY_VAR` is set in `.env`

## Root Cause
`os.environ.get()` returns `None` on missing keys by default — it does not raise. A single-character typo in the variable name silently returns `None`. The error surfaces far from the typo, making it hard to diagnose.

## Fix

### Option 1: Use os.environ[] instead of .get() for required variables

```python
import os

# WRONG — silent None on typo or missing var
api_key = os.environ.get("ANTHROPIC_API_KEEY")  # typo: extra E

# RIGHT — raises KeyError immediately with the missing name
api_key = os.environ["ANTHROPIC_API_KEY"]

# KeyError: 'ANTHROPIC_API_KEY'  ← clear error at startup, not later
```

### Option 2: Validate all required env vars at startup

```python
import os, sys

REQUIRED_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "APP_SECRET_KEY",
]

def validate_env():
    missing = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
    if missing:
        print(f"ERROR: Missing required environment variables:")
        for var in missing:
            print(f"  - {var}")
        sys.exit(1)

validate_env()  # Call before anything else at startup
```

### Option 3: Use pydantic-settings for typed env validation

```python
from pydantic_settings import BaseSettings
from pydantic import Field

class AgentSettings(BaseSettings):
    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    database_url: str = Field(..., alias="DATABASE_URL")
    max_tokens: int = Field(4096, alias="MAX_TOKENS")
    debug: bool = Field(False, alias="DEBUG")

    class Config:
        env_file = ".env"
        case_sensitive = True

# Raises ValidationError at startup if required fields are missing
settings = AgentSettings()

# Usage — typos caught at class definition time, not runtime
client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
```

### Option 4: python-dotenv with explicit error on missing

```python
from dotenv import dotenv_values, load_dotenv
import os

load_dotenv()  # Load .env file

def require_env(name: str) -> str:
    """Get env var or raise with a helpful message"""
    value = os.environ.get(name)
    if value is None:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set.\n"
            f"Add it to your .env file or export it in your shell.\n"
            f"Available vars matching '{name[:5]}': "
            f"{[k for k in os.environ if k.startswith(name[:5])]}"
        )
    if not value.strip():
        raise EnvironmentError(f"Environment variable '{name}' is set but empty.")
    return value

api_key = require_env("ANTHROPIC_API_KEY")
```

### Option 5: Audit env var usage with grep

```bash
# Find all env var references in your codebase
grep -rn 'os\.environ' . --include="*.py" | grep -v ".pyc"
grep -rn 'os\.getenv' . --include="*.py"

# Compare against what's actually set
env | sort

# Check for common typos (double letters, transpositions)
grep -rn 'os\.environ' . --include="*.py" | \
    grep -oP '"[A-Z_]+"' | sort | uniq
```

### Option 6: IDE / pre-commit linting

```bash
# .pre-commit-config.yaml — catch undefined env vars
repos:
  - repo: local
    hooks:
      - id: check-env-vars
        name: Check .env.example has all required vars
        language: python
        entry: python scripts/check_env_completeness.py
        pass_filenames: false
```

```python
# scripts/check_env_completeness.py
import re, sys
from pathlib import Path

# Extract all env var names used in code
code_vars = set()
for py_file in Path(".").rglob("*.py"):
    content = py_file.read_text()
    code_vars.update(re.findall(r'os\.environ(?:\.get)?\(["\']([A-Z_]+)["\']', content))

# Compare against .env.example
example_vars = set()
if Path(".env.example").exists():
    for line in Path(".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            example_vars.add(line.split("=")[0].strip())

undocumented = code_vars - example_vars
if undocumented:
    print(f"Env vars used in code but missing from .env.example: {undocumented}")
    sys.exit(1)
```

## Common Typo Patterns

| Actual name | Typo | Result |
|---|---|---|
| `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEEY` | `None` |
| `DATABASE_URL` | `DATABSE_URL` | `None` |
| `REDIS_HOST` | `REDIS_HOST ` (trailing space) | `None` |
| `APP_SECRET` | `app_secret` (lowercase) | `None` (case-sensitive) |
| `MAX_RETRIES` | `MAX_RETRY` (singular) | `None` |

## Expected Token Savings
Debugging silent None propagation: ~5,000 tokens
Startup validation catches it: 0 tokens wasted

## Environment
- Any Python agent reading configuration from environment variables
- Source: direct experience; extremely common source of subtle bugs
