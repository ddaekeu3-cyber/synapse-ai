---
title: "Agent Doesn't Implement Degraded Mode Operation"
description: "Solutions for keeping an AI agent partially functional when dependencies fail — returning reduced-capability responses instead of hard errors when models, tools, or services are unavailable."
tags: [reliability, degraded-mode, graceful-degradation, fallback, resilience]
difficulty: intermediate
---

## Problem

When a model API is overloaded, a tool times out, or a downstream service goes down, agents typically fail completely: returning an error, crashing, or hanging. Instead, agents should detect partial failures and operate in a degraded mode — serving reduced but still useful responses — until full capability is restored.

---

## Solution 1: Capability-Tiered Fallback System

Define capability tiers (full, reduced, minimal, offline) and automatically step down to the highest available tier.

```python
import anthropic
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable

client = anthropic.Anthropic()

class CapabilityTier(Enum):
    FULL = "full"          # All tools + best model
    REDUCED = "reduced"    # Core tools + cheaper model
    MINIMAL = "minimal"    # No tools + cheapest model
    OFFLINE = "offline"    # Cached responses only

@dataclass
class TierConfig:
    tier: CapabilityTier
    model: str
    tools_enabled: bool
    max_tokens: int
    timeout_seconds: int
    description: str

TIER_CONFIGS = {
    CapabilityTier.FULL: TierConfig(
        tier=CapabilityTier.FULL,
        model="claude-opus-4-6",
        tools_enabled=True,
        max_tokens=4096,
        timeout_seconds=60,
        description="Full capability with all tools",
    ),
    CapabilityTier.REDUCED: TierConfig(
        tier=CapabilityTier.REDUCED,
        model="claude-sonnet-4-6",
        tools_enabled=True,
        max_tokens=2048,
        timeout_seconds=30,
        description="Reduced capability, fast model",
    ),
    CapabilityTier.MINIMAL: TierConfig(
        tier=CapabilityTier.MINIMAL,
        model="claude-haiku-4-5-20251001",
        tools_enabled=False,
        max_tokens=512,
        timeout_seconds=15,
        description="Minimal: text-only, haiku model",
    ),
    CapabilityTier.OFFLINE: TierConfig(
        tier=CapabilityTier.OFFLINE,
        model="",
        tools_enabled=False,
        max_tokens=0,
        timeout_seconds=0,
        description="Offline: cached responses only",
    ),
}

RESPONSE_CACHE = {
    "what is python": "Python is a high-level programming language known for readability and versatility.",
    "hello": "Hello! I'm currently operating in offline mode with limited capability.",
}

class TieredAgent:
    def __init__(self, tools: list = None):
        self._tools = tools or []
        self._current_tier = CapabilityTier.FULL
        self._failure_counts: dict[CapabilityTier, int] = {}
        self._tier_degraded_at: dict[CapabilityTier, float] = {}
        self._recovery_window = 120  # seconds before trying to recover

    def _try_recover(self):
        now = time.time()
        tier_order = [CapabilityTier.FULL, CapabilityTier.REDUCED, CapabilityTier.MINIMAL]
        for tier in tier_order:
            if tier < self._current_tier:
                degraded_at = self._tier_degraded_at.get(tier, 0)
                if now - degraded_at > self._recovery_window:
                    print(f"[Recovery] Attempting to restore tier: {tier.value}")
                    self._current_tier = tier
                    return

    def _degrade(self, reason: str):
        tier_order = [CapabilityTier.FULL, CapabilityTier.REDUCED,
                      CapabilityTier.MINIMAL, CapabilityTier.OFFLINE]
        current_idx = tier_order.index(self._current_tier)
        if current_idx < len(tier_order) - 1:
            old_tier = self._current_tier
            self._tier_degraded_at[old_tier] = time.time()
            self._current_tier = tier_order[current_idx + 1]
            print(f"[Degradation] {old_tier.value} → {self._current_tier.value}: {reason}")

    def respond(self, user_message: str) -> dict:
        self._try_recover()

        if self._current_tier == CapabilityTier.OFFLINE:
            cached = RESPONSE_CACHE.get(user_message.lower().strip())
            return {
                "tier": "offline",
                "response": cached or "Service temporarily unavailable. Please try again later.",
                "degraded": True,
            }

        config = TIER_CONFIGS[self._current_tier]
        messages = [{"role": "user", "content": user_message}]
        kwargs = {}
        if config.tools_enabled and self._tools:
            kwargs["tools"] = self._tools

        try:
            response = client.messages.create(
                model=config.model,
                max_tokens=config.max_tokens,
                messages=messages,
                **kwargs,
            )
            text = response.content[0].text if response.content else ""
            return {
                "tier": self._current_tier.value,
                "response": text,
                "degraded": self._current_tier != CapabilityTier.FULL,
                "model": config.model,
            }
        except anthropic.APIStatusError as e:
            if e.status_code in (529, 503, 502):
                self._degrade(f"API overloaded: {e.status_code}")
                return self.respond(user_message)  # retry at lower tier
            raise
        except Exception as e:
            self._degrade(f"Unexpected error: {e}")
            return self.respond(user_message)

agent = TieredAgent()

for message in ["What is machine learning?", "Summarize this week's news.", "Hello"]:
    result = agent.respond(message)
    print(f"[Tier:{result['tier']}] {result['response'][:80]}...")
    if result.get("degraded"):
        print(f"  ⚠ Operating in degraded mode")
```

