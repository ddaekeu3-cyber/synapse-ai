---
title: "Agent Doesn't Implement Configuration Hot Reload Without Restart"
description: "AI agents that require a full restart to apply configuration changes accumulate downtime and risk during every deployment. Learn six patterns for hot-reloading prompts, model settings, and feature flags without interrupting in-flight requests."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-configuration-hot-reload-without-restart
tags: [configuration, hot-reload, deployment, zero-downtime, feature-flags, reliability]
symptoms:
  - "Changing the system prompt requires restarting all agent processes"
  - "Feature flag updates take 10 minutes to propagate because of restart cycles"
  - "In-flight requests are dropped when configuration is updated"
  - "Model version pin changes require a full deployment pipeline"
  - "Configuration drift between instances because each restart applies a different config version"
---

## The Problem

AI agents carry significant configuration: system prompts, model selection, temperature, tool schemas, feature flags, rate limit settings, and prompt templates. In most agent implementations, these values are baked into the process at startup. Any change requires a full restart, which drops in-flight requests, introduces a restart window during which the service is unavailable, and requires coordinating rolling deployments across instances.

Hot reload solves this: configuration changes are detected, validated, and applied to new requests immediately, while in-flight requests complete using the version of config they started with.

```python
# ❌ Config baked at startup — requires restart to change
SYSTEM_PROMPT = os.environ["SYSTEM_PROMPT"]  # Frozen at process start

# ✓ Hot-reloadable config
config = HotReloadableConfig("config.yaml")
system_prompt = config.get("system_prompt")  # Always returns current version
```

---

## Solution 1: File Watcher with Atomic Config Swap

Watch configuration files for changes using `watchfiles` (or `inotify`). Parse and validate the new config, then atomically swap the reference so new requests see the updated config immediately.

```python
import asyncio
import json
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import copy


@dataclass
class AgentConfig:
    system_prompt: str = "You are a helpful assistant."
    model: str = "claude-3-5-sonnet-20241022"
    max_tokens: int = 4096
    temperature: float = 1.0
    tool_timeout_seconds: float = 30.0
    feature_flags: dict[str, bool] = field(default_factory=dict)
    version: int = 0
    loaded_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class HotReloadableConfig:
    """
    Watches a config file and atomically swaps config on change.
    In-flight requests hold a reference to the config version they started with.
    """

    def __init__(self, config_path: str, poll_interval_seconds: float = 5.0):
        self._path = Path(config_path)
        self._poll_interval = poll_interval_seconds
        self._current: AgentConfig = self._load()
        self._lock = threading.RLock()
        self._reload_callbacks: list = []
        self._last_mtime: float = self._path.stat().st_mtime if self._path.exists() else 0.0
        self._reload_count = 0
        self._watcher_task: asyncio.Task | None = None

    def _load(self) -> AgentConfig:
        if not self._path.exists():
            return AgentConfig()
        try:
            data = json.loads(self._path.read_text())
            config = AgentConfig.from_dict(data)
            config.version = self._reload_count
            return config
        except Exception as e:
            print(f"[config] Failed to parse {self._path}: {e}")
            return self._current if hasattr(self, '_current') else AgentConfig()

    def get(self) -> AgentConfig:
        """Return the current config snapshot. Thread-safe."""
        with self._lock:
            return self._current

    def get_value(self, key: str, default: Any = None) -> Any:
        config = self.get()
        return getattr(config, key, default)

    def on_reload(self, callback):
        """Register a callback invoked after successful config reload."""
        self._reload_callbacks.append(callback)
        return self

    async def _watch_loop(self):
        """Periodically check for file changes and reload if modified."""
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                current_mtime = self._path.stat().st_mtime if self._path.exists() else 0.0
                if current_mtime != self._last_mtime:
                    new_config = self._load()
                    if new_config:
                        with self._lock:
                            old_version = self._current.version
                            self._current = new_config
                            self._current.version = self._reload_count + 1
                            self._reload_count += 1
                        self._last_mtime = current_mtime
                        print(
                            f"[config] Reloaded v{self._current.version} "
                            f"from {self._path} (was v{old_version})"
                        )
                        for cb in self._reload_callbacks:
                            if asyncio.iscoroutinefunction(cb):
                                await cb(self._current)
                            else:
                                cb(self._current)
            except Exception as e:
                print(f"[config] Watch error: {e}")

    async def start_watching(self):
        self._watcher_task = asyncio.create_task(self._watch_loop())

    def stop_watching(self):
        if self._watcher_task:
            self._watcher_task.cancel()

    async def force_reload(self):
        """Manually trigger a reload."""
        self._last_mtime = 0.0  # Force detection on next poll
```

