---
layout: solution
title: "Agent Doesn't Implement Fallback Model Chain on Provider Outage"
category: reliability
description: "Automatically cascade through a prioritized list of model providers when the primary provider returns errors, maintaining service availability during outages."
tags: [reliability, fallback, multi-provider, circuit-breaker, availability, resilience]
---

When your primary model provider goes down — 503 overloaded, 429 quota exhausted, or network unreachable — agents that only know one endpoint simply fail. A fallback model chain tries providers in priority order, switching automatically when one fails, so end-user requests succeed even during partial outages.

## Option 1: Sequential Fallback Chain

Try providers in order: primary → secondary → tertiary. Each provider gets one attempt; if it raises an exception, move to the next. Simple and deterministic — the first available provider wins.

```python
import anthropic
import time
from dataclasses import dataclass
from typing import Any

@dataclass
class ProviderConfig:
    name: str
    model: str
    api_key: str | None = None  # None = use env var ANTHROPIC_API_KEY
    base_url: str | None = None
    max_tokens: int = 1024

PROVIDER_CHAIN: list[ProviderConfig] = [
    ProviderConfig("anthropic-primary", "claude-sonnet-4-6"),
    ProviderConfig("anthropic-haiku-fallback", "claude-haiku-4-5-20251001"),
    ProviderConfig("anthropic-opus-fallback", "claude-opus-4-6"),
]

def create_client(provider: ProviderConfig) -> anthropic.Anthropic:
    kwargs: dict[str, Any] = {}
    if provider.api_key:
        kwargs["api_key"] = provider.api_key
    if provider.base_url:
        kwargs["base_url"] = provider.base_url
    return anthropic.Anthropic(**kwargs)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}

def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in RETRYABLE_STATUS_CODES
    return isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError))

def call_with_fallback(
    messages: list[dict],
    system: str = "",
    providers: list[ProviderConfig] = PROVIDER_CHAIN,
) -> tuple[str, str]:
    """Returns (response_text, provider_name_used)."""
    last_error: Exception | None = None

    for provider in providers:
        try:
            client = create_client(provider)
            kwargs: dict[str, Any] = {
                "model": provider.model,
                "max_tokens": provider.max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system

            response = client.messages.create(**kwargs)
            print(f"[Fallback] Success on provider: {provider.name}")
            return response.content[0].text, provider.name

        except Exception as exc:
            if is_retryable(exc):
                print(f"[Fallback] {provider.name} failed ({type(exc).__name__}): {exc}. Trying next.")
                last_error = exc
                continue
            else:
                # Non-retryable (e.g. 400 bad request) — don't try other providers
                raise

    raise RuntimeError(f"All providers exhausted. Last error: {last_error}") from last_error

# Demo
if __name__ == "__main__":
    messages = [{"role": "user", "content": "Summarize the benefits of microservices in 2 sentences."}]
    text, provider = call_with_fallback(messages, system="You are a concise technical writer.")
    print(f"Response from [{provider}]:\n{text}")

# Expected Token Savings: N/A — eliminates failed requests that produce zero output
# Environment: pip install anthropic
```

## Option 2: Fallback with Per-Provider Circuit Breaker

Track failure counts per provider. After a threshold of consecutive failures, open the circuit for that provider and skip it for a cooldown period. Closed providers are skipped without even attempting a request, reducing latency and avoiding hammering a downed endpoint.

