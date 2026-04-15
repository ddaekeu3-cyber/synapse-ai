---
layout: solution
title: "Agent Doesn't Implement Graceful Degradation When Tools Are Unavailable"
category: general
description: "When external tools fail or become unavailable, agents crash, return empty responses, or enter error loops. Graceful degradation keeps the agent functional by falling back to cached data, alternative tools, or LLM-only responses."
tags: [general, resilience, graceful-degradation, fallback, availability, tool-failure]
---

# Agent Doesn't Implement Graceful Degradation When Tools Are Unavailable

## Problem

Agents that depend on external tools — search APIs, databases, calculators, web fetchers — become completely non-functional when those tools return errors or time out. Instead of providing partial value, the agent returns nothing or fails loudly. Users lose trust and tasks are abandoned when the agent should instead fall back gracefully to its best available capability.

## Why This Happens

Most agent implementations treat tool failures as terminal errors. The error propagates up without a fallback path. There is no definition of "what should the agent do if this specific tool is unavailable?" — so the implicit answer is "fail."

## Solutions

### Option 1: Tool Registry with Fallback Chain — Define primary + fallback per capability

```python
import anthropic
from dataclasses import dataclass, field
from typing import Callable, Any
import time

@dataclass
class ToolOption:
    name: str
    fn: Callable[..., Any]
    timeout: float = 5.0

@dataclass
class CapabilityDefinition:
    capability: str
    options: list[ToolOption]  # Ordered: first = primary, rest = fallbacks
    llm_fallback: str = ""     # System prompt for LLM-only fallback


class DegradingToolRegistry:
    def __init__(self):
        self._capabilities: dict[str, CapabilityDefinition] = {}

    def register(self, capability: CapabilityDefinition) -> None:
        self._capabilities[capability.capability] = capability

    def execute(self, capability: str, **kwargs) -> tuple[Any, str]:
        """Execute with fallback chain. Returns (result, source_name)."""
        cap = self._capabilities.get(capability)
        if not cap:
            return None, "not_registered"

        for option in cap.options:
            try:
                start = time.time()
                result = option.fn(**kwargs)
                if time.time() - start > option.timeout:
                    print(f"[WARN] {option.name} exceeded timeout")
                    continue
                if result is not None:
                    return result, option.name
            except Exception as e:
                print(f"[FALLBACK] {option.name} failed: {e}. Trying next option.")

        return None, "all_tools_failed"


# Simulated tool implementations
def live_weather_api(location: str) -> str:
    raise ConnectionError("Weather API is down")  # Simulate failure

def cached_weather(location: str) -> str | None:
    cache = {"London": "Partly cloudy, 15°C (cached 2h ago)", "Paris": "Sunny, 22°C (cached 2h ago)"}
    return cache.get(location)

def llm_weather_estimate(location: str) -> str:
    return f"Unable to fetch live weather for {location}. Based on typical patterns for this time of year, expect mild temperatures."


class DegradingAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.registry = DegradingToolRegistry()
        self._setup_capabilities()

    def _setup_capabilities(self) -> None:
        self.registry.register(CapabilityDefinition(
            capability="weather",
            options=[
                ToolOption("live_weather_api", live_weather_api),
                ToolOption("cached_weather", cached_weather),
                ToolOption("llm_estimate", llm_weather_estimate),
            ],
            llm_fallback="Provide a best-effort weather estimate based on general knowledge."
        ))

    def answer(self, question: str, location: str = "") -> str:
        # Try to get weather data with degradation
        weather_data, source = self.registry.execute("weather", location=location)

        if weather_data:
            degradation_note = f" [Source: {source}]" if source != "live_weather_api" else ""
            context = f"Weather info{degradation_note}: {weather_data}"
        else:
            context = "No weather data available. Answer based on general knowledge only."

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=context,
            messages=[{"role": "user", "content": question}]
        )
        return response.content[0].text


# Usage
agent = DegradingAgent()
answer = agent.answer("What should I wear today?", location="London")
print(answer)

# Expected Token Savings: Avoids full session failure; partial answers save re-run costs
# Environment: Any agent with external tool dependencies; production agents serving real users
```