---

## Solution 2: Versioned Config with Request-Scoped Binding

Each request captures its config version at start time and uses that version for its entire lifetime. Config updates never affect in-flight requests.

```python
import asyncio
import contextvars
import time
from dataclasses import dataclass
from typing import Any


_request_config: contextvars.ContextVar["VersionedConfig | None"] = \
    contextvars.ContextVar("_request_config", default=None)


@dataclass
class VersionedConfig:
    version: int
    data: dict
    captured_at: float


class VersionedConfigManager:
    """
    Maintains a versioned config store.
    Each request binds to the config version current at request start.
    Updates are lock-free using atomic reference swap.
    """

    def __init__(self, initial: dict | None = None):
        self._version = 0
        self._config = VersionedConfig(
            version=0,
            data=initial or {},
            captured_at=time.time(),
        )

    def current(self) -> VersionedConfig:
        return self._config

    def update(self, new_data: dict, merge: bool = False) -> int:
        """
        Apply a config update. Returns new version number.
        merge=True: deep merge with existing config.
        merge=False: full replacement.
        """
        if merge:
            merged = {**self._config.data, **new_data}
        else:
            merged = new_data
        new_version = self._version + 1
        self._config = VersionedConfig(
            version=new_version,
            data=merged,
            captured_at=time.time(),
        )
        self._version = new_version
        print(f"[config] Updated to v{new_version}")
        return new_version

    def patch(self, key: str, value: Any) -> int:
        """Update a single key."""
        return self.update({key: value}, merge=True)

    def get(self, key: str, default: Any = None) -> Any:
        """Get value from the request-scoped config, or current if no request scope."""
        req_config = _request_config.get()
        config = req_config if req_config else self._config
        return config.data.get(key, default)

    def bind_to_request(self) -> contextvars.Token:
        """Bind the current config version to this request's context."""
        token = _request_config.set(self._config)
        return token

    def release_request(self, token: contextvars.Token):
        _request_config.reset(token)

    def version(self) -> int:
        return self._version

    # Context manager for request-scoped config
    class RequestScope:
        def __init__(self, manager: "VersionedConfigManager"):
            self._mgr = manager
            self._token = None

        def __enter__(self):
            self._token = self._mgr.bind_to_request()
            return self._mgr

        def __exit__(self, *_):
            if self._token:
                self._mgr.release_request(self._token)

    def request_scope(self) -> "VersionedConfigManager.RequestScope":
        return self.RequestScope(self)
```

---

## Solution 3: Feature Flag Manager with Gradual Rollout

Hot-reloadable feature flags with gradual rollout percentages, per-tenant overrides, and kill-switch support. Config changes take effect on the next request without restart.

