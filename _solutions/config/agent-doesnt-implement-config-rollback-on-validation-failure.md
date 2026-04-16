---
layout: solution
title: "Agent Doesn't Implement Config Rollback on Validation Failure"
category: config
description: "Detect invalid configuration at load time and automatically roll back to the last known-good state, preventing agents from starting or reloading with a broken config."
tags: [config, rollback, validation, reliability, hot-reload, safety, startup]
---

# Agent Doesn't Implement Config Rollback on Validation Failure

## Problem

An operator edits a config file, introduces a typo in the model name or sets `max_tokens` to a string instead of an integer, and the running agent reloads. Without rollback, the agent either crashes, starts serving errors, or silently falls back to hardcoded defaults with no visibility. The fix is to validate before committing and restore the previous config if validation fails.

## Solution Options

### Option 1: Validate-Before-Apply with Backup

```python
import anthropic
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentConfig:
    model: str
    max_tokens: int
    system_prompt: str
    temperature: float = 1.0

    def validate(self) -> list[str]:
        errors = []
        valid_models = {"claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"}
        if self.model not in valid_models:
            errors.append(f"Invalid model '{self.model}'. Must be one of {valid_models}")
        if not (1 <= self.max_tokens <= 8192):
            errors.append(f"max_tokens={self.max_tokens} out of range [1, 8192]")
        if not (0.0 <= self.temperature <= 1.0):
            errors.append(f"temperature={self.temperature} out of range [0.0, 1.0]")
        if not self.system_prompt.strip():
            errors.append("system_prompt must not be empty")
        return errors


class ConfigLoader:
    def __init__(self, config_path: str) -> None:
        self.config_path = Path(config_path)
        self.backup_path = self.config_path.with_suffix(".backup.json")
        self._current: AgentConfig | None = None

    def load(self) -> AgentConfig:
        raw = json.loads(self.config_path.read_text())
        candidate = AgentConfig(**raw)
        errors = candidate.validate()

        if errors:
            print(f"[config] Validation failed: {errors}")
            if self.backup_path.exists():
                print("[config] Rolling back to last known-good config")
                backup_raw = json.loads(self.backup_path.read_text())
                self._current = AgentConfig(**backup_raw)
                return self._current
            raise ValueError(f"Config invalid and no backup available: {errors}")

        # Valid — save backup
        shutil.copy2(self.config_path, self.backup_path)
        self._current = candidate
        print(f"[config] Loaded successfully (model={candidate.model})")
        return candidate

    @property
    def current(self) -> AgentConfig | None:
        return self._current


def run_agent(config: AgentConfig, user_message: str) -> str:
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        system=config.system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text


def demo() -> None:
    config_path = "/tmp/agent_config.json"

    # Write a valid config
    valid_cfg = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "system_prompt": "You are a helpful assistant.",
        "temperature": 1.0,
    }
    Path(config_path).write_text(json.dumps(valid_cfg))

    loader = ConfigLoader(config_path)
    config = loader.load()  # creates backup
    print("First load:", run_agent(config, "Say hi")[:60])

    # Simulate a bad config reload
    bad_cfg = {"model": "gpt-invalid", "max_tokens": 256, "system_prompt": "Hi", "temperature": 1.0}
    Path(config_path).write_text(json.dumps(bad_cfg))

    config = loader.load()  # should rollback
    print("After bad reload, model used:", config.model)  # should still be haiku


if __name__ == "__main__":
    demo()

# Expected Token Savings: No extra tokens; bad config never reaches the model
# Environment: Any agent with file-based config that supports hot reload
```

---

### Option 2: Versioned Config Store with Multi-Version History

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConfigVersion:
    version: int
    config: dict
    loaded_at: float = field(default_factory=time.time)
    valid: bool = True
    errors: list[str] = field(default_factory=list)


