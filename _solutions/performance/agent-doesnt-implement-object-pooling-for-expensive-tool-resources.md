---
title: "Agent Doesn't Implement Object Pooling for Expensive Tool Resources"
description: "AI agents that instantiate expensive objects — browser sessions, database connections, LLM tokenizers, NLP models, compiled regex patterns — on every tool call pay full initialization cost per request. Object pooling pre-allocates a bounded set of these resources, checks them out to callers, and returns them to the pool on release, amortizing initialization cost across thousands of calls."
date: 2025-02-18
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-object-pooling-for-expensive-tool-resources
tags:
  - object-pooling
  - resource-reuse
  - performance
  - initialization-cost
  - browser-session
  - tokenizer
  - nlp-model
symptoms:
  - "Each web scraping tool call launches a new browser context, taking 2–3 seconds"
  - "NLP model loaded from disk on every tool invocation instead of once"
  - "Regex patterns compiled on every call despite never changing"
  - "Tokenizer initialized per-request causing 200ms overhead per tool call"
  - "Database connection opened and closed for every query instead of reused"
---

## Problem

Some resources are expensive to create but cheap to use: a headless browser context, a loaded NLP model, a compiled tokenizer, an established gRPC channel. Creating them per-call multiplies initialization cost by request volume. A pool maintains N pre-created instances, lends each to one caller at a time, and accepts it back when the caller is done. Callers wait briefly if all instances are busy rather than creating a new one. The pool also validates instances before lending (rejecting crashed or stale ones) and recreates failed instances transparently.

---

## Solution 1: GenericObjectPool — Reusable Bounded Pool for Any Resource

```python
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Generic, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class PooledItem(Generic[T]):
    obj: T
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    use_count: int = 0
    healthy: bool = True


class GenericObjectPool(Generic[T]):
    """
    Async-safe bounded pool for any expensive resource.
    Pre-creates `min_size` objects at startup and creates up to `max_size`
    on demand. Objects are validated before being lent; unhealthy objects
    are discarded and replaced transparently.

    Usage:
        async def create_tokenizer():
            return tiktoken.get_encoding("cl100k_base")

        pool = GenericObjectPool(
            factory=create_tokenizer,
            min_size=2,
            max_size=8,
            max_idle_s=300,
        )
        await pool.start()

        async with pool.acquire() as tokenizer:
            tokens = tokenizer.encode(text)
    """

    def __init__(self, factory: Callable,
                  min_size: int = 2,
                  max_size: int = 8,
                  max_idle_s: float = 300.0,
                  validate_fn: Optional[Callable] = None,
                  destroy_fn: Optional[Callable] = None,
                  acquire_timeout_s: float = 10.0):
        self._factory = factory
        self._min = min_size
        self._max = max_size
        self._max_idle = max_idle_s
        self._validate = validate_fn
        self._destroy = destroy_fn
        self._timeout = acquire_timeout_s
        self._idle: asyncio.Queue = asyncio.Queue(maxsize=max_size)
        self._active_count = 0
        self._total_created = 0
        self._total_acquired = 0
        self._total_destroyed = 0

    async def start(self):
        """Pre-create minimum pool instances."""
        for _ in range(self._min):
            item = await self._create()
            await self._idle.put(item)
        logger.info(
            "pool_started min=%d max=%d type=%s",
            self._min, self._max, self._factory.__name__,
        )

    async def _create(self) -> PooledItem:
        obj = await self._factory() if asyncio.iscoroutinefunction(self._factory) \
              else self._factory()
        self._total_created += 1
        return PooledItem(obj=obj)

    async def _acquire_one(self) -> PooledItem:
        total = self._active_count + self._idle.qsize()

        if not self._idle.empty():
            item = await self._idle.get()
        elif total < self._max:
            item = await self._create()
        else:
            item = await asyncio.wait_for(self._idle.get(), timeout=self._timeout)

        # Validate
        if self._validate:
            try:
                valid = await self._validate(item.obj) \
                    if asyncio.iscoroutinefunction(self._validate) \
                    else self._validate(item.obj)
            except Exception:
                valid = False
            if not valid:
                await self._discard(item)
                return await self._create()

        item.last_used = time.monotonic()
        item.use_count += 1
        self._active_count += 1
        self._total_acquired += 1
        return item

    async def _release(self, item: PooledItem):
        self._active_count -= 1
        idle_age = time.monotonic() - item.created_at
        if idle_age > self._max_idle or not item.healthy:
            await self._discard(item)
            if self._idle.qsize() < self._min:
                new_item = await self._create()
                await self._idle.put(new_item)
        else:
            await self._idle.put(item)

    async def _discard(self, item: PooledItem):
        self._total_destroyed += 1
        if self._destroy:
            try:
                await self._destroy(item.obj) \
                    if asyncio.iscoroutinefunction(self._destroy) \
                    else self._destroy(item.obj)
            except Exception as exc:
                logger.warning("pool_destroy_error: %s", exc)

    @asynccontextmanager
    async def acquire(self):
        item = await self._acquire_one()
        try:
            yield item.obj
        except Exception:
            item.healthy = False
            raise
        finally:
            await self._release(item)

    def stats(self) -> dict:
        return {
            "idle": self._idle.qsize(),
            "active": self._active_count,
            "total_created": self._total_created,
            "total_acquired": self._total_acquired,
            "total_destroyed": self._total_destroyed,
        }

    async def close(self):
        while not self._idle.empty():
            item = await self._idle.get()
            await self._discard(item)
```

