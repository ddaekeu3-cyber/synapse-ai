---
title: "Agent Doesn't Implement API Key Rotation Without Downtime"
slug: agent-doesnt-implement-api-key-rotation-without-downtime
category: security
tags: [security, api-key, rotation, secrets, zero-downtime, anthropic-sdk]
description: >
  The agent hard-codes or statically loads its Anthropic API key at startup,
  making it impossible to rotate compromised credentials without restarting
  every instance. Zero-downtime rotation requires the agent to support multiple
  concurrent keys, gracefully drain in-flight requests using the old key, and
  reload secrets from a vault without service interruption.
symptoms:
  - Rotating a compromised key requires a full deployment or pod restart
  - In-flight requests fail when an old key is revoked before rotation completes
  - No audit trail of which key version served each request
  - Dev/staging accidentally uses production key because rotation is manual
related_solutions:
  - agent-doesnt-implement-input-size-limits-and-payload-validation
  - agent-doesnt-implement-distributed-trace-propagation
---

## Problem

API keys expire, leak, and must be rotated. Without a rotation strategy the
only safe response to a leaked key is emergency downtime: revoke → redeploy →
resume. A proper zero-downtime rotation strategy supports two simultaneous
keys (old + new) during the transition window, automatically drains old-key
requests, and hot-reloads the new key from a secret store without restarting
any service instances.

---

## Solution 1 — Dual-Key Switcher with Atomic Swap

Hold two `AsyncAnthropic` client instances (primary and secondary). On rotation,
the secondary becomes the new primary atomically via a `threading.Lock`-protected
swap. In-flight calls using the old primary finish normally; new calls use the
new key immediately.

```python
import anthropic
import asyncio
import os
import threading
from dataclasses import dataclass, field


@dataclass
class DualKeyClient:
    """Holds primary + optional secondary key; swaps atomically on rotation."""
    _primary:   anthropic.AsyncAnthropic = field(init=False)
    _secondary: anthropic.AsyncAnthropic | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self):
        key = os.environ["ANTHROPIC_API_KEY"]
        self._primary = anthropic.AsyncAnthropic(api_key=key)

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        with self._lock:
            return self._primary

    def stage_new_key(self, new_key: str) -> None:
        """Load new key as secondary — does not affect in-flight requests yet."""
        with self._lock:
            self._secondary = anthropic.AsyncAnthropic(api_key=new_key)
        print("[rotation] new key staged")

    def commit_rotation(self) -> None:
        """Atomically promote secondary to primary."""
        with self._lock:
            if self._secondary is None:
                raise RuntimeError("No secondary key staged")
            self._primary = self._secondary
            self._secondary = None
        print("[rotation] new key is now primary")

    def rollback(self) -> None:
        with self._lock:
            self._secondary = None
        print("[rotation] rollback — staging cleared")

    async def create_message(self, messages: list, model: str = "claude-sonnet-4-6") -> str:
        resp = await self.client.messages.create(
            model=model, max_tokens=512, messages=messages
        )
        return resp.content[0].text


_client = DualKeyClient()


async def demo_rotation():
    # Normal operation
    reply = await _client.create_message(
        [{"role": "user", "content": "What is a nonce in cryptography?"}]
    )
    print(f"Before rotation: {reply[:60]}")

    # Stage new key (validate it works before committing)
    new_key = os.environ.get("ANTHROPIC_API_KEY_NEW", os.environ["ANTHROPIC_API_KEY"])
    _client.stage_new_key(new_key)

    # Validate new key with a test call before committing
    test_client = anthropic.AsyncAnthropic(api_key=new_key)
    try:
        await test_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            messages=[{"role": "user", "content": "ping"}],
        )
        _client.commit_rotation()
    except anthropic.AuthenticationError:
        _client.rollback()
        raise RuntimeError("New key validation failed — rotation aborted")

    # New key is now active
    reply2 = await _client.create_message(
        [{"role": "user", "content": "What is HMAC?"}]
    )
    print(f"After rotation:  {reply2[:60]}")


asyncio.run(demo_rotation())
```

---

## Solution 2 — Periodic Secret Reload from Environment / Vault

Poll a secret source (environment variable, file, or Vault HTTP API) on a
configurable interval and hot-swap the client when the key changes. No restart
required; rotation happens within one poll interval.