### Option 2: Capability-Level Circuit Breaker — Disable failing tools temporarily, restore on success

```python
import anthropic
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Any

class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Tool disabled — fast-fail
    HALF_OPEN = "half_open" # Testing if tool recovered

@dataclass
class ToolCircuit:
    name: str
    failure_threshold: int = 3      # Failures before opening
    recovery_timeout: float = 60.0  # Seconds before trying again
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            print(f"[CIRCUIT] {self.name} OPEN after {self.failure_count} failures")

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def is_available(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True  # HALF_OPEN: allow one attempt


class ResilientToolSet:
    def __init__(self):
        self.circuits: dict[str, ToolCircuit] = {}
        self.tools: dict[str, Callable] = {}
        self.fallbacks: dict[str, Callable] = {}

    def register(self, name: str, fn: Callable, fallback: Callable | None = None) -> None:
        self.tools[name] = fn
        self.circuits[name] = ToolCircuit(name=name)
        if fallback:
            self.fallbacks[name] = fallback

    def call(self, name: str, **kwargs) -> tuple[Any, bool]:
        """Returns (result, is_degraded)."""
        circuit = self.circuits.get(name)
        fn = self.tools.get(name)
        fallback = self.fallbacks.get(name)

        if circuit and circuit.is_available() and fn:
            try:
                result = fn(**kwargs)
                circuit.record_success()
                return result, False
            except Exception as e:
                print(f"[TOOL] {name} error: {e}")
                circuit.record_failure()

        # Circuit open or tool failed — use fallback
        if fallback:
            try:
                result = fallback(**kwargs)
                return result, True  # is_degraded=True
            except Exception:
                pass

        return None, True


class CircuitBreakerAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.tools = ResilientToolSet()
        self._register_tools()

    def _register_tools(self) -> None:
        def search_live(query: str) -> str:
            raise TimeoutError("Search API timeout")  # Simulated failure

        def search_cache(query: str) -> str:
            return f"[CACHED] Results for '{query}': common programming answers available."

        def db_lookup(user_id: str) -> dict:
            raise ConnectionError("DB unavailable")

        def db_cache(user_id: str) -> dict:
            return {"user_id": user_id, "name": "Unknown (cached)", "tier": "free"}

        self.tools.register("search", search_live, fallback=search_cache)
        self.tools.register("user_db", db_lookup, fallback=db_cache)

    def handle(self, user_message: str, user_id: str = "u1") -> str:
        search_result, search_degraded = self.tools.call("search", query=user_message)
        user_data, user_degraded = self.tools.call("user_db", user_id=user_id)

        degradation_notes = []
        if search_degraded:
            degradation_notes.append("search results may be outdated")
        if user_degraded:
            degradation_notes.append("user profile from cache")

        system = "You are a helpful assistant."
        if degradation_notes:
            system += f" Note: {', '.join(degradation_notes)}. Provide best-effort answers."

        context_parts = []
        if search_result:
            context_parts.append(f"Search: {search_result}")
        if user_data:
            context_parts.append(f"User: {user_data}")

        messages = [{
            "role": "user",
            "content": f"{chr(10).join(context_parts)}\n\n{user_message}" if context_parts else user_message
        }]

        response = self.client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            system=system, messages=messages
        )
        return response.content[0].text


# Usage
agent = CircuitBreakerAgent()
# Circuit will open after 3 failures, then try fallbacks
for i in range(5):
    reply = agent.handle("How do I sort a dictionary in Python?", user_id=f"user-{i}")
    print(f"Turn {i+1}: {reply[:100]}...")

# Expected Token Savings: Prevents retry storms; circuit open = immediate fallback, no wasted retries
# Environment: Agents with rate-limited or unreliable external APIs; microservice architectures
```

### Option 3: Capability Negotiation — Agent declares what it can do given available tools

