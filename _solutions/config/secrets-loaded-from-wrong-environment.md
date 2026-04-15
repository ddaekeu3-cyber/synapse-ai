---
layout: solution
title: "Agent Loads Secrets from Wrong Environment — Dev Credentials Hit Production"
category: config
description: "Agent running in CI or staging accidentally picks up production credentials from environment variables, .env files, or mounted secrets. Or the reverse: production agent uses dev API keys with lower rate limits."
tags: [config, secrets, environment, dev, prod, credentials, env-file, contamination]
---

# Agent Loads Secrets from Wrong Environment — Dev Credentials Hit Production

## Symptom
- Dev agent accidentally calls production database — real data modified or deleted
- Production agent uses dev API key — lower rate limits cause unexpected 429s
- CI pipeline picks up `.env` from developer's machine via Docker volume mount
- Agent uses `STRIPE_KEY` from environment that points to live Stripe instead of test mode
- Rotating production secret breaks staging because both shared the same key name

## Root Cause
Credential resolution is implicit: `os.environ.get("API_KEY")` picks up whatever is in the environment without verifying it matches the intended deployment target. When environments are not strictly isolated — shared `.env` files, inherited shell environments, Docker bind mounts, or CI/CD that injects production secrets into test jobs — the wrong credentials silently take effect.

## Fix

### Option 1: Require explicit environment declaration and validate credentials match

```python
import os
import hashlib

EXPECTED_ENV = os.environ.get("AGENT_ENV")  # "dev", "staging", "prod"

# Map environment to expected credential fingerprints (first 8 chars of key prefix)
CREDENTIAL_ENV_MARKERS = {
    "dev": {
        "STRIPE_KEY": "sk_test_",          # Test keys start with sk_test_
        "OPENAI_API_KEY": "sk-proj-dev",   # Dev project prefix
        "DATABASE_URL": "localhost",        # Dev DB is always localhost
    },
    "prod": {
        "STRIPE_KEY": "sk_live_",          # Live keys start with sk_live_
        "DATABASE_URL": ".rds.amazonaws",  # Prod DB is RDS
    }
}

def validate_environment_credentials(env: str) -> list[str]:
    """
    Verify loaded credentials match the expected environment.
    Returns list of mismatches found.
    """
    if env not in CREDENTIAL_ENV_MARKERS:
        return [f"Unknown AGENT_ENV='{env}'. Expected: {list(CREDENTIAL_ENV_MARKERS.keys())}"]

    mismatches = []
    markers = CREDENTIAL_ENV_MARKERS[env]

    for key, expected_prefix in markers.items():
        value = os.environ.get(key, "")
        if not value:
            mismatches.append(f"{key}: not set")
        elif not value.startswith(expected_prefix):
            mismatches.append(
                f"{key}: expected prefix '{expected_prefix}', "
                f"got '{value[:len(expected_prefix)]}...' — wrong environment?"
            )

    return mismatches

# At startup:
if not EXPECTED_ENV:
    raise RuntimeError("AGENT_ENV must be set ('dev', 'staging', or 'prod')")

mismatches = validate_environment_credentials(EXPECTED_ENV)
if mismatches:
    raise RuntimeError(
        f"Credential mismatch for AGENT_ENV={EXPECTED_ENV}:\n" +
        "\n".join(f"  - {m}" for m in mismatches)
    )
```

### Option 2: Namespace secrets by environment

```python
import os

class EnvironmentConfig:
    """
    Load secrets with environment-namespaced keys.
    DEV_DATABASE_URL, STAGING_DATABASE_URL, PROD_DATABASE_URL
    — only the correct one is accessible per environment.
    """

    def __init__(self, env: str):
        valid_envs = {"dev", "staging", "prod"}
        if env not in valid_envs:
            raise ValueError(f"Invalid environment: '{env}'. Must be one of {valid_envs}")
        self.env = env.upper()

    def get(self, key: str, required: bool = True) -> str:
        """Get an environment-namespaced secret"""
        namespaced_key = f"{self.env}_{key}"
        value = os.environ.get(namespaced_key)

        if not value and required:
            raise KeyError(
                f"Required secret '{namespaced_key}' not set. "
                f"Set {namespaced_key} in your environment or secrets manager."
            )
        return value or ""

    def database_url(self) -> str:
        return self.get("DATABASE_URL")

    def api_key(self, service: str) -> str:
        return self.get(f"{service}_API_KEY")

# Usage:
config = EnvironmentConfig(env=os.environ["AGENT_ENV"])
db_url = config.database_url()         # Reads PROD_DATABASE_URL
stripe_key = config.api_key("STRIPE")  # Reads PROD_STRIPE_API_KEY

# Can't accidentally use prod secrets in dev because key names are different
```

### Option 3: Secrets manager with environment enforcement

```python
import boto3

class SecretsManagerConfig:
    """
    Load secrets from AWS Secrets Manager with environment path enforcement.
    Secrets are stored at /dev/stripe-key, /prod/stripe-key — never shared.
    """

    def __init__(self, env: str, region: str = "us-east-1"):
        valid = {"dev", "staging", "prod"}
        if env not in valid:
            raise ValueError(f"Invalid env: {env}")
        self.env = env
        self.client = boto3.client("secretsmanager", region_name=region)
        self._cache: dict[str, str] = {}

    def get_secret(self, name: str) -> str:
        """
        Fetch secret from /{env}/{name} path.
        Physically impossible to load prod secret in dev — different path.
        """
        secret_path = f"/{self.env}/{name}"

        if secret_path not in self._cache:
            try:
                response = self.client.get_secret_value(SecretId=secret_path)
                self._cache[secret_path] = response["SecretString"]
            except self.client.exceptions.ResourceNotFoundException:
                raise KeyError(
                    f"Secret not found: {secret_path}\n"
                    f"Create it: aws secretsmanager create-secret "
                    f"--name {secret_path} --secret-string 'value'"
                )

        return self._cache[secret_path]

config = SecretsManagerConfig(env="prod")
stripe_key = config.get_secret("stripe-key")   # → /prod/stripe-key
db_password = config.get_secret("db-password") # → /prod/db-password
# Dev agent with env="dev" gets /dev/stripe-key — physically separate secret
```

