---
title: "Agent Doesn't Implement Feature Toggle for Gradual Rollout"
description: "AI agents that deploy new prompt logic, tools, or model versions all-at-once risk production incidents with no fast rollback. Feature toggles let you release changes to 1% of users first, expand when metrics look good, and kill the change instantly without redeployment."
date: 2025-02-03
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-feature-toggle-for-gradual-rollout
tags:
  - feature-toggles
  - gradual-rollout
  - canary
  - rollback
  - experimentation
  - reliability
symptoms:
  - "New prompt or model change ships to 100% of users simultaneously"
  - "Rolling back a bad LLM prompt requires a full redeployment"
  - "No way to A/B test two prompt variants without separate deployments"
  - "A single bad tool update breaks all sessions at once"
  - "Cannot measure the impact of a change on a subset of users before full rollout"
---

## Problem

Agent behaviour is driven by prompts, model choices, and tool configurations that change frequently. Deploying each change as a hard cutover means:

- If the new prompt causes hallucinations, every user is affected immediately.
- Rollback requires a redeployment cycle (minutes to hours).
- You cannot measure whether the change is better before giving it to everyone.

Feature toggles (flags) solve this by separating **deployment** (shipping the code) from **release** (turning on the behaviour). A toggle can be flipped in milliseconds from a config store; no code redeployment needed.

---

## Solution 1: Simple In-Memory Feature Flag Store

A lightweight flag store with per-user rollout percentages. Each flag has a target percentage; users are bucketed deterministically by their user ID hash.

```python
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class FeatureFlag:
    name: str
    enabled: bool = False
    rollout_pct: float = 0.0       # 0.0 – 100.0
    override_users: set = field(default_factory=set)   # always on for these users
    excluded_users: set = field(default_factory=set)   # always off for these users
    metadata: Dict[str, Any] = field(default_factory=dict)


class FeatureFlagStore:
    """
    In-memory feature flag store with deterministic per-user bucketing.

    Usage:
        store = FeatureFlagStore()
        store.add(FeatureFlag("new_prompt_v2", enabled=True, rollout_pct=10.0))

        if store.is_enabled("new_prompt_v2", user_id="user_123"):
            prompt = NEW_PROMPT
        else:
            prompt = OLD_PROMPT
    """

    def __init__(self):
        self._flags: Dict[str, FeatureFlag] = {}

    def add(self, flag: FeatureFlag):
        self._flags[flag.name] = flag

    def set_rollout(self, name: str, pct: float):
        if name in self._flags:
            self._flags[name].rollout_pct = max(0.0, min(100.0, pct))

    def kill(self, name: str):
        """Immediately disable a flag for all users."""
        if name in self._flags:
            self._flags[name].enabled = False
            self._flags[name].rollout_pct = 0.0

    def is_enabled(self, name: str, user_id: str = "") -> bool:
        flag = self._flags.get(name)
        if flag is None or not flag.enabled:
            return False
        if user_id in flag.excluded_users:
            return False
        if user_id in flag.override_users:
            return True
        if flag.rollout_pct >= 100.0:
            return True
        if flag.rollout_pct <= 0.0:
            return False
        # Deterministic bucket: same user always gets same result
        bucket = int(hashlib.md5(f"{name}:{user_id}".encode()).hexdigest(), 16) % 100
        return bucket < flag.rollout_pct

    def all_flags(self) -> Dict[str, dict]:
        return {
            name: {
                "enabled": f.enabled,
                "rollout_pct": f.rollout_pct,
                "metadata": f.metadata,
            }
            for name, f in self._flags.items()
        }
```

---

## Solution 2: Remote Feature Flag Client (LaunchDarkly / Unleash Compatible)

Polls a remote config endpoint for flag updates. Changes propagate within `poll_interval` seconds without redeployment.