---

## Solution 2: Feature-Flag-Based Capability Degradation

Disable specific features individually (tool use, streaming, memory, extended context) rather than wholesale model switching.

```python
import anthropic
from dataclasses import dataclass, field
from typing import Optional, Any
from enum import Flag, auto

client = anthropic.Anthropic()

class Feature(Flag):
    TOOLS           = auto()
    STREAMING       = auto()
    MEMORY          = auto()
    EXTENDED_CONTEXT = auto()
    CITATIONS       = auto()
    ALL = TOOLS | STREAMING | MEMORY | EXTENDED_CONTEXT | CITATIONS

@dataclass
class FeatureState:
    feature: Feature
    enabled: bool
    disabled_reason: Optional[str] = None
    disabled_at: Optional[float] = None

class DegradedModeManager:
    def __init__(self):
        import time
        self._features: dict[Feature, FeatureState] = {
            f: FeatureState(feature=f, enabled=True)
            for f in [Feature.TOOLS, Feature.STREAMING, Feature.MEMORY,
                      Feature.EXTENDED_CONTEXT, Feature.CITATIONS]
        }
        self._disabled_count = 0

    def disable(self, feature: Feature, reason: str):
        import time
        for f in [Feature.TOOLS, Feature.STREAMING, Feature.MEMORY,
                  Feature.EXTENDED_CONTEXT, Feature.CITATIONS]:
            if feature & f:
                state = self._features[f]
                if state.enabled:
                    state.enabled = False
                    state.disabled_reason = reason
                    state.disabled_at = time.time()
                    self._disabled_count += 1
                    print(f"[Feature Disabled] {f.name}: {reason}")

    def is_enabled(self, feature: Feature) -> bool:
        for f in [Feature.TOOLS, Feature.STREAMING, Feature.MEMORY,
                  Feature.EXTENDED_CONTEXT, Feature.CITATIONS]:
            if feature & f:
                if not self._features[f].enabled:
                    return False
        return True

    def enabled_features(self) -> list[str]:
        return [f.name for f, state in self._features.items() if state.enabled]

    def status(self) -> dict:
        return {
            "enabled": [f.name for f, s in self._features.items() if s.enabled],
            "disabled": [
                {"feature": f.name, "reason": s.disabled_reason}
                for f, s in self._features.items() if not s.enabled
            ],
            "degradation_level": f"{self._disabled_count}/5 features disabled",
        }

class FeatureAwareAgent:
    def __init__(self, tools: list = None):
        self._tools = tools or []
        self._manager = DegradedModeManager()
        self._memory: list[str] = []

    def disable_feature(self, feature: Feature, reason: str):
        self._manager.disable(feature, reason)

    def respond(self, user_message: str) -> dict:
        messages = [{"role": "user", "content": user_message}]

        # Inject memory if available
        if self._manager.is_enabled(Feature.MEMORY) and self._memory:
            context = "Previous context: " + "; ".join(self._memory[-3:])
            messages.insert(0, {"role": "user", "content": context})
            messages.insert(1, {"role": "assistant", "content": "Understood."})

        # Limit context if extended context disabled
        max_tokens = 4096 if self._manager.is_enabled(Feature.EXTENDED_CONTEXT) else 512
        model = "claude-sonnet-4-6"

        kwargs: dict[str, Any] = {}
        if self._manager.is_enabled(Feature.TOOLS) and self._tools:
            kwargs["tools"] = self._tools

        response = client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages, **kwargs
        )
        text = response.content[0].text if response.content else ""

        # Store in memory
        if self._manager.is_enabled(Feature.MEMORY):
            self._memory.append(f"User: {user_message[:50]} → Agent: {text[:50]}")

        warnings = []
        if not self._manager.is_enabled(Feature.TOOLS):
            warnings.append("Tool use disabled — answering from knowledge only")
        if not self._manager.is_enabled(Feature.MEMORY):
            warnings.append("Memory disabled — no conversation history")
        if not self._manager.is_enabled(Feature.EXTENDED_CONTEXT):
            warnings.append("Extended context disabled — response length limited")

        return {
            "response": text,
            "warnings": warnings,
            "enabled_features": self._manager.enabled_features(),
        }

agent = FeatureAwareAgent()
print("--- Full capability ---")
result = agent.respond("What is Python?")
print(f"Response: {result['response'][:60]}...")
print(f"Features: {result['enabled_features']}")

# Simulate tool service failure
agent.disable_feature(Feature.TOOLS, "Tool service endpoint unreachable")
agent.disable_feature(Feature.MEMORY, "Redis connection failed")

print("\n--- Degraded mode ---")
result = agent.respond("Search for Python tutorials online")
print(f"Response: {result['response'][:80]}...")
for w in result['warnings']:
    print(f"  ⚠ {w}")
```

