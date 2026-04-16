---
title: "Agent Doesn't Implement Event-Driven Configuration Propagation"
description: "How to propagate configuration changes across distributed agent instances in real time using pub/sub channels, event buses, and push-based config distribution — eliminating polling and ensuring all agents converge on the same config state."
date: 2025-01-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-event-driven-configuration-propagation
tags:
  - reliability
  - configuration
  - event-driven
  - pub-sub
  - distributed-systems
  - real-time-updates
  - config-sync
symptoms:
  - "Configuration changes take minutes to propagate to all agent instances because of polling intervals"
  - "Some agent replicas pick up new config while others still run with stale values"
  - "No way to push urgent configuration changes (e.g., kill switch) instantly to all agents"
  - "Agent instances diverge when a config update partially propagates during rolling deploy"
  - "Configuration change audit trail is missing — no record of what changed when"
  - "Agents poll a shared database for config every N seconds, generating unnecessary load"
---

## Why This Happens

Agents deployed as multiple replicas commonly poll a config store (database, S3, Consul) on a timer. Polling introduces latency proportional to the poll interval — if you poll every 60 seconds, it can take up to 60 seconds for a critical config change (rate limit, kill switch, model swap) to reach all instances. During that window, replicas are inconsistent. Additionally, polling under load generates unnecessary database reads that scale linearly with replica count.

Event-driven configuration propagation inverts this: the config store *pushes* changes to all subscribers immediately when they occur. Agents receive updates within milliseconds and apply them without polling. This reduces propagation latency from O(poll interval) to O(network RTT) and eliminates background polling load.

---

## Solution 1: Redis Pub/Sub Config Channel

Use Redis pub/sub to broadcast configuration changes to all subscribed agent instances the moment a change is committed.

```python
import asyncio
import json
import logging
import time
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

class RedisConfigChannel:
    """
    Publishes config change events to a Redis channel.
    All subscribed agents receive the update within milliseconds.
    """

    CHANNEL = "agent:config:updates"

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        import redis.asyncio as aioredis
        self._pub = aioredis.from_url(redis_url, decode_responses=True)
        self._sub_client = aioredis.from_url(redis_url, decode_responses=True)
        self._handlers: list[Callable[[str, Any], Awaitable[None]]] = []
        self._sub_task: asyncio.Task | None = None

    async def publish(self, key: str, value: Any, author: str = "system") -> None:
        """Publish a config change event to all subscribers."""
        event = json.dumps({
            "key": key,
            "value": value,
            "author": author,
            "timestamp": time.time(),
            "version": int(time.time() * 1000),
        })
        await self._pub.publish(self.CHANNEL, event)
        logger.info("Config published: %s=%s by %s", key, value, author)

    def on_change(self, handler: Callable[[str, Any], Awaitable[None]]) -> None:
        """Register a callback invoked when any config key changes."""
        self._handlers.append(handler)

    async def start_listening(self) -> None:
        """Start background task that listens for config change events."""
        self._sub_task = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self) -> None:
        pubsub = self._sub_client.pubsub()
        await pubsub.subscribe(self.CHANNEL)
        logger.info("Config subscriber started on channel: %s", self.CHANNEL)

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                event = json.loads(message["data"])
                for handler in self._handlers:
                    try:
                        await handler(event["key"], event["value"])
                    except Exception as exc:
                        logger.error("Config handler error: %s", exc)
            except json.JSONDecodeError:
                logger.warning("Invalid config event: %s", message["data"])

    async def stop(self) -> None:
        if self._sub_task:
            self._sub_task.cancel()


class EventDrivenConfigStore:
    """
    Config store that publishes changes immediately via Redis pub/sub.
    Agents subscribe and apply changes in real time.
    """

    def __init__(self, channel: RedisConfigChannel, initial_config: dict | None = None):
        self._config: dict[str, Any] = initial_config or {}
        self._channel = channel
        self._version: dict[str, int] = {}

        # Listen for remote changes from other instances
        self._channel.on_change(self._apply_remote_change)

    async def start(self) -> None:
        await self._channel.start_listening()

    async def set(self, key: str, value: Any, author: str = "local") -> None:
        """Update config locally and broadcast to all subscribers."""
        self._config[key] = value
        self._version[key] = int(time.time() * 1000)
        await self._channel.publish(key, value, author=author)

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    async def _apply_remote_change(self, key: str, value: Any) -> None:
        """Apply a config change received from a remote publisher."""
        old = self._config.get(key)
        self._config[key] = value
        if old != value:
            logger.info("Config updated: %s: %s -> %s", key, old, value)

    def snapshot(self) -> dict:
        return dict(self._config)


# --- Usage ---

async def demo_redis_config():
    channel = RedisConfigChannel()
    store = EventDrivenConfigStore(channel, initial_config={
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 4096,
        "rate_limit_rps": 10,
    })
    await store.start()

    # Change propagates to all replicas instantly
    await store.set("rate_limit_rps", 5, author="ops-team")
    print("Rate limit updated:", store.get("rate_limit_rps"))
```

