---
layout: solution
title: "Agent Doesn't Implement Config Version Pinning"
category: config
description: "Pin agent configuration to explicit version identifiers — preventing silent config upgrades from changing agent behavior, enabling rollback to known-good states, and making config changes as auditable as code changes."
tags: [config, versioning, pinning, rollback, sqlite, python]
---

# Agent Doesn't Implement Config Version Pinning

Agents that load the latest configuration on every start silently change behavior when a config key is updated — a prompt template, model ID, or feature flag changes without the agent team knowing. Config version pinning gives each deployment a fixed config snapshot, explicit upgrade paths, and rollback capability when a new version causes regressions.

## Option 1: Semantic Version Tags for Config Snapshots

```python
import anthropic
import json
import os
from dataclasses import dataclass

@dataclass
class VersionedConfig:
    version: str
    model: str
    max_tokens: int
    system_prompt: str
    temperature: float

# Registry of pinned config versions
CONFIG_REGISTRY: dict[str, VersionedConfig] = {
    "1.0.0": VersionedConfig(
        version="1.0.0",
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system_prompt="You are a helpful assistant.",
        temperature=1.0,
    ),
    "1.1.0": VersionedConfig(
        version="1.1.0",
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system_prompt="You are a precise, helpful assistant. Be concise.",
        temperature=1.0,
    ),
    "2.0.0": VersionedConfig(
        version="2.0.0",
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system_prompt="You are an expert assistant with deep technical knowledge.",
        temperature=1.0,
    ),
}

PINNED_VERSION = os.environ.get("AGENT_CONFIG_VERSION", "1.1.0")

def load_pinned_config(version: str) -> VersionedConfig:
    if version not in CONFIG_REGISTRY:
        available = list(CONFIG_REGISTRY.keys())
        raise ValueError(f"Config version {version!r} not found. Available: {available}")
    return CONFIG_REGISTRY[version]

def run_with_pinned_config(prompt: str, version: str | None = None) -> dict:
    cfg = load_pinned_config(version or PINNED_VERSION)
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        system=cfg.system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )
    return {
        "config_version": cfg.version,
        "model": cfg.model,
        "response": resp.content[0].text,
        "tokens": resp.usage.input_tokens + resp.usage.output_tokens,
    }

# Show version selection
print(f"Active pinned version: {PINNED_VERSION}")
for v in ["1.0.0", "1.1.0"]:
    cfg = load_pinned_config(v)
    print(f"  v{v}: model={cfg.model} max_tokens={cfg.max_tokens}")

result = run_with_pinned_config("What is Python?")
print(f"\nResponse (v{result['config_version']}): {result['response'][:80]}")

# Expected Token Savings: max_tokens pinned per version prevents silent upgrades from doubling output cost
# Environment: set AGENT_CONFIG_VERSION in deployment env; pin to known-good version in production
```

## Option 2: File-Based Config with Hash Pinning

