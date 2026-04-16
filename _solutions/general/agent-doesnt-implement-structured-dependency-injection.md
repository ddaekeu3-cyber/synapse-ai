---
layout: solution
title: "Agent Doesn't Implement Structured Dependency Injection"
category: general
description: "Use dependency injection to decouple agent components from their concrete dependencies — making the model client, memory store, and tool registry swappable without modifying agent logic, and testable without live API calls."
tags: [general, dependency-injection, architecture, testability, decoupling, python]
---

# Agent Doesn't Implement Structured Dependency Injection

Agents that instantiate `anthropic.Anthropic()` directly inside business logic are tightly coupled to a specific client, impossible to test without live API calls, and hard to swap for different models or environments. Dependency injection separates construction from use — the agent receives its dependencies rather than creating them.

## Option 1: Constructor Injection via Dataclass

```python
import anthropic
from dataclasses import dataclass, field

@dataclass
class AgentConfig:
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 512
    system: str = "You are a helpful assistant."

@dataclass
class Agent:
    client: anthropic.Anthropic
    config: AgentConfig = field(default_factory=AgentConfig)

    def respond(self, user_message: str) -> str:
        resp = self.client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=self.config.system,
            messages=[{"role": "user", "content": user_message}],
        )
        return resp.content[0].text

# Production wiring — done once at startup
def build_agent() -> Agent:
    client = anthropic.Anthropic()
    config = AgentConfig(model="claude-haiku-4-5-20251001", max_tokens=256)
    return Agent(client=client, config=config)

# Test wiring — swap client without touching Agent logic
class StubClient:
    def messages_create(self, **kwargs):
        return type("R", (), {"content": [type("C", (), {"text": "stub response"})()]})()
    @property
    def messages(self):
        return self

def build_test_agent() -> Agent:
    return Agent(client=StubClient(), config=AgentConfig())

agent = build_agent()
print(agent.respond("What is Python?")[:80])

# Expected Token Savings: Test agent uses stub — zero API calls in unit tests
# Environment: dataclass injection works with any Python 3.7+; no framework required
```

## Option 2: Protocol-Based Interface for Swappable Clients

```python
import anthropic
from typing import Protocol, runtime_checkable

@runtime_checkable
class MessageClient(Protocol):
    """Protocol that any message client must satisfy."""
    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict],
        system: str = "",
    ) -> object: ...

class AnthropicAdapter:
    """Wraps anthropic.Anthropic to satisfy MessageClient protocol."""
    def __init__(self):
        self._client = anthropic.Anthropic()

    def create(self, *, model, max_tokens, messages, system=""):
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system
        return self._client.messages.create(**kwargs)

class EchoAdapter:
    """Stub for testing — echoes input back."""
    def create(self, *, model, max_tokens, messages, system=""):
        user_text = messages[-1]["content"] if messages else ""
        return type("R", (), {
            "content": [type("C", (), {"text": f"[echo] {user_text[:40]}"})()]
        })()

class Agent:
    def __init__(self, client: MessageClient, model: str = "claude-haiku-4-5-20251001"):
        assert isinstance(client, MessageClient), "client must satisfy MessageClient protocol"
        self._client = client
        self._model = model

    def ask(self, question: str) -> str:
        resp = self._client.create(
            model=self._model,
            max_tokens=256,
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text

# Production
prod_agent = Agent(client=AnthropicAdapter())
print(prod_agent.ask("What is dependency injection?")[:80])

# Test — no API call
test_agent = Agent(client=EchoAdapter())
assert "[echo]" in test_agent.ask("hello")
print("Protocol injection ✓")

# Expected Token Savings: EchoAdapter in tests = zero tokens; swap adapters per environment
# Environment: runtime_checkable Protocol requires Python 3.8+; no extra dependencies
```

## Option 3: Service Locator / Registry Pattern

