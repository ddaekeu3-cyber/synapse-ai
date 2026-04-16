---
title: "Agent Doesn't Implement Secrets Vault Integration for Dynamic Credentials"
description: "AI agents that store API keys in environment variables or config files create long-lived secret exposure windows. Learn six patterns for dynamic secret retrieval, automatic rotation, and least-privilege credential management using secrets vaults."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-secrets-vault-integration-for-dynamic-credentials
tags: [secrets, vault, credentials, rotation, security, least-privilege]
symptoms:
  - "API keys are stored in .env files committed to version control"
  - "Secrets don't rotate — a leaked key stays valid until manually revoked"
  - "All agent instances share the same static API key regardless of role"
  - "Secret rotation requires restarting all agent processes"
  - "No audit trail of which agent instance accessed which secret and when"
---

## The Problem

Most AI agents load secrets at startup from environment variables or config files. These static credentials have three critical problems: they never rotate (a leaked key is permanently valid), they're shared across all instances (a compromise affects everything), and they require a restart to update (no zero-downtime rotation).

Dynamic credential management means secrets are fetched from a vault at use time, automatically rotated, and scoped to the minimum required privilege. The vault maintains an audit trail of every access.

```python
# ❌ Static credentials — long-lived, shared, no rotation
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]  # Same key forever

# ✓ Dynamic credentials from vault
async with vault.lease("anthropic/api_key", ttl=3600) as creds:
    client = anthropic.AsyncAnthropic(api_key=creds.value)
    # Key auto-rotates; old key invalidated after TTL
```

---

## Solution 1: In-Process Secret Cache with TTL and Auto-Refresh

A lightweight secret cache that fetches credentials from a backend vault, caches them with TTL, and transparently refreshes before expiry — no restart needed.

```python
import asyncio
import time
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable
import os


@dataclass
class SecretLease:
    secret_id: str
    value: str
    lease_id: str
    expires_at: float
    renewable: bool = True
    metadata: dict = field(default_factory=dict)

    def is_expiring(self, buffer_seconds: float = 60.0) -> bool:
        return time.time() >= self.expires_at - buffer_seconds

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def ttl_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())


class SecretCache:
    """
    Caches secrets with TTL and auto-refreshes before expiry.
    Transparently handles backend vault calls.
    """

    REFRESH_BUFFER_SECONDS = 60.0  # Refresh when < 60s remaining

    def __init__(self, vault_backend: "VaultBackend"):
        self._vault = vault_backend
        self._cache: dict[str, SecretLease] = {}
        self._refresh_tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def get(self, secret_id: str, force_refresh: bool = False) -> SecretLease:
        async with self._lock:
            cached = self._cache.get(secret_id)
            if cached and not cached.is_expiring() and not force_refresh:
                return cached

        # Fetch from vault (outside lock to avoid blocking other gets)
        lease = await self._vault.fetch(secret_id)
        async with self._lock:
            self._cache[secret_id] = lease
            # Schedule background refresh
            if lease.renewable:
                self._schedule_refresh(secret_id, lease)
        return lease

    def _schedule_refresh(self, secret_id: str, lease: SecretLease):
        # Cancel any existing refresh task
        existing = self._refresh_tasks.get(secret_id)
        if existing and not existing.done():
            existing.cancel()

        refresh_at = lease.ttl_remaining() - self.REFRESH_BUFFER_SECONDS
        if refresh_at > 0:
            self._refresh_tasks[secret_id] = asyncio.create_task(
                self._delayed_refresh(secret_id, refresh_at)
            )

    async def _delayed_refresh(self, secret_id: str, delay: float):
        await asyncio.sleep(delay)
        try:
            new_lease = await self._vault.fetch(secret_id)
            async with self._lock:
                self._cache[secret_id] = new_lease
                self._schedule_refresh(secret_id, new_lease)
            print(f"[vault] Auto-refreshed secret '{secret_id}'")
        except Exception as e:
            print(f"[vault] Failed to refresh '{secret_id}': {e}")

    async def revoke(self, secret_id: str):
        async with self._lock:
            lease = self._cache.pop(secret_id, None)
            task = self._refresh_tasks.pop(secret_id, None)
            if task:
                task.cancel()
        if lease:
            await self._vault.revoke(lease.lease_id)

    def cached_secrets(self) -> dict[str, dict]:
        return {
            sid: {
                "expires_in": lease.ttl_remaining(),
                "expiring_soon": lease.is_expiring(),
            }
            for sid, lease in self._cache.items()
        }


class VaultBackend:
    """Base class for vault backends. Subclass for HashiCorp Vault, AWS SSM, etc."""

    async def fetch(self, secret_id: str) -> SecretLease:
        raise NotImplementedError

    async def revoke(self, lease_id: str):
        raise NotImplementedError
```

