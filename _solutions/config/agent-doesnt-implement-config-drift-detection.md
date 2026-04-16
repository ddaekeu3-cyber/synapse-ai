---
layout: solution
title: "Agent Doesn't Implement Config Drift Detection"
category: config
description: "Detect when live configuration has drifted from its expected baseline — catching silent env var overrides, stale secrets, mismatched model IDs, and config mutations between deployments before they cause production incidents."
tags: [config, drift-detection, validation, observability, sqlite, python]
---

# Agent Doesn't Implement Config Drift Detection

Agents deployed across environments silently accumulate config drift — a staging secret leaks into production, a model ID stays pinned to an older version, an env var override from a hotfix is never reverted. Drift detection compares the live config snapshot against a known-good baseline and alerts on any divergence before it causes failures.

## Option 1: Snapshot Comparison at Startup

```python
import anthropic
import os
import json
import hashlib
from dataclasses import dataclass, asdict

@dataclass
class AgentConfig:
    model: str
    max_tokens: int
    api_base: str
    environment: str
    log_level: str

def load_live_config() -> AgentConfig:
    """Load config from environment variables (live state)."""
    return AgentConfig(
        model       = os.environ.get("AGENT_MODEL",  "claude-haiku-4-5-20251001"),
        max_tokens  = int(os.environ.get("MAX_TOKENS", "512")),
        api_base    = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        environment = os.environ.get("ENVIRONMENT", "development"),
        log_level   = os.environ.get("LOG_LEVEL", "INFO"),
    )

def load_baseline() -> AgentConfig:
    """Load expected baseline config from a committed file."""
    baseline_path = "config_baseline.json"
    if not os.path.exists(baseline_path):
        # Write the current config as baseline on first run
        cfg = load_live_config()
        with open(baseline_path, "w") as f:
            json.dump(asdict(cfg), f, indent=2)
        print(f"Baseline written to {baseline_path}")
        return cfg
    with open(baseline_path) as f:
        data = json.load(f)
    return AgentConfig(**data)

def config_hash(cfg: AgentConfig) -> str:
    return hashlib.sha256(json.dumps(asdict(cfg), sort_keys=True).encode()).hexdigest()[:12]

def detect_drift(live: AgentConfig, baseline: AgentConfig) -> list[dict]:
    live_d     = asdict(live)
    baseline_d = asdict(baseline)
    drifts = []
    for key in live_d:
        if live_d[key] != baseline_d.get(key):
            drifts.append({
                "field":    key,
                "expected": baseline_d.get(key),
                "actual":   live_d[key],
            })
    return drifts

def startup_check():
    live     = load_live_config()
    baseline = load_baseline()
    drifts   = detect_drift(live, baseline)

    print(f"Config hash (live):     {config_hash(live)}")
    print(f"Config hash (baseline): {config_hash(baseline)}")

    if drifts:
        print(f"\n[DRIFT DETECTED] {len(drifts)} field(s) differ from baseline:")
        for d in drifts:
            print(f"  {d['field']:15s}: expected={d['expected']!r} actual={d['actual']!r}")
    else:
        print("\nConfig matches baseline ✓")
    return drifts

# Simulate drift: override one env var
os.environ["MAX_TOKENS"] = "999"  # unexpected override
drifts = startup_check()

# Expected Token Savings: Drift caught at startup prevents subtle model behavior changes from wrong max_tokens
# Environment: commit config_baseline.json to git; CI checks drift against tagged release baseline
```

## Option 2: Field-Level Drift with Severity Classification

