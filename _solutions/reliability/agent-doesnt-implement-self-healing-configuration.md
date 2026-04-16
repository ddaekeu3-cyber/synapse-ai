---
title: "Agent doesn't implement self-healing configuration"
description: "The agent loads config once at startup and never validates it again. Config drift—missing keys, wrong types, out-of-range values, schema version mismatches—goes undetected until the agent crashes or silently misbehaves."
difficulty: intermediate
category: reliability
tags: [configuration, self-healing, schema-validation, drift-detection, auto-repair]
---

## Problem

Production agents run for hours or days. Config files get edited by operators, mounted volumes are swapped, environment variables drift, and feature flags change behind the agent's back. Without ongoing config validation and repair, these silent mutations accumulate until the agent crashes with a `KeyError`, returns wrong results because a threshold is now `None`, or silently disables a safety feature because a boolean flipped to `"false"` (string).

Self-healing configuration means: validate on load, re-validate periodically, detect drift against a canonical snapshot, auto-repair fixable issues, and escalate un-fixable ones.

```python
# BAD: config loaded once, no type checking, no drift detection
config = json.load(open("config.json"))
model = config["model"]           # KeyError if missing
max_tokens = config["max_tokens"] # could be string "2048" — silent bug
```

## Solution 1: Schema-validated loader with default injection

Use `pydantic` to declare the config schema. Missing optional keys receive defaults; missing required keys raise immediately with a clear error.

```python
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings
from typing import Literal
import json
import os


class AgentConfig(BaseSettings):
    model: Literal[
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
    ] = "claude-sonnet-4-6"

    max_tokens: int = Field(default=4096, ge=1, le=200_000)
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    timeout_seconds: float = Field(default=30.0, gt=0)
    retry_attempts: int = Field(default=3, ge=1, le=10)
    enable_tools: bool = True
    system_prompt: str = Field(default="You are a helpful assistant.", min_length=10)

    model_config = {"env_prefix": "AGENT_", "env_file": ".env"}

    @field_validator("max_tokens", mode="before")
    @classmethod
    def coerce_max_tokens(cls, v):
        # Accept "2048" as well as 2048
        return int(v)

    @field_validator("temperature", mode="before")
    @classmethod
    def coerce_temperature(cls, v):
        return float(v)


def load_config(path: str | None = None) -> AgentConfig:
    """Load config from JSON file, environment variables, or both."""
    if path and os.path.exists(path):
        with open(path) as f:
            file_data = json.load(f)
        return AgentConfig(**file_data)
    return AgentConfig()  # falls back to env vars + defaults


# --- Usage ---
config = load_config("config.json")
print(f"Model: {config.model}, max_tokens: {config.max_tokens}")
```

## Solution 2: Periodic drift detector that compares live config to canonical snapshot

Capture the validated config at startup as the canonical snapshot. A background task re-reads the source every N seconds and reports any drift.

```python
import asyncio
import json
import os
import copy
import logging
from typing import Any, Callable, Awaitable

log = logging.getLogger(__name__)


class ConfigDriftDetector:
    def __init__(
        self,
        config_path: str,
        canonical: dict[str, Any],
        interval: float = 60.0,
        on_drift: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self.config_path = config_path
        self.canonical = copy.deepcopy(canonical)
        self.interval = interval
        self.on_drift = on_drift
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop(), name="config-drift-detector")

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self._check()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Config drift check failed: %s", e)

    async def _check(self):
        if not os.path.exists(self.config_path):
            log.warning("Config file missing: %s", self.config_path)
            return

        with open(self.config_path) as f:
            current = json.load(f)

        diffs = self._diff(self.canonical, current)
        if diffs:
            log.warning("Config drift detected: %s", diffs)
            if self.on_drift:
                await self.on_drift(diffs)

    @staticmethod
    def _diff(canonical: dict, current: dict) -> dict[str, dict]:
        diffs = {}
        all_keys = set(canonical) | set(current)
        for key in all_keys:
            if key not in current:
                diffs[key] = {"status": "removed", "was": canonical[key]}
            elif key not in canonical:
                diffs[key] = {"status": "added", "now": current[key]}
            elif canonical[key] != current[key]:
                diffs[key] = {"status": "changed", "was": canonical[key], "now": current[key]}
        return diffs


# --- Usage ---

async def on_drift(diffs: dict):
    log.error("ALERT: config drifted — %s", list(diffs.keys()))
    # Could trigger reload, alert, or rollback here


async def main():
    canonical = {"model": "claude-sonnet-4-6", "max_tokens": 4096, "temperature": 0.7}
    detector = ConfigDriftDetector(
        config_path="config.json",
        canonical=canonical,
        interval=30.0,
        on_drift=on_drift,
    )
    detector.start()
    await asyncio.sleep(120)
    detector.stop()


asyncio.run(main())
```