```python
import anthropic
import json
import hashlib
import os
from pathlib import Path

CONFIG_DIR = Path("configs")
PINLOCK = Path("config.lock")  # like package-lock.json

def config_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

def write_config(name: str, data: dict):
    CONFIG_DIR.mkdir(exist_ok=True)
    path = CONFIG_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return config_hash(data)

def pin_config(name: str) -> dict:
    """Pin the current config by recording its hash in config.lock."""
    path = CONFIG_DIR / f"{name}.json"
    with open(path) as f:
        data = json.load(f)
    h = config_hash(data)
    lock = {}
    if PINLOCK.exists():
        with open(PINLOCK) as f:
            lock = json.load(f)
    lock[name] = {"hash": h, "path": str(path)}
    with open(PINLOCK, "w") as f:
        json.dump(lock, f, indent=2)
    print(f"Pinned {name} @ {h}")
    return {"name": name, "hash": h}

def load_pinned(name: str) -> dict:
    """Load config only if hash matches pin."""
    if not PINLOCK.exists():
        raise FileNotFoundError("config.lock not found — run pin_config first")
    with open(PINLOCK) as f:
        lock = json.load(f)
    if name not in lock:
        raise KeyError(f"Config {name!r} not pinned")
    pinned_hash = lock[name]["hash"]
    path = CONFIG_DIR / f"{name}.json"
    with open(path) as f:
        data = json.load(f)
    actual_hash = config_hash(data)
    if actual_hash != pinned_hash:
        raise ValueError(
            f"Config {name!r} has changed since pinning!\n"
            f"  Expected: {pinned_hash}\n"
            f"  Actual:   {actual_hash}\n"
            "Run pin_config to update the lock or rollback the config file."
        )
    return data

# Write a config and pin it
base_config = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 256,
    "system": "You are a helpful assistant.",
}
write_config("agent_v1", base_config)
pin_config("agent_v1")

# Load — succeeds
cfg = load_pinned("agent_v1")
print(f"Loaded: {cfg['model']}")

# Simulate drift: someone edits the config without updating the lock
modified = {**base_config, "model": "claude-opus-4-6"}  # unauthorized change
write_config("agent_v1", modified)

try:
    load_pinned("agent_v1")
except ValueError as e:
    print(f"\nDrift detected: {e}")

# Expected Token Savings: Hash pinning prevents silent model upgrades (haiku→opus = 15x cost increase)
# Environment: commit config.lock to git; fail startup if hash mismatch detected
```

## Option 3: SQLite Config Version Registry with Rollback

```python
import anthropic
import sqlite3
import json
import time
import hashlib

DB = "config_versions.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS config_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT UNIQUE, config TEXT,
            config_hash TEXT, active INTEGER DEFAULT 0,
            deployed_ts REAL, notes TEXT
        )
    """)
    con.commit(); con.close()

def register_version(version: str, config: dict, notes: str = "") -> str:
    init_db()
    h = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR REPLACE INTO config_versions (version, config, config_hash, active, deployed_ts, notes) VALUES (?,?,?,0,?,?)",
        (version, json.dumps(config), h, time.time(), notes),
    )
    con.commit(); con.close()
    print(f"Registered v{version} [{h}]")
    return h

def activate_version(version: str):
    con = sqlite3.connect(DB)
    # Deactivate all others
    con.execute("UPDATE config_versions SET active=0")
    rows = con.execute(
        "UPDATE config_versions SET active=1, deployed_ts=? WHERE version=?",
        (time.time(), version),
    ).rowcount
    con.commit(); con.close()
    if rows == 0:
        raise KeyError(f"Version {version!r} not found")
    print(f"Activated v{version}")

def get_active_config() -> dict:
    init_db()
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT version, config FROM config_versions WHERE active=1"
    ).fetchone()
    con.close()
    if not row:
        raise RuntimeError("No active config version — call activate_version first")
    return {"version": row[0], **json.loads(row[1])}

def rollback_to(version: str):
    activate_version(version)
    print(f"Rolled back to v{version}")

def version_history() -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT version, config_hash, active, deployed_ts, notes "
        "FROM config_versions ORDER BY id DESC"
    ).fetchall()
    con.close()
    return [{"version": r[0], "hash": r[1], "active": bool(r[2]),
             "deployed_ts": r[3], "notes": r[4]} for r in rows]

# Register two versions
register_version("1.0", {"model": "claude-haiku-4-5-20251001", "max_tokens": 256}, "initial")
register_version("1.1", {"model": "claude-haiku-4-5-20251001", "max_tokens": 512}, "increased budget")
register_version("2.0", {"model": "claude-sonnet-4-6", "max_tokens": 1024}, "upgrade to sonnet")

activate_version("2.0")
cfg = get_active_config()
print(f"Active: v{cfg['version']} model={cfg['model']}")

# Simulate: v2.0 causes issues, rollback
rollback_to("1.1")
cfg = get_active_config()
print(f"After rollback: v{cfg['version']} model={cfg['model']}")

print("\nVersion history:")
for v in version_history():
    marker = "★" if v["active"] else " "
    print(f"  {marker} v{v['version']} [{v['hash']}] {v['notes']}")

# Use active config for API call
client = anthropic.Anthropic()
resp = client.messages.create(
    model=cfg["model"],
    max_tokens=cfg["max_tokens"],
    messages=[{"role": "user", "content": "What is Python?"}],
)
print(f"\nResponse (v{cfg['version']}): {resp.content[0].text[:60]}")

# Expected Token Savings: Version history shows cost per version; rollback restores cheaper model instantly
# Environment: SQLite version registry survives restarts; add migration hooks for schema changes between versions
```

