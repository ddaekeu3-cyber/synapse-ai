---
layout: solution
title: "Agent doesn't use feature flags for gradual rollout"
category: general
description: "Agent deploys new behavior to all users simultaneously with no ability to roll back quickly, causing widespread impact when a new prompt or model change misbehaves."
tags: [general, feature-flags, rollout, reliability, deployment]
---

## Symptom

A new model version, revised system prompt, or updated tool schema is deployed and immediately affects 100% of production traffic. When the change causes regressions (worse responses, increased hallucinations, broken tool calls), the only remediation is a full re-deploy — which takes minutes to hours. In that window, every user experiences the broken behavior.

```
Deploy new system prompt → 100% of users affected immediately
Discover regression 10 min later → Full re-deploy needed (15 min)
Total exposure window: 25 minutes × all users
```

## Root Cause

The agent has no mechanism to vary behavior based on a user or request cohort. Every configuration change — model version, system prompt, temperature, tool list — is a binary flip from old to new with no intermediate state and no quick kill switch.

## Fix

Implement feature flags that route a configurable percentage of traffic to new behavior. Keep the old behavior as the default. Monitor metrics per cohort and promote (or roll back) by adjusting the percentage.

---

### Option 1 — Simple percentage-based flag using user ID hash

```python
import anthropic
import hashlib

client = anthropic.Anthropic()

# Feature flag registry — adjust percentages without redeploying
FLAGS = {
    "new_system_prompt":  {"enabled": True, "rollout_pct": 10},  # 10% of users
    "claude_sonnet":      {"enabled": True, "rollout_pct": 5},
    "extended_tools":     {"enabled": False, "rollout_pct": 0},
}

def is_flag_enabled(flag_name: str, user_id: str) -> bool:
    flag = FLAGS.get(flag_name, {"enabled": False, "rollout_pct": 0})
    if not flag["enabled"]:
        return False
    # Deterministic bucketing: same user always gets same experience
    raw = f"{flag_name}:{user_id}".encode()
    bucket = int(hashlib.sha256(raw).hexdigest(), 16) % 100
    return bucket < flag["rollout_pct"]

# System prompts
SYSTEM_V1 = "You are a helpful assistant."
SYSTEM_V2 = "You are a concise, expert assistant. Prioritize clarity and brevity."

def get_config(user_id: str) -> dict:
    return {
        "model":  "claude-sonnet-4-6" if is_flag_enabled("claude_sonnet", user_id) else "claude-haiku-4-5-20251001",
        "system": SYSTEM_V2 if is_flag_enabled("new_system_prompt", user_id) else SYSTEM_V1,
        "flags":  {f: is_flag_enabled(f, user_id) for f in FLAGS},
    }

def agent_respond(user_id: str, message: str) -> tuple[str, dict]:
    config = get_config(user_id)
    response = client.messages.create(
        model=config["model"],
        max_tokens=128,
        system=config["system"],
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text.strip(), config

# Simulate 10 users
for uid in [f"user_{i:03d}" for i in range(10)]:
    reply, cfg = agent_respond(uid, "What is machine learning in one sentence?")
    flags_on = [f for f, v in cfg["flags"].items() if v]
    print(f"{uid} | model={cfg['model'][-10:]} | flags={flags_on or ['none']}")
    print(f"  → {reply[:80]}\n")
```

**Expected Token Savings:** No direct token savings; prevents costly incident response by limiting blast radius to 5–10% of users; allows data-driven promotion without a code deploy.

**Environment:** Any production agent; user ID bucketing is deterministic so the same user always gets a consistent experience.

---

### Option 2 — Environment-driven flag loader with hot reload