```python
import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class RemoteFeatureFlagClient:
    """
    Polls a JSON endpoint for feature flag configuration.
    Falls back to last known state on network errors.

    Usage:
        client = RemoteFeatureFlagClient(
            endpoint="https://config.internal/flags",
            poll_interval=30,
        )
        await client.start()
        if client.is_enabled("new_tool_v3", user_id="u1"):
            ...
    """

    def __init__(self, endpoint: str, poll_interval: float = 30.0,
                 http_client=None):
        self._endpoint = endpoint
        self._interval = poll_interval
        self._http = http_client
        self._flags: Dict[str, Any] = {}
        self._last_updated: float = 0.0
        self._local_store = FeatureFlagStore()
        self._callbacks: list = []
        self._task: Optional[asyncio.Task] = None

    def on_change(self, callback: Callable[[str, bool], None]):
        self._callbacks.append(callback)
        return self

    async def start(self):
        await self._fetch()
        self._task = asyncio.create_task(self._poll_loop(), name="flag_poller")

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _poll_loop(self):
        while True:
            await asyncio.sleep(self._interval)
            await self._fetch()

    async def _fetch(self):
        if self._http is None:
            return
        try:
            resp = await self._http.get(self._endpoint)
            new_flags = await resp.json()
            changed = {k for k, v in new_flags.items()
                       if self._flags.get(k) != v}
            self._flags = new_flags
            self._last_updated = time.time()
            self._rebuild_local()
            for key in changed:
                for cb in self._callbacks:
                    cb(key, new_flags[key].get("enabled", False))
        except Exception as exc:
            logger.warning("Flag fetch failed: %s (using cached state)", exc)

    def _rebuild_local(self):
        for name, cfg in self._flags.items():
            self._local_store.add(FeatureFlag(
                name=name,
                enabled=cfg.get("enabled", False),
                rollout_pct=cfg.get("rollout_pct", 0.0),
                override_users=set(cfg.get("override_users", [])),
                excluded_users=set(cfg.get("excluded_users", [])),
                metadata=cfg.get("metadata", {}),
            ))

    def is_enabled(self, name: str, user_id: str = "") -> bool:
        return self._local_store.is_enabled(name, user_id)

    @property
    def staleness_seconds(self) -> float:
        return time.time() - self._last_updated if self._last_updated else float("inf")
```

---

## Solution 3: Prompt A/B Testing with Feature Flags

Route users to different prompt variants using flags. Track which variant each user received for analysis.

```python
import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class PromptVariant:
    variant_id: str
    prompt_template: str
    weight: float = 1.0     # relative weight for assignment


class PromptABTestManager:
    """
    Assigns users to prompt variants using weighted random selection
    (deterministic per user_id).

    Usage:
        mgr = PromptABTestManager()
        mgr.register_experiment("search_prompt_v3", [
            PromptVariant("control",   "Search for: {query}", weight=0.7),
            PromptVariant("treatment", "Find information about: {query}", weight=0.3),
        ])

        variant = mgr.get_variant("search_prompt_v3", user_id="u123")
        prompt = variant.prompt_template.format(query=user_query)
        mgr.record_impression("search_prompt_v3", "u123", variant.variant_id)
    """

    def __init__(self, flag_store: Optional[FeatureFlagStore] = None):
        self._experiments: Dict[str, List[PromptVariant]] = {}
        self._flags = flag_store or FeatureFlagStore()
        self._impressions: Dict[str, Dict[str, int]] = {}

    def register_experiment(self, name: str, variants: List[PromptVariant]):
        self._experiments[name] = variants
        self._impressions[name] = {v.variant_id: 0 for v in variants}

    def get_variant(self, experiment: str, user_id: str) -> PromptVariant:
        variants = self._experiments.get(experiment)
        if not variants:
            raise KeyError(f"Unknown experiment: {experiment}")
        # Deterministic assignment using hash
        import hashlib
        seed = int(hashlib.md5(f"{experiment}:{user_id}".encode()).hexdigest(), 16)
        total_weight = sum(v.weight for v in variants)
        r = (seed % 10000) / 10000.0 * total_weight
        cumulative = 0.0
        for v in variants:
            cumulative += v.weight
            if r < cumulative:
                return v
        return variants[-1]

    def record_impression(self, experiment: str, user_id: str, variant_id: str):
        if experiment in self._impressions:
            self._impressions[experiment][variant_id] = \
                self._impressions[experiment].get(variant_id, 0) + 1

    def stats(self) -> Dict[str, Dict[str, int]]:
        return dict(self._impressions)
```