```python
import anthropic
import asyncio
import hashlib
import os
import time
from pathlib import Path


class HotReloadClient:
    """Reloads Anthropic API key from a secret source on a timer."""

    def __init__(
        self,
        source: str = "env",         # "env" | "file:<path>" | "vault:<url>"
        poll_interval: float = 30.0,  # seconds between checks
    ):
        self._source = source
        self._poll_interval = poll_interval
        self._client: anthropic.AsyncAnthropic | None = None
        self._key_hash: str = ""
        self._lock = asyncio.Lock()
        self._reload_task: asyncio.Task | None = None

    def _fetch_key(self) -> str:
        if self._source == "env":
            return os.environ["ANTHROPIC_API_KEY"]
        if self._source.startswith("file:"):
            path = self._source[5:]
            return Path(path).read_text().strip()
        if self._source.startswith("vault:"):
            # In production: call Vault HTTP API
            # import httpx; resp = httpx.get(url, headers={"X-Vault-Token": token})
            raise NotImplementedError("Vault source requires httpx + Vault token")
        raise ValueError(f"Unknown source: {self._source}")

    def _key_fingerprint(self, key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    async def _maybe_reload(self) -> bool:
        key = self._fetch_key()
        fp  = self._key_fingerprint(key)
        if fp == self._key_hash:
            return False
        async with self._lock:
            self._client  = anthropic.AsyncAnthropic(api_key=key)
            old_fp        = self._key_hash
            self._key_hash = fp
        print(f"[hot-reload] key rotated  old={old_fp}  new={fp}")
        return True

    async def start(self) -> None:
        await self._maybe_reload()
        self._reload_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._maybe_reload()
            except Exception as e:
                print(f"[hot-reload] reload error: {e}")

    async def stop(self) -> None:
        if self._reload_task:
            self._reload_task.cancel()

    async def create_message(self, messages: list, model: str = "claude-sonnet-4-6") -> str:
        async with self._lock:
            client = self._client
        resp = await client.messages.create(
            model=model, max_tokens=512, messages=messages
        )
        return resp.content[0].text


async def demo_hot_reload():
    manager = HotReloadClient(source="env", poll_interval=5.0)
    await manager.start()

    for i in range(2):
        reply = await manager.create_message(
            [{"role": "user", "content": f"Question {i}: define salting in password hashing."}]
        )
        print(f"[{i}] {reply[:60]}")
        await asyncio.sleep(1)

    await manager.stop()


asyncio.run(demo_hot_reload())
```

---

## Solution 3 — Key Version Tagging for Audit Trails

Tag every API call with the key version (fingerprint) so you can correlate
post-incident which requests used a compromised key and when the rotation took
effect.

```python
import anthropic
import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field


@dataclass
class VersionedKeyStore:
    _versions: list[dict] = field(default_factory=list)
    _current_idx: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def load_initial(self, key: str) -> None:
        fp = self._fingerprint(key)
        async with self._lock:
            self._versions.append({
                "key":       key,
                "fp":        fp,
                "version":   1,
                "loaded_at": time.time(),
                "revoked_at": None,
            })
            self._current_idx = 0

    async def rotate(self, new_key: str) -> None:
        fp = self._fingerprint(new_key)
        async with self._lock:
            # Mark current as revoked
            self._versions[self._current_idx]["revoked_at"] = time.time()
            # Add new version
            new_ver = self._versions[-1]["version"] + 1
            self._versions.append({
                "key":       new_key,
                "fp":        fp,
                "version":   new_ver,
                "loaded_at": time.time(),
                "revoked_at": None,
            })
            self._current_idx = len(self._versions) - 1
        print(f"[key-store] rotated to version={new_ver}  fp={fp}")

    async def current(self) -> tuple[str, str, int]:
        """Returns (key, fingerprint, version)."""
        async with self._lock:
            v = self._versions[self._current_idx]
            return v["key"], v["fp"], v["version"]

    def _fingerprint(self, key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def audit_log(self) -> list[dict]:
        return [
            {k: v for k, v in ver.items() if k != "key"}
            for ver in self._versions
        ]


_store = VersionedKeyStore()


async def versioned_create(messages: list, model: str = "claude-sonnet-4-6") -> dict:
    key, fp, version = await _store.current()
    client = anthropic.AsyncAnthropic(api_key=key)
    resp = await client.messages.create(
        model=model, max_tokens=256, messages=messages
    )
    record = {
        "key_version":  version,
        "key_fp":       fp,
        "model":        model,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "ts":           time.time(),
    }
    print(f"[audit] {json.dumps(record)}")
    return {"text": resp.content[0].text, **record}


async def demo_versioned():
    initial_key = os.environ["ANTHROPIC_API_KEY"]
    await _store.load_initial(initial_key)

    r1 = await versioned_create([{"role": "user", "content": "What is certificate pinning?"}])
    print(f"v{r1['key_version']} response: {r1['text'][:50]}")

    # Simulate rotation
    await _store.rotate(initial_key)   # same key in demo; in prod: new key
    r2 = await versioned_create([{"role": "user", "content": "What is PKCE?"}])
    print(f"v{r2['key_version']} response: {r2['text'][:50]}")

    print("\nAudit log:")
    for entry in _store.audit_log():
        print(" ", entry)


asyncio.run(demo_versioned())
```

