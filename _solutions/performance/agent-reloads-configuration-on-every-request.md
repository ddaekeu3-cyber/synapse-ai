---
layout: solution
title: "Agent Reloads Configuration on Every Request"
category: performance
description: "Agent reads config files, environment variables, or remote settings on every API call instead of caching them at startup, adding 10-200ms of I/O overhead to every request."
tags: [performance, configuration, caching, startup, efficiency, singleton]
---

## Symptom

Every API call triggers a file read of `config.yaml`, a subprocess call to fetch secrets from Vault, or repeated `os.environ` dictionary reconstructions. Under load, strace shows hundreds of `open()` calls per second on the same config file. Response latency has a 20-50ms floor even for trivial requests because every call pays the I/O cost. Profiling reveals that `load_config()` appears in the top 5 functions by cumulative time.

## Root Cause

Configuration loading is placed inside the request handler or the function that calls the LLM, so it runs on every invocation. This is often unintentional — the developer copied the config load from a one-off script where it didn't matter. In production, config rarely changes between requests, but the I/O cost (disk read, YAML parse, env reconstruction, or network round-trip to a secrets manager) is paid on every call. Python's `os.getenv()` is fast, but rebuilding a full config dataclass from it is not.

## Fix

### Option 1 — Module-level singleton: load once at import time

```python
import os
import time
import anthropic

# WRONG — config reconstructed on every call
def ask_bad(question: str) -> str:
    api_key    = os.getenv("ANTHROPIC_API_KEY", "")
    model      = os.getenv("AGENT_MODEL", "claude-haiku-4-5-20251001")
    max_tokens = int(os.getenv("AGENT_MAX_TOKENS", "256"))
    timeout    = int(os.getenv("AGENT_TIMEOUT", "30"))
    client     = anthropic.Anthropic(api_key=api_key, timeout=timeout)   # new each call
    response   = client.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

# CORRECT — config and client built once at module load
_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
_MODEL      = os.getenv("AGENT_MODEL", "claude-haiku-4-5-20251001")
_MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "256"))
_TIMEOUT    = int(os.getenv("AGENT_TIMEOUT", "30"))
_CLIENT     = anthropic.Anthropic(api_key=_API_KEY, timeout=_TIMEOUT)

def ask_good(question: str) -> str:
    response = _CLIENT.messages.create(
        model=_MODEL, max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

questions = ["What is a lambda?", "What is a closure?", "What is a decorator?"]

print("Per-call config (bad):")
t0 = time.perf_counter()
for q in questions:
    ask_bad(q)
bad_ms = (time.perf_counter() - t0) * 1000
print(f"  {bad_ms:.0f}ms total ({bad_ms/len(questions):.0f}ms/call avg)")

print("Module-level singleton (good):")
t0 = time.perf_counter()
for q in questions:
    ask_good(q)
good_ms = (time.perf_counter() - t0) * 1000
print(f"  {good_ms:.0f}ms total ({good_ms/len(questions):.0f}ms/call avg)")
print(f"  Speedup: {bad_ms/good_ms:.1f}x")
```

**Expected Token Savings:** No token reduction; eliminates 10-50ms of env-read + client-construction overhead per call — for 1,000 calls/min, this saves ~30 CPU-seconds of unnecessary work per minute.
**Environment:** All agents; module-level singletons are the simplest and most impactful config-caching pattern and require no additional dependencies.

---

### Option 2 — `functools.lru_cache` to memoize config loading

