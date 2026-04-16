---
layout: solution
title: "Agent Doesn't Implement Feature Flags for Gradual Rollout"
category: general
description: "Deploying new agent prompts, models, or behaviors without feature flags forces all-or-nothing releases. Feature flags enable gradual rollout, A/B testing, and instant rollback without redeployment."
tags: [general, feature-flags, rollout, ab-testing, deployment, python]
---

## Problem

When an agent's prompt, model version, or tool behavior changes, shipping it to 100% of users simultaneously is high risk. A single regression impacts everyone. Feature flags let teams release changes to 1% of traffic, observe metrics, then gradually increase — or immediately roll back by flipping a flag, not by redeploying code.

## Solutions

### Option 1: Simple Percentage-Based Feature Flag

```python
import anthropic
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class FeatureFlag:
    name: str
    enabled: bool = False
    rollout_pct: float = 0.0   # 0.0–100.0
    allowed_users: list[str] = field(default_factory=list)  # Allowlist
    config: dict = field(default_factory=dict)  # Flag-specific config

class FeatureFlagStore:
    def __init__(self):
        self._flags: dict[str, FeatureFlag] = {}

    def register(self, flag: FeatureFlag) -> None:
        self._flags[flag.name] = flag

    def is_enabled(self, flag_name: str, user_id: str) -> bool:
        flag = self._flags.get(flag_name)
        if not flag or not flag.enabled:
            return False
        if user_id in flag.allowed_users:
            return True
        # Stable hash: same user always gets same bucket (0–100)
        bucket = int(hashlib.md5(f"{flag_name}:{user_id}".encode()).hexdigest(), 16) % 100
        return bucket < flag.rollout_pct

    def get_config(self, flag_name: str) -> dict:
        return self._flags.get(flag_name, FeatureFlag("")).config

    def set_rollout(self, flag_name: str, pct: float) -> None:
        if flag_name in self._flags:
            self._flags[flag_name].rollout_pct = pct
            print(f"[FLAGS] {flag_name} rollout → {pct:.0f}%")

# Global flag store
flags = FeatureFlagStore()
flags.register(FeatureFlag(
    name="new_system_prompt_v2",
    enabled=True,
    rollout_pct=20.0,  # 20% of users get new prompt
    config={
        "system_prompt": "You are a concise assistant. Keep answers under 2 sentences.",
    }
))
flags.register(FeatureFlag(
    name="use_claude_sonnet",
    enabled=True,
    rollout_pct=5.0,   # 5% of users get Sonnet instead of Haiku
    allowed_users=["beta-tester-1", "beta-tester-2"],
))

def get_agent_config(user_id: str) -> dict:
    config = {
        "model": "claude-haiku-4-5-20251001",
        "system": "You are a helpful assistant.",
        "max_tokens": 100,
    }
    if flags.is_enabled("use_claude_sonnet", user_id):
        config["model"] = "claude-sonnet-4-6"
        print(f"  [FLAG:sonnet] user={user_id}")
    if flags.is_enabled("new_system_prompt_v2", user_id):
        config["system"] = flags.get_config("new_system_prompt_v2")["system_prompt"]
        print(f"  [FLAG:prompt_v2] user={user_id}")
    return config

def run_for_user(client: anthropic.Anthropic, user_id: str, prompt: str) -> str:
    config = get_agent_config(user_id)
    response = client.messages.create(
        model=config["model"],
        max_tokens=config["max_tokens"],
        system=config["system"],
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

if __name__ == "__main__":
    client = anthropic.Anthropic()
    users = [f"user-{i:04d}" for i in range(10)] + ["beta-tester-1"]
    prompt = "Explain what a neural network is."

    for user_id in users:
        result = run_for_user(client, user_id, prompt)
        model = "sonnet" if flags.is_enabled("use_claude_sonnet", user_id) else "haiku"
        v2 = flags.is_enabled("new_system_prompt_v2", user_id)
        print(f"[{user_id}] model={model} prompt_v2={v2}: {result[:60]}\n")

# Expected Token Savings: Staged rollout limits exposure of expensive models to small % of users
# Environment: pip install anthropic
```

### Option 2: Flag Store with Targeting Rules and Override