```python
import anthropic
from typing import Any, Callable, TypeVar

T = TypeVar("T")

class ServiceRegistry:
    """Central registry for all agent dependencies."""
    _registry: dict[str, Any] = {}
    _factories: dict[str, Callable] = {}

    @classmethod
    def register(cls, name: str, instance: Any):
        cls._registry[name] = instance

    @classmethod
    def register_factory(cls, name: str, factory: Callable):
        cls._factories[name] = factory

    @classmethod
    def get(cls, name: str) -> Any:
        if name in cls._registry:
            return cls._registry[name]
        if name in cls._factories:
            instance = cls._factories[name]()
            cls._registry[name] = instance
            return instance
        raise KeyError(f"No service registered for '{name}'")

    @classmethod
    def reset(cls):
        cls._registry.clear()
        cls._factories.clear()

# Register production dependencies
ServiceRegistry.register_factory("anthropic_client", anthropic.Anthropic)
ServiceRegistry.register("model", "claude-haiku-4-5-20251001")
ServiceRegistry.register("max_tokens", 256)

class Agent:
    def ask(self, question: str) -> str:
        client = ServiceRegistry.get("anthropic_client")
        model  = ServiceRegistry.get("model")
        max_tok = ServiceRegistry.get("max_tokens")
        resp = client.messages.create(
            model=model,
            max_tokens=max_tok,
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text

# Production usage
agent = Agent()
print(agent.ask("What is Python?")[:80])

# Test override — replace live client with stub before test
class _StubMessages:
    def create(self, **kwargs):
        return type("R", (), {"content": [type("C", (), {"text": "stub"})()]})()

class _StubClient:
    messages = _StubMessages()

ServiceRegistry.register("anthropic_client", _StubClient())
test_agent = Agent()
assert test_agent.ask("hi") == "stub"
print("Registry injection ✓")

ServiceRegistry.reset()  # clean up after test

# Expected Token Savings: Override registry in tests = zero API calls; lazy factories = no client until needed
# Environment: singleton registry works for single-process; use threading.local for thread isolation
```

## Option 4: Dependency Container with Async Support

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

@dataclass
class MemoryStore:
    """Simple in-memory store — swappable for Redis/SQLite."""
    _data: dict = field(default_factory=dict)

    def save(self, key: str, value: str):
        self._data[key] = value

    def load(self, key: str) -> str | None:
        return self._data.get(key)

@dataclass
class AgentDeps:
    """All dependencies in one container — injected as a unit."""
    client: anthropic.AsyncAnthropic
    memory: MemoryStore
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 256

class AsyncAgent:
    def __init__(self, deps: AgentDeps):
        self._deps = deps

    async def ask(self, question: str, session_id: str = "default") -> str:
        # Load prior context from memory
        prior = self._deps.memory.load(session_id) or ""
        messages = []
        if prior:
            messages.append({"role": "assistant", "content": f"[context: {prior[:100]}]"})
        messages.append({"role": "user", "content": question})

        async with self._deps.client.messages.stream(
            model=self._deps.model,
            max_tokens=self._deps.max_tokens,
            messages=messages,
        ) as stream:
            result = await stream.get_final_text()

        # Save response to memory
        self._deps.memory.save(session_id, result[:200])
        return result

def build_prod_deps() -> AgentDeps:
    return AgentDeps(
        client=anthropic.AsyncAnthropic(),
        memory=MemoryStore(),
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
    )

async def main():
    deps = build_prod_deps()
    agent = AsyncAgent(deps)
    r1 = await agent.ask("What is Python?", session_id="user-1")
    print(f"R1: {r1[:60]}")
    # Second call has prior context injected from memory
    r2 = await agent.ask("Give me one example.", session_id="user-1")
    print(f"R2: {r2[:60]}")

asyncio.run(main())

# Expected Token Savings: Swapping MemoryStore for a no-op in tests = isolated test sessions
# Environment: AsyncAnthropic + asyncio; deps container replaces N individual constructor args
```

## Option 5: Layered Injection with Middleware Chain

```python
import anthropic
import time
from typing import Callable

# Type alias for handler functions
Handler = Callable[[dict], dict]

def make_llm_handler(client: anthropic.Anthropic, model: str, max_tokens: int) -> Handler:
    """Core handler — calls the model."""
    def handle(request: dict) -> dict:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=request["messages"],
        )
        return {"text": resp.content[0].text, "usage": resp.usage.model_dump()}
    return handle

def logging_middleware(next_handler: Handler) -> Handler:
    """Logs every request/response."""
    def handle(request: dict) -> dict:
        print(f"  [LOG] -> {request['messages'][-1]['content'][:40]!r}")
        result = next_handler(request)
        print(f"  [LOG] <- {result['text'][:40]!r}")
        return result
    return handle

def timing_middleware(next_handler: Handler) -> Handler:
    """Injects latency into each response dict."""
    def handle(request: dict) -> dict:
        t0 = time.monotonic()
        result = next_handler(request)
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return result
    return handle

def validation_middleware(next_handler: Handler) -> Handler:
    """Validates request structure before passing downstream."""
    def handle(request: dict) -> dict:
        if not request.get("messages"):
            raise ValueError("request.messages must not be empty")
        for msg in request["messages"]:
            if "role" not in msg or "content" not in msg:
                raise ValueError(f"Invalid message: {msg}")
        return next_handler(request)
    return handle

