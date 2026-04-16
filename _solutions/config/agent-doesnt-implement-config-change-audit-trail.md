---
layout: solution
title: "Agent Doesn't Implement Config Change Audit Trail"
category: config
description: "Record every configuration change with who changed it, what changed, when, and why — enabling rollback, compliance auditing, and incident root-cause analysis."
tags: [config, audit-trail, changelog, compliance, rollback, observability]
---

# Agent Doesn't Implement Config Change Audit Trail

When an agent misbehaves after a config update, there's no record of what changed. Teams spend hours manually diffing configs and asking "who changed the model from haiku to opus at 2am?" An audit trail records every config mutation as an immutable log entry with the old value, new value, timestamp, and author — enabling instant rollback and compliance reporting.

## Option 1: File-Based JSON Audit Log

```python
import json
import os
import time
import copy
from pathlib import Path
import anthropic

AUDIT_LOG_PATH = Path("config_audit.jsonl")
CONFIG_PATH = Path("agent_config.json")

DEFAULT_CONFIG = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 512,
    "temperature": 1.0,
    "system_prompt": "You are a helpful assistant.",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def audit_change(key: str, old_value, new_value, author: str = "system", reason: str = "") -> None:
    entry = {
        "ts": time.time(),
        "key": key,
        "old": old_value,
        "new": new_value,
        "author": author,
        "reason": reason,
    }
    with AUDIT_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[AUDIT] {author} changed '{key}': {old_value!r} → {new_value!r}")


def update_config(updates: dict, author: str = "system", reason: str = "") -> dict:
    config = load_config()
    for key, new_value in updates.items():
        old_value = config.get(key)
        if old_value != new_value:
            audit_change(key, old_value, new_value, author=author, reason=reason)
            config[key] = new_value
    save_config(config)
    return config


def print_audit_log(last_n: int = 10) -> None:
    if not AUDIT_LOG_PATH.exists():
        print("No audit log found.")
        return
    lines = AUDIT_LOG_PATH.read_text().strip().split("\n")
    entries = [json.loads(l) for l in lines[-last_n:] if l.strip()]
    print(f"\n=== Last {len(entries)} config changes ===")
    for e in entries:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))
        print(f"  {ts} [{e['author']}] {e['key']}: {e['old']!r} → {e['new']!r}  ({e.get('reason', '')})")


def run_agent(user_input: str) -> str:
    config = load_config()
    client = anthropic.Anthropic()
    r = client.messages.create(
        model=config["model"],
        max_tokens=config["max_tokens"],
        system=config["system_prompt"],
        messages=[{"role": "user", "content": user_input}],
    )
    return r.content[0].text


if __name__ == "__main__":
    update_config({"model": "claude-haiku-4-5-20251001"}, author="admin", reason="cost reduction")
    update_config({"max_tokens": 256, "system_prompt": "Be concise."}, author="ops-bot", reason="performance tuning")

    print(run_agent("What is asyncio?"))
    print_audit_log()

# Expected Token Savings: N/A (compliance pattern); audit log enables instant rollback on incidents
# Environment: Python 3.9+; rotate AUDIT_LOG_PATH daily in production for bounded file sizes
```

## Option 2: SQLite Audit Database with Diff and Rollback

