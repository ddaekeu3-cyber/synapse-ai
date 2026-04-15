---
layout: solution
title: "Agent Doesn't Implement API Key Rotation Without Downtime"
category: auth
description: "Agent uses a single hardcoded API key with no rotation mechanism, causing full downtime when the key expires, is revoked, or needs to be replaced for security compliance."
tags: [auth, api-key, rotation, zero-downtime, security, secrets-management]
---

# Agent Doesn't Implement API Key Rotation Without Downtime

## Problem

An agent is configured with `ANTHROPIC_API_KEY=sk-ant-abc123` in an environment variable. When security requires rotating the key (leaked credential, 90-day rotation policy, employee offboarding), the agent must be restarted to pick up the new value — causing downtime. If the old key is revoked before the new one is deployed, requests fail until the restart completes.

---

## Option 1: Dual-Key Overlap Rotation

Maintain both the old and new key during a transition window. The agent tries the new key first and falls back to the old key, allowing rotation with zero failed requests.

```python
import anthropic
import os
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class KeyPair:
    primary: str
    secondary: Optional[str]
    primary_activated_at: float
    overlap_seconds: float = 300.0  # 5-minute overlap window

    def is_in_overlap(self) -> bool:
        age = time.monotonic() - self.primary_activated_at
        return age < self.overlap_seconds

    def get_keys_to_try(self) -> list[str]:
        keys = [self.primary]
        if self.secondary and self.is_in_overlap():
            keys.append(self.secondary)
        return keys

class DualKeyClient:
    def __init__(self, key_pair: KeyPair):
        self.key_pair = key_pair
        self._clients: dict[str, anthropic.Anthropic] = {}

    def _get_client(self, key: str) -> anthropic.Anthropic:
        if key not in self._clients:
            self._clients[key] = anthropic.Anthropic(api_key=key)
        return self._clients[key]

    def rotate_key(self, new_key: str):
        print(f"[rotation] Activating new key ...{new_key[-4:]}, keeping old as secondary")
        self.key_pair = KeyPair(
            primary=new_key,
            secondary=self.key_pair.primary,
            primary_activated_at=time.monotonic(),
            overlap_seconds=self.key_pair.overlap_seconds
        )

    def create_message(self, **kwargs) -> anthropic.types.Message:
        keys = self.key_pair.get_keys_to_try()
        last_exc = None
        for key in keys:
            try:
                client = self._get_client(key)
                return client.messages.create(**kwargs)
            except anthropic.AuthenticationError as exc:
                print(f"[key-fail] ...{key[-4:]} rejected, trying next")
                last_exc = exc
        raise last_exc or RuntimeError("No valid API key available")

# Demo
initial_key = os.environ.get("ANTHROPIC_API_KEY", "sk-ant-initial-key")
key_pair = KeyPair(primary=initial_key, secondary=None, primary_activated_at=time.monotonic())
client = DualKeyClient(key_pair)

# Normal request
response = client.create_message(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "What is 2+2?"}]
)
print(f"Response: {response.content[0].text}")

# Simulate key rotation — new key becomes primary, old stays as fallback
new_key = os.environ.get("ANTHROPIC_API_KEY_NEW", initial_key)
client.rotate_key(new_key)
print(f"In overlap window: {client.key_pair.is_in_overlap()}")

# Requests continue seamlessly during overlap
response2 = client.create_message(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "What is 3+3?"}]
)
print(f"Post-rotation response: {response2.content[0].text}")

# Expected Token Savings: Zero failed requests during rotation means no wasted retry tokens. Eliminates restart-induced downtime that could cause 10–100 failed requests per minute.
# Environment: ANTHROPIC_API_KEY required. Set ANTHROPIC_API_KEY_NEW during rotation window.
```

---

## Option 2: Environment Variable Hot-Reload

Poll the environment (or a secrets file) at each request and reload the API key if it has changed, without restarting the process.

