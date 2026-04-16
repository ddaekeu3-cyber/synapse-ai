---
title: "Agent Doesn't Implement Dependency Injection for Testable Tool Wiring"
description: "AI agents that hard-code tool dependencies inside their logic are difficult to test, mock, and swap. Dependency injection decouples the agent from its concrete tools so unit tests can substitute fakes, integration tests can use stubs, and production wires up real implementations without changing agent code."
date: 2025-02-02
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-dependency-injection-for-testable-tool-wiring
tags:
  - dependency-injection
  - testability
  - tool-wiring
  - architecture
  - mocking
  - reliability
symptoms:
  - "Agent tests require a live database, LLM API, or external service to run"
  - "Swapping one tool implementation requires editing agent internals"
  - "Integration tests are slow because they cannot mock expensive tool calls"
  - "Bug reproduction is hard because the agent always uses the same real-world state"
  - "Multiple environments (dev/staging/prod) require different binaries instead of different wiring"
---

## Problem

When an agent class directly instantiates its tools (`self.db = PostgresClient()`, `self.llm = OpenAIClient()`), the agent becomes inseparable from those concrete implementations. Every test that exercises the agent must bring up real infrastructure, making tests slow, flaky, and hard to parallelize.

Dependency injection (DI) inverts this: the agent receives its tools as constructor arguments. The caller — a test, a factory, or a DI container — decides which implementations to provide. The agent's logic is unchanged whether it receives a real database client or an in-memory fake.

---

## Solution 1: Constructor Injection with Protocol Interfaces

Define tool interfaces as Python `Protocol`s. The agent accepts any object that satisfies the protocol, enabling easy substitution in tests.

```python
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class WebSearchTool(Protocol):
    async def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        ...


@runtime_checkable
class DatabaseTool(Protocol):
    async def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        ...

    async def execute(self, sql: str, params: tuple = ()) -> int:
        ...


@runtime_checkable
class LLMTool(Protocol):
    async def complete(self, prompt: str, **kwargs) -> str:
        ...


class ResearchAgent:
    """
    All tools injected via constructor — no concrete imports at module level.

    Usage (production):
        agent = ResearchAgent(
            search=RealWebSearch(api_key=...),
            db=PostgresClient(dsn=...),
            llm=OpenAIClient(model="gpt-4o"),
        )

    Usage (test):
        agent = ResearchAgent(
            search=FakeWebSearch(results=[...]),
            db=InMemoryDatabase(),
            llm=FakeLLM(responses=["answer1", "answer2"]),
        )
    """

    def __init__(self, search: WebSearchTool, db: DatabaseTool, llm: LLMTool):
        self._search = search
        self._db = db
        self._llm = llm

    async def research(self, topic: str) -> str:
        # Agent logic is identical regardless of which implementations are injected
        raw_results = await self._search.search(topic, max_results=5)
        context = "\n".join(r.get("snippet", "") for r in raw_results)
        prompt = f"Summarise this research on '{topic}':\n{context}"
        summary = await self._llm.complete(prompt)
        await self._db.execute(
            "INSERT INTO research_cache(topic, summary) VALUES(?,?)",
            (topic, summary),
        )
        return summary


# ── Fakes for testing ────────────────────────────────────────────────────────

class FakeWebSearch:
    def __init__(self, results: List[Dict[str, Any]] = None):
        self._results = results or []
        self.calls: List[str] = []

    async def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        self.calls.append(query)
        return self._results[:max_results]


class InMemoryDatabase:
    def __init__(self):
        self._rows: List[tuple] = []
        self.queries: List[str] = []

    async def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        self.queries.append(sql)
        return []

    async def execute(self, sql: str, params: tuple = ()) -> int:
        self._rows.append(params)
        return 1


class FakeLLM:
    def __init__(self, responses: List[str] = None):
        self._queue = list(responses or ["default response"])
        self.prompts: List[str] = []

    async def complete(self, prompt: str, **kwargs) -> str:
        self.prompts.append(prompt)
        return self._queue.pop(0) if self._queue else "fallback response"
```

---

## Solution 2: Tool Registry with Named Bindings

A lightweight registry maps string names to tool instances. The agent looks up tools by name, allowing runtime reconfiguration without code changes.

