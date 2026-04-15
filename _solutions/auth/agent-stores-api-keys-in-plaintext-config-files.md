---
layout: solution
title: "Agent Stores API Keys in Plaintext Config Files"
category: auth
description: "Agent reads API keys from plaintext files committed to version control or left world-readable on disk, exposing credentials to anyone with repo or filesystem access."
tags: [auth, security, secrets, credentials, production]
---

## Symptom

During a security audit the team finds `config.json` in the git repository containing `"anthropic_api_key": "sk-ant-..."`. Alternatively, a developer's `.env` file with credentials is world-readable on a shared server. In either case the key is valid, billing charges begin appearing from unknown IP addresses, and the team must rotate all affected credentials and audit every API call made since the leak.

## Root Cause

Developers write credentials directly into config files for convenience during development, then forget to remove them before committing or deploying. Without `.gitignore` enforcement, pre-commit hooks, or secret-scanning CI checks, plaintext credentials flow silently into version control. On shared servers, default file permissions (`644`) allow any local user to read the file.

## Fix

### Option 1 — Environment variables via python-dotenv (local dev)

```python
import os
import anthropic
from dotenv import load_dotenv  # pip install python-dotenv

# Load from .env file in development — .env must be in .gitignore
load_dotenv()

def get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. "
            "Add it to your .env file (never commit .env) or set it in your shell."
        )
    # Validate format without logging the key itself
    if not api_key.startswith("sk-ant-"):
        raise RuntimeError("ANTHROPIC_API_KEY has unexpected format — check for copy-paste errors.")
    return anthropic.Anthropic(api_key=api_key)

client = get_client()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Ping."}],
)
print(response.content[0].text)

# .env file (never commit):
# ANTHROPIC_API_KEY=sk-ant-...
#
# .gitignore entry:
# .env
# *.env
# .env.*
```

**Expected Token Savings:** No direct token savings, but prevents credential theft that can run up unlimited bills on your account.
**Environment:** Local development; the `load_dotenv()` call is a no-op in production (where env vars are set by the orchestrator), so the same code works everywhere.

---

### Option 2 — AWS Secrets Manager: fetch at runtime, never touch disk

```python
import os
import json
import anthropic

# pip install boto3
try:
    import boto3
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

_SECRET_CACHE: dict = {}

def get_secret(secret_name: str, region: str = "us-east-1") -> dict:
    """Fetch secret from AWS Secrets Manager with in-process cache."""
    if secret_name in _SECRET_CACHE:
        return _SECRET_CACHE[secret_name]
    if not HAS_BOTO3:
        raise RuntimeError("boto3 not installed — pip install boto3")
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    secret = json.loads(response["SecretString"])
    _SECRET_CACHE[secret_name] = secret
    return secret

def get_anthropic_client() -> anthropic.Anthropic:
    if HAS_BOTO3 and os.environ.get("AWS_REGION"):
        secret = get_secret("prod/agent/anthropic")
        api_key = secret["ANTHROPIC_API_KEY"]
    else:
        # Fallback for local dev
        api_key = os.environ["ANTHROPIC_API_KEY"]
    return anthropic.Anthropic(api_key=api_key)

client = get_anthropic_client()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Hello from AWS Secrets Manager."}],
)
print(response.content[0].text)

# AWS CLI to create the secret:
# aws secretsmanager create-secret \
#   --name prod/agent/anthropic \
#   --secret-string '{"ANTHROPIC_API_KEY":"sk-ant-..."}'
```

**Expected Token Savings:** Centralised secret storage enables key rotation without redeployment; rotated key is picked up on next cache miss without any agent downtime.
**Environment:** AWS Lambda, ECS, EC2; any workload with IAM role attached.

---

### Option 3 — HashiCorp Vault: dynamic secrets with lease TTL

