---
layout: solution
title: "Agent Doesn't Implement Config Schema Validation on Startup"
category: config
description: "Agents that read configuration at runtime without validating it on startup silently fail when a key is missing, a value has the wrong type, or an environment variable is misnamed — startup validation catches these errors before they reach users."
tags: [config, validation, startup, pydantic, environment-variables, fail-fast, schema]
---

# Agent Doesn't Implement Config Schema Validation on Startup

## Problem

Agents pull configuration from environment variables, YAML files, or `.env` files at runtime. Without startup validation, a misnamed variable (`ANTHROPIC_API_KEY` vs `ANTHROPIC_KEY`), a wrong type (`MAX_TOKENS="abc"`), or a missing required value causes cryptic failures deep in request handling — not at boot time. The agent silently degrades, returns errors to users, or crashes mid-task. Startup validation follows the fail-fast principle: refuse to start unless every required config value is present and valid.

## Solutions

### Option 1: Pydantic Settings with Environment Variables

`pydantic-settings` validates all configuration at import time. If anything is wrong, the process exits before serving a single request.

```python
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal
import sys

class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Required fields — missing any of these raises ValidationError on startup
    anthropic_api_key: str = Field(..., min_length=10, description="Anthropic API key")

    # Optional with defaults and constraints
    model: str = Field("claude-haiku-4-5-20251001", description="Model to use")
    max_tokens: int = Field(1024, ge=1, le=32000, description="Max output tokens")
    temperature: float = Field(0.7, ge=0.0, le=1.0, description="Sampling temperature")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field("INFO")
    port: int = Field(8000, ge=1024, le=65535, description="Server port")
    timeout_seconds: int = Field(30, ge=5, le=300, description="Request timeout")
    environment: Literal["development", "staging", "production"] = Field("development")

    # Derived / computed validation
    @field_validator("anthropic_api_key")
    @classmethod
    def validate_api_key_format(cls, v: str) -> str:
        if not v.startswith("sk-ant-"):
            raise ValueError(
                f"ANTHROPIC_API_KEY must start with 'sk-ant-', got prefix: {v[:10]}..."
            )
        return v

    @field_validator("model")
    @classmethod
    def validate_model_name(cls, v: str) -> str:
        allowed = {
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
            "claude-opus-4-6",
        }
        if v not in allowed:
            raise ValueError(f"Model '{v}' not in allowed set: {allowed}")
        return v

    @model_validator(mode="after")
    def validate_production_rules(self) -> "AgentConfig":
        if self.environment == "production":
            if self.log_level == "DEBUG":
                raise ValueError("DEBUG logging must not be enabled in production")
            if self.temperature > 0.5:
                raise ValueError("Temperature >0.5 not allowed in production for reproducibility")
        return self

def load_config() -> AgentConfig:
    """Call this at application startup. Exits on validation failure."""
    try:
        config = AgentConfig()
        print(f"Config validated: model={config.model}, env={config.environment}, port={config.port}")
        return config
    except Exception as e:
        print(f"FATAL: Invalid configuration:\n{e}", file=sys.stderr)
        sys.exit(1)

# Usage — call at module level so validation runs at import/startup
# config = load_config()
print("Pydantic Settings config class defined. Install pydantic-settings to use.")
# Expected Token Savings: Prevents wasted API calls from misconfigured agents
# Environment: Any Python agent; FastAPI services; Docker containers; Lambda functions
```

### Option 2: Manual Validation with Fail-Fast Error Accumulation

No extra dependencies — pure Python validation that collects all errors before reporting, so operators fix everything in one cycle.

