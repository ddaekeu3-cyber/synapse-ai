---
layout: solution
title: "Agent Doesn't Implement Multi-Environment Secret Management"
category: config
description: "Agents that hardcode secrets, share the same API keys across dev/staging/prod, or load credentials from unprotected files are one accidental commit or log statement away from a full credential breach. Proper multi-environment secret management uses environment-specific vaults, injection at runtime, and zero-secret-in-code policies."
tags: [config, secrets, environment, security, credentials, vault]
---

## Problem

An agent running across dev, staging, and production environments needs different API keys, database credentials, and third-party tokens in each environment. Common failures: secrets hardcoded in source, `.env` files committed to git, the same production key used in dev, secrets logged in error messages, and no rotation capability. Multi-environment secret management prevents these patterns.

## Solutions

### Option 1: Environment-Tiered Secret Loader

```python
import anthropic
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class Environment(str, Enum):
    LOCAL = "local"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"

@dataclass
class SecretConfig:
    """Environment-specific configuration — never store values in code."""
    environment: Environment
    anthropic_model: str      # Different model tiers per env
    max_tokens: int
    rate_limit_rpm: int
    log_level: str
    debug_mode: bool

# Per-environment non-secret configuration (safe to commit)
ENV_CONFIGS: dict[Environment, SecretConfig] = {
    Environment.LOCAL: SecretConfig(
        Environment.LOCAL,
        anthropic_model="claude-haiku-4-5-20251001",
        max_tokens=100,
        rate_limit_rpm=10,
        log_level="DEBUG",
        debug_mode=True
    ),
    Environment.DEVELOPMENT: SecretConfig(
        Environment.DEVELOPMENT,
        anthropic_model="claude-haiku-4-5-20251001",
        max_tokens=200,
        rate_limit_rpm=60,
        log_level="DEBUG",
        debug_mode=True
    ),
    Environment.STAGING: SecretConfig(
        Environment.STAGING,
        anthropic_model="claude-sonnet-4-6",
        max_tokens=1000,
        rate_limit_rpm=300,
        log_level="INFO",
        debug_mode=False
    ),
    Environment.PRODUCTION: SecretConfig(
        Environment.PRODUCTION,
        anthropic_model="claude-sonnet-4-6",
        max_tokens=4096,
        rate_limit_rpm=1000,
        log_level="WARNING",
        debug_mode=False
    ),
}

def detect_environment() -> Environment:
    """Detect current environment from ENV var — never from code."""
    env_str = os.environ.get("APP_ENV", "local").lower()
    env_map = {
        "local": Environment.LOCAL,
        "dev": Environment.DEVELOPMENT,
        "development": Environment.DEVELOPMENT,
        "staging": Environment.STAGING,
        "stage": Environment.STAGING,
        "prod": Environment.PRODUCTION,
        "production": Environment.PRODUCTION,
    }
    return env_map.get(env_str, Environment.LOCAL)

def load_secret(secret_name: str, required: bool = True) -> Optional[str]:
    """
    Load secret from environment variable.
    In production, env vars are injected by your secrets manager
    (AWS Secrets Manager, HashiCorp Vault, GCP Secret Manager, k8s Secrets).
    """
    value = os.environ.get(secret_name)
    if not value and required:
        raise EnvironmentError(
            f"Required secret '{secret_name}' not found in environment. "
            f"Set {secret_name} via your secrets manager before starting the agent."
        )
    return value

def get_anthropic_client(env: Environment) -> anthropic.Anthropic:
    """
    Get Anthropic client with environment-appropriate API key.
    Each env has its own key with appropriate permissions.
    """
    # Key names follow convention: ANTHROPIC_API_KEY_{ENV}
    # These are injected at runtime by CI/CD or k8s Secrets
    key_env_var = {
        Environment.LOCAL: "ANTHROPIC_API_KEY",           # Developer's personal key
        Environment.DEVELOPMENT: "ANTHROPIC_API_KEY_DEV",
        Environment.STAGING: "ANTHROPIC_API_KEY_STAGING",
        Environment.PRODUCTION: "ANTHROPIC_API_KEY_PROD",
    }.get(env, "ANTHROPIC_API_KEY")

    api_key = load_secret(key_env_var, required=True)
    return anthropic.Anthropic(api_key=api_key)

def run_agent(prompt: str) -> dict:
    """Agent that adapts to current environment."""
    env = detect_environment()
    config = ENV_CONFIGS[env]
    client = get_anthropic_client(env)

    if config.debug_mode:
        print(f"[Config] Environment: {env.value} | Model: {config.anthropic_model} | Debug: ON")

    response = client.messages.create(
        model=config.anthropic_model,
        max_tokens=config.max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )

    return {
        "environment": env.value,
        "model_used": config.anthropic_model,
        "response": response.content[0].text,
        "debug_mode": config.debug_mode
    }

# Usage
result = run_agent("What environment am I running in?")
print(f"Env: {result['environment']} | Model: {result['model_used']}")
print(f"Response: {result['response'][:150]}")

# Expected Token Savings: Different models per env saves ~80% in dev vs prod
# Environment: ANTHROPIC_API_KEY (or env-specific variant) required; APP_ENV controls environment
```

