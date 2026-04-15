---
layout: solution
title: "Agent Ignores Environment-Specific Feature Flags — Dev Features Hit Production"
category: config
description: "Agent has a feature flag system but reads flags from the wrong environment. A flag enabled for testing in dev is also enabled in prod. Or a new experimental agent behavior rolls out to all users because the flag check is bypassed."
tags: [config, feature-flags, environment, rollout, flags, dev, prod, gating]
---

# Agent Ignores Environment-Specific Feature Flags — Dev Features Hit Production

## Symptom
- Experimental agent behavior rolled out to all production users because flag check was skipped
- `USE_NEW_SEARCH_ALGORITHM=true` in dev `.env` file also applies in production
- Feature flag service returns wrong flags because it's using the dev project key in prod
- A/B test that should only affect 10% of users affects 100% because flag is hardcoded
- Agent uses a new unvalidated tool in production because the flag defaults to `True`

## Root Cause
Feature flag checks are bypassed or misconfigured in several ways: hardcoded fallback defaults that are too permissive, flag service initialized with wrong environment key, or `.env` file values overriding the intended production flags. Without explicit environment enforcement in the flag system, flags bleed across environments.

## Fix

### Option 1: Environment-aware feature flag client

```python
import os
from enum import Enum

class Environment(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"

class FeatureFlagClient:
    """
    Feature flag client that is explicitly environment-aware.
    Flags are defined per-environment — no cross-contamination possible.
    """

    # Define flags per environment — prod is always conservative
    FLAGS: dict[str, dict[str, bool]] = {
        "use_new_search_algorithm": {
            Environment.DEV: True,       # Enabled for dev testing
            Environment.STAGING: True,   # Enabled for staging validation
            Environment.PROD: False,     # Not yet enabled in prod
        },
        "experimental_tool_use": {
            Environment.DEV: True,
            Environment.STAGING: False,
            Environment.PROD: False,
        },
        "verbose_logging": {
            Environment.DEV: True,
            Environment.STAGING: True,
            Environment.PROD: False,     # Never verbose in prod
        },
    }

    def __init__(self, env: str):
        try:
            self.env = Environment(env)
        except ValueError:
            raise ValueError(
                f"Invalid environment: '{env}'. Must be one of: "
                f"{[e.value for e in Environment]}"
            )

    def is_enabled(self, flag_name: str, default: bool = False) -> bool:
        """
        Check if a flag is enabled for the current environment.
        default only applies if flag is not defined — use False to be safe.
        """
        if flag_name not in self.FLAGS:
            print(f"Unknown feature flag '{flag_name}' — using default={default}")
            return default

        return self.FLAGS[flag_name].get(self.env, False)  # Default False for unknown envs

# Initialize once at startup:
ENV = os.environ.get("AGENT_ENV", "dev")
flags = FeatureFlagClient(env=ENV)

# Usage — cannot use prod-disabled features in prod:
if flags.is_enabled("use_new_search_algorithm"):
    results = new_search(query)
else:
    results = legacy_search(query)
```

### Option 2: Remote feature flag service with environment isolation