```python
import os
import sys
from dataclasses import dataclass
from typing import Any

@dataclass
class ConfigError:
    field: str
    message: str

class ConfigValidator:
    def __init__(self):
        self.errors: list[ConfigError] = []
        self.warnings: list[str] = []
        self.config: dict[str, Any] = {}

    def require_str(self, key: str, min_len: int = 1, prefix: str = "") -> str | None:
        val = os.environ.get(key, "").strip()
        if not val:
            self.errors.append(ConfigError(key, f"Required environment variable '{key}' is missing or empty"))
            return None
        if len(val) < min_len:
            self.errors.append(ConfigError(key, f"'{key}' must be at least {min_len} chars, got {len(val)}"))
            return None
        if prefix and not val.startswith(prefix):
            self.errors.append(ConfigError(key, f"'{key}' must start with '{prefix}', got '{val[:len(prefix)+3]}...'"))
            return None
        return val

    def require_int(self, key: str, min_val: int = 0, max_val: int = 2**31) -> int | None:
        raw = os.environ.get(key, "")
        if not raw:
            self.errors.append(ConfigError(key, f"Required environment variable '{key}' is missing"))
            return None
        try:
            val = int(raw)
        except ValueError:
            self.errors.append(ConfigError(key, f"'{key}' must be an integer, got '{raw}'"))
            return None
        if not (min_val <= val <= max_val):
            self.errors.append(ConfigError(key, f"'{key}' must be in [{min_val}, {max_val}], got {val}"))
            return None
        return val

    def optional_str(self, key: str, default: str, choices: list[str] | None = None) -> str:
        val = os.environ.get(key, default).strip()
        if choices and val not in choices:
            self.errors.append(ConfigError(key, f"'{key}' must be one of {choices}, got '{val}'"))
            return default
        return val

    def optional_float(self, key: str, default: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
        raw = os.environ.get(key, str(default))
        try:
            val = float(raw)
        except ValueError:
            self.errors.append(ConfigError(key, f"'{key}' must be a float, got '{raw}'"))
            return default
        if not (min_val <= val <= max_val):
            self.errors.append(ConfigError(key, f"'{key}' must be in [{min_val}, {max_val}], got {val}"))
            return default
        return val

    def warn_if_default(self, key: str, message: str):
        if not os.environ.get(key):
            self.warnings.append(f"WARNING: {key} not set — {message}")

def validate_and_load() -> dict[str, Any]:
    v = ConfigValidator()

    # Required
    api_key = v.require_str("ANTHROPIC_API_KEY", min_len=20, prefix="sk-ant-")

    # Optional with constraints
    model = v.optional_str("AGENT_MODEL", "claude-haiku-4-5-20251001", choices=[
        "claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"
    ])
    max_tokens = v.require_int("MAX_TOKENS", min_val=1, max_val=32000) or 1024
    port = v.optional_str("PORT", "8000")
    temperature = v.optional_float("TEMPERATURE", 0.7, 0.0, 1.0)
    log_level = v.optional_str("LOG_LEVEL", "INFO", ["DEBUG", "INFO", "WARNING", "ERROR"])
    environment = v.optional_str("ENVIRONMENT", "development",
                                 ["development", "staging", "production"])

    # Cross-field validation
    if environment == "production" and log_level == "DEBUG":
        v.errors.append(ConfigError("LOG_LEVEL", "DEBUG log level not allowed in production"))

    # Warnings for missing optional-but-recommended vars
    v.warn_if_default("SENTRY_DSN", "error tracking disabled")
    v.warn_if_default("REDIS_URL", "using in-memory state (not suitable for multi-instance)")

    # Report
    for warning in v.warnings:
        print(warning, file=sys.stderr)

    if v.errors:
        print("\nFATAL: Configuration validation failed:\n", file=sys.stderr)
        for err in v.errors:
            print(f"  [{err.field}] {err.message}", file=sys.stderr)
        print(f"\n{len(v.errors)} error(s) found. Fix and restart.\n", file=sys.stderr)
        sys.exit(1)

    print(f"Config OK: model={model}, env={environment}, port={port}")
    return {
        "api_key": api_key,
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "log_level": log_level,
        "environment": environment,
        "port": int(port),
    }

# Simulate partial env (in tests, set os.environ before calling)
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key-for-demo-only")
os.environ.setdefault("MAX_TOKENS", "512")
config = validate_and_load()
print(f"Loaded: {config}")
# Expected Token Savings: Prevents wasted startup calls with bad config; reduces debugging time
# Environment: Any production agent; Docker CMD; Lambda init; systemd service
```