```python
import os
import json
import time
import functools
import anthropic

# Simulate a config file
CONFIG_PATH = "/tmp/agent_config.json"
with open(CONFIG_PATH, "w") as f:
    json.dump({
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 128,
        "temperature": 0,
        "system":     "You are a concise assistant.",
    }, f)

@functools.lru_cache(maxsize=1)
def load_config(path: str = CONFIG_PATH) -> dict:
    """Load config once; subsequent calls return the cached result."""
    print(f"  [config] reading {path}")
    with open(path) as f:
        return json.load(f)

@functools.lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    """Build Anthropic client once; cache it."""
    print("  [client] creating Anthropic client")
    return anthropic.Anthropic()

def ask(question: str) -> str:
    cfg    = load_config()                # cached after first call
    client = get_client()                 # cached after first call
    response = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        system=cfg["system"],
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

questions = ["What is memoisation?", "What is a pure function?", "What is referential transparency?"]

print("First call triggers config + client load:")
ask(questions[0])

print(f"\nCache info after 1st call: {load_config.cache_info()}")

print("\nSubsequent calls use cache (no [config]/[client] lines):")
t0 = time.perf_counter()
for q in questions[1:]:
    ask(q)
elapsed = (time.perf_counter() - t0) * 1000
print(f"  {elapsed:.0f}ms for {len(questions)-1} calls (no config I/O)")

# Force cache invalidation when config changes
def reload_config() -> None:
    load_config.cache_clear()
    get_client.cache_clear()
    print("  [config] cache cleared — will reload on next call")

reload_config()
```

**Expected Token Savings:** `lru_cache` adds zero overhead after the first call; config file reads (even fast ones: ~1ms) accumulate to seconds of wasted I/O at 1,000+ calls/min. Cache also survives across invocations within the same process.
**Environment:** Single-process agents and scripts; `lru_cache` requires no infrastructure and is the idiomatic Python solution for memoising expensive pure functions.

---

### Option 3 — Dataclass config with lazy initialisation

```python
import os
import time
import dataclasses
import anthropic

@dataclasses.dataclass
class AgentConfig:
    model:      str   = dataclasses.field(default_factory=lambda: os.getenv("AGENT_MODEL", "claude-haiku-4-5-20251001"))
    max_tokens: int   = dataclasses.field(default_factory=lambda: int(os.getenv("AGENT_MAX_TOKENS", "128")))
    system:     str   = dataclasses.field(default_factory=lambda: os.getenv("AGENT_SYSTEM", "Answer concisely."))
    timeout:    float = dataclasses.field(default_factory=lambda: float(os.getenv("AGENT_TIMEOUT", "30")))

    # Lazy fields — not set at construction time
    _client: anthropic.Anthropic | None = dataclasses.field(default=None, init=False, repr=False)

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            print("  [config] creating Anthropic client (lazy init)")
            self._client = anthropic.Anthropic(timeout=self.timeout)
        return self._client

# Single global config instance
_CONFIG = AgentConfig()

def ask(question: str) -> str:
    cfg = _CONFIG                   # O(1) reference — no I/O
    response = cfg.client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        system=cfg.system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

questions = [
    "What is a dataclass?",
    "What is lazy evaluation?",
    "What is a property in Python?",
]

print("Calling with dataclass-based lazy config:")
for q in questions:
    t0 = time.perf_counter()
    r  = ask(q)
    ms = (time.perf_counter() - t0) * 1000
    print(f"  [{ms:.0f}ms] {q}: {r.strip()[:60]}")

print(f"\nConfig: model={_CONFIG.model} max_tokens={_CONFIG.max_tokens}")
```

**Expected Token Savings:** Dataclass config separates config concerns from request handling; lazy client init means the expensive SDK constructor runs only when the first real request arrives, not at import time (useful for testing where the client should not be created).
**Environment:** Agents with complex configuration objects; dataclasses provide IDE auto-complete, type safety, and clean `repr()` for debugging without any third-party dependency.

---

### Option 4 — Environment variable cache with TTL for dynamic secrets