---

## Solution 3: Circuit-Breaker-Per-Dependency with Degraded Paths

Maintain a circuit breaker for each external dependency (model API, database, tool service) and define what the agent does when each is open.

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Any
from enum import Enum

client = anthropic.Anthropic()

class CircuitState(Enum):
    CLOSED = "closed"      # Normal — requests pass through
    OPEN = "open"          # Failed — requests blocked, use fallback
    HALF_OPEN = "half-open"  # Testing recovery

@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 60.0
    _state: CircuitState = CircuitState.CLOSED
    _failure_count: int = 0
    _last_failure: float = 0.0
    _success_count: int = 0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure > self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self):
        self._failure_count = 0
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            print(f"[Circuit {self.name}] HALF_OPEN → CLOSED (recovered)")

    def record_failure(self):
        self._failure_count += 1
        self._last_failure = time.time()
        if self._failure_count >= self.failure_threshold:
            if self._state != CircuitState.OPEN:
                print(f"[Circuit {self.name}] CLOSED → OPEN ({self._failure_count} failures)")
            self._state = CircuitState.OPEN

    def allow_request(self) -> bool:
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.HALF_OPEN:
            return True  # Let one through to test
        return False  # OPEN — block

class ResilientAgent:
    def __init__(self):
        self._circuits = {
            "primary-model": CircuitBreaker("primary-model", failure_threshold=2, recovery_timeout=30),
            "fallback-model": CircuitBreaker("fallback-model", failure_threshold=3, recovery_timeout=60),
            "tool-service":   CircuitBreaker("tool-service",   failure_threshold=2, recovery_timeout=20),
            "memory-service": CircuitBreaker("memory-service", failure_threshold=2, recovery_timeout=15),
        }
        self._static_cache = {
            "hello": "Hello! I'm here to help.",
            "help":  "I can answer questions, assist with code, and analyze information.",
        }

    def _model_call(self, circuit_name: str, model: str,
                    messages: list, max_tokens: int = 512) -> Optional[str]:
        cb = self._circuits[circuit_name]
        if not cb.allow_request():
            return None
        try:
            response = client.messages.create(
                model=model, max_tokens=max_tokens, messages=messages
            )
            cb.record_success()
            return response.content[0].text
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            cb.record_failure()
            print(f"[{circuit_name}] Failed: {e.__class__.__name__}")
            return None

    def respond(self, user_message: str) -> dict:
        messages = [{"role": "user", "content": user_message}]
        warnings = []
        model_used = None

        # Try primary model (opus)
        response = self._model_call("primary-model", "claude-opus-4-6", messages, 1024)
        if response:
            model_used = "claude-opus-4-6"
        else:
            warnings.append("Primary model unavailable — using fallback")
            # Try fallback (haiku)
            response = self._model_call("fallback-model", "claude-haiku-4-5-20251001", messages, 512)
            if response:
                model_used = "claude-haiku-4-5-20251001"
            else:
                warnings.append("Fallback model unavailable — using static cache")
                # Static cache fallback
                key = user_message.lower().strip().split()[0] if user_message else ""
                response = self._static_cache.get(key, "Service temporarily unavailable.")
                model_used = "static-cache"

        circuit_status = {
            name: cb.state.value for name, cb in self._circuits.items()
        }
        return {
            "response": response,
            "model_used": model_used,
            "degraded": len(warnings) > 0,
            "warnings": warnings,
            "circuit_states": circuit_status,
        }

