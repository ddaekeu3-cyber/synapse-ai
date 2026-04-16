---
title: "Agent Doesn't Implement Deferred Initialization for Expensive Components"
description: "AI agents that eagerly initialize all components at startup — loading embedding models, opening database pools, compiling regex patterns — pay the full startup cost even when most components are never used in a given session. Deferred initialization loads components on first use, cutting cold-start time by 50–90%."
date: 2025-02-03
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-deferred-initialization-for-expensive-components
tags:
  - lazy-initialization
  - deferred-init
  - cold-start
  - performance
  - startup-time
  - dependency-injection
symptoms:
  - "Agent process takes 15–30 seconds to start because it loads all models upfront"
  - "Memory usage is high from the first request even for simple workloads"
  - "Serverless / container deployments time out during cold start"
  - "Loading a rarely-used tool schema or model increases startup time for every request"
  - "Health check endpoint fails during startup because components are still loading"
---

## Problem

Agent frameworks often call `__init__` on every component during startup: embedding models are loaded into GPU memory, connection pools are opened, tokenisers are compiled. If the agent serves diverse request types, most sessions use only a subset of these components — but everyone pays the full startup cost.

Deferred (lazy) initialisation delays component construction until the first access. The component object exists from construction time (as a placeholder), but the expensive work happens only on first use. After that, the result is cached so subsequent accesses are instant.

---

## Solution 1: Lazy Property Descriptor

A `lazy_property` descriptor replaces itself with the computed value on first access, making subsequent reads a simple dict lookup with no overhead.

```python
import threading
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")


class lazy_property(Generic[T]):
    """
    Descriptor that computes the value on first access and caches it
    on the instance's __dict__, making all subsequent reads O(1).

    Usage:
        class MyAgent:
            @lazy_property
            def embedding_model(self):
                return load_embedding_model()   # called only once

            @lazy_property
            def vector_db(self):
                return connect_vector_db()
    """

    def __init__(self, func: Callable[..., T]):
        self._func = func
        self._name: Optional[str] = None

    def __set_name__(self, owner, name: str):
        self._name = name

    def __get__(self, obj, objtype=None) -> T:
        if obj is None:
            return self  # class-level access returns the descriptor
        if self._name not in obj.__dict__:
            obj.__dict__[self._name] = self._func(obj)
        return obj.__dict__[self._name]


class thread_safe_lazy_property(Generic[T]):
    """Thread-safe variant using a per-instance lock."""

    def __init__(self, func: Callable[..., T]):
        self._func = func
        self._lock_attr = f"_lock_{func.__name__}"
        self._name: Optional[str] = None

    def __set_name__(self, owner, name: str):
        self._name = name

    def __get__(self, obj, objtype=None) -> T:
        if obj is None:
            return self
        if self._name in obj.__dict__:
            return obj.__dict__[self._name]
        lock = obj.__dict__.setdefault(self._lock_attr, threading.Lock())
        with lock:
            if self._name not in obj.__dict__:
                obj.__dict__[self._name] = self._func(obj)
        return obj.__dict__[self._name]


# Example agent with deferred components
class ResearchAgent:
    def __init__(self, config: dict):
        self._config = config
        # Nothing expensive happens here

    @lazy_property
    def embedding_model(self):
        """Loaded only when first embedding is needed."""
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(self._config.get("embed_model", "all-MiniLM-L6-v2"))

    @lazy_property
    def vector_store(self):
        """Connection opened only when first search is needed."""
        import chromadb
        return chromadb.HttpClient(host=self._config["chroma_host"])

    @lazy_property
    def tokeniser(self):
        """Compiled only when first tokenisation is needed."""
        import tiktoken
        return tiktoken.encoding_for_model(self._config.get("model", "gpt-4o"))
```

---

## Solution 2: Async Lazy Initialiser

Async components (database pools, HTTP sessions) cannot be initialised in a synchronous `__get__`. This wrapper defers async initialisation to first use.

```python
import asyncio
from typing import Awaitable, Callable, Generic, Optional, TypeVar

T = TypeVar("T")


class AsyncLazyComponent(Generic[T]):
    """
    Async-safe lazy initialiser. The factory coroutine is called once on
    first `await component.get()`.

    Usage:
        class MyAgent:
            def __init__(self):
                self._db = AsyncLazyComponent(self._create_db_pool)
                self._http = AsyncLazyComponent(self._create_http_session)

            async def _create_db_pool(self):
                import asyncpg
                return await asyncpg.create_pool(dsn=os.environ["DB_URL"])

            async def query(self, sql: str):
                db = await self._db.get()
                return await db.fetch(sql)
    """

    def __init__(self, factory: Callable[[], Awaitable[T]]):
        self._factory = factory
        self._value: Optional[T] = None
        self._lock = asyncio.Lock()
        self._initialised = False

    async def get(self) -> T:
        if self._initialised:
            return self._value
        async with self._lock:
            if not self._initialised:
                self._value = await self._factory()
                self._initialised = True
        return self._value

    def reset(self):
        """Force re-initialisation on next access."""
        self._initialised = False
        self._value = None

    @property
    def is_ready(self) -> bool:
        return self._initialised
```