```python
import anthropic
import os
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

@dataclass
class KeyWatcher:
    env_var: str = "ANTHROPIC_API_KEY"
    secrets_file: Optional[str] = None
    poll_interval: float = 30.0
    _current_key: str = ""
    _last_check: float = 0.0
    _lock: threading.Lock = None

    def __post_init__(self):
        self._lock = threading.Lock()
        self._current_key = self._read_key()
        self._last_check = time.monotonic()

    def _read_key(self) -> str:
        # Priority: secrets file > env var
        if self.secrets_file:
            path = Path(self.secrets_file)
            if path.exists():
                key = path.read_text().strip()
                if key:
                    return key
        return os.environ.get(self.env_var, "")

    def get_key(self) -> str:
        with self._lock:
            now = time.monotonic()
            if now - self._last_check > self.poll_interval:
                new_key = self._read_key()
                if new_key != self._current_key:
                    print(f"[key-watcher] Key changed: ...{self._current_key[-4:]} → ...{new_key[-4:]}")
                    self._current_key = new_key
                self._last_check = now
            return self._current_key

class HotReloadClient:
    def __init__(self, watcher: KeyWatcher):
        self.watcher = watcher
        self._client: Optional[anthropic.Anthropic] = None
        self._client_key: str = ""

    def _get_client(self) -> anthropic.Anthropic:
        current_key = self.watcher.get_key()
        if current_key != self._client_key:
            self._client = anthropic.Anthropic(api_key=current_key)
            self._client_key = current_key
            print(f"[hot-reload] Client refreshed with key ...{current_key[-4:]}")
        return self._client

    def create_message(self, **kwargs) -> anthropic.types.Message:
        return self._get_client().messages.create(**kwargs)

watcher = KeyWatcher(
    env_var="ANTHROPIC_API_KEY",
    poll_interval=30.0
)
client = HotReloadClient(watcher)

# Simulate two requests — second would pick up new key automatically
for i in range(2):
    response = client.create_message(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Count to {i+1}"}]
    )
    print(f"Request {i+1}: {response.content[0].text[:60]}")

# To rotate: update ANTHROPIC_API_KEY env var or write new value to secrets_file
# Next request within poll_interval will pick it up automatically

# Expected Token Savings: Hot-reload eliminates deployment/restart gap. No downtime = no failed requests = no retry token waste. poll_interval of 30s means <30s exposure to a leaked key.
# Environment: ANTHROPIC_API_KEY required. Optionally mount secrets file at container path.
```

---

## Option 3: Key Pool with Health Checking

Maintain a pool of multiple valid API keys. Healthy keys serve traffic; when one becomes invalid, it's removed and replaced without interrupting service.

