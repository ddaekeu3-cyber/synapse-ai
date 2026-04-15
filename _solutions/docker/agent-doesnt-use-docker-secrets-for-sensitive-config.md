---
layout: solution
title: "Agent Doesn't Use Docker Secrets for Sensitive Config"
category: docker
description: "API keys and database passwords are passed to agent containers via environment variables in docker-compose.yml or Kubernetes manifests, where they appear in plaintext in version control, CI logs, and docker inspect output."
tags: [docker, secrets, security, kubernetes, vault, environment-variables, docker-compose]
---

# Agent Doesn't Use Docker Secrets for Sensitive Config

## Problem

A developer commits `docker-compose.yml` with `ANTHROPIC_API_KEY=sk-ant-...` to a shared Git repository. The key is now in version control history, visible in CI logs, and exposed by `docker inspect`. Rotating the key requires finding every place it was hardcoded. Docker Secrets, Kubernetes Secrets, and secrets managers solve this by keeping sensitive values out of container configs entirely.

## Solutions

### Option 1: Docker Compose Secrets with File Mounts

```yaml
# docker-compose.yml
version: "3.9"

services:
  agent:
    build: .
    secrets:
      - anthropic_api_key
      - database_url
    environment:
      # Non-sensitive config can stay in env vars
      ANTHROPIC_MODEL: claude-sonnet-4-6
      LOG_LEVEL: INFO
      # Tell the app where to find the secrets
      ANTHROPIC_API_KEY_FILE: /run/secrets/anthropic_api_key
      DATABASE_URL_FILE: /run/secrets/database_url

secrets:
  anthropic_api_key:
    file: ./secrets/anthropic_api_key.txt   # NOT committed to git
  database_url:
    file: ./secrets/database_url.txt
```

```
# .gitignore
secrets/
*.txt
.env
.env.*
```

```python
# config/secrets_loader.py
"""
Read secrets from Docker secret files (mounted at /run/secrets/).
Falls back to environment variables for local development.
"""
import os
from pathlib import Path


def read_secret(name: str, env_var: str | None = None) -> str:
    """
    Read a secret from:
    1. /run/secrets/<name>  (Docker Secrets mount)
    2. <name>_FILE env var pointing to a file
    3. <name> env var (fallback for local dev)
    Raises RuntimeError if not found.
    """
    # Docker Secrets mount location
    secret_file = Path(f"/run/secrets/{name}")
    if secret_file.exists():
        return secret_file.read_text().strip()

    # File path from env var
    file_env = os.environ.get(f"{name.upper()}_FILE")
    if file_env:
        p = Path(file_env)
        if p.exists():
            return p.read_text().strip()

    # Direct env var (last resort — dev/test only)
    direct_env = env_var or name.upper()
    value = os.environ.get(direct_env)
    if value:
        return value

    raise RuntimeError(
        f"Secret '{name}' not found. Checked: "
        f"/run/secrets/{name}, {name.upper()}_FILE env var, {direct_env} env var"
    )


# Load secrets at startup
ANTHROPIC_API_KEY = read_secret("anthropic_api_key", "ANTHROPIC_API_KEY")
DATABASE_URL = read_secret("database_url", "DATABASE_URL")
```

```python
# main.py
import anthropic
from config.secrets_loader import ANTHROPIC_API_KEY

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
```

**Expected Token Savings:** Not applicable — security hardening
**Environment:** Docker Compose v3.9+

---

### Option 2: Kubernetes Secrets with Volume Mounts

```yaml
# k8s/secrets.yaml
# Create secrets with kubectl, NOT by committing this file
# kubectl create secret generic agent-secrets \
#   --from-literal=anthropic-api-key="sk-ant-..." \
#   --from-literal=database-url="postgresql://..." \
#   --namespace=production

# If you do use a YAML file, encrypt it with Sealed Secrets or SOPS
# NEVER commit plaintext Secret YAML to version control
apiVersion: v1
kind: Secret
metadata:
  name: agent-secrets
  namespace: production
type: Opaque
# Values are base64-encoded, NOT encrypted — use Sealed Secrets for real security
data:
  anthropic-api-key: <base64-encoded-value>
  database-url: <base64-encoded-value>
```

```yaml
# k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-agent
spec:
  template:
    spec:
      containers:
        - name: agent
          image: your-registry/ai-agent:latest
          # Option A: Mount secrets as files (more secure — not in env)
          volumeMounts:
            - name: agent-secrets
              mountPath: /run/secrets
              readOnly: true
          # Option B: Expose as env vars (convenient, but visible in kubectl describe)
          env:
            - name: ANTHROPIC_API_KEY
              valueFrom:
                secretKeyRef:
                  name: agent-secrets
                  key: anthropic-api-key
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: agent-secrets
                  key: database-url
      volumes:
        - name: agent-secrets
          secret:
            secretName: agent-secrets
            defaultMode: 0400  # Owner read-only
```