```python
import hashlib
import time
import json
from dataclasses import dataclass
from typing import Any
from pathlib import Path


@dataclass
class FeatureFlag:
    name: str
    enabled: bool
    rollout_percentage: float = 100.0     # 0-100
    tenant_overrides: dict[str, bool] = None   # tenant_id → force on/off
    description: str = ""
    updated_at: float = 0.0

    def __post_init__(self):
        if self.tenant_overrides is None:
            self.tenant_overrides = {}


class FeatureFlagManager:
    """
    In-memory feature flag manager with file-based persistence.
    Supports gradual rollout, per-tenant overrides, and hot reload.
    Changes take effect immediately without restart.
    """

    def __init__(self, flags_file: str | None = None):
        self._flags: dict[str, FeatureFlag] = {}
        self._flags_file = Path(flags_file) if flags_file else None
        self._load_from_file()

    def _load_from_file(self):
        if not self._flags_file or not self._flags_file.exists():
            return
        try:
            data = json.loads(self._flags_file.read_text())
            for name, spec in data.items():
                self._flags[name] = FeatureFlag(name=name, **spec)
            print(f"[flags] Loaded {len(self._flags)} flags from {self._flags_file}")
        except Exception as e:
            print(f"[flags] Failed to load flags: {e}")

    def _save_to_file(self):
        if not self._flags_file:
            return
        data = {
            name: {
                "enabled": f.enabled,
                "rollout_percentage": f.rollout_percentage,
                "tenant_overrides": f.tenant_overrides,
                "description": f.description,
                "updated_at": f.updated_at,
            }
            for name, f in self._flags.items()
        }
        self._flags_file.write_text(json.dumps(data, indent=2))

    def is_enabled(self, flag_name: str, tenant_id: str = "", default: bool = False) -> bool:
        flag = self._flags.get(flag_name)
        if flag is None:
            return default

        if not flag.enabled:
            return False

        # Tenant override takes precedence
        if tenant_id and tenant_id in flag.tenant_overrides:
            return flag.tenant_overrides[tenant_id]

        # Gradual rollout using hash-based bucketing
        if flag.rollout_percentage < 100.0:
            bucket_key = f"{flag_name}:{tenant_id or 'anonymous'}"
            bucket = int(hashlib.md5(bucket_key.encode()).hexdigest(), 16) % 100
            return bucket < flag.rollout_percentage

        return True

    def set_flag(self, name: str, enabled: bool,
                 rollout_pct: float = 100.0, description: str = "") -> FeatureFlag:
        flag = FeatureFlag(
            name=name, enabled=enabled,
            rollout_percentage=rollout_pct,
            description=description,
            updated_at=time.time(),
        )
        self._flags[name] = flag
        self._save_to_file()
        print(f"[flags] Set '{name}': enabled={enabled} rollout={rollout_pct}%")
        return flag

    def set_tenant_override(self, flag_name: str, tenant_id: str, enabled: bool):
        flag = self._flags.get(flag_name)
        if not flag:
            raise KeyError(f"Unknown flag: {flag_name}")
        flag.tenant_overrides[tenant_id] = enabled
        flag.updated_at = time.time()
        self._save_to_file()

    def kill_switch(self, flag_name: str):
        """Emergency: immediately disable a flag for all tenants."""
        if flag_name in self._flags:
            self._flags[flag_name].enabled = False
            self._flags[flag_name].updated_at = time.time()
            self._save_to_file()
            print(f"[flags] KILL SWITCH: '{flag_name}' disabled globally")

    def reload(self):
        """Re-read flags file. Called by file watcher."""
        self._flags.clear()
        self._load_from_file()

    def all_flags(self) -> dict[str, dict]:
        return {
            name: {
                "enabled": f.enabled,
                "rollout_pct": f.rollout_percentage,
                "tenant_override_count": len(f.tenant_overrides),
                "updated_at": f.updated_at,
            }
            for name, f in self._flags.items()
        }
```

---

## Solution 4: Prompt Template Hot Reload with Validation

System prompts and prompt templates are the most frequently changed configuration in AI agents. Hot-reload them with schema validation and a dry-run test before activating.