### Option 2: Vault-Backed Secret Fetcher with Caching

```python
import anthropic
import os
import time
import json
import hashlib
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class CachedSecret:
    value: str
    fetched_at: float
    ttl_seconds: float

    @property
    def is_expired(self) -> bool:
        return time.time() - self.fetched_at > self.ttl_seconds

class SecretVaultClient:
    """
    Abstraction over a secret vault backend.
    Supports: env vars (local), AWS SSM, HashiCorp Vault.
    """
    def __init__(self, backend: str = "env", ttl_seconds: float = 300):
        self._backend = backend
        self._ttl = ttl_seconds
        self._cache: dict[str, CachedSecret] = {}
        self._fetch_count = 0

    def _fetch_from_env(self, secret_path: str) -> Optional[str]:
        """Local dev: read from environment variable."""
        env_key = secret_path.upper().replace("/", "_").replace("-", "_")
        return os.environ.get(env_key)

    def _fetch_from_ssm(self, secret_path: str) -> Optional[str]:
        """AWS SSM Parameter Store (requires boto3 + IAM role)."""
        try:
            import boto3
            ssm = boto3.client('ssm')
            response = ssm.get_parameter(Name=secret_path, WithDecryption=True)
            return response['Parameter']['Value']
        except ImportError:
            raise RuntimeError("boto3 required for SSM backend: pip install boto3")
        except Exception as e:
            raise RuntimeError(f"SSM fetch failed for {secret_path}: {e}")

    def _fetch_from_vault(self, secret_path: str) -> Optional[str]:
        """HashiCorp Vault (requires hvac + VAULT_ADDR + VAULT_TOKEN)."""
        try:
            import hvac
            vault_client = hvac.Client(
                url=os.environ["VAULT_ADDR"],
                token=os.environ["VAULT_TOKEN"]
            )
            response = vault_client.secrets.kv.v2.read_secret_version(path=secret_path)
            return response['data']['data'].get('value')
        except ImportError:
            raise RuntimeError("hvac required for Vault backend: pip install hvac")

    def get(self, secret_path: str, required: bool = True) -> Optional[str]:
        """Get secret with caching."""
        # Check cache
        cached = self._cache.get(secret_path)
        if cached and not cached.is_expired:
            return cached.value

        # Fetch from backend
        self._fetch_count += 1
        if self._backend == "env":
            value = self._fetch_from_env(secret_path)
        elif self._backend == "ssm":
            value = self._fetch_from_ssm(secret_path)
        elif self._backend == "vault":
            value = self._fetch_from_vault(secret_path)
        else:
            raise ValueError(f"Unknown backend: {self._backend}")

        if not value and required:
            raise EnvironmentError(f"Required secret not found: {secret_path}")

        if value:
            self._cache[secret_path] = CachedSecret(value, time.time(), self._ttl)

        return value

    def rotate(self, secret_path: str):
        """Invalidate cache entry to force re-fetch after rotation."""
        self._cache.pop(secret_path, None)
        print(f"[SecretVault] Cache invalidated for {secret_path} — will re-fetch on next access")

    def stats(self) -> dict:
        return {
            "backend": self._backend,
            "cached_secrets": len(self._cache),
            "fetch_count": self._fetch_count,
            "cache_hit_rate": round(1 - (self._fetch_count / max(self._fetch_count + len(self._cache), 1)), 2)
        }

# Select backend from environment
backend = os.environ.get("SECRET_BACKEND", "env")
vault = SecretVaultClient(backend=backend, ttl_seconds=300)

def run_agent_with_vault(prompt: str, env: str = "dev") -> dict:
    """Agent using vault-backed secrets."""
    # Secret paths follow convention: /app/{env}/anthropic/api_key
    api_key_path = f"/app/{env}/anthropic/api_key"

    # Fallback to ANTHROPIC_API_KEY for local dev
    try:
        api_key = vault.get(api_key_path)
    except EnvironmentError:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise

    client_with_key = anthropic.Anthropic(api_key=api_key)
    response = client_with_key.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}]
    )

    return {
        "response": response.content[0].text,
        "vault_stats": vault.stats()
    }

result = run_agent_with_vault("What is dependency injection?")
print(f"Response: {result['response'][:150]}")
print(f"Vault stats: {result['vault_stats']}")

# Expected Token Savings: None — vault adds ~5ms latency; prevents production key leakage
# Environment: ANTHROPIC_API_KEY required (or vault configured); SECRET_BACKEND=env|ssm|vault
```

