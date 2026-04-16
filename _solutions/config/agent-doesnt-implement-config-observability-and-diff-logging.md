---
title: "Agent Doesn't Implement Config Observability and Diff Logging"
description: "How to log configuration changes, produce human-readable diffs, and make config state observable so you can correlate behavior changes with config changes."
categories: [config]
difficulty: intermediate
---

When agent behavior changes unexpectedly, the first question is always "what changed?" Without config observability, you're left guessing. Logging config diffs at startup, on reload, and on every change gives you an audit trail that makes root-cause analysis fast.

## Solution 1: Startup Config Snapshot and Diff

At startup, load the current config, compare it to the last known snapshot, and log the diff.

```python
import json
import time
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = Path("/tmp/config_snapshot.json")


@dataclass
class ConfigSnapshot:
    config: dict
    timestamp: float = field(default_factory=time.time)
    checksum: str = ""

    def __post_init__(self):
        self.checksum = hashlib.sha256(
            json.dumps(self.config, sort_keys=True).encode()
        ).hexdigest()[:12]


def load_snapshot() -> ConfigSnapshot | None:
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        data = json.loads(SNAPSHOT_PATH.read_text())
        snap = ConfigSnapshot(config=data["config"], timestamp=data["timestamp"])
        snap.checksum = data.get("checksum", snap.checksum)
        return snap
    except Exception:
        return None


def save_snapshot(snapshot: ConfigSnapshot):
    SNAPSHOT_PATH.write_text(json.dumps({
        "config": snapshot.config,
        "timestamp": snapshot.timestamp,
        "checksum": snapshot.checksum,
    }, indent=2))


def compute_diff(old: dict, new: dict, path: str = "") -> list[dict]:
    diffs = []
    all_keys = set(old) | set(new)
    for key in sorted(all_keys):
        full_path = f"{path}.{key}" if path else key
        if key not in old:
            diffs.append({"path": full_path, "type": "added", "value": new[key]})
        elif key not in new:
            diffs.append({"path": full_path, "type": "removed", "old_value": old[key]})
        elif isinstance(old[key], dict) and isinstance(new[key], dict):
            diffs.extend(compute_diff(old[key], new[key], full_path))
        elif old[key] != new[key]:
            diffs.append({"path": full_path, "type": "changed", "old": old[key], "new": new[key]})
    return diffs


def log_config_diff(diffs: list[dict]):
    if not diffs:
        print("[config] No changes detected since last run.")
        return
    print(f"[config] {len(diffs)} change(s) detected since last snapshot:")
    for d in diffs:
        if d["type"] == "added":
            print(f"  + {d['path']}: {d['value']!r}")
        elif d["type"] == "removed":
            print(f"  - {d['path']}: {d['old_value']!r}")
        elif d["type"] == "changed":
            print(f"  ~ {d['path']}: {d['old']!r} → {d['new']!r}")


def observe_startup_config(current_config: dict):
    current = ConfigSnapshot(config=current_config)
    previous = load_snapshot()

    if previous is None:
        print(f"[config] First run — snapshot saved (checksum: {current.checksum})")
    elif previous.checksum == current.checksum:
        print(f"[config] Config unchanged (checksum: {current.checksum})")
    else:
        diffs = compute_diff(previous.config, current.config)
        log_config_diff(diffs)

    save_snapshot(current)
    return current


def main():
    # Simulate current config
    config = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "temperature": 0.7,
        "features": {"streaming": True, "caching": False},
    }
    observe_startup_config(config)

    # Simulate a config change on next run
    config["model"] = "claude-sonnet-4-6"
    config["max_tokens"] = 2048
    config["features"]["caching"] = True
    print("\n--- Simulated next startup ---")
    observe_startup_config(config)


main()
```

## Solution 2: Structured Config Change Event Log

Emit structured JSON log events for every config change, making them queryable by log aggregators.