---

## Solution 2: HashiCorp Vault Integration with AppRole Auth

Authenticate to HashiCorp Vault using AppRole (role_id + secret_id), fetch dynamic secrets, and renew the Vault token before expiry.

```python
import aiohttp
import asyncio
import time
import os
from dataclasses import dataclass


@dataclass
class VaultToken:
    token: str
    expires_at: float
    renewable: bool
    policies: list[str]


class HashiCorpVaultClient:
    """
    HashiCorp Vault client with AppRole authentication.
    Handles token lifecycle: acquire → use → renew before expiry.
    """

    def __init__(
        self,
        vault_addr: str,
        role_id: str,
        secret_id: str,
        mount_path: str = "approle",
        renew_buffer_seconds: float = 300.0,
    ):
        self.vault_addr = vault_addr.rstrip("/")
        self.role_id = role_id
        self.secret_id = secret_id
        self.mount_path = mount_path
        self.renew_buffer = renew_buffer_seconds
        self._token: VaultToken | None = None
        self._renew_task: asyncio.Task | None = None

    async def _authenticate(self) -> VaultToken:
        """Authenticate with AppRole and get a Vault token."""
        url = f"{self.vault_addr}/v1/auth/{self.mount_path}/login"
        payload = {"role_id": self.role_id, "secret_id": self.secret_id}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Vault auth failed: HTTP {resp.status}")
                data = await resp.json()

        auth = data["auth"]
        return VaultToken(
            token=auth["client_token"],
            expires_at=time.time() + auth["lease_duration"],
            renewable=auth["renewable"],
            policies=auth.get("policies", []),
        )

    async def _ensure_authenticated(self):
        if self._token is None or time.time() >= self._token.expires_at - self.renew_buffer:
            self._token = await self._authenticate()
            if self._token.renewable:
                self._schedule_token_renewal()
            print(f"[vault] Authenticated — policies: {self._token.policies}")

    def _schedule_token_renewal(self):
        if self._renew_task and not self._renew_task.done():
            self._renew_task.cancel()
        delay = max(0, self._token.expires_at - time.time() - self.renew_buffer)
        self._renew_task = asyncio.create_task(self._renew_token_loop(delay))

    async def _renew_token_loop(self, initial_delay: float):
        await asyncio.sleep(initial_delay)
        while True:
            try:
                await self._renew_token()
                delay = max(0, self._token.expires_at - time.time() - self.renew_buffer)
                await asyncio.sleep(delay)
            except Exception as e:
                print(f"[vault] Token renewal failed: {e}")
                await asyncio.sleep(30)

    async def _renew_token(self):
        await self._ensure_authenticated()
        url = f"{self.vault_addr}/v1/auth/token/renew-self"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers={"X-Vault-Token": self._token.token}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._token.expires_at = time.time() + data["auth"]["lease_duration"]
                    print(f"[vault] Token renewed, expires in {data['auth']['lease_duration']}s")

    async def read_secret(self, path: str) -> dict:
        """Read a KV v2 secret from Vault."""
        await self._ensure_authenticated()
        # KV v2: data is at /secret/data/<path>
        parts = path.split("/", 1)
        url = f"{self.vault_addr}/v1/{parts[0]}/data/{parts[1] if len(parts) > 1 else ''}"

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers={"X-Vault-Token": self._token.token}
            ) as resp:
                if resp.status == 404:
                    raise KeyError(f"Secret not found: {path}")
                if resp.status != 200:
                    raise RuntimeError(f"Vault read failed: HTTP {resp.status}")
                data = await resp.json()

        return data["data"]["data"]

    async def fetch(self, secret_id: str) -> SecretLease:
        """Implement VaultBackend interface."""
        data = await self.read_secret(secret_id)
        value = data.get("value") or data.get("api_key") or str(data)
        return SecretLease(
            secret_id=secret_id,
            value=value,
            lease_id=f"{secret_id}:{time.time()}",
            expires_at=time.time() + 3600,
            metadata=data,
        )

    async def revoke(self, lease_id: str):
        pass  # KV v2 doesn't have leasable secrets; revoke via token
```