```python
import anthropic
import json
import os
import time
from pathlib import Path

client = anthropic.Anthropic()

FLAGS_FILE = Path("/tmp/agent_flags.json")

DEFAULT_FLAGS = {
    "new_retrieval_tool": {"enabled": False, "rollout_pct": 0, "model_override": None},
    "extended_context":   {"enabled": False, "rollout_pct": 0, "model_override": None},
    "verbose_system":     {"enabled": True,  "rollout_pct": 20, "model_override": None},
}

def write_flags(flags: dict) -> None:
    FLAGS_FILE.write_text(json.dumps(flags, indent=2))

def load_flags() -> dict:
    """Load flags from file — supports live edits without restart."""
    if FLAGS_FILE.exists():
        try:
            return json.loads(FLAGS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return DEFAULT_FLAGS

# Initialize flags file
write_flags(DEFAULT_FLAGS)

class FlagLoader:
    def __init__(self, reload_interval: float = 5.0) -> None:
        self._flags: dict = load_flags()
        self._last_load = time.monotonic()
        self._reload_interval = reload_interval

    def get(self, flag_name: str, user_id: str) -> bool:
        # Hot-reload check
        if time.monotonic() - self._last_load > self._reload_interval:
            self._flags = load_flags()
            self._last_load = time.monotonic()

        flag = self._flags.get(flag_name, {"enabled": False, "rollout_pct": 0})
        if not flag["enabled"]:
            return False
        import hashlib
        bucket = int(hashlib.md5(f"{flag_name}:{user_id}".encode()).hexdigest(), 16) % 100
        return bucket < flag["rollout_pct"]

    def override_model(self, flag_name: str) -> str | None:
        return self._flags.get(flag_name, {}).get("model_override")

loader = FlagLoader(reload_interval=2.0)

SYSTEM_DEFAULT = "You are a helpful assistant. Be thorough."
SYSTEM_VERBOSE  = "You are a detailed assistant. Explain every step clearly and provide examples."

def respond(user_id: str, message: str) -> str:
    use_verbose = loader.get("verbose_system", user_id)
    system = SYSTEM_VERBOSE if use_verbose else SYSTEM_DEFAULT

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=system,
        messages=[{"role": "user", "content": message}],
    )
    variant = "verbose" if use_verbose else "default"
    print(f"  [{user_id}] variant={variant}")
    return response.content[0].text.strip()

# Simulate production traffic
for uid in ["alice", "bob", "carol", "dave", "eve"]:
    reply = respond(uid, "Explain what an API is.")
    print(f"  {uid}: {reply[:80]}\n")

# Simulate ops team updating flags live (no restart needed)
print("\n[OPS] Increasing verbose_system rollout to 80%...")
flags = load_flags()
flags["verbose_system"]["rollout_pct"] = 80
write_flags(flags)

time.sleep(3)  # wait for hot reload

print("\nAfter rollout increase:")
for uid in ["alice", "bob", "carol", "dave", "eve"]:
    respond(uid, "What is an API?")
```

**Expected Token Savings:** Hot-reload eliminates re-deploy cost; ops can adjust rollout percentage or kill a flag via file edit in seconds; no code changes required.

**Environment:** File-system accessible deployments; replace the file backend with Redis, LaunchDarkly, or AWS AppConfig for distributed agents.

---

### Option 3 — A/B test flag with metric tracking

```python
import anthropic
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class Experiment:
    name: str
    variants: list[str]           # e.g. ["control", "treatment"]
    rollout_pct: int = 50         # % of users in the experiment
    active: bool = True

@dataclass
class ExperimentMetrics:
    calls: int = 0
    total_output_tokens: int = 0
    errors: int = 0

class ABTestManager:
    def __init__(self) -> None:
        self._experiments: dict[str, Experiment] = {}
        self._metrics: dict[str, dict[str, ExperimentMetrics]] = defaultdict(
            lambda: defaultdict(ExperimentMetrics)
        )

    def register(self, exp: Experiment) -> None:
        self._experiments[exp.name] = exp

    def get_variant(self, exp_name: str, user_id: str) -> str | None:
        exp = self._experiments.get(exp_name)
        if not exp or not exp.active:
            return None
        # Check if user is in the experiment
        bucket = int(hashlib.sha256(f"{exp_name}:bucket:{user_id}".encode()).hexdigest(), 16) % 100
        if bucket >= exp.rollout_pct:
            return None  # user not in experiment
        # Assign variant
        variant_bucket = int(hashlib.sha256(f"{exp_name}:variant:{user_id}".encode()).hexdigest(), 16)
        return exp.variants[variant_bucket % len(exp.variants)]

    def record(self, exp_name: str, variant: str, output_tokens: int, error: bool = False) -> None:
        m = self._metrics[exp_name][variant]
        m.calls += 1
        m.total_output_tokens += output_tokens
        if error:
            m.errors += 1

    def report(self) -> dict:
        out = {}
        for exp_name, variants in self._metrics.items():
            out[exp_name] = {
                variant: {
                    "calls": m.calls,
                    "avg_output_tokens": round(m.total_output_tokens / max(m.calls, 1)),
                    "error_rate": round(m.errors / max(m.calls, 1), 3),
                }
                for variant, m in variants.items()
            }
        return out

ab = ABTestManager()
ab.register(Experiment(
    name="concise_vs_detailed",
    variants=["control", "concise", "detailed"],
    rollout_pct=100,  # all users for demo
))

SYSTEMS = {
    "control":  "You are a helpful assistant.",
    "concise":  "You are a helpful assistant. Be brief — one to two sentences maximum.",
    "detailed": "You are a thorough assistant. Explain with examples and context.",
}

def respond_ab(user_id: str, message: str) -> str:
    variant = ab.get_variant("concise_vs_detailed", user_id) or "control"
    system  = SYSTEMS[variant]

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        out_tokens = response.usage.output_tokens
        ab.record("concise_vs_detailed", variant, out_tokens)
        return f"[{variant:8}] {response.content[0].text.strip()[:80]}"
    except Exception as e:
        ab.record("concise_vs_detailed", variant, 0, error=True)
        return f"[{variant:8}] ERROR: {e}"

users = [f"user_{i}" for i in range(15)]
for uid in users:
    result = respond_ab(uid, "Explain what an API gateway does.")
    print(f"{uid}: {result}")

print("\n=== A/B Metrics ===")
print(json.dumps(ab.report(), indent=2))
```