```python
import os
import time
import anthropic

# pip install hvac
try:
    import hvac
    HAS_HVAC = True
except ImportError:
    HAS_HVAC = False

class VaultSecretManager:
    """Fetches and auto-renews secrets from HashiCorp Vault."""

    def __init__(self, vault_addr: str, vault_token: str, secret_path: str):
        self._path   = secret_path
        self._secret: dict | None = None
        self._fetched_at: float   = 0
        self._ttl_seconds: float  = 300  # re-fetch every 5 min

        if HAS_HVAC:
            self._client = hvac.Client(url=vault_addr, token=vault_token)
        else:
            self._client = None

    def get(self, key: str) -> str:
        if (not self._secret) or (time.monotonic() - self._fetched_at > self._ttl_seconds):
            self._refresh()
        return self._secret[key]  # type: ignore[index]

    def _refresh(self) -> None:
        if not self._client:
            # Dev fallback
            self._secret = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
        else:
            resp = self._client.secrets.kv.v2.read_secret_version(path=self._path)
            self._secret = resp["data"]["data"]
        self._fetched_at = time.monotonic()
        print("[vault] secrets refreshed")

vault = VaultSecretManager(
    vault_addr=os.environ.get("VAULT_ADDR",  "http://127.0.0.1:8200"),
    vault_token=os.environ.get("VAULT_TOKEN", "dev-token"),
    secret_path="agent/anthropic",
)

def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=vault.get("ANTHROPIC_API_KEY"))

client = get_client()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Hello from Vault."}],
)
print(response.content[0].text)
```

**Expected Token Savings:** Vault dynamic secrets have short TTLs; a leaked token expires automatically, bounding blast radius without manual rotation.
**Environment:** On-premise or hybrid deployments; teams already running HashiCorp Vault for other secrets.

---

### Option 4 — OS keychain via keyring (desktop agents)

```python
import anthropic

# pip install keyring
import keyring
import keyring.errors

SERVICE_NAME = "synapse-ai-agent"
KEY_NAME     = "ANTHROPIC_API_KEY"

def store_api_key(api_key: str) -> None:
    """Call this once during agent setup to store the key in the OS keychain."""
    keyring.set_password(SERVICE_NAME, KEY_NAME, api_key)
    print(f"[keyring] API key stored in OS keychain under '{SERVICE_NAME}'")

def get_api_key() -> str:
    """Retrieve the key from the OS keychain — never touches disk."""
    try:
        key = keyring.get_password(SERVICE_NAME, KEY_NAME)
    except keyring.errors.NoKeyringError:
        raise RuntimeError(
            "No OS keychain available. Set ANTHROPIC_API_KEY environment variable instead."
        )
    if not key:
        raise RuntimeError(
            f"No API key found in keychain under '{SERVICE_NAME}'. "
            "Run store_api_key() once to set it up."
        )
    return key

def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=get_api_key())

# First-time setup (run once):
# store_api_key("sk-ant-...")

client = get_client()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Hello from keychain."}],
)
print(response.content[0].text)
```

**Expected Token Savings:** OS keychain is encrypted at rest and only accessible by the current user process; eliminates the entire class of plaintext-on-disk credential leaks.
**Environment:** Desktop CLI agents on macOS (Keychain), Windows (Credential Manager), or Linux (Secret Service / GNOME Keyring).

---

### Option 5 — Pre-commit hook: block credential commits automatically