### Option 3: YAML Schema Validation with jsonschema

When config comes from a YAML file rather than environment variables, validate it against a JSON Schema before the agent starts.

```python
import yaml
import json
import sys
from pathlib import Path

try:
    from jsonschema import validate, ValidationError, Draft7Validator
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

CONFIG_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["anthropic", "agent", "server"],
    "additionalProperties": False,
    "properties": {
        "anthropic": {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string", "minLength": 10},
                "model": {
                    "type": "string",
                    "enum": ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"]
                },
                "max_tokens": {"type": "integer", "minimum": 1, "maximum": 32000},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            "additionalProperties": False,
        },
        "agent": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "max_concurrent_requests": {"type": "integer", "minimum": 1, "maximum": 1000},
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
            },
            "additionalProperties": False,
        },
        "server": {
            "type": "object",
            "required": ["port"],
            "properties": {
                "port": {"type": "integer", "minimum": 1024, "maximum": 65535},
                "host": {"type": "string"},
                "log_level": {"type": "string", "enum": ["DEBUG", "INFO", "WARNING", "ERROR"]},
            },
            "additionalProperties": False,
        },
    },
}

SAMPLE_CONFIG = {
    "anthropic": {
        "api_key": "sk-ant-demo-key",
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "timeout_seconds": 30,
    },
    "agent": {
        "name": "MyAgent",
        "description": "A helpful assistant",
        "max_concurrent_requests": 10,
        "tools": ["web_search", "code_exec"],
    },
    "server": {
        "port": 8080,
        "host": "0.0.0.0",
        "log_level": "INFO",
    },
}

def validate_yaml_config(config_path: str | None = None, config_dict: dict | None = None) -> dict:
    """Validate config from file or dict. Exits on schema violation."""
    if config_dict:
        config = config_dict
    elif config_path:
        path = Path(config_path)
        if not path.exists():
            print(f"FATAL: Config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)
        with open(path) as f:
            config = yaml.safe_load(f)
    else:
        raise ValueError("Must provide config_path or config_dict")

    if not HAS_JSONSCHEMA:
        print("WARNING: jsonschema not installed — skipping schema validation")
        return config

    validator = Draft7Validator(CONFIG_SCHEMA)
    errors = sorted(validator.iter_errors(config), key=lambda e: list(e.absolute_path))

    if errors:
        print("FATAL: Config schema validation failed:\n", file=sys.stderr)
        for err in errors:
            path = " -> ".join(str(p) for p in err.absolute_path) or "(root)"
            print(f"  [{path}] {err.message}", file=sys.stderr)
        print(f"\n{len(errors)} schema error(s). Fix config and restart.\n", file=sys.stderr)
        sys.exit(1)

    print(f"Config schema valid: {len(json.dumps(config))} bytes")
    return config

# Validate the sample config
cfg = validate_yaml_config(config_dict=SAMPLE_CONFIG)
print(f"Agent name: {cfg['agent']['name']}, model: {cfg['anthropic']['model']}")

# Demonstrate what a bad config looks like
bad_config = {**SAMPLE_CONFIG, "anthropic": {**SAMPLE_CONFIG["anthropic"], "model": "gpt-4"}}
# validate_yaml_config(config_dict=bad_config)  # Would exit(1) with: model must be one of [...]
print("Bad config would have been caught at startup")
# Expected Token Savings: Prevents entire class of misconfiguration runtime errors
# Environment: Containerized agents, Kubernetes deployments, YAML-configured services
```

### Option 4: Health Check Endpoint that Validates Config

