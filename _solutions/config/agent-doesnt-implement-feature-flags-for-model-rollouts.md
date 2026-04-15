---
layout: solution
title: "Agent Doesn't Implement Feature Flags for Model Rollouts"
category: config
description: "New Claude models are deployed by changing a constant and redeploying. There's no way to gradually roll out a new model, A/B test it against the current one, or roll back instantly when quality degrades."
tags: [config, feature-flags, model-rollout, ab-testing, gradual-rollout, canary, anthropic]
---

# Agent Doesn't Implement Feature Flags for Model Rollouts

## Problem

Switching from `claude-sonnet-4-6` to `claude-opus-4-6` — or rolling back when a new model behaves unexpectedly — requires a code change and a full redeployment. There's no way to test the new model on 5% of traffic, compare outputs side-by-side, or flip back instantly. Feature flags solve this: model selection becomes a runtime config change with no deploy required.

## Solutions

### Option 1: Environment Variable Feature Flag

```python
# config/model_flags.py
"""
Simplest model feature flag: read model selection from an environment variable.
Change the env var in your secrets manager or deployment config — no redeploy needed.
"""
import os
import anthropic
from typing import Literal


ModelTier = Literal["haiku", "sonnet", "opus"]

# Map tier names to current model IDs
MODEL_MAP: dict[ModelTier, str] = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}

# Default tier from env var — override per-request if needed
DEFAULT_TIER: ModelTier = os.environ.get("AGENT_MODEL_TIER", "sonnet")  # type: ignore


def get_model(tier: ModelTier | None = None) -> str:
    """Resolve the model ID for the given tier (or the global default)."""
    resolved_tier = tier or DEFAULT_TIER
    if resolved_tier not in MODEL_MAP:
        resolved_tier = "sonnet"  # Safe fallback
    return MODEL_MAP[resolved_tier]


def ask(
    user_message: str,
    tier: ModelTier | None = None,
    max_tokens: int = 512,
) -> str:
    client = anthropic.Anthropic()
    model = get_model(tier)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# ── To upgrade all traffic to opus: ──────────────────────────────────────────
#   export AGENT_MODEL_TIER=opus
#   (no redeploy — process rereads os.environ on each call)
#
# ── To roll back: ────────────────────────────────────────────────────────────
#   export AGENT_MODEL_TIER=sonnet
```

**Expected Token Savings:** Not applicable — deployment control
**Environment:** `pip install anthropic`

---

### Option 2: Percentage-Based Canary Rollout

```python
# config/canary_rollout.py
"""
Route a configurable percentage of requests to a new model while keeping
the rest on the stable model. Gradually ramp up as confidence grows.
Supports per-user consistent hashing so the same user always gets the same model.
"""
import hashlib
import os
import time
import anthropic
from dataclasses import dataclass


@dataclass
class ModelCanaryConfig:
    stable_model: str = "claude-sonnet-4-6"
    canary_model: str = "claude-opus-4-6"
    canary_percent: float = float(os.environ.get("CANARY_PERCENT", "0"))  # 0–100
    salt: str = os.environ.get("CANARY_SALT", "default-salt")


_config = ModelCanaryConfig()


def _bucket(user_id: str, salt: str) -> float:
    """Stable bucket 0–100 for a user, using HMAC for uniform distribution."""
    h = hashlib.sha256(f"{salt}:{user_id}".encode()).digest()
    # Use first 4 bytes as a 32-bit int, normalize to 0–100
    bucket = int.from_bytes(h[:4], "big") % 10000 / 100.0
    return bucket


def select_model(user_id: str, config: ModelCanaryConfig | None = None) -> tuple[str, str]:
    """
    Returns (model_id, variant) where variant is 'stable' or 'canary'.
    Consistent: same user_id always maps to same variant for a given config.
    """
    cfg = config or _config
    bucket = _bucket(user_id, cfg.salt)
    if bucket < cfg.canary_percent:
        return cfg.canary_model, "canary"
    return cfg.stable_model, "stable"


def ask_with_canary(user_message: str, user_id: str, max_tokens: int = 512) -> dict:
    model, variant = select_model(user_id)
    client = anthropic.Anthropic()
    start = time.perf_counter()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_message}],
    )
    latency_ms = (time.perf_counter() - start) * 1000

    return {
        "text": response.content[0].text,
        "model": model,
        "variant": variant,
        "latency_ms": latency_ms,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


# ── Ramp schedule example ─────────────────────────────────────────────────────
# Day 1: CANARY_PERCENT=5   (5% on opus)
# Day 3: CANARY_PERCENT=20  (if metrics look good)
# Day 7: CANARY_PERCENT=100 (full cutover)
# Rollback: CANARY_PERCENT=0 (instantly back to stable)
```

