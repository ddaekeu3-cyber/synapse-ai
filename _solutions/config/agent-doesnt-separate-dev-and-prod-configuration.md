---
layout: solution
title: "Agent Doesn't Separate Dev and Prod Configuration"
category: config
description: "Agent uses the same configuration for development and production, causing prod data to be processed by dev models, dev API keys to exhaust prod quotas, or debug logging to leak sensitive data."
tags: [config, environment, production, reliability, security]
---

## Symptom

A developer runs the agent locally with `DEBUG=true` and the production database URL accidentally checked into the repo. The agent processes real user records with debug logging enabled, exposing PII in logs. Alternatively, the prod agent uses a dev API key with a low rate limit, causing 429 errors under real load. In both cases, configuration meant for one environment is silently used in another.

## Root Cause

The agent reads a single flat config file or a single set of environment variables without distinguishing between environments. Without explicit environment gating (`if ENV == "production"`) or per-environment config files, any setting can bleed across contexts. The problem compounds when `.env` files are shared between team members or when CI/CD pipelines use the same config block for all stages.

## Fix

### Option 1 — Environment-aware config class with Pydantic

```python
import os
import anthropic
from pydantic import BaseModel, field_validator
from typing import Literal

class Config(BaseModel):
    env:           Literal["development", "staging", "production"] = "development"
    model:         str  = "claude-haiku-4-5-20251001"
    max_tokens:    int  = 256
    log_level:     str  = "DEBUG"
    debug_prompts: bool = True   # log full prompts in dev only

    @field_validator("model")
    @classmethod
    def validate_model_for_env(cls, v, info):
        env = info.data.get("env", "development")
        if env == "production" and v == "claude-haiku-4-5-20251001":
            print("[config] WARNING: using haiku in production — consider sonnet for better quality")
        return v

    @field_validator("debug_prompts")
    @classmethod
    def no_debug_in_prod(cls, v, info):
        if info.data.get("env") == "production" and v:
            raise ValueError("debug_prompts must be False in production (PII risk)")
        return v

ENV_DEFAULTS = {
    "development": {
        "model":         "claude-haiku-4-5-20251001",
        "max_tokens":    256,
        "log_level":     "DEBUG",
        "debug_prompts": True,
    },
    "staging": {
        "model":         "claude-haiku-4-5-20251001",
        "max_tokens":    512,
        "log_level":     "INFO",
        "debug_prompts": False,
    },
    "production": {
        "model":         "claude-sonnet-4-6",
        "max_tokens":    1024,
        "log_level":     "WARNING",
        "debug_prompts": False,
    },
}

def load_config() -> Config:
    env = os.environ.get("AGENT_ENV", "development")
    defaults = ENV_DEFAULTS.get(env, ENV_DEFAULTS["development"])
    # Environment variables override defaults
    return Config(
        env=env,
        model=os.environ.get("AGENT_MODEL", defaults["model"]),
        max_tokens=int(os.environ.get("AGENT_MAX_TOKENS", defaults["max_tokens"])),
        log_level=os.environ.get("LOG_LEVEL", defaults["log_level"]),
        debug_prompts=os.environ.get("DEBUG_PROMPTS", str(defaults["debug_prompts"])).lower() == "true",
    )

cfg = load_config()
print(f"[config] env={cfg.env} model={cfg.model} debug={cfg.debug_prompts}")

client = anthropic.Anthropic()
response = client.messages.create(
    model=cfg.model,
    max_tokens=cfg.max_tokens,
    messages=[{"role": "user", "content": "Hello from env-aware config."}],
)
if cfg.debug_prompts:
    print(f"[debug] full response: {response.content[0].text}")
else:
    print(f"[agent] done (response omitted in {cfg.env})")
```

**Expected Token Savings:** Production uses sonnet with higher max_tokens for quality; dev uses haiku with low max_tokens for cheap iteration — correct model selection per environment reduces waste.
**Environment:** Any Python agent; Pydantic validation catches misconfiguration at startup before any API call is made.

---

### Option 2 — Per-environment config files with explicit loading