---

## Solution 3: Component Registry with Load-on-Demand

A registry holds component factories. Components are constructed only when first resolved by name, and cached for subsequent calls.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class ComponentEntry:
    name: str
    factory: Callable
    is_async: bool
    instance: Optional[Any] = None
    load_time_ms: float = 0.0
    load_count: int = 0


class DeferredComponentRegistry:
    """
    Registry for expensive agent components.
    All factories registered upfront; instances created on first access.

    Usage:
        registry = DeferredComponentRegistry()
        registry.register("embed_model", lambda: load_heavy_model(), async_factory=False)
        registry.register_async("db_pool", create_pool)

        # In agent:
        model = await registry.get("embed_model")
        db = await registry.get("db_pool")

        print(registry.stats())   # shows which components have been loaded
    """

    def __init__(self):
        self._components: Dict[str, ComponentEntry] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def register(self, name: str, factory: Callable,
                 async_factory: bool = False):
        self._components[name] = ComponentEntry(
            name=name, factory=factory, is_async=async_factory
        )
        self._locks[name] = asyncio.Lock()

    def register_async(self, name: str, factory: Callable):
        self.register(name, factory, async_factory=True)

    async def get(self, name: str) -> Any:
        entry = self._components.get(name)
        if entry is None:
            raise KeyError(f"Unknown component: {name}")
        if entry.instance is not None:
            return entry.instance
        async with self._locks[name]:
            if entry.instance is None:
                t0 = time.monotonic()
                if entry.is_async:
                    entry.instance = await entry.factory()
                else:
                    entry.instance = await asyncio.to_thread(entry.factory)
                entry.load_time_ms = (time.monotonic() - t0) * 1000
                entry.load_count += 1
        return entry.instance

    def is_loaded(self, name: str) -> bool:
        entry = self._components.get(name)
        return entry is not None and entry.instance is not None

    def stats(self) -> Dict[str, dict]:
        return {
            name: {
                "loaded": e.instance is not None,
                "load_time_ms": round(e.load_time_ms, 1),
            }
            for name, e in self._components.items()
        }

    def preload(self, names: list) -> asyncio.Task:
        """Background-preload components by name list."""
        async def _preload():
            await asyncio.gather(*(self.get(n) for n in names))
        return asyncio.create_task(_preload(), name="component_preload")
```

---

## Solution 4: Tiered Startup with Critical-Path Prioritisation

Separate components into tiers: critical (loaded before first request), warm (loaded in background after first request), cold (loaded on first use only). The agent starts serving in milliseconds instead of seconds.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class StartupTier(Enum):
    CRITICAL = 1    # must be ready before serving
    WARM = 2        # loaded in background during first few requests
    COLD = 3        # loaded on first actual use


@dataclass
class TieredComponent:
    name: str
    factory: Callable
    tier: StartupTier
    is_async: bool = False
    instance: Optional[Any] = None


class TieredStartupManager:
    """
    Manages component loading across startup tiers.

    Usage:
        mgr = TieredStartupManager()
        mgr.add("health_check", tier=CRITICAL, factory=lambda: True)
        mgr.add("config", tier=CRITICAL, factory=load_config)
        mgr.add("embed_model", tier=WARM, factory=load_model)
        mgr.add("report_generator", tier=COLD, factory=load_report_gen)

        await mgr.boot()          # loads CRITICAL tier; returns in < 500 ms
        asyncio.create_task(mgr.warm())   # loads WARM tier in background
        # COLD tier loads on first access
    """

    def __init__(self):
        self._components: Dict[str, TieredComponent] = {}
        self._registry = DeferredComponentRegistry()

    def add(self, name: str, tier: StartupTier,
            factory: Callable, is_async: bool = False):
        comp = TieredComponent(name=name, factory=factory,
                               tier=tier, is_async=is_async)
        self._components[name] = comp
        self._registry.register(name, factory, async_factory=is_async)

    async def boot(self) -> float:
        """Load CRITICAL tier. Returns elapsed ms."""
        t0 = time.monotonic()
        critical = [
            name for name, c in self._components.items()
            if c.tier == StartupTier.CRITICAL
        ]
        await asyncio.gather(*(self._registry.get(n) for n in critical))
        return (time.monotonic() - t0) * 1000

    async def warm(self):
        """Load WARM tier in background."""
        warm = [
            name for name, c in self._components.items()
            if c.tier == StartupTier.WARM
        ]
        for name in warm:
            await self._registry.get(name)
            await asyncio.sleep(0)   # yield between loads

    async def get(self, name: str) -> Any:
        """Get any component (COLD tier initialises on first call)."""
        return await self._registry.get(name)

    def startup_report(self) -> dict:
        stats = self._registry.stats()
        return {
            "critical_loaded": all(
                stats[n]["loaded"] for n, c in self._components.items()
                if c.tier == StartupTier.CRITICAL
            ),
            "components": stats,
        }
```

