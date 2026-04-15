---
layout: solution
title: "Agent Uses Hardcoded API Endpoint — Breaks on Environment Change"
category: config
description: "Agent has production API URL hardcoded in source code. Developer tests in staging but the agent calls production. Staging environment doesn't exist in code. Changing environments requires code changes and redeployment. URL accidentally committed to public repo."
tags: [config, hardcoded, endpoint, environment, url, staging, production, env-vars, configuration]
---

# Agent Uses Hardcoded API Endpoint — Breaks on Environment Change

## Symptom
- Agent always calls `https://api.production.example.com` even when running in staging
- Switching from prod to staging requires changing source code and redeploying
- Agent calls production API during local development — corrupts real data
- Base URL is duplicated in 15 files — updating it requires 15 changes
- Agent works in dev but fails in prod because the hardcoded URL uses a dev hostname
- Public GitHub repo accidentally exposes internal API endpoint structure

## Root Cause
Hardcoded URLs conflate configuration (which environment to talk to) with code (what to do). URLs change across environments, between teams, and over time. Embedding them in source code means every environment change requires a code change. It also breaks the principle that the same build artifact should be deployable to any environment — only configuration should differ.

## Fix

### Option 1: Environment variables for all endpoint configuration

```python
import os
from dataclasses import dataclass

@dataclass(frozen=True)
class APIEndpoints:
    """
    All API endpoints loaded from environment — zero hardcoded URLs.
    """
    base_url: str
    auth_url: str
    webhook_url: str
    internal_service_url: str

    @classmethod
    def from_env(cls) -> "APIEndpoints":
        """Load endpoints from environment with validation"""
        required = {
            "API_BASE_URL": "https://api.example.com",
            "AUTH_URL": "https://auth.example.com",
            "WEBHOOK_URL": "https://hooks.example.com",
            "INTERNAL_SERVICE_URL": "http://internal-service:8080"
        }

        missing = [k for k in required if k not in os.environ]
        if missing:
            raise EnvironmentError(
                f"Missing required endpoint configuration: {missing}\n"
                f"Set these environment variables before starting the agent.\n"
                f"Example values:\n" +
                "\n".join(f"  {k}={v}" for k, v in required.items() if k in missing)
            )

        return cls(
            base_url=os.environ["API_BASE_URL"].rstrip("/"),
            auth_url=os.environ["AUTH_URL"].rstrip("/"),
            webhook_url=os.environ["WEBHOOK_URL"].rstrip("/"),
            internal_service_url=os.environ["INTERNAL_SERVICE_URL"].rstrip("/"),
        )

    def validate(self):
        """Sanity-check that endpoints look like valid URLs"""
        for name, url in {
            "base_url": self.base_url,
            "auth_url": self.auth_url,
        }.items():
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"{name} must start with http:// or https://: {url}")

# Load once at startup — fail fast if misconfigured:
endpoints = APIEndpoints.from_env()
endpoints.validate()
print(f"Agent configured for: {endpoints.base_url}")

# Usage — no URLs in business logic:
async def fetch_user(user_id: str) -> dict:
    url = f"{endpoints.base_url}/users/{user_id}"
    # ...

async def refresh_token(refresh_token: str) -> dict:
    url = f"{endpoints.auth_url}/oauth/token"
    # ...
```

### Option 2: Environment-specific config files