**Expected Token Savings:** A/B metrics expose which variant uses fewer tokens while maintaining quality; "concise" variants typically save 40–60% output tokens and can be promoted with evidence.

**Environment:** Product teams that want data before committing to a prompt change; metrics feed into dashboards or alerting systems.

---

### Option 4 — Kill switch flag for emergency rollback

```python
import anthropic
import json
import time
from pathlib import Path

client = anthropic.Anthropic()

KILL_SWITCHES_FILE = Path("/tmp/agent_kill_switches.json")

# Kill switches: set to True to instantly disable a feature for ALL users
KILL_SWITCHES: dict[str, bool] = {
    "new_tool_schema":    False,
    "claude_opus":        False,
    "experimental_chain": False,
}

def write_kill_switches(ks: dict) -> None:
    KILL_SWITCHES_FILE.write_text(json.dumps(ks))

def read_kill_switches() -> dict[str, bool]:
    if KILL_SWITCHES_FILE.exists():
        try:
            return json.loads(KILL_SWITCHES_FILE.read_text())
        except Exception:
            pass
    return KILL_SWITCHES.copy()

write_kill_switches(KILL_SWITCHES)

_last_ks_read = 0.0
_cached_ks: dict[str, bool] = {}

def is_killed(feature: str) -> bool:
    global _last_ks_read, _cached_ks
    now = time.monotonic()
    if now - _last_ks_read > 1.0:   # re-read every second
        _cached_ks = read_kill_switches()
        _last_ks_read = now
    return _cached_ks.get(feature, False)

TOOL_V2 = {
    "name": "get_data",
    "description": "Get data (new experimental schema).",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}, "format": {"type": "string", "enum": ["json", "csv"]}},
        "required": ["query", "format"],
    },
}

TOOL_V1 = {
    "name": "get_data",
    "description": "Get data.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}

def respond(user_id: str, message: str) -> str:
    if is_killed("new_tool_schema"):
        tools = [TOOL_V1]
        variant = "v1_safe"
    else:
        tools = [TOOL_V2]
        variant = "v2_experimental"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=tools,
        messages=[{"role": "user", "content": message}],
    )
    return f"[{variant}] stop={response.stop_reason}"

# Normal operation
print("Before kill switch:")
for uid in ["alice", "bob"]:
    print(f"  {uid}: {respond(uid, 'Get data for query: sales')}")

# Ops team kills the feature during an incident
print("\n[OPS INCIDENT] Activating kill switch for new_tool_schema...")
ks = read_kill_switches()
ks["new_tool_schema"] = True
write_kill_switches(ks)

time.sleep(1.5)  # wait for cache to expire

print("\nAfter kill switch (should use v1_safe for all):")
for uid in ["alice", "bob", "carol"]:
    print(f"  {uid}: {respond(uid, 'Get data for query: revenue')}")
```

**Expected Token Savings:** Kill switches enable sub-second rollback for all users; incident duration drops from 15+ minutes (re-deploy) to 1–2 seconds (file edit); prevents continued token spend on a misbehaving feature.