---

## Solution 5: Lazy Import Manager

Large Python dependencies (torch, transformers, sentence_transformers) add hundreds of milliseconds of import time. This manager defers imports until first use and caches the module reference.

```python
import importlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


class LazyImportManager:
    """
    Defers heavy module imports until first use.
    Subsequent accesses return the cached module.

    Usage:
        lazy = LazyImportManager()
        lazy.register("torch", "torch")
        lazy.register("transformers", "transformers")
        lazy.register("st", "sentence_transformers")

        # First access triggers import:
        torch = lazy.get("torch")      # ~200 ms on first call
        torch = lazy.get("torch")      # ~0 ms on subsequent calls

        print(lazy.import_times())
    """

    def __init__(self):
        self._modules: Dict[str, Optional[Any]] = {}
        self._import_times: Dict[str, float] = {}
        self._module_paths: Dict[str, str] = {}

    def register(self, alias: str, module_path: str):
        self._module_paths[alias] = module_path
        self._modules[alias] = None

    def get(self, alias: str) -> Any:
        if alias not in self._module_paths:
            raise KeyError(f"Module alias '{alias}' not registered")
        if self._modules[alias] is None:
            t0 = time.monotonic()
            self._modules[alias] = importlib.import_module(
                self._module_paths[alias]
            )
            self._import_times[alias] = (time.monotonic() - t0) * 1000
        return self._modules[alias]

    def is_imported(self, alias: str) -> bool:
        return self._modules.get(alias) is not None

    def import_times(self) -> Dict[str, float]:
        return {
            alias: round(ms, 1)
            for alias, ms in self._import_times.items()
        }

    def preimport_all(self):
        """Eagerly import all registered modules (e.g. during a warm-up period)."""
        for alias in self._module_paths:
            self.get(alias)


# Global lazy import registry
_lazy = LazyImportManager()
_lazy.register("torch", "torch")
_lazy.register("np", "numpy")
_lazy.register("transformers", "transformers")
_lazy.register("st", "sentence_transformers")
```

---

## Solution 6: Adaptive Pre-Warming Predictor

Tracks which components are used by which request types. Starts pre-loading predicted components in the background when a new request arrives, so they are ready by the time the agent actually needs them.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class UsageRecord:
    request_type: str
    component_name: str
    timestamp: float


class AdaptivePrewarmingPredictor:
    """
    Observes which components each request type uses.
    When a new request of a known type arrives, pre-warms likely components.

    Usage:
        predictor = AdaptivePrewarmingPredictor(registry)
        predictor.observe("search_request", "embed_model")
        predictor.observe("search_request", "vector_store")
        predictor.observe("report_request", "report_generator")

        # On new request:
        await predictor.prewarm("search_request")
        # embed_model and vector_store start loading in background
    """

    def __init__(self, registry: DeferredComponentRegistry,
                 min_observations: int = 3):
        self._registry = registry
        self._min_obs = min_observations
        self._usage: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def observe(self, request_type: str, component_name: str):
        self._usage[request_type][component_name] += 1

    def predicted_components(self, request_type: str,
                              min_count: int = None) -> List[str]:
        threshold = min_count or self._min_obs
        return [
            comp for comp, count in self._usage[request_type].items()
            if count >= threshold
        ]

    async def prewarm(self, request_type: str):
        """Start loading predicted components in the background."""
        components = self.predicted_components(request_type)
        not_yet_loaded = [
            c for c in components if not self._registry.is_loaded(c)
        ]
        if not_yet_loaded:
            asyncio.create_task(
                asyncio.gather(*(self._registry.get(c) for c in not_yet_loaded)),
                name=f"prewarm_{request_type}",
            )

    def stats(self) -> dict:
        return {
            rt: dict(components) for rt, components in self._usage.items()
        }
```

---

## Comparison

| Approach | Startup Time Impact | Thread-Safe | Async-Compatible |
|---|---|---|---|
| **lazy_property Descriptor** | Eliminates per-component cost | With thread-safe variant | No |
| **AsyncLazyComponent** | Eliminates per-component cost | Yes | Yes |
| **Deferred Component Registry** | Centralised + tracked | Yes | Yes |
| **Tiered Startup Manager** | Critical < 500 ms | Yes | Yes |
| **Lazy Import Manager** | Defers 200–800 ms import cost | Yes | No |
| **Adaptive Pre-Warming** | Reduces latency on first use | Yes | Yes |

**Key insight**: start with `lazy_property` for synchronous components and `AsyncLazyComponent` for async ones — both are two-line changes. Add the Tiered Startup Manager when cold-start SLOs become critical, and the Adaptive Pre-Warmer when your profiler shows consistent first-access latency.