class VersionedConfigStore:
    """
    Keeps the last N config versions in memory.
    On validation failure, rolls back to the most recent valid version.
    """

    REQUIRED_KEYS = {"model", "max_tokens", "system_prompt"}
    VALID_MODELS = {"claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"}

    def __init__(self, max_history: int = 5) -> None:
        self.max_history = max_history
        self._history: list[ConfigVersion] = []
        self._current_version = 0

    def _validate(self, cfg: dict) -> list[str]:
        errors = []
        missing = self.REQUIRED_KEYS - cfg.keys()
        if missing:
            errors.append(f"Missing keys: {missing}")
            return errors
        if cfg["model"] not in self.VALID_MODELS:
            errors.append(f"Invalid model: {cfg['model']}")
        if not isinstance(cfg["max_tokens"], int) or not (1 <= cfg["max_tokens"] <= 8192):
            errors.append(f"Invalid max_tokens: {cfg['max_tokens']}")
        if not cfg.get("system_prompt", "").strip():
            errors.append("system_prompt is empty")
        return errors

    def apply(self, raw_config: dict) -> tuple[dict, bool]:
        """
        Returns (effective_config, was_rolled_back).
        """
        self._current_version += 1
        errors = self._validate(raw_config)
        version = ConfigVersion(
            version=self._current_version,
            config=raw_config,
            valid=not errors,
            errors=errors,
        )
        self._history.append(version)
        if len(self._history) > self.max_history:
            self._history.pop(0)

        if not errors:
            print(f"[config] v{self._current_version} applied successfully")
            return raw_config, False

        # Find most recent valid version
        valid_versions = [v for v in reversed(self._history[:-1]) if v.valid]
        if valid_versions:
            prev = valid_versions[0]
            print(f"[config] v{self._current_version} invalid: {errors}")
            print(f"[config] Rolling back to v{prev.version}")
            return prev.config, True

        raise RuntimeError(f"Config v{self._current_version} invalid and no rollback available: {errors}")

    def history_summary(self) -> list[dict]:
        return [
            {"version": v.version, "valid": v.valid, "errors": v.errors}
            for v in self._history
        ]


def demo() -> None:
    store = VersionedConfigStore()
    client = anthropic.Anthropic()

    configs = [
        {"model": "claude-haiku-4-5-20251001", "max_tokens": 128, "system_prompt": "You are helpful."},
        {"model": "claude-haiku-4-5-20251001", "max_tokens": 256, "system_prompt": "You are concise."},
        {"model": "BAD_MODEL", "max_tokens": 256, "system_prompt": "Bad config"},         # invalid
        {"model": "claude-haiku-4-5-20251001", "max_tokens": "not_an_int", "system_prompt": "X"},  # invalid
    ]

    for cfg in configs:
        try:
            effective, rolled_back = store.apply(cfg)
            resp = client.messages.create(
                model=effective["model"],
                max_tokens=effective["max_tokens"],
                system=effective["system_prompt"],
                messages=[{"role": "user", "content": "Hello"}],
            )
            tag = " [ROLLED BACK]" if rolled_back else ""
            print(f"Response{tag}: {resp.content[0].text[:50]}")
        except RuntimeError as e:
            print(f"Error: {e}")

    print("\nHistory:", store.history_summary())


if __name__ == "__main__":
    demo()

# Expected Token Savings: No extra tokens; validation catches bad configs before any API call
# Environment: Agents supporting multiple config versions with audit trail requirements
```

---

### Option 3: Async Hot-Reload with Atomic Swap

```python
import anthropic
import asyncio
import json
import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LiveConfig:
    model: str
    max_tokens: int
    system_prompt: str
    _hash: str = ""

    def __post_init__(self) -> None:
        self._hash = hashlib.md5(f"{self.model}{self.max_tokens}{self.system_prompt}".encode()).hexdigest()[:8]

    def validate(self) -> list[str]:
        errors = []
        valid = {"claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"}
        if self.model not in valid:
            errors.append(f"model '{self.model}' not in {valid}")
        if not isinstance(self.max_tokens, int) or self.max_tokens < 1:
            errors.append(f"max_tokens must be positive int, got {self.max_tokens!r}")
        if len(self.system_prompt) < 5:
            errors.append("system_prompt too short")
        return errors