**Expected Token Savings:** Controls cost exposure during rollout; ~0% extra overhead
**Environment:** `pip install anthropic`

---

### Option 3: LaunchDarkly-Style In-Process Flag Store

```python
# config/flag_store.py
"""
A lightweight in-process feature flag store that can be updated at runtime
via an admin API or config file watch. No external service required.
Supports per-user overrides for QA testing on specific accounts.
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
import anthropic


FLAGS_FILE = Path(os.environ.get("FLAGS_FILE", "feature_flags.json"))

_flags: dict[str, Any] = {}
_overrides: dict[str, dict[str, Any]] = {}  # user_id -> {flag_name: value}
_lock = threading.RLock()


def _load_flags():
    global _flags
    if FLAGS_FILE.exists():
        with open(FLAGS_FILE) as f:
            with _lock:
                _flags = json.load(f)


def _watch_flags(interval: float = 10.0):
    """Background thread: reload flags file every N seconds."""
    last_mtime = 0.0
    while True:
        try:
            mtime = FLAGS_FILE.stat().st_mtime if FLAGS_FILE.exists() else 0.0
            if mtime != last_mtime:
                _load_flags()
                last_mtime = mtime
        except Exception:
            pass
        time.sleep(interval)


def start_flag_watcher():
    t = threading.Thread(target=_watch_flags, daemon=True)
    t.start()


def get_flag(name: str, user_id: str | None = None, default: Any = None) -> Any:
    with _lock:
        # Per-user override takes precedence
        if user_id and user_id in _overrides:
            if name in _overrides[user_id]:
                return _overrides[user_id][name]
        return _flags.get(name, default)


def set_override(user_id: str, flag_name: str, value: Any):
    """Set a per-user flag override (e.g. for QA testing)."""
    with _lock:
        if user_id not in _overrides:
            _overrides[user_id] = {}
        _overrides[user_id][flag_name] = value


def clear_override(user_id: str, flag_name: str):
    with _lock:
        if user_id in _overrides:
            _overrides[user_id].pop(flag_name, None)


# ── feature_flags.json example ───────────────────────────────────────────────
# {
#   "agent_model": "claude-sonnet-4-6",
#   "enable_extended_thinking": false,
#   "max_tokens_override": null,
#   "use_prompt_caching": true
# }

def get_model_for_user(user_id: str) -> str:
    return get_flag("agent_model", user_id=user_id, default="claude-sonnet-4-6")


def ask_with_flags(user_message: str, user_id: str, max_tokens: int = 512) -> str:
    model = get_model_for_user(user_id)
    use_cache = get_flag("use_prompt_caching", user_id=user_id, default=True)
    extended_thinking = get_flag("enable_extended_thinking", user_id=user_id, default=False)

    client = anthropic.Anthropic()
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_message}],
    }
    if extended_thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 2000}

    response = client.messages.create(**kwargs)
    return response.content[0].text


# Initialize
_load_flags()
start_flag_watcher()
```

**Expected Token Savings:** Not applicable — deployment control; enables cost-saving model downgrades
**Environment:** `pip install anthropic`

---

### Option 4: A/B Test with Metric Collection