```python
import os
import httpx
from functools import lru_cache

class RemoteFlagClient:
    """
    Use a remote feature flag service (LaunchDarkly, Unleash, etc.)
    with environment-specific SDK keys — physical separation of environments.
    """

    # Each environment has a DIFFERENT SDK key — wrong key = wrong flags
    SDK_KEYS = {
        "dev": os.environ.get("LAUNCHDARKLY_SDK_KEY_DEV"),
        "staging": os.environ.get("LAUNCHDARKLY_SDK_KEY_STAGING"),
        "prod": os.environ.get("LAUNCHDARKLY_SDK_KEY_PROD"),
    }

    def __init__(self, env: str, base_url: str = "https://app.launchdarkly.com"):
        self.env = env
        self.sdk_key = self.SDK_KEYS.get(env)
        if not self.sdk_key:
            raise RuntimeError(
                f"No SDK key for environment '{env}'. "
                f"Set LAUNCHDARKLY_SDK_KEY_{env.upper()} environment variable."
            )
        self.base_url = base_url
        self._cache: dict[str, bool] = {}

    async def is_enabled(self, flag_key: str, agent_id: str = "default") -> bool:
        """
        Check flag from remote service — flag state is managed externally.
        Using wrong SDK key returns flags for the wrong environment.
        """
        cache_key = f"{flag_key}:{agent_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/sdk/latest-all",
                headers={"Authorization": self.sdk_key},
                timeout=5
            )
            flags = resp.json()
            flag = flags.get("flags", {}).get(flag_key, {})
            enabled = flag.get("on", False)

        self._cache[cache_key] = enabled
        return enabled
```

### Option 3: Flag audit — verify production flags at startup

```python
def audit_production_flags(flags: FeatureFlagClient) -> list[str]:
    """
    Check for flags that should never be enabled in production.
    Run at agent startup in prod to catch misconfigurations.
    """
    if flags.env != Environment.PROD:
        return []  # Only audit prod

    SHOULD_BE_DISABLED_IN_PROD = [
        "debug_mode",
        "verbose_logging",
        "experimental_tool_use",
        "bypass_rate_limits",
        "skip_validation",
        "mock_external_apis",
    ]

    violations = []
    for flag in SHOULD_BE_DISABLED_IN_PROD:
        if flags.is_enabled(flag):
            violations.append(
                f"DANGER: '{flag}' is enabled in PRODUCTION. "
                f"This flag should only be enabled in dev/staging."
            )

    return violations

# At startup in prod:
violations = audit_production_flags(flags)
if violations:
    for v in violations:
        print(f"FLAG AUDIT VIOLATION: {v}")
    raise RuntimeError(
        "Production flag audit failed. Fix flags before starting agent in prod."
    )
```

### Option 4: Gradual rollout with percentage-based flags

```python
import hashlib

class RolloutFlagClient:
    """
    Percentage-based rollout flags — only enable for X% of users/sessions.
    Deterministic: same user_id always gets same result.
    """

    # Flag definitions: (prod_percentage, dev_percentage, staging_percentage)
    ROLLOUTS = {
        "new_summarizer": (0, 100, 50),       # 0% prod, 100% dev, 50% staging
        "faster_retrieval": (10, 100, 100),   # 10% prod rollout
        "gpt4_responses": (5, 100, 20),       # 5% prod, controlled rollout
    }

    def __init__(self, env: str):
        self.env = env
        self._env_index = {"dev": 1, "staging": 2, "prod": 0}.get(env, 0)

    def _bucket(self, flag_name: str, user_id: str) -> int:
        """Deterministic bucket 0-99 based on flag+user"""
        key = f"{flag_name}:{user_id}"
        hash_val = int(hashlib.sha256(key.encode()).hexdigest(), 16)
        return hash_val % 100

    def is_enabled(self, flag_name: str, user_id: str = "default") -> bool:
        if flag_name not in self.ROLLOUTS:
            return False

        rollout_pct = self.ROLLOUTS[flag_name][self._env_index]

        if rollout_pct == 0:
            return False
        if rollout_pct == 100:
            return True

        # Percentage rollout — same user always gets same result
        return self._bucket(flag_name, user_id) < rollout_pct

rollout = RolloutFlagClient(env="prod")

# In prod with 10% rollout:
# 10% of user IDs → True
# 90% of user IDs → False
# Same user_id ALWAYS gets the same result (deterministic)
if rollout.is_enabled("faster_retrieval", user_id=session.user_id):
    use_faster_retrieval()
```

### Option 5: Flag change detection and alerts

