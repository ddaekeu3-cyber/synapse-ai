---
title: "Agent Doesn't Implement Async Dependency Injection"
description: "Hard-coding clients, database connections, and service references inside agent functions makes them untestable, difficult to swap, and impossible to mock. Async dependency injection provides dependencies through the call stack, enabling clean testing, hot-swapping, and per-request configuration."
difficulty: intermediate
category: concurrency
tags: [dependency-injection, async, testing, architecture, clients, context-propagation]
---

## Problem

Agent functions that instantiate their own dependencies — creating `AsyncAnthropic()` inline, calling `redis.from_url()` inside handlers, opening database connections per call — cannot be tested without hitting real APIs, cannot be swapped for cheaper alternatives, and cannot be configured differently per request. Async dependency injection solves this by passing dependencies down the call stack instead of constructing them at the point of use.

```python
# BAD: hard-coded dependencies — impossible to test or configure
async def handle_request(prompt: str) -> str:
    client = AsyncAnthropic()          # always real Anthropic
    cache = redis.from_url("redis://localhost")  # always local Redis
    result = await client.messages.create(...)
    await cache.set(prompt, result)
    return result
```

## Solution 1: Dependency Container with Async Lifecycle

A container holds shared async dependencies, initialized once and injected everywhere.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic
from typing import Any

@dataclass
class AgentDependencies:
    """Container for all async dependencies. Initialize once per process."""
    anthropic_client: AsyncAnthropic
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1024
    # Add more: db_pool, redis_client, http_session, etc.
    _metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    async def create(
        cls,
        model: str = "claude-haiku-4-5-20251001",
        **kwargs
    ) -> "AgentDependencies":
        client = AsyncAnthropic()
        return cls(anthropic_client=client, model=model, **kwargs)

    async def close(self):
        await self.anthropic_client.close()

async def call_model(deps: AgentDependencies, prompt: str) -> str:
    response = await deps.anthropic_client.messages.create(
        model=deps.model,
        max_tokens=deps.max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text if response.content else ""

async def summarize(deps: AgentDependencies, text: str) -> str:
    return await call_model(deps, f"Summarize in one sentence: {text}")

async def analyze(deps: AgentDependencies, text: str) -> str:
    summary = await summarize(deps, text)
    return await call_model(deps, f"What are the key insights from: {summary}")

async def main():
    # Production: real dependencies
    deps = await AgentDependencies.create(model="claude-haiku-4-5-20251001")
    try:
        result = await analyze(deps, "Async dependency injection improves testability and flexibility.")
        print(result[:200])
    finally:
        await deps.close()

    # Test: swap dependencies trivially
    class MockClient:
        class messages:
            @staticmethod
            async def create(**kwargs):
                class Msg:
                    content = [type("C", (), {"text": "mock response"})()]
                    usage = type("U", (), {"output_tokens": 5})()
                return Msg()

    mock_deps = AgentDependencies(anthropic_client=MockClient(), model="mock")  # type: ignore
    result = await analyze(mock_deps, "test input")
    print(f"Mock result: {result}")

asyncio.run(main())
```

## Solution 2: Context Variable Injection (asyncio.contextvars)

Use `contextvars.ContextVar` to propagate dependencies through async call chains without threading them through every function signature.

```python
import asyncio
import contextvars
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import TypeVar, Callable, Any

T = TypeVar("T")

# Context variables — set once per request, readable anywhere in the async chain
_current_client: contextvars.ContextVar[AsyncAnthropic] = contextvars.ContextVar("client")
_current_model: contextvars.ContextVar[str] = contextvars.ContextVar("model", default="claude-haiku-4-5-20251001")
_current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="unknown")

def get_client() -> AsyncAnthropic:
    try:
        return _current_client.get()
    except LookupError:
        raise RuntimeError("No client in context. Call with_dependencies() first.")

def get_model() -> str:
    return _current_model.get()

async def with_dependencies(
    client: AsyncAnthropic,
    model: str,
    request_id: str,
    coro_fn: Callable[..., Any],
    *args,
    **kwargs
) -> Any:
    """Run a coroutine with injected dependencies in context."""
    token_client = _current_client.set(client)
    token_model = _current_model.set(model)
    token_rid = _current_request_id.set(request_id)
    try:
        return await coro_fn(*args, **kwargs)
    finally:
        _current_client.reset(token_client)
        _current_model.reset(token_model)
        _current_request_id.reset(token_rid)

# Functions use context — no deps parameter needed
async def call_llm(prompt: str) -> str:
    client = get_client()
    model = get_model()
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text if response.content else ""

async def pipeline(text: str) -> dict:
    summary = await call_llm(f"Summarize: {text}")
    keywords = await call_llm(f"Extract 3 keywords from: {summary}")
    return {"summary": summary, "keywords": keywords, "request_id": _current_request_id.get()}

async def main():
    client = AsyncAnthropic()
    try:
        # Each request gets its own dependency context
        result = await with_dependencies(
            client, "claude-haiku-4-5-20251001", "req-001",
            pipeline, "Dependency injection patterns for async Python systems"
        )
        print(f"[{result['request_id']}] Summary: {result['summary'][:150]}")
    finally:
        await client.close()

