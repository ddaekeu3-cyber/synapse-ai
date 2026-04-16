---
layout: solution
title: "Agent Doesn't Implement Multi-Model Fallback Chain"
category: general
description: "Agent is hardcoded to one model and fails completely when that model is unavailable or overloaded, instead of falling back to an equivalent alternative."
tags: [general, fallback, reliability, multi-model, resilience]
---

# Agent Doesn't Implement Multi-Model Fallback Chain

## Problem

Agents hardcoded to a single model become completely unavailable whenever that model is overloaded (529), rate-limited (429), or experiencing an outage. A fallback chain defines an ordered list of models to try in sequence — or tiers to downgrade through — so the agent degrades gracefully rather than failing entirely. This converts hard outages into soft degradations where users may receive a slightly slower or less capable response instead of an error.

## Solution Options

### Option 1: Simple Linear Fallback Chain

```python
import anthropic
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ModelTier:
    model: str
    label: str
    max_tokens: int

# Ordered from most to least preferred
FALLBACK_CHAIN: list[ModelTier] = [
    ModelTier("claude-opus-4-6", "primary", 1024),
    ModelTier("claude-sonnet-4-6", "secondary", 512),
    ModelTier("claude-haiku-4-5-20251001", "fallback", 256),
]

RETRYABLE_STATUS_CODES = {429, 529, 500, 502, 503, 504}

def call_with_fallback(
    messages: list[dict],
    system: str = "",
    max_retries_per_model: int = 1,
) -> dict:
    """Try each model in the chain; return first successful response."""
    last_error = None

    for tier in FALLBACK_CHAIN:
        for attempt in range(max_retries_per_model + 1):
            try:
                kwargs = {
                    "model": tier.model,
                    "max_tokens": tier.max_tokens,
                    "messages": messages,
                }
                if system:
                    kwargs["system"] = system

                response = client.messages.create(**kwargs)
                if attempt > 0 or tier.label != "primary":
                    print(f"[FALLBACK] Used {tier.label} model: {tier.model}")

                return {
                    "text": response.content[0].text,
                    "model": tier.model,
                    "tier": tier.label,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                }
            except anthropic.RateLimitError as e:
                last_error = e
                print(f"[RATE LIMIT] {tier.model} — trying next tier")
                break  # Don't retry same model on rate limit; move to next
            except anthropic.APIStatusError as e:
                last_error = e
                if e.status_code in RETRYABLE_STATUS_CODES:
                    if attempt < max_retries_per_model:
                        wait = 2 ** attempt
                        print(f"[RETRY] {tier.model} attempt {attempt+1} — waiting {wait}s")
                        time.sleep(wait)
                    else:
                        print(f"[EXHAUSTED] {tier.model} — trying next tier")
                        break
                else:
                    raise  # Non-retryable error
            except anthropic.APIConnectionError:
                last_error = Exception("Connection error")
                print(f"[CONN ERROR] {tier.model} — trying next tier")
                break

    raise RuntimeError(f"All models in fallback chain exhausted. Last error: {last_error}")

# Usage
result = call_with_fallback(
    messages=[{"role": "user", "content": "Explain what a REST API is."}],
    system="You are a helpful assistant.",
)
print(f"Model used: {result['tier']} ({result['model']})")
print(f"Response: {result['text'][:100]}...")

# Expected Token Savings: None — fallback may use fewer tokens on cheaper models
# Environment: Any production agent requiring high availability across model tiers
```

### Option 2: Capability-Aware Fallback with Graceful Degradation