class AsyncHotReloadConfig:
    """
    Watches a config file for changes.
    On change: validate → atomically swap → or roll back.
    Never drops a request during the swap.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._current: LiveConfig | None = None
        self._last_hash: str = ""
        self._lock = asyncio.Lock()
        self._reload_count = 0
        self._rollback_count = 0

    async def _read_and_validate(self) -> tuple[LiveConfig | None, list[str]]:
        try:
            raw = json.loads(self._path.read_text())
            cfg = LiveConfig(**raw)
            errors = cfg.validate()
            return (cfg, errors) if not errors else (None, errors)
        except Exception as e:
            return None, [str(e)]

    async def initialize(self) -> None:
        cfg, errors = await self._read_and_validate()
        if errors:
            raise RuntimeError(f"Initial config invalid: {errors}")
        async with self._lock:
            self._current = cfg
        print(f"[config] Initialized (hash={cfg._hash})")

    async def reload(self) -> bool:
        """Returns True if reload succeeded, False if rolled back."""
        new_cfg, errors = await self._read_and_validate()
        if errors:
            print(f"[config] Reload rejected: {errors}")
            self._rollback_count += 1
            return False

        async with self._lock:
            old_hash = self._current._hash if self._current else "none"
            if new_cfg._hash == old_hash:
                return True  # no change
            self._current = new_cfg
            self._reload_count += 1

        print(f"[config] Hot-reloaded: {old_hash} → {new_cfg._hash}")
        return True

    async def get(self) -> LiveConfig:
        async with self._lock:
            if self._current is None:
                raise RuntimeError("Config not initialized")
            return self._current

    async def watch(self, interval: float = 2.0) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.reload()


async def serve_requests(config_mgr: AsyncHotReloadConfig, n: int = 5) -> None:
    client = anthropic.AsyncAnthropic()
    for i in range(n):
        cfg = await config_mgr.get()
        resp = await client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=cfg.system_prompt,
            messages=[{"role": "user", "content": f"Request {i}"}],
        )
        print(f"[req {i}] model={cfg.model} → {resp.content[0].text[:40]}")
        await asyncio.sleep(0.3)
    await client.close()


async def main() -> None:
    cfg_path = "/tmp/live_config.json"
    Path(cfg_path).write_text(json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 64,
        "system_prompt": "You are helpful.",
    }))

    mgr = AsyncHotReloadConfig(cfg_path)
    await mgr.initialize()

    # Simulate a bad config write mid-run
    async def inject_bad_config() -> None:
        await asyncio.sleep(0.5)
        Path(cfg_path).write_text(json.dumps({"model": "INVALID", "max_tokens": 64, "system_prompt": "bad"}))
        await mgr.reload()
        await asyncio.sleep(0.5)
        # Restore good config
        Path(cfg_path).write_text(json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 64, "system_prompt": "Restored."}))
        await mgr.reload()

    await asyncio.gather(serve_requests(mgr, 6), inject_bad_config())
    print(f"Reloads: {mgr._reload_count}, Rollbacks: {mgr._rollback_count}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; atomic swap guarantees in-flight requests use valid config
# Environment: Async servers requiring zero-downtime config updates
```

---

### Option 4: Schema-Validated Config with Migration Support

```python
import anthropic
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_SCHEMA_V1 = {
    "required": ["model", "max_tokens", "system_prompt"],
    "types": {"model": str, "max_tokens": int, "system_prompt": str},
    "ranges": {"max_tokens": (1, 8192)},
    "enums": {"model": {"claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"}},
}


def validate_against_schema(cfg: dict, schema: dict) -> list[str]:
    errors = []
    for key in schema["required"]:
        if key not in cfg:
            errors.append(f"Missing required key: '{key}'")
    for key, typ in schema.get("types", {}).items():
        if key in cfg and not isinstance(cfg[key], typ):
            errors.append(f"'{key}' must be {typ.__name__}, got {type(cfg[key]).__name__}")
    for key, (lo, hi) in schema.get("ranges", {}).items():
        if key in cfg and isinstance(cfg[key], (int, float)):
            if not (lo <= cfg[key] <= hi):
                errors.append(f"'{key}'={cfg[key]} out of range [{lo}, {hi}]")
    for key, allowed in schema.get("enums", {}).items():
        if key in cfg and cfg[key] not in allowed:
            errors.append(f"'{key}'='{cfg[key]}' not in {allowed}")
    return errors


@dataclass
class ConfigCheckpoint:
    data: dict
    path: str
    schema_version: int = 1

    def save(self) -> None:
        Path(self.path).write_text(json.dumps({"schema_version": self.schema_version, **self.data}, indent=2))

    @classmethod
    def load(cls, path: str) -> "ConfigCheckpoint":
        raw = json.loads(Path(path).read_text())
        version = raw.pop("schema_version", 1)
        return cls(data=raw, path=path, schema_version=version)


class MigratingConfigLoader:
    """
    Validates config against versioned schema.
    Supports migration from older schema versions.
    Rolls back on failure.
    """

    MIGRATIONS: dict[int, Any] = {
        # v1 → v2 would go here
    }

    def __init__(self, config_path: str, checkpoint_path: str) -> None:
        self.config_path = config_path
        self.checkpoint_path = checkpoint_path
        self._active: dict | None = None

    def _migrate(self, cfg: dict, from_version: int) -> dict:
        version = from_version
        while version in self.MIGRATIONS:
            cfg = self.MIGRATIONS[version](cfg)
            version += 1
        return cfg

    def load(self) -> dict:
        raw = json.loads(Path(self.config_path).read_text())
        schema_version = raw.pop("schema_version", 1)
        migrated = self._migrate(raw, schema_version)
        errors = validate_against_schema(migrated, CONFIG_SCHEMA_V1)

        if errors:
            print(f"[config] Invalid: {errors}")
            if Path(self.checkpoint_path).exists():
                checkpoint = ConfigCheckpoint.load(self.checkpoint_path)
                print("[config] Rolling back to checkpoint")
                self._active = checkpoint.data
                return checkpoint.data
            raise ValueError(f"Config invalid and no checkpoint: {errors}")

        # Save checkpoint
        checkpoint = ConfigCheckpoint(data=migrated, path=self.checkpoint_path)
        checkpoint.save()
        self._active = migrated
        print(f"[config] Loaded v{schema_version} (model={migrated['model']})")
        return migrated


def demo() -> None:
    config_path = "/tmp/schema_config.json"
    checkpoint_path = "/tmp/schema_config.checkpoint.json"
    client = anthropic.Anthropic()

    # Write valid config
    Path(config_path).write_text(json.dumps({
        "schema_version": 1,
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 128,
        "system_prompt": "You are a helpful assistant.",
    }))

    loader = MigratingConfigLoader(config_path, checkpoint_path)
    cfg = loader.load()
    resp = client.messages.create(
        model=cfg["model"], max_tokens=cfg["max_tokens"],
        system=cfg["system_prompt"],
        messages=[{"role": "user", "content": "Hello"}],
    )
    print("Valid config response:", resp.content[0].text[:60])

    # Write invalid config
    Path(config_path).write_text(json.dumps({
        "schema_version": 1,
        "model": "gpt-99",
        "max_tokens": -1,
        "system_prompt": "",
    }))

    cfg = loader.load()  # should roll back
    print("After rollback, model:", cfg["model"])


if __name__ == "__main__":
    demo()

# Expected Token Savings: No extra tokens; schema validation catches type errors before any API call
# Environment: Agents with evolving config schemas that need forward/backward compatibility
```

---

### Option 5: Runtime Config Canary with Shadow Validation

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CanaryConfig:
    model: str
    max_tokens: int
    system_prompt: str


class CanaryConfigValidator:
    """
    Before committing a new config, tests it against a real API call
    (canary probe). Only promotes to active if the probe succeeds.
    """

    CANARY_PROBE = "Respond with exactly one word: OK"

    def __init__(self) -> None:
        self._active: CanaryConfig | None = None
        self._pending: CanaryConfig | None = None

    async def _probe(self, cfg: CanaryConfig) -> bool:
        """Returns True if the config produces a valid response."""
        try:
            client = anthropic.AsyncAnthropic()
            resp = await asyncio.wait_for(
                client.messages.create(
                    model=cfg.model,
                    max_tokens=10,
                    system=cfg.system_prompt,
                    messages=[{"role": "user", "content": self.CANARY_PROBE}],
                ),
                timeout=10.0,
            )
            await client.close()
            return bool(resp.content[0].text.strip())
        except Exception as e:
            print(f"[canary] Probe failed: {e}")
            return False

    async def propose(self, raw: dict) -> bool:
        """Returns True if config was promoted, False if rolled back."""
        try:
            candidate = CanaryConfig(**raw)
        except TypeError as e:
            print(f"[canary] Parse error: {e}")
            return False

        print(f"[canary] Probing candidate (model={candidate.model})")
        ok = await self._probe(candidate)
        if ok:
            self._active = candidate
            print("[canary] Promoted to active")
            return True
        else:
            print("[canary] Probe failed — keeping current config")
            return False

    def get_active(self) -> CanaryConfig:
        if self._active is None:
            raise RuntimeError("No active config")
        return self._active


async def main() -> None:
    validator = CanaryConfigValidator()
    client = anthropic.AsyncAnthropic()

    # Good config
    await validator.propose({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 128,
        "system_prompt": "You are helpful.",
    })

    cfg = validator.get_active()
    resp = await client.messages.create(
        model=cfg.model, max_tokens=cfg.max_tokens,
        system=cfg.system_prompt,
        messages=[{"role": "user", "content": "Say hello"}],
    )
    print("Active config response:", resp.content[0].text[:60])

    # Bad config — will probe and fail
    promoted = await validator.propose({
        "model": "claude-haiku-4-5-20251001",  # valid model but...
        "max_tokens": 0,  # will fail at API level
        "system_prompt": "bad",
    })
    print(f"Bad config promoted: {promoted}")
    print("Config after rejection:", validator.get_active().max_tokens)
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 1 probe token per config change; prevents costly downstream failures
# Environment: High-availability agents where a bad config reload causes cascading errors
```

---

### Option 6: Multi-Layer Config Validation with Alert on Rollback

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ConfigValidator:
    """Chain-of-responsibility validators for layered config checking."""

    def __init__(self) -> None:
        self._checks: list[Callable[[dict], ValidationResult]] = []

    def add_check(self, fn: Callable[[dict], ValidationResult]) -> "ConfigValidator":
        self._checks.append(fn)
        return self

    def validate(self, cfg: dict) -> ValidationResult:
        all_errors: list[str] = []
        all_warnings: list[str] = []
        for check in self._checks:
            result = check(cfg)
            all_errors.extend(result.errors)
            all_warnings.extend(result.warnings)
        return ValidationResult(valid=not all_errors, errors=all_errors, warnings=all_warnings)


def model_check(cfg: dict) -> ValidationResult:
    valid = {"claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"}
    if cfg.get("model") not in valid:
        return ValidationResult(False, errors=[f"model '{cfg.get('model')}' invalid"])
    return ValidationResult(True)


def token_check(cfg: dict) -> ValidationResult:
    mt = cfg.get("max_tokens")
    if not isinstance(mt, int) or not (1 <= mt <= 8192):
        return ValidationResult(False, errors=[f"max_tokens={mt!r} invalid"])
    if mt > 4096:
        return ValidationResult(True, warnings=[f"max_tokens={mt} is high; consider reducing for cost"])
    return ValidationResult(True)


def prompt_check(cfg: dict) -> ValidationResult:
    sp = cfg.get("system_prompt", "")
    if len(sp) < 10:
        return ValidationResult(False, errors=["system_prompt too short (< 10 chars)"])
    if len(sp) > 10000:
        return ValidationResult(True, warnings=["system_prompt is very long; may affect latency"])
    return ValidationResult(True)


class RollbackConfigManager:
    def __init__(self, config_path: str) -> None:
        self.config_path = Path(config_path)
        self._active: dict | None = None
        self._history: list[tuple[float, dict]] = []
        self._rollback_log: list[dict] = []

        self.validator = (
            ConfigValidator()
            .add_check(model_check)
            .add_check(token_check)
            .add_check(prompt_check)
        )
        self._alert_handlers: list[Callable[[dict], None]] = []

    def on_rollback(self, handler: Callable[[dict], None]) -> None:
        self._alert_handlers.append(handler)

    def _alert(self, event: dict) -> None:
        for h in self._alert_handlers:
            h(event)

    def load(self) -> dict:
        raw = json.loads(self.config_path.read_text())
        result = self.validator.validate(raw)

        if result.warnings:
            print(f"[config] Warnings: {result.warnings}")

        if result.valid:
            self._history.append((time.time(), raw))
            if len(self._history) > 10:
                self._history.pop(0)
            self._active = raw
            print(f"[config] Loaded OK (model={raw.get('model')})")
            return raw

        # Rollback
        event = {"timestamp": time.time(), "errors": result.errors, "rejected": raw}
        self._rollback_log.append(event)
        self._alert(event)

        if self._history:
            ts, prev = self._history[-1]
            print(f"[config] Rollback → config from {time.ctime(ts)}: {result.errors}")
            self._active = prev
            return prev

        raise RuntimeError(f"Config invalid, no history to roll back to: {result.errors}")


def demo() -> None:
    cfg_path = "/tmp/ml_config.json"

    def alert(event: dict) -> None:
        print(f"[ALERT] Config rejected at {time.ctime(event['timestamp'])}: {event['errors']}")

    manager = RollbackConfigManager(cfg_path)
    manager.on_rollback(alert)
    client = anthropic.Anthropic()

    for i, cfg in enumerate([
        {"model": "claude-haiku-4-5-20251001", "max_tokens": 128, "system_prompt": "You are helpful always."},
        {"model": "claude-haiku-4-5-20251001", "max_tokens": 6000, "system_prompt": "High token config."},  # warning
        {"model": "INVALID_MODEL", "max_tokens": 128, "system_prompt": "This is a bad model."},  # error
    ]):
        Path(cfg_path).write_text(json.dumps(cfg))
        active = manager.load()
        resp = client.messages.create(
            model=active["model"], max_tokens=min(active["max_tokens"], 64),
            system=active["system_prompt"],
            messages=[{"role": "user", "content": f"Request {i}"}],
        )
        print(f"[req {i}] model={active['model']} → {resp.content[0].text[:40]}")

    print(f"\nRollback log: {len(manager._rollback_log)} entries")


if __name__ == "__main__":
    demo()

# Expected Token Savings: No extra tokens; layered validation prevents silent degradation
# Environment: Production agents with on-call alerting requirements on config failures
```

---

## Comparison

| Option | Approach | Best For | Rollback Target | Complexity |
|--------|----------|----------|-----------------|------------|
| 1 | File backup copy before apply | Simple hot-reload scenarios | Last backup file | Low |
| 2 | In-memory version history (N versions) | Multi-version audit trail | Last valid in-memory version | Medium |
| 3 | Async atomic swap with hash change detect | Async zero-downtime hot reload | Previous in-memory config | Medium |
| 4 | Schema versioning + migration + checkpoint | Evolving configs with migration | Saved checkpoint file | Medium-High |
| 5 | Live canary probe before promotion | High-availability critical agents | Previous active config | High |
| 6 | Multi-layer validator + rollback alert | Production with alerting/on-call | Last valid in history | High |