```python
import json
import sqlite3
import time
import copy
import anthropic

DB_PATH = "config_audit.db"
client = anthropic.Anthropic()

CURRENT_CONFIG: dict = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 512,
    "system_prompt": "You are a helpful assistant.",
    "temperature": 1.0,
}


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, key TEXT, old_value TEXT, new_value TEXT,
            author TEXT, reason TEXT, snapshot TEXT
        )
    """)
    conn.commit()
    return conn


def record_change(
    conn: sqlite3.Connection,
    key: str,
    old_value,
    new_value,
    author: str,
    reason: str,
    snapshot: dict,
) -> None:
    conn.execute(
        "INSERT INTO config_audit VALUES (NULL,?,?,?,?,?,?,?)",
        (time.time(), key, json.dumps(old_value), json.dumps(new_value),
         author, reason, json.dumps(snapshot)),
    )
    conn.commit()


def update_config(
    conn: sqlite3.Connection,
    updates: dict,
    author: str = "system",
    reason: str = "",
) -> None:
    for key, new_value in updates.items():
        old_value = CURRENT_CONFIG.get(key)
        if old_value != new_value:
            CURRENT_CONFIG[key] = new_value
            record_change(conn, key, old_value, new_value, author, reason, copy.deepcopy(CURRENT_CONFIG))
            print(f"[AUDIT] {key}: {old_value!r} → {new_value!r} by {author}")


def rollback_to(conn: sqlite3.Connection, audit_id: int) -> dict:
    """Restore config to the snapshot captured at a specific audit entry."""
    row = conn.execute("SELECT snapshot FROM config_audit WHERE id=?", (audit_id,)).fetchone()
    if not row:
        raise ValueError(f"Audit entry {audit_id} not found")
    snapshot = json.loads(row[0])
    CURRENT_CONFIG.clear()
    CURRENT_CONFIG.update(snapshot)
    print(f"[ROLLBACK] Restored to snapshot from audit entry #{audit_id}")
    return snapshot


def audit_report(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, ts, key, old_value, new_value, author, reason FROM config_audit ORDER BY ts DESC LIMIT 10"
    ).fetchall()
    print("\n=== Config Audit Report ===")
    for row in rows:
        ts = time.strftime("%H:%M:%S", time.localtime(row[1]))
        old = json.loads(row[3])
        new = json.loads(row[4])
        print(f"  #{row[0]} {ts} [{row[5]}] {row[2]}: {old!r} → {new!r} | {row[6]}")


def run_agent(prompt: str) -> str:
    r = client.messages.create(
        model=CURRENT_CONFIG["model"],
        max_tokens=CURRENT_CONFIG["max_tokens"],
        system=CURRENT_CONFIG["system_prompt"],
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


if __name__ == "__main__":
    conn = init_db()
    update_config(conn, {"max_tokens": 256}, author="perf-bot", reason="reduce latency")
    update_config(conn, {"model": "claude-haiku-4-5-20251001"}, author="admin", reason="cost optimization")

    print(run_agent("Explain audit trails in one sentence."))
    audit_report(conn)

    # Rollback to entry #1
    rollback_to(conn, 1)
    print(f"\n[CONFIG] After rollback: {CURRENT_CONFIG}")
    conn.close()

# Expected Token Savings: Rollback restores known-good config instantly; no manual guessing
# Environment: Python 3.9+, SQLite3; store DB on persistent volume for cross-restart audit history
```

## Option 3: Immutable Versioned Config with Changelog