```python
import anthropic
from dataclasses import dataclass, field
from enum import Enum
import time

client = anthropic.Anthropic()

class Capability(Enum):
    TOOL_USE = "tool_use"
    VISION = "vision"
    LONG_CONTEXT = "long_context"
    EXTENDED_THINKING = "extended_thinking"

@dataclass
class ModelSpec:
    model: str
    label: str
    capabilities: set[Capability]
    max_context: int
    max_output: int
    cost_tier: int  # 1=cheapest, 3=most expensive

MODEL_REGISTRY: list[ModelSpec] = [
    ModelSpec(
        "claude-opus-4-6", "opus",
        {Capability.TOOL_USE, Capability.VISION, Capability.LONG_CONTEXT, Capability.EXTENDED_THINKING},
        200_000, 32_000, 3,
    ),
    ModelSpec(
        "claude-sonnet-4-6", "sonnet",
        {Capability.TOOL_USE, Capability.VISION, Capability.LONG_CONTEXT},
        200_000, 8_096, 2,
    ),
    ModelSpec(
        "claude-haiku-4-5-20251001", "haiku",
        {Capability.TOOL_USE, Capability.VISION},
        200_000, 8_096, 1,
    ),
]

def find_capable_models(required: set[Capability]) -> list[ModelSpec]:
    """Return models that support all required capabilities, sorted by preference."""
    return [m for m in MODEL_REGISTRY if required.issubset(m.capabilities)]

def call_with_capability_fallback(
    messages: list[dict],
    system: str = "",
    required_capabilities: set[Capability] | None = None,
    tools: list[dict] | None = None,
) -> dict:
    if required_capabilities is None:
        required_capabilities = set()
    if tools:
        required_capabilities.add(Capability.TOOL_USE)

    capable_models = find_capable_models(required_capabilities)
    if not capable_models:
        raise ValueError(f"No models support required capabilities: {required_capabilities}")

    degradation_log = []

    for spec in capable_models:
        try:
            kwargs: dict = {
                "model": spec.model,
                "max_tokens": spec.max_output,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            if tools:
                kwargs["tools"] = tools

            response = client.messages.create(**kwargs)
            return {
                "text": next((b.text for b in response.content if hasattr(b, "text")), ""),
                "tool_use": [b for b in response.content if b.type == "tool_use"],
                "model": spec.model,
                "label": spec.label,
                "degraded": len(degradation_log) > 0,
                "degradation_log": degradation_log,
                "stop_reason": response.stop_reason,
            }
        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            degradation_log.append({
                "model": spec.model,
                "error": str(e)[:60],
            })
            print(f"[DEGRADED] {spec.label} unavailable — trying next capable model")
            time.sleep(0.5)

    raise RuntimeError(f"All capable models exhausted. Degradation log: {degradation_log}")

# Example: request requiring tool use
tools = [{
    "name": "search",
    "description": "Search the web",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}]

result = call_with_capability_fallback(
    messages=[{"role": "user", "content": "Search for information about async Python."}],
    system="You are a helpful assistant.",
    required_capabilities={Capability.TOOL_USE},
    tools=tools,
)

print(f"Model: {result['label']} | Degraded: {result['degraded']}")
print(f"Stop reason: {result['stop_reason']}")
if result["degradation_log"]:
    print(f"Degradation path: {result['degradation_log']}")

# Expected Token Savings: Fallback to cheaper models reduces cost 70-90% when primary is unavailable
# Environment: Agents with capability requirements where not all fallbacks support all features
```

### Option 3: Async Fallback with Race-to-First

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

@dataclass
class AsyncModelTier:
    model: str
    label: str
    delay_ms: float  # Delay before launching this tier (for ordered preference)
    max_tokens: int

ASYNC_CHAIN: list[AsyncModelTier] = [
    AsyncModelTier("claude-opus-4-6", "primary", delay_ms=0, max_tokens=512),
    AsyncModelTier("claude-sonnet-4-6", "secondary", delay_ms=2000, max_tokens=512),  # Try after 2s
    AsyncModelTier("claude-haiku-4-5-20251001", "tertiary", delay_ms=4000, max_tokens=256),  # Try after 4s
]

async def attempt_model(
    tier: AsyncModelTier,
    messages: list[dict],
    system: str,
    result_holder: list,
    cancel_event: asyncio.Event,
) -> None:
    """Try a model after its delay; set result_holder if first to succeed."""
    if tier.delay_ms > 0:
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=tier.delay_ms / 1000)
            return  # Another model already won
        except asyncio.TimeoutError:
            pass  # Timeout expired, proceed with this tier

    if cancel_event.is_set():
        return

    try:
        print(f"[ASYNC FALLBACK] Launching {tier.label} ({tier.model})")
        response = await async_client.messages.create(
            model=tier.model,
            max_tokens=tier.max_tokens,
            system=system,
            messages=messages,
        )
        if not cancel_event.is_set():
            result_holder.append({
                "text": response.content[0].text,
                "model": tier.model,
                "label": tier.label,
            })
            cancel_event.set()
    except Exception as e:
        print(f"[ASYNC FALLBACK] {tier.label} failed: {e}")