asyncio.run(main())
```

## Solution 3: Factory Functions with Configurable Overrides

Use factory functions to create agents with injected dependencies, with easy override points for testing.

```python
import asyncio
from anthropic import AsyncAnthropic
from typing import Protocol, Callable, Awaitable

class LLMClient(Protocol):
    async def complete(self, prompt: str, system: str = "") -> str: ...

class AnthropicAdapter:
    def __init__(self, client: AsyncAnthropic, model: str = "claude-haiku-4-5-20251001"):
        self._client = client
        self._model = model

    async def complete(self, prompt: str, system: str = "") -> str:
        kwargs = {
            "model": self._model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}]
        }
        if system:
            kwargs["system"] = system
        response = await self._client.messages.create(**kwargs)
        return response.content[0].text if response.content else ""

class MockLLMClient:
    def __init__(self, response: str = "mock response"):
        self._response = response
        self.calls: list[dict] = []

    async def complete(self, prompt: str, system: str = "") -> str:
        self.calls.append({"prompt": prompt, "system": system})
        return self._response

# Agent factory: accepts dependencies as parameters
def make_summarizer(llm: LLMClient) -> Callable[[str], Awaitable[str]]:
    async def summarize(text: str) -> str:
        return await llm.complete(
            f"Summarize this in 2 sentences: {text}",
            system="You are a precise summarizer."
        )
    return summarize

def make_classifier(llm: LLMClient, categories: list[str]) -> Callable[[str], Awaitable[str]]:
    async def classify(text: str) -> str:
        cats = ", ".join(categories)
        return await llm.complete(f"Classify into one of [{cats}]: {text}")
    return classify

async def main():
    # Production
    real_client = AsyncAnthropic()
    adapter = AnthropicAdapter(real_client)
    summarize = make_summarizer(adapter)
    classify = make_classifier(adapter, ["technical", "business", "general"])

    result = await summarize("Dependency injection decouples construction from use.")
    print(f"Summary: {result[:150]}")

    category = await classify(result)
    print(f"Category: {category}")
    await real_client.close()

    # Test: inject mock
    mock = MockLLMClient("This is a technical topic.")
    test_summarize = make_summarizer(mock)
    test_result = await test_summarize("any input")
    assert test_result == "This is a technical topic."
    assert len(mock.calls) == 1
    print(f"Mock test passed. Calls: {mock.calls}")

asyncio.run(main())
```

## Solution 4: Request-Scoped Dependency Injection

Create fresh, scoped dependency instances per request, with automatic cleanup.

```python
import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic
from typing import AsyncIterator

@dataclass
class RequestScope:
    request_id: str
    client: AsyncAnthropic
    model: str
    metadata: dict = field(default_factory=dict)
    _call_count: int = field(default=0, init=False)
    _total_tokens: int = field(default=0, init=False)

    async def complete(self, prompt: str, system: str = "") -> str:
        self._call_count += 1
        kwargs = {
            "model": self.model,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}]
        }
        if system:
            kwargs["system"] = system
        response = await self.client.messages.create(**kwargs)
        self._total_tokens += response.usage.output_tokens
        return response.content[0].text if response.content else ""

    def stats(self) -> dict:
        return {
            "request_id": self.request_id,
            "calls": self._call_count,
            "total_output_tokens": self._total_tokens,
        }

@asynccontextmanager
async def request_scope(
    client: AsyncAnthropic,
    model: str = "claude-haiku-4-5-20251001",
    **metadata
) -> AsyncIterator[RequestScope]:
    scope = RequestScope(
        request_id=str(uuid.uuid4())[:8],
        client=client,
        model=model,
        metadata=metadata
    )
    try:
        yield scope
    finally:
        # Log stats, flush metrics, cleanup
        print(f"[Scope {scope.request_id}] {scope.stats()}")

async def process_document(scope: RequestScope, doc: str) -> dict:
    summary = await scope.complete(f"Summarize: {doc}")
    sentiment = await scope.complete(f"Sentiment (positive/neutral/negative): {summary}")
    return {"summary": summary, "sentiment": sentiment.strip()}

async def main():
    client = AsyncAnthropic()
    try:
        async with request_scope(client, user_id="usr-001") as scope:
            result = await process_document(scope, "Async dependency injection reduces coupling.")
            print(f"Result: {result}")
    finally:
        await client.close()

asyncio.run(main())
```

## Solution 5: Layered Dependency Override for A/B Testing

Support multiple dependency configurations simultaneously for A/B testing different models or backends.

```python
import asyncio
import random
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Callable, Awaitable

@dataclass
class ModelConfig:
    name: str
    model: str
    max_tokens: int
    weight: float  # probability of selection