```python
# config/k8s_secrets.py
"""
Read Kubernetes secrets mounted as files (preferred over env vars
because they don't appear in `kubectl describe pod` output).
"""
import os
from pathlib import Path


SECRETS_MOUNT = Path(os.environ.get("SECRETS_MOUNT_PATH", "/run/secrets"))


def get_secret(key: str) -> str:
    """Read a Kubernetes secret from the mounted volume."""
    path = SECRETS_MOUNT / key
    if path.exists():
        return path.read_text().strip()
    # Fallback for local dev
    env_val = os.environ.get(key.upper().replace("-", "_"))
    if env_val:
        return env_val
    raise RuntimeError(f"Secret not found: {key} (checked {path} and env)")
```

**Expected Token Savings:** Not applicable — security compliance
**Environment:** Kubernetes

---

### Option 3: HashiCorp Vault Agent Sidecar Injection

```yaml
# k8s/deployment-vault.yaml
# Vault Agent Sidecar Injector automatically injects secrets into the container
# without the application knowing about Vault at all.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-agent
spec:
  template:
    metadata:
      annotations:
        # Vault sidecar injector annotations
        vault.hashicorp.com/agent-inject: "true"
        vault.hashicorp.com/role: "ai-agent"
        # Inject ANTHROPIC_API_KEY as a file
        vault.hashicorp.com/agent-inject-secret-anthropic-key: "secret/data/ai-agent/anthropic"
        vault.hashicorp.com/agent-inject-template-anthropic-key: |
          {{- with secret "secret/data/ai-agent/anthropic" -}}
          {{ .Data.data.api_key }}
          {{- end }}
        # Inject DATABASE_URL
        vault.hashicorp.com/agent-inject-secret-database-url: "secret/data/ai-agent/database"
        vault.hashicorp.com/agent-inject-template-database-url: |
          {{- with secret "secret/data/ai-agent/database" -}}
          {{ .Data.data.url }}
          {{- end }}
    spec:
      serviceAccountName: ai-agent
      containers:
        - name: agent
          image: your-registry/ai-agent:latest
          # Secrets appear at /vault/secrets/<name> — no env vars needed
          env:
            - name: ANTHROPIC_API_KEY_FILE
              value: /vault/secrets/anthropic-key
            - name: DATABASE_URL_FILE
              value: /vault/secrets/database-url
```

```python
# config/vault_secrets.py
"""
Read secrets injected by Vault Agent sidecar.
Vault automatically rotates and re-injects secrets; the app just re-reads the file.
"""
import os
from pathlib import Path
import time


class VaultSecretReader:
    """Reads Vault-injected secrets with optional hot-reload."""

    def __init__(self, file_path: str):
        self._path = Path(file_path)
        self._cached_value: str | None = None
        self._cache_time: float = 0
        self._cache_ttl: float = 30.0  # Re-read file every 30s

    def read(self) -> str:
        """Read the secret, using cache if fresh."""
        now = time.time()
        if self._cached_value is None or now - self._cache_time > self._cache_ttl:
            if not self._path.exists():
                raise RuntimeError(f"Vault secret file not found: {self._path}")
            self._cached_value = self._path.read_text().strip()
            self._cache_time = now
        return self._cached_value

    def invalidate(self):
        """Force re-read on next access (call after known rotation)."""
        self._cached_value = None


def _get_reader(env_var: str, fallback_env: str) -> VaultSecretReader | None:
    file_path = os.environ.get(env_var)
    if file_path:
        return VaultSecretReader(file_path)
    return None


_anthropic_reader = _get_reader("ANTHROPIC_API_KEY_FILE", "ANTHROPIC_API_KEY")


def get_anthropic_api_key() -> str:
    if _anthropic_reader:
        return _anthropic_reader.read()
    return os.environ.get("ANTHROPIC_API_KEY", "")
```

**Expected Token Savings:** Not applicable — enterprise security
**Environment:** HashiCorp Vault + Kubernetes

---

### Option 4: AWS Secrets Manager with Boto3