async def async_fallback_call(
    messages: list[dict],
    system: str = "",
    timeout_seconds: float = 15.0,
) -> dict:
    """Race all tiers; first success wins. Tiers launch with delays to prefer faster primary."""
    t0 = time.monotonic()
    cancel_event = asyncio.Event()
    result_holder: list[dict] = []

    tasks = [
        asyncio.create_task(attempt_model(tier, messages, system, result_holder, cancel_event))
        for tier in ASYNC_CHAIN
    ]

    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        pass

    for task in tasks:
        task.cancel()

    if not result_holder:
        raise RuntimeError("All async model tiers failed or timed out")

    result = result_holder[0]
    result["total_latency_ms"] = int((time.monotonic() - t0) * 1000)
    return result

async def main():
    messages = [{"role": "user", "content": "What is the event loop in Python?"}]

    result = await async_fallback_call(
        messages=messages,
        system="You are a helpful assistant.",
        timeout_seconds=30.0,
    )
    print(f"Winner: {result['label']} ({result['model']}) | Latency: {result['total_latency_ms']}ms")
    print(f"Response: {result['text'][:100]}...")

asyncio.run(main())

# Expected Token Savings: None; async fallback minimizes wait time by racing tiers in parallel
# Environment: Latency-sensitive agents where waiting for primary timeout is too costly
```

### Option 4: Fallback Chain with Circuit Breaker per Model

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CircuitBreaker:
    """Per-model circuit breaker: open after N failures, reset after cooldown."""
    model: str
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0

    _failures: int = 0
    _last_failure: float = 0.0
    _state: str = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    def is_open(self) -> bool:
        if self._state == "OPEN":
            if time.time() - self._last_failure > self.cooldown_seconds:
                self._state = "HALF_OPEN"
                print(f"[CB:{self.model[:20]}] → HALF_OPEN (testing recovery)")
                return False
            return True
        return False

    def record_success(self):
        self._failures = 0
        if self._state == "HALF_OPEN":
            self._state = "CLOSED"
            print(f"[CB:{self.model[:20]}] → CLOSED (recovered)")

    def record_failure(self):
        self._failures += 1
        self._last_failure = time.time()
        if self._failures >= self.failure_threshold:
            self._state = "OPEN"
            print(f"[CB:{self.model[:20]}] → OPEN ({self._failures} failures)")

@dataclass
class FallbackChain:
    tiers: list[tuple[str, int]]  # (model, max_tokens) pairs

    _breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    _call_stats: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        for model, _ in self.tiers:
            self._breakers[model] = CircuitBreaker(model=model, failure_threshold=2, cooldown_seconds=30)
            self._call_stats[model] = {"success": 0, "failure": 0, "skipped_cb": 0}

    def call(self, messages: list[dict], system: str = "") -> dict:
        for model, max_tokens in self.tiers:
            breaker = self._breakers[model]
            if breaker.is_open():
                self._call_stats[model]["skipped_cb"] += 1
                print(f"[CB SKIP] {model} circuit is OPEN — skipping")
                continue

            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                )
                breaker.record_success()
                self._call_stats[model]["success"] += 1
                return {
                    "text": response.content[0].text,
                    "model": model,
                    "stats": self.stats(),
                }
            except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
                breaker.record_failure()
                self._call_stats[model]["failure"] += 1
                print(f"[FAILURE] {model}: {e}")
                time.sleep(0.2)

        raise RuntimeError("All fallback chain models are unavailable or circuit-broken")

    def stats(self) -> dict:
        return {model: dict(data) for model, data in self._call_stats.items()}

chain = FallbackChain(tiers=[
    ("claude-opus-4-6", 512),
    ("claude-sonnet-4-6", 512),
    ("claude-haiku-4-5-20251001", 256),
])

# Simulate requests
for i in range(5):
    try:
        result = chain.call(
            messages=[{"role": "user", "content": f"Brief answer to question {i+1}."}],
            system="You are a helpful assistant.",
        )
        print(f"[OK] Request {i+1}: model={result['model']}")
    except RuntimeError as e:
        print(f"[FAIL] Request {i+1}: {e}")

print(f"\nChain stats: {chain.stats()}")

# Expected Token Savings: None; circuit breaker prevents wasted retries on known-bad models
# Environment: Long-running agents where failing fast on broken models is critical
```