class WeightedModelRouter:
    def __init__(self, client: AsyncAnthropic, configs: list[ModelConfig]):
        self._client = client
        self._configs = configs
        self._usage: dict[str, int] = {c.name: 0 for c in configs}

    def _select_config(self) -> ModelConfig:
        weights = [c.weight for c in self._configs]
        return random.choices(self._configs, weights=weights, k=1)[0]

    async def complete(self, prompt: str, system: str = "") -> tuple[str, str]:
        config = self._select_config()
        self._usage[config.name] += 1
        kwargs = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "messages": [{"role": "user", "content": prompt}]
        }
        if system:
            kwargs["system"] = system
        response = await self._client.messages.create(**kwargs)
        output = response.content[0].text if response.content else ""
        return output, config.name

    def usage_stats(self) -> dict:
        return dict(self._usage)

async def run_ab_test(router: WeightedModelRouter, prompts: list[str]) -> list[dict]:
    results = []
    for prompt in prompts:
        output, variant = await router.complete(prompt)
        results.append({"prompt": prompt[:50], "variant": variant, "output": output[:100]})
    return results

async def main():
    client = AsyncAnthropic()
    configs = [
        ModelConfig("haiku-fast", "claude-haiku-4-5-20251001", 256, weight=0.7),
        ModelConfig("haiku-thorough", "claude-haiku-4-5-20251001", 1024, weight=0.3),
    ]
    router = WeightedModelRouter(client, configs)

    prompts = [
        "What is async programming?",
        "Explain dependency injection briefly.",
        "What is Python's GIL?",
    ]
    results = await run_ab_test(router, prompts)
    for r in results:
        print(f"[{r['variant']}] {r['prompt']}... → {r['output'][:80]}")

    print(f"\nUsage: {router.usage_stats()}")
    await client.close()

asyncio.run(main())
```

## Solution 6: Async Dependency Graph with Lazy Initialization

Declare dependencies as a graph; each node is initialized lazily when first needed, with automatic ordering.

```python
import asyncio
from anthropic import AsyncAnthropic
from typing import Any, Callable, Awaitable

class AsyncDependencyGraph:
    """
    Lazy dependency graph. Each dependency is initialized once on first access.
    Supports async initializers and automatic dependency ordering.
    """
    def __init__(self):
        self._factories: dict[str, Callable[..., Awaitable[Any]]] = {}
        self._instances: dict[str, Any] = {}
        self._deps: dict[str, list[str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def register(
        self,
        name: str,
        factory: Callable[..., Awaitable[Any]],
        depends_on: list[str] | None = None
    ):
        self._factories[name] = factory
        self._deps[name] = depends_on or []
        self._locks[name] = asyncio.Lock()

    async def get(self, name: str) -> Any:
        if name in self._instances:
            return self._instances[name]

        async with self._locks[name]:
            # Double-check after acquiring lock
            if name in self._instances:
                return self._instances[name]

            # Resolve dependencies first
            dep_values = {}
            for dep_name in self._deps.get(name, []):
                dep_values[dep_name] = await self.get(dep_name)

            factory = self._factories[name]
            instance = await factory(**dep_values) if dep_values else await factory()
            self._instances[name] = instance
            return instance

    async def close_all(self):
        for name, instance in self._instances.items():
            if hasattr(instance, "close"):
                try:
                    await instance.close()
                except Exception:
                    pass
        self._instances.clear()

# Define factories
async def create_anthropic_client() -> AsyncAnthropic:
    return AsyncAnthropic()

async def create_llm_adapter(anthropic_client: AsyncAnthropic) -> dict:
    async def complete(prompt: str) -> str:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text if response.content else ""
    return {"complete": complete}

async def create_agent(anthropic_client: AsyncAnthropic) -> dict:
    async def run(task: str) -> str:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": task}]
        )
        return response.content[0].text if response.content else ""
    return {"run": run}

async def main():
    graph = AsyncDependencyGraph()
    graph.register("anthropic_client", create_anthropic_client)
    graph.register("llm_adapter", create_llm_adapter, depends_on=["anthropic_client"])
    graph.register("agent", create_agent, depends_on=["anthropic_client"])

    try:
        # Lazy init — only creates what's needed
        agent = await graph.get("agent")
        result = await agent["run"]("What is dependency injection in three sentences?")
        print(result[:300])

        # Second call: reuses cached instance, no re-initialization
        agent2 = await graph.get("agent")
        assert agent is agent2  # same instance

    finally:
        await graph.close_all()

asyncio.run(main())
```

## Comparison

| Approach | Testability | Scope | Overhead | Best For |
|---|---|---|---|---|
| Dependency Container | High | Process | None | Shared long-lived deps |
| Context Variables | High | Request (implicit) | Minimal | Deep call chains |
| Factory Functions | Very High | Per-factory | None | Functional style |
| Request Scope | High | Per-request | Minimal | Metrics, per-request config |
| Weighted Router | High | Per-call | Minimal | A/B testing, model routing |
| Lazy Dependency Graph | High | Process | Minimal | Complex dep graphs |

**Rule of thumb**: Use a dependency container for process-scoped resources (DB pools, HTTP clients), context variables for request-scoped propagation through deep chains, and factory functions for testability-first design.