```python
import anthropic
import os
import json
from dataclasses import dataclass
from enum import Enum

class Severity(Enum):
    CRITICAL = "CRITICAL"   # blocks startup
    WARNING  = "WARNING"    # logs and continues
    INFO     = "INFO"       # informational only

@dataclass
class FieldPolicy:
    field: str
    severity: Severity
    reason: str

# Define which fields matter most for drift
DRIFT_POLICIES: dict[str, FieldPolicy] = {
    "model": FieldPolicy(
        "model", Severity.CRITICAL,
        "Wrong model ID changes cost and capability drastically",
    ),
    "environment": FieldPolicy(
        "environment", Severity.CRITICAL,
        "Wrong environment could route prod traffic to staging",
    ),
    "max_tokens": FieldPolicy(
        "max_tokens", Severity.WARNING,
        "Unexpected token limit affects response quality",
    ),
    "log_level": FieldPolicy(
        "log_level", Severity.INFO,
        "Log level drift is low-risk but should be tracked",
    ),
    "api_base": FieldPolicy(
        "api_base", Severity.CRITICAL,
        "Wrong API base could route to unauthorized endpoint",
    ),
}

BASELINE = {
    "model":       "claude-haiku-4-5-20251001",
    "max_tokens":  512,
    "environment": "production",
    "log_level":   "WARNING",
    "api_base":    "https://api.anthropic.com",
}

def get_live() -> dict:
    return {
        "model":       os.environ.get("AGENT_MODEL",   BASELINE["model"]),
        "max_tokens":  int(os.environ.get("MAX_TOKENS", BASELINE["max_tokens"])),
        "environment": os.environ.get("ENVIRONMENT",   BASELINE["environment"]),
        "log_level":   os.environ.get("LOG_LEVEL",     BASELINE["log_level"]),
        "api_base":    os.environ.get("ANTHROPIC_BASE_URL", BASELINE["api_base"]),
    }

def classify_drift(live: dict, baseline: dict) -> list[dict]:
    results = []
    for field, expected in baseline.items():
        actual = live.get(field)
        if actual != expected:
            policy = DRIFT_POLICIES.get(field, FieldPolicy(field, Severity.INFO, "Unregistered field"))
            results.append({
                "field":    field,
                "expected": expected,
                "actual":   actual,
                "severity": policy.severity,
                "reason":   policy.reason,
            })
    return sorted(results, key=lambda d: ["CRITICAL", "WARNING", "INFO"].index(d["severity"].value))

import sys

def enforce_drift_policy(drifts: list[dict], fail_on_critical: bool = True):
    if not drifts:
        print("No drift detected ✓")
        return
    has_critical = False
    for d in drifts:
        sev = d["severity"].value
        print(f"  [{sev:8s}] {d['field']}: {d['expected']!r} -> {d['actual']!r}")
        print(f"             Reason: {d['reason']}")
        if d["severity"] == Severity.CRITICAL:
            has_critical = True
    if has_critical and fail_on_critical:
        print("\n[STARTUP BLOCKED] Critical config drift detected.")
        sys.exit(1)

# Simulate: wrong model + wrong environment
os.environ["AGENT_MODEL"]  = "claude-opus-4-6"
os.environ["ENVIRONMENT"]  = "staging"

live   = get_live()
drifts = classify_drift(live, BASELINE)
print(f"Drift report ({len(drifts)} fields):")
enforce_drift_policy(drifts, fail_on_critical=False)  # False to not exit in demo

# Expected Token Savings: CRITICAL drift caught before any API call; wrong model ID prevented = no surprise costs
# Environment: set fail_on_critical=True in production; WARNING/INFO logged but not blocking
```

## Option 3: Continuous Drift Polling with SQLite History

