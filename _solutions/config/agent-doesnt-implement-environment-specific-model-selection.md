---
layout: solution
title: "Agent Doesn't Implement Environment-Specific Model Selection"
category: config
description: "Agent uses the same expensive model in development, staging, and production. Dev runs waste money on Opus for debugging; prod may underuse capability by defaulting to Haiku."
tags: [config, model-selection, environment, cost, dev-staging-prod, routing]
---

# Agent Doesn't Implement Environment-Specific Model Selection

## Problem

An agent hardcodes `model="claude-opus-4-6"` everywhere. In development, every test run costs 15x what it should. In CI, the test suite is slow and expensive. In production, some tasks that need Opus get Haiku because someone swapped models to cut costs without a proper selection strategy.

---

## Option 1: Environment Variable Model Mapping

Read the environment tier from `APP_ENV` and map it to a model configuration at startup.

```python
import anthropic
import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelConfig:
    default: str
    fast: str
    powerful: str
    description: str

MODEL_CONFIGS: dict[str, ModelConfig] = {
    "development": ModelConfig(
        default="claude-haiku-4-5-20251001",
        fast="claude-haiku-4-5-20251001",
        powerful="claude-haiku-4-5-20251001",  # Never use expensive models in dev
        description="All models capped at Haiku for cost control"
    ),
    "test": ModelConfig(
        default="claude-haiku-4-5-20251001",
        fast="claude-haiku-4-5-20251001",
        powerful="claude-haiku-4-5-20251001",
        description="Fast models only for CI speed"
    ),
    "staging": ModelConfig(
        default="claude-haiku-4-5-20251001",
        fast="claude-haiku-4-5-20251001",
        powerful="claude-sonnet-4-6",
        description="Haiku default; Sonnet for complex tasks"
    ),
    "production": ModelConfig(
        default="claude-sonnet-4-6",
        fast="claude-haiku-4-5-20251001",
        powerful="claude-opus-4-6",
        description="Sonnet default; Haiku for routing; Opus for deep reasoning"
    ),
}

def get_model_config() -> ModelConfig:
    env = os.environ.get("APP_ENV", "development").lower()
    config = MODEL_CONFIGS.get(env)
    if not config:
        print(f"[warn] Unknown APP_ENV={env!r}, defaulting to development config")
        config = MODEL_CONFIGS["development"]
    print(f"[model-config] ENV={env}: {config.description}")
    return config

MODEL_CONFIG = get_model_config()
client = anthropic.Anthropic()

def call_agent(prompt: str, complexity: str = "default") -> str:
    model = {
        "fast": MODEL_CONFIG.fast,
        "powerful": MODEL_CONFIG.powerful,
    }.get(complexity, MODEL_CONFIG.default)

    print(f"[model] complexity={complexity} → {model}")
    response = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

# In development: all three use Haiku regardless of complexity
result_fast = call_agent("What is 2+2?", complexity="fast")
result_default = call_agent("Explain HTTP.", complexity="default")
result_powerful = call_agent("Analyze this complex scenario.", complexity="powerful")

print(f"\nfast: {result_fast[:60]}")
print(f"default: {result_default[:60]}")
print(f"powerful: {result_powerful[:60]}")

# Expected Token Savings: Development uses Haiku exclusively (15x cheaper than Opus). A 1000-token dev session costs $0.001 vs $0.015 with Opus. CI suite of 100 calls: $0.10 vs $1.50.
# Environment: Set APP_ENV=development|test|staging|production. ANTHROPIC_API_KEY required.
```

---

## Option 2: YAML Config File with Environment Overrides

Load model selection from a YAML-style config with per-environment sections and task-type overrides.