agent = ResilientAgent()

# Simulate primary model failure
agent._circuits["primary-model"].record_failure()
agent._circuits["primary-model"].record_failure()

result = agent.respond("What is machine learning?")
print(f"Model used: {result['model_used']}")
print(f"Degraded: {result['degraded']}")
print(f"Response: {result['response'][:80]}...")
for w in result['warnings']:
    print(f"  ⚠ {w}")
print(f"Circuit states: {result['circuit_states']}")
```

---

## Solution 4: Read-Only Degraded Mode for Write-Heavy Agents

When write operations fail (database writes, API mutations), automatically switch to read-only mode — still useful for queries while protecting data integrity.

```python
import anthropic
from dataclasses import dataclass
from typing import Optional, Callable
from enum import Enum

client = anthropic.Anthropic()

class OperationMode(Enum):
    READ_WRITE = "read_write"
    READ_ONLY  = "read_only"
    QUERY_ONLY = "query_only"   # No writes, no external reads either

@dataclass
class OperationPolicy:
    mode: OperationMode
    allowed_operations: set[str]
    denied_operations: set[str]
    user_message: str

OPERATION_POLICIES = {
    OperationMode.READ_WRITE: OperationPolicy(
        mode=OperationMode.READ_WRITE,
        allowed_operations={"read", "write", "delete", "create", "update", "query"},
        denied_operations=set(),
        user_message="",
    ),
    OperationMode.READ_ONLY: OperationPolicy(
        mode=OperationMode.READ_ONLY,
        allowed_operations={"read", "query"},
        denied_operations={"write", "delete", "create", "update"},
        user_message="⚠ System is in read-only mode. Data modifications are temporarily unavailable.",
    ),
    OperationMode.QUERY_ONLY: OperationPolicy(
        mode=OperationMode.QUERY_ONLY,
        allowed_operations={"query"},
        denied_operations={"read", "write", "delete", "create", "update"},
        user_message="⚠ System is in restricted mode. Only analytical queries are available.",
    ),
}

class ReadOnlyDegradedAgent:
    def __init__(self):
        self._mode = OperationMode.READ_WRITE
        self._write_failure_count = 0
        self._read_failure_count = 0

    def set_mode(self, mode: OperationMode, reason: str):
        old = self._mode
        self._mode = mode
        print(f"[Mode Change] {old.value} → {mode.value}: {reason}")

    def _classify_intent(self, user_message: str) -> str:
        msg = user_message.lower()
        for write_kw in ["save", "create", "update", "delete", "remove", "write", "modify", "set", "add"]:
            if write_kw in msg:
                return "write"
        for read_kw in ["show", "list", "get", "find", "search", "query", "what", "how many"]:
            if read_kw in msg:
                return "read"
        return "query"

    def respond(self, user_message: str) -> dict:
        policy = OPERATION_POLICIES[self._mode]
        intent = self._classify_intent(user_message)

        if intent in policy.denied_operations:
            return {
                "response": (
                    f"{policy.user_message}\n\n"
                    f"Your request to '{user_message[:50]}' requires write access "
                    f"which is currently disabled. Please try again later or contact support."
                ),
                "mode": self._mode.value,
                "blocked": True,
                "operation": intent,
            }

        # Add mode context to system prompt
        mode_context = ""
        if self._mode != OperationMode.READ_WRITE:
            mode_context = (
                f"IMPORTANT: You are operating in {self._mode.value} mode. "
                f"Do NOT suggest or offer to perform write operations. "
                f"Only help with: {', '.join(policy.allowed_operations)}."
            )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=mode_context if mode_context else "You are a helpful assistant.",
            messages=[{"role": "user", "content": user_message}],
        )

        return {
            "response": response.content[0].text,
            "mode": self._mode.value,
            "blocked": False,
            "operation": intent,
        }

agent = ReadOnlyDegradedAgent()

# Normal operation
result = agent.respond("Show me all active users")
print(f"[{result['mode']}] Read query: OK")

result = agent.respond("Create a new user account for alice@example.com")
print(f"[{result['mode']}] Write: OK")