---

## Solution 4: Agent Behaviour Toggle Decorator

A decorator that gates agent method execution behind a feature flag. The wrapped method calls the new implementation when the flag is on, falls back to the old one otherwise.

```python
import asyncio
import functools
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def feature_gated(flag_name: str,
                  store: Optional[FeatureFlagStore] = None,
                  fallback: Optional[Callable] = None):
    """
    Decorator: execute new implementation only when flag is enabled.
    Falls back to `fallback` function (or raises) when flag is off.

    Usage:
        @feature_gated("new_retrieval_v2", store=flag_store, fallback=old_retrieve)
        async def retrieve(self, query: str, user_id: str = "") -> list:
            return await new_vector_search(query)
    """
    def decorator(fn: Callable):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            uid = kwargs.get("user_id", "")
            flag_store = store
            enabled = flag_store.is_enabled(flag_name, uid) if flag_store else False
            if enabled:
                return await fn(*args, **kwargs)
            elif fallback:
                return await fallback(*args, **kwargs)
            else:
                logger.debug("Flag '%s' off for user='%s'; skipping", flag_name, uid)
                return None
        return wrapper
    return decorator


# Example usage:

class SearchAgent:
    def __init__(self, flag_store: FeatureFlagStore):
        self._flags = flag_store

    async def _old_keyword_search(self, query: str, user_id: str = "") -> list:
        return [{"title": f"keyword result for {query}"}]

    @feature_gated("semantic_search_v2",
                   store=None,  # set at runtime via SearchAgent.init
                   fallback=None)
    async def search(self, query: str, user_id: str = "") -> list:
        return [{"title": f"semantic result for {query}"}]
```

---

## Solution 5: Rollout State Machine

A state machine that manages the lifecycle of a rollout: draft → canary (1%) → limited (10%) → broad (50%) → full (100%) → stable. Each transition can be triggered manually or automatically based on metrics.

```python
import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class RolloutState(Enum):
    DRAFT = "draft"
    CANARY = "canary"         # 1%
    LIMITED = "limited"       # 10%
    BROAD = "broad"           # 50%
    FULL = "full"             # 100%
    KILLED = "killed"         # 0%, emergency off


STATE_ROLLOUT_PCT = {
    RolloutState.DRAFT:   0.0,
    RolloutState.CANARY:  1.0,
    RolloutState.LIMITED: 10.0,
    RolloutState.BROAD:   50.0,
    RolloutState.FULL:    100.0,
    RolloutState.KILLED:  0.0,
}

VALID_TRANSITIONS = {
    RolloutState.DRAFT:   {RolloutState.CANARY, RolloutState.KILLED},
    RolloutState.CANARY:  {RolloutState.LIMITED, RolloutState.KILLED},
    RolloutState.LIMITED: {RolloutState.BROAD, RolloutState.KILLED},
    RolloutState.BROAD:   {RolloutState.FULL, RolloutState.KILLED},
    RolloutState.FULL:    {RolloutState.KILLED},
    RolloutState.KILLED:  set(),
}


@dataclass
class RolloutRecord:
    state: RolloutState
    timestamp: float
    reason: str


class RolloutStateMachine:
    """
    Manages feature flag rollout through defined stages.

    Usage:
        rm = RolloutStateMachine("new_prompt_v3", flag_store)
        rm.advance(reason="canary looks good")   # DRAFT -> CANARY
        rm.advance(reason="1% error rate 0.1%")  # CANARY -> LIMITED
        rm.kill(reason="p99 latency spiked")     # -> KILLED
    """

    def __init__(self, flag_name: str, flag_store: FeatureFlagStore):
        self._name = flag_name
        self._store = flag_store
        self._state = RolloutState.DRAFT
        self._history: list = [RolloutRecord(self._state, time.time(), "created")]
        self._apply_pct()

    def _apply_pct(self):
        pct = STATE_ROLLOUT_PCT[self._state]
        self._store.set_rollout(self._name, pct)
        if self._state == RolloutState.KILLED:
            self._store.kill(self._name)
        elif pct > 0:
            flag = self._store._flags.get(self._name)
            if flag:
                flag.enabled = True

    def advance(self, reason: str = "") -> RolloutState:
        for next_state in VALID_TRANSITIONS[self._state]:
            if next_state != RolloutState.KILLED:
                self._state = next_state
                self._history.append(RolloutRecord(self._state, time.time(), reason))
                self._apply_pct()
                return self._state
        raise ValueError(f"No valid non-kill transition from {self._state}")

    def kill(self, reason: str = ""):
        self._state = RolloutState.KILLED
        self._history.append(RolloutRecord(self._state, time.time(), reason))
        self._apply_pct()

    @property
    def current_state(self) -> RolloutState:
        return self._state

    @property
    def current_pct(self) -> float:
        return STATE_ROLLOUT_PCT[self._state]

    def history(self) -> list:
        return [
            {"state": r.state.value, "timestamp": r.timestamp, "reason": r.reason}
            for r in self._history
        ]
```