---

## Solution 2: Hierarchical Config with Inheritance and Overrides

Support org-level, team-level, and agent-level config layers with event-driven propagation at each level.

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ConfigLayer:
    """A single layer in the config hierarchy."""
    level: str           # "global", "team", "agent"
    scope_id: str        # e.g., "global", "team-abc", "agent-xyz"
    values: dict = field(default_factory=dict)
    version: int = 0

class HierarchicalConfigStore:
    """
    Multi-level config with inheritance: global < team < agent.
    Changes at any level propagate instantly and are resolved by priority.
    """

    LEVELS = ["global", "team", "agent"]

    def __init__(self, channel: RedisConfigChannel, agent_id: str, team_id: str):
        self.agent_id = agent_id
        self.team_id = team_id
        self._layers: dict[str, ConfigLayer] = {
            "global": ConfigLayer("global", "global"),
            "team":   ConfigLayer("team",   team_id),
            "agent":  ConfigLayer("agent",  agent_id),
        }
        self._channel = channel
        self._channel.on_change(self._handle_event)
        self._change_callbacks: list[Callable] = []

    async def start(self) -> None:
        await self._channel.start_listening()

    def get(self, key: str, default: Any = None) -> Any:
        """Resolve key by walking from most specific (agent) to least (global)."""
        for level in reversed(self.LEVELS):
            layer = self._layers[level]
            if key in layer.values:
                return layer.values[key]
        return default

    async def set(self, key: str, value: Any, level: str = "agent") -> None:
        if level not in self._layers:
            raise ValueError(f"Unknown level: {level}")
        self._layers[level].values[key] = value
        self._layers[level].version += 1

        event_key = f"{level}:{self._layers[level].scope_id}:{key}"
        await self._channel.publish(event_key, value)

    async def _handle_event(self, event_key: str, value: Any) -> None:
        parts = event_key.split(":", 2)
        if len(parts) != 3:
            return
        level, scope_id, key = parts

        if level not in self._layers:
            return
        layer = self._layers[level]

        # Only apply if this event targets our scope or global scope
        if scope_id not in ("global", self.team_id, self.agent_id):
            return

        old = layer.values.get(key)
        layer.values[key] = value
        layer.version += 1

        if old != value:
            for cb in self._change_callbacks:
                await cb(level, key, old, value)

    def on_change(self, callback: Callable) -> None:
        self._change_callbacks.append(callback)

    def effective_config(self) -> dict:
        """Return fully resolved config (all levels merged)."""
        merged = {}
        for level in self.LEVELS:
            merged.update(self._layers[level].values)
        return merged
```

---

## Solution 3: Config Event Bus with Typed Events

A strongly typed config event bus that routes different config categories to specialized handlers.

```python
from enum import Enum
from dataclasses import dataclass
import asyncio

class ConfigCategory(str, Enum):
    MODEL    = "model"
    RATE     = "rate"
    FEATURE  = "feature"
    SECURITY = "security"
    PROMPT   = "prompt"