```python
import os
import json
import anthropic
from pathlib import Path

# Config files live in config/{env}.json — never committed with real secrets
CONFIG_DIR = Path(__file__).parent / "config"

BASE_CONFIG = {
    "model":         "claude-haiku-4-5-20251001",
    "max_tokens":    256,
    "log_requests":  False,
    "retry_limit":   3,
    "timeout":       30,
}

ENV_OVERRIDES = {
    "development": {
        "log_requests": True,
        "retry_limit":  1,       # fail fast in dev
        "timeout":      10,
    },
    "staging": {
        "model":       "claude-haiku-4-5-20251001",
        "max_tokens":  512,
        "retry_limit": 2,
    },
    "production": {
        "model":       "claude-sonnet-4-6",
        "max_tokens":  1024,
        "retry_limit": 5,
        "timeout":     60,
    },
}

def load_config() -> dict:
    env = os.environ.get("AGENT_ENV", "development")
    cfg = dict(BASE_CONFIG)

    # Apply env-specific overrides from code
    cfg.update(ENV_OVERRIDES.get(env, {}))

    # Apply file-based overrides (for secrets / deployment-specific values)
    config_file = CONFIG_DIR / f"{env}.json"
    if config_file.exists():
        with open(config_file) as f:
            file_cfg = json.load(f)
        # File can override anything EXCEPT log_requests in production
        if env == "production":
            file_cfg.pop("log_requests", None)
        cfg.update(file_cfg)

    cfg["env"] = env
    print(f"[config] loaded env={env}: model={cfg['model']}, log={cfg['log_requests']}")
    return cfg

cfg = load_config()

client = anthropic.Anthropic()
response = client.messages.create(
    model=cfg["model"],
    max_tokens=cfg["max_tokens"],
    messages=[{"role": "user", "content": "Hello."}],
)
if cfg.get("log_requests"):
    print(f"[debug] response: {response.content[0].text}")
```

**Expected Token Savings:** Per-environment model selection (haiku in dev, sonnet in prod) means dev runs cost a fraction of prod; misconfigured dev runs against prod endpoints are prevented.
**Environment:** Teams with explicit staging pipelines; CI/CD systems where `AGENT_ENV` is set per pipeline stage.

---

### Option 3 — dotenv hierarchy: .env.development / .env.production

```python
import os
import anthropic
from pathlib import Path

# pip install python-dotenv
from dotenv import load_dotenv

def load_env_hierarchy() -> str:
    """
    Load environment variables in priority order:
    1. Existing environment (CI/CD sets these — highest priority)
    2. .env.{AGENT_ENV}  (environment-specific overrides)
    3. .env.local        (local developer overrides — gitignored)
    4. .env              (shared defaults — committed, no secrets)
    """
    env = os.environ.get("AGENT_ENV", "development")

    # Load in reverse priority (later loads override earlier)
    base = Path(".")
    load_dotenv(base / ".env",            override=False)
    load_dotenv(base / ".env.local",      override=True)
    load_dotenv(base / f".env.{env}",     override=True)
    # Existing env vars are NOT overridden (load_dotenv default behaviour)

    print(f"[env] loaded hierarchy for AGENT_ENV={env}")
    return env

env = load_env_hierarchy()

# Validate required vars are present
required = {"ANTHROPIC_API_KEY"}
missing = required - set(os.environ.keys())
if missing:
    raise RuntimeError(f"Missing required env vars: {missing}")

client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY automatically

# Model selection based on env
MODEL_MAP = {
    "development": "claude-haiku-4-5-20251001",
    "staging":     "claude-haiku-4-5-20251001",
    "production":  "claude-sonnet-4-6",
}
model = os.environ.get("AGENT_MODEL", MODEL_MAP.get(env, "claude-haiku-4-5-20251001"))

response = client.messages.create(
    model=model,
    max_tokens=256,
    messages=[{"role": "user", "content": "Hello from dotenv hierarchy."}],
)
print(f"[{env}] {response.content[0].text[:60]}")

# File structure:
# .env              → AGENT_ENV=development (committed, safe defaults)
# .env.local        → developer overrides (gitignored)
# .env.development  → dev-specific (committed, no secrets)
# .env.staging      → staging-specific (committed, no secrets)
# .env.production   → prod-specific (gitignored or managed by CI/CD)
```