## Option 4: Environment-Locked Config with Signature

```python
import anthropic
import json
import hmac
import hashlib
import os
import base64

SECRET_KEY = os.environ.get("CONFIG_SIGN_SECRET", "dev-signing-key-changeme")

def sign_config(config: dict) -> str:
    """Create HMAC signature for config integrity."""
    payload = json.dumps(config, sort_keys=True).encode()
    sig = hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).digest()
    return base64.b64encode(sig).decode()

def verify_config(config: dict, signature: str) -> bool:
    expected = sign_config(config)
    return hmac.compare_digest(expected, signature)

def bundle_config(env: str, config: dict) -> dict:
    """Bundle config with version, env, and signature."""
    sig = sign_config(config)
    return {
        "env": env,
        "config": config,
        "signature": sig,
        "pinned_at": "2026-01-15T10:00:00Z",
    }

def load_verified_config(bundle: dict, expected_env: str) -> dict:
    """Load config only if signature valid and env matches."""
    if bundle["env"] != expected_env:
        raise EnvironmentError(
            f"Config env mismatch: expected {expected_env!r}, got {bundle['env']!r}"
        )
    if not verify_config(bundle["config"], bundle["signature"]):
        raise ValueError("Config signature invalid — possible tampering")
    return bundle["config"]

# Create signed bundles per environment
prod_config = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "system": "Production assistant. Be accurate and professional.",
}
dev_config = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 256,
    "system": "Dev assistant.",
}

prod_bundle = bundle_config("production", prod_config)
dev_bundle  = bundle_config("development", dev_config)

# Load for correct environment
cfg = load_verified_config(dev_bundle, expected_env="development")
print(f"Dev config loaded: {cfg['model']}")

# Wrong environment rejected
try:
    load_verified_config(prod_bundle, expected_env="development")
except EnvironmentError as e:
    print(f"Env mismatch: {e}")

# Tampered config rejected
tampered = {**prod_bundle, "config": {**prod_config, "model": "claude-opus-4-6"}}
try:
    load_verified_config(tampered, expected_env="production")
except ValueError as e:
    print(f"Tamper detected: {e}")

# Use config
client = anthropic.Anthropic()
resp = client.messages.create(
    model=cfg["model"],
    max_tokens=cfg["max_tokens"],
    system=cfg.get("system", ""),
    messages=[{"role": "user", "content": "What is Python?"}],
)
print(f"\nResponse: {resp.content[0].text[:60]}")

# Expected Token Savings: Signature verification prevents prod config being used in dev (and vice versa — expensive models)
# Environment: store SECRET_KEY in vault; rotate key quarterly; use separate keys per environment
```

## Option 5: Config Pinning with Automatic Upgrade Approval Gate