---

## Solution 4 — Graceful Drain During Rotation

Track in-flight requests per key version using an `asyncio.Semaphore`-based
drain counter. The old key version is not revoked until all its in-flight
requests complete, preventing mid-request authentication failures.

```python
import anthropic
import asyncio
import os
import time
from dataclasses import dataclass, field


@dataclass
class DrainableKey:
    key:        str
    version:    int
    client:     anthropic.AsyncAnthropic = field(init=False)
    in_flight:  int = 0
    draining:   bool = False
    _lock:      asyncio.Lock = field(default_factory=asyncio.Lock)
    _drained:   asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self):
        self.client = anthropic.AsyncAnthropic(api_key=self.key)
        if self.in_flight == 0:
            self._drained.set()

    async def acquire(self) -> bool:
        """Returns False if key is draining and should not accept new requests."""
        async with self._lock:
            if self.draining:
                return False
            self.in_flight += 1
            self._drained.clear()
            return True

    async def release(self) -> None:
        async with self._lock:
            self.in_flight -= 1
            if self.in_flight == 0:
                self._drained.set()

    async def begin_drain(self) -> None:
        async with self._lock:
            self.draining = True
        print(f"[drain] v{self.version}: draining {self.in_flight} in-flight requests")
        await self._drained.wait()
        print(f"[drain] v{self.version}: drained — safe to revoke")


class ZeroDowntimeRotator:
    def __init__(self):
        key = os.environ["ANTHROPIC_API_KEY"]
        self._active = DrainableKey(key=key, version=1)
        self._next:   DrainableKey | None = None
        self._lock = asyncio.Lock()

    async def create_message(self, messages: list, model: str = "claude-sonnet-4-6") -> str:
        async with self._lock:
            drainable = self._next if self._next else self._active
            ok = await drainable.acquire()
            if not ok and self._active:
                ok = await self._active.acquire()
                drainable = self._active

        try:
            resp = await drainable.client.messages.create(
                model=model, max_tokens=512, messages=messages
            )
            return resp.content[0].text
        finally:
            await drainable.release()

    async def rotate(self, new_key: str) -> None:
        new_version = self._active.version + 1
        self._next = DrainableKey(key=new_key, version=new_version)
        old = self._active
        async with self._lock:
            self._active = self._next
            self._next   = None
        print(f"[rotator] v{new_version} is now active")
        # Drain old key — wait for in-flight requests to complete
        await old.begin_drain()


async def demo_drain():
    rotator = ZeroDowntimeRotator()

    async def long_request(i: int):
        await asyncio.sleep(i * 0.1)  # stagger starts
        result = await rotator.create_message(
            [{"role": "user", "content": f"Request {i}: define TLS 1.3."}]
        )
        print(f"[req-{i}] {result[:40]}")

    # Start 3 requests, rotate during their execution
    tasks = [asyncio.create_task(long_request(i)) for i in range(3)]
    await asyncio.sleep(0.15)
    await rotator.rotate(os.environ["ANTHROPIC_API_KEY"])  # same key in demo
    await asyncio.gather(*tasks)


asyncio.run(demo_drain())
```

---

## Solution 5 — Multi-Environment Key Resolver