**Expected Token Savings:** The hierarchy prevents production API keys from being used in development; dev uses haiku by default, reducing iteration cost by ~10x vs sonnet.
**Environment:** Small teams following the twelve-factor app pattern; local development with Docker Compose.

---

### Option 4 — Feature flags per environment: control agent behaviour at runtime

```python
import os
import anthropic

client = anthropic.Anthropic()

class FeatureFlags:
    """Runtime behaviour switches that differ by environment."""

    def __init__(self):
        env = os.environ.get("AGENT_ENV", "development")
        self._env = env
        self._flags = self._load(env)

    def _load(self, env: str) -> dict:
        defaults = {
            "enable_extended_thinking": False,
            "enable_prompt_caching":    False,
            "stream_responses":         False,
            "max_parallel_tasks":       2,
            "enable_cost_tracking":     False,
            "strict_output_validation": False,
        }
        overrides = {
            "development": {
                "max_parallel_tasks": 1,
                "strict_output_validation": True,   # catch bugs early
            },
            "staging": {
                "enable_prompt_caching":    True,
                "stream_responses":         True,
                "max_parallel_tasks":       4,
                "enable_cost_tracking":     True,
                "strict_output_validation": True,
            },
            "production": {
                "enable_extended_thinking": True,
                "enable_prompt_caching":    True,
                "stream_responses":         True,
                "max_parallel_tasks":       10,
                "enable_cost_tracking":     True,
            },
        }
        return {**defaults, **overrides.get(env, {})}

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._flags.get(name, False)

    def summary(self) -> dict:
        return {"env": self._env, **self._flags}

flags = FeatureFlags()
print(f"[flags] {flags.summary()}")

# Use flags to control agent behaviour
def run_task(prompt: str) -> str:
    kwargs = {
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "messages":   [{"role": "user", "content": prompt}],
    }
    if flags.enable_prompt_caching:
        kwargs["system"] = [{"type": "text", "text": "You are a helpful assistant.",
                              "cache_control": {"type": "ephemeral"}}]
        kwargs["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}

    if flags.stream_responses:
        with client.messages.stream(**kwargs) as stream:
            result = ""
            for text in stream.text_stream:
                result += text
            return result
    else:
        response = client.messages.create(**kwargs)
        return response.content[0].text

result = run_task("Explain feature flags in one sentence.")
print(f"[agent] {result[:80]}")
```

**Expected Token Savings:** Prompt caching and extended thinking are disabled in dev (reducing iteration cost); they activate automatically in production where the benefit justifies the setup overhead.
**Environment:** Agents with optional expensive features (extended thinking, caching); gradual rollout of new capabilities per environment.

---

### Option 5 — Config schema validation at startup with environment assertion

```python
import os
import sys
import anthropic

REQUIRED_VARS = {
    "development": ["ANTHROPIC_API_KEY"],
    "staging":     ["ANTHROPIC_API_KEY", "DATABASE_URL"],
    "production":  ["ANTHROPIC_API_KEY", "DATABASE_URL", "SENTRY_DSN", "SECRET_KEY"],
}

FORBIDDEN_IN_PROD = {
    "DEBUG":          "Debug mode must be off in production.",
    "ALLOW_MOCK_API": "Mock API must not be enabled in production.",
}

def assert_environment() -> str:
    env = os.environ.get("AGENT_ENV", "development")

    # Check required variables
    required = REQUIRED_VARS.get(env, [])
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"[config] FATAL: missing required vars for {env}: {missing}", file=sys.stderr)
        sys.exit(1)

    # Check forbidden settings
    if env == "production":
        for var, reason in FORBIDDEN_IN_PROD.items():
            if os.environ.get(var, "").lower() in ("1", "true", "yes"):
                print(f"[config] FATAL: {var} is set in production. {reason}", file=sys.stderr)
                sys.exit(1)

    # Warn about dev-only settings bleeding into staging
    if env == "staging" and os.environ.get("DEBUG"):
        print("[config] WARNING: DEBUG is set in staging — this will log sensitive data")

    print(f"[config] environment '{env}' validated OK")
    return env

env = assert_environment()

# Environment-specific client configuration
CLIENT_KWARGS = {
    "development": {"timeout": 10.0},
    "staging":     {"timeout": 30.0},
    "production":  {"timeout": 60.0, "max_retries": 5},
}

client = anthropic.Anthropic(**CLIENT_KWARGS.get(env, {}))
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": f"Running in {env}."}],
)
print(f"[{env}] {response.content[0].text[:60]}")
```