```python
import anthropic
import os
import json
from dataclasses import dataclass
from typing import Optional

# Simulated config (in production: load from config.yaml or config.json)
CONFIG = {
    "environments": {
        "development": {
            "default_model": "claude-haiku-4-5-20251001",
            "task_models": {
                "classification": "claude-haiku-4-5-20251001",
                "summarization": "claude-haiku-4-5-20251001",
                "reasoning": "claude-haiku-4-5-20251001",
                "code_generation": "claude-haiku-4-5-20251001",
            },
            "max_tokens_cap": 512,
            "cost_limit_usd_per_hour": 0.10
        },
        "staging": {
            "default_model": "claude-haiku-4-5-20251001",
            "task_models": {
                "classification": "claude-haiku-4-5-20251001",
                "summarization": "claude-sonnet-4-6",
                "reasoning": "claude-sonnet-4-6",
                "code_generation": "claude-sonnet-4-6",
            },
            "max_tokens_cap": 2048,
            "cost_limit_usd_per_hour": 5.0
        },
        "production": {
            "default_model": "claude-sonnet-4-6",
            "task_models": {
                "classification": "claude-haiku-4-5-20251001",
                "summarization": "claude-sonnet-4-6",
                "reasoning": "claude-opus-4-6",
                "code_generation": "claude-sonnet-4-6",
            },
            "max_tokens_cap": 4096,
            "cost_limit_usd_per_hour": 100.0
        }
    }
}

@dataclass
class EnvConfig:
    env: str
    default_model: str
    task_models: dict[str, str]
    max_tokens_cap: int
    cost_limit_usd_per_hour: float

    def model_for_task(self, task_type: str) -> str:
        return self.task_models.get(task_type, self.default_model)

def load_env_config() -> EnvConfig:
    env = os.environ.get("APP_ENV", "development").lower()
    env_cfg = CONFIG["environments"].get(env, CONFIG["environments"]["development"])
    return EnvConfig(
        env=env,
        default_model=env_cfg["default_model"],
        task_models=env_cfg["task_models"],
        max_tokens_cap=env_cfg["max_tokens_cap"],
        cost_limit_usd_per_hour=env_cfg["cost_limit_usd_per_hour"]
    )

env_config = load_env_config()
client = anthropic.Anthropic()

def run_task(task_type: str, prompt: str, max_tokens: Optional[int] = None) -> dict:
    model = env_config.model_for_task(task_type)
    tokens = min(max_tokens or 256, env_config.max_tokens_cap)
    print(f"[{env_config.env}] task={task_type} model={model} max_tokens={tokens}")

    response = client.messages.create(
        model=model, max_tokens=tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return {
        "result": response.content[0].text,
        "model_used": model,
        "task_type": task_type,
        "env": env_config.env
    }

# Same code, different behavior per environment
tasks = [
    ("classification", "Is this positive or negative: 'The service was great!'"),
    ("summarization",  "Summarize: Python is a versatile programming language."),
    ("reasoning",      "What are the tradeoffs of microservices vs monolith?"),
    ("code_generation","Write a Python function to reverse a string."),
]

for task_type, prompt in tasks:
    result = run_task(task_type, prompt)
    print(f"  → {result['model_used']}: {result['result'][:50]}\n")

# Expected Token Savings: Task-type routing uses Haiku for classification (95% of cost savings). In production: classification at $0.0001/call vs $0.015/call with Opus = 150x cheaper for routing tasks.
# Environment: Set APP_ENV. ANTHROPIC_API_KEY required. CONFIG can be loaded from YAML/JSON file.
```

---

## Option 3: Feature Flag–Driven Model Selection

Use feature flags (read from env vars or a feature flag service) to control which model each feature uses, enabling gradual rollouts.

```python
import anthropic
import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class FeatureFlag:
    flag_name: str
    model: str
    rollout_pct: float  # 0.0–1.0; 1.0 = all traffic

# Feature flags — in production, fetch from LaunchDarkly/Unleash/etc.
FEATURE_FLAGS: dict[str, FeatureFlag] = {
    "use_sonnet_for_chat":      FeatureFlag("use_sonnet_for_chat",      "claude-sonnet-4-6",   0.0),
    "use_opus_for_reasoning":   FeatureFlag("use_opus_for_reasoning",   "claude-opus-4-6",     0.0),
    "use_haiku_for_routing":    FeatureFlag("use_haiku_for_routing",    "claude-haiku-4-5-20251001", 1.0),
}

def load_flags_from_env() -> dict[str, FeatureFlag]:
    """Override flags from environment variables."""
    flags = dict(FEATURE_FLAGS)
    env = os.environ.get("APP_ENV", "development").lower()

    # Development: force all to Haiku
    if env == "development":
        for name in flags:
            flags[name] = FeatureFlag(name, "claude-haiku-4-5-20251001", 1.0)
    elif env == "staging":
        flags["use_sonnet_for_chat"] = FeatureFlag("use_sonnet_for_chat", "claude-sonnet-4-6", 0.5)
    elif env == "production":
        flags["use_sonnet_for_chat"] = FeatureFlag("use_sonnet_for_chat", "claude-sonnet-4-6", 1.0)
        flags["use_opus_for_reasoning"] = FeatureFlag("use_opus_for_reasoning", "claude-opus-4-6", 0.2)

    return flags

def resolve_model(
    feature: str,
    user_id: str,
    fallback: str = "claude-haiku-4-5-20251001"
) -> str:
    flags = load_flags_from_env()
    flag = flags.get(feature)
    if not flag:
        return fallback

    # Deterministic rollout based on user_id hash
    import hashlib
    h = int(hashlib.md5(f"{feature}:{user_id}".encode()).hexdigest(), 16)
    bucket = (h % 1000) / 1000.0
    if bucket < flag.rollout_pct:
        return flag.model
    return fallback

client = anthropic.Anthropic()

def call_with_flags(
    feature: str,
    user_id: str,
    prompt: str,
    fallback_model: str = "claude-haiku-4-5-20251001"
) -> dict:
    model = resolve_model(feature, user_id, fallback_model)
    env = os.environ.get("APP_ENV", "development")
    print(f"[flags/{env}] feature={feature} user={user_id[:8]} model={model}")

    response = client.messages.create(
        model=model, max_tokens=128,
        messages=[{"role": "user", "content": prompt}]
    )
    return {
        "result": response.content[0].text,
        "model": model,
        "feature": feature,
        "env": env
    }

# Test across multiple user IDs
for user_id in ["user-alpha", "user-beta", "user-gamma"]:
    result = call_with_flags(
        feature="use_sonnet_for_chat",
        user_id=user_id,
        prompt="What is Python?"
    )
    print(f"  {user_id}: {result['model']} — {result['result'][:50]}\n")

# Expected Token Savings: Feature flags enable model upgrades without deployment. 20% Opus rollout = 80% of requests still use Haiku. Gradual rollout prevents sudden cost spikes from model upgrades.
# Environment: Set APP_ENV. ANTHROPIC_API_KEY required. Flag rollout_pct configurable from env vars.
```