---

## Solution 6: Unified Feature Management Agent Middleware

Combines flag store, A/B testing, and rollout state machine into a single facade used by agents to gate all new behaviour.

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class AgentRequest:
    user_id: str
    session_id: str
    payload: Any


class FeatureManagementMiddleware:
    """
    Central point for all feature decisions in an agent.

    Usage:
        fm = FeatureManagementMiddleware()
        fm.add_flag("new_tool_v2", rollout_pct=5.0)
        fm.add_experiment("prompt_ab", [
            PromptVariant("v1", "Old prompt: {q}", weight=0.5),
            PromptVariant("v2", "New prompt: {q}", weight=0.5),
        ])

        async def handle(request: AgentRequest):
            ctx = fm.evaluate(request.user_id)
            if ctx.flag("new_tool_v2"):
                result = await new_tool(request.payload)
            else:
                result = await old_tool(request.payload)
            prompt = ctx.variant("prompt_ab").prompt_template
            ...
    """

    def __init__(self):
        self._store = FeatureFlagStore()
        self._ab = PromptABTestManager(self._store)
        self._rollouts: Dict[str, RolloutStateMachine] = {}

    def add_flag(self, name: str, rollout_pct: float = 0.0, enabled: bool = True):
        self._store.add(FeatureFlag(name=name, enabled=enabled,
                                     rollout_pct=rollout_pct))

    def add_experiment(self, name: str, variants: List[PromptVariant]):
        self._ab.register_experiment(name, variants)

    def start_rollout(self, flag_name: str) -> RolloutStateMachine:
        rm = RolloutStateMachine(flag_name, self._store)
        self._rollouts[flag_name] = rm
        return rm

    def evaluate(self, user_id: str) -> "_EvaluationContext":
        return _EvaluationContext(user_id, self._store, self._ab)

    def dashboard(self) -> dict:
        return {
            "flags": self._store.all_flags(),
            "experiments": self._ab.stats(),
            "rollouts": {
                name: rm.history()[-1]
                for name, rm in self._rollouts.items()
            },
        }


class _EvaluationContext:
    def __init__(self, user_id: str,
                 store: FeatureFlagStore,
                 ab: PromptABTestManager):
        self._uid = user_id
        self._store = store
        self._ab = ab

    def flag(self, name: str) -> bool:
        return self._store.is_enabled(name, self._uid)

    def variant(self, experiment: str) -> PromptVariant:
        return self._ab.get_variant(experiment, self._uid)
```

---

## Comparison

| Approach | Rollback Speed | Granularity | Requires Infra |
|---|---|---|---|
| **In-Memory Flag Store** | Instant (code path) | Per-user | No |
| **Remote Flag Client** | < poll_interval (30 s) | Per-user | Config endpoint |
| **Prompt A/B Testing** | Instant | Per-user, weighted | No |
| **Feature-Gated Decorator** | Instant | Per-method | No |
| **Rollout State Machine** | Instant kill switch | Per-stage % | No |
| **Feature Management Middleware** | Instant | Per-user, per-method | No |

**Key insight**: the kill switch matters more than the gradual rollout. Ensure every flag can be turned to 0% in under 60 seconds without a code deploy. The remote client achieves this with a simple HTTP config endpoint and no external dependencies.