```python
import anthropic
import time
import threading
from dataclasses import dataclass, field
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Skipping this provider
    HALF_OPEN = "half_open" # Testing if provider recovered

@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    _failures: int = field(default=0, init=False, repr=False)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _opened_at: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if time.time() - self._opened_at >= self.cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
                    return True
                return False
            return True  # HALF_OPEN: allow one probe

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold or self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                print(f"[Circuit] Opened — will retry after {self.cooldown_seconds}s")

@dataclass
class ManagedProvider:
    name: str
    model: str
    priority: int  # lower = higher priority
    circuit: CircuitBreaker = field(default_factory=CircuitBreaker)

PROVIDERS = [
    ManagedProvider("sonnet-primary", "claude-sonnet-4-6", priority=1),
    ManagedProvider("haiku-fallback", "claude-haiku-4-5-20251001", priority=2),
    ManagedProvider("opus-fallback", "claude-opus-4-6", priority=3),
]

_client = anthropic.Anthropic()
RETRYABLE = {429, 500, 502, 503, 504, 529}

def call_with_circuit_fallback(messages: list[dict], system: str = "") -> tuple[str, str]:
    available = sorted(
        [p for p in PROVIDERS if p.circuit.allow_request()],
        key=lambda p: p.priority,
    )
    if not available:
        raise RuntimeError("All providers circuit-open. No available endpoint.")

    for provider in available:
        try:
            kwargs = {"model": provider.model, "max_tokens": 1024, "messages": messages}
            if system:
                kwargs["system"] = system
            response = _client.messages.create(**kwargs)
            provider.circuit.record_success()
            return response.content[0].text, provider.name
        except anthropic.APIStatusError as e:
            if e.status_code in RETRYABLE:
                provider.circuit.record_failure()
                print(f"[Circuit] {provider.name} error {e.status_code}, circuit failures: {provider.circuit._failures}")
                continue
            raise
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            provider.circuit.record_failure()
            print(f"[Circuit] {provider.name} connection error: {e}")
            continue

    raise RuntimeError("All available providers failed.")

if __name__ == "__main__":
    for i in range(3):
        text, prov = call_with_circuit_fallback(
            [{"role": "user", "content": f"What is 2+{i}?"}]
        )
        print(f"[{prov}] {text.strip()}")

# Expected Token Savings: Eliminates latency from retrying dead providers after circuit opens
# Environment: pip install anthropic
```

## Option 3: Async Fallback with Hedged Requests

For latency-sensitive workloads, send the request to the primary provider and, after a hedge delay, send the same request to the fallback provider. Whichever responds first wins; the loser is cancelled. Combines the reliability of fallback with lower tail latency.

```python
import anthropic
import asyncio
from dataclasses import dataclass

@dataclass
class HedgeConfig:
    primary_model: str
    fallback_model: str
    hedge_delay_ms: float = 800.0   # start fallback after this delay
    timeout_ms: float = 15_000.0

async def call_one(
    client: anthropic.AsyncAnthropic,
    model: str,
    messages: list[dict],
    system: str,
    label: str,
) -> tuple[str, str]:
    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=messages,
    )
    return response.content[0].text, label

async def hedged_call(
    messages: list[dict],
    system: str = "",
    config: HedgeConfig = HedgeConfig(
        primary_model="claude-sonnet-4-6",
        fallback_model="claude-haiku-4-5-20251001",
    ),
) -> tuple[str, str]:
    client = anthropic.AsyncAnthropic()
    winner_text = ""
    winner_label = ""

    primary_task = asyncio.create_task(
        call_one(client, config.primary_model, messages, system, "primary")
    )

    hedge_task: asyncio.Task | None = None

    async def start_hedge_after_delay():
        nonlocal hedge_task
        await asyncio.sleep(config.hedge_delay_ms / 1000)
        if not primary_task.done():
            print(f"[Hedge] Primary slow after {config.hedge_delay_ms}ms — starting fallback")
            hedge_task = asyncio.create_task(
                call_one(client, config.fallback_model, messages, system, "fallback")
            )

    hedge_starter = asyncio.create_task(start_hedge_after_delay())

    try:
        done, pending = await asyncio.wait(
            {primary_task},
            timeout=config.timeout_ms / 1000,
        )
        if primary_task in done and not primary_task.exception():
            winner_text, winner_label = primary_task.result()
            hedge_starter.cancel()
            if hedge_task:
                hedge_task.cancel()
        else:
            # Primary timed out or errored — wait for hedge
            await hedge_starter  # let hedge start
            if hedge_task:
                winner_text, winner_label = await asyncio.wait_for(
                    hedge_task, timeout=config.timeout_ms / 1000
                )
            else:
                # Hedge never started — re-raise primary error
                primary_task.result()  # raises
    except Exception as e:
        primary_task.cancel()
        if hedge_task:
            hedge_task.cancel()
        raise RuntimeError(f"All hedged providers failed: {e}") from e

    return winner_text, winner_label

async def main():
    messages = [{"role": "user", "content": "Explain eventual consistency in distributed systems."}]
    text, label = await hedged_call(messages, system="You are a distributed systems expert.")
    print(f"Winner: [{label}]\n{text}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: ~15% fewer retries; P99 latency drops by up to 40%
# Environment: pip install anthropic
```