```python
import anthropic
import json
from dataclasses import dataclass, field
from enum import Enum

class CapabilityStatus(Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"

@dataclass
class Capability:
    name: str
    status: CapabilityStatus
    description: str
    degraded_description: str = ""

    def to_prompt_line(self) -> str:
        if self.status == CapabilityStatus.AVAILABLE:
            return f"✓ {self.name}: {self.description}"
        elif self.status == CapabilityStatus.DEGRADED:
            return f"~ {self.name}: {self.degraded_description} (degraded)"
        else:
            return f"✗ {self.name}: unavailable — do NOT attempt"


class CapabilityNegotiatingAgent:
    """Agent that tells the LLM exactly what it can and cannot do."""

    def __init__(self):
        self.client = anthropic.Anthropic()
        self.capabilities: list[Capability] = []

    def register_capability(
        self,
        name: str,
        check_fn,
        description: str,
        degraded_description: str = "",
    ) -> None:
        try:
            result = check_fn()
            if result:
                status = CapabilityStatus.AVAILABLE
            else:
                status = CapabilityStatus.DEGRADED
        except Exception:
            status = CapabilityStatus.UNAVAILABLE

        self.capabilities.append(Capability(
            name=name,
            status=status,
            description=description,
            degraded_description=degraded_description,
        ))

    def capability_system_prompt(self) -> str:
        lines = ["You are a helpful assistant. Your current capabilities:"]
        for cap in self.capabilities:
            lines.append(f"  {cap.to_prompt_line()}")
        lines.append("")
        lines.append("Work within these constraints. For unavailable capabilities, explain what you cannot do and offer alternatives.")
        return "\n".join(lines)

    def chat(self, user_message: str) -> str:
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=self.capability_system_prompt(),
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text


# Setup: probe each capability
agent = CapabilityNegotiatingAgent()

# Simulate: search is down, calculator works, DB is degraded
agent.register_capability(
    "web_search",
    lambda: (_ for _ in ()).throw(ConnectionError("Search down")),
    "Search the web for current information",
    ""
)
agent.register_capability(
    "calculator",
    lambda: True,  # Works
    "Perform mathematical calculations",
)
agent.register_capability(
    "user_database",
    lambda: False,  # Degraded (returns False)
    "Look up user account details",
    "Return cached/approximate user data only"
)
agent.register_capability(
    "file_system",
    lambda: True,
    "Read and write local files",
)

print("System prompt:")
print(agent.capability_system_prompt())
print()

reply = agent.chat("Can you search for today's news and show my account balance?")
print("Agent:", reply)

# Expected Token Savings: Prevents wasted tool-call attempts; LLM self-limits based on declared capabilities
# Environment: Multi-capability agents, customer service bots, assistants with variable tool availability
```

### Option 4: Soft Dependency Graph — Mark tools as required vs optional per request type

```python
import anthropic
from dataclasses import dataclass, field
from typing import Callable, Any

@dataclass
class ToolDependency:
    name: str
    fn: Callable
    required: bool = False      # If True, request fails without it. If False, degrade gracefully.
    provides: str = ""          # What context key this tool fills

@dataclass
class ToolExecutionPlan:
    available_context: dict[str, Any]
    missing_required: list[str]
    missing_optional: list[str]

    @property
    def can_proceed(self) -> bool:
        return len(self.missing_required) == 0


class SoftDependencyAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()

    def execute_dependencies(self, deps: list[ToolDependency], **kwargs) -> ToolExecutionPlan:
        context: dict[str, Any] = {}
        missing_required: list[str] = []
        missing_optional: list[str] = []

        for dep in deps:
            try:
                result = dep.fn(**{k: v for k, v in kwargs.items()})
                if result is not None:
                    context[dep.provides or dep.name] = result
                else:
                    if dep.required:
                        missing_required.append(dep.name)
                    else:
                        missing_optional.append(dep.name)
            except Exception as e:
                print(f"[DEP] {dep.name} failed: {e}")
                if dep.required:
                    missing_required.append(dep.name)
                else:
                    missing_optional.append(dep.name)

        return ToolExecutionPlan(
            available_context=context,
            missing_required=missing_required,
            missing_optional=missing_optional,
        )

    def answer_question(self, question: str, deps: list[ToolDependency], **kwargs) -> str:
        plan = self.execute_dependencies(deps, **kwargs)

        if not plan.can_proceed:
            return (
                f"Cannot answer this question because required tools are unavailable: "
                f"{', '.join(plan.missing_required)}. Please try again later."
            )

        # Build context string from available data
        context_parts = [f"{k}: {v}" for k, v in plan.available_context.items()]

        degradation_notice = ""
        if plan.missing_optional:
            degradation_notice = (
                f"\nNote: The following optional data sources are unavailable: "
                f"{', '.join(plan.missing_optional)}. Answer may be incomplete."
            )

        full_context = "\n".join(context_parts) + degradation_notice

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=f"Answer using this context:\n{full_context}" if full_context else "Answer based on your knowledge.",
            messages=[{"role": "user", "content": question}]
        )
        return response.content[0].text


# Usage
agent = SoftDependencyAgent()

def get_product_info(product_id: str) -> dict:
    return {"id": product_id, "name": "Widget Pro", "price": 49.99, "stock": 12}

def get_user_history(product_id: str) -> list:
    raise ConnectionError("Recommendation DB offline")  # Optional — fails gracefully

def get_reviews(product_id: str) -> list:
    raise TimeoutError("Review API timeout")  # Optional — fails gracefully

deps = [
    ToolDependency("product_db", get_product_info, required=True, provides="product"),
    ToolDependency("user_history", get_user_history, required=False, provides="purchase_history"),
    ToolDependency("review_api", get_reviews, required=False, provides="reviews"),
]

reply = agent.answer_question(
    "Should I buy this product? Is it in stock?",
    deps=deps,
    product_id="prod-123"
)
print(reply)

# Expected Token Savings: Avoids retries on optional failures; proceeds with partial data
# Environment: E-commerce agents, recommendation systems, any workflow with optional enrichment data
```