@dataclass
class ConfigEvent:
    category: ConfigCategory
    key: str
    old_value: Any
    new_value: Any
    timestamp: float
    author: str
    version: int

ConfigHandler = Callable[[ConfigEvent], Awaitable[None]]

class TypedConfigEventBus:
    """
    Routes config change events to typed category handlers.
    Handlers can respond differently to model changes vs. rate limit changes.
    """

    def __init__(self):
        self._handlers: dict[ConfigCategory, list[ConfigHandler]] = {c: [] for c in ConfigCategory}
        self._global_handlers: list[ConfigHandler] = []
        self._event_log: list[ConfigEvent] = []

    def subscribe(
        self,
        handler: ConfigHandler,
        categories: list[ConfigCategory] | None = None,
    ) -> None:
        if categories is None:
            self._global_handlers.append(handler)
        else:
            for cat in categories:
                self._handlers[cat].append(handler)

    async def emit(self, event: ConfigEvent) -> None:
        self._event_log.append(event)
        handlers = self._handlers[event.category] + self._global_handlers
        await asyncio.gather(*[h(event) for h in handlers], return_exceptions=True)

    def get_audit_log(self, category: ConfigCategory | None = None) -> list[ConfigEvent]:
        if category:
            return [e for e in self._event_log if e.category == category]
        return list(self._event_log)

    def _infer_category(self, key: str) -> ConfigCategory:
        if "model" in key or "temperature" in key:
            return ConfigCategory.MODEL
        if "rate" in key or "limit" in key or "quota" in key:
            return ConfigCategory.RATE
        if "feature" in key or "flag" in key:
            return ConfigCategory.FEATURE
        if "prompt" in key or "system" in key:
            return ConfigCategory.PROMPT
        if "secret" in key or "auth" in key or "allow" in key:
            return ConfigCategory.SECURITY
        return ConfigCategory.FEATURE


class BusBackedConfigStore:
    """Config store that emits typed events on every change."""

    def __init__(self, bus: TypedConfigEventBus):
        self._store: dict[str, Any] = {}
        self._versions: dict[str, int] = {}
        self._bus = bus

    async def set(self, key: str, value: Any, author: str = "system") -> None:
        old = self._store.get(key)
        version = self._versions.get(key, 0) + 1
        self._store[key] = value
        self._versions[key] = version

        category = self._bus._infer_category(key)
        await self._bus.emit(ConfigEvent(
            category=category,
            key=key,
            old_value=old,
            new_value=value,
            timestamp=time.time(),
            author=author,
            version=version,
        ))

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)


# --- Agent that reacts to specific config categories ---

class ConfigAwareAgent:
    def __init__(self, store: BusBackedConfigStore, bus: TypedConfigEventBus):
        self.store = store
        self._current_model = store.get("model", "claude-3-haiku-20240307")
        self._rate_limit = store.get("rate_limit_rps", 10)

        bus.subscribe(self._on_model_change, [ConfigCategory.MODEL])
        bus.subscribe(self._on_rate_change,  [ConfigCategory.RATE])

    async def _on_model_change(self, event: ConfigEvent) -> None:
        logger.info("Model config changed: %s -> %s", event.old_value, event.new_value)
        if event.key == "model":
            self._current_model = event.new_value

    async def _on_rate_change(self, event: ConfigEvent) -> None:
        logger.info("Rate config changed: %s=%s", event.key, event.new_value)
        if event.key == "rate_limit_rps":
            self._rate_limit = event.new_value
```

---

## Solution 4: Config Snapshot with Delta Sync

On startup, agents download the full config snapshot; afterward they receive only deltas via the event channel, minimizing bandwidth.

```python
import hashlib
import json
from dataclasses import dataclass