Expose a `/healthz` endpoint that validates config liveness — not just process health. Required config values are re-verified on each health check to catch dynamic changes.

```python
import os
import anthropic
from fastapi import FastAPI, Response
from pydantic import BaseModel
from typing import Any
import time

app = FastAPI()

class HealthStatus(BaseModel):
    status: str          # "healthy" | "degraded" | "unhealthy"
    checks: dict[str, Any]
    timestamp: float

def check_api_key() -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}
    if not key.startswith("sk-ant-"):
        return {"ok": False, "error": "ANTHROPIC_API_KEY has invalid format"}
    # Mask key in health response
    return {"ok": True, "key_preview": f"{key[:8]}...{key[-4:]}"}

def check_model_config() -> dict:
    model = os.environ.get("AGENT_MODEL", "claude-haiku-4-5-20251001")
    allowed = {"claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"}
    if model not in allowed:
        return {"ok": False, "error": f"AGENT_MODEL '{model}' not in {allowed}"}
    return {"ok": True, "model": model}

def check_numeric_configs() -> dict:
    errors = []
    for key, min_v, max_v in [
        ("MAX_TOKENS", 1, 32000),
        ("PORT", 1024, 65535),
        ("TIMEOUT_SECONDS", 1, 300),
    ]:
        raw = os.environ.get(key)
        if raw is None:
            continue  # Optional — only validate if set
        try:
            val = int(raw)
            if not (min_v <= val <= max_v):
                errors.append(f"{key}={val} out of range [{min_v},{max_v}]")
        except ValueError:
            errors.append(f"{key}='{raw}' is not an integer")
    return {"ok": len(errors) == 0, "errors": errors}

def check_anthropic_reachable() -> dict:
    """Quick liveness check — doesn't cost tokens."""
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "invalid"))
        # Just instantiating the client validates the key format
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}

@app.get("/healthz", response_model=HealthStatus)
async def health_check(response: Response):
    checks = {
        "api_key": check_api_key(),
        "model_config": check_model_config(),
        "numeric_configs": check_numeric_configs(),
        "anthropic_client": check_anthropic_reachable(),
    }

    all_ok = all(c["ok"] for c in checks.values())
    has_errors = any(not c["ok"] for c in checks.values() if "error" in c)

    if all_ok:
        status = "healthy"
        response.status_code = 200
    elif has_errors:
        status = "unhealthy"
        response.status_code = 503
    else:
        status = "degraded"
        response.status_code = 200

    return HealthStatus(
        status=status,
        checks=checks,
        timestamp=time.time(),
    )

@app.on_event("startup")
async def startup_validation():
    """Validate critical config at startup. Prevents serving requests with bad config."""
    critical_checks = {
        "api_key": check_api_key(),
        "model_config": check_model_config(),
    }
    failures = {k: v for k, v in critical_checks.items() if not v["ok"]}
    if failures:
        import sys
        print(f"FATAL startup config validation failed: {failures}", file=sys.stderr)
        sys.exit(1)
    print("Startup config validation: OK")

print("FastAPI health check config validator defined. Run with uvicorn to use.")
# Expected Token Savings: Kubernetes readiness probes catch bad config before traffic routing
# Environment: Kubernetes, ECS, Cloud Run — anywhere health probes control traffic
```

### Option 5: Typed Config Loader with Dotenv Support

A zero-dependency typed config loader that reads `.env` files, validates types, and reports all errors before the agent starts.