```python
import anthropic
import os
import sqlite3
import json
import time
import hashlib

DB = "config_drift.db"

BASELINE = {
    "model":      "claude-haiku-4-5-20251001",
    "max_tokens": "512",
    "environment": "production",
    "log_level":  "WARNING",
}

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS drift_events (
            ts REAL, config_hash TEXT, field TEXT,
            expected TEXT, actual TEXT, resolved INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS config_snapshots (
            ts REAL, config_hash TEXT, snapshot TEXT
        )
    """)
    con.commit(); con.close()

def snapshot_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]

def get_live_config() -> dict:
    return {k: os.environ.get(k.upper().replace("-", "_"), v) for k, v in BASELINE.items()}

def check_and_record_drift():
    init_db()
    live = get_live_config()
    h    = snapshot_hash(live)
    ts   = time.time()

    con = sqlite3.connect(DB)
    con.execute("INSERT INTO config_snapshots VALUES (?,?,?)", (ts, h, json.dumps(live)))

    drifted_fields = []
    for field, expected in BASELINE.items():
        actual = live.get(field)
        if str(actual) != str(expected):
            drifted_fields.append(field)
            # Check if this drift was already recorded (not yet resolved)
            existing = con.execute(
                "SELECT COUNT(*) FROM drift_events WHERE field=? AND actual=? AND resolved=0",
                (field, str(actual)),
            ).fetchone()[0]
            if not existing:
                con.execute(
                    "INSERT INTO drift_events VALUES (?,?,?,?,?,0)",
                    (ts, h, field, str(expected), str(actual)),
                )

    # Mark resolved: fields that are back to baseline
    if not drifted_fields:
        con.execute("UPDATE drift_events SET resolved=1 WHERE resolved=0")

    con.commit(); con.close()
    return drifted_fields

def drift_dashboard() -> dict:
    con = sqlite3.connect(DB)
    open_drifts = con.execute(
        "SELECT field, expected, actual, ts FROM drift_events WHERE resolved=0"
    ).fetchall()
    resolved = con.execute("SELECT COUNT(*) FROM drift_events WHERE resolved=1").fetchone()[0]
    snapshots = con.execute("SELECT COUNT(*) FROM config_snapshots").fetchone()[0]
    con.close()
    return {
        "open_drifts": [{"field": r[0], "expected": r[1], "actual": r[2]} for r in open_drifts],
        "resolved_count": resolved,
        "total_snapshots": snapshots,
    }

# Simulate continuous polling
print("Poll 1: clean config")
drifts = check_and_record_drift()
print(f"  Drifts: {drifts or 'none'}")

print("Poll 2: drift introduced")
os.environ["MODEL"] = "claude-sonnet-4-6"
drifts = check_and_record_drift()
print(f"  Drifts: {drifts}")

print("Poll 3: still drifted")
drifts = check_and_record_drift()
print(f"  Drifts: {drifts}")

dashboard = drift_dashboard()
print(f"\nDashboard: {dashboard['total_snapshots']} snapshots | "
      f"{len(dashboard['open_drifts'])} open | {dashboard['resolved_count']} resolved")
for d in dashboard["open_drifts"]:
    print(f"  OPEN: {d['field']} = {d['actual']!r} (expected {d['expected']!r})")

# Expected Token Savings: Polling detects env var mutations between requests; open_drifts track MTTR
# Environment: run check_and_record_drift() in a background thread every 60s; alert on new open drifts
```

## Option 4: Git-Baseline Config Drift — Compare Against Committed Values

```python
import anthropic
import subprocess
import os
import json
import sys

def git_file_content(filepath: str, ref: str = "HEAD") -> str | None:
    """Read a file from git at a specific ref without checking out."""
    try:
        return subprocess.check_output(
            ["git", "show", f"{ref}:{filepath}"],
            stderr=subprocess.DEVNULL,
        ).decode()
    except subprocess.CalledProcessError:
        return None

def load_committed_config(config_path: str = "agent_config.json") -> dict | None:
    content = git_file_content(config_path)
    if content is None:
        return None
    return json.loads(content)

def load_live_config(config_path: str = "agent_config.json") -> dict | None:
    if not os.path.exists(config_path):
        return None
    with open(config_path) as f:
        return json.load(f)

def detect_git_drift(committed: dict, live: dict, prefix: str = "") -> list[dict]:
    """Recursively compare nested dicts."""
    drifts = []
    for key in set(committed) | set(live):
        full_key = f"{prefix}.{key}" if prefix else key
        if key not in live:
            drifts.append({"field": full_key, "expected": committed[key], "actual": "MISSING"})
        elif key not in committed:
            drifts.append({"field": full_key, "expected": "NOT IN BASELINE", "actual": live[key]})
        elif isinstance(committed[key], dict) and isinstance(live[key], dict):
            drifts.extend(detect_git_drift(committed[key], live[key], prefix=full_key))
        elif committed[key] != live[key]:
            drifts.append({"field": full_key, "expected": committed[key], "actual": live[key]})
    return drifts

# Write a sample config file for demo
sample_config = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 512,
    "features": {"streaming": True, "caching": False},
}
with open("agent_config.json", "w") as f:
    json.dump(sample_config, f, indent=2)

committed = load_committed_config("agent_config.json")
if committed is None:
    print("Config not committed to git — skipping git drift check")
    committed = sample_config

# Simulate local drift: modify live config
live_config = json.loads(json.dumps(sample_config))  # copy
live_config["max_tokens"] = 1024          # changed
live_config["features"]["caching"] = True  # feature flag flipped
live_config["new_field"] = "unexpected"   # new key

drifts = detect_git_drift(committed, live_config)
if drifts:
    print(f"[GIT DRIFT] {len(drifts)} field(s) differ from committed config:")
    for d in drifts:
        print(f"  {d['field']:30s}: {d['expected']!r} -> {d['actual']!r}")
else:
    print("Config matches committed baseline ✓")

# Expected Token Savings: Git baseline is authoritative; detects hotfix env changes not reverted to main
# Environment: run in CI on every PR; git show HEAD:path works without checkout or file I/O
```