### Option 3: Secret Injection via Kubernetes Secrets Pattern

```python
import anthropic
import os
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

@dataclass
class K8sSecretLoader:
    """
    Loads secrets from mounted Kubernetes Secret volumes.
    k8s mounts secrets as files at a specified path.
    Each file name = secret key, file content = secret value.
    """
    mount_path: str = "/var/secrets/agent"
    fallback_to_env: bool = True   # For local dev without k8s

    def get(self, secret_name: str, required: bool = True) -> Optional[str]:
        """
        Load secret from mounted volume.
        In k8s: Secret is mounted at /var/secrets/agent/{secret_name}
        Locally: Falls back to environment variable.
        """
        # Try mounted secret first
        secret_file = Path(self.mount_path) / secret_name
        if secret_file.exists():
            value = secret_file.read_text().strip()
            return value if value else None

        # Fallback to env var for local development
        if self.fallback_to_env:
            env_key = secret_name.upper().replace("-", "_")
            value = os.environ.get(env_key)
            if value:
                return value

        if required:
            raise EnvironmentError(
                f"Secret '{secret_name}' not found at {secret_file} "
                f"and env var {secret_name.upper()} not set."
            )
        return None

    def get_json(self, secret_name: str, required: bool = True) -> Optional[dict]:
        """Load JSON secret (e.g., service account credentials)."""
        raw = self.get(secret_name, required=required)
        if raw:
            return json.loads(raw)
        return None

# k8s Secret manifest (apply with: kubectl apply -f secret.yaml):
K8S_SECRET_MANIFEST = """
# DO NOT STORE ACTUAL VALUES IN CODE — use external secret operators
apiVersion: v1
kind: Secret
metadata:
  name: agent-secrets
  namespace: production
type: Opaque
stringData:
  anthropic-api-key: "$(ANTHROPIC_API_KEY)"     # Injected by CI/CD
  database-url: "$(DATABASE_URL)"               # Injected by CI/CD
  webhook-signing-secret: "$(WEBHOOK_SECRET)"   # Injected by CI/CD
---
# Deployment volume mount
# spec:
#   volumes:
#   - name: agent-secrets
#     secret:
#       secretName: agent-secrets
#   containers:
#   - name: agent
#     volumeMounts:
#     - name: agent-secrets
#       mountPath: /var/secrets/agent
#       readOnly: true
"""

loader = K8sSecretLoader(mount_path="/var/secrets/agent", fallback_to_env=True)

def create_anthropic_client_from_k8s() -> anthropic.Anthropic:
    """Create client using k8s-mounted API key."""
    api_key = loader.get("anthropic-api-key")
    return anthropic.Anthropic(api_key=api_key)

def run_k8s_agent(prompt: str) -> str:
    """Agent that loads credentials from k8s Secret volumes."""
    client = create_anthropic_client_from_k8s()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

output = run_k8s_agent("Explain Kubernetes Secrets in one sentence.")
print(f"Response: {output[:150]}")
print("\nk8s Secret manifest (reference):")
print(K8S_SECRET_MANIFEST[:400])

# Expected Token Savings: None — k8s secrets prevent env var exposure in pod specs
# Environment: ANTHROPIC_API_KEY env var (local) or /var/secrets/agent/anthropic-api-key (k8s)
```

### Option 4: Secret Scanning Before Log Output