---

## Solution 3: AWS Secrets Manager Integration with Automatic Rotation

Fetch secrets from AWS Secrets Manager, respect rotation events via EventBridge, and invalidate the local cache when a rotation occurs.

```python
import asyncio
import json
import time
import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass
class AWSSecret:
    secret_id: str
    value: Any
    version_id: str
    created_at: float


class AWSSecretsManagerClient:
    """
    AWS Secrets Manager client with rotation-aware caching.
    Subscribes to rotation events to invalidate cache immediately.
    """

    def __init__(
        self,
        region: str = "us-east-1",
        cache_ttl_seconds: float = 300.0,
    ):
        self.region = region
        self.cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[AWSSecret, float]] = {}  # id → (secret, expires_at)

    async def _fetch_from_aws(self, secret_id: str) -> AWSSecret:
        """Fetch from AWS Secrets Manager via boto3."""
        try:
            import boto3
            client = boto3.client("secretsmanager", region_name=self.region)
            response = client.get_secret_value(SecretId=secret_id)

            value = response.get("SecretString") or response.get("SecretBinary", b"").decode()
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass

            return AWSSecret(
                secret_id=secret_id,
                value=value,
                version_id=response.get("VersionId", ""),
                created_at=time.time(),
            )
        except ImportError:
            # Fallback for environments without boto3
            raise RuntimeError("boto3 required for AWS Secrets Manager")

    async def get_secret(self, secret_id: str, force_refresh: bool = False) -> AWSSecret:
        """Get secret with local cache."""
        cached = self._cache.get(secret_id)
        if cached and not force_refresh:
            secret, expires_at = cached
            if time.time() < expires_at:
                return secret

        secret = await self._fetch_from_aws(secret_id)
        self._cache[secret_id] = (secret, time.time() + self.cache_ttl)
        return secret

    def get_string(self, secret_id: str) -> str:
        """Synchronous version for non-async contexts."""
        import asyncio
        loop = asyncio.get_event_loop()
        secret = loop.run_until_complete(self.get_secret(secret_id))
        return secret.value if isinstance(secret.value, str) else json.dumps(secret.value)

    def on_rotation_event(self, secret_id: str):
        """
        Called when AWS EventBridge fires a rotation event.
        Invalidates cache so next access fetches fresh credentials.
        """
        if secret_id in self._cache:
            del self._cache[secret_id]
            print(f"[secrets] Cache invalidated for '{secret_id}' due to rotation event")

    async def get_api_key(self, secret_id: str, key_field: str = "api_key") -> str:
        """Convenience: extract a specific field from a JSON secret."""
        secret = await self.get_secret(secret_id)
        value = secret.value
        if isinstance(value, dict):
            return value.get(key_field) or value.get("value") or str(value)
        return str(value)

    def cache_stats(self) -> dict:
        now = time.time()
        return {
            "cached_secrets": len(self._cache),
            "secrets": {
                sid: {
                    "expires_in": expires_at - now,
                    "stale": now >= expires_at,
                }
                for sid, (_, expires_at) in self._cache.items()
            },
        }
```

---

## Solution 4: Least-Privilege Dynamic API Keys Per Operation

Instead of a single shared API key, generate scoped, short-lived credentials for each specific operation. Each key can only perform the operation it was issued for.