**Expected Token Savings:** Startup assertion catches misconfiguration before any API call; prevents expensive production runs from accidentally using debug settings that double token usage through verbose logging.
**Environment:** All agents with deployment pipelines; `sys.exit(1)` on misconfiguration integrates with Docker health checks and Kubernetes liveness probes.

---

### Option 6 — Immutable config object passed through dependency injection

```python
import os
from dataclasses import dataclass, field
from typing import ClassVar
import anthropic

@dataclass(frozen=True)   # immutable — cannot be accidentally mutated at runtime
class AgentConfig:
    env:            str
    model:          str
    max_tokens:     int
    timeout:        float
    retry_limit:    int
    log_sensitive:  bool
    base_url:       str

    # Class-level registry of valid environments
    VALID_ENVS: ClassVar[set] = {"development", "staging", "production"}

    def __post_init__(self):
        if self.env not in self.VALID_ENVS:
            raise ValueError(f"env must be one of {self.VALID_ENVS}, got '{self.env}'")
        if self.log_sensitive and self.env == "production":
            raise ValueError("log_sensitive=True is forbidden in production")

    @classmethod
    def from_env(cls) -> "AgentConfig":
        env = os.environ.get("AGENT_ENV", "development")
        profiles = {
            "development": dict(model="claude-haiku-4-5-20251001", max_tokens=256,
                                timeout=10.0, retry_limit=1, log_sensitive=True,
                                base_url="https://api.anthropic.com"),
            "staging":     dict(model="claude-haiku-4-5-20251001", max_tokens=512,
                                timeout=30.0, retry_limit=3, log_sensitive=False,
                                base_url="https://api.anthropic.com"),
            "production":  dict(model="claude-sonnet-4-6", max_tokens=1024,
                                timeout=60.0, retry_limit=5, log_sensitive=False,
                                base_url="https://api.anthropic.com"),
        }
        profile = profiles.get(env, profiles["development"])
        return cls(env=env, **profile)

class AgentService:
    """Service that accepts config via DI — never reads env vars directly."""
    def __init__(self, config: AgentConfig):
        self._cfg    = config
        self._client = anthropic.Anthropic(
            timeout=config.timeout,
            max_retries=config.retry_limit,
            base_url=config.base_url,
        )

    def run(self, prompt: str) -> str:
        resp = self._client.messages.create(
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        result = resp.content[0].text
        if self._cfg.log_sensitive:
            print(f"[debug] full response: {result}")
        return result

config = AgentConfig.from_env()
print(f"[config] {config.env}: model={config.model}, log_sensitive={config.log_sensitive}")

service = AgentService(config)
result = service.run("What environment am I running in?")
print(f"[agent] {result[:60]}")
```

**Expected Token Savings:** Frozen immutable config prevents runtime mutation bugs where a debug flag gets enabled mid-run; DI makes the config explicit and testable — test code can inject a dev config to use cheap haiku even in integration tests.
**Environment:** Larger codebases with multiple services; test suites that need to inject config without touching environment variables.

---

## Comparison

| Option | Config Source | Immutable | Startup Validation | Secret-Safe | Best For |
|---|---|---|---|---|---|
| 1. Pydantic config class | Env vars + code | Yes (frozen) | Yes (validator) | Yes | Type-safe config with validation rules |
| 2. Per-env JSON files | Files + env vars | No | Partial | Partial | Teams with explicit file-per-env workflow |
| 3. dotenv hierarchy | .env files | No | No | Yes (gitignore) | Twelve-factor apps; Docker Compose |
| 4. Feature flags | Code + env vars | No | No | Yes | Gradual feature rollout; optional capabilities |
| 5. Startup assertion | Env vars | No | Yes (sys.exit) | Yes | CI/CD pipelines; container orchestration |
| 6. Immutable DI config | Env vars + code | Yes (frozen) | Yes (__post_init__) | Yes | Large codebases; testable services |