```python
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_EVENT_LOG = Path("/tmp/config_events.jsonl")


@dataclass
class ConfigChangeEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: float = field(default_factory=time.time)
    change_type: str = ""       # startup | reload | runtime_override | rollback
    source: str = ""            # file | env | api | default
    key_path: str = ""
    old_value: Any = None
    new_value: Any = None
    triggered_by: str = ""      # process_id, user, scheduler, etc.

    def to_json(self) -> str:
        return json.dumps({
            "event_id": self.event_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "change_type": self.change_type,
            "source": self.source,
            "key_path": self.key_path,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "triggered_by": self.triggered_by,
        })


class ConfigEventLogger:
    def __init__(self, log_path: Path = CONFIG_EVENT_LOG):
        self._log = log_path

    def emit(self, event: ConfigChangeEvent):
        with self._log.open("a") as f:
            f.write(event.to_json() + "\n")
        self._print(event)

    def _print(self, e: ConfigChangeEvent):
        if e.old_value is None:
            print(f"[config:{e.change_type}] {e.key_path} = {e.new_value!r} (from {e.source})")
        else:
            print(f"[config:{e.change_type}] {e.key_path}: {e.old_value!r} → {e.new_value!r} (from {e.source})")

    def emit_startup(self, config: dict, source: str = "file"):
        for key, value in self._flatten(config).items():
            self.emit(ConfigChangeEvent(
                change_type="startup",
                source=source,
                key_path=key,
                new_value=value,
                triggered_by="process_start",
            ))

    def emit_change(self, key_path: str, old: Any, new: Any, source: str, triggered_by: str = "system"):
        self.emit(ConfigChangeEvent(
            change_type="runtime_override",
            source=source,
            key_path=key_path,
            old_value=old,
            new_value=new,
            triggered_by=triggered_by,
        ))

    @staticmethod
    def _flatten(d: dict, prefix: str = "") -> dict:
        result = {}
        for k, v in d.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                result.update(ConfigEventLogger._flatten(v, path))
            else:
                result[path] = v
        return result

    def query_recent(self, n: int = 10) -> list[dict]:
        if not self._log.exists():
            return []
        lines = self._log.read_text().splitlines()
        return [json.loads(l) for l in lines[-n:]]


def main():
    logger = ConfigEventLogger()
    config = {"model": "claude-haiku-4-5-20251001", "max_tokens": 1024}

    logger.emit_startup(config)
    logger.emit_change("model", "claude-haiku-4-5-20251001", "claude-sonnet-4-6", "env", "hot_reload")
    logger.emit_change("max_tokens", 1024, 2048, "api", "admin_user")

    print("\nRecent events:")
    for e in logger.query_recent(5):
        print(f"  {e['timestamp']} [{e['change_type']}] {e['key_path']}: {e.get('old_value')} → {e.get('new_value')}")


main()
```

## Solution 3: Human-Readable Config Diff Reporter

Generate a human-readable Markdown or plaintext diff report suitable for changelogs and incident reports.

```python
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPORT_PATH = Path("/tmp/config_diff_report.md")


@dataclass
class DiffLine:
    path: str
    change_type: str   # added | removed | changed | unchanged
    old_value: Any = None
    new_value: Any = None

    def to_markdown(self) -> str:
        if self.change_type == "added":
            return f"| `{self.path}` | ➕ Added | — | `{self.new_value}` |"
        if self.change_type == "removed":
            return f"| `{self.path}` | ➖ Removed | `{self.old_value}` | — |"
        if self.change_type == "changed":
            return f"| `{self.path}` | 🔄 Changed | `{self.old_value}` | `{self.new_value}` |"
        return f"| `{self.path}` | ✅ Unchanged | `{self.old_value}` | — |"


def flatten(d: dict, prefix: str = "") -> dict:
    result = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(flatten(v, path))
        else:
            result[path] = v
    return result


def diff_configs(old: dict, new: dict) -> list[DiffLine]:
    old_flat = flatten(old)
    new_flat = flatten(new)
    lines = []
    all_keys = sorted(set(old_flat) | set(new_flat))
    for key in all_keys:
        if key not in old_flat:
            lines.append(DiffLine(key, "added", new_value=new_flat[key]))
        elif key not in new_flat:
            lines.append(DiffLine(key, "removed", old_value=old_flat[key]))
        elif old_flat[key] != new_flat[key]:
            lines.append(DiffLine(key, "changed", old_flat[key], new_flat[key]))
    return lines


def generate_report(
    old_config: dict,
    new_config: dict,
    title: str = "Config Change Report",
    context: str = "",
) -> str:
    diffs = diff_configs(old_config, new_config)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    lines = [
        f"# {title}",
        f"**Generated:** {timestamp}",
        f"**Changes:** {len(diffs)}",
        "",
    ]
    if context:
        lines += [f"**Context:** {context}", ""]

    if diffs:
        lines += [
            "## Changed Configuration",
            "",
            "| Key | Change | Old Value | New Value |",
            "|---|---|---|---|",
        ]
        for d in diffs:
            lines.append(d.to_markdown())
    else:
        lines.append("_No configuration changes detected._")

    return "\n".join(lines)


def main():
    old = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "features": {"streaming": True, "caching": False},
        "timeout_seconds": 30,
    }
    new = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 2048,
        "features": {"streaming": True, "caching": True},
        "retry_count": 3,   # Added
        # timeout_seconds removed
    }

    report = generate_report(old, new, "Deployment Config Diff", "Staging → Production rollout")
    REPORT_PATH.write_text(report)
    print(report)


main()
```