```python
import os
import json
from pathlib import Path
from dataclasses import dataclass

@dataclass
class EnvironmentConfig:
    name: str
    api_base_url: str
    auth_url: str
    log_level: str
    feature_flags: dict
    timeouts: dict

ENV_CONFIGS = {
    "development": EnvironmentConfig(
        name="development",
        api_base_url="http://localhost:8000",
        auth_url="http://localhost:9000",
        log_level="DEBUG",
        feature_flags={"experimental_tools": True, "verbose_errors": True},
        timeouts={"api": 60, "auth": 30}
    ),
    "staging": EnvironmentConfig(
        name="staging",
        api_base_url="https://api.staging.example.com",
        auth_url="https://auth.staging.example.com",
        log_level="INFO",
        feature_flags={"experimental_tools": True, "verbose_errors": False},
        timeouts={"api": 30, "auth": 15}
    ),
    "production": EnvironmentConfig(
        name="production",
        api_base_url="https://api.example.com",
        auth_url="https://auth.example.com",
        log_level="WARNING",
        feature_flags={"experimental_tools": False, "verbose_errors": False},
        timeouts={"api": 30, "auth": 15}
    ),
}

def load_config() -> EnvironmentConfig:
    """
    Load config for current environment.
    ENV defaults to 'development' — must be explicitly set for staging/production.
    """
    env = os.environ.get("AGENT_ENV", "development").lower()

    if env not in ENV_CONFIGS:
        raise ValueError(
            f"Unknown environment '{env}'. "
            f"Valid values: {list(ENV_CONFIGS.keys())}. "
            f"Set AGENT_ENV environment variable."
        )

    config = ENV_CONFIGS[env]

    # Allow per-env overrides via environment variables:
    if "API_BASE_URL" in os.environ:
        config.api_base_url = os.environ["API_BASE_URL"]

    print(f"Loaded config for environment: {env} ({config.api_base_url})")

    # Safety guard — prevent dev config from running in production:
    if env == "production" and "localhost" in config.api_base_url:
        raise RuntimeError(
            "CRITICAL: Production environment points to localhost. Refusing to start."
        )

    return config

config = load_config()
```

### Option 3: Pydantic settings with environment variable binding

```python
from pydantic import BaseSettings, AnyHttpUrl, validator
import os

class AgentSettings(BaseSettings):
    """
    All configuration loaded from environment variables.
    Pydantic validates types, formats, and required fields automatically.
    """

    # Endpoints — all required, no defaults
    api_base_url: AnyHttpUrl
    auth_service_url: AnyHttpUrl
    webhook_base_url: AnyHttpUrl

    # Optional with sensible defaults
    internal_timeout_seconds: int = 30
    max_retries: int = 3
    log_level: str = "INFO"

    # Feature flags
    enable_caching: bool = True
    enable_streaming: bool = True

    @validator("api_base_url", "auth_service_url")
    def strip_trailing_slash(cls, v):
        return str(v).rstrip("/")

    @validator("log_level")
    def validate_log_level(cls, v):
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return v.upper()

    class Config:
        env_prefix = "AGENT_"  # All vars must be prefixed: AGENT_API_BASE_URL, etc.
        env_file = ".env"       # Load from .env in development
        env_file_encoding = "utf-8"
        case_sensitive = False

settings = AgentSettings()

# Access anywhere without re-reading env vars:
print(settings.api_base_url)  # → https://api.example.com (from AGENT_API_BASE_URL)
```

### Option 4: Audit and remove hardcoded URLs

```python
# scripts/audit_hardcoded_urls.py
# Run in CI to prevent new hardcoded URLs from being merged

import re
import sys
from pathlib import Path

HARDCODED_URL_PATTERN = re.compile(
    r'["\']https?://(?!localhost|127\.0\.0\.1|0\.0\.0\.0)[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}',
    re.IGNORECASE
)

ALLOWED_PATTERNS = [
    r'example\.com',     # Documentation examples
    r'schema\.org',      # Schema URLs (not API endpoints)
    r'anthropic\.com',   # SDK base URL (acceptable)
    r'openai\.com',      # SDK base URL (acceptable)
    r'github\.com/.*#',  # GitHub anchor links in comments
]

def check_file(path: Path) -> list[str]:
    violations = []
    content = path.read_text(encoding="utf-8", errors="ignore")

    for match in HARDCODED_URL_PATTERN.finditer(content):
        url = match.group(0).strip("'\"")
        line_num = content[:match.start()].count('\n') + 1

        # Check against allowed patterns
        if any(re.search(p, url, re.IGNORECASE) for p in ALLOWED_PATTERNS):
            continue

        # Skip URLs in comments that are examples
        line = content.split('\n')[line_num - 1]
        if any(x in line.lower() for x in ['# example', '# e.g.', '# see', 'example.com']):
            continue

        violations.append(f"{path}:{line_num}: Hardcoded URL: {url}")

    return violations

def main():
    source_dir = Path(".")
    all_violations = []

    for path in source_dir.rglob("*.py"):
        if any(skip in str(path) for skip in [".venv", "__pycache__", "test_", "_test"]):
            continue
        all_violations.extend(check_file(path))

    if all_violations:
        print("HARDCODED URL VIOLATIONS FOUND:")
        for v in all_violations:
            print(f"  {v}")
        print(f"\nFix: Move URLs to environment variables or config files.")
        sys.exit(1)
    else:
        print(f"No hardcoded URLs found.")
        sys.exit(0)

if __name__ == "__main__":
    main()
```