```python
import anthropic
import json
import sqlite3
import time
from dataclasses import dataclass

DB = "config_approvals.db"

@dataclass
class ConfigDiff:
    field: str
    old_value: object
    new_value: object
    risk: str  # low / medium / high

RISK_RULES = {
    "model":       "high",    # model change affects cost and capability
    "max_tokens":  "medium",  # affects cost
    "system":      "medium",  # affects behavior
    "temperature": "low",     # minor behavior change
}

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS config_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_version TEXT, to_version TEXT,
            diff TEXT, risk_level TEXT,
            status TEXT DEFAULT 'pending',
            requested_ts REAL, approved_ts REAL, approver TEXT
        )
    """)
    con.commit(); con.close()

def diff_configs(old: dict, new: dict) -> list[ConfigDiff]:
    diffs = []
    for key in set(old) | set(new):
        old_v = old.get(key, "MISSING")
        new_v = new.get(key, "MISSING")
        if old_v != new_v:
            risk = RISK_RULES.get(key, "low")
            diffs.append(ConfigDiff(key, old_v, new_v, risk))
    return sorted(diffs, key=lambda d: ["high", "medium", "low"].index(d.risk))

def request_upgrade(from_version: str, to_version: str,
                    old_cfg: dict, new_cfg: dict) -> dict:
    init_db()
    diffs = diff_configs(old_cfg, new_cfg)
    max_risk = diffs[0].risk if diffs else "low"
    diff_json = json.dumps([
        {"field": d.field, "old": str(d.old_value), "new": str(d.new_value), "risk": d.risk}
        for d in diffs
    ])
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO config_approvals (from_version, to_version, diff, risk_level, requested_ts) VALUES (?,?,?,?,?)",
        (from_version, to_version, diff_json, max_risk, time.time()),
    )
    con.commit(); con.close()

    print(f"Upgrade {from_version} -> {to_version} requires approval (max risk: {max_risk})")
    for d in diffs:
        print(f"  [{d.risk:6s}] {d.field}: {d.old_value!r} -> {d.new_value!r}")

    # Auto-approve low-risk changes; gate high/medium
    if max_risk == "low":
        approve_upgrade(from_version, to_version, approver="auto")
        return {"approved": True, "config": new_cfg}
    return {"approved": False, "reason": f"Requires manual approval (risk: {max_risk})"}

def approve_upgrade(from_version: str, to_version: str, approver: str = "admin"):
    con = sqlite3.connect(DB)
    con.execute(
        "UPDATE config_approvals SET status='approved', approved_ts=?, approver=? "
        "WHERE from_version=? AND to_version=? AND status='pending'",
        (time.time(), approver, from_version, to_version),
    )
    con.commit(); con.close()
    print(f"Approved by {approver}: {from_version} -> {to_version}")

v1 = {"model": "claude-haiku-4-5-20251001", "max_tokens": 256, "temperature": 1.0}
v2 = {"model": "claude-haiku-4-5-20251001", "max_tokens": 256, "temperature": 0.5}  # low risk
v3 = {"model": "claude-sonnet-4-6", "max_tokens": 1024, "temperature": 1.0}         # high risk

print("=== Low-risk upgrade ===")
result = request_upgrade("1.0", "1.1", v1, v2)
print(f"Result: {'✓ approved' if result['approved'] else '✗ ' + result.get('reason','')}")

print("\n=== High-risk upgrade ===")
result = request_upgrade("1.0", "2.0", v1, v3)
print(f"Result: {'✓ approved' if result['approved'] else '✗ ' + result.get('reason','')}")

# Expected Token Savings: Approval gate blocks accidental model upgrades (haiku→sonnet = 6x cost)
# Environment: integrate with Slack/PagerDuty for approval notifications; store approvals in audit DB
```

## Option 6: Immutable Config Objects with Change Audit