```python
import json
import hashlib
import time
import anthropic
from dataclasses import dataclass, field
from typing import Any

client = anthropic.Anthropic()


@dataclass
class ConfigVersion:
    version: int
    config: dict
    timestamp: float
    author: str
    reason: str
    parent_hash: str
    config_hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.config_hash = hashlib.sha256(
            json.dumps(self.config, sort_keys=True).encode()
        ).hexdigest()[:12]


class VersionedConfig:
    def __init__(self, initial: dict) -> None:
        v0 = ConfigVersion(
            version=0, config=initial, timestamp=time.time(),
            author="system", reason="initial", parent_hash="",
        )
        self._history: list[ConfigVersion] = [v0]

    @property
    def current(self) -> dict:
        return self._history[-1].config.copy()

    @property
    def version(self) -> int:
        return self._history[-1].version

    def update(self, updates: dict, author: str = "system", reason: str = "") -> ConfigVersion:
        current = self.current
        new_config = {**current, **updates}
        if new_config == current:
            return self._history[-1]

        new_version = ConfigVersion(
            version=self.version + 1,
            config=new_config,
            timestamp=time.time(),
            author=author,
            reason=reason,
            parent_hash=self._history[-1].config_hash,
        )
        self._history.append(new_version)
        print(f"[VERSION] v{new_version.version} by {author}: {list(updates.keys())} [{new_version.config_hash}]")
        return new_version

    def diff(self, v1: int, v2: int) -> dict[str, dict[str, Any]]:
        cfg1 = self._history[v1].config
        cfg2 = self._history[v2].config
        all_keys = set(cfg1) | set(cfg2)
        return {
            k: {"before": cfg1.get(k), "after": cfg2.get(k)}
            for k in all_keys if cfg1.get(k) != cfg2.get(k)
        }

    def rollback(self, version: int, author: str = "system") -> None:
        target = self._history[version]
        self.update(target.config, author=author, reason=f"rollback to v{version}")

    def changelog(self) -> None:
        print("\n=== Config Changelog ===")
        for v in reversed(self._history):
            ts = time.strftime("%H:%M:%S", time.localtime(v.timestamp))
            print(f"  v{v.version} [{ts}] {v.author}: {v.reason} ({v.config_hash})")


CONFIG = VersionedConfig({
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 512,
    "system_prompt": "You are a helpful assistant.",
})


def run_agent(prompt: str) -> str:
    cfg = CONFIG.current
    r = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        system=cfg["system_prompt"],
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


if __name__ == "__main__":
    CONFIG.update({"max_tokens": 128}, author="ops", reason="reduce cost")
    CONFIG.update({"system_prompt": "Be terse."}, author="product", reason="user feedback")
    CONFIG.update({"model": "claude-haiku-4-5-20251001"}, author="admin", reason="pin stable model")

    print(run_agent("What is version control?"))

    print("\nDiff v0 → v3:")
    for key, change in CONFIG.diff(0, CONFIG.version).items():
        print(f"  {key}: {change['before']!r} → {change['after']!r}")

    CONFIG.changelog()
    CONFIG.rollback(0, author="incident-responder")
    print(f"\nAfter rollback: model={CONFIG.current['model']}")

# Expected Token Savings: Immutable versions enable atomic rollback; diff shows exactly what changed
# Environment: Python 3.9+; persist history to SQLite for cross-restart access
```

## Option 4: Config Watcher with Async Change Detection

```python
import asyncio
import json
import hashlib
import time
import anthropic
from pathlib import Path

CONFIG_FILE = Path("watched_config.json")
client = anthropic.AsyncAnthropic()
AUDIT_LOG: list[dict] = []

_current_hash = ""
_current_config: dict = {}


def file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.md5(path.read_bytes()).hexdigest()


def load_config_from_file() -> dict:
    if not CONFIG_FILE.exists():
        default = {"model": "claude-haiku-4-5-20251001", "max_tokens": 256}
        CONFIG_FILE.write_text(json.dumps(default, indent=2))
        return default
    return json.loads(CONFIG_FILE.read_text())


def detect_changes(old: dict, new: dict) -> list[dict]:
    changes = []
    for key in set(old) | set(new):
        if old.get(key) != new.get(key):
            changes.append({"key": key, "old": old.get(key), "new": new.get(key)})
    return changes


async def config_watcher(interval: float = 2.0, stop_event: asyncio.Event | None = None) -> None:
    global _current_hash, _current_config

    _current_config = load_config_from_file()
    _current_hash = file_hash(CONFIG_FILE)

    while not (stop_event and stop_event.is_set()):
        await asyncio.sleep(interval)
        new_hash = file_hash(CONFIG_FILE)

        if new_hash != _current_hash:
            new_config = load_config_from_file()
            changes = detect_changes(_current_config, new_config)

            for change in changes:
                entry = {
                    "ts": time.time(),
                    "key": change["key"],
                    "old": change["old"],
                    "new": change["new"],
                    "source": "file_watch",
                }
                AUDIT_LOG.append(entry)
                print(f"[WATCH] Config changed: {change['key']}: {change['old']!r} → {change['new']!r}")

            _current_config = new_config
            _current_hash = new_hash


async def run_agent(prompt: str) -> str:
    cfg = _current_config or load_config_from_file()
    r = await client.messages.create(
        model=cfg.get("model", "claude-haiku-4-5-20251001"),
        max_tokens=cfg.get("max_tokens", 256),
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


async def main() -> None:
    global _current_config
    _current_config = load_config_from_file()
    stop = asyncio.Event()

    # Start watcher in background
    watcher_task = asyncio.create_task(config_watcher(interval=1.0, stop_event=stop))

    # Simulate config change mid-run
    async def simulate_change() -> None:
        await asyncio.sleep(1.5)
        new_cfg = {"model": "claude-haiku-4-5-20251001", "max_tokens": 128}
        CONFIG_FILE.write_text(json.dumps(new_cfg, indent=2))
        print("[TEST] Wrote new config to file")

    await asyncio.gather(
        simulate_change(),
        run_agent("What is configuration management?"),
    )

    await asyncio.sleep(2)
    stop.set()
    watcher_task.cancel()

    print(f"\n[AUDIT] {len(AUDIT_LOG)} change(s) detected:")
    for entry in AUDIT_LOG:
        ts = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
        print(f"  {ts} {entry['key']}: {entry['old']!r} → {entry['new']!r}")


asyncio.run(main())

# Expected Token Savings: File watcher detects external config changes; audit log catches surprise updates
# Environment: Python 3.11+; replace file polling with inotify/fsevents for production efficiency
```