```python
from typing import Any, Callable, Dict, Optional, Type


class ToolRegistry:
    """
    Named tool registry with lazy initialisation.

    Usage:
        registry = ToolRegistry()
        registry.bind("search", lambda: RealWebSearch(api_key=os.environ["SEARCH_KEY"]))
        registry.bind("db", lambda: PostgresClient(dsn=os.environ["DATABASE_URL"]))

        # In tests:
        registry.override("search", lambda: FakeWebSearch())
        registry.override("db", lambda: InMemoryDatabase())

        agent = ResearchAgentV2(registry)
    """

    def __init__(self):
        self._factories: Dict[str, Callable] = {}
        self._instances: Dict[str, Any] = {}

    def bind(self, name: str, factory: Callable):
        """Register a factory (lazy — not called until first resolve)."""
        self._factories[name] = factory
        self._instances.pop(name, None)  # clear any cached instance

    def override(self, name: str, factory: Callable):
        """Override a binding (useful in tests)."""
        self.bind(name, factory)

    def resolve(self, name: str) -> Any:
        if name not in self._instances:
            if name not in self._factories:
                raise KeyError(f"No binding for tool '{name}'")
            self._instances[name] = self._factories[name]()
        return self._instances[name]

    def reset(self):
        self._instances.clear()


class ResearchAgentV2:
    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    @property
    def _search(self) -> WebSearchTool:
        return self._registry.resolve("search")

    @property
    def _db(self) -> DatabaseTool:
        return self._registry.resolve("db")

    @property
    def _llm(self) -> LLMTool:
        return self._registry.resolve("llm")

    async def research(self, topic: str) -> str:
        results = await self._search.search(topic)
        context = "\n".join(r.get("snippet", "") for r in results)
        summary = await self._llm.complete(f"Summarise: {topic}\n{context}")
        await self._db.execute(
            "INSERT INTO cache(topic, summary) VALUES(?,?)", (topic, summary)
        )
        return summary
```

---

## Solution 3: Dataclass-Based Wiring Container

Collect all tool bindings in a single dataclass. Factory functions create the appropriate wiring for each environment. Tests supply a test wiring; production code supplies a prod wiring.

```python
from dataclasses import dataclass
from typing import Optional
import os


@dataclass
class AgentWiring:
    """All dependencies for a ResearchAgent in one place."""
    search: WebSearchTool
    db: DatabaseTool
    llm: LLMTool


def production_wiring() -> AgentWiring:
    """Wire up real implementations from environment."""
    from real_tools import RealWebSearch, PostgresClient, OpenAIClient
    return AgentWiring(
        search=RealWebSearch(api_key=os.environ["SEARCH_API_KEY"]),
        db=PostgresClient(dsn=os.environ["DATABASE_URL"]),
        llm=OpenAIClient(model=os.environ.get("MODEL", "gpt-4o")),
    )


def test_wiring(
    search_results=None,
    db_rows=None,
    llm_responses=None,
) -> AgentWiring:
    """Wire up fakes for testing."""
    return AgentWiring(
        search=FakeWebSearch(results=search_results or []),
        db=InMemoryDatabase(),
        llm=FakeLLM(responses=llm_responses or ["test summary"]),
    )


def staging_wiring() -> AgentWiring:
    """Wire real LLM but stubbed external services."""
    from real_tools import OpenAIClient
    return AgentWiring(
        search=FakeWebSearch(results=[{"snippet": "staging data"}]),
        db=InMemoryDatabase(),
        llm=OpenAIClient(model="gpt-4o-mini"),
    )


class ResearchAgentV3:
    def __init__(self, wiring: AgentWiring):
        self._w = wiring

    async def research(self, topic: str) -> str:
        results = await self._w.search.search(topic)
        context = "\n".join(r.get("snippet", "") for r in results)
        summary = await self._w.llm.complete(f"Summarise: {topic}\n{context}")
        await self._w.db.execute(
            "INSERT INTO cache(topic, summary) VALUES(?,?)", (topic, summary)
        )
        return summary
```

---

## Solution 4: Decorator-Based Tool Injection

A `@inject` decorator reads tool requirements from function annotations and resolves them from a module-level registry. Useful for functional-style agents that don't want class boilerplate.

```python
import asyncio
import functools
import inspect
from typing import Any, Callable, TypeVar

_REGISTRY: Dict[type, Any] = {}


def provide(interface: type, instance: Any):
    """Register a concrete instance for a Protocol/ABC interface."""
    _REGISTRY[interface] = instance


def inject(fn: Callable) -> Callable:
    """
    Decorator: resolves parameters whose type is registered in _REGISTRY.

    Usage:
        @inject
        async def research(topic: str, search: WebSearchTool, llm: LLMTool) -> str:
            results = await search.search(topic)
            return await llm.complete(str(results))

        # In tests:
        provide(WebSearchTool, FakeWebSearch())
        provide(LLMTool, FakeLLM(["answer"]))
        result = await research("climate change")
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        for name, param in sig.parameters.items():
            if name not in bound.arguments:
                annotation = param.annotation
                if annotation in _REGISTRY:
                    kwargs[name] = _REGISTRY[annotation]
        return await fn(*args, **kwargs)

    return wrapper


@inject
async def research_fn(topic: str, search: WebSearchTool, llm: LLMTool) -> str:
    results = await search.search(topic)
    context = "\n".join(r.get("snippet", "") for r in results)
    return await llm.complete(f"Summarise: {topic}\n{context}")
```