### Option 5: Cached Degraded Mode — Serve stale data with transparency when live tools fail

```python
import anthropic
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

CACHE_DB = Path("/tmp/degraded_cache.db")

@dataclass
class CachedResult:
    data: Any
    cached_at: float
    source: str

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.cached_at) / 60

    @property
    def staleness_label(self) -> str:
        mins = self.age_minutes
        if mins < 5:
            return "fresh"
        if mins < 60:
            return f"{mins:.0f} min old"
        return f"{mins/60:.1f} hr old"


class StaleCacheAgent:
    CACHE_TTL = 3600  # 1 hour — serve stale within this window

    def __init__(self):
        self.client = anthropic.Anthropic()
        self._init_cache()

    def _init_cache(self) -> None:
        with sqlite3.connect(CACHE_DB) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tool_cache (
                    tool_key TEXT PRIMARY KEY,
                    result_json TEXT,
                    cached_at REAL
                )
            """)

    def _cache_get(self, key: str) -> CachedResult | None:
        with sqlite3.connect(CACHE_DB) as conn:
            row = conn.execute(
                "SELECT result_json, cached_at FROM tool_cache WHERE tool_key = ?",
                (key,)
            ).fetchone()
        if row and (time.time() - row[1]) < self.CACHE_TTL:
            return CachedResult(
                data=json.loads(row[0]),
                cached_at=row[1],
                source="cache"
            )
        return None

    def _cache_set(self, key: str, result: Any) -> None:
        with sqlite3.connect(CACHE_DB) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tool_cache VALUES (?, ?, ?)",
                (key, json.dumps(result), time.time())
            )

    def call_with_cache(self, tool_name: str, fn, cache_key: str, **kwargs) -> tuple[Any, str]:
        """Call tool; on failure, serve from cache; label source."""
        try:
            result = fn(**kwargs)
            self._cache_set(cache_key, result)
            return result, "live"
        except Exception as e:
            print(f"[DEGRADE] {tool_name} failed: {e}")
            cached = self._cache_get(cache_key)
            if cached:
                return cached.data, f"stale ({cached.staleness_label})"
            return None, "unavailable"

    def answer(self, question: str) -> str:
        # Simulate live tools failing
        def get_stock_price(symbol: str) -> dict:
            raise ConnectionError("Market data API offline")

        price_data, source = self.call_with_cache(
            "stock_price",
            lambda **kw: get_stock_price(**kw),
            cache_key="stock:AAPL",
            symbol="AAPL"
        )

        if price_data:
            context = f"AAPL stock data [{source}]: {price_data}"
        else:
            context = "Real-time market data unavailable. Answer based on general knowledge only."

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=context,
            messages=[{"role": "user", "content": question}]
        )
        return response.content[0].text


# Usage
agent = StaleCacheAgent()
reply = agent.answer("What is Apple's stock doing today?")
print(reply)

# Expected Token Savings: Prevents re-generation from scratch; stale answers often 80% as useful
# Environment: Financial agents, news bots, any agent serving time-sensitive data
```