```python
import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Permission(Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    EXECUTE_TOOLS = "execute_tools"
    ADMIN = "admin"


@dataclass
class ScopedCredential:
    credential_id: str
    value: str
    permissions: list[Permission]
    tenant_id: str
    operation_id: str
    issued_at: float
    expires_at: float
    used_count: int = 0
    max_uses: int = 1  # Single-use by default

    def is_valid(self) -> bool:
        return time.time() < self.expires_at and self.used_count < self.max_uses

    def consume(self) -> bool:
        if not self.is_valid():
            return False
        self.used_count += 1
        return True


class LeastPrivilegeCredentialManager:
    """
    Issues scoped, short-lived credentials for specific operations.
    Each credential has the minimum permissions required for its intended use.
    """

    def __init__(self, master_key: str, issuer_id: str = "agent-vault"):
        self._master_key = master_key.encode()
        self._issuer = issuer_id
        self._issued: dict[str, ScopedCredential] = {}
        self._revoked: set[str] = set()
        self._audit_log: list[dict] = []

    def _generate_credential(
        self, scope: str, ttl_seconds: float
    ) -> str:
        """Generate a scoped HMAC-signed credential."""
        nonce = secrets.token_hex(16)
        payload = f"{scope}:{nonce}:{time.time()}"
        sig = hmac.new(self._master_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{nonce}.{sig[:32]}"

    def issue(
        self,
        tenant_id: str,
        operation_id: str,
        permissions: list[Permission],
        ttl_seconds: float = 300.0,
        max_uses: int = 1,
    ) -> ScopedCredential:
        scope = f"{tenant_id}:{operation_id}:{','.join(p.value for p in permissions)}"
        cred = ScopedCredential(
            credential_id=secrets.token_hex(8),
            value=self._generate_credential(scope, ttl_seconds),
            permissions=permissions,
            tenant_id=tenant_id,
            operation_id=operation_id,
            issued_at=time.time(),
            expires_at=time.time() + ttl_seconds,
            max_uses=max_uses,
        )
        self._issued[cred.credential_id] = cred
        self._audit("issued", cred)
        return cred

    def validate(
        self, credential_id: str, required_permissions: list[Permission]
    ) -> tuple[bool, str]:
        if credential_id in self._revoked:
            return False, "credential_revoked"

        cred = self._issued.get(credential_id)
        if not cred:
            return False, "credential_not_found"

        if not cred.is_valid():
            return False, f"credential_expired_or_exhausted"

        missing_perms = [p for p in required_permissions if p not in cred.permissions]
        if missing_perms:
            return False, f"insufficient_permissions: {missing_perms}"

        cred.consume()
        self._audit("used", cred)
        return True, "ok"

    def revoke(self, credential_id: str):
        self._revoked.add(credential_id)
        cred = self._issued.get(credential_id)
        if cred:
            self._audit("revoked", cred)

    def revoke_all_for_tenant(self, tenant_id: str):
        for cred_id, cred in self._issued.items():
            if cred.tenant_id == tenant_id:
                self._revoked.add(cred_id)
        print(f"[credentials] Revoked all credentials for tenant {tenant_id}")

    def _audit(self, action: str, cred: ScopedCredential):
        self._audit_log.append({
            "action": action,
            "credential_id": cred.credential_id,
            "tenant_id": cred.tenant_id,
            "operation_id": cred.operation_id,
            "permissions": [p.value for p in cred.permissions],
            "timestamp": time.time(),
        })

    def audit_trail(self, tenant_id: str | None = None) -> list[dict]:
        if tenant_id:
            return [e for e in self._audit_log if e["tenant_id"] == tenant_id]
        return list(self._audit_log)
```

---

## Solution 5: Secret Rotation Orchestrator

Coordinates zero-downtime rotation of long-lived secrets: generates a new version, propagates it to all agent instances, verifies the new version works, then revokes the old one.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class RotationPhase(Enum):
    IDLE = "idle"
    GENERATING = "generating"
    PROPAGATING = "propagating"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"
    FAILED = "failed"


@dataclass
class RotationJob:
    secret_id: str
    phase: RotationPhase = RotationPhase.IDLE
    old_value: str = ""
    new_value: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    error: str = ""