## Solution 4: Environment Variable Config Tracker

Track which configuration values came from environment variables, files, or defaults, and log overrides.

```python
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConfigSource(Enum):
    DEFAULT = "default"
    FILE = "file"
    ENV = "env"
    RUNTIME = "runtime"


@dataclass
class TrackedValue:
    value: Any
    source: ConfigSource
    env_var: str | None = None
    set_at: float = field(default_factory=time.time)


class ObservableConfig:
    def __init__(self):
        self._store: dict[str, TrackedValue] = {}
        self._change_log: list[dict] = []

    def set_default(self, key: str, value: Any):
        self._store[key] = TrackedValue(value, ConfigSource.DEFAULT)

    def load_from_env(self, key: str, env_var: str, transform=None):
        raw = os.environ.get(env_var)
        if raw is not None:
            value = transform(raw) if transform else raw
            old = self._store.get(key)
            self._store[key] = TrackedValue(value, ConfigSource.ENV, env_var=env_var)
            if old and old.value != value:
                self._log_change(key, old.value, value, ConfigSource.ENV, f"env:{env_var}")

    def set_runtime(self, key: str, value: Any, triggered_by: str = "system"):
        old = self._store.get(key)
        self._store[key] = TrackedValue(value, ConfigSource.RUNTIME)
        self._log_change(key, old.value if old else None, value, ConfigSource.RUNTIME, triggered_by)

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._store.get(key)
        return entry.value if entry else default

    def _log_change(self, key: str, old: Any, new: Any, source: ConfigSource, actor: str):
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "key": key,
            "old_value": old,
            "new_value": new,
            "source": source.value,
            "actor": actor,
        }
        self._change_log.append(entry)
        print(f"[config:{source.value}] {key}: {old!r} → {new!r} (actor={actor})")

    def summary(self) -> dict:
        by_source = {}
        for key, tv in self._store.items():
            src = tv.source.value
            by_source.setdefault(src, []).append(key)
        return {
            "total_keys": len(self._store),
            "by_source": by_source,
            "changes": len(self._change_log),
        }

    def print_status(self):
        print("\n[config] Current state:")
        for key, tv in sorted(self._store.items()):
            src_label = f"env:{tv.env_var}" if tv.env_var else tv.source.value
            print(f"  {key} = {tv.value!r} [{src_label}]")


def main():
    cfg = ObservableConfig()

    # Set defaults
    cfg.set_default("model", "claude-haiku-4-5-20251001")
    cfg.set_default("max_tokens", 1024)
    cfg.set_default("timeout", 30)
    cfg.set_default("debug", False)

    # Override from environment
    os.environ["AGENT_MODEL"] = "claude-sonnet-4-6"
    os.environ["AGENT_MAX_TOKENS"] = "2048"
    cfg.load_from_env("model", "AGENT_MODEL")
    cfg.load_from_env("max_tokens", "AGENT_MAX_TOKENS", transform=int)

    # Runtime override
    cfg.set_runtime("debug", True, triggered_by="admin_user")

    cfg.print_status()
    print(f"\nSummary: {cfg.summary()}")


main()
```

## Solution 5: Config Hash Fingerprint for Reproducibility

Compute a deterministic fingerprint of the full config at every run to enable exact reproducibility tracking.