**Environment:** Any production agent; hook kill switches to PagerDuty runbooks or Slack /slash commands for one-click incident response.

---

### Option 5 — Canary deployment with automatic promotion

```python
import anthropic
import hashlib
import time
import statistics
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CanaryConfig:
    name: str
    initial_pct: int    = 5      # start at 5%
    max_pct: int        = 100
    promotion_interval: float = 30.0   # seconds between auto-promotions
    error_rate_limit: float   = 0.05   # roll back if error rate > 5%
    latency_limit_ms: float   = 2000.0

@dataclass
class CanaryState:
    config: CanaryConfig
    current_pct: int                  = field(init=False)
    promoted_at: float                = field(default_factory=time.monotonic)
    errors: list[bool]                = field(default_factory=list)
    latencies: list[float]            = field(default_factory=list)
    rolled_back: bool                 = False

    def __post_init__(self) -> None:
        self.current_pct = self.config.initial_pct

    def record(self, error: bool, latency_ms: float) -> None:
        self.errors.append(error)
        self.latencies.append(latency_ms)
        # Keep only last 20 observations
        self.errors    = self.errors[-20:]
        self.latencies = self.latencies[-20:]

    @property
    def should_rollback(self) -> bool:
        if len(self.errors) < 5:
            return False
        err_rate = sum(self.errors) / len(self.errors)
        avg_lat  = statistics.mean(self.latencies)
        if err_rate > self.config.error_rate_limit:
            print(f"[CANARY] ROLLBACK: error_rate={err_rate:.1%} > {self.config.error_rate_limit:.1%}")
            return True
        if avg_lat > self.config.latency_limit_ms:
            print(f"[CANARY] ROLLBACK: avg_latency={avg_lat:.0f}ms > {self.config.latency_limit_ms}ms")
            return True
        return False

    def maybe_promote(self) -> None:
        if self.rolled_back or self.current_pct >= self.config.max_pct:
            return
        if time.monotonic() - self.promoted_at >= self.config.promotion_interval:
            self.current_pct = min(self.current_pct + 10, self.config.max_pct)
            self.promoted_at = time.monotonic()
            print(f"[CANARY] Promoted to {self.current_pct}%")

canary = CanaryState(CanaryConfig("new_prompt", initial_pct=20, promotion_interval=5.0))

SYSTEM_OLD = "You are a helpful assistant."
SYSTEM_NEW  = "You are a concise, expert assistant."

def is_canary(user_id: str) -> bool:
    if canary.rolled_back:
        return False
    bucket = int(hashlib.sha256(f"canary:{user_id}".encode()).hexdigest(), 16) % 100
    return bucket < canary.current_pct

def respond_canary(user_id: str, message: str) -> str:
    in_canary = is_canary(user_id)
    system    = SYSTEM_NEW if in_canary else SYSTEM_OLD

    t0 = time.monotonic()
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        latency = (time.monotonic() - t0) * 1000
        canary.record(error=False, latency_ms=latency)
        variant = "NEW" if in_canary else "OLD"
        return f"[{variant} {canary.current_pct}%] {response.content[0].text.strip()[:60]}"
    except Exception as e:
        canary.record(error=True, latency_ms=30000)
        if canary.should_rollback:
            canary.rolled_back = True
        return f"[ERROR] {e}"

for i in range(20):
    canary.maybe_promote()
    uid = f"user_{i%8}"
    result = respond_canary(uid, "Name one programming language.")
    print(f"[t={i:2d}] {uid}: {result}")
    time.sleep(0.5)
```

**Expected Token Savings:** Canary limits blast radius to a small percentage during testing; automatic promotion eliminates manual intervention; rollback on error rate spike prevents sustained token waste on broken prompts.

**Environment:** Agents with measurable quality metrics (latency, error rate, downstream signals); combine with LLM-as-judge for quality-based promotion decisions.

---

### Option 6 — Feature flag SDK wrapper around LaunchDarkly / OpenFeature pattern

