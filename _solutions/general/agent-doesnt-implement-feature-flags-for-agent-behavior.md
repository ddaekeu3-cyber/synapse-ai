---
layout: solution
title: "Agent Doesn't Implement Feature Flags for Agent Behavior"
category: general
description: "Gate agent capabilities, prompt variants, model choices, and tool access behind feature flags to enable safe rollouts, A/B testing, and instant kill switches."
tags: [general, feature-flags, rollout, ab-testing, kill-switch, configuration]
---

# Agent Doesn't Implement Feature Flags for Agent Behavior

Deploying a new agent capability to all users at once is risky — a bad prompt, a misconfigured tool, or an unexpected edge case can affect every session simultaneously. Without feature flags, rollbacks require redeployment. With feature flags, new behaviors can be gated by user ID, percentage rollout, or environment, and instantly disabled without touching code.

## Option 1: Simple Boolean Flag Registry

```python
import anthropic
import os

client = anthropic.Anthropic()

# Feature flags — in production, load from env vars, config service, or LaunchDarkly
FLAGS: dict[str, bool] = {
    "use_extended_thinking":    False,
    "enable_tool_use":          True,
    "use_new_system_prompt":    False,
    "enable_response_caching":  True,
    "verbose_citations":        False,
}


def flag(name: str) -> bool:
    """Read a feature flag, defaulting to False if unknown."""
    return FLAGS.get(name, False)


SYSTEM_PROMPT_V1 = "You are a helpful assistant."
SYSTEM_PROMPT_V2 = "You are a helpful assistant. Always structure your response with a Summary, Details, and Next Steps section."

TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


def run_agent(user_message: str) -> str:
    system = SYSTEM_PROMPT_V2 if flag("use_new_system_prompt") else SYSTEM_PROMPT_V1
    tools = TOOLS if flag("enable_tool_use") else []

    kwargs: dict = dict(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    if tools:
        kwargs["tools"] = tools

    response = client.messages.create(**kwargs)

    active_flags = [k for k, v in FLAGS.items() if v]
    print(f"Active flags: {active_flags}")
    return next((b.text for b in response.content if hasattr(b, "text")), "")


result = run_agent("Explain Python decorators briefly.")
print(f"Answer: {result[:300]}")

# Expected Token Savings: N/A (control pattern); kill switches prevent costly runaway behavior from reaching all users
# Environment: Python 3.11+; load FLAGS from os.environ or a config service at startup; never hardcode in production
```

## Option 2: Percentage-Based Rollout with User Hashing

```python
import anthropic
import hashlib

client = anthropic.Anthropic()

# Flag definitions: name -> rollout percentage (0-100)
ROLLOUT_FLAGS: dict[str, int] = {
    "new_summarization_model":   20,   # 20% of users
    "extended_context_window":   50,   # 50% of users
    "chain_of_thought_prompting": 10,  # 10% of users — early experiment
    "verbose_tool_output":        0,   # Disabled (0%)
    "fast_mode":                100,   # Fully rolled out (100%)
}


def is_enabled(flag_name: str, user_id: str) -> bool:
    """Deterministically assign user to flag bucket based on user_id hash."""
    pct = ROLLOUT_FLAGS.get(flag_name, 0)
    if pct == 0:
        return False
    if pct == 100:
        return True
    # Stable hash: same user always gets same assignment
    h = int(hashlib.md5(f"{flag_name}:{user_id}".encode()).hexdigest(), 16)
    bucket = h % 100
    return bucket < pct


MODELS = {
    "default": "claude-haiku-4-5-20251001",
    "new":     "claude-haiku-4-5-20251001",  # swap to sonnet in production
}

PROMPTS = {
    "default": "You are a helpful assistant. Answer concisely.",
    "cot":     "You are a helpful assistant. Think step by step before answering.",
}


def run_agent_for_user(user_id: str, question: str) -> tuple[str, dict]:
    # Evaluate flags for this user
    use_new_model = is_enabled("new_summarization_model", user_id)
    use_cot = is_enabled("chain_of_thought_prompting", user_id)

    model = MODELS["new"] if use_new_model else MODELS["default"]
    system = PROMPTS["cot"] if use_cot else PROMPTS["default"]

    active = {
        "new_summarization_model": use_new_model,
        "chain_of_thought_prompting": use_cot,
        "fast_mode": is_enabled("fast_mode", user_id),
    }

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text, active


# Test multiple users — some get new behavior, some get default
users = ["user-001", "user-042", "user-123", "user-999", "user-777"]
question = "What is dependency injection?"

print(f"Rollout: new_model={ROLLOUT_FLAGS['new_summarization_model']}%, cot={ROLLOUT_FLAGS['chain_of_thought_prompting']}%\n")
for uid in users:
    answer, active = run_agent_for_user(uid, question)
    print(f"[{uid}] flags={active}")
    print(f"  Answer: {answer[:80]}\n")

# Expected Token Savings: N/A; rollout flags limit blast radius — a bad prompt variant affects 10% not 100%
# Environment: Python 3.11+; user_id hash is stable — same user always lands in same bucket across restarts
```