## Option 5: Schema-Validated Config with Drift Alert Webhook

```python
import anthropic
import os
import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass

@dataclass
class ConfigSchema:
    model:          str
    max_tokens:     int
    environment:    str
    timeout_s:      float
    retry_attempts: int

EXPECTED = ConfigSchema(
    model          = "claude-haiku-4-5-20251001",
    max_tokens     = 512,
    environment    = "production",
    timeout_s      = 30.0,
    retry_attempts = 3,
)

def load_and_validate() -> tuple[ConfigSchema, list[str]]:
    errors = []
    try:
        model     = os.environ.get("AGENT_MODEL", EXPECTED.model)
        max_tok   = int(os.environ.get("MAX_TOKENS", str(EXPECTED.max_tokens)))
        env       = os.environ.get("ENVIRONMENT", EXPECTED.environment)
        timeout   = float(os.environ.get("TIMEOUT_S", str(EXPECTED.timeout_s)))
        retries   = int(os.environ.get("RETRY_ATTEMPTS", str(EXPECTED.retry_attempts)))
    except (ValueError, TypeError) as e:
        errors.append(f"Parse error: {e}")
        return EXPECTED, errors

    cfg = ConfigSchema(model, max_tok, env, timeout, retries)

    # Schema validation
    if max_tok < 64 or max_tok > 8192:
        errors.append(f"max_tokens={max_tok} out of range [64, 8192]")
    if env not in ("development", "staging", "production"):
        errors.append(f"environment={env!r} is not a known environment")
    if timeout < 1 or timeout > 300:
        errors.append(f"timeout_s={timeout} out of range [1, 300]")

    return cfg, errors

def drift_fields(live: ConfigSchema, expected: ConfigSchema) -> list[dict]:
    drifts = []
    for field in vars(expected):
        exp_val = getattr(expected, field)
        act_val = getattr(live, field)
        if exp_val != act_val:
            drifts.append({"field": field, "expected": exp_val, "actual": act_val})
    return drifts

def send_drift_alert(drifts: list[dict], webhook_url: str | None = None):
    payload = {
        "alert": "config_drift",
        "timestamp": time.time(),
        "drifts": drifts,
        "host": os.environ.get("HOSTNAME", "unknown"),
    }
    msg = json.dumps(payload, indent=2)
    print(f"[ALERT PAYLOAD]\n{msg}")
    if webhook_url:
        try:
            req = urllib.request.Request(
                webhook_url,
                data=msg.encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            print("Alert sent ✓")
        except urllib.error.URLError as e:
            print(f"Alert delivery failed: {e}")

# Simulate drift
os.environ["ENVIRONMENT"] = "staging"
os.environ["MAX_TOKENS"]  = "9999"  # out of range

live, errors = load_and_validate()
if errors:
    print(f"Validation errors: {errors}")

drifts = drift_fields(live, EXPECTED)
if drifts:
    send_drift_alert(drifts, webhook_url=None)  # set webhook URL in production

# Expected Token Savings: Schema validation catches type errors before API calls; webhook enables instant ops response
# Environment: set WEBHOOK_URL env var to Slack/PagerDuty endpoint; run on every container start
```