---

## Solution 5: Scoped Dependency Container (Per-Request Isolation)

Create a new container scope per request. Each scope can override bindings for that request without affecting other concurrent requests. Useful for tenant-specific tool configurations.

```python
import asyncio
import contextvars
from typing import Any, Callable, Dict, Optional

_scope_var: contextvars.ContextVar[Optional["DependencyScope"]] = \
    contextvars.ContextVar("_scope", default=None)


class DependencyScope:
    """
    Per-request dependency scope.
    Falls back to the global registry for bindings not overridden in this scope.

    Usage:
        async def handle_request(tenant_id: str):
            async with DependencyScope() as scope:
                scope.override("db", lambda: TenantDB(tenant_id))
                agent = ResearchAgentV2(ScopeAdapter(scope))
                return await agent.research("topic")
    """

    def __init__(self, parent: Optional["DependencyScope"] = None,
                 global_registry: Optional[ToolRegistry] = None):
        self._parent = parent
        self._global = global_registry
        self._local: Dict[str, Any] = {}
        self._factories: Dict[str, Callable] = {}

    def override(self, name: str, factory: Callable):
        self._factories[name] = factory
        self._local.pop(name, None)

    def resolve(self, name: str) -> Any:
        if name not in self._local:
            if name in self._factories:
                self._local[name] = self._factories[name]()
            elif self._global:
                return self._global.resolve(name)
            else:
                raise KeyError(f"No binding for '{name}' in scope")
        return self._local[name]

    async def __aenter__(self):
        self._token = _scope_var.set(self)
        return self

    async def __aexit__(self, *_):
        _scope_var.reset(self._token)
        self._local.clear()


def current_scope() -> Optional[DependencyScope]:
    return _scope_var.get()


class ScopeAdapter:
    """Adapts DependencyScope to the ToolRegistry interface."""
    def __init__(self, scope: DependencyScope):
        self._scope = scope

    def resolve(self, name: str) -> Any:
        return self._scope.resolve(name)
```

---

## Solution 6: Agent Factory with Environment-Aware Wiring

Central factory that reads environment config and assembles the correct agent wiring. All environment-specific logic lives here, not scattered across agent classes.

```python
import os
from enum import Enum
from typing import Optional


class Environment(Enum):
    TEST = "test"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class AgentFactory:
    """
    Creates fully-wired agents for any environment.

    Usage:
        agent = AgentFactory.create(Environment.PRODUCTION)
        result = await agent.research("topic")

        # In tests:
        agent = AgentFactory.create(Environment.TEST,
                                     llm_responses=["expected answer"])
        result = await agent.research("topic")
        assert result == "expected answer"
    """

    @classmethod
    def create(
        cls,
        env: Environment = Environment.PRODUCTION,
        llm_responses: Optional[list] = None,
        search_results: Optional[list] = None,
    ) -> ResearchAgentV3:
        if env == Environment.TEST:
            wiring = test_wiring(
                llm_responses=llm_responses,
                search_results=search_results,
            )
        elif env == Environment.DEVELOPMENT:
            wiring = test_wiring(
                llm_responses=llm_responses or ["dev response"],
            )
        elif env == Environment.STAGING:
            wiring = staging_wiring()
        else:
            wiring = production_wiring()
        return ResearchAgentV3(wiring)

    @classmethod
    def from_env(cls) -> ResearchAgentV3:
        env_name = os.environ.get("AGENT_ENV", "production").lower()
        env = Environment(env_name)
        return cls.create(env)
```

---

## Comparison

| Approach | Boilerplate | Test Ergonomics | Runtime Reconfiguration |
|---|---|---|---|
| **Constructor Injection + Protocol** | Low | Excellent | Requires rebuild |
| **Tool Registry** | Medium | Good | Yes (override at runtime) |
| **Dataclass Wiring** | Low | Excellent | Via factory function |
| **Decorator @inject** | Minimal | Good (module-level) | Yes (provide()) |
| **Scoped Container** | Medium | Good | Yes (per-request) |
| **Agent Factory** | Low | Excellent | Via Environment enum |

**Recommendation**: use constructor injection + Protocol for simple agents; add the Agent Factory pattern as the single place where environment-specific wiring lives. The scoped container pays off in multi-tenant deployments where each request needs different credentials.
