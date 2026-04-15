---
layout: solution
title: "Agent Doesn't Implement Secrets Rotation Without Restart"
category: config
description: "Agents that bake secrets into module-level variables require a full restart to pick up rotated credentials, causing downtime and stale-key failures."
tags: [config, secrets, rotation, hot-reload, vault, kubernetes]
---

# Agent Doesn't Implement Secrets Rotation Without Restart

When API keys, database passwords, or JWT signing keys are rotated, agents with hardcoded or module-level secrets continue using the old value until restarted. This forces a deployment for every secret rotation, creating downtime windows and leaving a gap where the old key is still in use.

## Why This Happens

Module-level `os.getenv("API_KEY")` is evaluated once at import time. There's no mechanism to re-read environment variables, re-mount secret files, or re-fetch from a secrets manager without restarting the process.

---

## Option 1: File-Watched Secret Reload

Watch the secret file on disk and reload it in-place when it changes. Works with Kubernetes Secret volume mounts.

```python
import os
import time
import threading
import anthropic

SECRET_FILE = "/run/secrets/anthropic_api_key"


class RotatingSecret:
    """Thread-safe secret value that reloads from file on change."""

    def __init__(self, path: str, poll_interval: float = 10.0):
        self._path = path
        self._value: str = self._read()
        self._mtime: float = os.path.getmtime(path)
        self._lock = threading.RLock()
        self._stop = threading.Event()

        thread = threading.Thread(target=self._watch, daemon=True)
        thread.start()

    def _read(self) -> str:
        with open(self._path) as f:
            return f.read().strip()

    def _watch(self):
        while not self._stop.wait(10.0):
            try:
                mtime = os.path.getmtime(self._path)
                if mtime != self._mtime:
                    new_value = self._read()
                    with self._lock:
                        self._value = new_value
                        self._mtime = mtime
                    print(f"[RotatingSecret] Reloaded {self._path}")
            except Exception as e:
                print(f"[RotatingSecret] Watch error: {e}")

    @property
    def value(self) -> str:
        with self._lock:
            return self._value

    def stop(self):
        self._stop.set()


# Initialize once at startup
api_key_secret = RotatingSecret(SECRET_FILE)


def get_client() -> anthropic.Anthropic:
    """Always build client with current secret value."""
    return anthropic.Anthropic(api_key=api_key_secret.value)


def run_agent(prompt: str) -> str:
    client = get_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


if __name__ == "__main__":
    # Simulate usage over time; secret rotates without restart
    for i in range(5):
        print(run_agent(f"Hello, iteration {i}"))
        time.sleep(15)
```

**Expected Token Savings:** Zero direct savings; eliminates downtime from secret rotation restarts.

**Environment:** Kubernetes with Secret volume mounts; any containerized deployment.

---

## Option 2: Environment Variable Hot-Reload via Signal

Handle `SIGHUP` to re-read environment variables injected by the container runtime.

```python
import os
import signal
import threading
import anthropic

_secrets_lock = threading.RLock()
_current_secrets: dict[str, str] = {}


def load_secrets():
    """Re-read all secrets from environment."""
    global _current_secrets
    new_secrets = {
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        "DATABASE_PASSWORD": os.environ.get("DATABASE_PASSWORD", ""),
        "JWT_SECRET": os.environ.get("JWT_SECRET", ""),
    }
    with _secrets_lock:
        _current_secrets = new_secrets
    print("[Secrets] Reloaded from environment")


def get_secret(name: str) -> str:
    with _secrets_lock:
        return _current_secrets.get(name, "")


def setup_rotation_handler():
    """Register SIGHUP handler for secret rotation."""
    def handler(signum, frame):
        threading.Thread(target=load_secrets, daemon=True).start()

    signal.signal(signal.SIGHUP, handler)
    print("[Secrets] SIGHUP handler registered for rotation")


# Initialize
load_secrets()
setup_rotation_handler()


def get_anthropic_client() -> anthropic.Anthropic:
    key = get_secret("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=key)


def run_agent(prompt: str) -> str:
    client = get_anthropic_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# To rotate: update env vars externally, then `kill -HUP <pid>`
if __name__ == "__main__":
    import time
    print(f"PID: {os.getpid()} — send SIGHUP to rotate secrets")
    while True:
        print(run_agent("Ping"))
        time.sleep(30)
```