def build_pipeline(client: anthropic.Anthropic, model: str, max_tokens: int) -> Handler:
    """Compose middleware chain via dependency injection."""
    core    = make_llm_handler(client, model, max_tokens)
    logged  = logging_middleware(core)
    timed   = timing_middleware(logged)
    pipeline = validation_middleware(timed)
    return pipeline

# Production
client = anthropic.Anthropic()
pipeline = build_pipeline(client, "claude-haiku-4-5-20251001", 128)
result = pipeline({"messages": [{"role": "user", "content": "What is asyncio?"}]})
print(f"Result: {result['text'][:60]}")
print(f"Latency: {result['latency_ms']}ms")

# Test — inject stub handler instead of real LLM
def stub_handler(request: dict) -> dict:
    return {"text": f"stub: {request['messages'][-1]['content'][:20]}", "usage": {}}

test_pipeline = validation_middleware(timing_middleware(stub_handler))
r = test_pipeline({"messages": [{"role": "user", "content": "hello"}]})
assert "stub" in r["text"]
print("Middleware injection ✓")

# Expected Token Savings: Stub handler in tests = zero API calls; middleware chain reused across environments
# Environment: pure Python; compose any subset of middleware layers per environment
```

## Option 6: Environment-Driven Wiring with SQLite Audit

```python
import anthropic
import os
import sqlite3
import time
from dataclasses import dataclass

DB = "di_audit.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS wiring_log (
            ts REAL, environment TEXT, component TEXT, impl TEXT
        )
    """)
    con.commit(); con.close()

@dataclass
class WiredAgent:
    client: object
    model: str
    max_tokens: int
    environment: str

    def ask(self, question: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text

class _StubMessages:
    def create(self, **kwargs):
        q = kwargs.get("messages", [{}])[-1].get("content", "")
        return type("R", (), {"content": [type("C", (), {"text": f"[test] {q[:30]}"})()]})()

class _StubClient:
    messages = _StubMessages()

def wire(environment: str | None = None) -> WiredAgent:
    """Build agent with dependencies driven by ENVIRONMENT env var."""
    init_db()
    env = environment or os.environ.get("ENVIRONMENT", "development")

    if env == "production":
        client = anthropic.Anthropic()
        model = "claude-sonnet-4-6"
        max_tokens = 1024
        client_impl = "anthropic.Anthropic (sonnet)"
    elif env == "staging":
        client = anthropic.Anthropic()
        model = "claude-haiku-4-5-20251001"
        max_tokens = 512
        client_impl = "anthropic.Anthropic (haiku)"
    else:  # development / test
        client = _StubClient()
        model = "stub"
        max_tokens = 0
        client_impl = "StubClient"

    # Log what was wired
    ts = time.time()
    con = sqlite3.connect(DB)
    for component, impl in [
        ("client", client_impl),
        ("model", model),
        ("max_tokens", str(max_tokens)),
    ]:
        con.execute("INSERT INTO wiring_log VALUES (?,?,?,?)", (ts, env, component, impl))
    con.commit(); con.close()

    print(f"[{env}] Wired: client={client_impl}, model={model}")
    return WiredAgent(client=client, model=model, max_tokens=max_tokens, environment=env)

def wiring_report():
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT environment, component, impl, COUNT(*) cnt
        FROM wiring_log GROUP BY environment, component, impl
        ORDER BY environment, component
    """).fetchall()
    con.close()
    print("\nWiring history:")
    for r in rows:
        print(f"  [{r[0]:12s}] {r[1]:12s} -> {r[2]} ({r[3]}x)")

# Test environment
test_agent = wire("development")
r = test_agent.ask("What is Python?")
assert "[test]" in r
print(f"Test response: {r}")

# Staging environment
staging_agent = wire("staging")
r2 = staging_agent.ask("What is asyncio?")
print(f"Staging response: {r2[:60]}")

wiring_report()

# Expected Token Savings: development/test uses stub = zero API calls; environment flag controls spend
# Environment: set ENVIRONMENT=production in prod; development is default for local/CI
```

## Comparison

| Option | Injection Style | Testable | Async | Config-Driven |
|--------|----------------|---------|-------|--------------|
| 1 — Constructor Dataclass | Constructor args | Yes | No | No |
| 2 — Protocol Interface | Protocol + adapter | Yes | No | No |
| 3 — Service Registry | Central registry | Yes | No | No |
| 4 — Async Container | Dep container | Yes | Yes | No |
| 5 — Middleware Chain | Handler composition | Yes | No | No |
| 6 — Environment Wiring | Env var + SQLite | Yes | No | Yes |