Resolve the correct API key based on the runtime environment (dev / staging /
prod) and request context (tenant, region). Prevents dev keys leaking into
production and enables per-tenant key isolation.

```python
import anthropic
import asyncio
import os
from enum import Enum


class Env(str, Enum):
    DEV     = "dev"
    STAGING = "staging"
    PROD    = "prod"


class MultiEnvKeyResolver:
    """
    Resolves API key based on environment + optional tenant override.
    In production, each environment reads from its own secret manager path.
    """

    def __init__(self):
        # In prod: replace with Vault / AWS SM / GCP Secret Manager lookups
        self._keys: dict[str, str] = {
            Env.DEV:     os.environ.get("ANTHROPIC_API_KEY_DEV",     os.environ["ANTHROPIC_API_KEY"]),
            Env.STAGING: os.environ.get("ANTHROPIC_API_KEY_STAGING", os.environ["ANTHROPIC_API_KEY"]),
            Env.PROD:    os.environ.get("ANTHROPIC_API_KEY_PROD",    os.environ["ANTHROPIC_API_KEY"]),
        }
        self._tenant_overrides: dict[str, str] = {}
        self._clients: dict[str, anthropic.AsyncAnthropic] = {}

    def register_tenant_key(self, tenant_id: str, api_key: str) -> None:
        """Per-tenant key for BYOK (bring-your-own-key) deployments."""
        self._tenant_overrides[tenant_id] = api_key
        self._clients.pop(f"tenant:{tenant_id}", None)

    def _get_key(self, env: Env, tenant_id: str | None) -> str:
        if tenant_id and tenant_id in self._tenant_overrides:
            return self._tenant_overrides[tenant_id]
        return self._keys[env]

    def _client_cache_key(self, env: Env, tenant_id: str | None) -> str:
        return f"tenant:{tenant_id}" if tenant_id in self._tenant_overrides else env.value

    def get_client(self, env: Env = Env.PROD, tenant_id: str | None = None) -> anthropic.AsyncAnthropic:
        ck = self._client_cache_key(env, tenant_id)
        if ck not in self._clients:
            api_key = self._get_key(env, tenant_id)
            self._clients[ck] = anthropic.AsyncAnthropic(api_key=api_key)
        return self._clients[ck]

    def rotate_env_key(self, env: Env, new_key: str) -> None:
        self._keys[env] = new_key
        self._clients.pop(env.value, None)
        print(f"[resolver] {env} key rotated — next call gets new client")

    async def create_message(
        self,
        messages: list,
        env: Env = Env.PROD,
        tenant_id: str | None = None,
        model: str = "claude-sonnet-4-6",
    ) -> str:
        client = self.get_client(env, tenant_id)
        resp = await client.messages.create(
            model=model, max_tokens=256, messages=messages
        )
        return resp.content[0].text


async def demo_multi_env():
    resolver = MultiEnvKeyResolver()

    prod_reply = await resolver.create_message(
        [{"role": "user", "content": "What is mTLS?"}],
        env=Env.PROD,
    )
    print(f"[prod]  {prod_reply[:60]}")

    # Register tenant BYOK
    resolver.register_tenant_key("acme", os.environ["ANTHROPIC_API_KEY"])
    tenant_reply = await resolver.create_message(
        [{"role": "user", "content": "What is OAuth2 PKCE?"}],
        env=Env.PROD,
        tenant_id="acme",
    )
    print(f"[acme]  {tenant_reply[:60]}")

    # Rotate prod key without restart
    resolver.rotate_env_key(Env.PROD, os.environ["ANTHROPIC_API_KEY"])
    print("[resolver] prod key rotated — no restart needed")


asyncio.run(demo_multi_env())
```

---

## Solution 6 — Canary Key Rotation with Traffic Splitting

Direct a small percentage of traffic to the new key first. If error rates stay
low for a configurable window, automatically promote the new key to 100 %;
otherwise roll back automatically.