```python
#!/usr/bin/env python3
"""
Pre-commit hook that blocks commits containing API keys.
Install: cp this file .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
Or use with pre-commit framework: see .pre-commit-config.yaml below.
"""
import re
import subprocess
import sys

# Patterns for common API key formats
PATTERNS = [
    (r"sk-ant-[a-zA-Z0-9\-_]{20,}", "Anthropic API key"),
    (r"sk-[a-zA-Z0-9]{48}",          "OpenAI API key"),
    (r"AIza[0-9A-Za-z\-_]{35}",      "Google API key"),
    (r"(?i)api[_\-]?key\s*[:=]\s*['\"][a-zA-Z0-9\-_]{20,}['\"]", "Generic API key assignment"),
    (r"(?i)secret\s*[:=]\s*['\"][a-zA-Z0-9\-_]{20,}['\"]",        "Generic secret assignment"),
]

def check_staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True, text=True,
    )
    return result.stdout.strip().splitlines()

def check_file_content(filepath: str) -> list[tuple[str, int, str]]:
    """Return list of (pattern_name, line_number, line) for matches."""
    violations = []
    try:
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            for lineno, line in enumerate(f, 1):
                for pattern, name in PATTERNS:
                    if re.search(pattern, line):
                        violations.append((name, lineno, line.rstrip()))
    except (OSError, UnicodeDecodeError):
        pass
    return violations

def main() -> int:
    files = check_staged_files()
    found_violations = False
    for filepath in files:
        if any(filepath.endswith(ext) for ext in (".py", ".js", ".ts", ".json", ".yaml", ".yml", ".env", ".cfg", ".ini", ".toml")):
            violations = check_file_content(filepath)
            for name, lineno, line in violations:
                print(f"[secret-scan] BLOCKED: {name} found in {filepath}:{lineno}")
                print(f"  {line[:120]}")
                found_violations = True
    if found_violations:
        print("\n[secret-scan] Commit blocked. Remove secrets and use environment variables.")
        print("  To bypass (dangerous): git commit --no-verify")
        return 1
    return 0

# Also demonstrate the agent still reads from env:
import os, anthropic
if os.environ.get("ANTHROPIC_API_KEY"):
    client = anthropic.Anthropic()  # reads from env automatically
    print("[agent] client ready — key from environment, not from disk")

if __name__ == "__main__":
    sys.exit(main())

# .pre-commit-config.yaml:
# repos:
#   - repo: https://github.com/gitleaks/gitleaks
#     rev: v8.18.0
#     hooks:
#       - id: gitleaks
```

**Expected Token Savings:** Prevents the key from ever reaching the repo; no incident response, no key rotation, no audit — the cheapest possible secret management.
**Environment:** Any team using git; pairs with CI secret scanning (GitHub Advanced Security, GitGuardian) for defense in depth.

---

### Option 6 — Kubernetes Secret mounted as env var (production)

```python
import os
import anthropic

# In Kubernetes, secrets are injected as environment variables or mounted as files.
# The agent code itself is credential-free — it only reads from the environment.

def get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Try file-mounted secret (alternative K8s pattern)
        secret_file = "/var/secrets/anthropic/api-key"
        if os.path.exists(secret_file):
            with open(secret_file) as f:
                api_key = f.read().strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not found in environment or /var/secrets/anthropic/api-key. "
            "Check Kubernetes Secret configuration."
        )
    return anthropic.Anthropic(api_key=api_key)

client = get_client()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Hello from Kubernetes."}],
)
print(response.content[0].text)

# kubernetes/secret.yaml (apply with kubectl, never commit the actual value):
# apiVersion: v1
# kind: Secret
# metadata:
#   name: anthropic-credentials
# type: Opaque
# stringData:
#   api-key: "sk-ant-..."   # use sealed-secrets or external-secrets-operator in prod
#
# kubernetes/deployment.yaml (inject as env var):
# env:
#   - name: ANTHROPIC_API_KEY
#     valueFrom:
#       secretKeyRef:
#         name: anthropic-credentials
#         key: api-key
#
# Or mount as file:
# volumeMounts:
#   - name: anthropic-secret
#     mountPath: /var/secrets/anthropic
#     readOnly: true
# volumes:
#   - name: anthropic-secret
#     secret:
#       secretName: anthropic-credentials
```

**Expected Token Savings:** Kubernetes Secrets are base64-encoded at rest (encrypt with KMS for production); secret rotation via `kubectl apply` takes effect on next pod restart without any code change.
**Environment:** Kubernetes-hosted agents; pairs with External Secrets Operator (syncs from AWS/GCP/Vault) for fully automated rotation.

---

## Comparison

| Option | Storage | Rotation | Multi-env | Zero-disk | Best For |
|---|---|---|---|---|---|
| 1. python-dotenv | `.env` file (gitignored) | Manual | Via file per env | No | Local development |
| 2. AWS Secrets Manager | AWS managed | Automated | Yes (by ARN) | Yes | AWS workloads |
| 3. HashiCorp Vault | Vault KV | Automated (TTL) | Yes (by path) | Yes | On-prem / hybrid |
| 4. OS keychain | OS-encrypted store | Manual | No (single user) | Yes | Desktop CLI agents |
| 5. Pre-commit hook | Git hook | N/A (prevention) | N/A | N/A | All repos; defense layer |
| 6. Kubernetes Secret | etcd (K8s) | Via kubectl apply | Yes (by namespace) | Yes | K8s-hosted agents |