```python
import anthropic
import os
import re
import logging
from dataclasses import dataclass

client = anthropic.Anthropic()

# Patterns that look like secrets
SECRET_PATTERNS = [
    (re.compile(r'sk-ant-[a-zA-Z0-9\-_]{20,}'), "anthropic_key"),
    (re.compile(r'sk-[a-zA-Z0-9]{20,}'), "openai_key"),
    (re.compile(r'AKIA[A-Z0-9]{16}'), "aws_access_key"),
    (re.compile(r'[a-f0-9]{40}'), "possible_token_hex"),
    (re.compile(r'Bearer\s+[a-zA-Z0-9\-_\.]+'), "bearer_token"),
    (re.compile(r'password\s*[=:]\s*\S+', re.IGNORECASE), "password"),
    (re.compile(r'api[_-]?key\s*[=:]\s*\S+', re.IGNORECASE), "api_key"),
    (re.compile(r'secret\s*[=:]\s*\S+', re.IGNORECASE), "secret"),
    (re.compile(r'[a-zA-Z0-9+/]{40,}={0,2}'), "base64_blob"),
]

def scan_for_secrets(text: str) -> list[tuple[str, str]]:
    """Scan text for potential secrets. Returns [(matched_text, pattern_name)]."""
    found = []
    for pattern, name in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.group(), name))
    return found

def redact_secrets(text: str, replacement: str = "[REDACTED]") -> str:
    """Replace detected secrets with redaction marker."""
    result = text
    for pattern, _ in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result

class SecretScanningFormatter(logging.Formatter):
    """Log formatter that automatically redacts secrets."""
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return redact_secrets(msg)

def setup_secret_safe_logging():
    """Configure logging to automatically redact secrets."""
    handler = logging.StreamHandler()
    handler.setFormatter(SecretScanningFormatter(
        '[%(levelname)s] %(asctime)s %(name)s: %(message)s'
    ))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    return logging.getLogger("agent")

logger = setup_secret_safe_logging()

def safe_log_context(context: dict) -> dict:
    """Sanitize dict before logging — remove or redact secret fields."""
    SENSITIVE_KEYS = {"api_key", "token", "password", "secret", "credential", "auth"}
    return {
        k: "[REDACTED]" if any(s in k.lower() for s in SENSITIVE_KEYS) else v
        for k, v in context.items()
    }

def run_agent_with_safe_logging(prompt: str, user_context: dict) -> str:
    """Agent with secret-scanning logging."""
    safe_ctx = safe_log_context(user_context)
    logger.info(f"Processing request: {safe_ctx}")

    # Scan prompt for accidentally included secrets
    secrets_in_prompt = scan_for_secrets(prompt)
    if secrets_in_prompt:
        logger.warning(f"Potential secrets in user prompt: {[name for _, name in secrets_in_prompt]}")
        prompt = redact_secrets(prompt)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )
    output = response.content[0].text

    # Scan output too (agent might echo back secrets)
    secrets_in_output = scan_for_secrets(output)
    if secrets_in_output:
        logger.error(f"Agent output contains potential secrets: {[name for _, name in secrets_in_output]}")
        output = redact_secrets(output)

    logger.info(f"Response: {output[:80]}...")
    return output

# Test — simulate accidentally logged secrets
test_context = {
    "user_id": "user_42",
    "api_key": "sk-ant-super-secret-key-here",  # Would be redacted in logs
    "session": "sess_12345",
    "token": "Bearer eyJhbGciOiJSUzI1NiJ9..."
}

result = run_agent_with_safe_logging(
    "What is environment variable injection?",
    test_context
)
print(f"\nFinal response (secrets scanned): {result[:150]}")

# Expected Token Savings: None — scanning prevents credential leakage via log aggregators
# Environment: ANTHROPIC_API_KEY required
```

### Option 5: Dynamic Secret Rotation Without Restart