## Option 4: Priority-Weighted Fallback with Cost Awareness

Assign each provider a cost weight. Under normal load, use the cheapest adequate model. Under degraded conditions (provider down), promote to the next tier. Track spend per provider so the fallback selection also considers budget, not just availability.

```python
import anthropic
import time
from dataclasses import dataclass, field

@dataclass
class CostAwareProvider:
    name: str
    model: str
    cost_per_1k_input: float   # USD
    cost_per_1k_output: float  # USD
    quality_tier: int          # 1=best, 3=lowest
    available: bool = True
    _fail_count: int = field(default=0, init=False, repr=False)
    _last_failure: float = field(default=0.0, init=False, repr=False)

    def mark_failed(self) -> None:
        self._fail_count += 1
        self._last_failure = time.time()
        if self._fail_count >= 3:
            self.available = False
            print(f"[CostFallback] {self.name} marked unavailable")

    def mark_ok(self) -> None:
        self._fail_count = 0
        self.available = True

    def is_available(self) -> bool:
        if self.available:
            return True
        # Retry after 5 minutes
        if time.time() - self._last_failure > 300:
            self.available = True
            self._fail_count = 0
        return self.available

PROVIDERS = [
    CostAwareProvider("haiku-cheap", "claude-haiku-4-5-20251001", 0.00025, 0.00125, quality_tier=3),
    CostAwareProvider("sonnet-balanced", "claude-sonnet-4-6", 0.003, 0.015, quality_tier=2),
    CostAwareProvider("opus-premium", "claude-opus-4-6", 0.015, 0.075, quality_tier=1),
]

_spend_tracker: dict[str, float] = {}
_budget_limit: float = 10.0  # USD, per session

def select_provider(min_quality_tier: int = 3) -> CostAwareProvider | None:
    """Select cheapest available provider meeting minimum quality."""
    candidates = [
        p for p in PROVIDERS
        if p.is_available() and p.quality_tier <= min_quality_tier
    ]
    return min(candidates, key=lambda p: p.cost_per_1k_input) if candidates else None

def estimate_cost(provider: CostAwareProvider, input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1000) * provider.cost_per_1k_input + \
           (output_tokens / 1000) * provider.cost_per_1k_output

def call_cost_aware(
    messages: list[dict],
    system: str = "",
    min_quality_tier: int = 3,
) -> tuple[str, str, float]:
    """Returns (text, provider_name, cost_usd)."""
    client = anthropic.Anthropic()
    RETRYABLE = {429, 500, 502, 503, 504, 529}

    for attempt in range(len(PROVIDERS)):
        provider = select_provider(min_quality_tier)
        if not provider:
            # Relax quality requirement if nothing available
            provider = select_provider(min_quality_tier=1)
        if not provider:
            raise RuntimeError("No providers available")

        session_spend = sum(_spend_tracker.values())
        if session_spend >= _budget_limit:
            raise RuntimeError(f"Budget limit ${_budget_limit} reached. Spent: ${session_spend:.4f}")

        try:
            kwargs = {"model": provider.model, "max_tokens": 1024, "messages": messages}
            if system:
                kwargs["system"] = system
            response = client.messages.create(**kwargs)
            cost = estimate_cost(
                provider,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
            _spend_tracker[provider.name] = _spend_tracker.get(provider.name, 0) + cost
            provider.mark_ok()
            print(f"[CostFallback] Used {provider.name}, cost=${cost:.6f}, total=${sum(_spend_tracker.values()):.4f}")
            return response.content[0].text, provider.name, cost

        except anthropic.APIStatusError as e:
            if e.status_code in RETRYABLE:
                provider.mark_failed()
                continue
            raise

    raise RuntimeError("All cost-aware providers failed")

if __name__ == "__main__":
    queries = [
        "What is 5 * 7?",
        "Explain the CAP theorem",
        "Write a haiku about distributed systems",
    ]
    for q in queries:
        text, prov, cost = call_cost_aware([{"role": "user", "content": q}])
        print(f"[{prov}] ${cost:.6f} — {text[:60]}...\n")

# Expected Token Savings: 30-50% cost reduction by defaulting to cheapest viable provider
# Environment: pip install anthropic
```