class SecretRotationOrchestrator:
    """
    Coordinates zero-downtime secret rotation:
    1. Generate new secret value
    2. Propagate to all instances (dual-write old+new during transition)
    3. Verify new secret works
    4. Revoke old secret
    """

    def __init__(
        self,
        generate_fn: Callable,    # async fn(secret_id) → new_value
        propagate_fn: Callable,   # async fn(secret_id, new_value) → bool
        verify_fn: Callable,      # async fn(secret_id, value) → bool
        revoke_fn: Callable,      # async fn(secret_id, old_value)
    ):
        self._generate = generate_fn
        self._propagate = propagate_fn
        self._verify = verify_fn
        self._revoke = revoke_fn
        self._jobs: dict[str, RotationJob] = {}

    async def rotate(self, secret_id: str, dry_run: bool = False) -> RotationJob:
        job = RotationJob(secret_id=secret_id)
        self._jobs[secret_id] = job

        try:
            # Phase 1: Generate
            job.phase = RotationPhase.GENERATING
            print(f"[rotation] {secret_id}: Generating new value...")
            new_value = await self._generate(secret_id)
            job.new_value = new_value

            if dry_run:
                print(f"[rotation] {secret_id}: Dry run — stopping after generation")
                job.phase = RotationPhase.IDLE
                return job

            # Phase 2: Propagate (both old and new are valid during this window)
            job.phase = RotationPhase.PROPAGATING
            print(f"[rotation] {secret_id}: Propagating to instances...")
            propagated = await self._propagate(secret_id, new_value)
            if not propagated:
                raise RuntimeError("Propagation failed")

            # Phase 3: Verify the new secret works end-to-end
            job.phase = RotationPhase.VERIFYING
            print(f"[rotation] {secret_id}: Verifying new value...")
            works = await self._verify(secret_id, new_value)
            if not works:
                raise RuntimeError("Verification failed — new secret rejected by service")

            # Phase 4: Revoke old secret
            job.phase = RotationPhase.FINALIZING
            if job.old_value:
                print(f"[rotation] {secret_id}: Revoking old value...")
                await self._revoke(secret_id, job.old_value)

            job.phase = RotationPhase.IDLE
            job.completed_at = time.time()
            duration = job.completed_at - job.started_at
            print(f"[rotation] {secret_id}: Rotation complete in {duration:.1f}s")

        except Exception as e:
            job.phase = RotationPhase.FAILED
            job.error = str(e)
            print(f"[rotation] {secret_id}: FAILED at {job.phase.value}: {e}")

        return job

    async def rotate_many(self, secret_ids: list[str], parallel: bool = False) -> dict[str, RotationJob]:
        if parallel:
            tasks = [self.rotate(sid) for sid in secret_ids]
            jobs = await asyncio.gather(*tasks, return_exceptions=True)
            return {sid: job for sid, job in zip(secret_ids, jobs) if isinstance(job, RotationJob)}
        else:
            results = {}
            for sid in secret_ids:
                results[sid] = await self.rotate(sid)
            return results

    def rotation_status(self) -> dict:
        return {
            sid: {
                "phase": job.phase.value,
                "started_ago": time.time() - job.started_at,
                "error": job.error,
            }
            for sid, job in self._jobs.items()
        }
```

---

## Solution 6: SecretManager Facade — Unified Interface

A single `SecretManager` class that abstracts over HashiCorp Vault, AWS Secrets Manager, or environment variables, with automatic fallback and a unified audit API.

```python
import os
import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class BackendType(Enum):
    ENV = "env"
    HASHICORP_VAULT = "hashicorp_vault"
    AWS_SECRETS_MANAGER = "aws_secrets_manager"
    MOCK = "mock"  # For testing