### Option 5: Docker Compose environment injection

```yaml
# docker-compose.yml — environment-specific endpoint injection
version: "3.9"

services:
  agent-dev:
    image: my-agent:latest
    profiles: ["dev"]
    environment:
      AGENT_ENV: development
      AGENT_API_BASE_URL: http://api-mock:8000
      AGENT_AUTH_SERVICE_URL: http://auth-mock:9000
      AGENT_LOG_LEVEL: DEBUG
    depends_on:
      - api-mock
      - auth-mock

  agent-staging:
    image: my-agent:latest
    profiles: ["staging"]
    environment:
      AGENT_ENV: staging
      AGENT_API_BASE_URL: https://api.staging.example.com
      AGENT_AUTH_SERVICE_URL: https://auth.staging.example.com
      AGENT_LOG_LEVEL: INFO

  # Local API mock for development — no real service needed
  api-mock:
    image: mockoon/cli:latest
    profiles: ["dev"]
    volumes:
      - ./mocks/api.json:/data/api.json
    command: --data /data/api.json --port 8000

  auth-mock:
    image: mockoon/cli:latest
    profiles: ["dev"]
    volumes:
      - ./mocks/auth.json:/data/auth.json
    command: --data /data/auth.json --port 9000

# Usage:
# Development: docker compose --profile dev up
# Staging:     docker compose --profile staging up
```

### Option 6: .env files per environment — never committed

```bash
# .env.example — committed to repo (no real values)
AGENT_ENV=development
AGENT_API_BASE_URL=https://api.example.com
AGENT_AUTH_SERVICE_URL=https://auth.example.com
AGENT_WEBHOOK_BASE_URL=https://hooks.example.com
AGENT_LOG_LEVEL=INFO

# .gitignore — never commit actual .env files
# .env
# .env.local
# .env.staging
# .env.production
```

```python
# Load environment-specific .env file at startup:
import os
from pathlib import Path
from dotenv import load_dotenv

def load_environment():
    """Load the appropriate .env file for the current environment"""
    env = os.environ.get("AGENT_ENV", "development")

    env_files = [
        Path(f".env.{env}.local"),   # Machine-local overrides (highest priority)
        Path(f".env.{env}"),          # Environment-specific
        Path(".env.local"),           # Local overrides for any environment
        Path(".env"),                 # Shared defaults (lowest priority)
    ]

    for env_file in env_files:
        if env_file.exists():
            load_dotenv(env_file, override=False)  # Don't override existing env vars
            print(f"Loaded: {env_file}")

    print(f"Environment: {env}")
    print(f"API base: {os.environ.get('AGENT_API_BASE_URL', 'NOT SET')}")

load_environment()
```

## Configuration Priority Order

| Source | Priority | Use Case |
|---|---|---|
| Actual environment variables | Highest | CI/CD, container orchestrators |
| `.env.{env}.local` | High | Developer machine overrides |
| `.env.{env}` | Medium | Per-environment defaults |
| `.env.local` | Medium | Local development |
| `.env` | Low | Shared defaults |
| Code defaults | Lowest | Only for truly optional config |
| Hardcoded in source | Never | Breaks environment portability |

## Expected Token Savings
Agent calls wrong environment → corrupts data → debug session + rollback: ~30,000 tokens
Environment-based config → correct environment always targeted: 0 environment confusion

## Environment
- Any agent deployed across multiple environments (dev/staging/prod); critical for any team with more than one developer or more than one deployment target
- Source: direct experience; hardcoded endpoints are the most common cause of production incidents from misconfigured agents