---

## Solution 2: BrowserSessionPool — Pool Playwright/Selenium Browser Contexts

```python
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

logger = logging.getLogger(__name__)


class BrowserSessionPool:
    """
    Pool of headless browser contexts (Playwright) for web scraping tools.
    Browser startup costs 1–3 seconds; reusing a pool of contexts reduces
    per-call overhead to <50ms.

    Usage:
        pool = BrowserSessionPool(size=4)
        await pool.start()

        async with pool.session() as page:
            await page.goto("https://example.com")
            content = await page.content()
    """

    def __init__(self, size: int = 4,
                  browser_type: str = "chromium",
                  headless: bool = True,
                  context_options: Optional[dict] = None):
        self._size = size
        self._browser_type = browser_type
        self._headless = headless
        self._ctx_options = context_options or {}
        self._pool: asyncio.Queue = asyncio.Queue(maxsize=size)
        self._browser = None
        self._created = 0
        self._reuses = 0

    async def start(self):
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            browser_launcher = getattr(self._playwright, self._browser_type)
            self._browser = await browser_launcher.launch(headless=self._headless)

            for _ in range(self._size):
                ctx = await self._browser.new_context(**self._ctx_options)
                await self._pool.put(ctx)
                self._created += 1

            logger.info(
                "browser_pool_started size=%d type=%s",
                self._size, self._browser_type,
            )
        except ImportError:
            logger.warning("playwright not available; browser pool disabled")

    @asynccontextmanager
    async def session(self, timeout_s: float = 30.0):
        ctx = await asyncio.wait_for(self._pool.get(), timeout=timeout_s)
        page = await ctx.new_page()
        try:
            self._reuses += 1
            yield page
        finally:
            await page.close()
            # Return context to pool (reset cookies/storage)
            await ctx.clear_cookies()
            await self._pool.put(ctx)

    async def close(self):
        while not self._pool.empty():
            ctx = await self._pool.get()
            await ctx.close()
        if self._browser:
            await self._browser.close()

    def stats(self) -> dict:
        return {
            "idle_contexts": self._pool.qsize(),
            "total_created": self._created,
            "total_reuses": self._reuses,
        }
```

---

## Solution 3: TokenizerPool — Reuse Expensive Tokenizer Instances