### Option 5: Cost-Optimized Fallback with Budget Tracking

```python
import anthropic
from dataclasses import dataclass, field
import time

client = anthropic.Anthropic()

MODEL_COSTS = {
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.25,  "output": 1.25},
}

@dataclass
class CostBudget:
    limit_usd: float
    spent_usd: float = 0.0

    def can_afford(self, model: str, est_output_tokens: int = 500) -> bool:
        costs = MODEL_COSTS.get(model, {"input": 0, "output": 0})
        est_cost = est_output_tokens / 1_000_000 * costs["output"]
        return self.spent_usd + est_cost <= self.limit_usd

    def record(self, model: str, in_tokens: int, out_tokens: int) -> float:
        costs = MODEL_COSTS[model]
        cost = in_tokens / 1_000_000 * costs["input"] + out_tokens / 1_000_000 * costs["output"]
        self.spent_usd += cost
        return cost

    @property
    def remaining(self) -> float:
        return self.limit_usd - self.spent_usd

@dataclass
class CostOptimizedChain:
    """
    Falls back to cheaper models not just on errors, but also when
    budget constraints prevent using more expensive models.
    """
    budget: CostBudget
    preferred_tiers: list[tuple[str, int]] = field(default_factory=list)

    def __post_init__(self):
        if not self.preferred_tiers:
            self.preferred_tiers = [
                ("claude-opus-4-6", 512),
                ("claude-sonnet-4-6", 512),
                ("claude-haiku-4-5-20251001", 256),
            ]

    def call(self, messages: list[dict], system: str = "", min_quality: str = "any") -> dict:
        # Map min_quality to minimum tier
        quality_gates = {"high": 0, "medium": 1, "any": 2}
        min_tier_idx = quality_gates.get(min_quality, 2)

        for i, (model, max_tokens) in enumerate(self.preferred_tiers):
            # Skip if below minimum quality for this task
            if i < min_tier_idx:
                continue

            # Skip if budget can't afford this model
            if not self.budget.can_afford(model):
                print(f"[BUDGET] Can't afford {model} (${self.budget.remaining:.4f} left) — downgrading")
                continue

            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                )
                cost = self.budget.record(model, response.usage.input_tokens, response.usage.output_tokens)
                print(f"[COST] {model}: ${cost:.5f} | Remaining: ${self.budget.remaining:.4f}")
                return {
                    "text": response.content[0].text,
                    "model": model,
                    "cost_usd": cost,
                    "budget_remaining": self.budget.remaining,
                }
            except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
                print(f"[ERROR] {model}: {e}")
                time.sleep(0.5)

        raise RuntimeError(f"No models available within budget ${self.budget.remaining:.4f}")

budget = CostBudget(limit_usd=0.01)  # $0.01 session budget
chain = CostOptimizedChain(budget=budget)

tasks = [
    ("Analyze the philosophical implications of AI consciousness in depth.", "high"),
    ("Summarize the above in 2 sentences.", "medium"),
    ("Translate 'hello' to French.", "any"),
    ("What is 2 + 2?", "any"),
]

for task_text, quality in tasks:
    print(f"\nTask ({quality} quality): {task_text[:50]}...")
    try:
        result = chain.call(
            messages=[{"role": "user", "content": task_text}],
            system="You are a helpful assistant.",
            min_quality=quality,
        )
        print(f"Used: {result['model']} | Cost: ${result['cost_usd']:.5f}")
    except RuntimeError as e:
        print(f"Failed: {e}")

# Expected Token Savings: 50-90% on budget-constrained sessions via automatic downgrade
# Environment: Cost-capped agents where budget depletion should trigger graceful degradation
```

### Option 6: Provider-Level Fallback (Anthropic + OpenAI-Compatible)