**Expected Token Savings:** Near-zero rotation downtime; no restart required; SIGHUP is instantaneous.

**Environment:** Linux/macOS; Docker with externally-managed env injection; systemd services.

---

## Option 3: HashiCorp Vault Dynamic Secrets with Lease Renewal

Fetch short-lived secrets from Vault and automatically renew or re-fetch before expiry.

```python
import asyncio
import time
import httpx
import anthropic

VAULT_ADDR = "http://vault:8200"
VAULT_TOKEN = "your-vault-token"
SECRET_PATH = "secret/data/anthropic"


class VaultSecretManager:
    def __init__(self, vault_addr: str, token: str, renew_before: float = 60.0):
        self._addr = vault_addr
        self._token = token
        self._renew_before = renew_before
        self._secret: str = ""
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _fetch(self) -> tuple[str, float]:
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"{self._addr}/v1/{SECRET_PATH}",
                headers={"X-Vault-Token": self._token},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            secret = data["data"]["data"]["api_key"]
            # Vault lease duration in seconds
            lease_duration = data.get("lease_duration", 3600)
            expires_at = time.time() + lease_duration
            return secret, expires_at

    async def get(self) -> str:
        async with self._lock:
            if time.time() >= self._expires_at - self._renew_before:
                self._secret, self._expires_at = await self._fetch()
                print(
                    f"[Vault] Secret refreshed, expires in "
                    f"{self._expires_at - time.time():.0f}s"
                )
            return self._secret


vault_secrets = VaultSecretManager(VAULT_ADDR, VAULT_TOKEN)
async_client = anthropic.AsyncAnthropic()


async def run_agent(prompt: str) -> str:
    api_key = await vault_secrets.get()

    # Re-create client with fresh key
    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def main():
    # Simulate continuous operation with automatic secret refresh
    for i in range(10):
        result = await run_agent(f"Query {i}")
        print(f"[{i}] {result[:80]}")
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Dynamic short-lived secrets; rotated automatically every lease period without any deployment.

**Environment:** HashiCorp Vault; AWS Secrets Manager (swap `_fetch` implementation).

---

## Option 4: Pydantic Settings with Periodic Refresh

Use Pydantic BaseSettings for structured config and add a background refresh loop that re-instantiates settings from the environment.

```python
import asyncio
import time
import os
from pydantic_settings import BaseSettings
from pydantic import SecretStr
import anthropic


class AgentSettings(BaseSettings):
    anthropic_api_key: SecretStr
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1024

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


class RefreshingSettings:
    """Wraps AgentSettings with periodic re-loading."""

    def __init__(self, refresh_interval: float = 60.0):
        self._settings = AgentSettings()
        self._loaded_at = time.monotonic()
        self._interval = refresh_interval
        self._lock = asyncio.Lock()

    async def get(self) -> AgentSettings:
        async with self._lock:
            age = time.monotonic() - self._loaded_at
            if age >= self._interval:
                try:
                    self._settings = AgentSettings()
                    self._loaded_at = time.monotonic()
                    print(f"[Settings] Refreshed after {age:.1f}s")
                except Exception as e:
                    print(f"[Settings] Refresh failed: {e} — keeping old settings")
            return self._settings


settings_manager = RefreshingSettings(refresh_interval=30.0)


