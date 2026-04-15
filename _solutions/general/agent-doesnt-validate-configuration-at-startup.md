---
layout: solution
title: "Agent doesn't validate configuration at startup"
category: general
description: "Agent accepts invalid model names, missing API keys, or impossible max_tokens values silently at startup — then fails on the first real request with a cryptic error that takes minutes to diagnose."
tags: [configuration, validation, startup, fail-fast, environment, api-key]
---

## Symptom

The agent starts successfully, logs "ready", accepts a user request, and then crashes with:

```
anthropic.AuthenticationError: 401 Unauthorized — API key is missing or invalid
anthropic.BadRequestError: model 'claude-3-sonnet' not found
ValueError: max_tokens 200000 exceeds model limit
```

The crash happens on the first API call, not at startup. By that time, the request is already in-flight and must be retried from scratch. In production, 100 workers all fail simultaneously on their first request.

## Root Cause

Configuration is read from environment variables or config files without validation. Python's `os.environ.get("ANTHROPIC_API_KEY")` returns `None` if the variable is missing — and `None` is passed to the SDK which only catches it at call time. Model names may be misspelled, token limits may exceed model caps, or required fields may be absent. Fail-fast validation at startup surfaces these issues in 10 milliseconds instead of 10 minutes into a production incident.

---

## Option 1 — Pydantic settings with startup validation

**Use `pydantic-settings` to declare, validate, and fail-fast on all configuration before the agent accepts any work.**

```python
import sys
import anthropic
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

VALID_MODELS = {
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
}

MODEL_MAX_TOKENS = {
    "claude-haiku-4-5-20251001": 8_192,
    "claude-sonnet-4-6":         64_000,
    "claude-opus-4-6":           32_000,
}


class AgentConfig(BaseSettings):
    anthropic_api_key: str
    model:             str  = "claude-haiku-4-5-20251001"
    max_tokens:        int  = 1024
    timeout_seconds:   float = 30.0
    max_retries:       int  = 3
    log_level:         str  = "INFO"

    @field_validator("anthropic_api_key")
    @classmethod
    def validate_api_key(cls, v: str) -> str:
        if not v or not v.startswith("sk-"):
            raise ValueError("ANTHROPIC_API_KEY must start with 'sk-'")
        if len(v) < 20:
            raise ValueError("ANTHROPIC_API_KEY appears too short to be valid")
        return v

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        if v not in VALID_MODELS:
            raise ValueError(f"Unknown model '{v}'. Valid models: {sorted(VALID_MODELS)}")
        return v

    @field_validator("max_tokens")
    @classmethod
    def validate_max_tokens(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_tokens must be at least 1")
        return v

    @model_validator(mode="after")
    def check_max_tokens_for_model(self) -> "AgentConfig":
        limit = MODEL_MAX_TOKENS.get(self.model, 8_192)
        if self.max_tokens > limit:
            raise ValueError(
                f"max_tokens={self.max_tokens} exceeds limit for {self.model} ({limit})"
            )
        return self

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


def create_agent() -> anthropic.Anthropic:
    try:
        config = AgentConfig()
        print(f"Config validated: model={config.model} max_tokens={config.max_tokens}")
    except Exception as e:
        print(f"FATAL: Invalid configuration — {e}")
        print("Fix the above error before starting the agent.")
        sys.exit(1)

    return anthropic.Anthropic(api_key=config.anthropic_api_key)


client = create_agent()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.content[0].text)
```

**Expected Token Savings:** Startup validation prevents 100 workers from each burning one failed API call before discovering a bad API key — saves N × first-call tokens (typically 200–500 tokens per call).

**Environment:** Any agent; `pydantic-settings>=2.0`; works with `.env` files, env vars, and AWS Secrets Manager.

---

## Option 2 — Lightweight startup probe: make one test API call

**After loading config, make a cheap API call to verify credentials and model availability before accepting traffic.**