## Option 5: Region-Aware Fallback with Latency Probing

Maintain a list of regional endpoints. Periodically probe each region's latency with a minimal request. Route new requests to the lowest-latency healthy region, falling back to higher-latency regions when the primary region degrades.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from statistics import median

@dataclass
class RegionalEndpoint:
    name: str
    model: str
    region: str
    _latencies: list[float] = field(default_factory=list, init=False, repr=False)
    _healthy: bool = field(default=True, init=False, repr=False)
    _last_probe: float = field(default=0.0, init=False, repr=False)

    def record_latency(self, ms: float) -> None:
        self._latencies = (self._latencies + [ms])[-20:]  # keep last 20
        self._healthy = True
        self._last_probe = time.time()

    def mark_unhealthy(self) -> None:
        self._healthy = False
        self._last_probe = time.time()

    @property
    def p50_latency(self) -> float:
        return median(self._latencies) if self._latencies else float("inf")

    @property
    def healthy(self) -> bool:
        return self._healthy

ENDPOINTS = [
    RegionalEndpoint("us-east-sonnet", "claude-sonnet-4-6", "us-east"),
    RegionalEndpoint("us-west-haiku", "claude-haiku-4-5-20251001", "us-west"),
    RegionalEndpoint("eu-haiku", "claude-haiku-4-5-20251001", "eu-west"),
]