```python
import os
import sys
from typing import TypeVar, Type, get_type_hints, get_origin, get_args, Literal
from pathlib import Path

T = TypeVar("T")

def load_dotenv(path: str = ".env"):
    """Minimal dotenv loader — no dependencies."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key not in os.environ:
            os.environ[key] = value

class TypedConfig:
    """Base class for typed configuration. Call validate() at startup."""

    _MISSING = object()

    @classmethod
    def from_env(cls: Type[T]) -> T:
        """Load config from environment and validate all fields."""
        load_dotenv()
        instance = cls.__new__(cls)
        errors: list[str] = []
        hints = get_type_hints(cls)

        for field_name, field_type in hints.items():
            if field_name.startswith("_"):
                continue

            env_key = field_name.upper()
            raw = os.environ.get(env_key, cls._MISSING)
            default = getattr(cls, field_name, cls._MISSING)

            # Required field check
            if raw is cls._MISSING:
                if default is cls._MISSING:
                    errors.append(f"[{env_key}] Required but not set")
                    continue
                setattr(instance, field_name, default)
                continue

            # Type coercion
            try:
                coerced = cls._coerce(field_name, raw, field_type)
                setattr(instance, field_name, coerced)
            except (ValueError, TypeError) as e:
                errors.append(f"[{env_key}] {e}")

        # Run custom validation
        try:
            instance._validate()
        except ValueError as e:
            errors.append(f"[cross-field] {e}")

        if errors:
            print("FATAL: Config validation failed:", file=sys.stderr)
            for err in errors:
                print(f"  {err}", file=sys.stderr)
            sys.exit(1)

        return instance

    @classmethod
    def _coerce(cls, field: str, raw: str, typ):
        origin = get_origin(typ)
        args = get_args(typ)

        if origin is Literal:
            if raw not in args:
                raise ValueError(f"Must be one of {list(args)}, got '{raw}'")
            return raw
        if typ is bool or typ == bool:
            return raw.lower() in ("1", "true", "yes", "on")
        if typ is int or typ == int:
            return int(raw)
        if typ is float or typ == float:
            return float(raw)
        if typ is str or typ == str:
            return raw
        return raw

    def _validate(self):
        """Override in subclass for cross-field validation."""
        pass

# Define your config using the base class
from typing import Literal as Lit

class AgentConfig(TypedConfig):
    # Type annotations define what's expected — no decorators needed
    anthropic_api_key: str          # Required (no default)
    agent_model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1024
    port: int = 8000
    log_level: Lit["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    environment: Lit["development", "staging", "production"] = "development"
    enable_caching: bool = True
    temperature: float = 0.7

    def _validate(self):
        if not self.anthropic_api_key.startswith("sk-ant-"):
            raise ValueError("ANTHROPIC_API_KEY must start with 'sk-ant-'")
        if not (1 <= self.max_tokens <= 32000):
            raise ValueError(f"MAX_TOKENS must be 1-32000, got {self.max_tokens}")
        if not (0.0 <= self.temperature <= 1.0):
            raise ValueError(f"TEMPERATURE must be 0.0-1.0, got {self.temperature}")
        if self.environment == "production" and self.log_level == "DEBUG":
            raise ValueError("DEBUG logging not allowed in production")

# Set env for demo
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-demo-key-for-testing"
os.environ["MAX_TOKENS"] = "512"
os.environ["ENVIRONMENT"] = "development"

cfg = AgentConfig.from_env()
print(f"Config loaded: model={cfg.agent_model}, env={cfg.environment}, tokens={cfg.max_tokens}")
# Expected Token Savings: Zero-dependency startup validation; catches all config errors before any API call
# Environment: Minimal deployments; Lambda; scripts without pydantic installed
```

### Option 6: Config Diff and Change Detection

Detect when config changes between deployments by hashing config state. Alert when unexpected changes occur — catches accidental environment variable mutations.