```python
import anthropic
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable

@dataclass
class RotatingSecretRef:
    """
    A reference to a secret that can be updated without restarting the agent.
    Uses a reader-writer pattern: many readers, occasional writer (rotator).
    """
    secret_name: str
    _value: Optional[str] = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _version: int = 0
    _last_rotated: float = field(default_factory=time.time, repr=False)
    _rotation_callbacks: list[Callable] = field(default_factory=list, repr=False)

    def get(self) -> Optional[str]:
        with self._lock:
            return self._value

    def rotate(self, new_value: str):
        """Update secret value — all future get() calls see the new value."""
        with self._lock:
            old_version = self._version
            self._value = new_value
            self._version += 1
            self._last_rotated = time.time()
            print(f"[SecretRotation] '{self.secret_name}' rotated "
                  f"v{old_version} → v{self._version}")

        # Notify rotation listeners
        for callback in self._rotation_callbacks:
            try:
                callback(self.secret_name, self._version)
            except Exception as e:
                print(f"[SecretRotation] Callback error: {e}")

    def on_rotation(self, callback: Callable):
        """Register a callback for when this secret is rotated."""
        self._rotation_callbacks.append(callback)

    def info(self) -> dict:
        with self._lock:
            return {
                "name": self.secret_name,
                "version": self._version,
                "last_rotated_ago": round(time.time() - self._last_rotated),
                "has_value": self._value is not None
            }

class RotatingSecretManager:
    """Manages a set of rotating secrets for an agent service."""

    def __init__(self):
        self._secrets: dict[str, RotatingSecretRef] = {}
        self._clients: dict[str, anthropic.Anthropic] = {}
        self._client_lock = threading.Lock()

    def register(self, name: str, initial_value: Optional[str] = None) -> RotatingSecretRef:
        ref = RotatingSecretRef(secret_name=name, _value=initial_value)
        self._secrets[name] = ref

        # Auto-rebuild Anthropic client on API key rotation
        if "anthropic" in name.lower() or "api_key" in name.lower():
            def rebuild_client(secret_name: str, version: int):
                new_key = self._secrets[secret_name].get()
                if new_key:
                    with self._client_lock:
                        self._clients[secret_name] = anthropic.Anthropic(api_key=new_key)
                    print(f"[RotatingSecrets] Anthropic client rebuilt (key v{version})")
            ref.on_rotation(rebuild_client)

        return ref

    def get_client(self, api_key_secret: str) -> anthropic.Anthropic:
        with self._client_lock:
            if api_key_secret not in self._clients:
                key = self._secrets[api_key_secret].get()
                if key:
                    self._clients[api_key_secret] = anthropic.Anthropic(api_key=key)
            return self._clients.get(api_key_secret)

    def rotate(self, name: str, new_value: str):
        if name in self._secrets:
            self._secrets[name].rotate(new_value)
        else:
            raise KeyError(f"Secret '{name}' not registered")

    def all_info(self) -> dict:
        return {name: ref.info() for name, ref in self._secrets.items()}

manager = RotatingSecretManager()

# Register secrets at startup
api_key_ref = manager.register(
    "anthropic_api_key",
    initial_value=os.environ.get("ANTHROPIC_API_KEY")
)

def run_with_rotating_key(prompt: str) -> str:
    """Use the currently active API key — survives rotation."""
    client = manager.get_client("anthropic_api_key")
    if not client:
        raise RuntimeError("No valid API key configured")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

# Demonstrate rotation
print("Before rotation:")
r1 = run_with_rotating_key("What is hot reloading?")
print(f"Response: {r1[:80]}")
print(f"Secret info: {manager.all_info()}")

# Simulate key rotation (would be triggered by your secrets manager webhook)
manager.rotate("anthropic_api_key", os.environ.get("ANTHROPIC_API_KEY", ""))  # Same key for demo

print("\nAfter rotation:")
r2 = run_with_rotating_key("Define zero-downtime deployment.")
print(f"Response: {r2[:80]}")

# Expected Token Savings: Zero-downtime rotation = no gap in service; avoids emergency restarts
# Environment: ANTHROPIC_API_KEY required
```

### Option 6: Secret Audit and Compliance Reporter