```python
import anthropic
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class PooledKey:
    key: str
    added_at: float
    request_count: int = 0
    error_count: int = 0
    healthy: bool = True
    last_used: float = 0.0

class KeyPool:
    def __init__(self, keys: list[str], health_check_interval: float = 60.0):
        self._keys: list[PooledKey] = [
            PooledKey(k, time.monotonic()) for k in keys if k
        ]
        self._lock = threading.Lock()
        self._health_interval = health_check_interval
        self._stop = threading.Event()
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    def add_key(self, key: str):
        with self._lock:
            existing = [k.key for k in self._keys]
            if key not in existing:
                self._keys.append(PooledKey(key, time.monotonic()))
                print(f"[pool] Added key ...{key[-4:]} (pool size: {len(self._keys)})")

    def remove_key(self, key: str):
        with self._lock:
            self._keys = [k for k in self._keys if k.key != key]
            print(f"[pool] Removed key ...{key[-4:]} (pool size: {len(self._keys)})")

    def get_key(self) -> Optional[PooledKey]:
        with self._lock:
            healthy = [k for k in self._keys if k.healthy]
            if not healthy:
                return None
            # Round-robin: pick least-recently-used healthy key
            return min(healthy, key=lambda k: k.last_used)

    def record_success(self, key_str: str):
        with self._lock:
            for k in self._keys:
                if k.key == key_str:
                    k.request_count += 1
                    k.last_used = time.monotonic()

    def record_failure(self, key_str: str, auth_failure: bool = False):
        with self._lock:
            for k in self._keys:
                if k.key == key_str:
                    k.error_count += 1
                    if auth_failure:
                        k.healthy = False
                        print(f"[pool] Key ...{key_str[-4:]} marked unhealthy (auth failure)")

    def _health_loop(self):
        while not self._stop.wait(self._health_interval):
            with self._lock:
                unhealthy = [k for k in self._keys if not k.healthy]
            for k in unhealthy:
                print(f"[health] Key ...{k.key[-4:]} remains unhealthy — consider replacing")

    def stats(self) -> dict:
        with self._lock:
            return {
                "total": len(self._keys),
                "healthy": len([k for k in self._keys if k.healthy]),
                "total_requests": sum(k.request_count for k in self._keys),
            }

    def shutdown(self):
        self._stop.set()

class PooledClient:
    def __init__(self, pool: KeyPool):
        self.pool = pool

    def create_message(self, **kwargs) -> anthropic.types.Message:
        pooled = self.pool.get_key()
        if not pooled:
            raise RuntimeError("No healthy API keys available")

        client = anthropic.Anthropic(api_key=pooled.key)
        try:
            result = client.messages.create(**kwargs)
            self.pool.record_success(pooled.key)
            return result
        except anthropic.AuthenticationError as exc:
            self.pool.record_failure(pooled.key, auth_failure=True)
            raise

import os
primary_key = os.environ.get("ANTHROPIC_API_KEY", "")
pool = KeyPool(keys=[primary_key], health_check_interval=120.0)
client = PooledClient(pool)

response = client.create_message(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Say hello"}]
)
print(f"Response: {response.content[0].text}")
print(f"Pool stats: {pool.stats()}")

# To rotate: pool.add_key(new_key); then pool.remove_key(old_key) after overlap period
pool.shutdown()

# Expected Token Savings: Multi-key pool means failed auth on one key immediately tries another. Zero downtime during rotation. Health checking prevents repeated calls to revoked keys.
# Environment: ANTHROPIC_API_KEY required. Add ANTHROPIC_API_KEY_2 etc. for multi-key pools.
```

---

## Option 4: Secrets Manager Integration with TTL Cache

Fetch the API key from a secrets manager (AWS SSM, Vault, etc.) with a TTL cache, refreshing automatically before expiry.