## Option 3: SQLite-Backed Flag Store with Audit Log

```python
import anthropic
import sqlite3
import json
import time
from typing import Any

client = anthropic.Anthropic()
DB_PATH = ":memory:"


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS feature_flags (
            name TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            rollout_pct INTEGER NOT NULL DEFAULT 0,
            config_json TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL,
            updated_by TEXT NOT NULL DEFAULT 'system'
        );
        CREATE TABLE IF NOT EXISTS flag_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            action TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            changed_by TEXT,
            changed_at REAL NOT NULL
        );
    """)
    conn.commit()


def set_flag(conn: sqlite3.Connection, name: str, enabled: bool,
             rollout_pct: int = 100, config: dict | None = None,
             changed_by: str = "admin") -> None:
    old = conn.execute("SELECT enabled, rollout_pct FROM feature_flags WHERE name=?", (name,)).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO feature_flags VALUES (?,?,?,?,?,?)",
        (name, int(enabled), rollout_pct, json.dumps(config or {}), time.time(), changed_by)
    )
    conn.execute(
        "INSERT INTO flag_audit VALUES (NULL,?,?,?,?,?,?)",
        (name, "update", json.dumps(old), json.dumps((int(enabled), rollout_pct)), changed_by, time.time())
    )
    conn.commit()


def get_flag(conn: sqlite3.Connection, name: str) -> tuple[bool, int, dict]:
    row = conn.execute(
        "SELECT enabled, rollout_pct, config_json FROM feature_flags WHERE name=?", (name,)
    ).fetchone()
    if not row:
        return False, 0, {}
    return bool(row[0]), row[1], json.loads(row[2])


def kill_switch(conn: sqlite3.Connection, name: str, reason: str = "") -> None:
    """Emergency disable a flag with audit trail."""
    set_flag(conn, name, enabled=False, rollout_pct=0, changed_by=f"kill-switch: {reason}")
    print(f"KILL SWITCH activated: {name} — {reason}")


def run_agent(conn: sqlite3.Connection, user_message: str) -> str:
    cot_enabled, _, cot_cfg = get_flag(conn, "chain_of_thought")
    new_model_enabled, pct, _ = get_flag(conn, "use_sonnet_model")

    system = "Think step by step, then answer." if cot_enabled else "Answer directly."
    model = "claude-haiku-4-5-20251001"  # Would be sonnet if new_model_enabled

    print(f"[flags] cot={cot_enabled}, new_model={new_model_enabled}({pct}%)")

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


conn = sqlite3.connect(DB_PATH)
init_db(conn)

# Setup flags
set_flag(conn, "chain_of_thought", enabled=True, rollout_pct=100, changed_by="engineer-1")
set_flag(conn, "use_sonnet_model", enabled=True, rollout_pct=25, changed_by="engineer-1")

r1 = run_agent(conn, "What are the pros and cons of microservices?")
print(f"Answer: {r1[:200]}\n")

# Simulate incident — emergency kill switch
kill_switch(conn, "chain_of_thought", reason="generating overly long responses")

r2 = run_agent(conn, "What are the pros and cons of microservices?")
print(f"Answer after kill switch: {r2[:200]}")

# Show audit trail
print("\nAudit log:")
for row in conn.execute("SELECT name, action, old_value, new_value, changed_by FROM flag_audit"):
    print(f"  {row}")

# Expected Token Savings: N/A; kill switch recovers from runaway token spend without redeployment
# Environment: Python 3.11+; replace :memory: with a shared DB; poll flags every 30s to pick up live changes
```

## Option 4: Environment-Scoped Flags with Override Hierarchy