### Option 6: LLM-Only Fallback Mode — Switch to pure reasoning when all tools fail

```python
import anthropic
from dataclasses import dataclass, field
from enum import Enum

class AgentMode(Enum):
    FULL = "full"           # All tools available
    DEGRADED = "degraded"   # Some tools available
    KNOWLEDGE_ONLY = "knowledge_only"  # No tools — LLM knowledge only


@dataclass
class ModeAwareAgent:
    available_tools: list[str] = field(default_factory=list)
    failed_tools: list[str] = field(default_factory=list)

    @property
    def mode(self) -> AgentMode:
        if not self.failed_tools:
            return AgentMode.FULL
        if self.available_tools:
            return AgentMode.DEGRADED
        return AgentMode.KNOWLEDGE_ONLY

    def system_prompt(self) -> str:
        if self.mode == AgentMode.FULL:
            return (
                "You are a fully capable assistant with access to: "
                + ", ".join(self.available_tools)
                + ". Use tools as needed."
            )
        elif self.mode == AgentMode.DEGRADED:
            available = ", ".join(self.available_tools)
            failed = ", ".join(self.failed_tools)
            return (
                f"You are operating in DEGRADED MODE.\n"
                f"Available tools: {available}\n"
                f"Unavailable tools: {failed}\n"
                f"Do your best with available tools. "
                f"Clearly note when information may be incomplete due to tool unavailability."
            )
        else:
            failed = ", ".join(self.failed_tools)
            return (
                f"You are operating in KNOWLEDGE-ONLY MODE.\n"
                f"All external tools are currently unavailable: {failed}\n"
                f"Answer based solely on your training knowledge. "
                f"Clearly state that you cannot access live data, and note your knowledge cutoff. "
                f"Suggest that the user retry when tools are available."
            )


class FallbackModeAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()

    def _probe_tools(self, tool_fns: dict) -> tuple[list[str], list[str]]:
        available, failed = [], []
        for name, fn in tool_fns.items():
            try:
                fn()
                available.append(name)
            except Exception:
                failed.append(name)
        return available, failed

    def run(self, user_message: str, tool_fns: dict) -> tuple[str, AgentMode]:
        available, failed = self._probe_tools(tool_fns)

        config = ModeAwareAgent(
            available_tools=available,
            failed_tools=failed,
        )

        if config.mode != AgentMode.FULL:
            print(f"[MODE] Operating in {config.mode.value} mode. "
                  f"Failed: {failed}. Available: {available}")

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=config.system_prompt(),
            messages=[{"role": "user", "content": user_message}]
        )

        return response.content[0].text, config.mode


# Usage
agent = FallbackModeAgent()

tool_fns = {
    "web_search": lambda: (_ for _ in ()).throw(ConnectionError("offline")),
    "calculator": lambda: True,
    "file_reader": lambda: (_ for _ in ()).throw(PermissionError("no access")),
}

reply, mode = agent.run("What are the latest Python features and what is 2^10?", tool_fns=tool_fns)
print(f"[{mode.value.upper()}] {reply}")

# Expected Token Savings: No wasted tool calls in knowledge-only mode; transparent to user
# Environment: Any agent deployment; ensures zero silent failures across all operating conditions
```

## Comparison

| Option | Recovery Strategy | State Persistence | User Transparency | Complexity |
|--------|-----------------|-------------------|------------------|------------|
| Fallback Chain | Priority ordering | None | Low | Low |
| Circuit Breaker | Auto-disable + restore | In-memory | Medium | Medium |
| Capability Negotiation | LLM self-limits | None | High | Low |
| Soft Dependencies | Required vs optional | None | Medium | Low |
| Stale Cache | Serve TTL-bounded old data | SQLite | High | Medium |
| LLM-Only Fallback | Pure reasoning mode | None | High | Low |