```python
# config/ab_test.py
"""
Run a proper A/B test between two models: route traffic by cohort, collect
quality + cost metrics, and provide a statistical summary to decide the winner.
"""
import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Literal
import anthropic


Variant = Literal["control", "treatment"]

DB = sqlite3.connect(str(Path("ab_metrics.db")), check_same_thread=False)
DB.execute("""
    CREATE TABLE IF NOT EXISTS ab_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        experiment TEXT NOT NULL,
        variant TEXT NOT NULL,
        user_id TEXT NOT NULL,
        model TEXT NOT NULL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        latency_ms REAL,
        thumbs_up INTEGER,
        created_at REAL NOT NULL
    )
""")
DB.execute("CREATE INDEX IF NOT EXISTS idx_experiment ON ab_events(experiment, variant)")
DB.commit()


EXPERIMENTS: dict[str, dict] = {
    "opus_vs_sonnet": {
        "control": "claude-sonnet-4-6",
        "treatment": "claude-opus-4-6",
        "treatment_percent": 20,  # 20% get opus
    }
}


def assign_variant(experiment: str, user_id: str) -> tuple[Variant, str]:
    cfg = EXPERIMENTS[experiment]
    h = int(hashlib.sha256(f"{experiment}:{user_id}".encode()).hexdigest(), 16)
    bucket = h % 100
    if bucket < cfg["treatment_percent"]:
        return "treatment", cfg["treatment"]
    return "control", cfg["control"]


def record_event(
    experiment: str,
    variant: Variant,
    user_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
):
    DB.execute(
        "INSERT INTO ab_events (experiment, variant, user_id, model, input_tokens, "
        "output_tokens, latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (experiment, variant, user_id, model, input_tokens, output_tokens, latency_ms, time.time()),
    )
    DB.commit()


def record_feedback(user_id: str, experiment: str, thumbs_up: bool):
    DB.execute(
        "UPDATE ab_events SET thumbs_up = ? "
        "WHERE user_id = ? AND experiment = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (1 if thumbs_up else 0, user_id, experiment),
    )
    DB.commit()


def get_experiment_summary(experiment: str) -> dict:
    rows = DB.execute("""
        SELECT variant,
               COUNT(*) as requests,
               AVG(latency_ms) as avg_latency,
               AVG(input_tokens + output_tokens) as avg_tokens,
               AVG(thumbs_up) as approval_rate
        FROM ab_events
        WHERE experiment = ?
        GROUP BY variant
    """, (experiment,)).fetchall()
    return {
        row[0]: {
            "requests": row[1],
            "avg_latency_ms": round(row[2] or 0, 1),
            "avg_tokens": round(row[3] or 0, 1),
            "approval_rate": round((row[4] or 0) * 100, 1),
        }
        for row in rows
    }


async def ask_ab(user_message: str, user_id: str, experiment: str = "opus_vs_sonnet") -> dict:
    variant, model = assign_variant(experiment, user_id)
    client = anthropic.AsyncAnthropic()
    start = time.perf_counter()
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}],
    )
    latency_ms = (time.perf_counter() - start) * 1000
    record_event(
        experiment=experiment, variant=variant, user_id=user_id, model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=latency_ms,
    )
    return {"text": response.content[0].text, "variant": variant, "model": model}
```

**Expected Token Savings:** Controlled exposure — 80% stay on cheaper sonnet during test
**Environment:** `pip install anthropic`

---

### Option 5: Remote Flag Service with Local Cache

```python
# config/remote_flags.py
"""
Poll a remote flags endpoint (e.g. your own config service, or a simple
JSON file on S3/GitHub) and cache locally. Falls back to cached values
if the remote is unreachable — no single point of failure.
"""
import asyncio
import json
import time
from typing import Any
import aiohttp
import anthropic


_cache: dict[str, Any] = {}
_cache_expiry: float = 0
_CACHE_TTL = 60.0  # seconds

FLAGS_URL = "https://your-config-service.internal/api/flags/agent"
FALLBACK_FLAGS = {
    "agent_model": "claude-sonnet-4-6",
    "canary_percent": 0,
    "enable_extended_thinking": False,
    "max_concurrent_requests": 20,
}


async def fetch_flags() -> dict[str, Any]:
    """Fetch flags from remote, return cached if fresh, fall back on error."""
    global _cache, _cache_expiry

    if time.time() < _cache_expiry and _cache:
        return _cache

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(FLAGS_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _cache = data
                    _cache_expiry = time.time() + _CACHE_TTL
                    return data
    except Exception as e:
        pass  # Use cached or fallback

    # Return stale cache if available, otherwise hardcoded fallback
    return _cache if _cache else FALLBACK_FLAGS.copy()


async def get_flag_async(name: str, default: Any = None) -> Any:
    flags = await fetch_flags()
    return flags.get(name, default)


async def ask_with_remote_flags(user_message: str, max_tokens: int = 512) -> str:
    model = await get_flag_async("agent_model", default="claude-sonnet-4-6")
    extended_thinking = await get_flag_async("enable_extended_thinking", default=False)

    client = anthropic.AsyncAnthropic()
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_message}],
    }
    if extended_thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 1500}

    response = await client.messages.create(**kwargs)
    return response.content[0].text


# ── Flags JSON served by your config service ─────────────────────────────────
# {
#   "agent_model": "claude-sonnet-4-6",
#   "canary_percent": 10,
#   "enable_extended_thinking": false,
#   "max_concurrent_requests": 20
# }
#
# To cut over: update the JSON at the config URL.
# All agent instances pick up the change within 60s (TTL).
# To roll back: revert the JSON.
```