```python
import anthropic
import os
from dataclasses import dataclass, field
from typing import Any

client = anthropic.Anthropic()


@dataclass
class FlagValue:
    default: Any
    dev: Any | None = None
    staging: Any | None = None
    prod: Any | None = None

    def resolve(self, env: str) -> Any:
        """Return value for this environment, falling back to default."""
        return getattr(self, env, None) or self.default


FLAG_DEFINITIONS: dict[str, FlagValue] = {
    "max_tool_calls":       FlagValue(default=3, dev=10, staging=5, prod=3),
    "use_streaming":        FlagValue(default=False, dev=True, staging=True, prod=True),
    "enable_debug_logging": FlagValue(default=False, dev=True, staging=False, prod=False),
    "model_override":       FlagValue(default=None, dev="claude-haiku-4-5-20251001"),
    "rate_limit_rps":       FlagValue(default=10, dev=100, staging=30, prod=10),
}

# Runtime overrides (e.g., from env vars) take precedence over all
ENV_OVERRIDES: dict[str, Any] = {
    k.replace("FLAG_", "").lower(): v
    for k, v in os.environ.items()
    if k.startswith("FLAG_")
}


def get(flag_name: str, env: str = "prod") -> Any:
    """Resolve flag with override hierarchy: env_var > environment > default."""
    if flag_name in ENV_OVERRIDES:
        return ENV_OVERRIDES[flag_name]
    definition = FLAG_DEFINITIONS.get(flag_name)
    if definition is None:
        return None
    return definition.resolve(env)


def run_agent(user_message: str, env: str = "prod") -> str:
    model = get("model_override", env) or "claude-haiku-4-5-20251001"
    max_tools = get("max_tool_calls", env)
    debug = get("enable_debug_logging", env)

    if debug:
        print(f"[debug] env={env} model={model} max_tool_calls={max_tools}")

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=f"You are a helpful assistant. (env={env}, max_tool_calls={max_tools})",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Same code, different behavior per environment
for env in ["dev", "staging", "prod"]:
    print(f"\n=== {env.upper()} ===")
    print(f"  max_tool_calls={get('max_tool_calls', env)}, "
          f"streaming={get('use_streaming', env)}, "
          f"debug={get('enable_debug_logging', env)}, "
          f"model_override={get('model_override', env)}")
    result = run_agent("What is a feature flag?", env=env)
    print(f"  Answer: {result[:100]}")

# Expected Token Savings: N/A; dev environment gets relaxed limits for testing without touching prod flags
# Environment: Python 3.11+; set FLAG_<name>=value env vars for emergency overrides without code changes
```

## Option 5: User Segment Targeting with Rule Engine

```python
import anthropic
from dataclasses import dataclass
from typing import Any, Callable

client = anthropic.Anthropic()


@dataclass
class User:
    id: str
    plan: str           # "free" | "pro" | "enterprise"
    country: str
    beta_tester: bool = False
    account_age_days: int = 0


@dataclass
class FlagRule:
    condition: Callable[[User], bool]
    value: Any
    description: str


@dataclass
class FeatureFlag:
    name: str
    default: Any
    rules: list[FlagRule]  # Evaluated in order — first match wins

    def evaluate(self, user: User) -> Any:
        for rule in self.rules:
            if rule.condition(user):
                return rule.value
        return self.default


FLAGS: dict[str, FeatureFlag] = {
    "max_context_tokens": FeatureFlag(
        name="max_context_tokens",
        default=4096,
        rules=[
            FlagRule(lambda u: u.plan == "enterprise", 100000, "Enterprise gets 100k context"),
            FlagRule(lambda u: u.plan == "pro",         32000, "Pro gets 32k context"),
            FlagRule(lambda u: u.beta_tester,           16000, "Beta testers get 16k"),
        ],
    ),
    "enable_advanced_tools": FeatureFlag(
        name="enable_advanced_tools",
        default=False,
        rules=[
            FlagRule(lambda u: u.plan in ("pro", "enterprise"), True, "Paid plans get advanced tools"),
            FlagRule(lambda u: u.beta_tester, True, "Beta testers get advanced tools"),
        ],
    ),
    "model_tier": FeatureFlag(
        name="model_tier",
        default="haiku",
        rules=[
            FlagRule(lambda u: u.plan == "enterprise", "opus",   "Enterprise gets Opus"),
            FlagRule(lambda u: u.plan == "pro",        "sonnet", "Pro gets Sonnet"),
            FlagRule(lambda u: u.beta_tester,          "sonnet", "Beta testers get Sonnet"),
        ],
    ),
}

MODEL_MAP = {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-haiku-4-5-20251001", "opus": "claude-haiku-4-5-20251001"}


def run_agent_for_user(user: User, question: str) -> str:
    tier = FLAGS["model_tier"].evaluate(user)
    model = MODEL_MAP[tier]
    max_ctx = FLAGS["max_context_tokens"].evaluate(user)
    advanced_tools = FLAGS["enable_advanced_tools"].evaluate(user)

    print(f"[{user.id}] plan={user.plan} -> model_tier={tier} max_ctx={max_ctx} advanced_tools={advanced_tools}")

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=f"You are a helpful assistant. (tier={tier}, tools={'enabled' if advanced_tools else 'basic'})",
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


users = [
    User("u1", "free",       "US",  beta_tester=False, account_age_days=10),
    User("u2", "pro",        "UK",  beta_tester=False, account_age_days=200),
    User("u3", "enterprise", "DE",  beta_tester=False, account_age_days=500),
    User("u4", "free",       "JP",  beta_tester=True,  account_age_days=5),
]

for user in users:
    answer = run_agent_for_user(user, "What AI tools do I have access to?")
    print(f"  Answer: {answer[:100]}\n")

# Expected Token Savings: N/A; segment targeting limits expensive models to paying customers only
# Environment: Python 3.11+; rules are evaluated in order — put most specific rules first
```