@dataclass
class ConfigSnapshot:
    version: int
    data: dict
    checksum: str
    created_at: float

    @classmethod
    def create(cls, data: dict) -> "ConfigSnapshot":
        payload = json.dumps(data, sort_keys=True).encode()
        return cls(
            version=int(time.time() * 1000),
            data=data,
            checksum=hashlib.sha256(payload).hexdigest()[:16],
            created_at=time.time(),
        )

    def apply_delta(self, key: str, value: Any) -> "ConfigSnapshot":
        new_data = {**self.data, key: value}
        return ConfigSnapshot.create(new_data)

class SnapshotBackedConfigStore:
    """
    Agents fetch a full snapshot on startup, then receive deltas.
    Reconnection triggers a fresh snapshot to avoid missing events.
    """

    def __init__(self, snapshot_url: str, channel: RedisConfigChannel):
        self._snapshot_url = snapshot_url
        self._channel = channel
        self._snapshot: ConfigSnapshot | None = None
        self._pending_deltas: list[tuple[str, Any]] = []
        self._channel.on_change(self._buffer_delta)

    async def initialize(self) -> None:
        """Fetch snapshot then replay any buffered deltas."""
        self._snapshot = await self._fetch_snapshot()
        # Apply any deltas that arrived during snapshot fetch
        for key, value in self._pending_deltas:
            self._snapshot = self._snapshot.apply_delta(key, value)
        self._pending_deltas.clear()
        await self._channel.start_listening()

    async def _fetch_snapshot(self) -> ConfigSnapshot:
        # Simulate fetching from config server
        return ConfigSnapshot.create({
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 4096,
            "rate_limit_rps": 10,
            "features": {"dark_mode": True, "beta": False},
        })

    async def _buffer_delta(self, key: str, value: Any) -> None:
        if self._snapshot is None:
            # Snapshot not ready yet — buffer
            self._pending_deltas.append((key, value))
        else:
            self._snapshot = self._snapshot.apply_delta(key, value)

    def get(self, key: str, default: Any = None) -> Any:
        if self._snapshot is None:
            return default
        return self._snapshot.data.get(key, default)

    @property
    def current_version(self) -> int:
        return self._snapshot.version if self._snapshot else 0
```

---

## Solution 5: Config Change Validator with Pre-Apply Hooks

Validate config changes before applying them and run pre-apply hooks to ensure the new config is safe.

```python
from typing import Optional

class ConfigValidationError(Exception):
    pass

ConfigValidator = Callable[[str, Any, Any], None]  # (key, old, new) -> raises on invalid

class ValidatedConfigStore:
    """
    Config store with pluggable validators and pre-apply hooks.
    Changes are rejected if any validator raises.
    """

    def __init__(self, channel: RedisConfigChannel):
        self._store: dict[str, Any] = {}
        self._validators: dict[str, list[ConfigValidator]] = {}
        self._pre_apply_hooks: list[Callable] = []
        self._post_apply_hooks: list[Callable] = []
        self._channel = channel
        self._channel.on_change(self._apply_validated_change)

    def add_validator(self, key_pattern: str, validator: ConfigValidator) -> None:
        self._validators.setdefault(key_pattern, []).append(validator)

    def on_pre_apply(self, hook: Callable) -> None:
        self._pre_apply_hooks.append(hook)

    def on_post_apply(self, hook: Callable) -> None:
        self._post_apply_hooks.append(hook)

    async def set(self, key: str, value: Any) -> None:
        old = self._store.get(key)
        self._validate(key, old, value)

        for hook in self._pre_apply_hooks:
            await hook(key, old, value)

        self._store[key] = value
        await self._channel.publish(key, value)

        for hook in self._post_apply_hooks:
            await hook(key, old, value)

    def _validate(self, key: str, old: Any, new: Any) -> None:
        for pattern, validators in self._validators.items():
            if pattern == key or key.startswith(pattern):
                for validator in validators:
                    validator(key, old, new)

    async def _apply_validated_change(self, key: str, value: Any) -> None:
        old = self._store.get(key)
        try:
            self._validate(key, old, value)
            self._store[key] = value
        except ConfigValidationError as exc:
            logger.error("Remote config change rejected for '%s': %s", key, exc)