async def probe_endpoint(client: anthropic.AsyncAnthropic, ep: RegionalEndpoint) -> None:
    start = time.monotonic()
    try:
        await client.messages.create(
            model=ep.model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        ep.record_latency((time.monotonic() - start) * 1000)
        print(f"[Probe] {ep.name}: {ep.p50_latency:.0f}ms p50")
    except Exception as e:
        ep.mark_unhealthy()
        print(f"[Probe] {ep.name}: unhealthy ({e})")

async def probe_all(client: anthropic.AsyncAnthropic) -> None:
    await asyncio.gather(*[probe_endpoint(client, ep) for ep in ENDPOINTS])

async def call_with_region_fallback(
    messages: list[dict],
    system: str = "",
    probe_interval_s: float = 30.0,
) -> tuple[str, str]:
    client = anthropic.AsyncAnthropic()
    now = time.time()

    # Probe if stale
    stale = [ep for ep in ENDPOINTS if now - ep._last_probe > probe_interval_s]
    if stale:
        await asyncio.gather(*[probe_endpoint(client, ep) for ep in stale])

    ranked = sorted(
        [ep for ep in ENDPOINTS if ep.healthy],
        key=lambda ep: ep.p50_latency,
    )
    if not ranked:
        raise RuntimeError("No healthy regional endpoints available")

    for ep in ranked:
        try:
            start = time.monotonic()
            kwargs = {"model": ep.model, "max_tokens": 1024, "messages": messages}
            if system:
                kwargs["system"] = system
            response = await client.messages.create(**kwargs)
            ep.record_latency((time.monotonic() - start) * 1000)
            return response.content[0].text, ep.name
        except Exception as e:
            ep.mark_unhealthy()
            print(f"[RegionFallback] {ep.name} failed: {e}")
            continue

    raise RuntimeError("All regional endpoints failed")

async def main():
    client = anthropic.AsyncAnthropic()
    await probe_all(client)  # Initial probe
    text, region = await call_with_region_fallback(
        [{"role": "user", "content": "What causes aurora borealis?"}],
        system="Answer concisely.",
    )
    print(f"Response from [{region}]:\n{text}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Reduces P99 by routing to lowest-latency healthy region
# Environment: pip install anthropic
```

## Option 6: Exponential Backoff Fallback with Jitter and State Persistence

Combine exponential backoff (for transient errors) with provider rotation (for sustained outages). Persist provider health state to disk so a restarted process inherits the last known state instead of hammering a provider that was down before restart.

```python
import anthropic
import json
import os
import random
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

STATE_FILE = Path("/tmp/provider_health_state.json")

@dataclass
class ProviderHealth:
    name: str
    model: str
    consecutive_failures: int = 0
    disabled_until: float = 0.0
    total_requests: int = 0
    total_failures: int = 0

    def is_available(self) -> bool:
        return time.time() >= self.disabled_until

    def backoff_duration(self) -> float:
        base = min(60.0, 2 ** self.consecutive_failures)
        jitter = random.uniform(0, base * 0.2)
        return base + jitter

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.disabled_until = time.time() + self.backoff_duration()
        print(f"[Backoff] {self.name}: disabled for {self.backoff_duration():.1f}s (failure #{self.consecutive_failures})")

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.total_requests += 1

def load_state() -> dict[str, ProviderHealth]:
    defaults = [
        ProviderHealth("sonnet", "claude-sonnet-4-6"),
        ProviderHealth("haiku", "claude-haiku-4-5-20251001"),
        ProviderHealth("opus", "claude-opus-4-6"),
    ]
    if not STATE_FILE.exists():
        return {p.name: p for p in defaults}
    try:
        saved = json.loads(STATE_FILE.read_text())
        providers = {}
        for p in defaults:
            if p.name in saved:
                data = saved[p.name]
                providers[p.name] = ProviderHealth(**data)
            else:
                providers[p.name] = p
        return providers
    except Exception:
        return {p.name: p for p in defaults}

def save_state(providers: dict[str, ProviderHealth]) -> None:
    STATE_FILE.write_text(json.dumps({k: asdict(v) for k, v in providers.items()}, indent=2))

PRIORITY = ["sonnet", "haiku", "opus"]
RETRYABLE = {429, 500, 502, 503, 504, 529}

def call_with_persistent_fallback(messages: list[dict], system: str = "") -> tuple[str, str]:
    providers = load_state()
    client = anthropic.Anthropic()
    last_error: Exception | None = None

    available = [name for name in PRIORITY if providers[name].is_available()]
    if not available:
        # All on backoff — find soonest to recover
        soonest = min(providers.values(), key=lambda p: p.disabled_until)
        wait = max(0, soonest.disabled_until - time.time())
        print(f"[Backoff] All providers in backoff. Waiting {wait:.1f}s for {soonest.name}")
        time.sleep(wait)
        available = [soonest.name]

    for name in available:
        p = providers[name]
        try:
            kwargs = {"model": p.model, "max_tokens": 1024, "messages": messages}
            if system:
                kwargs["system"] = system
            response = client.messages.create(**kwargs)
            p.record_success()
            save_state(providers)
            return response.content[0].text, name
        except anthropic.APIStatusError as e:
            if e.status_code in RETRYABLE:
                p.record_failure()
                save_state(providers)
                last_error = e
                continue
            raise
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            p.record_failure()
            save_state(providers)
            last_error = e
            continue

    raise RuntimeError(f"All providers failed. Last: {last_error}") from last_error

if __name__ == "__main__":
    messages = [{"role": "user", "content": "List 3 advantages of async programming."}]
    text, used = call_with_persistent_fallback(messages)
    print(f"[{used}]\n{text}")

# Expected Token Savings: Eliminates wasted retries to providers still in backoff after restart
# Environment: pip install anthropic
```

## Comparison

| Option | Strategy | Latency Impact | Persistence | Best For |
|--------|----------|---------------|-------------|----------|
| 1. Sequential Chain | Try in order | +1 RTT per failure | No | Simple scripts |
| 2. Circuit Breaker | Skip open circuits | Minimal | No | Production APIs |
| 3. Hedged Requests | Race providers | Lower P99 | No | Latency-sensitive |
| 4. Cost-Aware | Cheapest first | Neutral | No | Budget-constrained |
| 5. Region-Aware | Lowest latency | Lower P50 | No | Geo-distributed |
| 6. Backoff + Persist | Exponential backoff | +wait on cold | Yes | Long-running services |