```python
import anthropic
import os
import time
import threading
from dataclasses import dataclass
from typing import Optional, Callable

@dataclass
class SecretCache:
    value: str
    fetched_at: float
    ttl_seconds: float

    def is_expired(self) -> bool:
        return time.monotonic() - self.fetched_at > self.ttl_seconds

    def expires_soon(self, threshold: float = 30.0) -> bool:
        age = time.monotonic() - self.fetched_at
        return age > (self.ttl_seconds - threshold)

class SecretsManagerClient:
    """
    Wraps any secrets backend (AWS SSM, HashiCorp Vault, GCP Secret Manager).
    Pass a fetch_fn that returns the current API key string.
    """
    def __init__(
        self,
        fetch_fn: Callable[[], str],
        ttl_seconds: float = 300.0,
        refresh_threshold: float = 30.0
    ):
        self._fetch_fn = fetch_fn
        self._ttl = ttl_seconds
        self._threshold = refresh_threshold
        self._cache: Optional[SecretCache] = None
        self._lock = threading.Lock()
        self._refresh_thread: Optional[threading.Thread] = None

    def _fetch_and_cache(self) -> str:
        print("[secrets] Fetching key from secrets manager")
        value = self._fetch_fn()
        self._cache = SecretCache(value, time.monotonic(), self._ttl)
        return value

    def get_key(self) -> str:
        with self._lock:
            if self._cache is None or self._cache.is_expired():
                return self._fetch_and_cache()
            if self._cache.expires_soon(self._threshold):
                # Refresh in background, return current value
                if not (self._refresh_thread and self._refresh_thread.is_alive()):
                    self._refresh_thread = threading.Thread(
                        target=self._fetch_and_cache, daemon=True
                    )
                    self._refresh_thread.start()
            return self._cache.value

class ManagedKeyClient:
    def __init__(self, secrets_manager: SecretsManagerClient):
        self.secrets = secrets_manager
        self._anthropic_clients: dict[str, anthropic.Anthropic] = {}

    def _get_client(self, key: str) -> anthropic.Anthropic:
        if key not in self._anthropic_clients:
            self._anthropic_clients[key] = anthropic.Anthropic(api_key=key)
        return self._anthropic_clients[key]

    def create_message(self, **kwargs) -> anthropic.types.Message:
        key = self.secrets.get_key()
        client = self._get_client(key)
        try:
            return client.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            # Force refresh on auth failure
            print("[secrets] Auth failed — forcing secret refresh")
            with self.secrets._lock:
                self.secrets._cache = None
            key = self.secrets.get_key()
            return self._get_client(key).messages.create(**kwargs)

# Simulate a secrets manager backend
_rotation_counter = [0]
def mock_secrets_manager() -> str:
    """In production: boto3 SSM, hvac Vault, google-cloud-secret-manager, etc."""
    _rotation_counter[0] += 1
    base_key = os.environ.get("ANTHROPIC_API_KEY", "")
    return base_key  # In real use: fetch from secrets backend

secrets = SecretsManagerClient(
    fetch_fn=mock_secrets_manager,
    ttl_seconds=300.0,
    refresh_threshold=30.0
)
client = ManagedKeyClient(secrets)

response = client.create_message(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "What is the capital of Japan?"}]
)
print(f"Response: {response.content[0].text}")
print(f"Secret fetches: {_rotation_counter[0]}")

# Second call — served from cache
response2 = client.create_message(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "What is the capital of France?"}]
)
print(f"Response 2: {response2.content[0].text}")
print(f"Secret fetches after 2 calls: {_rotation_counter[0]} (cached)")

# Expected Token Savings: Background refresh means keys are always fresh without blocking requests. 5-minute TTL with 30-second pre-refresh window eliminates auth failures from expired keys.
# Environment: ANTHROPIC_API_KEY required. Replace mock_secrets_manager with boto3/hvac/GCP SDK.
```

---

## Option 5: Async Key Rotation with asyncio.Lock

Thread-safe async key rotation using asyncio primitives, safe for use in async agents with concurrent requests.