## Option 6: Multi-Environment Config Comparator

```python
import anthropic
import json
import os
import sqlite3
import time

DB = "env_drift.db"

# Canonical configs per environment — committed to source
CANONICAL = {
    "development": {
        "model": "claude-haiku-4-5-20251001", "max_tokens": 256,
        "log_level": "DEBUG", "caching": False,
    },
    "staging": {
        "model": "claude-haiku-4-5-20251001", "max_tokens": 512,
        "log_level": "INFO", "caching": True,
    },
    "production": {
        "model": "claude-sonnet-4-6", "max_tokens": 1024,
        "log_level": "WARNING", "caching": True,
    },
}

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS env_comparisons (
            ts REAL, environment TEXT, field TEXT,
            canonical TEXT, live TEXT, drifted INTEGER
        )
    """)
    con.commit(); con.close()

def get_live_for_env(env: str) -> dict:
    """In practice, each env would pull from its own config source."""
    canonical = CANONICAL[env].copy()
    # Simulate drift in staging
    if env == "staging":
        canonical["model"] = "claude-opus-4-6"  # upgrade not rolled out yet
        canonical["caching"] = False            # accidentally disabled
    return canonical

def compare_env(env: str) -> list[dict]:
    init_db()
    canon = CANONICAL.get(env)
    if canon is None:
        return [{"env": env, "error": "Unknown environment"}]

    live = get_live_for_env(env)
    drifts = []
    ts = time.time()
    con = sqlite3.connect(DB)
    for field, expected in canon.items():
        actual  = live.get(field)
        drifted = int(str(actual) != str(expected))
        con.execute(
            "INSERT INTO env_comparisons VALUES (?,?,?,?,?,?)",
            (ts, env, field, str(expected), str(actual), drifted),
        )
        if drifted:
            drifts.append({"environment": env, "field": field,
                           "expected": expected, "actual": actual})
    con.commit(); con.close()
    return drifts

def multi_env_report():
    all_drifts = []
    for env in CANONICAL:
        drifts = compare_env(env)
        status = f"{len(drifts)} drift(s)" if drifts else "clean"
        print(f"  [{env:12s}] {status}")
        for d in drifts:
            print(f"    {d['field']:15s}: {d['expected']!r} -> {d['actual']!r}")
        all_drifts.extend(drifts)

    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT environment, COUNT(*) FROM env_comparisons WHERE drifted=1 GROUP BY environment"
    ).fetchall()
    con.close()
    print("\nSQLite drift totals:")
    for r in row:
        print(f"  {r[0]:12s}: {r[1]} drifted fields")
    return all_drifts

print("Multi-environment config drift check:")
drifts = multi_env_report()
print(f"\nTotal drifts found: {len(drifts)}")

# Expected Token Savings: Cross-env comparison catches config that worked in dev but breaks prod
# Environment: run as deployment gate; SQLite records drift history for trend analysis across releases
```

## Comparison

| Option | Baseline Source | Detection Timing | Severity Levels | Alert Mechanism |
|--------|----------------|-----------------|----------------|----------------|
| 1 — Snapshot File | JSON file | Startup | No | Print |
| 2 — Field Policy | Hardcoded dict | Startup | CRITICAL/WARN/INFO | Exit on critical |
| 3 — SQLite History | Hardcoded dict | Continuous poll | No | Open drift count |
| 4 — Git Baseline | git show HEAD | On-demand | No | Print + exit |
| 5 — Schema + Webhook | Hardcoded schema | Startup | Schema errors | HTTP webhook |
| 6 — Multi-Env Compare | Per-env canonical | On-demand | No | SQLite history |