## Option 5: Multi-Env Config with Promotion Audit

```python
import json
import time
import copy
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

ENVIRONMENTS = ["dev", "staging", "prod"]


@dataclass
class EnvConfig:
    env: str
    config: dict
    version: int = 0
    last_updated: float = field(default_factory=time.time)
    last_author: str = "system"


@dataclass
class PromotionEvent:
    from_env: str
    to_env: str
    keys_promoted: list[str]
    author: str
    reason: str
    timestamp: float = field(default_factory=time.time)
    config_snapshot: dict = field(default_factory=dict)


class MultiEnvConfigManager:
    def __init__(self) -> None:
        base = {"model": "claude-haiku-4-5-20251001", "max_tokens": 512, "debug": False}
        self._envs: dict[str, EnvConfig] = {
            env: EnvConfig(env=env, config=copy.deepcopy(base))
            for env in ENVIRONMENTS
        }
        self._promotions: list[PromotionEvent] = []

    def update(self, env: str, updates: dict, author: str = "system", reason: str = "") -> None:
        cfg = self._envs[env]
        for key, val in updates.items():
            old = cfg.config.get(key)
            if old != val:
                cfg.config[key] = val
                print(f"[ENV:{env}] {key}: {old!r} → {val!r} by {author}")
        cfg.version += 1
        cfg.last_updated = time.time()
        cfg.last_author = author

    def promote(self, from_env: str, to_env: str, keys: list[str] | None, author: str, reason: str) -> None:
        src = self._envs[from_env].config
        dst = self._envs[to_env]
        keys_to_promote = keys or list(src.keys())

        promoted = []
        for key in keys_to_promote:
            if src.get(key) != dst.config.get(key):
                dst.config[key] = copy.deepcopy(src[key])
                promoted.append(key)

        if promoted:
            event = PromotionEvent(
                from_env=from_env, to_env=to_env, keys_promoted=promoted,
                author=author, reason=reason,
                config_snapshot=copy.deepcopy(dst.config),
            )
            self._promotions.append(event)
            print(f"[PROMOTE] {from_env}→{to_env}: {promoted} by {author}")

    def audit_report(self) -> None:
        print("\n=== Promotion Audit ===")
        for p in self._promotions:
            ts = time.strftime("%H:%M:%S", time.localtime(p.timestamp))
            print(f"  {ts} {p.from_env}→{p.to_env} by {p.author}: {p.keys_promoted} | {p.reason}")

    def get_config(self, env: str) -> dict:
        return self._envs[env].config.copy()


MGR = MultiEnvConfigManager()


def run_agent(prompt: str, env: str = "prod") -> str:
    cfg = MGR.get_config(env)
    r = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


if __name__ == "__main__":
    # Dev changes
    MGR.update("dev", {"max_tokens": 128, "debug": True}, author="dev-team", reason="testing")
    MGR.update("dev", {"model": "claude-haiku-4-5-20251001"}, author="dev-team", reason="pin model")

    # Promote dev → staging (specific keys)
    MGR.promote("dev", "staging", keys=["model", "max_tokens"], author="ci-bot", reason="staging deploy")

    # Promote staging → prod (all keys)
    MGR.promote("staging", "prod", keys=None, author="release-bot", reason="v1.2 release")

    print(run_agent("What is config promotion?", env="prod"))
    MGR.audit_report()

# Expected Token Savings: Promotion audit catches what reached prod and when; rollback by re-promoting
# Environment: Python 3.9+; replace in-memory state with SQLite for persistence across restarts
```

