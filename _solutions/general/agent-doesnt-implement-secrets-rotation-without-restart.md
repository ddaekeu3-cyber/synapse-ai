---
layout: solution
title: "Agent Doesn't Implement Secrets Rotation Without Restart"
category: general
description: "Agent reads API keys and secrets at startup and caches them for the process lifetime — when secrets are rotated, the agent continues using the old (now-revoked) key until it is manually restarted, causing a window of 401 failures."
tags: [security, secrets, credentials, reliability, rotation]
---

## Symptom

Security team rotates the Anthropic API key at 02:00 AM. At 02:01 AM, the agent starts returning errors:

```
[02:01:03] APIError: 401 Unauthorized {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}
[02:01:04] APIError: 401 Unauthorized
[02:01:05] APIError: 401 Unauthorized
...
[02:15:00] [PAGERDUTY] Agent down — 14 minutes of 401 errors
[02:15:30] On-call engineer restarts process — service restored
```

14 minutes of downtime caused by a planned security operation.

## Root Cause

The API key is read once at module load time and cached in the client object:

```python
import os
import anthropic

# Key read at import time — never refreshed
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def ask(prompt: str) -> str:
    # client always uses the key from startup
    return client.messages.create(...).content[0].text
```

When the secret is rotated in Vault, AWS Secrets Manager, or the environment, the in-memory client is unaffected.

---

## Fix

### Option 1 — Re-read the key from environment on each request

Don't cache the client at module level. Create a lightweight wrapper that reads the current key on each call.

```python
import os
import anthropic


def get_client() -> anthropic.Anthropic:
    """Read key from environment on every call — picks up rotated keys automatically."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=key)


def ask(prompt: str) -> str:
    # Fresh client on each call — always uses current key
    response = get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# After secrets rotation: next call automatically uses new key
print(ask("Hello"))

# Expected Token Savings: zero downtime during rotation → no failed calls burned
# Environment: any agent where restart during rotation is unacceptable
```

---

### Option 2 — TTL-cached client with periodic refresh

Cache the client for a configurable TTL (e.g., 5 minutes). After TTL expires, re-read the key and build a new client.

```python
import os
import time
import threading
import anthropic

KEY_TTL_SECONDS = 300  # Refresh key every 5 minutes


class RotatingClient:
    def __init__(self, ttl: float = KEY_TTL_SECONDS):
        self._ttl = ttl
        self._client: anthropic.Anthropic | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> anthropic.Anthropic:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        client = anthropic.Anthropic(api_key=key)
        self._client = client
        self._expires_at = time.monotonic() + self._ttl
        return client

    @property
    def client(self) -> anthropic.Anthropic:
        with self._lock:
            if self._client is None or time.monotonic() >= self._expires_at:
                return self._refresh()
            return self._client

    def messages_create(self, **kwargs):
        """Forward to current client — retries once on auth error with fresh key."""
        try:
            return self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            # Auth failure → force immediate refresh and retry once
            with self._lock:
                self._expires_at = 0.0
            return self.client.messages.create(**kwargs)


_client = RotatingClient(ttl=300)


def ask(prompt: str) -> str:
    response = _client.messages_create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


print(ask("What is the capital of France?"))

# Expected Token Savings: at most TTL/2 window of stale key; automatic retry on 401
# Environment: high-frequency agents where creating a new client per call adds overhead
```

---

### Option 3 — AWS Secrets Manager with live refresh

Fetch the secret from AWS Secrets Manager at startup and on a background refresh interval. Fall back to cached value if Secrets Manager is unreachable.