```python
import anthropic
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Callable

class RuleOperator(Enum):
    EQUALS = "eq"
    CONTAINS = "contains"
    IN_LIST = "in"
    GTE = "gte"

@dataclass
class TargetingRule:
    attribute: str          # e.g. "plan", "country", "account_age_days"
    operator: RuleOperator
    value: Any

    def matches(self, context: dict) -> bool:
        actual = context.get(self.attribute)
        if actual is None:
            return False
        if self.operator == RuleOperator.EQUALS:
            return actual == self.value
        if self.operator == RuleOperator.CONTAINS:
            return self.value in str(actual)
        if self.operator == RuleOperator.IN_LIST:
            return actual in self.value
        if self.operator == RuleOperator.GTE:
            return float(actual) >= float(self.value)
        return False

@dataclass
class AdvancedFlag:
    name: str
    enabled: bool
    default_variant: str
    variants: dict[str, Any]   # variant_name → config
    rollout_pct: float = 100.0
    targeting_rules: list[TargetingRule] = field(default_factory=list)
    overrides: dict[str, str] = field(default_factory=dict)  # user_id → variant

class AdvancedFlagStore:
    def __init__(self):
        self._flags: dict[str, AdvancedFlag] = {}

    def register(self, flag: AdvancedFlag) -> None:
        self._flags[flag.name] = flag

    def evaluate(self, flag_name: str, user_id: str,
                 context: Optional[dict] = None) -> tuple[str, Any]:
        """Returns (variant_name, variant_config)."""
        flag = self._flags.get(flag_name)
        if not flag or not flag.enabled:
            return flag.default_variant, flag.variants.get(flag.default_variant) if flag else None

        # Override check
        if user_id in flag.overrides:
            variant = flag.overrides[user_id]
            return variant, flag.variants.get(variant)

        # Targeting rules (first match wins)
        ctx = context or {}
        for rule in flag.targeting_rules:
            if rule.matches(ctx):
                variant = "treatment"
                return variant, flag.variants.get(variant)

        # Rollout bucket
        bucket = int(hashlib.sha256(f"{flag_name}:{user_id}".encode()).hexdigest(), 16) % 100
        if bucket < flag.rollout_pct:
            variant = "treatment"
        else:
            variant = flag.default_variant
        return variant, flag.variants.get(variant)

# Setup flags
store = AdvancedFlagStore()
store.register(AdvancedFlag(
    name="model_experiment",
    enabled=True,
    default_variant="control",
    variants={
        "control":   {"model": "claude-haiku-4-5-20251001", "max_tokens": 100},
        "treatment": {"model": "claude-sonnet-4-6", "max_tokens": 150},
    },
    rollout_pct=25.0,
    targeting_rules=[
        TargetingRule("plan", RuleOperator.IN_LIST, ["pro", "enterprise"]),
    ],
    overrides={"power-user-1": "treatment"},
))

def run(client: anthropic.Anthropic, user_id: str, context: dict, prompt: str) -> dict:
    variant, cfg = store.evaluate("model_experiment", user_id, context)
    response = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        messages=[{"role": "user", "content": prompt}],
    )
    return {"user": user_id, "variant": variant, "model": cfg["model"],
            "result": response.content[0].text[:60]}

if __name__ == "__main__":
    client = anthropic.Anthropic()
    users = [
        ("user-free-01", {"plan": "free"}),
        ("user-pro-01",  {"plan": "pro"}),
        ("power-user-1", {"plan": "free"}),  # Override → treatment
        ("user-free-02", {"plan": "free"}),
    ]
    for user_id, ctx in users:
        result = run(client, user_id, ctx, "What is photosynthesis?")
        print(f"[{result['variant']:9}] {result['user']:15} model={result['model'].split('-')[1]}: {result['result'][:50]}")

# Expected Token Savings: Target expensive models only to users where ROI justifies cost
# Environment: pip install anthropic
```

### Option 3: File-Backed Feature Flag with Hot Reload