# Database write failure — degrade to read-only
agent.set_mode(OperationMode.READ_ONLY, "Database write replica unavailable")

result = agent.respond("Show me all active users")
print(f"[{result['mode']}] Read: OK")

result = agent.respond("Delete all inactive accounts")
print(f"[{result['mode']}] Write blocked: {result['blocked']}")
print(f"  Response: {result['response'][:100]}...")
```

---

## Solution 5: Stale Cache Serve with Freshness Disclosure

When live data is unavailable, serve the last known good response from cache with an explicit staleness notice.

```python
import anthropic
import time
import hashlib
from dataclasses import dataclass, field
from typing import Optional, Any

client = anthropic.Anthropic()

@dataclass
class CacheEntry:
    key: str
    response: str
    cached_at: float
    model: str
    ttl_seconds: float = 300.0

    @property
    def is_fresh(self) -> bool:
        return time.time() - self.cached_at < self.ttl_seconds

    @property
    def age_seconds(self) -> float:
        return time.time() - self.cached_at

    @property
    def staleness_warning(self) -> str:
        age = self.age_seconds
        if age < 60:
            return f"Response from {age:.0f} seconds ago"
        elif age < 3600:
            return f"Response from {age/60:.0f} minutes ago"
        else:
            return f"Response from {age/3600:.1f} hours ago — may be outdated"

class StaleCacheAgent:
    def __init__(self, live_ttl: float = 300, stale_ttl: float = 86400):
        self._cache: dict[str, CacheEntry] = {}
        self._live_ttl = live_ttl
        self._stale_ttl = stale_ttl
        self._live_available = True

    def _cache_key(self, messages: list) -> str:
        content = str(messages)
        return hashlib.md5(content.encode()).hexdigest()

    def _is_stale_acceptable(self, entry: CacheEntry) -> bool:
        return entry.age_seconds < self._stale_ttl

    def simulate_outage(self, down: bool):
        self._live_available = not down
        print(f"[Service] {'DOWN — serving stale cache' if down else 'UP — serving live responses'}")

    def respond(self, messages: list, model: str = "claude-haiku-4-5-20251001") -> dict:
        key = self._cache_key(messages)
        existing = self._cache.get(key)

        # If live is available and cache is fresh, try live
        if self._live_available:
            try:
                response = client.messages.create(
                    model=model, max_tokens=512, messages=messages
                )
                text = response.content[0].text
                self._cache[key] = CacheEntry(
                    key=key, response=text, cached_at=time.time(),
                    model=model, ttl_seconds=self._live_ttl
                )
                return {"response": text, "source": "live", "stale": False, "warning": None}
            except (anthropic.APIStatusError, anthropic.APIConnectionError):
                self._live_available = False
                print("[Outage detected — falling back to stale cache]")

        # Live unavailable — check stale cache
        if existing and self._is_stale_acceptable(existing):
            return {
                "response": existing.response,
                "source": "stale-cache",
                "stale": True,
                "warning": existing.staleness_warning,
                "cached_at": existing.cached_at,
            }

        # No usable cache entry
        return {
            "response": "Service temporarily unavailable. Please try again in a few minutes.",
            "source": "error",
            "stale": False,
            "warning": "No cached response available",
        }

agent = StaleCacheAgent(live_ttl=60, stale_ttl=3600)
messages = [{"role": "user", "content": "What is the current best practice for API authentication?"}]

# Normal call — populates cache
result = agent.respond(messages)
print(f"Live response: {result['response'][:60]}... [source={result['source']}]")

# Simulate outage
agent.simulate_outage(True)

# Returns stale cached response with warning
result = agent.respond(messages)
print(f"Stale response: {result['response'][:60]}...")
if result.get('warning'):
    print(f"  ⚠ {result['warning']}")