```python
import anthropic
import sqlite3
import json
import time
import hashlib
from typing import Any

DB = "config_audit.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS config_audit (
            ts REAL, actor TEXT, action TEXT,
            version TEXT, field TEXT, old_val TEXT, new_val TEXT
        )
    """)
    con.commit(); con.close()

class ImmutableConfig:
    """Config object that logs all attempted mutations."""
    def __init__(self, version: str, data: dict, actor: str = "system"):
        init_db()
        object.__setattr__(self, "_version", version)
        object.__setattr__(self, "_data", dict(data))
        object.__setattr__(self, "_actor", actor)
        object.__setattr__(self, "_hash",
            hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12])
        self._audit("loaded", None, None, None)

    def _audit(self, action: str, field: Any, old: Any, new: Any):
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO config_audit VALUES (?,?,?,?,?,?,?)",
                    (time.time(), self._actor, action, self._version,
                     str(field), str(old), str(new)))
        con.commit(); con.close()

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        old = self._data.get(name, "MISSING")
        self._audit("mutation_attempt", name, old, value)
        raise AttributeError(
            f"Config v{self._version} is immutable. "
            f"Attempted: {name}={value!r}. "
            "Create a new version instead."
        )

    def __getattr__(self, name):
        if name in self._data:
            return self._data[name]
        raise AttributeError(f"Config has no field {name!r}")

    def evolve(self, actor: str, **changes) -> "ImmutableConfig":
        """Create a new version with changes applied — original unchanged."""
        new_data = {**self._data, **changes}
        parts = self._version.split(".")
        new_version = f"{parts[0]}.{int(parts[1])+1}" if len(parts) >= 2 else f"{self._version}.1"
        for field, new_val in changes.items():
            self._audit("evolved", field, self._data.get(field), new_val)
        return ImmutableConfig(new_version, new_data, actor=actor)

    def __repr__(self):
        return f"ImmutableConfig(v{self._version}, hash={self._hash}, data={self._data})"

def audit_log() -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT ts, actor, action, version, field, old_val, new_val FROM config_audit ORDER BY ts DESC LIMIT 20").fetchall()
    con.close()
    return [{"ts": r[0], "actor": r[1], "action": r[2], "version": r[3], "field": r[4], "old": r[5], "new": r[6]} for r in rows]

# Create and use immutable config
cfg_v1 = ImmutableConfig("1.0", {"model": "claude-haiku-4-5-20251001", "max_tokens": 256}, actor="admin")
print(f"v1 model: {cfg_v1.model}")

# Mutation blocked
try:
    cfg_v1.model = "claude-opus-4-6"
except AttributeError as e:
    print(f"Mutation blocked: {e}")

# Evolve to new version
cfg_v2 = cfg_v1.evolve("engineer", max_tokens=512)
print(f"v2 max_tokens: {cfg_v2.max_tokens}")
print(f"v1 unchanged: {cfg_v1.max_tokens}")

# Use v1 config for API call
client = anthropic.Anthropic()
resp = client.messages.create(
    model=cfg_v1.model,
    max_tokens=cfg_v1.max_tokens,
    messages=[{"role": "user", "content": "What is Python?"}],
)
print(f"\nResponse: {resp.content[0].text[:60]}")

print("\nAudit log:")
for entry in audit_log()[-5:]:
    print(f"  [{entry['action']:20s}] v{entry['version']} {entry['field']} {entry['old']} -> {entry['new']}")

# Expected Token Savings: Immutable configs prevent accidental model upgrades mid-session; audit shows who changed what
# Environment: evolve() creates new version object; SQLite audit is append-only and survives restarts
```

## Comparison

| Option | Pin Mechanism | Rollback | Audit | Tamper Detection |
|--------|-------------|---------|-------|-----------------|
| 1 — Semantic Version Tags | Registry dict | Manual version select | No | No |
| 2 — File Hash Lock | SHA-256 of file | Overwrite file | No | Hash mismatch |
| 3 — SQLite Registry | Active flag | activate_version() | Version history | No |
| 4 — HMAC Signature | HMAC + env tag | Bundle swap | No | Signature verify |
| 5 — Approval Gate | Risk-based gate | Revert approval | SQLite | No |
| 6 — Immutable Objects | Python __setattr__ | evolve() new version | SQLite mutations | Mutation block |