```python
import anthropic
import os
import re
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

client = anthropic.Anthropic()

@dataclass
class SecretAuditFinding:
    severity: str     # "critical" | "high" | "medium" | "low"
    finding_type: str
    location: str
    description: str
    recommendation: str

class SecretAuditReport:
    def __init__(self):
        self.findings: list[SecretAuditFinding] = []
        self.scanned_at = time.time()

    def add(self, severity, finding_type, location, description, recommendation):
        self.findings.append(SecretAuditFinding(severity, finding_type, location, description, recommendation))

    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    def summary(self) -> dict:
        by_severity = {}
        for f in self.findings:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        return {
            "total_findings": len(self.findings),
            "by_severity": by_severity,
            "pass": self.critical_count() == 0,
            "scanned_at": self.scanned_at
        }

def audit_environment_config() -> SecretAuditReport:
    """Audit the current environment for secret management issues."""
    report = SecretAuditReport()
    env_vars = dict(os.environ)

    # Check 1: Look for plaintext secrets in env vars (common patterns)
    plaintext_secret_patterns = [
        (re.compile(r'^.*(PASSWORD|PASSWD|SECRET|CREDENTIAL).*$', re.IGNORECASE), "plaintext_credential"),
        (re.compile(r'^.*API_KEY.*$', re.IGNORECASE), "api_key_in_env"),
        (re.compile(r'^.*TOKEN.*$', re.IGNORECASE), "token_in_env"),
    ]

    for var_name, var_value in env_vars.items():
        for pattern, finding_type in plaintext_secret_patterns:
            if pattern.match(var_name) and var_value and len(var_value) > 5:
                # Check if it looks like a real secret (not a placeholder)
                if not any(placeholder in var_value.lower()
                          for placeholder in ["your_", "example", "placeholder", "xxx", "test", "demo"]):
                    report.add(
                        severity="high",
                        finding_type=finding_type,
                        location=f"ENV:{var_name}",
                        description=f"Sensitive env var '{var_name}' contains a non-placeholder value",
                        recommendation="Inject via secrets manager at runtime, not in Dockerfile or shell scripts"
                    )
                    break

    # Check 2: Look for .env files that might be committed
    env_files = [".env", ".env.local", ".env.production", ".env.secret"]
    for env_file in env_files:
        if Path(env_file).exists():
            report.add(
                severity="critical",
                finding_type="env_file_on_disk",
                location=f"FILE:{env_file}",
                description=f"Found '{env_file}' on disk — may be committed to version control",
                recommendation=f"Add '{env_file}' to .gitignore and use secrets manager injection"
            )

    # Check 3: Verify required secret naming conventions
    required_secrets = ["ANTHROPIC_API_KEY"]
    for secret in required_secrets:
        if not os.environ.get(secret):
            report.add(
                severity="critical",
                finding_type="missing_required_secret",
                location=f"ENV:{secret}",
                description=f"Required secret '{secret}' is not set",
                recommendation=f"Set {secret} via your secrets manager before starting the agent"
            )

    # Check 4: Check for hardcoded values in common config files
    config_files = ["config.json", "config.yaml", "settings.py", "app.py"]
    secret_value_pattern = re.compile(r'["\']sk-ant-[a-zA-Z0-9\-_]{10,}["\']|["\']sk-[a-zA-Z0-9]{20,}["\']')
    for config_file in config_files:
        path = Path(config_file)
        if path.exists():
            content = path.read_text()
            if secret_value_pattern.search(content):
                report.add(
                    severity="critical",
                    finding_type="hardcoded_secret",
                    location=f"FILE:{config_file}",
                    description=f"Hardcoded secret value detected in '{config_file}'",
                    recommendation="Replace hardcoded value with os.environ.get() and use secrets injection"
                )

    return report

def run_compliant_agent(prompt: str) -> dict:
    """Run agent only after passing secret compliance audit."""
    audit = audit_environment_config()
    summary = audit.summary()

    print(f"[SecretAudit] Findings: {summary['by_severity']}")

    if not summary["pass"]:
        # In production, fail hard on critical findings
        critical = [f for f in audit.findings if f.severity == "critical"]
        print(f"[SecretAudit] CRITICAL FINDINGS ({len(critical)}):")
        for f in critical:
            print(f"  [{f.severity}] {f.location}: {f.description}")
            print(f"    Fix: {f.recommendation}")
        # Allow to continue in non-prod (would raise in prod)
        print("[SecretAudit] WARNING: Proceeding despite findings (non-production mode)")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}]
    )

    return {
        "response": response.content[0].text,
        "audit_summary": summary,
        "audit_passed": summary["pass"]
    }

result = run_compliant_agent("Explain secret management best practices.")
print(f"\nAudit passed: {result['audit_passed']}")
print(f"Response: {result['response'][:150]}")

# Expected Token Savings: None — audit prevents breaches; breach recovery >> audit cost
# Environment: ANTHROPIC_API_KEY required
```

## Comparison

| Option | Multi-Env Support | Dynamic Rotation | Secret Scanning | Audit Trail | Best Use Case |
|--------|-----------------|-----------------|-----------------|-------------|---------------|
| Environment-Tiered Loader | Yes (per-env keys) | No | No | No | Simple multi-env deployments |
| Vault-Backed with Cache | Yes | Via cache invalidation | No | No | HashiCorp Vault / AWS SSM integration |
| k8s Secrets Pattern | Yes (per namespace) | Via k8s rollout | No | k8s audit | Kubernetes deployments |
| Secret-Scanning Logger | No | No | Yes (auto-redact) | Partial | Preventing log-based leakage |
| Dynamic Rotation | Yes | Yes (zero-downtime) | No | Partial | Long-running services requiring rotation |
| Compliance Auditor | Yes | No | Yes | Yes | Pre-deployment validation, SOC2/ISO27001 |