### Option 4: Detect and block production credentials in non-prod environments

```python
import os
import re

# Patterns that indicate production credentials
PROD_CREDENTIAL_PATTERNS = [
    (r"^sk_live_", "Stripe live key"),
    (r"^prod-", "production-prefixed key"),
    (r"\.rds\.amazonaws\.com", "AWS RDS production database"),
    (r"mongodb\+srv://.*\.mongodb\.net", "MongoDB Atlas production cluster"),
    (r"redis://.*\.cache\.amazonaws\.com", "AWS ElastiCache production Redis"),
]

def block_prod_credentials_in_dev():
    """
    Raise immediately if production credentials are detected in a non-prod environment.
    Call this at startup in dev and staging.
    """
    current_env = os.environ.get("AGENT_ENV", "unknown")
    if current_env == "prod":
        return  # Production — allow production credentials

    violations = []
    for env_var, value in os.environ.items():
        for pattern, description in PROD_CREDENTIAL_PATTERNS:
            if re.search(pattern, value):
                violations.append(
                    f"{env_var} contains {description} (matched: {pattern})"
                )
                break

    if violations:
        raise RuntimeError(
            f"Production credentials detected in {current_env} environment!\n" +
            "\n".join(f"  - {v}" for v in violations) +
            "\nThis is a safety block. Remove production credentials from this environment."
        )

# Call at startup in dev/staging:
block_prod_credentials_in_dev()
```

### Option 5: Environment-aware .env file loading

```python
from pathlib import Path
from dotenv import load_dotenv
import os

def load_environment_config(env: str = None) -> None:
    """
    Load the correct .env file for the current environment.
    Never loads production secrets in dev/test.
    """
    env = env or os.environ.get("AGENT_ENV", "dev")

    # Precedence: .env.{env}.local → .env.{env} → .env.local (never .env in prod)
    env_files = [
        Path(f".env.{env}.local"),
        Path(f".env.{env}"),
    ]
    if env != "prod":
        env_files.append(Path(".env.local"))
        # Never load bare .env in production — it might contain dev credentials

    loaded = False
    for env_file in env_files:
        if env_file.exists():
            load_dotenv(env_file, override=True)
            print(f"Loaded config from {env_file}")
            loaded = True
            break

    if not loaded:
        print(f"No .env file found for environment '{env}'. Using system environment only.")

    # Verify environment is set correctly after loading
    actual_env = os.environ.get("AGENT_ENV")
    if actual_env and actual_env != env:
        raise RuntimeError(
            f"Environment mismatch: expected AGENT_ENV='{env}' "
            f"but loaded config has AGENT_ENV='{actual_env}'"
        )

# File structure:
# .env.dev      — dev credentials (committed, non-sensitive)
# .env.staging  — staging credentials (committed or CI-injected)
# .env.prod     — NEVER committed; injected by deployment system only
# .env.local    — developer overrides (gitignored)
```

### Option 6: CI/CD pipeline credential isolation

```yaml
# GitHub Actions: environment-scoped secrets prevent cross-contamination
name: Deploy

jobs:
  deploy-staging:
    environment: staging          # Only staging secrets are available here
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run agent
        env:
          AGENT_ENV: staging
          STRIPE_KEY: ${{ secrets.STAGING_STRIPE_KEY }}   # staging-scoped secret
          DATABASE_URL: ${{ secrets.STAGING_DATABASE_URL }}
        run: python agent.py

  deploy-prod:
    environment: production       # Only production secrets are available here
    runs-on: ubuntu-latest
    needs: [deploy-staging]       # Must pass staging first
    steps:
      - uses: actions/checkout@v4
      - name: Run agent
        env:
          AGENT_ENV: prod
          STRIPE_KEY: ${{ secrets.PROD_STRIPE_KEY }}      # prod-scoped secret
          DATABASE_URL: ${{ secrets.PROD_DATABASE_URL }}
        run: python agent.py

# GitHub environment protection rules:
# - production environment requires manual approval
# - staging secrets are NOT accessible in production jobs (and vice versa)
```

## Environment Contamination Risk Matrix

| Source | Risk | Prevention |
|---|---|---|
| Shared `.env` file across envs | High | Separate `.env.dev` / `.env.prod` files |
| CI inherits developer shell env | High | Explicit `env:` block in CI config |
| Docker volume mounts `.env` | Medium | Use secrets manager, not file mounts |
| Same secret name, different values | Medium | Namespace keys by env (`PROD_DB_URL`) |
| Secrets manager without env path | Medium | Use `/prod/key` vs `/dev/key` paths |
| Environment-scoped CI secrets | Low | GitHub Environments, AWS IAM conditions |

## Expected Token Savings
Incident response after dev agent hits prod database: ~200,000 tokens (plus data recovery)
Startup credential validation catches contamination before any work: 0 wasted

## Environment
- Any agent deployed across multiple environments (dev/staging/prod); critical for agents with write access to databases or payment APIs
- Source: direct experience; environment credential contamination causes the most severe production incidents