```python
# config/aws_secrets.py
"""
Fetch secrets from AWS Secrets Manager at startup.
Supports automatic rotation: the secret can be rotated in AWS without
any redeployment — just restart the container (or use the hot-reload variant).
"""
import json
import os
import time
from functools import lru_cache
import boto3
from botocore.exceptions import ClientError


SECRET_ID = os.environ.get("AWS_SECRET_ID", "prod/ai-agent/credentials")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


@lru_cache(maxsize=1)
def _fetch_secret(secret_id: str, region: str) -> dict:
    """Fetch and parse a JSON secret from AWS Secrets Manager."""
    client = boto3.client("secretsmanager", region_name=region)
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            raise RuntimeError(f"Secret not found in AWS Secrets Manager: {secret_id}")
        elif code == "AccessDeniedException":
            raise RuntimeError(f"No permission to read secret: {secret_id}")
        raise
    secret_string = response.get("SecretString", "{}")
    return json.loads(secret_string)


def get_credentials() -> dict:
    """
    Returns credentials dict with keys: anthropic_api_key, database_url, etc.
    Cached after first fetch — restart container to pick up rotated values.
    """
    return _fetch_secret(SECRET_ID, AWS_REGION)


# Hot-reload variant: re-fetch periodically without restart
_credentials_cache: dict = {}
_cache_expiry: float = 0
CACHE_TTL = float(os.environ.get("SECRET_CACHE_TTL_SECONDS", "300"))  # 5 minutes


def get_credentials_with_rotation() -> dict:
    """Re-fetch secrets periodically to pick up rotations."""
    global _credentials_cache, _cache_expiry
    if time.time() > _cache_expiry:
        _credentials_cache = _fetch_secret.__wrapped__(SECRET_ID, AWS_REGION)
        _cache_expiry = time.time() + CACHE_TTL
    return _credentials_cache


# ── Usage ─────────────────────────────────────────────────────────────────────
import anthropic


def get_anthropic_client() -> anthropic.Anthropic:
    creds = get_credentials()
    return anthropic.Anthropic(api_key=creds["anthropic_api_key"])
```

```dockerfile
# Dockerfile — no secrets in ENV, IAM role provides access to Secrets Manager
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV AWS_REGION=us-east-1 \
    AWS_SECRET_ID=prod/ai-agent/credentials
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Expected Token Savings:** Not applicable — cloud security
**Environment:** `pip install boto3 anthropic`

---

### Option 5: Secrets Validation and Audit at Startup

```python
# config/secrets_audit.py
"""
At container startup:
1. Verify all required secrets are present and loadable.
2. Validate secret format (key prefix, length).
3. Log a secrets audit trail (presence, not values).
4. Warn if secrets appear to be test/dev values in production.
"""
import os
import sys
import logging
import hashlib
import time
from pathlib import Path

logger = logging.getLogger("secrets.audit")


REQUIRED_SECRETS = {
    "ANTHROPIC_API_KEY": {
        "description": "Anthropic API key",
        "validate": lambda v: v.startswith("sk-ant-") and len(v) > 20,
        "hint": "Must start with 'sk-ant-'",
        "test_patterns": ["sk-ant-test", "dev-key", "placeholder"],
    },
    "DATABASE_URL": {
        "description": "PostgreSQL connection string",
        "validate": lambda v: v.startswith(("postgresql://", "postgres://")),
        "hint": "Must be a PostgreSQL connection string",
        "test_patterns": ["localhost", "127.0.0.1", "test_db"],
    },
}


def _load_secret(name: str) -> str | None:
    """Load secret from file mount or env var."""
    # Check Docker/Vault secret file first
    secret_file = Path(f"/run/secrets/{name.lower()}")
    if secret_file.exists():
        return secret_file.read_text().strip()
    file_env = os.environ.get(f"{name}_FILE")
    if file_env and Path(file_env).exists():
        return Path(file_env).read_text().strip()
    return os.environ.get(name)


def _fingerprint(value: str) -> str:
    """Short fingerprint for audit logging — never logs the actual value."""
    h = hashlib.sha256(value.encode()).hexdigest()
    return f"sha256:{h[:8]}...{h[-4:]}"


def audit_secrets(environment: str = "production") -> dict:
    results = {"ok": True, "secrets": {}, "warnings": [], "errors": []}

    for name, config in REQUIRED_SECRETS.items():
        value = _load_secret(name)
        if not value:
            results["errors"].append(f"{name}: NOT FOUND — {config['description']}")
            results["ok"] = False
            results["secrets"][name] = {"status": "missing"}
            continue

        valid = config["validate"](value)
        fingerprint = _fingerprint(value)

        if not valid:
            results["errors"].append(f"{name}: INVALID FORMAT — {config['hint']}")
            results["ok"] = False
            results["secrets"][name] = {"status": "invalid", "fingerprint": fingerprint}
            continue

        # Warn if test values found in production
        if environment == "production":
            for pattern in config.get("test_patterns", []):
                if pattern.lower() in value.lower():
                    results["warnings"].append(
                        f"{name}: appears to be a test/dev value in production (contains '{pattern}')"
                    )

        results["secrets"][name] = {"status": "ok", "fingerprint": fingerprint}
        logger.info("Secret loaded: %s fingerprint=%s", name, fingerprint)

    return results