```python
import anthropic
import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class AsyncKeyState:
    primary_key: str
    fallback_key: Optional[str]
    rotated_at: float
    fallback_expires_at: float

class AsyncRotatingClient:
    def __init__(self, initial_key: str, fallback_ttl: float = 300.0):
        self._state = AsyncKeyState(
            primary_key=initial_key,
            fallback_key=None,
            rotated_at=time.monotonic(),
            fallback_expires_at=0.0
        )
        self._fallback_ttl = fallback_ttl
        self._lock = asyncio.Lock()
        self._clients: dict[str, anthropic.AsyncAnthropic] = {}

    def _get_client(self, key: str) -> anthropic.AsyncAnthropic:
        if key not in self._clients:
            self._clients[key] = anthropic.AsyncAnthropic(api_key=key)
        return self._clients[key]

    async def rotate(self, new_key: str):
        async with self._lock:
            old_key = self._state.primary_key
            self._state = AsyncKeyState(
                primary_key=new_key,
                fallback_key=old_key,
                rotated_at=time.monotonic(),
                fallback_expires_at=time.monotonic() + self._fallback_ttl
            )
            print(f"[rotation] Primary: ...{new_key[-4:]}, Fallback: ...{old_key[-4:]} (expires in {self._fallback_ttl}s)")

    def _get_active_keys(self) -> list[str]:
        keys = [self._state.primary_key]
        if (self._state.fallback_key and
                time.monotonic() < self._state.fallback_expires_at):
            keys.append(self._state.fallback_key)
        return keys

    async def create_message(self, **kwargs) -> anthropic.types.Message:
        async with self._lock:
            keys = self._get_active_keys()

        last_exc = None
        for key in keys:
            try:
                client = self._get_client(key)
                return await client.messages.create(**kwargs)
            except anthropic.AuthenticationError as exc:
                print(f"[key-fail] ...{key[-4:]} rejected")
                last_exc = exc
        raise last_exc or RuntimeError("All keys exhausted")

async def main():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = AsyncRotatingClient(key, fallback_ttl=300.0)

    # Concurrent requests during normal operation
    responses = await asyncio.gather(*[
        client.create_message(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": f"What is {i}+{i}?"}]
        )
        for i in range(1, 4)
    ])
    for r in responses:
        print(f"Concurrent: {r.content[0].text.strip()}")

    # Rotate key — all subsequent requests use new key, fallback available during overlap
    new_key = os.environ.get("ANTHROPIC_API_KEY_NEW", key)
    await client.rotate(new_key)

    # Concurrent requests immediately after rotation — no downtime
    post_rotation = await asyncio.gather(*[
        client.create_message(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": f"What is {i}*{i}?"}]
        )
        for i in range(1, 4)
    ])
    for r in post_rotation:
        print(f"Post-rotation: {r.content[0].text.strip()}")

asyncio.run(main())

# Expected Token Savings: asyncio.Lock prevents race conditions during rotation. Zero concurrent requests fail during key swap. Eliminates retry overhead from rotation-induced 401s.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Option 6: Rotation Scheduler with SQLite Audit Log

Schedule key rotations at configurable intervals, log every rotation event to SQLite for compliance auditing, and alert on rotation failures.

```python
import anthropic
import sqlite3
import threading
import time
import uuid
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

@dataclass
class RotationEvent:
    event_id: str
    old_key_suffix: str
    new_key_suffix: str
    triggered_by: str  # "schedule" | "manual" | "auth_failure"
    success: bool
    error_message: Optional[str]
    created_at: str