```python
import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class TokenizerPool:
    """
    Thread-safe pool for tokenizer objects (tiktoken, sentencepiece,
    transformers tokenizer). Tokenizer initialization loads vocabulary
    files (10–500ms); pooling amortizes this across all calls.
    Uses a threading.Semaphore since tokenizer calls are typically sync.

    Usage:
        pool = TokenizerPool(
            factory=lambda: tiktoken.get_encoding("cl100k_base"),
            size=4,
        )

        with pool.acquire() as tokenizer:
            token_count = len(tokenizer.encode(text))
    """

    def __init__(self, factory: Callable,
                  size: int = 4,
                  name: str = "tokenizer"):
        self._factory = factory
        self._size = size
        self._name = name
        self._items = []
        self._sem = threading.Semaphore(0)
        self._lock = threading.Lock()
        self._in_use = 0

    def initialize(self):
        """Create all pool instances. Call once at startup."""
        for _ in range(self._size):
            obj = self._factory()
            self._items.append(obj)
            self._sem.release()
        logger.info("tokenizer_pool_ready name=%s size=%d", self._name, self._size)

    @contextmanager
    def acquire(self, timeout: float = 5.0):
        if not self._sem.acquire(timeout=timeout):
            raise TimeoutError(
                f"TokenizerPool '{self._name}' all {self._size} instances busy"
            )
        with self._lock:
            obj = self._items.pop()
            self._in_use += 1
        try:
            yield obj
        finally:
            with self._lock:
                self._items.append(obj)
                self._in_use -= 1
            self._sem.release()

    def stats(self) -> dict:
        return {
            "name": self._name,
            "size": self._size,
            "in_use": self._in_use,
            "idle": self._size - self._in_use,
        }
```

---

## Solution 4: CompiledPatternCache — Pool Compiled Regex Patterns

```python
import logging
import re
import threading
from typing import Dict, List, Optional, Pattern, Union

logger = logging.getLogger(__name__)


class CompiledPatternCache:
    """
    Caches compiled regex Pattern objects by their string representation.
    Python's `re` module has its own internal cache (limited to 512 entries),
    but this explicit cache provides deterministic retention and usage metrics.

    Usage:
        cache = CompiledPatternCache()
        pattern = cache.get(r'\b\d{4}-\d{2}-\d{2}\b', re.IGNORECASE)
        matches = pattern.findall(tool_output)
    """

    def __init__(self, max_patterns: int = 256):
        self._cache: Dict[tuple, re.Pattern] = {}
        self._lock = threading.Lock()
        self._max = max_patterns
        self._hits = 0
        self._misses = 0

    def get(self, pattern: str,
             flags: Union[int, re.RegexFlag] = 0) -> re.Pattern:
        key = (pattern, int(flags))
        with self._lock:
            if key in self._cache:
                self._hits += 1
                return self._cache[key]

        compiled = re.compile(pattern, flags)
        with self._lock:
            if len(self._cache) >= self._max:
                # Evict oldest entry
                oldest = next(iter(self._cache))
                del self._cache[oldest]
            self._cache[key] = compiled
            self._misses += 1

        return compiled

    def precompile(self, patterns: List[str],
                    flags: Union[int, re.RegexFlag] = 0):
        """Pre-compile a list of patterns at startup."""
        for p in patterns:
            self.get(p, flags)
        logger.info("pattern_cache_precompiled count=%d", len(patterns))

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "cached": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 3),
        }
```

---

## Solution 5: NLPModelPool — Pool Heavy ML Model Instances

```python
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


class NLPModelPool:
    """
    Pool for heavy NLP models (spaCy, sentence-transformers, HuggingFace)
    that take seconds to load from disk. Each pool slot holds one loaded
    model instance; callers borrow a slot for inference and return it.

    Usage:
        pool = NLPModelPool(
            loader=lambda: spacy.load("en_core_web_lg"),
            size=2,
            name="spacy-lg",
        )
        await pool.load()

        async with pool.model() as nlp:
            doc = nlp(text)
            entities = [(e.text, e.label_) for e in doc.ents]
    """

    def __init__(self, loader: Callable,
                  size: int = 2,
                  name: str = "nlp_model"):
        self._loader = loader
        self._size = size
        self._name = name
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=size)
        self._loaded = False
        self._load_time_s: float = 0.0
        self._inference_count = 0

    async def load(self):
        """Load all model instances. Run once at agent startup."""
        t0 = time.monotonic()
        loop = asyncio.get_event_loop()
        for _ in range(self._size):
            model = await loop.run_in_executor(None, self._loader)
            await self._queue.put(model)
        self._load_time_s = time.monotonic() - t0
        self._loaded = True
        logger.info(
            "nlp_pool_loaded name=%s size=%d load_s=%.2f",
            self._name, self._size, self._load_time_s,
        )

    @asynccontextmanager
    async def model(self, timeout_s: float = 10.0):
        if not self._loaded:
            raise RuntimeError(f"NLPModelPool '{self._name}' not loaded — call await load()")
        model = await asyncio.wait_for(self._queue.get(), timeout=timeout_s)
        try:
            self._inference_count += 1
            yield model
        finally:
            await self._queue.put(model)

    def stats(self) -> dict:
        return {
            "name": self._name,
            "size": self._size,
            "idle": self._queue.qsize(),
            "load_time_s": round(self._load_time_s, 2),
            "inference_count": self._inference_count,
        }
```