```python
import asyncio
import re
import time
import hashlib
from dataclasses import dataclass, field
from typing import Any
import anthropic


@dataclass
class PromptTemplate:
    name: str
    template: str
    variables: list[str]      # Required template variables
    version: str              # SHA256 of content
    validated: bool = False
    loaded_at: float = field(default_factory=time.time)

    def render(self, **kwargs) -> str:
        missing = [v for v in self.variables if v not in kwargs]
        if missing:
            raise ValueError(f"Missing template variables: {missing}")
        result = self.template
        for k, v in kwargs.items():
            result = result.replace(f"{{{{{k}}}}}", str(v))
        return result

    @classmethod
    def parse(cls, name: str, template: str) -> "PromptTemplate":
        variables = re.findall(r"\{\{(\w+)\}\}", template)
        version = hashlib.sha256(template.encode()).hexdigest()[:12]
        return cls(name=name, template=template, variables=list(set(variables)), version=version)


class PromptTemplateRegistry:
    """
    Hot-reloadable registry of prompt templates.
    Validates templates before activating and keeps previous version for rollback.
    """

    def __init__(self, validate_on_load: bool = True):
        self._templates: dict[str, PromptTemplate] = {}
        self._previous: dict[str, PromptTemplate] = {}  # For rollback
        self._validate = validate_on_load
        self._client = anthropic.AsyncAnthropic() if validate_on_load else None

    async def register(self, name: str, template: str,
                       dry_run_vars: dict | None = None) -> PromptTemplate:
        """Register or update a template. Optionally validate with a dry-run LLM call."""
        parsed = PromptTemplate.parse(name, template)

        if self._validate and dry_run_vars is not None:
            ok, error = await self._validate_template(parsed, dry_run_vars)
            if not ok:
                raise ValueError(f"Template '{name}' validation failed: {error}")
            parsed.validated = True

        # Save previous version for rollback
        if name in self._templates:
            self._previous[name] = self._templates[name]

        self._templates[name] = parsed
        print(
            f"[prompts] Registered '{name}' v{parsed.version} "
            f"(validated={parsed.validated})"
        )
        return parsed

    async def _validate_template(
        self, template: PromptTemplate, vars: dict
    ) -> tuple[bool, str]:
        """Dry-run: render template and send a minimal request to verify it doesn't fail."""
        try:
            rendered = template.render(**vars)
            resp = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=16,
                system=rendered,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True, ""
        except Exception as e:
            return False, str(e)

    def get(self, name: str) -> PromptTemplate:
        if name not in self._templates:
            raise KeyError(f"Unknown prompt template: '{name}'")
        return self._templates[name]

    def render(self, name: str, **kwargs) -> str:
        return self.get(name).render(**kwargs)

    def rollback(self, name: str) -> PromptTemplate | None:
        """Revert to the previous version of a template."""
        prev = self._previous.get(name)
        if prev:
            self._templates[name] = prev
            del self._previous[name]
            print(f"[prompts] Rolled back '{name}' to v{prev.version}")
        return prev

    def list_templates(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "version": t.version,
                "variables": t.variables,
                "validated": t.validated,
                "has_previous": t.name in self._previous,
            }
            for t in self._templates.values()
        ]
```

---

## Solution 5: Remote Config with Polling and Local Cache

Poll a remote config service (e.g., a Config API, AWS AppConfig, LaunchDarkly) at configurable intervals. Cache locally to serve requests even when the remote is unavailable.