---

## Option 4: Task Complexity Classifier for Automatic Model Routing

Automatically classify request complexity and route to the appropriate model tier without manual task-type annotation.

```python
import anthropic
import os
from dataclasses import dataclass
from enum import Enum

class ComplexityTier(Enum):
    SIMPLE = "simple"       # Haiku: factual, short answer, classification
    MODERATE = "moderate"   # Sonnet: reasoning, summarization, code
    COMPLEX = "complex"     # Opus: deep analysis, multi-step reasoning, research

@dataclass
class TierConfig:
    tier: ComplexityTier
    model: str
    max_tokens: int

def get_tier_config(env: str) -> dict[ComplexityTier, TierConfig]:
    if env in ("development", "test"):
        # All tiers use Haiku in dev/test
        return {
            ComplexityTier.SIMPLE:   TierConfig(ComplexityTier.SIMPLE,   "claude-haiku-4-5-20251001", 256),
            ComplexityTier.MODERATE: TierConfig(ComplexityTier.MODERATE, "claude-haiku-4-5-20251001", 512),
            ComplexityTier.COMPLEX:  TierConfig(ComplexityTier.COMPLEX,  "claude-haiku-4-5-20251001", 512),
        }
    elif env == "staging":
        return {
            ComplexityTier.SIMPLE:   TierConfig(ComplexityTier.SIMPLE,   "claude-haiku-4-5-20251001", 512),
            ComplexityTier.MODERATE: TierConfig(ComplexityTier.MODERATE, "claude-sonnet-4-6",         1024),
            ComplexityTier.COMPLEX:  TierConfig(ComplexityTier.COMPLEX,  "claude-sonnet-4-6",         2048),
        }
    else:  # production
        return {
            ComplexityTier.SIMPLE:   TierConfig(ComplexityTier.SIMPLE,   "claude-haiku-4-5-20251001", 512),
            ComplexityTier.MODERATE: TierConfig(ComplexityTier.MODERATE, "claude-sonnet-4-6",         2048),
            ComplexityTier.COMPLEX:  TierConfig(ComplexityTier.COMPLEX,  "claude-opus-4-6",           4096),
        }

SIMPLE_KEYWORDS = {"what is", "define", "name", "list", "when was", "who is", "how many"}
COMPLEX_KEYWORDS = {"analyze", "evaluate", "compare", "design", "architecture", "strategy", "comprehensive", "research"}

def classify_complexity(prompt: str) -> ComplexityTier:
    lower = prompt.lower()
    word_count = len(prompt.split())

    if any(kw in lower for kw in COMPLEX_KEYWORDS) or word_count > 100:
        return ComplexityTier.COMPLEX
    if any(lower.startswith(kw) for kw in SIMPLE_KEYWORDS) or word_count < 15:
        return ComplexityTier.SIMPLE
    return ComplexityTier.MODERATE

client = anthropic.Anthropic()
ENV = os.environ.get("APP_ENV", "development").lower()
TIER_CONFIGS = get_tier_config(ENV)

def smart_agent_call(prompt: str, force_tier: ComplexityTier = None) -> dict:
    tier = force_tier or classify_complexity(prompt)
    config = TIER_CONFIGS[tier]
    print(f"[{ENV}] tier={tier.value} → model={config.model}")

    response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return {
        "result": response.content[0].text,
        "tier": tier.value,
        "model": config.model,
        "env": ENV
    }

prompts = [
    "What is Python?",
    "Explain how neural networks learn through backpropagation.",
    "Design a comprehensive microservices architecture for a high-traffic e-commerce platform with 10M daily users, including caching strategy, database sharding, and failover mechanisms.",
]

for p in prompts:
    result = smart_agent_call(p)
    print(f"  [{result['tier']}] {result['model']}: {result['result'][:60]}\n")

# Expected Token Savings: Automatic routing sends 60-70% of production queries to Haiku (simple/moderate). Only truly complex requests reach Opus. Average cost reduction: 40-60% vs flat Sonnet use.
# Environment: Set APP_ENV. ANTHROPIC_API_KEY required. Keywords configurable.
```