```python
import os
import time
import threading
import anthropic

class SecretCache:
    """
    Caches secrets with a TTL so they refresh periodically
    without blocking every request with a secrets-manager round-trip.
    """

    def __init__(self, ttl_seconds: float = 300.0):
        self._cache:      dict[str, str]  = {}
        self._expires_at: dict[str, float] = {}
        self._lock        = threading.Lock()
        self._ttl         = ttl_seconds

    def get(self, key: str, default: str = "") -> str:
        now = time.monotonic()
        with self._lock:
            if key in self._cache and now < self._expires_at[key]:
                return self._cache[key]
            # Reload from env (in prod: fetch from Vault/SSM/etc.)
            value = os.getenv(key, default)
            print(f"  [cache] refreshed {key!r} (ttl={self._ttl}s)")
            self._cache[key]      = value
            self._expires_at[key] = now + self._ttl
            return value

_SECRETS = SecretCache(ttl_seconds=300.0)

def get_client() -> anthropic.Anthropic:
    api_key = _SECRETS.get("ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=api_key or None)   # None → SDK uses env

# Build client once using cached secret
_CLIENT = get_client()

def ask(question: str) -> str:
    model      = _SECRETS.get("AGENT_MODEL", "claude-haiku-4-5-20251001")
    max_tokens = int(_SECRETS.get("AGENT_MAX_TOKENS", "128"))
    response   = _CLIENT.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

questions = ["What is a TTL?", "What is a secrets manager?"]
for q in questions:
    r = ask(q)
    print(f"Q: {q}")
    print(f"A: {r.strip()[:80]}\n")

# Simulate time passing — next request after TTL would refresh
print("(In production, secrets rotate every 300s without restarting the agent)")
```

**Expected Token Savings:** TTL cache serves 99.9% of requests from memory while still picking up rotated secrets within the TTL window; avoids the latency of a Vault/SSM round-trip (~50-200ms) on every request while staying secure.
**Environment:** Production agents with rotating secrets (API keys, database passwords); TTL-based cache is the industry-standard pattern for balancing security (fresh secrets) with performance (no per-request round-trip).

---

### Option 5 — Config watcher with hot-reload (no restart required)

```python
import os
import json
import time
import threading
import anthropic

CONFIG_PATH = "/tmp/hot_reload_config.json"

# Write initial config
with open(CONFIG_PATH, "w") as f:
    json.dump({"model": "claude-haiku-4-5-20251001", "max_tokens": 64, "system": "Answer briefly."}, f)

class HotReloadConfig:
    """Watches a config file and reloads it when modified."""

    def __init__(self, path: str, poll_interval: float = 5.0):
        self._path      = path
        self._lock      = threading.RLock()
        self._cfg: dict = {}
        self._mtime     = 0.0
        self._poll      = poll_interval
        self._reload()                          # initial load
        self._watcher   = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _reload(self) -> None:
        try:
            mtime = os.path.getmtime(self._path)
            if mtime == self._mtime:
                return
            with open(self._path) as f:
                new_cfg = json.load(f)
            with self._lock:
                self._cfg   = new_cfg
                self._mtime = mtime
            print(f"  [config] reloaded {self._path} (mtime changed)")
        except Exception as e:
            print(f"  [config] reload error: {e}")

    def _watch(self) -> None:
        while True:
            time.sleep(self._poll)
            self._reload()

    def __getitem__(self, key: str):
        with self._lock:
            return self._cfg[key]

    def get(self, key: str, default=None):
        with self._lock:
            return self._cfg.get(key, default)

_CONFIG = HotReloadConfig(CONFIG_PATH, poll_interval=5.0)
_CLIENT = anthropic.Anthropic()

def ask(question: str) -> str:
    response = _CLIENT.messages.create(
        model=_CONFIG["model"],
        max_tokens=_CONFIG["max_tokens"],
        system=_CONFIG.get("system", ""),
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

questions = ["What is hot-reloading?", "What is a config watcher?"]
for q in questions:
    r = ask(q)
    print(f"Q: {q}\nA: {r.strip()[:100]}\n")

# Simulate config update — agent picks it up without restart
time.sleep(0.1)
with open(CONFIG_PATH, "w") as f:
    json.dump({"model": "claude-haiku-4-5-20251001", "max_tokens": 128, "system": "Answer in detail."}, f)
print("Config updated on disk — watcher will reload within 5s, no restart needed")
```