async def run_agent(prompt: str) -> str:
    settings = await settings_manager.get()
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value()
    )
    response = await client.messages.create(
        model=settings.model,
        max_tokens=settings.max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def main():
    # Update .env between calls to simulate rotation
    tasks = [run_agent(f"Hello {i}") for i in range(5)]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(r[:80])


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Settings refresh every 30s; new keys picked up without restart or signal.

**Environment:** Apps using pydantic-settings; `.env` files or environment injection.

---

## Option 5: AWS Secrets Manager with Automatic Rotation

Fetch secrets from AWS Secrets Manager and respect the `VersionStage: AWSCURRENT` label for zero-downtime rotation.

```python
import asyncio
import time
import json
import boto3
from botocore.exceptions import ClientError
import anthropic

SECRET_NAME = "prod/synapse-agent/anthropic"
CACHE_TTL = 300  # 5 minutes


class AWSSecretsCache:
    def __init__(self, secret_name: str, region: str = "us-east-1"):
        self._name = secret_name
        self._client = boto3.client("secretsmanager", region_name=region)
        self._value: dict = {}
        self._fetched_at: float = 0.0

    def get(self, key: str) -> str:
        if time.time() - self._fetched_at > CACHE_TTL:
            self._refresh()
        return self._value.get(key, "")

    def _refresh(self):
        try:
            resp = self._client.get_secret_value(
                SecretId=self._name,
                VersionStage="AWSCURRENT",
            )
            self._value = json.loads(resp["SecretString"])
            self._fetched_at = time.time()
            print(f"[AWS Secrets] Refreshed {self._name}")
        except ClientError as e:
            print(f"[AWS Secrets] Refresh failed: {e}")
            # Keep stale value rather than crash
        except json.JSONDecodeError as e:
            print(f"[AWS Secrets] JSON parse error: {e}")


secrets = AWSSecretsCache(SECRET_NAME)


def get_anthropic_client() -> anthropic.Anthropic:
    api_key = secrets.get("ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=api_key)


def run_agent(prompt: str) -> str:
    client = get_anthropic_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# AWS rotates the secret; agent picks it up within CACHE_TTL seconds
if __name__ == "__main__":
    for i in range(20):
        print(run_agent(f"Iteration {i}"))
        time.sleep(30)
```

**Expected Token Savings:** Follows AWS automatic rotation schedule; no deployment needed when rotation fires.

**Environment:** AWS deployments; compatible with RDS password rotation, API key rotation workflows.

---

## Option 6: Dual-Key Grace Period During Rotation

Support two active keys simultaneously during rotation to prevent any request failures while old key is phased out.

```python
import threading
import time
import anthropic

# Both keys are valid during rotation window
_active_keys: list[str] = []
_keys_lock = threading.RLock()

ROTATION_GRACE_PERIOD = 300  # 5 minutes


def update_keys(new_key: str, old_key: str | None = None):
    """
    Called when a new key is provisioned.
    Keep old key active for grace period.
    """
    with _keys_lock:
        _active_keys.clear()
        _active_keys.append(new_key)
        if old_key:
            _active_keys.append(old_key)
            # Schedule old key removal after grace period
            timer = threading.Timer(
                ROTATION_GRACE_PERIOD,
                lambda: _retire_key(old_key),
            )
            timer.daemon = True
            timer.start()
            print(f"[Keys] New key active; old key valid for {ROTATION_GRACE_PERIOD}s")


def _retire_key(key: str):
    with _keys_lock:
        if key in _active_keys:
            _active_keys.remove(key)
            print("[Keys] Old key retired")


def try_call_with_rotation(prompt: str) -> str:
    """Try each active key; succeed with first working key."""
    with _keys_lock:
        keys = list(_active_keys)

    last_error: Exception | None = None
    for key in keys:
        try:
            client = anthropic.Anthropic(api_key=key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.AuthenticationError as e:
            last_error = e
            print(f"[Keys] Key failed, trying next: {e}")
            continue

    if last_error:
        raise last_error
    raise RuntimeError("No active keys")


if __name__ == "__main__":
    # Initial key
    update_keys("sk-ant-initial-key")

    # Simulate rotation: new key provisioned, old kept for grace period
    time.sleep(2)
    update_keys("sk-ant-rotated-key", old_key="sk-ant-initial-key")

    # All requests during grace period succeed regardless of which key the
    # upstream rotator has already invalidated
    for i in range(3):
        try:
            print(try_call_with_rotation(f"Ping {i}"))
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(1)
```

**Expected Token Savings:** Zero failed requests during rotation; eliminates authentication errors that would otherwise require retries.

**Environment:** High-availability production agents; critical paths where any auth failure is unacceptable.

---

## Comparison

| Option | Reload Trigger | Zero Downtime | External System | Grace Period |
|--------|---------------|---------------|-----------------|--------------|
| 1. File watch | inotify/poll | Yes | K8s Secrets | No |
| 2. SIGHUP handler | OS signal | Yes | Any injector | No |
| 3. Vault dynamic | TTL-based | Yes | HashiCorp Vault | Vault lease |
| 4. Pydantic refresh | Time interval | Near-zero | `.env` / env | No |
| 5. AWS Secrets Manager | TTL-based | Yes | AWS | AWS rotation |
| 6. Dual-key grace | Manual call | Yes | Any | Configurable |