```python
import anthropic
import json
import time
import threading
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Any

FLAG_FILE = "/tmp/agent_flags.json"

DEFAULT_FLAGS = {
    "extended_thinking": {
        "enabled": False,
        "rollout_pct": 0,
        "config": {"budget_tokens": 2000}
    },
    "verbose_responses": {
        "enabled": True,
        "rollout_pct": 50,
        "config": {}
    },
    "new_tool_schema": {
        "enabled": True,
        "rollout_pct": 10,
        "config": {"version": "2.0"}
    },
}

class HotReloadFlagStore:
    def __init__(self, flag_file: str = FLAG_FILE, poll_seconds: float = 5.0):
        self._path = Path(flag_file)
        self._poll_seconds = poll_seconds
        self._flags: dict = {}
        self._last_mtime: float = 0.0
        self._lock = threading.RLock()

        # Write defaults if file doesn't exist
        if not self._path.exists():
            self._path.write_text(json.dumps(DEFAULT_FLAGS, indent=2))

        self._reload()
        self._start_watcher()

    def _reload(self) -> bool:
        try:
            mtime = self._path.stat().st_mtime
            if mtime == self._last_mtime:
                return False
            flags = json.loads(self._path.read_text())
            with self._lock:
                self._flags = flags
                self._last_mtime = mtime
            print(f"[FLAGS] Reloaded {len(flags)} flags from {self._path}")
            return True
        except Exception as e:
            print(f"[FLAGS] Reload error: {e}")
            return False

    def _start_watcher(self) -> None:
        def watch():
            while True:
                time.sleep(self._poll_seconds)
                self._reload()
        t = threading.Thread(target=watch, daemon=True)
        t.start()

    def is_enabled(self, flag_name: str, user_id: str) -> bool:
        with self._lock:
            flag = self._flags.get(flag_name, {})
        if not flag.get("enabled", False):
            return False
        pct = flag.get("rollout_pct", 0)
        bucket = int(hashlib.md5(f"{flag_name}:{user_id}".encode()).hexdigest(), 16) % 100
        return bucket < pct

    def get_config(self, flag_name: str) -> dict:
        with self._lock:
            return self._flags.get(flag_name, {}).get("config", {})

    def update_flag(self, flag_name: str, updates: dict) -> None:
        """Update a flag and write to file (simulates ops team changing rollout %)."""
        with self._lock:
            if flag_name not in self._flags:
                self._flags[flag_name] = {}
            self._flags[flag_name].update(updates)
            self._path.write_text(json.dumps(self._flags, indent=2))
        print(f"[FLAGS] Updated {flag_name}: {updates}")

flag_store = HotReloadFlagStore()

def run_agent(client: anthropic.Anthropic, user_id: str, prompt: str) -> str:
    kwargs: dict[str, Any] = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": prompt}],
    }

    if flag_store.is_enabled("verbose_responses", user_id):
        kwargs["system"] = "Provide detailed, thorough responses."

    response = client.messages.create(**kwargs)
    return response.content[0].text

if __name__ == "__main__":
    client = anthropic.Anthropic()

    users = [f"user-{i:03d}" for i in range(5)]
    for user_id in users:
        enabled = flag_store.is_enabled("verbose_responses", user_id)
        result = run_agent(client, user_id, "What is gravity?")
        print(f"[{user_id}] verbose={enabled}: {result[:60]}")

    # Simulate ops team increasing rollout
    print("\n--- Increasing verbose rollout to 80% ---")
    flag_store.update_flag("verbose_responses", {"rollout_pct": 80})

    for user_id in users:
        enabled = flag_store.is_enabled("verbose_responses", user_id)
        print(f"  {user_id}: verbose={enabled}")

# Expected Token Savings: Hot reload avoids redeployment; gradual rollout limits expensive flag exposure
# Environment: pip install anthropic
```

### Option 4: Async Flag Evaluation with Metrics Collection