## Solution 3: Auto-repair pipeline for fixable config errors

Classify each config error as fixable (wrong type, missing optional key, out-of-range value) or fatal (missing required key, unknown schema version). Repair fixable ones automatically and write the repaired file back.

```python
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any


DEFAULTS = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 4096,
    "temperature": 0.7,
    "timeout_seconds": 30.0,
    "retry_attempts": 3,
    "enable_tools": True,
}

REQUIRED = {"model"}

RANGES = {
    "max_tokens": (1, 200_000),
    "temperature": (0.0, 1.0),
    "timeout_seconds": (1.0, 300.0),
    "retry_attempts": (1, 10),
}

TYPES = {
    "max_tokens": int,
    "temperature": float,
    "timeout_seconds": float,
    "retry_attempts": int,
    "enable_tools": bool,
    "model": str,
}


@dataclass
class RepairReport:
    repairs: list[str] = field(default_factory=list)
    fatal_errors: list[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return not self.fatal_errors


def repair_config(cfg: dict[str, Any]) -> tuple[dict[str, Any], RepairReport]:
    report = RepairReport()
    out = dict(cfg)

    # 1. Inject missing keys with defaults
    for key, default in DEFAULTS.items():
        if key not in out:
            if key in REQUIRED:
                report.fatal_errors.append(f"Required key '{key}' is missing")
            else:
                out[key] = default
                report.repairs.append(f"Injected default for '{key}': {default}")

    # 2. Coerce types
    for key, expected_type in TYPES.items():
        if key in out and not isinstance(out[key], expected_type):
            try:
                out[key] = expected_type(out[key])
                report.repairs.append(f"Coerced '{key}' to {expected_type.__name__}")
            except (ValueError, TypeError):
                report.fatal_errors.append(
                    f"Cannot coerce '{key}'={out[key]!r} to {expected_type.__name__}"
                )

    # 3. Clamp out-of-range values
    for key, (lo, hi) in RANGES.items():
        if key in out:
            clamped = max(lo, min(hi, out[key]))
            if clamped != out[key]:
                report.repairs.append(f"Clamped '{key}' from {out[key]} to {clamped}")
                out[key] = clamped

    return out, report


def load_and_repair(path: str) -> dict[str, Any]:
    with open(path) as f:
        raw = json.load(f)

    repaired, report = repair_config(raw)

    if report.fatal_errors:
        raise ValueError(f"Unrecoverable config errors: {report.fatal_errors}")

    if report.repairs:
        backup = f"{path}.bak.{int(time.time())}"
        shutil.copy2(path, backup)
        with open(path, "w") as f:
            json.dump(repaired, f, indent=2)
        print(f"Config repaired ({len(report.repairs)} fixes). Backup: {backup}")
        for r in report.repairs:
            print(f"  - {r}")

    return repaired


# --- Usage ---
# config = load_and_repair("config.json")
```

## Solution 4: Config health-check endpoint

Expose a `/health/config` endpoint that returns the current config validation status, last drift check time, and any active issues — useful for monitoring and alerting.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ValidationError


class AgentConfigSchema(BaseModel):
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    temperature: float = 0.7
    timeout_seconds: float = 30.0
    retry_attempts: int = 3


@dataclass
class ConfigHealthState:
    last_check_ts: float = 0.0
    last_check_ok: bool = True
    validation_errors: list[str] = field(default_factory=list)
    drift_keys: list[str] = field(default_factory=list)
    repair_count: int = 0
    config_path: str = "config.json"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["age_seconds"] = round(time.time() - self.last_check_ts, 1)
        d["status"] = "ok" if self.last_check_ok else "degraded"
        return d


_state = ConfigHealthState()
app = FastAPI()


async def validate_config_file(path: str) -> list[str]:
    try:
        with open(path) as f:
            raw = json.load(f)
        AgentConfigSchema(**raw)
        return []
    except FileNotFoundError:
        return [f"Config file not found: {path}"]
    except json.JSONDecodeError as e:
        return [f"Invalid JSON: {e}"]
    except ValidationError as e:
        return [err["msg"] for err in e.errors()]


@app.get("/health/config")
async def config_health():
    errors = await validate_config_file(_state.config_path)
    _state.last_check_ts = time.time()
    _state.last_check_ok = not errors
    _state.validation_errors = errors
    return _state.as_dict()


async def background_config_monitor(interval: float = 60.0):
    while True:
        await asyncio.sleep(interval)
        errors = await validate_config_file(_state.config_path)
        _state.last_check_ts = time.time()
        _state.last_check_ok = not errors
        _state.validation_errors = errors
        if errors:
            print(f"[CONFIG HEALTH] Issues detected: {errors}")
```

## Solution 5: Immutable versioned config with migration path

Pin the schema to a version number. On startup, detect the version and run registered migration functions to bring old configs up to the current schema.

```python
import json
import copy
from typing import Callable, Any