```python
import asyncio
import json
import time
import hashlib
from dataclasses import dataclass, field
from typing import Any
import aiohttp


@dataclass
class RemoteConfigSnapshot:
    etag: str
    data: dict
    fetched_at: float
    source: str  # "remote" or "cache"


class RemoteConfigPoller:
    """
    Polls a remote config endpoint with ETag-based conditional requests.
    Falls back to cached config when remote is unavailable.
    Applies changes only after validation passes.
    """

    def __init__(
        self,
        config_url: str,
        poll_interval_seconds: float = 30.0,
        cache_path: str = "/tmp/agent_config_cache.json",
        on_change=None,
    ):
        self.url = config_url
        self.poll_interval = poll_interval_seconds
        self.cache_path = cache_path
        self.on_change = on_change
        self._current: RemoteConfigSnapshot | None = self._load_cache()
        self._etag: str = self._current.etag if self._current else ""
        self._consecutive_failures = 0
        self._task: asyncio.Task | None = None

    def _load_cache(self) -> RemoteConfigSnapshot | None:
        try:
            with open(self.cache_path) as f:
                data = json.load(f)
            snap = RemoteConfigSnapshot(
                etag=data["etag"],
                data=data["config"],
                fetched_at=data["fetched_at"],
                source="cache",
            )
            print(f"[remote_config] Loaded cache (etag={snap.etag[:8]}...)")
            return snap
        except Exception:
            return None

    def _save_cache(self, snap: RemoteConfigSnapshot):
        try:
            with open(self.cache_path, "w") as f:
                json.dump({
                    "etag": snap.etag,
                    "config": snap.data,
                    "fetched_at": snap.fetched_at,
                }, f, indent=2)
        except Exception as e:
            print(f"[remote_config] Cache write failed: {e}")

    def _validate(self, data: dict) -> tuple[bool, str]:
        """Validate config schema before applying."""
        required = ["model", "system_prompt"]
        missing = [k for k in required if k not in data]
        if missing:
            return False, f"Missing required keys: {missing}"
        if not isinstance(data.get("max_tokens", 1024), int):
            return False, "max_tokens must be an integer"
        return True, "ok"

    async def _poll(self):
        headers = {}
        if self._etag:
            headers["If-None-Match"] = self._etag

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 304:
                        # Not modified — no change
                        self._consecutive_failures = 0
                        return
                    if resp.status != 200:
                        raise aiohttp.ClientError(f"HTTP {resp.status}")

                    new_etag = resp.headers.get("ETag", hashlib.md5(
                        str(time.time()).encode()
                    ).hexdigest())
                    data = await resp.json()

                    ok, reason = self._validate(data)
                    if not ok:
                        print(f"[remote_config] Validation failed: {reason}")
                        return

                    snap = RemoteConfigSnapshot(
                        etag=new_etag, data=data,
                        fetched_at=time.time(), source="remote",
                    )
                    old_etag = self._etag
                    self._current = snap
                    self._etag = new_etag
                    self._consecutive_failures = 0
                    self._save_cache(snap)

                    print(f"[remote_config] Updated config (etag={new_etag[:8]}...)")
                    if self.on_change and old_etag != new_etag:
                        if asyncio.iscoroutinefunction(self.on_change):
                            await self.on_change(snap)
                        else:
                            self.on_change(snap)

        except Exception as e:
            self._consecutive_failures += 1
            print(
                f"[remote_config] Poll failed ({self._consecutive_failures}): {e}. "
                f"Using cached config."
            )

    def get(self, key: str, default: Any = None) -> Any:
        if self._current:
            return self._current.data.get(key, default)
        return default

    def snapshot(self) -> RemoteConfigSnapshot | None:
        return self._current

    async def start(self):
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self):
        while True:
            await self._poll()
            await asyncio.sleep(self.poll_interval)

    def stop(self):
        if self._task:
            self._task.cancel()
```

---

## Solution 6: Config Change Bus with Subscriber Notifications

A config change bus that notifies all registered subscribers when specific config keys change. Allows different agent components (prompt manager, tool executor, rate limiter) to react to relevant changes independently.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ConfigChangeEvent:
    key: str
    old_value: Any
    new_value: Any
    version: int
    changed_at: float = field(default_factory=time.time)