```python
import os
import sys
import anthropic


def probe_api(api_key: str, model: str) -> None:
    """Make a minimal API call to verify config is valid. Exit on failure."""
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
        )
        print(f"API probe OK — model={model} id={response.id}")
    except anthropic.AuthenticationError as e:
        print(f"FATAL: Authentication failed — check ANTHROPIC_API_KEY\n  {e}")
        sys.exit(1)
    except anthropic.NotFoundError as e:
        print(f"FATAL: Model not found — check MODEL env var\n  {e}")
        sys.exit(1)
    except anthropic.BadRequestError as e:
        print(f"FATAL: Bad request — check max_tokens or other params\n  {e}")
        sys.exit(1)
    except anthropic.APIConnectionError as e:
        print(f"WARNING: Could not connect to Anthropic API at startup — {e}")
        # Don't exit — network may be transiently unavailable; let the app start


def load_and_validate_config() -> dict:
    config = {
        "api_key":    os.environ.get("ANTHROPIC_API_KEY", ""),
        "model":      os.environ.get("MODEL", "claude-haiku-4-5-20251001"),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "1024")),
    }

    errors = []
    if not config["api_key"]:
        errors.append("ANTHROPIC_API_KEY is not set")
    if config["max_tokens"] < 1:
        errors.append("MAX_TOKENS must be >= 1")

    if errors:
        for err in errors:
            print(f"FATAL: {err}")
        sys.exit(1)

    return config


config = load_and_validate_config()
probe_api(config["api_key"], config["model"])

# Main agent work — only reached if probe passes
client = anthropic.Anthropic(api_key=config["api_key"])
print("Agent ready.")
```

**Expected Token Savings:** 5-token probe call costs ~$0.000015 but prevents a misconfigured agent from running indefinitely. For a service restarted 50× per day with a bad key, saves 50 × first-request-tokens daily.

**Environment:** Any HTTP-serving agent; add probe to Kubernetes `readinessProbe` command or Docker `HEALTHCHECK`.

---

## Option 3 — Config schema with JSON Schema validation

**Store config in a JSON/YAML file; validate against a schema at startup using `jsonschema`.**

```python
import json
import os
import sys
import anthropic
import jsonschema

CONFIG_SCHEMA = {
    "type": "object",
    "required": ["model", "max_tokens", "system_prompt"],
    "properties": {
        "model": {
            "type": "string",
            "enum": ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"],
        },
        "max_tokens": {
            "type": "integer",
            "minimum": 1,
            "maximum": 64_000,
        },
        "system_prompt": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100_000,
        },
        "temperature": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "tools": {
            "type": "array",
            "items": {"type": "object"},
        },
    },
    "additionalProperties": False,
}


def load_config(path: str = "agent_config.json") -> dict:
    if not os.path.exists(path):
        print(f"FATAL: Config file not found: {path}")
        sys.exit(1)

    with open(path) as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError as e:
            print(f"FATAL: Config file is not valid JSON: {e}")
            sys.exit(1)

    validator = jsonschema.Draft7Validator(CONFIG_SCHEMA)
    errors = sorted(validator.iter_errors(config), key=lambda e: e.path)
    if errors:
        print(f"FATAL: {len(errors)} config validation error(s):")
        for err in errors:
            path = ".".join(str(p) for p in err.absolute_path) or "root"
            print(f"  {path}: {err.message}")
        sys.exit(1)

    print(f"Config valid: {json.dumps({k: config[k] for k in ['model', 'max_tokens']})}")
    return config


# Example: create a valid config file for testing
sample_config = {
    "model":         "claude-haiku-4-5-20251001",
    "max_tokens":    1024,
    "system_prompt": "You are a helpful assistant.",
}
with open("agent_config.json", "w") as f:
    json.dump(sample_config, f)

config = load_config()
client = anthropic.Anthropic()
print("Agent started with validated config.")
```

**Expected Token Savings:** Config file validation catches typos in model names and out-of-range token limits before deployment — prevents misconfigured staging environments from burning API quota on bad requests.

**Environment:** Agents with config files (JSON/YAML); `jsonschema>=4.0`.

---

## Option 4 — Environment variable checker with helpful error messages

**Check all required env vars at module import time with human-readable error messages that tell the developer exactly what to do.**