```python
import anthropic
import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Any, Optional

@dataclass
class FlagEvaluation:
    flag_name: str
    user_id: str
    variant: str
    evaluated_at: float = field(default_factory=time.time)
    response_ok: bool = True
    latency_ms: float = 0.0

class AsyncFlagStore:
    def __init__(self):
        self._flags: dict[str, dict] = {
            "prompt_chain_v2": {
                "enabled": True,
                "rollout_pct": 30,
                "variants": {
                    "control":   {"steps": 1},
                    "treatment": {"steps": 2},
                },
            },
        }
        self._evaluations: list[FlagEvaluation] = []
        self._lock = asyncio.Lock()

    async def evaluate(self, flag_name: str, user_id: str) -> tuple[str, dict]:
        flag = self._flags.get(flag_name, {})
        if not flag.get("enabled"):
            return "control", flag.get("variants", {}).get("control", {})
        bucket = int(hashlib.md5(f"{flag_name}:{user_id}".encode()).hexdigest(), 16) % 100
        variant = "treatment" if bucket < flag["rollout_pct"] else "control"
        config = flag.get("variants", {}).get(variant, {})
        return variant, config

    async def record(self, evaluation: FlagEvaluation) -> None:
        async with self._lock:
            self._evaluations.append(evaluation)

    async def metrics(self) -> dict:
        async with self._lock:
            by_variant: dict[str, dict] = defaultdict(lambda: {"count": 0, "ok": 0, "latency": []})
            for e in self._evaluations:
                key = f"{e.flag_name}:{e.variant}"
                by_variant[key]["count"] += 1
                if e.response_ok:
                    by_variant[key]["ok"] += 1
                by_variant[key]["latency"].append(e.latency_ms)
            return {
                k: {
                    "count": v["count"],
                    "success_rate": v["ok"] / max(v["count"], 1),
                    "avg_latency_ms": sum(v["latency"]) / max(len(v["latency"]), 1),
                }
                for k, v in by_variant.items()
            }

store = AsyncFlagStore()

async def run_with_flag(client: anthropic.AsyncAnthropic,
                         user_id: str, prompt: str) -> str:
    variant, config = await store.evaluate("prompt_chain_v2", user_id)
    steps = config.get("steps", 1)
    t0 = time.monotonic()
    ok = True

    messages = [{"role": "user", "content": prompt}]
    final_response = ""

    try:
        for step in range(steps):
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=messages,
            )
            final_response = response.content[0].text
            if step < steps - 1:
                # For multi-step: add response and ask for refinement
                messages.append({"role": "assistant", "content": final_response})
                messages.append({"role": "user", "content": "Refine and improve your answer."})
    except Exception:
        ok = False

    await store.record(FlagEvaluation(
        flag_name="prompt_chain_v2",
        user_id=user_id, variant=variant,
        response_ok=ok,
        latency_ms=(time.monotonic() - t0) * 1000,
    ))
    return final_response

async def main():
    client = anthropic.AsyncAnthropic()
    users = [f"user-{i:04d}" for i in range(8)]
    prompt = "Explain what machine learning is."

    results = await asyncio.gather(*[
        run_with_flag(client, user_id, prompt) for user_id in users
    ])

    for user_id, result in zip(users, results):
        variant, _ = await store.evaluate("prompt_chain_v2", user_id)
        print(f"[{variant:9}] {user_id}: {result[:55]}")

    metrics = await store.metrics()
    print("\nFlag metrics:")
    for key, m in metrics.items():
        print(f"  {key}: count={m['count']} success={m['success_rate']:.0%} "
              f"avg_latency={m['avg_latency_ms']:.0f}ms")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Multi-step chain costs 2× — feature flags limit exposure during testing
# Environment: pip install anthropic
```

### Option 5: Flag Lifecycle with Canary, Ramp, and GA Stages

```python
import anthropic
import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class Stage(Enum):
    DISABLED = "disabled"   # 0% — off
    CANARY = "canary"       # 1-5% — initial signal
    RAMP = "ramp"           # 5-50% — expanding
    GA = "ga"               # 100% — general availability
    ROLLBACK = "rollback"   # 0% — emergency off

STAGE_PCT = {
    Stage.DISABLED: 0,
    Stage.CANARY: 2,
    Stage.RAMP: 20,
    Stage.GA: 100,
    Stage.ROLLBACK: 0,
}

@dataclass
class LifecycleFlag:
    name: str
    stage: Stage = Stage.DISABLED
    stage_history: list[tuple[Stage, float]] = field(default_factory=list)
    config: dict = field(default_factory=dict)

    @property
    def rollout_pct(self) -> int:
        return STAGE_PCT[self.stage]

    def advance(self, new_stage: Stage, reason: str = "") -> None:
        self.stage_history.append((self.stage, time.time()))
        self.stage = new_stage
        print(f"[FLAG:{self.name}] {self.stage_history[-1][0].value} → {new_stage.value} "
              f"({self.rollout_pct}%) {reason}")

    def rollback(self) -> None:
        self.advance(Stage.ROLLBACK, "EMERGENCY ROLLBACK")

class LifecycleFlagStore:
    def __init__(self):
        self._flags: dict[str, LifecycleFlag] = {}

    def register(self, flag: LifecycleFlag) -> None:
        self._flags[flag.name] = flag

    def get_flag(self, name: str) -> Optional[LifecycleFlag]:
        return self._flags.get(name)

    def is_enabled(self, flag_name: str, user_id: str) -> bool:
        flag = self._flags.get(flag_name)
        if not flag or flag.stage in (Stage.DISABLED, Stage.ROLLBACK):
            return False
        bucket = int(hashlib.sha256(f"{flag_name}:{user_id}".encode()).hexdigest(), 16) % 100
        return bucket < flag.rollout_pct

def simulate_lifecycle():
    client = anthropic.Anthropic()
    store = LifecycleFlagStore()

    flag = LifecycleFlag(
        name="smart_routing_v3",
        config={"model": "claude-sonnet-4-6", "system": "You route tasks intelligently."}
    )
    store.register(flag)

    users = [f"user-{i:04d}" for i in range(20)]
    prompt = "Classify this task: write a Python sort function."

    for stage in [Stage.CANARY, Stage.RAMP, Stage.GA]:
        flag.advance(stage)
        enabled_users = [u for u in users if store.is_enabled("smart_routing_v3", u)]
        print(f"  Users in treatment: {len(enabled_users)}/{len(users)}")

        # Sample one enabled user
        if enabled_users:
            sample_user = enabled_users[0]
            cfg = flag.config
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",  # Use haiku for cost in demo
                max_tokens=50,
                system=cfg.get("system", ""),
                messages=[{"role": "user", "content": prompt}],
            )
            print(f"  Sample ({sample_user}): {response.content[0].text[:60]}")

    # Simulate incident — rollback
    print("\n--- Simulating incident: rolling back ---")
    flag.rollback()
    still_enabled = [u for u in users if store.is_enabled("smart_routing_v3", u)]
    print(f"After rollback, enabled users: {len(still_enabled)}")

if __name__ == "__main__":
    simulate_lifecycle()

# Expected Token Savings: Canary at 2% limits exposure of risky expensive model upgrades to 98%
# Environment: pip install anthropic
```