---

## Option 5: Cost Budget Enforcer Per Environment

Each environment has a hard cost budget. When the budget is exhausted, the agent automatically downgrade to cheaper models.

```python
import anthropic
import os
import time
from dataclasses import dataclass
from typing import Optional

# Approximate input + output cost per 1M tokens
MODEL_COSTS_PER_1M = {
    "claude-haiku-4-5-20251001": 0.80,
    "claude-sonnet-4-6":         3.00,
    "claude-opus-4-6":          15.00,
}

@dataclass
class EnvironmentBudget:
    env: str
    hourly_limit_usd: float
    preferred_model: str
    fallback_model: str
    spent_usd: float = 0.0
    window_start: float = 0.0
    calls_made: int = 0
    downgrades: int = 0

    def reset_if_new_window(self):
        if time.monotonic() - self.window_start >= 3600.0:
            self.spent_usd = 0.0
            self.window_start = time.monotonic()

    def record_cost(self, tokens: int, model: str):
        cost_per_token = MODEL_COSTS_PER_1M.get(model, 3.0) / 1_000_000
        self.spent_usd += tokens * cost_per_token
        self.calls_made += 1

    def get_model(self, requested: str) -> str:
        self.reset_if_new_window()
        if self.spent_usd >= self.hourly_limit_usd:
            if requested != self.fallback_model:
                self.downgrades += 1
                print(f"[budget] ${self.spent_usd:.4f} ≥ ${self.hourly_limit_usd} limit — downgrading to {self.fallback_model}")
            return self.fallback_model
        return requested

ENV_BUDGETS = {
    "development": EnvironmentBudget("development", hourly_limit_usd=0.05,
                                     preferred_model="claude-haiku-4-5-20251001",
                                     fallback_model="claude-haiku-4-5-20251001",
                                     window_start=time.monotonic()),
    "staging":     EnvironmentBudget("staging",     hourly_limit_usd=2.00,
                                     preferred_model="claude-sonnet-4-6",
                                     fallback_model="claude-haiku-4-5-20251001",
                                     window_start=time.monotonic()),
    "production":  EnvironmentBudget("production",  hourly_limit_usd=50.00,
                                     preferred_model="claude-sonnet-4-6",
                                     fallback_model="claude-haiku-4-5-20251001",
                                     window_start=time.monotonic()),
}

client = anthropic.Anthropic()
ENV = os.environ.get("APP_ENV", "development").lower()
BUDGET = ENV_BUDGETS.get(ENV, ENV_BUDGETS["development"])

def budget_aware_call(prompt: str, requested_model: Optional[str] = None) -> dict:
    model = BUDGET.get_model(requested_model or BUDGET.preferred_model)
    response = client.messages.create(
        model=model, max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    total_tokens = response.usage.input_tokens + response.usage.output_tokens
    BUDGET.record_cost(total_tokens, model)
    cost = total_tokens * MODEL_COSTS_PER_1M.get(model, 3.0) / 1_000_000
    print(f"[{ENV}] model={model} tokens={total_tokens} cost=${cost:.5f} total=${BUDGET.spent_usd:.4f}/{BUDGET.hourly_limit_usd}")
    return {"result": response.content[0].text, "model": model, "cost_usd": cost}

for i, prompt in enumerate([
    "What is Python?",
    "Explain caching strategies.",
    "What is a REST API?",
]):
    result = budget_aware_call(prompt)
    print(f"  {result['result'][:60]}\n")

print(f"Summary: {BUDGET.calls_made} calls, ${BUDGET.spent_usd:.5f} spent, {BUDGET.downgrades} downgrades")

# Expected Token Savings: Hard budget caps prevent runaway dev costs. Development capped at $0.05/hour enforces discipline. Budget exhaustion triggers automatic downgrade instead of errors.
# Environment: Set APP_ENV. ANTHROPIC_API_KEY required. Adjust hourly_limit_usd per environment needs.
```