```python
import os
import sys


REQUIRED_VARS = {
    "ANTHROPIC_API_KEY": {
        "example":    "sk-ant-api03-...",
        "docs":       "https://console.anthropic.com/settings/keys",
        "validator":  lambda v: v.startswith("sk-") and len(v) > 20,
        "error_hint": "Get your API key from the Anthropic console",
    },
}

OPTIONAL_VARS = {
    "MODEL": {
        "default":   "claude-haiku-4-5-20251001",
        "validator": lambda v: v in {"claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"},
        "error_hint": "Valid values: claude-haiku-4-5-20251001, claude-sonnet-4-6, claude-opus-4-6",
    },
    "MAX_TOKENS": {
        "default":   "1024",
        "validator": lambda v: v.isdigit() and 1 <= int(v) <= 64_000,
        "error_hint": "Must be an integer between 1 and 64000",
    },
    "LOG_LEVEL": {
        "default":   "INFO",
        "validator": lambda v: v in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
        "error_hint": "Must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL",
    },
}


def validate_env() -> dict[str, str]:
    errors   = []
    warnings = []
    result   = {}

    # Required vars
    for var, meta in REQUIRED_VARS.items():
        value = os.environ.get(var, "")
        if not value:
            errors.append(
                f"  Missing: {var}\n"
                f"    Example: export {var}={meta['example']}\n"
                f"    Docs:    {meta['docs']}\n"
                f"    Hint:    {meta['error_hint']}"
            )
        elif not meta["validator"](value):
            errors.append(
                f"  Invalid: {var}={value!r}\n"
                f"    Hint: {meta['error_hint']}"
            )
        else:
            result[var] = value

    # Optional vars with defaults
    for var, meta in OPTIONAL_VARS.items():
        value = os.environ.get(var, meta["default"])
        if not meta["validator"](value):
            warnings.append(
                f"  {var}={value!r} is invalid (using default={meta['default']!r})\n"
                f"    Hint: {meta['error_hint']}"
            )
            value = meta["default"]
        result[var] = value

    if warnings:
        print("Configuration warnings:")
        for w in warnings:
            print(w)

    if errors:
        print("FATAL: Configuration errors (agent cannot start):")
        for e in errors:
            print(e)
        print("\nSet the above environment variables and restart.")
        sys.exit(1)

    return result


# Called at module import — fails fast before any work starts
config = validate_env()

import anthropic
client = anthropic.Anthropic(api_key=config["ANTHROPIC_API_KEY"])
print(f"Agent ready: model={config['MODEL']} max_tokens={config['MAX_TOKENS']}")
```

**Expected Token Savings:** Human-readable error messages at startup eliminate the debugging loop where a developer starts the agent, sees a cryptic error, googles it, tries a fix, and repeats — each iteration may include test API calls.

**Environment:** Any agent deployed via environment variables (Docker, Kubernetes, Heroku, AWS ECS).

---

## Option 5 — Dataclass config with `__post_init__` validation

**Pure stdlib approach: use a dataclass with `__post_init__` to validate all fields at construction time.**

```python
import os
import sys
from dataclasses import dataclass, field
import anthropic


@dataclass
class AgentConfig:
    api_key:         str
    model:           str   = "claude-haiku-4-5-20251001"
    max_tokens:      int   = 1024
    timeout:         float = 30.0
    system_prompt:   str   = "You are a helpful assistant."

    _VALID_MODELS = frozenset({
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
    })

    def __post_init__(self) -> None:
        errors = []

        if not self.api_key or not self.api_key.startswith("sk-"):
            errors.append("api_key must start with 'sk-'")

        if self.model not in self._VALID_MODELS:
            errors.append(f"model must be one of {sorted(self._VALID_MODELS)}, got {self.model!r}")

        if not (1 <= self.max_tokens <= 200_000):
            errors.append(f"max_tokens must be 1–200000, got {self.max_tokens}")

        if not (0.1 <= self.timeout <= 600.0):
            errors.append(f"timeout must be 0.1–600s, got {self.timeout}")

        if not self.system_prompt.strip():
            errors.append("system_prompt must not be empty")

        if errors:
            raise ValueError(
                "Invalid AgentConfig:\n" + "\n".join(f"  • {e}" for e in errors)
            )

    @classmethod
    def from_env(cls) -> "AgentConfig":
        try:
            return cls(
                api_key       = os.environ.get("ANTHROPIC_API_KEY", ""),
                model         = os.environ.get("MODEL",             "claude-haiku-4-5-20251001"),
                max_tokens    = int(os.environ.get("MAX_TOKENS",    "1024")),
                timeout       = float(os.environ.get("TIMEOUT",    "30.0")),
                system_prompt = os.environ.get("SYSTEM_PROMPT",    "You are a helpful assistant."),
            )
        except (ValueError, TypeError) as e:
            print(f"FATAL: {e}")
            sys.exit(1)


try:
    config = AgentConfig.from_env()
    print(f"Config OK: {config.model} / max_tokens={config.max_tokens}")
except SystemExit:
    raise


client = anthropic.Anthropic(api_key=config.api_key)
response = client.messages.create(
    model=config.model,
    max_tokens=config.max_tokens,
    system=config.system_prompt,
    messages=[{"role": "user", "content": "Ready check."}],
)
print(response.content[0].text)
```