**Expected Token Savings:** Hot-reload serves every request from memory (0ms config I/O) while allowing ops teams to tune `max_tokens`, system prompts, and model tiers without restarting the agent process — eliminates restart-related downtime for config changes.
**Environment:** Long-running production agents (Docker containers, EC2 instances) where restarting to apply a config change is disruptive; hot-reload enables zero-downtime config management.

---

### Option 6 — Async config with `asyncio.Lock` for coroutine-safe initialisation

```python
import asyncio
import os
import time
import anthropic

class AsyncConfig:
    """Thread- and coroutine-safe lazy config with asyncio.Lock."""

    def __init__(self):
        self._lock:   asyncio.Lock | None       = None   # created inside event loop
        self._client: anthropic.AsyncAnthropic | None = None
        self._model:  str  = ""
        self._tokens: int  = 0
        self._ready:  bool = False

    async def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def ensure_ready(self) -> None:
        lock = await self._ensure_lock()
        async with lock:
            if self._ready:
                return
            # Simulate async config load (e.g., fetch from remote config service)
            await asyncio.sleep(0.01)   # 10ms config fetch
            self._model  = os.getenv("AGENT_MODEL", "claude-haiku-4-5-20251001")
            self._tokens = int(os.getenv("AGENT_MAX_TOKENS", "128"))
            self._client = anthropic.AsyncAnthropic()
            self._ready  = True
            print("  [config] async config loaded (once)")

    @property
    def model(self) -> str:
        return self._model

    @property
    def max_tokens(self) -> int:
        return self._tokens

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        assert self._client is not None, "Config not ready — call ensure_ready() first"
        return self._client

_CONFIG = AsyncConfig()

async def ask(question: str) -> str:
    await _CONFIG.ensure_ready()   # no-op after first call
    response = await _CONFIG.client.messages.create(
        model=_CONFIG.model,
        max_tokens=_CONFIG.max_tokens,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

async def main() -> None:
    questions = [
        "What is an asyncio Lock?",
        "What is a coroutine?",
        "What is an event loop?",
        "What is asyncio.gather?",
    ]

    print("Launching 4 concurrent requests — config loads exactly once:")
    t0 = time.perf_counter()
    results = await asyncio.gather(*[ask(q) for q in questions])
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  {len(questions)} concurrent calls in {elapsed:.0f}ms")
    for q, r in zip(questions, results):
        print(f"  Q: {q}")
        print(f"  A: {r.strip()[:80]}\n")

asyncio.run(main())
```

**Expected Token Savings:** `asyncio.Lock` ensures that when 10 coroutines arrive simultaneously and all find `_ready=False`, only one performs the config fetch while the other 9 await the lock — no duplicate fetches, no race conditions, and every subsequent call pays zero config overhead.
**Environment:** Async agents (FastAPI, aiohttp, asyncio workers) where multiple coroutines may call `ensure_ready()` concurrently at startup; the lock pattern is the standard solution for lazy async initialisation.

---

## Comparison

| Option | Reload on Change | Thread-Safe | Async-Safe | Best For |
|---|---|---|---|---|
| 1. Module-level singleton | No (restart required) | Yes | Yes | All scripts — simplest pattern |
| 2. `lru_cache` | Via `cache_clear()` | Yes (GIL) | No | Single-process agents |
| 3. Dataclass + lazy init | No | Yes | No | Typed config with IDE support |
| 4. TTL secret cache | Yes (TTL-based) | Yes | No | Rotating secrets / Vault / SSM |
| 5. File watcher hot-reload | Yes (poll-based) | Yes | No | Long-running processes, ops config |
| 6. Async lock init | No | N/A | Yes | Async agents with concurrent startup |