class ConfigChangeBus:
    """
    Pub/sub bus for config changes.
    Components subscribe to specific keys and are notified on change.
    """

    def __init__(self):
        self._config: dict[str, Any] = {}
        self._version: int = 0
        self._subscribers: dict[str, list[Callable]] = {}  # key → [callbacks]
        self._wildcard_subscribers: list[Callable] = []   # Subscribed to all changes
        self._change_log: list[ConfigChangeEvent] = []
        self._lock = asyncio.Lock()

    def subscribe(self, key: str, callback: Callable):
        """Subscribe to changes on a specific key."""
        if key == "*":
            self._wildcard_subscribers.append(callback)
        else:
            self._subscribers.setdefault(key, []).append(callback)

    def unsubscribe(self, key: str, callback: Callable):
        if key == "*":
            self._wildcard_subscribers = [c for c in self._wildcard_subscribers if c != callback]
        elif key in self._subscribers:
            self._subscribers[key] = [c for c in self._subscribers[key] if c != callback]

    async def set(self, key: str, value: Any):
        async with self._lock:
            old_value = self._config.get(key)
            if old_value == value:
                return  # No change — don't notify
            self._config[key] = value
            self._version += 1
            event = ConfigChangeEvent(
                key=key, old_value=old_value,
                new_value=value, version=self._version,
            )
            self._change_log.append(event)

        await self._notify(event)

    async def set_many(self, updates: dict[str, Any]):
        """Batch update multiple keys, emitting one event per changed key."""
        for key, value in updates.items():
            await self.set(key, value)

    async def _notify(self, event: ConfigChangeEvent):
        callbacks = list(self._subscribers.get(event.key, []))
        callbacks += list(self._wildcard_subscribers)

        for cb in callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(event)
                else:
                    cb(event)
            except Exception as e:
                print(f"[config_bus] Subscriber error for key '{event.key}': {e}")

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def get_all(self) -> dict:
        return dict(self._config)

    def version(self) -> int:
        return self._version

    def recent_changes(self, n: int = 10) -> list[ConfigChangeEvent]:
        return self._change_log[-n:]


# Example usage: agent components subscribe to their relevant config keys

async def demo():
    bus = ConfigChangeBus()

    # Rate limiter subscribes to its config
    async def on_rate_limit_change(event: ConfigChangeEvent):
        print(f"[rate_limiter] Updating limit: {event.old_value} → {event.new_value}")
    bus.subscribe("max_requests_per_minute", on_rate_limit_change)

    # Prompt manager subscribes to system prompt changes
    async def on_prompt_change(event: ConfigChangeEvent):
        print(f"[prompt_mgr] System prompt updated (len={len(str(event.new_value))})")
    bus.subscribe("system_prompt", on_prompt_change)

    # Wildcard subscriber for audit log
    def audit_log(event: ConfigChangeEvent):
        print(f"[audit] Config changed: {event.key} v{event.version}")
    bus.subscribe("*", audit_log)

    # Apply changes — all subscribers notified automatically
    await bus.set_many({
        "system_prompt": "You are an expert coding assistant.",
        "max_requests_per_minute": 100,
        "model": "claude-opus-4-6",
    })
```

---

## Comparison

| Pattern | Change Detection | In-Flight Safety | Rollback Support | Best For |
|---|---|---|---|---|
| File watcher + atomic swap | File mtime polling | Yes (atomic swap) | No built-in | Config files in containers |
| Versioned + request-scoped | Push on demand | Full (per-request snapshot) | Via version history | Long-running requests |
| Feature flag manager | File or push | Yes (new requests only) | Kill switch | Gradual rollout, A/B testing |
| Prompt template registry | Push (register call) | Yes | Yes (explicit rollback) | Frequent prompt iteration |
| Remote config poller | ETag-based poll | Yes (swap on poll) | Via cache | Centralized config service |
| Config change bus | Push/event | Yes (async notify) | Via change log | Decoupled multi-component agents |

**Recommendations:**
- Use **versioned + request-scoped** (Solution 2) as the safety foundation — it guarantees in-flight requests are never affected by config changes.
- Add **file watcher** (Solution 1) for container-based deployments where config is mounted as a volume.
- Use **feature flags** (Solution 3) for any behavioral changes you want to roll out gradually or kill instantly.
- Use **prompt template registry** (Solution 4) when you iterate on prompts frequently — the dry-run validation prevents broken prompts from going live.
- Deploy **remote config poller** (Solution 5) when multiple agent instances need to synchronize config from a central source.
- Use the **config change bus** (Solution 6) in agents with many independent components that need to react to different config keys independently.