**Expected Token Savings:** Dataclass validation is zero-dependency and runs in microseconds — eliminates all configuration-caused first-request failures with no external libraries.

**Environment:** Minimal dependency environments; Python 3.10+ stdlib only.

---

## Option 6 — CI config linter that validates before deployment

**Add a config validation step to CI/CD so misconfigured agents never reach production.**

```python
#!/usr/bin/env python3
"""
scripts/validate_agent_config.py
Usage: python scripts/validate_agent_config.py .env.production
"""
import re
import sys
from pathlib import Path

VALID_MODELS = {"claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"}

CHECKS = [
    ("ANTHROPIC_API_KEY", True,  lambda v: bool(re.match(r"^sk-[a-zA-Z0-9\-_]{20,}$", v)),
     "must match sk-<alphanumeric 20+ chars>"),
    ("MODEL",             False, lambda v: v in VALID_MODELS,
     f"must be one of {sorted(VALID_MODELS)}"),
    ("MAX_TOKENS",        False, lambda v: v.isdigit() and 1 <= int(v) <= 200_000,
     "must be integer 1–200000"),
    ("LOG_LEVEL",         False, lambda v: v in {"DEBUG", "INFO", "WARNING", "ERROR"},
     "must be DEBUG, INFO, WARNING, or ERROR"),
    ("TIMEOUT_SECONDS",   False, lambda v: v.replace(".", "", 1).isdigit() and 0 < float(v) <= 600,
     "must be a float 0–600"),
]


def parse_env_file(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def validate(env_path: str) -> int:
    try:
        env = parse_env_file(env_path)
    except FileNotFoundError:
        print(f"ERROR: {env_path} not found")
        return 1

    errors = 0
    for var, required, validator, hint in CHECKS:
        value = env.get(var, "")
        if not value:
            if required:
                print(f"ERROR: {var} is missing (required)")
                errors += 1
            else:
                print(f"  OK: {var} not set (optional)")
            continue
        if not validator(value):
            print(f"ERROR: {var}={value!r} is invalid — {hint}")
            errors += 1
        else:
            masked = value[:8] + "..." if "KEY" in var else value
            print(f"  OK: {var}={masked}")

    print(f"\n{'PASS' if errors == 0 else 'FAIL'}: {errors} error(s)")
    return errors


if __name__ == "__main__":
    env_file = sys.argv[1] if len(sys.argv) > 1 else ".env"
    sys.exit(validate(env_file))
```

**GitHub Actions integration:**
```yaml
- name: Validate agent configuration
  run: python scripts/validate_agent_config.py .env.production
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

**Expected Token Savings:** CI validation blocks misconfigured deployments before they reach any real traffic — prevents entire deployment windows where all requests fail due to a config error.

**Environment:** Any CI/CD pipeline; stdlib only; run as a pre-deployment gate.

---

## Comparison

| Option | Validation Point | Library | Catches at | Complexity |
|--------|-----------------|---------|-----------|------------|
| 1. Pydantic settings | Module load | `pydantic-settings` | Import time | Low |
| 2. API probe call | Startup | None | First call | Very Low |
| 3. JSON Schema | File load | `jsonschema` | Import time | Low |
| 4. Env var checker | Module load | None | Import time | Very Low |
| 5. Dataclass `__post_init__` | Construction | None (stdlib) | Construction | Very Low |
| 6. CI config linter | Pre-deployment | None (stdlib) | Before deploy | Low |

**Recommended path:** Add Option 4 (env var checker) at the top of your main module — zero dependencies, immediate value. Layer Option 2 (API probe) for production deployments to verify credentials are accepted by Anthropic's servers, not just syntactically valid. Add Option 6 (CI linter) to prevent misconfigured deployments from shipping.