```python
import os
import json
import time
import threading
import boto3
import anthropic

SECRET_NAME = os.environ.get("SECRET_NAME", "prod/anthropic-api-key")
REFRESH_INTERVAL = 600  # 10 minutes


class SecretsManagerClient:
    def __init__(self, secret_name: str, refresh_interval: float = REFRESH_INTERVAL):
        self._secret_name = secret_name
        self._refresh_interval = refresh_interval
        self._api_key: str = ""
        self._client: anthropic.Anthropic | None = None
        self._lock = threading.RLock()
        self._sm = boto3.client("secretsmanager")

        self._load_secret()
        self._start_refresh_thread()

    def _load_secret(self) -> None:
        try:
            resp = self._sm.get_secret_value(SecretId=self._secret_name)
            raw = resp.get("SecretString", "{}")
            data = json.loads(raw)
            new_key = data.get("ANTHROPIC_API_KEY", "")

            if new_key and new_key != self._api_key:
                with self._lock:
                    self._api_key = new_key
                    self._client = anthropic.Anthropic(api_key=new_key)
                print(f"[secrets] Key refreshed from Secrets Manager")

        except Exception as exc:
            print(f"[secrets] Refresh failed: {exc} — using cached key")

    def _start_refresh_thread(self) -> None:
        def loop():
            while True:
                time.sleep(self._refresh_interval)
                self._load_secret()

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    @property
    def client(self) -> anthropic.Anthropic:
        with self._lock:
            if self._client is None:
                raise RuntimeError("No API key loaded")
            return self._client

    def ask(self, prompt: str) -> str:
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text
        except anthropic.AuthenticationError:
            # Force immediate refresh on auth failure
            self._load_secret()
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text


# agent = SecretsManagerClient(SECRET_NAME)
# print(agent.ask("Hello"))

# Expected Token Savings: zero downtime during rotation; refresh thread picks up new key within 10 min
# Environment: AWS-deployed agents using Secrets Manager for credential management
```

---

### Option 4 — File-watcher based rotation (Kubernetes secrets)

In Kubernetes, mounted secrets are updated in-place as files. Watch the file for changes and reload without restarting.

```python
import os
import time
import threading
import anthropic
from pathlib import Path

SECRET_PATH = Path(os.environ.get("API_KEY_FILE", "/run/secrets/anthropic_api_key"))
WATCH_INTERVAL = 30  # seconds


class FileSecretClient:
    def __init__(self, secret_path: Path):
        self._path = secret_path
        self._api_key: str = ""
        self._mtime: float = 0.0
        self._client: anthropic.Anthropic | None = None
        self._lock = threading.RLock()

        self._reload_if_changed()
        self._start_watcher()

    def _reload_if_changed(self) -> bool:
        try:
            stat = self._path.stat()
            if stat.st_mtime == self._mtime:
                return False  # Unchanged

            new_key = self._path.read_text().strip()
            if not new_key:
                return False

            with self._lock:
                self._api_key = new_key
                self._mtime = stat.st_mtime
                self._client = anthropic.Anthropic(api_key=new_key)

            print(f"[secrets] Loaded new key from {self._path}")
            return True

        except FileNotFoundError:
            print(f"[secrets] Key file not found: {self._path}")
            return False

    def _start_watcher(self) -> None:
        def loop():
            while True:
                time.sleep(WATCH_INTERVAL)
                self._reload_if_changed()

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    @property
    def client(self) -> anthropic.Anthropic:
        with self._lock:
            if self._client is None:
                raise RuntimeError("Secret not loaded")
            return self._client

    def ask(self, prompt: str) -> str:
        for attempt in range(2):
            try:
                return self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": prompt}]
                ).content[0].text
            except anthropic.AuthenticationError:
                if attempt == 0:
                    self._reload_if_changed()
                else:
                    raise


# Kubernetes mounts updated secret as file — watcher detects and reloads
# file_client = FileSecretClient(SECRET_PATH)
# print(file_client.ask("Hello"))

# Expected Token Savings: Kubernetes secret rotation propagates within WATCH_INTERVAL
# Environment: Kubernetes-deployed agents with mounted secret volumes
```

---

### Option 5 — On-demand key validation with circuit breaker

Add a circuit breaker that detects 401 errors, validates whether a fresh key is available, and switches to it automatically.