```python
import anthropic
import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from collections import deque


@dataclass
class KeySlot:
    key:     str
    version: int
    weight:  float = 1.0      # 0.0 – 1.0 traffic share
    errors:  deque = field(default_factory=lambda: deque(maxlen=50))
    calls:   int   = 0

    @property
    def error_rate(self) -> float:
        if not self.errors:
            return 0.0
        return sum(self.errors) / len(self.errors)

    def record(self, ok: bool) -> None:
        self.errors.append(0 if ok else 1)
        self.calls += 1


class CanaryRotator:
    def __init__(self, initial_key: str):
        self._primary = KeySlot(key=initial_key, version=1, weight=1.0)
        self._canary:  KeySlot | None = None
        self._lock = asyncio.Lock()
        self._canary_task: asyncio.Task | None = None

    def _choose_slot(self) -> KeySlot:
        if self._canary is None or self._canary.weight == 0:
            return self._primary
        if random.random() < self._canary.weight:
            return self._canary
        return self._primary

    async def create_message(self, messages: list, model: str = "claude-sonnet-4-6") -> str:
        async with self._lock:
            slot = self._choose_slot()

        client = anthropic.AsyncAnthropic(api_key=slot.key)
        try:
            resp = await client.messages.create(
                model=model, max_tokens=256, messages=messages
            )
            async with self._lock:
                slot.record(ok=True)
            return resp.content[0].text
        except Exception as e:
            async with self._lock:
                slot.record(ok=False)
            raise

    async def start_canary(
        self,
        new_key: str,
        initial_weight: float = 0.05,
        ramp_interval_s: float = 30.0,
        error_threshold: float = 0.05,
    ) -> None:
        async with self._lock:
            self._canary = KeySlot(key=new_key, version=self._primary.version + 1,
                                   weight=initial_weight)
        print(f"[canary] v{self._canary.version} started at {initial_weight*100:.0f}% traffic")
        self._canary_task = asyncio.create_task(
            self._ramp(ramp_interval_s, error_threshold)
        )

    async def _ramp(self, interval_s: float, error_threshold: float) -> None:
        ramp_steps = [0.05, 0.10, 0.25, 0.50, 1.0]
        for target_weight in ramp_steps[1:]:
            await asyncio.sleep(interval_s)
            async with self._lock:
                if self._canary is None:
                    return
                er = self._canary.error_rate
                if er > error_threshold:
                    print(f"[canary] ROLLBACK — error_rate={er:.2%}")
                    self._canary = None
                    return
                self._canary.weight = target_weight
                print(f"[canary] ramp → {target_weight*100:.0f}%  error_rate={er:.2%}")

        # Full promotion
        async with self._lock:
            if self._canary:
                print(f"[canary] PROMOTED v{self._canary.version} to 100%")
                self._primary = self._canary
                self._primary.weight = 1.0
                self._canary = None


async def demo_canary():
    rotator = CanaryRotator(initial_key=os.environ["ANTHROPIC_API_KEY"])

    # Fire some baseline traffic
    for i in range(3):
        r = await rotator.create_message(
            [{"role": "user", "content": f"Q{i}: what is JWT?"}]
        )
        print(f"[baseline-{i}] {r[:40]}")

    # Start canary with same key (demo); in prod use new_key
    await rotator.start_canary(
        new_key=os.environ["ANTHROPIC_API_KEY"],
        initial_weight=0.50,
        ramp_interval_s=2.0,
    )

    for i in range(4):
        await asyncio.sleep(1)
        r = await rotator.create_message(
            [{"role": "user", "content": f"Canary Q{i}: what is RBAC?"}]
        )
        print(f"[canary-{i}] {r[:40]}")


asyncio.run(demo_canary())
```

---

## Comparison

| Approach | Rotation downtime | Audit trail | Multi-env | Canary safety | Complexity |
|---|---|---|---|---|---|
| Dual-key atomic swap | Zero | No | No | No | Low |
| Periodic secret reload | < poll interval | No | No | No | Low |
| Key version tagging | Zero | Yes | No | No | Medium |
| Graceful drain | Zero (waits for in-flight) | No | No | No | Medium |
| Multi-env resolver | Zero | No | Yes | No | Medium |
| Canary traffic split | Zero | No | No | Yes — auto-rollback | High |

**Rule of thumb:**
- Small service → dual-key atomic swap (Solution 1) + hot-reload (Solution 2)
- Compliance requirement → add key version tagging (Solution 3) for audit trail
- High-traffic production → canary rotation (Solution 6) to catch bad keys before full roll-out
- Multi-tenant SaaS → multi-env resolver (Solution 5) with per-tenant BYOK support