def run_startup_audit():
    env = os.environ.get("ENVIRONMENT", "production")
    report = audit_secrets(env)

    for warning in report["warnings"]:
        logger.warning("Secret warning: %s", warning)
    for error in report["errors"]:
        logger.error("Secret error: %s", error)

    if not report["ok"]:
        logger.critical("Secrets audit FAILED — aborting startup")
        sys.exit(1)

    logger.info("Secrets audit PASSED (%d secrets loaded)", len(report["secrets"]))
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_startup_audit()
```

**Expected Token Savings:** Not applicable — security posture
**Environment:** stdlib only

---

### Option 6: .env.secrets Pattern with gitignore Enforcement

```python
# scripts/check_secrets_not_committed.py
"""
Pre-commit hook: scan staged files for patterns that look like secrets.
Blocks commits that accidentally include API keys, passwords, or tokens.
"""
import re
import subprocess
import sys


# Patterns that indicate a secret is hardcoded
SECRET_PATTERNS = [
    (r"sk-ant-[a-zA-Z0-9\-_]{20,}", "Anthropic API key"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID"),
    (r"postgresql://[^:]+:[^@]+@", "PostgreSQL URL with credentials"),
    (r"redis://:[^@]+@", "Redis URL with password"),
    (r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]", "Generic secret assignment"),
    (r"-----BEGIN (RSA |EC )?PRIVATE KEY-----", "Private key"),
]

EXCLUDED_FILES = {".env.example", ".env.template", "CLAUDE.md", "*.md"}
EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "tests/fixtures"}


def get_staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True,
    )
    return result.stdout.splitlines()


def scan_file(filepath: str) -> list[dict]:
    from pathlib import Path
    # Skip excluded patterns
    p = Path(filepath)
    if any(part in EXCLUDED_DIRS for part in p.parts):
        return []
    if p.suffix in (".png", ".jpg", ".pdf", ".ico"):
        return []
    try:
        content = p.read_text(errors="ignore")
    except (FileNotFoundError, PermissionError):
        return []
    findings = []
    for pattern, label in SECRET_PATTERNS:
        for match in re.finditer(pattern, content):
            line_num = content[:match.start()].count("\n") + 1
            # Mask the matched value for output
            masked = match.group()[:6] + "***" if len(match.group()) > 6 else "***"
            findings.append({
                "file": filepath,
                "line": line_num,
                "label": label,
                "match_preview": masked,
            })
    return findings


def main():
    staged = get_staged_files()
    all_findings = []
    for f in staged:
        all_findings.extend(scan_file(f))

    if not all_findings:
        print("✓ No secrets detected in staged files.")
        sys.exit(0)

    print(f"\n⚠ SECRET SCAN FAILED — {len(all_findings)} potential secret(s) found:\n")
    for f in all_findings:
        print(f"  {f['file']}:{f['line']} [{f['label']}] — {f['match_preview']}")
    print(
        "\nIf this is a false positive, add the line to .secretsignore "
        "or use 'git commit --no-verify' (use sparingly)."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
```

```bash
# Install as pre-commit hook:
cp scripts/check_secrets_not_committed.py .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit

# Or use with pre-commit framework:
# .pre-commit-config.yaml:
# repos:
#   - repo: local
#     hooks:
#       - id: check-secrets
#         name: Check for committed secrets
#         entry: python scripts/check_secrets_not_committed.py
#         language: python
#         stages: [commit]
```

**Expected Token Savings:** Not applicable — prevents credential exposure
**Environment:** stdlib + git

---

## Comparison Table

| Option | Secret Storage | Rotation Support | K8s Native | Cloud Agnostic | Startup Validation |
|--------|---------------|-----------------|------------|----------------|-------------------|
| 1: Docker Compose files | Host filesystem | Manual | No | Yes | Via secrets_loader |
| 2: Kubernetes Secrets | etcd (encrypted) | Via kubectl | Yes | No | Via volumeMount |
| 3: Vault sidecar | HashiCorp Vault | Auto (Vault) | Yes | Yes | Via sidecar |
| 4: AWS Secrets Manager | AWS | Auto (rotation) | Via IAM | No (AWS only) | At startup |
| 5: Startup audit | Any source | N/A (audit only) | Yes | Yes | Yes |
| 6: Pre-commit scan | N/A (prevention) | N/A | N/A | Yes | Pre-commit |