```python
import json
from pathlib import Path
from datetime import datetime

class FlagChangeMonitor:
    """
    Detect when flags change and alert before they affect production.
    """

    def __init__(self, snapshot_path: str = "flag_snapshots.json"):
        self.path = Path(snapshot_path)
        self._history: list[dict] = []
        if self.path.exists():
            self._history = json.loads(self.path.read_text())

    def snapshot(self, flags: FeatureFlagClient, flag_names: list[str]) -> dict:
        """Take a snapshot of current flag state"""
        state = {
            "env": flags.env.value,
            "timestamp": datetime.utcnow().isoformat(),
            "flags": {name: flags.is_enabled(name) for name in flag_names}
        }
        return state

    def compare_and_alert(self, current: dict, prev: dict) -> list[str]:
        """Find flags that changed between snapshots"""
        changes = []
        for flag, value in current["flags"].items():
            prev_value = prev["flags"].get(flag)
            if prev_value is not None and prev_value != value:
                changes.append(
                    f"Flag '{flag}' changed: {prev_value} → {value} "
                    f"(env: {current['env']})"
                )
        return changes

    def record(self, flags: FeatureFlagClient, flag_names: list[str]) -> list[str]:
        current = self.snapshot(flags, flag_names)
        changes = []

        if self._history:
            prev = next(
                (s for s in reversed(self._history) if s["env"] == current["env"]),
                None
            )
            if prev:
                changes = self.compare_and_alert(current, prev)

        self._history.append(current)
        self.path.write_text(json.dumps(self._history[-50:], indent=2))  # Keep last 50
        return changes

monitor = FlagChangeMonitor()
changes = monitor.record(flags, ["new_search_algorithm", "experimental_tool_use"])
if changes:
    for change in changes:
        print(f"FLAG CHANGE DETECTED: {change}")
    # Send to alerting system, Slack, PagerDuty, etc.
```

### Option 6: Test that flags behave correctly per environment

```python
import pytest

@pytest.fixture
def dev_flags():
    return FeatureFlagClient(env="dev")

@pytest.fixture
def prod_flags():
    return FeatureFlagClient(env="prod")

class TestFeatureFlags:
    """Ensure flags are correctly set per environment"""

    def test_experimental_flags_disabled_in_prod(self, prod_flags):
        """Experimental features must never be enabled in prod by default"""
        experimental = [
            "experimental_tool_use",
            "debug_mode",
            "bypass_rate_limits",
        ]
        for flag in experimental:
            assert not prod_flags.is_enabled(flag), (
                f"'{flag}' is enabled in PROD — this is dangerous!"
            )

    def test_experimental_flags_enabled_in_dev(self, dev_flags):
        """Experimental features should be available for dev testing"""
        experimental = ["experimental_tool_use", "debug_mode"]
        for flag in experimental:
            assert dev_flags.is_enabled(flag), (
                f"'{flag}' should be enabled in dev for testing"
            )

    def test_invalid_environment_raises(self):
        with pytest.raises(ValueError):
            FeatureFlagClient(env="invalid_env")

    def test_unknown_flag_returns_safe_default(self, prod_flags):
        assert not prod_flags.is_enabled("nonexistent_flag", default=False)
```

## Flag Configuration Risks

| Risk | Scenario | Prevention |
|---|---|---|
| Default=True | Unknown flag defaults to enabled in prod | Always default=False |
| Shared flag key | Same LaunchDarkly key for dev+prod | Separate SDK keys per env |
| `.env` override | Dev env var overrides prod flag service | Explicit env validation |
| Missing flag → defaults | Flag removed from service → default applied | Audit missing flags |
| Hardcoded True | Developer hardcodes `True` during testing | Code review, flag audit |

## Expected Token Savings
Experimental agent behavior in prod causes incident + rollback: ~200,000 tokens
Explicit per-environment flags + prod audit: 0 incidents

## Environment
- Any agent with multiple deployment environments and feature flags for controlled rollout
- Source: direct experience; flag misconfiguration causes the most invisible production regressions in agent systems