# --- Built-in validators ---

def rate_limit_validator(key: str, old: Any, new: Any) -> None:
    if not isinstance(new, (int, float)) or new <= 0 or new > 10_000:
        raise ConfigValidationError(f"rate_limit must be 0 < x <= 10000, got {new}")

def model_validator(key: str, old: Any, new: Any) -> None:
    allowed = {"claude-3-haiku-20240307", "claude-3-5-sonnet-20241022", "claude-opus-4-6"}
    if new not in allowed:
        raise ConfigValidationError(f"Model '{new}' not in allowed set: {allowed}")


def demo_validated_config():
    import asyncio
    channel = RedisConfigChannel()
    store = ValidatedConfigStore(channel)
    store.add_validator("rate_limit_rps", rate_limit_validator)
    store.add_validator("model", model_validator)
    # asyncio.run(store.set("model", "claude-opus-4-6"))  # OK
    # asyncio.run(store.set("model", "gpt-4"))            # Raises ConfigValidationError
```

---

## Solution 6: Config Convergence Monitor

Detect when agent replicas diverge from the expected config state and alert or force-sync.

```python
import asyncio
import time
from dataclasses import dataclass

@dataclass
class ReplicaConfigState:
    replica_id: str
    config_version: int
    config_checksum: str
    last_seen: float

class ConfigConvergenceMonitor:
    """
    Monitors all known replicas' config versions.
    Alerts when replicas are behind or diverged from the expected state.
    """

    def __init__(self, expected_version_fn: Callable[[], int], staleness_threshold: float = 30.0):
        self._replicas: dict[str, ReplicaConfigState] = {}
        self._expected_version = expected_version_fn
        self._staleness_threshold = staleness_threshold

    def report_state(self, replica_id: str, version: int, checksum: str) -> None:
        """Called by each replica on heartbeat."""
        self._replicas[replica_id] = ReplicaConfigState(
            replica_id=replica_id,
            config_version=version,
            config_checksum=checksum,
            last_seen=time.time(),
        )

    def convergence_report(self) -> dict:
        expected = self._expected_version()
        now = time.time()

        diverged = []
        stale = []
        converged = []

        for replica_id, state in self._replicas.items():
            if now - state.last_seen > self._staleness_threshold:
                stale.append(replica_id)
            elif state.config_version < expected:
                diverged.append({
                    "replica_id": replica_id,
                    "version": state.config_version,
                    "behind_by": expected - state.config_version,
                })
            else:
                converged.append(replica_id)

        return {
            "expected_version": expected,
            "total_replicas": len(self._replicas),
            "converged": len(converged),
            "diverged": diverged,
            "stale": stale,
            "convergence_rate": len(converged) / len(self._replicas) if self._replicas else 1.0,
        }

    def is_converged(self) -> bool:
        report = self.convergence_report()
        return not report["diverged"] and not report["stale"]
```

---

## Comparison

| Solution | Latency | Ordering | Durability | Best For |
|---|---|---|---|---|
| Redis Pub/Sub Channel | ~1ms | Unordered | None (fire-and-forget) | Simple real-time push |
| Hierarchical Config | ~1ms | Unordered | None | Multi-tenant with override levels |
| Typed Event Bus | ~1ms | Per-category | In-memory log | Category-specific handlers |
| Snapshot + Delta Sync | ~1ms (delta) | Eventual | Snapshot durable | Large configs + reconnect safety |
| Validated Config Store | ~1ms + validation | Ordered per key | None | Safety-critical config changes |
| Convergence Monitor | N/A (passive) | N/A | N/A | Detecting replica drift |

**Use Redis Pub/Sub** as the default transport — it's simple, fast, and widely available. **Add hierarchical config** when different teams or agent types need different defaults. **Add the convergence monitor** to detect drift in production multi-replica deployments. **Always add validators** for rate limits, model names, and other constrained values to prevent bad configs from propagating. **Use snapshot + delta** for large configs (>100 keys) to handle reconnects gracefully.