## Option 6: Signed Config Changes with Integrity Verification

```python
import json
import hashlib
import hmac
import time
import anthropic

SIGNING_KEY = b"agent-config-signing-key-change-in-prod"
client = anthropic.Anthropic()

_config: dict = {"model": "claude-haiku-4-5-20251001", "max_tokens": 512}
_signed_log: list[dict] = []


def sign_entry(entry: dict) -> str:
    payload = json.dumps(entry, sort_keys=True).encode()
    return hmac.new(SIGNING_KEY, payload, hashlib.sha256).hexdigest()


def create_audit_entry(key: str, old_val, new_val, author: str, reason: str) -> dict:
    entry = {
        "ts": time.time(),
        "key": key,
        "old": old_val,
        "new": new_val,
        "author": author,
        "reason": reason,
        "prev_sig": _signed_log[-1]["sig"] if _signed_log else "",
    }
    entry["sig"] = sign_entry({k: v for k, v in entry.items() if k != "sig"})
    return entry


def verify_chain() -> tuple[bool, str]:
    for i, entry in enumerate(_signed_log):
        expected_sig = sign_entry({k: v for k, v in entry.items() if k != "sig"})
        if entry["sig"] != expected_sig:
            return False, f"Signature mismatch at entry {i}"
        if i > 0 and entry["prev_sig"] != _signed_log[i-1]["sig"]:
            return False, f"Chain broken at entry {i}"
    return True, "Chain valid"


def update_config(key: str, value, author: str = "system", reason: str = "") -> None:
    old = _config.get(key)
    if old == value:
        return
    _config[key] = value
    entry = create_audit_entry(key, old, value, author, reason)
    _signed_log.append(entry)
    print(f"[SIGNED] {key}: {old!r} → {value!r} | sig={entry['sig'][:8]}...")


def print_verified_log() -> None:
    valid, msg = verify_chain()
    print(f"\n=== Signed Audit Log (chain: {msg}) ===")
    for e in _signed_log:
        ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        print(f"  {ts} [{e['author']}] {e['key']}: {e['old']!r} → {e['new']!r} [{e['sig'][:8]}]")


def run_agent(prompt: str) -> str:
    r = client.messages.create(
        model=_config["model"],
        max_tokens=_config["max_tokens"],
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


if __name__ == "__main__":
    update_config("max_tokens", 256, author="ops-bot", reason="perf optimization")
    update_config("model", "claude-haiku-4-5-20251001", author="admin", reason="cost reduction")

    print(run_agent("What is HMAC signing?"))
    print_verified_log()

    # Detect tampering
    if _signed_log:
        _signed_log[0]["new"] = "tampered-value"
        ok, msg = verify_chain()
        print(f"\n[TAMPER TEST] Chain valid: {ok} — {msg}")

# Expected Token Savings: Signed chain detects unauthorized config changes; compliance-grade audit
# Environment: Python 3.9+; use secrets.token_bytes(32) for SIGNING_KEY; store in vault, not code
```

## Comparison

| Option | Storage | Rollback | Diff | Multi-Env | Integrity | Best For |
|--------|---------|---------|------|-----------|-----------|----------|
| 1. JSON Log | JSONL file | Manual | No | No | No | Minimal audit trail |
| 2. SQLite Audit | SQLite | Snapshot rollback | No | No | No | Full history with rollback |
| 3. Versioned Config | In-memory | Version rollback | Yes | No | No | Diffing and rollback |
| 4. File Watcher | In-memory | No | No | No | No | Detecting external changes |
| 5. Multi-Env Promotion | In-memory | Re-promote | No | Yes | No | Multi-environment pipelines |
| 6. Signed Chain | In-memory | No | No | No | HMAC | Compliance and tamper detection |