```python
import os
import json
import hashlib
import time
from pathlib import Path
from datetime import datetime

CONFIG_SNAPSHOT_PATH = Path("/tmp/agent_config_snapshot.json")

REQUIRED_VARS = ["ANTHROPIC_API_KEY"]
MONITORED_VARS = [
    "ANTHROPIC_API_KEY",
    "AGENT_MODEL",
    "MAX_TOKENS",
    "ENVIRONMENT",
    "PORT",
    "LOG_LEVEL",
    "TEMPERATURE",
    "REDIS_URL",
    "DATABASE_URL",
]

def collect_config_state() -> dict:
    """Collect current config values (mask secrets)."""
    state = {}
    for key in MONITORED_VARS:
        val = os.environ.get(key)
        if val is None:
            state[key] = None
        elif "key" in key.lower() or "secret" in key.lower() or "password" in key.lower():
            # Store hash of secrets, not plaintext
            state[key] = f"sha256:{hashlib.sha256(val.encode()).hexdigest()[:16]}"
        else:
            state[key] = val
    return state

def config_hash(state: dict) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]

def save_snapshot(state: dict):
    snapshot = {
        "config": state,
        "hash": config_hash(state),
        "captured_at": datetime.now().isoformat(),
        "pid": os.getpid(),
    }
    CONFIG_SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2))

def load_snapshot() -> dict | None:
    if not CONFIG_SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_SNAPSHOT_PATH.read_text())
    except Exception:
        return None

def validate_and_diff() -> dict:
    """Validate config and detect changes from last snapshot."""
    import sys

    current = collect_config_state()
    errors = []
    warnings = []

    # Required variable check
    for key in REQUIRED_VARS:
        if current.get(key) is None:
            errors.append(f"Required variable '{key}' is not set")

    # Compare to previous snapshot
    previous = load_snapshot()
    changes = []
    if previous:
        prev_config = previous.get("config", {})
        for key in MONITORED_VARS:
            prev_val = prev_config.get(key)
            curr_val = current.get(key)
            if prev_val != curr_val:
                changes.append({
                    "key": key,
                    "previous": prev_val,
                    "current": curr_val,
                })
                if key == "ENVIRONMENT":
                    warnings.append(f"ENVIRONMENT changed: {prev_val} → {curr_val}")
                elif key in ("ANTHROPIC_API_KEY",):
                    warnings.append(f"{key} changed (hash differs)")

    if errors:
        print("FATAL: Config validation failed:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        sys.exit(1)

    if changes:
        print(f"Config changed ({len(changes)} keys):")
        for ch in changes:
            print(f"  {ch['key']}: {ch['previous']} → {ch['current']}")
    else:
        print("Config unchanged from last startup")

    for w in warnings:
        print(f"WARNING: {w}")

    # Save new snapshot
    save_snapshot(current)

    return {
        "valid": True,
        "config_hash": config_hash(current),
        "changes": changes,
        "warnings": warnings,
    }

# Set demo env
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-demo-key")
os.environ.setdefault("AGENT_MODEL", "claude-haiku-4-5-20251001")
os.environ.setdefault("ENVIRONMENT", "development")

result = validate_and_diff()
print(f"Config hash: {result['config_hash']}")

# Second run simulates a restart with same config
result2 = validate_and_diff()
print(f"Second run — changes: {len(result2['changes'])}")
# Expected Token Savings: Change detection prevents silent config drift between deployments
# Environment: CI/CD pipelines; canary deployments; config-as-code environments
```

## Comparison Table

| Option | Dependencies | Validation Depth | Error Reporting | Best For |
|--------|-------------|-----------------|-----------------|----------|
| 1: Pydantic Settings | pydantic-settings | Deep + type-safe | All-at-once | FastAPI apps, modern Python services |
| 2: Manual Accumulation | None | Medium | All-at-once | Zero-dependency environments |
| 3: YAML + jsonschema | pyyaml, jsonschema | Schema-driven | All-at-once | YAML-configured services, Helm charts |
| 4: Health Check Endpoint | fastapi | Continuous | Per-check | Kubernetes, ECS, Cloud Run |
| 5: Typed Config Loader | None | Type + cross-field | All-at-once | Minimal Lambda/script deployments |
| 6: Config Diff Detection | None | Change-aware | Delta report | CI/CD pipelines, canary deployments |