```python
import anthropic
import json
import time
from dataclasses import dataclass
from typing import Any

client = anthropic.Anthropic()

@dataclass
class ProviderSpec:
    name: str
    model: str
    provider: str  # "anthropic" or "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = 512
    priority: int = 1  # Lower = higher priority

def call_anthropic(spec: ProviderSpec, messages: list[dict], system: str) -> dict:
    response = client.messages.create(
        model=spec.model,
        max_tokens=spec.max_tokens,
        system=system,
        messages=messages,
    )
    return {
        "text": response.content[0].text,
        "model": spec.model,
        "provider": "anthropic",
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }

def call_openai_compatible(spec: ProviderSpec, messages: list[dict], system: str) -> dict:
    """Fallback to an OpenAI-compatible endpoint (e.g., local Ollama, Groq, Together AI)."""
    import urllib.request
    import urllib.error

    oai_messages = [{"role": "system", "content": system}] + messages
    payload = json.dumps({
        "model": spec.model,
        "messages": oai_messages,
        "max_tokens": spec.max_tokens,
    }).encode()

    req = urllib.request.Request(
        f"{spec.base_url}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {spec.api_key or 'none'}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return {
                "text": data["choices"][0]["message"]["content"],
                "model": spec.model,
                "provider": spec.name,
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
            }
    except Exception as e:
        raise RuntimeError(f"{spec.name} call failed: {e}")

# Define provider fallback chain
PROVIDER_CHAIN: list[ProviderSpec] = [
    ProviderSpec("anthropic_opus", "claude-opus-4-6", "anthropic", priority=1),
    ProviderSpec("anthropic_sonnet", "claude-sonnet-4-6", "anthropic", priority=2),
    ProviderSpec("anthropic_haiku", "claude-haiku-4-5-20251001", "anthropic", priority=3),
    ProviderSpec("local_ollama", "llama3", "openai_compatible",
                 base_url="http://localhost:11434/v1", priority=4),  # Local fallback
]

def multi_provider_call(
    messages: list[dict],
    system: str = "You are a helpful assistant.",
    degradation_log: list | None = None,
) -> dict:
    if degradation_log is None:
        degradation_log = []

    for spec in sorted(PROVIDER_CHAIN, key=lambda s: s.priority):
        try:
            if spec.provider == "anthropic":
                result = call_anthropic(spec, messages, system)
            else:
                result = call_openai_compatible(spec, messages, system)

            result["degradation_log"] = degradation_log
            result["degraded"] = len(degradation_log) > 0
            if degradation_log:
                print(f"[FALLBACK] Serving from {spec.name} after {len(degradation_log)} failures")
            return result

        except Exception as e:
            degradation_log.append({"provider": spec.name, "error": str(e)[:60]})
            print(f"[FAIL] {spec.name}: {e}")
            time.sleep(0.3)

    raise RuntimeError(f"All providers exhausted. Log: {degradation_log}")

result = multi_provider_call(
    messages=[{"role": "user", "content": "What is event-driven architecture?"}],
)
print(f"Provider: {result['provider']} | Model: {result['model']}")
print(f"Degraded: {result['degraded']} | Degradation log: {result['degradation_log']}")
print(f"Response: {result['text'][:100]}...")

# Expected Token Savings: None; cross-provider fallback ensures continuity during vendor outages
# Environment: Mission-critical agents requiring vendor-level redundancy
```

## Comparison

| Option | Fallback Strategy | Circuit Breaker | Async | Cost-Aware | Cross-Provider | Best For |
|--------|-----------------|----------------|-------|-----------|---------------|---------|
| 1. Linear Chain | Sequential try | No | No | No | No | Basic single-vendor fallback |
| 2. Capability-Aware | Capability filter | No | No | No | No | Agents with capability requirements |
| 3. Async Race | Delayed parallel | No | Yes | No | No | Latency-sensitive fallback |
| 4. Circuit Breaker | Sequential + CB | Yes | No | No | No | Long-running agents with model instability |
| 5. Cost-Optimized | Budget-gated | No | No | Yes | No | Budget-constrained agents |
| 6. Multi-Provider | Cross-vendor | No | No | No | Yes | Mission-critical vendor redundancy |