---

## Solution 6: ResourcePoolRegistry — Manage Multiple Pools as a Registry

```python
import asyncio
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ResourcePoolRegistry:
    """
    Centralized registry of named resource pools. Agents register all
    expensive resources at startup; tool functions look up pools by name
    and acquire from them without knowing pool internals.

    Usage:
        registry = ResourcePoolRegistry()
        registry.register_generic("tokenizer", create_tokenizer, min_size=2, max_size=4)
        registry.register_generic("embedder", create_embedder, min_size=1, max_size=2)
        await registry.start_all()

        # In tool functions:
        async with registry.acquire("tokenizer") as tok:
            return tok.encode(text)
    """

    def __init__(self):
        self._pools: Dict[str, Any] = {}

    def register_generic(self, name: str,
                           factory: Callable,
                           min_size: int = 2,
                           max_size: int = 8,
                           **kwargs):
        self._pools[name] = GenericObjectPool(
            factory=factory,
            min_size=min_size,
            max_size=max_size,
            **kwargs,
        )

    def register_browser(self, name: str = "browser",
                          size: int = 4, **kwargs):
        self._pools[name] = BrowserSessionPool(size=size, **kwargs)

    def register_nlp(self, name: str,
                      loader: Callable,
                      size: int = 2):
        self._pools[name] = NLPModelPool(loader=loader, size=size, name=name)

    async def start_all(self):
        for name, pool in self._pools.items():
            try:
                if hasattr(pool, "start"):
                    await pool.start()
                elif hasattr(pool, "load"):
                    await pool.load()
                logger.info("pool_started name=%s", name)
            except Exception as exc:
                logger.error("pool_start_failed name=%s error=%s", name, exc)

    def acquire(self, name: str):
        pool = self._pools.get(name)
        if pool is None:
            raise KeyError(f"No pool registered with name '{name}'")
        if hasattr(pool, "acquire"):
            return pool.acquire()
        if hasattr(pool, "session"):
            return pool.session()
        if hasattr(pool, "model"):
            return pool.model()
        raise AttributeError(f"Pool '{name}' has no acquire-style method")

    async def close_all(self):
        for name, pool in self._pools.items():
            if hasattr(pool, "close"):
                await pool.close()

    def all_stats(self) -> Dict[str, Any]:
        return {
            name: pool.stats()
            for name, pool in self._pools.items()
            if hasattr(pool, "stats")
        }
```

---

## Comparison

| Approach | Generic Resources | Browser Sessions | Tokenizers | NLP Models | Regex Patterns | Integrated |
|---|---|---|---|---|---|---|
| **GenericObjectPool** | Yes | No | No | No | No | No |
| **BrowserSessionPool** | No | Yes | No | No | No | No |
| **TokenizerPool** | No | No | Yes | No | No | No |
| **CompiledPatternCache** | No | No | No | No | Yes | No |
| **NLPModelPool** | No | No | No | Yes | No | No |
| **ResourcePoolRegistry** | Yes | Yes | No | Yes | No | Yes |

**Key insight**: size pools by `ceil(expected_concurrent_calls * p99_hold_time_s / mean_hold_time_s)` — a pool that is too small causes queuing; too large wastes memory on idle instances. For browser contexts, `size=4` handles most chat agent workloads. For tokenizers and NLP models, pool size equals the number of CPU cores dedicated to inference (usually 2–4). Always call `validate_fn` before lending a pooled object — a crashed browser context or a tokenizer that raised during a previous call should be discarded and recreated, not returned to callers.