**Expected Token Savings:** Not applicable — deployment control; TTL prevents thundering-herd
**Environment:** `pip install anthropic aiohttp`

---

### Option 6: Model Flag with Automatic Quality Guard

```python
# config/quality_guard.py
"""
Feature flag that automatically rolls back to the stable model if the
new model's error rate or latency exceeds thresholds within a sliding window.
Implements a self-healing canary.
"""
import asyncio
import collections
import time
import os
import anthropic


@dataclass_like = None  # Using plain class for clarity


class ModelQualityGuard:
    """
    Switches between stable and canary model.
    Auto-rolls back if canary error rate exceeds threshold.
    """
    def __init__(
        self,
        stable_model: str = "claude-sonnet-4-6",
        canary_model: str = "claude-opus-4-6",
        canary_percent: float = float(os.environ.get("CANARY_PERCENT", "10")),
        error_threshold: float = 0.05,   # Roll back if error rate > 5%
        window_seconds: float = 300.0,   # 5-minute rolling window
        min_samples: int = 20,           # Need at least 20 samples before auto-rollback
    ):
        self.stable_model = stable_model
        self.canary_model = canary_model
        self.canary_percent = canary_percent
        self.error_threshold = error_threshold
        self.window_seconds = window_seconds
        self.min_samples = min_samples
        self._rolled_back = False
        # (timestamp, is_error) pairs
        self._canary_outcomes: collections.deque = collections.deque()

    def _is_canary(self, user_id: str) -> bool:
        if self._rolled_back:
            return False
        h = int(__import__("hashlib").sha256(user_id.encode()).hexdigest(), 16)
        return (h % 100) < self.canary_percent

    def _record_outcome(self, is_error: bool):
        now = time.time()
        self._canary_outcomes.append((now, is_error))
        # Evict old entries
        cutoff = now - self.window_seconds
        while self._canary_outcomes and self._canary_outcomes[0][0] < cutoff:
            self._canary_outcomes.popleft()

    def _check_auto_rollback(self):
        if self._rolled_back or len(self._canary_outcomes) < self.min_samples:
            return
        error_rate = sum(1 for _, err in self._canary_outcomes if err) / len(self._canary_outcomes)
        if error_rate > self.error_threshold:
            self._rolled_back = True
            print(
                f"[QualityGuard] AUTO-ROLLBACK: canary error rate {error_rate:.1%} "
                f"> threshold {self.error_threshold:.1%}. Reverting to {self.stable_model}."
            )

    async def ask(self, user_message: str, user_id: str, max_tokens: int = 512) -> dict:
        use_canary = self._is_canary(user_id)
        model = self.canary_model if use_canary else self.stable_model
        client = anthropic.AsyncAnthropic()
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": user_message}],
                ),
                timeout=30.0,
            )
            if use_canary:
                self._record_outcome(is_error=False)
                self._check_auto_rollback()
            return {"text": response.content[0].text, "model": model, "variant": "canary" if use_canary else "stable"}
        except Exception as e:
            if use_canary:
                self._record_outcome(is_error=True)
                self._check_auto_rollback()
            raise

    @property
    def status(self) -> dict:
        samples = list(self._canary_outcomes)
        error_rate = sum(1 for _, e in samples if e) / max(len(samples), 1)
        return {
            "rolled_back": self._rolled_back,
            "canary_percent": 0 if self._rolled_back else self.canary_percent,
            "window_samples": len(samples),
            "canary_error_rate": round(error_rate, 4),
            "active_model": self.stable_model if self._rolled_back else f"{self.canary_percent}% {self.canary_model}",
        }


guard = ModelQualityGuard()
```

**Expected Token Savings:** Prevents cost blowout from a broken canary model
**Environment:** `pip install anthropic`

---

## Comparison Table

| Option | Rollout Control | Auto-Rollback | A/B Metrics | Per-User Override | Remote Update |
|--------|----------------|---------------|-------------|-------------------|---------------|
| 1: Env var flag | Instant (env change) | No | No | No | Via secrets mgr |
| 2: Canary percent | Gradual ramp | No | No | No | Via env var |
| 3: In-process store | File watch (10s) | No | No | Yes | File change |
| 4: A/B test | Cohort split | No | Yes (SQLite) | No | Code change |
| 5: Remote flags | TTL refresh (60s) | No | No | No | Config service |
| 6: Quality guard | Auto-rollback | Yes | Error rate | No | Code config |