print(f"  Source: {result['source']}")
```

---

## Solution 6: Progressive Capability Restoration Monitor

After degrading, continuously probe the failed service in the background and automatically restore capabilities when it recovers.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

client = anthropic.AsyncAnthropic()

class ServiceStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"

@dataclass
class ServiceHealth:
    name: str
    status: ServiceStatus = ServiceStatus.HEALTHY
    last_check: float = field(default_factory=time.time)
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    recovery_threshold: int = 3  # successes needed to restore

    def mark_failure(self):
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        self.last_check = time.time()
        if self.consecutive_failures >= 3:
            self.status = ServiceStatus.DOWN
        elif self.consecutive_failures >= 1:
            self.status = ServiceStatus.DEGRADED

    def mark_success(self):
        self.consecutive_successes += 1
        self.consecutive_failures = 0
        self.last_check = time.time()
        if self.consecutive_successes >= self.recovery_threshold:
            if self.status != ServiceStatus.HEALTHY:
                print(f"[Recovery] {self.name} restored to HEALTHY")
            self.status = ServiceStatus.HEALTHY
        elif self.consecutive_successes >= 1:
            self.status = ServiceStatus.DEGRADED

class ProgressiveRestorationAgent:
    def __init__(self):
        self._services = {
            "model-api": ServiceHealth("model-api"),
            "tool-service": ServiceHealth("tool-service"),
        }
        self._probe_task: Optional[asyncio.Task] = None
        self._probe_interval = 10.0

    async def start_monitoring(self):
        self._probe_task = asyncio.create_task(self._probe_loop())

    async def stop_monitoring(self):
        if self._probe_task:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass

    async def _probe_loop(self):
        while True:
            await asyncio.sleep(self._probe_interval)
            await self._probe_all()

    async def _probe_all(self):
        for name, health in self._services.items():
            if health.status != ServiceStatus.HEALTHY:
                success = await self._probe_service(name)
                if success:
                    health.mark_success()
                    print(f"[Probe] {name}: success ({health.consecutive_successes}/{health.recovery_threshold})")
                else:
                    health.mark_failure()

    async def _probe_service(self, service_name: str) -> bool:
        if service_name == "model-api":
            try:
                await client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=5,
                    messages=[{"role": "user", "content": "ping"}]
                )
                return True
            except Exception:
                return False
        elif service_name == "tool-service":
            # Simulate tool health check
            return True  # In real impl: hit /health endpoint

    def mark_failed(self, service_name: str):
        if service_name in self._services:
            self._services[service_name].mark_failure()
            print(f"[Failure] {service_name}: {self._services[service_name].status.value}")

    async def respond(self, user_message: str) -> dict:
        model_health = self._services["model-api"]
        model = (
            "claude-opus-4-6" if model_health.status == ServiceStatus.HEALTHY
            else "claude-haiku-4-5-20251001" if model_health.status == ServiceStatus.DEGRADED
            else None
        )

        if model is None:
            return {
                "response": "Model API is currently down. Restoring in the background...",
                "degraded": True,
                "retry_in_seconds": self._probe_interval,
            }

        response = await client.messages.create(
            model=model, max_tokens=512,
            messages=[{"role": "user", "content": user_message}]
        )
        return {
            "response": response.content[0].text,
            "degraded": model_health.status != ServiceStatus.HEALTHY,
            "model": model,
            "service_status": {n: h.status.value for n, h in self._services.items()},
        }

async def main():
    agent = ProgressiveRestorationAgent()
    await agent.start_monitoring()

    # Normal operation
    result = await agent.respond("What is Python?")
    print(f"Healthy: {result['response'][:60]}...")
    print(f"Services: {result.get('service_status')}")

    # Simulate degradation
    agent.mark_failed("model-api")
    agent.mark_failed("model-api")

    result = await agent.respond("What is Python?")
    print(f"\nDegraded: {result['response'][:60]}...")
    print(f"Degraded mode: {result.get('degraded')}")

    await agent.stop_monitoring()

asyncio.run(main())
```

---

## Comparison

| Solution | Granularity | Automatic Recovery | User Transparency | Implementation Complexity | Best For |
|---|---|---|---|---|---|
| Capability-Tiered Fallback | Tier-level | Yes (timeout-based) | Partial | Low | General agents |
| Feature-Flag Degradation | Feature-level | No | Full | Medium | Feature-rich agents |
| Circuit Breaker Per Dependency | Dependency-level | Yes (half-open) | Partial | Medium | Multi-dependency agents |
| Read-Only Mode | Operation-level | No | Full | Low | Data-mutating agents |
| Stale Cache Serve | Response-level | No | Full (staleness warning) | Low | Query-heavy agents |
| Progressive Restoration | Service-level | Yes (background probes) | Partial | High | Production services |

**Recommended approach:** Start with Solution 1 (tiered fallback) as the foundation — it's simple and covers most failure scenarios. Add Solution 4 (read-only mode) if your agent mutates data, and Solution 6 (progressive restoration) in production to avoid manual recovery intervention.