## Option 6: Real-Time Flag Polling with In-Process Cache

```python
import asyncio
import anthropic
import time
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()


@dataclass
class FlagCache:
    """In-process cache for feature flags with TTL-based refresh."""
    flags: dict[str, bool] = field(default_factory=dict)
    last_refresh: float = 0.0
    ttl: float = 30.0  # Refresh every 30 seconds

    def is_stale(self) -> bool:
        return time.monotonic() - self.last_refresh > self.ttl

    def update(self, flags: dict[str, bool]) -> None:
        self.flags = flags
        self.last_refresh = time.monotonic()

    def get(self, name: str, default: bool = False) -> bool:
        return self.flags.get(name, default)


# Simulate a remote flag store (replace with HTTP call to LaunchDarkly, Unleash, etc.)
REMOTE_FLAGS_DB: dict[str, bool] = {
    "enable_streaming":      True,
    "use_new_prompt":        False,
    "enable_cost_tracking":  True,
    "experimental_tools":    False,
}


async def fetch_flags() -> dict[str, bool]:
    """Fetch flags from remote config service (simulated)."""
    await asyncio.sleep(0.05)  # Simulate network latency
    return dict(REMOTE_FLAGS_DB)


_flag_cache = FlagCache()
_refresh_lock = asyncio.Lock()


async def get_flag(name: str) -> bool:
    """Get flag value, refreshing cache if stale."""
    global _flag_cache
    if _flag_cache.is_stale():
        async with _refresh_lock:
            if _flag_cache.is_stale():  # Double-check after acquiring lock
                flags = await fetch_flags()
                _flag_cache.update(flags)
                print(f"[flags] Refreshed from remote: {flags}")
    return _flag_cache.get(name)


async def run_agent(user_message: str) -> str:
    use_streaming = await get_flag("enable_streaming")
    use_new_prompt = await get_flag("use_new_prompt")
    cost_tracking = await get_flag("enable_cost_tracking")

    system = (
        "You are a next-generation assistant (v2). Structure all answers clearly."
        if use_new_prompt
        else "You are a helpful assistant."
    )

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    if cost_tracking:
        print(f"[cost] input={response.usage.input_tokens} output={response.usage.output_tokens}")

    return next((b.text for b in response.content if hasattr(b, "text")), "")


async def main() -> None:
    # Simulate 5 requests — cache is warm after first
    for i in range(5):
        print(f"\n=== Request {i+1} ===")
        result = await run_agent("What is caching?")
        print(f"Answer: {result[:100]}")
        await asyncio.sleep(0.1)

    # Simulate flag change on remote
    REMOTE_FLAGS_DB["use_new_prompt"] = True
    _flag_cache.last_refresh = 0.0  # Force refresh on next request

    print("\n=== After flag change (use_new_prompt=True) ===")
    result = await run_agent("What is caching?")
    print(f"Answer: {result[:100]}")


asyncio.run(main())

# Expected Token Savings: N/A; TTL cache prevents flag service from becoming a latency bottleneck (30s TTL = 1 lookup per 30s)
# Environment: Python 3.11+; replace fetch_flags() with HTTP GET to LaunchDarkly/Unleash/Flipt; use asyncio.Lock for safe refresh
```

## Comparison

| Option | Storage | Rollout Control | Kill Switch | Audit | Real-Time | Best For |
|--------|---------|----------------|-------------|-------|-----------|----------|
| 1. Boolean Registry | In-process dict | No | Yes (set False) | No | No | Simple on/off capabilities |
| 2. Percentage Rollout | In-process dict | Yes (% bucket) | Yes | No | No | Gradual feature rollout |
| 3. SQLite + Audit | SQLite | Yes | Yes + logged | Yes | No | Production with compliance needs |
| 4. Environment Scoped | Env vars + code | Per env | Via env var | No | No | Multi-environment deployments |
| 5. Segment Targeting | In-process rules | Per user segment | Yes | No | No | Plan-based or role-based gating |
| 6. Remote Poll + Cache | Remote store + TTL | Yes (remote) | Yes (remote) | Via remote | Yes | Distributed agents, live updates |