class SecretManager:
    """
    Unified secret management facade.
    Supports multiple backends with automatic fallback chain.
    All accesses are logged for audit purposes.
    """

    def __init__(self, primary: BackendType, fallback: BackendType | None = BackendType.ENV):
        self.primary_type = primary
        self.fallback_type = fallback
        self._primary_backend = self._init_backend(primary)
        self._fallback_backend = self._init_backend(fallback) if fallback else None
        self._access_log: list[dict] = []
        self._cache = SecretCache(self._primary_backend) if primary != BackendType.ENV else None

    def _init_backend(self, backend_type: BackendType):
        if backend_type == BackendType.HASHICORP_VAULT:
            return HashiCorpVaultClient(
                vault_addr=os.environ.get("VAULT_ADDR", "http://localhost:8200"),
                role_id=os.environ.get("VAULT_ROLE_ID", ""),
                secret_id=os.environ.get("VAULT_SECRET_ID", ""),
            )
        elif backend_type == BackendType.AWS_SECRETS_MANAGER:
            return AWSSecretsManagerClient(
                region=os.environ.get("AWS_REGION", "us-east-1")
            )
        elif backend_type == BackendType.ENV:
            return EnvBackend()
        elif backend_type == BackendType.MOCK:
            return MockBackend()
        return None

    async def get(self, secret_id: str, field: str | None = None) -> str:
        """
        Get a secret value. Tries primary backend, falls back to secondary.
        All accesses are logged.
        """
        value = None
        source = None

        # Try primary
        try:
            if self._cache:
                lease = await self._cache.get(secret_id)
                raw = lease.value
            else:
                raw = await self._primary_backend.fetch_string(secret_id)
            value = raw
            source = self.primary_type.value
        except Exception as e:
            print(f"[secrets] Primary backend failed for '{secret_id}': {e}")
            if self._fallback_backend:
                try:
                    raw = await self._fallback_backend.fetch_string(secret_id)
                    value = raw
                    source = f"fallback:{self.fallback_type.value}"
                except Exception as e2:
                    raise RuntimeError(f"All backends failed for '{secret_id}': primary={e}, fallback={e2}")

        if value is None:
            raise KeyError(f"Secret not found: {secret_id}")

        # Extract field if JSON
        if field:
            import json
            try:
                data = json.loads(value)
                value = data.get(field, "")
            except (json.JSONDecodeError, TypeError):
                pass

        self._log_access(secret_id, source, bool(value))
        return value

    def _log_access(self, secret_id: str, source: str, success: bool):
        self._access_log.append({
            "secret_id": secret_id,
            "source": source,
            "success": success,
            "timestamp": time.time(),
        })

    def access_audit_trail(self, secret_id: str | None = None) -> list[dict]:
        if secret_id:
            return [e for e in self._access_log if e["secret_id"] == secret_id]
        return list(self._access_log)

    async def rotate(self, secret_id: str):
        """Trigger rotation for this secret."""
        if self._cache:
            await self._cache.revoke(secret_id)
        print(f"[secrets] Triggered rotation for '{secret_id}'")


class EnvBackend:
    """Falls back to environment variables."""
    async def fetch_string(self, secret_id: str) -> str:
        env_key = secret_id.upper().replace("/", "_").replace("-", "_")
        value = os.environ.get(env_key)
        if value is None:
            raise KeyError(f"Env var not set: {env_key}")
        return value


class MockBackend:
    """In-memory mock for testing."""
    def __init__(self):
        self._store: dict[str, str] = {}

    def set(self, key: str, value: str):
        self._store[key] = value

    async def fetch_string(self, secret_id: str) -> str:
        if secret_id not in self._store:
            raise KeyError(f"Mock secret not found: {secret_id}")
        return self._store[secret_id]

    async def fetch(self, secret_id: str) -> SecretLease:
        value = await self.fetch_string(secret_id)
        return SecretLease(
            secret_id=secret_id, value=value,
            lease_id=f"mock:{secret_id}",
            expires_at=time.time() + 3600,
        )

    async def revoke(self, lease_id: str):
        pass
```

---

## Comparison

| Pattern | Rotation Support | Audit Trail | Zero-Downtime | Best For |
|---|---|---|---|---|
| In-process TTL cache | Yes (auto-refresh) | No | Yes | Any agent — baseline improvement over env vars |
| HashiCorp Vault AppRole | Yes (Vault manages) | Yes (Vault audit) | Yes | Self-hosted infrastructure |
| AWS Secrets Manager | Yes (managed rotation) | Yes (CloudTrail) | Yes | AWS-native deployments |
| Least-privilege dynamic keys | Per-operation | Yes (built-in) | Yes | Multi-tenant or high-security agents |
| Rotation orchestrator | Full lifecycle | Partial | Yes (dual-write) | Coordinating rotation across instances |
| SecretManager facade | Depends on backend | Yes (access log) | Yes | Portability across environments |

**Recommendations:**
- Replace all `os.environ["API_KEY"]` with **SecretManager facade** (Solution 6) immediately — it's a drop-in change that adds rotation and audit capability.
- Use **HashiCorp Vault** (Solution 2) or **AWS Secrets Manager** (Solution 3) as the primary backend for production agents.
- Implement **least-privilege dynamic keys** (Solution 4) for multi-tenant agents where different tenants must not share credentials.
- Run the **rotation orchestrator** (Solution 5) on a schedule (every 24-48 hours) to ensure credentials are never more than 2 days old.
- Keep the **in-process TTL cache** (Solution 1) as a performance layer in front of any remote vault — it prevents vault rate limiting on high-traffic agents.