```python
import anthropic
import hashlib
import json
import os
from pathlib import Path

client = anthropic.Anthropic()

# Minimal implementation following the OpenFeature SDK pattern
class FeatureFlagProvider:
    """
    Drop-in interface compatible with OpenFeature SDK conventions.
    Replace the _load_flags() backend with LaunchDarkly, Flagsmith,
    AWS AppConfig, or any other provider.
    """

    def __init__(self, flags_source: str = "/tmp/openfeature_flags.json") -> None:
        self._source = Path(flags_source)
        self._flags: dict = {}
        self._load()

    def _load(self) -> None:
        if self._source.exists():
            self._flags = json.loads(self._source.read_text())

    def bool_variation(self, flag_key: str, context: dict, default: bool) -> bool:
        self._load()  # re-read on every call for simplicity; use TTL cache in production
        flag = self._flags.get(flag_key)
        if flag is None:
            return default
        if flag.get("kind") == "percentage":
            user_id = context.get("user_id", "anonymous")
            bucket = int(hashlib.sha256(f"{flag_key}:{user_id}".encode()).hexdigest(), 16) % 100
            return bucket < flag.get("rollout_pct", 0)
        return bool(flag.get("value", default))

    def string_variation(self, flag_key: str, context: dict, default: str) -> str:
        self._load()
        flag = self._flags.get(flag_key)
        if flag is None:
            return default
        return str(flag.get("value", default))

# Initialize flag file
INITIAL_FLAGS = {
    "use_extended_system_prompt": {"kind": "percentage", "rollout_pct": 25},
    "model_version":              {"kind": "static",     "value": "claude-haiku-4-5-20251001"},
    "max_tokens_override":        {"kind": "static",     "value": 128},
}
Path("/tmp/openfeature_flags.json").write_text(json.dumps(INITIAL_FLAGS, indent=2))

provider = FeatureFlagProvider()

SYSTEM_DEFAULT  = "You are a helpful assistant."
SYSTEM_EXTENDED = "You are a helpful, knowledgeable assistant. Be precise and cite your reasoning."

def respond(user_id: str, message: str) -> str:
    ctx = {"user_id": user_id}

    use_extended = provider.bool_variation("use_extended_system_prompt", ctx, default=False)
    model        = provider.string_variation("model_version", ctx, default="claude-haiku-4-5-20251001")
    max_tokens   = int(provider.string_variation("max_tokens_override", ctx, default="128"))

    system = SYSTEM_EXTENDED if use_extended else SYSTEM_DEFAULT

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": message}],
    )

    variant = "extended" if use_extended else "default"
    return f"[{variant}] {response.content[0].text.strip()[:80]}"

for uid in ["alice", "bob", "carol", "dave", "eve", "frank", "grace", "henry"]:
    result = respond(uid, "What is dependency injection?")
    print(f"{uid}: {result}")

# Demonstrate ops update: increase rollout
print("\n[OPS] Increasing rollout to 75%...")
flags = json.loads(Path("/tmp/openfeature_flags.json").read_text())
flags["use_extended_system_prompt"]["rollout_pct"] = 75
Path("/tmp/openfeature_flags.json").write_text(json.dumps(flags))

print("\nAfter rollout increase:")
for uid in ["alice", "bob", "carol", "dave", "eve"]:
    result = respond(uid, "What is dependency injection?")
    print(f"{uid}: {result}")
```

**Expected Token Savings:** OpenFeature-compatible interface allows swapping backends (LaunchDarkly, Unleash, Flagsmith) without changing agent code; `string_variation` for model and max_tokens enables token budget experiments without re-deploying.

**Environment:** Enterprise agents requiring integration with existing feature flag infrastructure; the interface is a thin wrapper — replace `_load()` with an SDK client call.

---

## Comparison

| Option | Rollout Strategy | Rollback Speed | Metrics | Best For |
|--------|-----------------|---------------|---------|---------|
| 1 — User ID hash | Percentage | Config change | None | Simple deployments |
| 2 — Hot-reload file | Percentage | < 5 seconds | None | Ops-friendly updates |
| 3 — A/B test | Multi-variant | Config change | Token / error rate | Data-driven decisions |
| 4 — Kill switch | All-or-nothing | < 1 second | None | Incident response |
| 5 — Canary + auto-promote | Incremental % | Auto-rollback | Error rate + latency | Safe progressive rollout |
| 6 — OpenFeature SDK | Provider-backed | Provider SLA | Provider metrics | Enterprise integration |

**Recommended default:** Option 4 (kill switch) as a baseline for every feature — it costs nothing until an incident occurs. Add Option 1 (percentage rollout) for new prompts and Option 5 (canary) for high-risk changes like model upgrades.