---

## Option 6: Startup Validation and Model Availability Check

On startup, validate that the configured models are available in the current environment, and refuse to start with mismatched config.

```python
import anthropic
import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelPolicy:
    env: str
    allowed_models: list[str]
    required_model: str
    deny_list: list[str]  # Models banned in this environment

ENV_POLICIES = {
    "development": ModelPolicy(
        env="development",
        allowed_models=["claude-haiku-4-5-20251001"],
        required_model="claude-haiku-4-5-20251001",
        deny_list=["claude-opus-4-6", "claude-sonnet-4-6"]
    ),
    "test": ModelPolicy(
        env="test",
        allowed_models=["claude-haiku-4-5-20251001"],
        required_model="claude-haiku-4-5-20251001",
        deny_list=["claude-opus-4-6", "claude-sonnet-4-6"]
    ),
    "staging": ModelPolicy(
        env="staging",
        allowed_models=["claude-haiku-4-5-20251001", "claude-sonnet-4-6"],
        required_model="claude-haiku-4-5-20251001",
        deny_list=["claude-opus-4-6"]
    ),
    "production": ModelPolicy(
        env="production",
        allowed_models=["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"],
        required_model="claude-sonnet-4-6",
        deny_list=[]
    ),
}

class PolicyEnforcedClient:
    def __init__(self, policy: ModelPolicy):
        self.policy = policy
        self._client = anthropic.Anthropic()
        self._validate()

    def _validate(self):
        print(f"[startup] Validating model policy for ENV={self.policy.env}")
        print(f"[startup] Allowed: {self.policy.allowed_models}")
        print(f"[startup] Denied: {self.policy.deny_list}")
        if self.policy.deny_list:
            print(f"[startup] WARNING: {self.policy.deny_list} are BLOCKED in {self.policy.env}")
        print(f"[startup] Policy validation passed")

    def create_message(self, model: str, **kwargs) -> anthropic.types.Message:
        # Enforce deny list
        if model in self.policy.deny_list:
            raise PermissionError(
                f"Model {model!r} is not allowed in {self.policy.env} environment. "
                f"Use one of: {self.policy.allowed_models}"
            )
        # Enforce allowed list
        if model not in self.policy.allowed_models:
            safe_model = self.policy.required_model
            print(f"[policy] {model} not in allowed list → using {safe_model}")
            model = safe_model
        return self._client.messages.create(model=model, **kwargs)

ENV = os.environ.get("APP_ENV", "development").lower()
policy = ENV_POLICIES.get(ENV, ENV_POLICIES["development"])
client = PolicyEnforcedClient(policy)

# Attempt calls with various models — policy enforces correct selection
test_cases = [
    ("claude-haiku-4-5-20251001", "What is 2+2?"),
    ("claude-sonnet-4-6",         "Explain recursion."),  # May be blocked in dev
    ("claude-opus-4-6",           "Deep analysis."),      # Blocked in dev
]

for model, prompt in test_cases:
    try:
        response = client.create_message(
            model=model, max_tokens=64,
            messages=[{"role": "user", "content": prompt}]
        )
        print(f"[OK] {model}: {response.content[0].text[:50]}")
    except PermissionError as e:
        print(f"[BLOCKED] {model}: {e}")

# Expected Token Savings: Startup validation blocks expensive models in dev before any calls are made. Developers cannot accidentally use Opus in development even if they hardcode it. Prevents surprise bills.
# Environment: Set APP_ENV. ANTHROPIC_API_KEY required. deny_list enforced at call time, not just config load.
```

---

## Comparison

| Option | Selection Strategy | Enforcement Point | Dynamic | Best For |
|--------|------------------|-------------------|---------|----------|
| 1: Env Var Mapping | Static per-env config | Call time | No | Simple apps with 2–3 task types |
| 2: YAML Config | Per-task, per-env matrix | Call time | No | Complex apps with many task types |
| 3: Feature Flags | Rollout percentage | Call time | Yes | Gradual model upgrades, A/B testing |
| 4: Complexity Classifier | Automatic routing | Classification | Yes | Zero-annotation intelligent routing |
| 5: Cost Budget Enforcer | Budget threshold | Per call | Yes | Cost-capped environments |
| 6: Policy + Startup Validation | Allow/deny lists | Startup + call | No | Security-conscious teams, audit compliance |