CURRENT_VERSION = 3

_migrations: dict[int, Callable[[dict], dict]] = {}


def migration(from_version: int):
    """Decorator to register a migration from version N to N+1."""
    def decorator(fn: Callable[[dict], dict]):
        _migrations[from_version] = fn
        return fn
    return decorator


@migration(1)
def v1_to_v2(cfg: dict) -> dict:
    """Rename 'timeout' → 'timeout_seconds'."""
    cfg = copy.deepcopy(cfg)
    if "timeout" in cfg:
        cfg["timeout_seconds"] = cfg.pop("timeout")
    cfg["schema_version"] = 2
    return cfg


@migration(2)
def v2_to_v3(cfg: dict) -> dict:
    """Add 'retry_attempts' with default 3; split 'model_name' → 'model'."""
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("retry_attempts", 3)
    if "model_name" in cfg:
        cfg["model"] = cfg.pop("model_name")
    cfg["schema_version"] = 3
    return cfg


def migrate(cfg: dict[str, Any]) -> dict[str, Any]:
    version = cfg.get("schema_version", 1)
    while version < CURRENT_VERSION:
        if version not in _migrations:
            raise ValueError(f"No migration registered for v{version} → v{version+1}")
        cfg = _migrations[version](cfg)
        version = cfg.get("schema_version", version + 1)
        print(f"Migrated config to v{version}")
    return cfg


def load_versioned_config(path: str) -> dict[str, Any]:
    with open(path) as f:
        raw = json.load(f)
    migrated = migrate(raw)
    # Write back migrated version
    with open(path, "w") as f:
        json.dump(migrated, f, indent=2)
    return migrated


# --- Usage ---
# config = load_versioned_config("config.json")
```

## Solution 6: LLM-assisted config repair for complex semantic constraints

When purely mechanical repair fails (e.g., conflicting flags, semantically invalid combinations), ask the LLM to propose a fix given the schema documentation and the current invalid config.

```python
import json
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

CONFIG_SCHEMA_DOC = """
Agent configuration schema (v3):
- model: one of ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"]
- max_tokens: integer 1–200000. For haiku use ≤4096, for opus use ≤16384 for cost reasons.
- temperature: float 0.0–1.0. Should be 0.0 for deterministic tasks, 0.7 for creative.
- timeout_seconds: float 1–300. Must be > 10 * retry_attempts to allow meaningful retries.
- retry_attempts: integer 1–10.
- enable_streaming: bool. Must be False when max_tokens > 100000 (network stability).
- enable_tools: bool. Cannot be True when model is haiku and max_tokens > 2048.
"""


async def llm_repair_config(broken_config: dict, errors: list[str]) -> dict:
    prompt = f"""The following agent configuration has validation errors.

Config:
{json.dumps(broken_config, indent=2)}

Errors:
{chr(10).join(f'- {e}' for e in errors)}

Schema documentation:
{CONFIG_SCHEMA_DOC}

Return ONLY a valid JSON object with the repaired configuration. Do not include any explanation."""

    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = message.content[0].text.strip()
    # Extract JSON if wrapped in markdown
    if "```" in raw_text:
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]

    repaired = json.loads(raw_text)
    return repaired


async def smart_repair_pipeline(config_path: str) -> dict:
    with open(config_path) as f:
        cfg = json.load(f)

    # Run basic mechanical repair first
    from solution3 import repair_config  # hypothetical import
    # mechanical_fixed, report = repair_config(cfg)

    # For complex semantic errors, fall back to LLM
    semantic_errors = [
        "enable_tools=True conflicts with haiku model and max_tokens > 2048",
    ]

    if semantic_errors:
        print("Calling LLM for complex config repair...")
        repaired = await llm_repair_config(cfg, semantic_errors)
        print(f"LLM proposed repair: {json.dumps(repaired, indent=2)}")
        return repaired

    return cfg


# asyncio.run(smart_repair_pipeline("config.json"))
```

## Comparison

| Approach | Detects at startup | Detects drift | Auto-repairs | Cross-process | LLM-assisted |
|---|---|---|---|---|---|
| Schema-validated loader | Yes | No | Defaults only | No | No |
| Periodic drift detector | No | Yes | No | No | No |
| Auto-repair pipeline | Yes | No | Yes (fixable) | No | No |
| Health-check endpoint | On demand | Background | No | Via HTTP | No |
| Versioned migration | Yes | No | Yes (schema bump) | No | No |
| LLM-assisted repair | Yes | No | Yes (semantic) | No | Yes |

**Recommendation**: Combine **schema-validated loader** (Solution 1) for startup correctness, **periodic drift detector** (Solution 2) for runtime monitoring, and **auto-repair pipeline** (Solution 3) for mechanical fixes. Add **LLM-assisted repair** (Solution 6) only for configs with complex semantic constraints that can't be expressed as simple range checks.