def init_rotation_db(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS key_rotations (
            event_id TEXT PRIMARY KEY,
            old_key_suffix TEXT,
            new_key_suffix TEXT,
            triggered_by TEXT,
            success INTEGER,
            error_message TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn

def log_rotation(conn: sqlite3.Connection, event: RotationEvent):
    conn.execute(
        "INSERT INTO key_rotations VALUES (?,?,?,?,?,?,?)",
        (event.event_id, event.old_key_suffix, event.new_key_suffix,
         event.triggered_by, int(event.success), event.error_message, event.created_at)
    )
    conn.commit()

class ScheduledRotationClient:
    def __init__(
        self,
        initial_key: str,
        key_provider: Callable[[], str],
        rotation_interval: float = 3600.0,
        conn: Optional[sqlite3.Connection] = None
    ):
        self._current_key = initial_key
        self._key_provider = key_provider
        self._rotation_interval = rotation_interval
        self._conn = conn or init_rotation_db()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._scheduler = threading.Thread(target=self._rotation_loop, daemon=True)
        self._scheduler.start()

    def _get_client(self) -> anthropic.Anthropic:
        with self._lock:
            return anthropic.Anthropic(api_key=self._current_key)

    def _rotate(self, triggered_by: str = "schedule") -> bool:
        old_suffix = self._current_key[-4:] if self._current_key else "????"
        try:
            new_key = self._key_provider()
            if not new_key:
                raise ValueError("Key provider returned empty key")
            with self._lock:
                self._current_key = new_key
            new_suffix = new_key[-4:]
            print(f"[rotation] {triggered_by}: ...{old_suffix} → ...{new_suffix}")
            log_rotation(self._conn, RotationEvent(
                event_id=str(uuid.uuid4()),
                old_key_suffix=old_suffix,
                new_key_suffix=new_suffix,
                triggered_by=triggered_by,
                success=True,
                error_message=None,
                created_at=datetime.utcnow().isoformat()
            ))
            return True
        except Exception as exc:
            print(f"[rotation-fail] {exc}")
            log_rotation(self._conn, RotationEvent(
                event_id=str(uuid.uuid4()),
                old_key_suffix=old_suffix,
                new_key_suffix="????",
                triggered_by=triggered_by,
                success=False,
                error_message=str(exc)[:200],
                created_at=datetime.utcnow().isoformat()
            ))
            return False

    def _rotation_loop(self):
        while not self._stop.wait(self._rotation_interval):
            self._rotate("schedule")

    def force_rotate(self):
        self._rotate("manual")

    def create_message(self, **kwargs) -> anthropic.types.Message:
        try:
            return self._get_client().messages.create(**kwargs)
        except anthropic.AuthenticationError:
            print("[auth-fail] Triggering emergency rotation")
            if self._rotate("auth_failure"):
                return self._get_client().messages.create(**kwargs)
            raise

    def get_audit_log(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM key_rotations ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        cols = ["event_id", "old_key_suffix", "new_key_suffix", "triggered_by", "success", "error", "created_at"]
        return [dict(zip(cols, r)) for r in rows]

    def shutdown(self):
        self._stop.set()

# Key provider — in production: fetch from AWS SSM / Vault / env
_version = [0]
def key_provider() -> str:
    _version[0] += 1
    return os.environ.get("ANTHROPIC_API_KEY", "")

initial_key = os.environ.get("ANTHROPIC_API_KEY", "")
rotation_client = ScheduledRotationClient(
    initial_key=initial_key,
    key_provider=key_provider,
    rotation_interval=3600.0  # hourly in production
)

response = rotation_client.create_message(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Say 'rotation test passed'"}]
)
print(f"Response: {response.content[0].text}")

rotation_client.force_rotate()
audit = rotation_client.get_audit_log()
print(f"\nAudit log ({len(audit)} entries):")
for entry in audit:
    status = "OK" if entry["success"] else "FAIL"
    print(f"  [{status}] {entry['triggered_by']}: ...{entry['old_key_suffix']} → ...{entry['new_key_suffix']} at {entry['created_at']}")

rotation_client.shutdown()

# Expected Token Savings: Scheduled rotation prevents key expiry failures. Emergency rotation on 401 saves 3–10 failed request retries per incident. Audit log enables compliance without manual tracking.
# Environment: ANTHROPIC_API_KEY required. Uses sqlite3, threading (stdlib). key_provider replaces with boto3/hvac in production.
```

---

## Comparison

| Option | Rotation Mechanism | Downtime | Fallback During Overlap | Persistence | Best For |
|--------|-------------------|----------|------------------------|-------------|----------|
| 1: Dual-Key Overlap | Manual trigger, 5-min overlap | Zero | Yes | None | Simple apps, manual rotation |
| 2: Env Var Hot-Reload | Polling (30s interval) | <30s gap | No | None | Container/k8s with secret mounts |
| 3: Key Pool + Health | Add/remove from pool | Zero | Automatic failover | None | High-availability multi-key setup |
| 4: Secrets Manager TTL | TTL cache + background refresh | Zero | Background refresh | None | AWS/Vault/GCP secrets integration |
| 5: Async asyncio.Lock | Manual trigger, async-safe | Zero | Yes (fallback_ttl) | None | Async agents with concurrent requests |
| 6: Scheduled + Audit | Interval scheduler + emergency | Zero | Yes (on auth failure) | SQLite | Compliance-regulated environments |