```python
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FINGERPRINT_LOG = Path("/tmp/config_fingerprints.jsonl")


@dataclass
class ConfigFingerprint:
    fingerprint: str
    timestamp: float = field(default_factory=time.time)
    config_keys: list[str] = field(default_factory=list)
    run_id: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "fingerprint": self.fingerprint,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "config_keys": self.config_keys,
            "run_id": self.run_id,
        })


def compute_fingerprint(config: dict) -> str:
    """Deterministic SHA-256 fingerprint of a config dict."""
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def record_fingerprint(config: dict, run_id: str = "") -> ConfigFingerprint:
    fp = ConfigFingerprint(
        fingerprint=compute_fingerprint(config),
        config_keys=sorted(config.keys()),
        run_id=run_id or f"run_{int(time.time())}",
    )
    with FINGERPRINT_LOG.open("a") as f:
        f.write(fp.to_json() + "\n")
    return fp


def find_runs_with_fingerprint(fingerprint: str) -> list[dict]:
    """Find all runs that used the same config fingerprint."""
    if not FINGERPRINT_LOG.exists():
        return []
    matches = []
    for line in FINGERPRINT_LOG.read_text().splitlines():
        try:
            entry = json.loads(line)
            if entry["fingerprint"] == fingerprint:
                matches.append(entry)
        except Exception:
            pass
    return matches


def main():
    base_config = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "temperature": 0.7,
    }

    # Run 1: standard config
    fp1 = record_fingerprint(base_config, "run_001")
    print(f"Run 001 fingerprint: {fp1.fingerprint}")

    # Run 2: identical config — same fingerprint
    fp2 = record_fingerprint(dict(base_config), "run_002")
    print(f"Run 002 fingerprint: {fp2.fingerprint} ({'SAME' if fp1.fingerprint == fp2.fingerprint else 'DIFFERENT'})")

    # Run 3: changed config — different fingerprint
    modified = dict(base_config)
    modified["max_tokens"] = 2048
    fp3 = record_fingerprint(modified, "run_003")
    print(f"Run 003 fingerprint: {fp3.fingerprint} ({'SAME' if fp1.fingerprint == fp3.fingerprint else 'DIFFERENT'})")

    # Look up all runs with same config as run 1
    matching = find_runs_with_fingerprint(fp1.fingerprint)
    print(f"\nRuns with fingerprint {fp1.fingerprint}: {[r['run_id'] for r in matching]}")


main()
```

## Solution 6: LLM-Assisted Config Change Impact Analyzer

When config changes, use an LLM to predict what behavior changes to expect and generate a checklist of things to verify.

```python
import asyncio
import json
from pathlib import Path
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


def flatten(d: dict, prefix: str = "") -> dict:
    result = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(flatten(v, path))
        else:
            result[path] = v
    return result


def compute_diff(old: dict, new: dict) -> list[dict]:
    old_flat, new_flat = flatten(old), flatten(new)
    diffs = []
    for key in sorted(set(old_flat) | set(new_flat)):
        if key not in old_flat:
            diffs.append({"path": key, "type": "added", "new": new_flat[key]})
        elif key not in new_flat:
            diffs.append({"path": key, "type": "removed", "old": old_flat[key]})
        elif old_flat[key] != new_flat[key]:
            diffs.append({"path": key, "type": "changed", "old": old_flat[key], "new": new_flat[key]})
    return diffs


async def analyze_config_impact(old_config: dict, new_config: dict, agent_description: str) -> str:
    diffs = compute_diff(old_config, new_config)
    if not diffs:
        return "No config changes — no impact analysis needed."

    diff_text = json.dumps(diffs, indent=2)
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Agent: {agent_description}\n\n"
                    f"Config changes:\n{diff_text}\n\n"
                    f"For each change:\n"
                    f"1. What behavior is likely to change?\n"
                    f"2. What should be verified or tested after deployment?\n\n"
                    f"Be concise and specific. Format as a checklist."
                ),
            }
        ],
    )
    return resp.content[0].text


async def main():
    old = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system_prompt": "Be concise.",
        "timeout": 10,
    }
    new = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 2048,
        "system_prompt": "Be thorough and detailed.",
        "timeout": 30,
        "retry_count": 3,
    }

    agent_desc = "Customer service agent that answers product questions."
    analysis = await analyze_config_impact(old, new, agent_desc)
    print("=== Config Impact Analysis ===")
    print(analysis)


asyncio.run(main())
```

## Comparison

| Solution | Storage | Real-time | LLM needed | Best for |
|---|---|---|---|---|
| **Startup snapshot diff** | File | On start | No | Detecting between-run changes |
| **Structured event log** | JSONL | Yes | No | Log aggregation (ELK, Datadog) |
| **Markdown diff reporter** | File | On change | No | Changelogs, incident reports |
| **Environment tracker** | Memory | Yes | No | Env-driven config debugging |
| **Fingerprint tracker** | JSONL | On run | No | Reproducibility tracking |
| **LLM impact analyzer** | None | On change | Yes (Haiku) | Pre-deployment risk assessment |

Start with **startup snapshot diff** (Solution 1) and **structured event log** (Solution 2) — together they give you both human-readable diffs and machine-queryable events with zero LLM cost. Add **LLM impact analyzer** (Solution 6) before production deployments.