### Option 6: Multi-Flag Composition with Dependency Resolution

```python
import anthropic
import hashlib
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class DependentFlag:
    name: str
    enabled: bool
    rollout_pct: float
    depends_on: Optional[str] = None  # Parent flag that must be enabled
    config: dict = field(default_factory=dict)

class ComposedFlagStore:
    """Flags can depend on other flags — child is only active if parent is active."""

    def __init__(self):
        self._flags: dict[str, DependentFlag] = {}

    def register(self, flag: DependentFlag) -> None:
        self._flags[flag.name] = flag

    def is_enabled(self, flag_name: str, user_id: str,
                   _seen: Optional[set] = None) -> bool:
        _seen = _seen or set()
        if flag_name in _seen:
            return False  # Cycle guard
        _seen.add(flag_name)

        flag = self._flags.get(flag_name)
        if not flag or not flag.enabled:
            return False

        # Check parent dependency
        if flag.depends_on:
            if not self.is_enabled(flag.depends_on, user_id, _seen):
                return False

        bucket = int(hashlib.md5(f"{flag_name}:{user_id}".encode()).hexdigest(), 16) % 100
        return bucket < flag.rollout_pct

    def active_flags(self, user_id: str) -> list[str]:
        return [name for name in self._flags if self.is_enabled(name, user_id)]

    def get_agent_config(self, user_id: str) -> dict:
        config = {
            "model": "claude-haiku-4-5-20251001",
            "system": "You are a helpful assistant.",
            "max_tokens": 100,
        }
        for flag_name in self.active_flags(user_id):
            flag = self._flags[flag_name]
            config.update(flag.config)
        return config

store = ComposedFlagStore()

# "new_pipeline" is the parent — must be on for children to apply
store.register(DependentFlag(
    "new_pipeline", enabled=True, rollout_pct=40,
    config={"system": "You are a next-gen AI assistant."},
))
store.register(DependentFlag(
    "extended_context", enabled=True, rollout_pct=20,
    depends_on="new_pipeline",   # Only active if new_pipeline is on
    config={"max_tokens": 300},
))
store.register(DependentFlag(
    "sonnet_upgrade", enabled=True, rollout_pct=10,
    depends_on="new_pipeline",   # Only active if new_pipeline is on
    config={"model": "claude-sonnet-4-6"},
))

if __name__ == "__main__":
    client = anthropic.Anthropic()
    users = [f"user-{i:04d}" for i in range(10)]

    for user_id in users:
        active = store.active_flags(user_id)
        cfg = store.get_agent_config(user_id)
        response = client.messages.create(
            model=cfg["model"],
            max_tokens=cfg["max_tokens"],
            system=cfg["system"],
            messages=[{"role": "user", "content": "What is AI?"}],
        )
        print(f"[{user_id}] flags={active} model={cfg['model'].split('-')[1]}: "
              f"{response.content[0].text[:55]}")

# Expected Token Savings: Dependency composition ensures expensive child flags only apply to eligible users
# Environment: pip install anthropic
```

## Comparison

| Option | Storage | Targeting | Hot Reload | Metrics | Best For |
|--------|---------|-----------|-----------|---------|----------|
| 1. Percentage + Allowlist | In-memory | User ID + allowlist | No | None | Simple rollouts |
| 2. Rules + Overrides | In-memory | Attribute targeting | No | None | Segment-based |
| 3. File-backed | JSON file | Percentage | Yes (poll) | None | Ops-friendly |
| 4. Async + Metrics | In-memory | Percentage | No | Yes | A/B analysis |
| 5. Lifecycle stages | In-memory | Percentage | No | None | Safe releases |
| 6. Dependency composition | In-memory | Percentage | No | None | Feature trees |