```python
import os
import time
import anthropic
from enum import StrEnum

class CircuitState(StrEnum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Key rejected — trying rotation
    HALF_OPEN = "half_open"  # Testing new key


class RotationCircuitBreaker:
    def __init__(self):
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure: float = 0.0
        self._failure_threshold = 3
        self._recovery_timeout = 60.0  # Try new key after 60s
        self._current_key: str = os.environ.get("ANTHROPIC_API_KEY", "")

    def _get_fresh_key(self) -> str:
        """Hook: replace with Vault/Secrets Manager fetch in production."""
        # In production: fetch from Secrets Manager, Vault, or reload from file
        fresh = os.environ.get("ANTHROPIC_API_KEY", "")
        return fresh

    def ask(self, prompt: str) -> str:
        now = time.monotonic()

        if self._state == CircuitState.OPEN:
            if now - self._last_failure < self._recovery_timeout:
                raise RuntimeError("Circuit open — auth failures detected, rotation in progress")
            self._state = CircuitState.HALF_OPEN

        if self._state == CircuitState.HALF_OPEN:
            fresh = self._get_fresh_key()
            if fresh and fresh != self._current_key:
                self._current_key = fresh
                print("[circuit] Switched to new API key")

        client = anthropic.Anthropic(api_key=self._current_key)

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            # Success — close circuit
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            return response.content[0].text

        except anthropic.AuthenticationError:
            self._failure_count += 1
            self._last_failure = now

            if self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN
                print(f"[circuit] OPEN — {self._failure_count} auth failures")

            raise


breaker = RotationCircuitBreaker()

try:
    print(breaker.ask("Hello world"))
except Exception as e:
    print(f"Error: {e}")

# Expected Token Savings: circuit opens fast → failed calls stop quickly vs infinite 401 retry
# Environment: any agent that needs automatic recovery from credential rotation failures
```

---

### Option 6 — Health endpoint that reports key status + rotation signal

Expose a `/rotate` endpoint. Ops team or automation calls it after rotating the secret — agent reloads without restart.

```python
import os
import threading
import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

_lock = threading.RLock()
_current_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
_client: anthropic.Anthropic = anthropic.Anthropic(api_key=_current_key)
_rotation_count: int = 0


def get_client() -> anthropic.Anthropic:
    with _lock:
        return _client


@app.post("/internal/rotate-secret")
async def rotate_secret(body: dict):
    """
    Called by ops automation after rotating the secret in Vault/AWS SM.
    Body: {"api_key": "sk-live-...new-key..."}
    """
    global _current_key, _client, _rotation_count

    new_key = body.get("api_key", "").strip()
    if not new_key:
        raise HTTPException(status_code=400, detail="api_key is required")

    if not new_key.startswith("sk-"):
        raise HTTPException(status_code=400, detail="Invalid key format")

    # Test new key before switching
    test_client = anthropic.Anthropic(api_key=new_key)
    try:
        test_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "test"}]
        )
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=400, detail="New key validation failed — not switching")

    with _lock:
        _current_key = new_key
        _client = test_client
        _rotation_count += 1

    return {"rotated": True, "rotation_count": _rotation_count}


@app.get("/health")
async def health():
    client = get_client()
    key_prefix = _current_key[:12] + "..." if _current_key else "NOT SET"
    return {
        "status": "ok",
        "key_prefix": key_prefix,
        "rotation_count": _rotation_count,
    }


@app.post("/ask")
async def ask(body: dict):
    prompt = body.get("prompt", "")
    client = get_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return {"result": response.content[0].text}

# After rotation: POST /internal/rotate-secret {"api_key": "sk-live-...new..."}
# Agent validates and switches — zero restart, zero downtime

# Expected Token Savings: zero downtime during planned rotations → no failed API calls
# Environment: production FastAPI agents with automated secret rotation pipelines
```

---

## Comparison

| Option | Zero Downtime | Restart Required | Secrets Manager | File Watch | Auth Retry |
|--------|--------------|------------------|-----------------|------------|------------|
| 1 | Yes (env re-read) | No | No | No | No |
| 2 | Mostly (TTL gap) | No | No | No | Yes |
| 3 | Yes | No | AWS | No | Yes |
| 4 | Yes | No | No | Yes (K8s) | Yes |
| 5 | Yes | No | No | No | Circuit |
| 6 | Yes | No | Any | No | Yes |

**Recommended starting point:** Option 2 (TTL-cached client with 401 retry) for most agents — a 5-line wrapper around the existing client construction with automatic re-read on auth failure. Option 3 for AWS deployments. Option 4 for Kubernetes with mounted secrets.
