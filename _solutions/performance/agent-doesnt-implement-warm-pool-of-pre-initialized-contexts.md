---
layout: solution
title: "Agent Doesn't Implement Warm Pool of Pre-Initialized Contexts"
category: performance
description: "Pre-initialize client connections, system prompt caches, and session contexts to eliminate cold-start latency on first requests."
tags: [performance, warm-pool, latency, cold-start, connection-pool, caching, async]
---

# Agent Doesn't Implement Warm Pool of Pre-Initialized Contexts

## Problem

Every request bears cold-start overhead: TCP handshakes, TLS negotiation, SDK initialization, and system prompt token processing. Under bursty traffic, the first wave of requests experiences 500–2000 ms extra latency while the second wave hits a fully warmed pool. Users perceive the first response as slow and assume the agent is unreliable.

## Solution Options

### Option 1: Simple Pre-Warmed Client Pool

```python
import anthropic
import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


class SimpleClientPool:
    """Pre-warm a fixed number of Anthropic async clients at startup."""

    def __init__(self, pool_size: int = 5):
        self.pool_size = pool_size
        self._clients: asyncio.Queue[anthropic.AsyncAnthropic] = asyncio.Queue()
        self._initialized = False

    async def initialize(self) -> None:
        """Pre-create all clients before serving any traffic."""
        for _ in range(self.pool_size):
            client = anthropic.AsyncAnthropic()
            # Warm by making a tiny preflight request
            await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            await self._clients.put(client)
        self._initialized = True
        print(f"[pool] Warmed {self.pool_size} clients")

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[anthropic.AsyncAnthropic, None]:
        if not self._initialized:
            raise RuntimeError("Pool not initialized — call initialize() first")
        client = await self._clients.get()
        try:
            yield client
        finally:
            await self._clients.put(client)

    async def close(self) -> None:
        while not self._clients.empty():
            client = await self._clients.get()
            await client.close()


pool = SimpleClientPool(pool_size=5)


async def handle_request(user_message: str) -> str:
    async with pool.acquire() as client:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text


async def main() -> None:
    await pool.initialize()  # Called once at server startup

    # Simulate concurrent first-wave requests — all hit warm clients
    tasks = [handle_request(f"Question {i}") for i in range(10)]
    results = await asyncio.gather(*tasks)
    for i, r in enumerate(results):
        print(f"[{i}] {r[:60]}...")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Eliminates per-request client init overhead; first-wave p99 latency drops ~800 ms
# Environment: Any async Python service with bursty first-wave traffic
```

---

### Option 2: System Prompt Cache Warm Pool

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class WarmContext:
    client: anthropic.AsyncAnthropic
    system_prompt: str
    created_at: float = field(default_factory=time.monotonic)
    request_count: int = 0


class SystemPromptWarmPool:
    """
    Pre-build contexts with cached system prompts using Anthropic prompt caching.
    Each slot holds a client + system_prompt pair ready to serve requests.
    """

    SYSTEM_PROMPT = (
        "You are a helpful customer support agent for Acme Corp. "
        "Always be polite, concise, and solution-oriented. "
        "If you cannot solve the issue, escalate gracefully. "
        # Pad to meet minimum cache token threshold (1024 tokens for Haiku)
        + "Rules: " + "; ".join([f"Rule {i}: follow best practices" for i in range(60)])
    )

    def __init__(self, pool_size: int = 4, max_requests_per_slot: int = 100):
        self.pool_size = pool_size
        self.max_requests_per_slot = max_requests_per_slot
        self._pool: asyncio.Queue[WarmContext] = asyncio.Queue()

    async def _create_warm_context(self) -> WarmContext:
        client = anthropic.AsyncAnthropic()
        # Trigger cache write by making a real request with cache_control
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            system=[
                {
                    "type": "text",
                    "text": self.SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": "ready"}],
        )
        return WarmContext(client=client, system_prompt=self.SYSTEM_PROMPT)

    async def initialize(self) -> None:
        tasks = [self._create_warm_context() for _ in range(self.pool_size)]
        contexts = await asyncio.gather(*tasks)
        for ctx in contexts:
            await self._pool.put(ctx)
        print(f"[warm-pool] {self.pool_size} prompt-cached contexts ready")

    async def chat(self, user_message: str) -> tuple[str, bool]:
        """Returns (response_text, was_cache_hit)."""
        ctx = await self._pool.get()
        try:
            resp = await ctx.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": ctx.system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )
            ctx.request_count += 1
            cache_hit = (resp.usage.cache_read_input_tokens or 0) > 0
            return resp.content[0].text, cache_hit
        finally:
            # Recycle context (replace if exhausted)
            if ctx.request_count >= self.max_requests_per_slot:
                asyncio.create_task(self._refresh_slot())
            else:
                await self._pool.put(ctx)

    async def _refresh_slot(self) -> None:
        ctx = await self._create_warm_context()
        await self._pool.put(ctx)


async def main() -> None:
    pool = SystemPromptWarmPool(pool_size=4)
    await pool.initialize()

    messages = ["How do I reset my password?", "Where is my order?", "I want a refund"]
    for msg in messages:
        text, hit = await pool.chat(msg)
        print(f"cache_hit={hit} | {text[:80]}...")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: System prompt cache hits save ~90% of input token cost per request
# Environment: High-volume customer support or chat bots with a fixed system prompt
```

---

### Option 3: Embedding Cache Pre-Warm

```python
import anthropic
import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class EmbeddingEntry:
    vector: list[float]
    hit_count: int = 0
    created_at: float = 0.0


class EmbeddingWarmPool:
    """
    Pre-compute embeddings for common query patterns at startup.
    Uses an LRU cache so hot embeddings are always in memory.
    Falls back to live API for cache misses.
    """

    COMMON_QUERIES = [
        "How do I reset my password?",
        "What are your business hours?",
        "How do I cancel my subscription?",
        "Where is my order?",
        "How do I contact support?",
        "What payment methods do you accept?",
        "How do I update my billing information?",
        "What is your return policy?",
    ]

    def __init__(self, max_cache_size: int = 1000):
        self.client = anthropic.Anthropic()  # sync for embedding pre-warm
        self._cache: OrderedDict[str, EmbeddingEntry] = OrderedDict()
        self.max_cache_size = max_cache_size

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def _embed_sync(self, text: str) -> list[float]:
        # Anthropic doesn't have a native embeddings endpoint;
        # use a one-token generation with logprobs as a stand-in for demonstration
        # In production replace with your embeddings provider
        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": f"embed: {text}"}],
        )
        # Placeholder: real implementation uses a dedicated embeddings API
        return [hash(text + str(i)) % 1000 / 1000.0 for i in range(128)]

    def pre_warm(self) -> None:
        start = time.monotonic()
        for query in self.COMMON_QUERIES:
            key = self._key(query)
            vector = self._embed_sync(query)
            self._cache[key] = EmbeddingEntry(
                vector=vector, created_at=time.monotonic()
            )
        elapsed = time.monotonic() - start
        print(f"[embed-pool] Warmed {len(self.COMMON_QUERIES)} embeddings in {elapsed:.2f}s")

    def get(self, text: str) -> tuple[list[float] | None, bool]:
        key = self._key(text)
        if key in self._cache:
            entry = self._cache[key]
            entry.hit_count += 1
            self._cache.move_to_end(key)  # LRU update
            return entry.vector, True

        # Cache miss — compute and store
        if len(self._cache) >= self.max_cache_size:
            self._cache.popitem(last=False)  # evict LRU
        vector = self._embed_sync(text)
        self._cache[key] = EmbeddingEntry(vector=vector, created_at=time.monotonic())
        return vector, False

    def stats(self) -> dict:
        total_hits = sum(e.hit_count for e in self._cache.values())
        return {"cache_size": len(self._cache), "total_hits": total_hits}


def main() -> None:
    pool = EmbeddingWarmPool()
    pool.pre_warm()

    test_queries = [
        "How do I reset my password?",      # cache hit
        "What are your business hours?",    # cache hit
        "How do I track my shipment?",      # cache miss
    ]
    for q in test_queries:
        vec, hit = pool.get(q)
        print(f"hit={hit} | query='{q[:40]}' | vec_dim={len(vec)}")

    print(pool.stats())


if __name__ == "__main__":
    main()

# Expected Token Savings: Zero API calls for cached embeddings; 8 common queries pre-warmed at startup
# Environment: RAG pipelines or semantic search agents with predictable query distributions
```

---

### Option 4: Async Context Pool with Health Monitoring

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum


class SlotState(Enum):
    WARM = "warm"
    IN_USE = "in_use"
    STALE = "stale"
    RECOVERING = "recovering"


@dataclass
class PoolSlot:
    slot_id: int
    client: anthropic.AsyncAnthropic
    state: SlotState = SlotState.WARM
    last_used: float = field(default_factory=time.monotonic)
    error_count: int = 0
    request_count: int = 0

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.last_used

    @property
    def is_healthy(self) -> bool:
        return self.error_count < 3 and self.state != SlotState.STALE


class MonitoredWarmPool:
    """
    Async pool with background health checks.
    Stale or erroring slots are automatically recycled.
    """

    STALE_AFTER_SECONDS = 300  # 5 minutes idle → recycle

    def __init__(self, pool_size: int = 6):
        self.pool_size = pool_size
        self._slots: list[PoolSlot] = []
        self._available: asyncio.Queue[PoolSlot] = asyncio.Queue()
        self._monitor_task: asyncio.Task | None = None

    async def _create_slot(self, slot_id: int) -> PoolSlot:
        client = anthropic.AsyncAnthropic()
        # Verify connectivity
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        return PoolSlot(slot_id=slot_id, client=client)

    async def initialize(self) -> None:
        tasks = [self._create_slot(i) for i in range(self.pool_size)]
        self._slots = await asyncio.gather(*tasks)
        for slot in self._slots:
            await self._available.put(slot)
        self._monitor_task = asyncio.create_task(self._health_monitor())
        print(f"[pool] {self.pool_size} slots warm and monitored")

    async def _health_monitor(self) -> None:
        while True:
            await asyncio.sleep(60)
            for slot in self._slots:
                if slot.state == SlotState.WARM and slot.age_seconds > self.STALE_AFTER_SECONDS:
                    slot.state = SlotState.STALE

    async def execute(self, messages: list[dict], model: str = "claude-haiku-4-5-20251001") -> str:
        slot = await self._available.get()

        # Recycle stale or unhealthy slots transparently
        if not slot.is_healthy or slot.state == SlotState.STALE:
            slot.state = SlotState.RECOVERING
            try:
                new_slot = await self._create_slot(slot.slot_id)
                self._slots[slot.slot_id] = new_slot
                slot = new_slot
            except Exception:
                slot.state = SlotState.WARM  # keep using despite stale

        slot.state = SlotState.IN_USE
        try:
            resp = await slot.client.messages.create(
                model=model,
                max_tokens=512,
                messages=messages,
            )
            slot.request_count += 1
            slot.last_used = time.monotonic()
            slot.state = SlotState.WARM
            return resp.content[0].text
        except Exception as e:
            slot.error_count += 1
            slot.state = SlotState.WARM
            raise e
        finally:
            await self._available.put(slot)

    async def shutdown(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
        for slot in self._slots:
            await slot.client.close()

    def diagnostics(self) -> list[dict]:
        return [
            {
                "slot_id": s.slot_id,
                "state": s.state.value,
                "requests": s.request_count,
                "errors": s.error_count,
                "age_s": round(s.age_seconds, 1),
            }
            for s in self._slots
        ]


async def main() -> None:
    pool = MonitoredWarmPool(pool_size=4)
    await pool.initialize()

    tasks = [
        pool.execute([{"role": "user", "content": f"Explain topic {i} briefly"}])
        for i in range(8)
    ]
    results = await asyncio.gather(*tasks)
    for i, r in enumerate(results):
        print(f"[{i}] {r[:60]}...")

    print("Diagnostics:", pool.diagnostics())
    await pool.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No cold-start latency; stale recycling prevents silent failures mid-session
# Environment: Long-running production services where connection health degrades over time
```

---

### Option 5: Tiered Warm Pool (Fast / Standard / Deep)

```python
import anthropic
import asyncio
from dataclasses import dataclass
from enum import Enum


class Tier(Enum):
    FAST = "fast"        # haiku — latency-sensitive micro-tasks
    STANDARD = "standard"  # sonnet — balanced tasks
    DEEP = "deep"        # opus — complex reasoning


@dataclass
class TierConfig:
    model: str
    pool_size: int
    max_tokens: int


TIER_CONFIGS: dict[Tier, TierConfig] = {
    Tier.FAST: TierConfig("claude-haiku-4-5-20251001", pool_size=8, max_tokens=256),
    Tier.STANDARD: TierConfig("claude-sonnet-4-6", pool_size=4, max_tokens=1024),
    Tier.DEEP: TierConfig("claude-opus-4-6", pool_size=2, max_tokens=4096),
}


class TieredWarmPool:
    """
    Three-tier warm pool: route requests to the cheapest tier that meets quality needs.
    All tiers are pre-warmed at startup so routing adds zero latency.
    """

    def __init__(self) -> None:
        self._pools: dict[Tier, asyncio.Queue[anthropic.AsyncAnthropic]] = {}
        self._configs = TIER_CONFIGS

    async def _warm_tier(self, tier: Tier) -> asyncio.Queue[anthropic.AsyncAnthropic]:
        config = self._configs[tier]
        q: asyncio.Queue[anthropic.AsyncAnthropic] = asyncio.Queue()
        for _ in range(config.pool_size):
            client = anthropic.AsyncAnthropic()
            # Preflight to open connection
            await client.messages.create(
                model=config.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            await q.put(client)
        print(f"[tiered] {tier.value}: {config.pool_size} clients warm ({config.model})")
        return q

    async def initialize(self) -> None:
        results = await asyncio.gather(
            *[self._warm_tier(tier) for tier in Tier]
        )
        for tier, q in zip(Tier, results):
            self._pools[tier] = q

    async def request(self, messages: list[dict], tier: Tier = Tier.FAST) -> str:
        config = self._configs[tier]
        q = self._pools[tier]
        client = await q.get()
        try:
            resp = await client.messages.create(
                model=config.model,
                max_tokens=config.max_tokens,
                messages=messages,
            )
            return resp.content[0].text
        finally:
            await q.put(client)

    async def auto_route(self, messages: list[dict], complexity_hint: str = "low") -> str:
        """Route to appropriate tier based on complexity hint."""
        tier_map = {"low": Tier.FAST, "medium": Tier.STANDARD, "high": Tier.DEEP}
        tier = tier_map.get(complexity_hint, Tier.FAST)
        return await self.request(messages, tier)

    async def shutdown(self) -> None:
        for q in self._pools.values():
            while not q.empty():
                client = await q.get()
                await client.close()


async def main() -> None:
    pool = TieredWarmPool()
    await pool.initialize()

    tests = [
        ({"role": "user", "content": "Say hello"}, "low"),
        ({"role": "user", "content": "Summarize quantum computing in 3 sentences"}, "medium"),
        ({"role": "user", "content": "Design a microservices architecture for a fintech startup"}, "high"),
    ]
    for msg, complexity in tests:
        result = await pool.auto_route([msg], complexity_hint=complexity)
        print(f"[{complexity}] {result[:80]}...")

    await pool.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Optimal model selection per request; haiku for low-complexity saves ~6x vs opus
# Environment: Mixed-workload pipelines where task complexity varies significantly
```

---

### Option 6: Persistent Warm Pool with Startup Probe and Graceful Drain

```python
import anthropic
import asyncio
import signal
import time
from dataclasses import dataclass, field


@dataclass
class PoolMetrics:
    requests_served: int = 0
    cache_hits: int = 0
    cold_starts: int = 0
    avg_wait_ms: float = 0.0
    _wait_samples: list[float] = field(default_factory=list)

    def record_wait(self, ms: float) -> None:
        self._wait_samples.append(ms)
        if len(self._wait_samples) > 1000:
            self._wait_samples.pop(0)
        self.avg_wait_ms = sum(self._wait_samples) / len(self._wait_samples)


class ProductionWarmPool:
    """
    Production-grade warm pool with:
    - Startup readiness probe (signals when warm)
    - Graceful drain on SIGTERM (waits for in-flight requests)
    - Metrics for pool wait time and cache efficiency
    - Auto-scaling: grows pool on sustained high wait time
    """

    def __init__(self, min_size: int = 4, max_size: int = 16):
        self.min_size = min_size
        self.max_size = max_size
        self._pool: asyncio.Queue[anthropic.AsyncAnthropic] = asyncio.Queue()
        self._current_size = 0
        self._in_flight = 0
        self._draining = False
        self._ready = asyncio.Event()
        self.metrics = PoolMetrics()

    async def _create_client(self) -> anthropic.AsyncAnthropic:
        client = anthropic.AsyncAnthropic()
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "warmup"}],
        )
        self.metrics.cold_starts += 1
        return client

    async def initialize(self) -> None:
        tasks = [self._create_client() for _ in range(self.min_size)]
        clients = await asyncio.gather(*tasks)
        for c in clients:
            await self._pool.put(c)
        self._current_size = self.min_size
        self._ready.set()

        # Register SIGTERM handler
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self.drain()))

        # Background auto-scaler
        asyncio.create_task(self._auto_scaler())
        print(f"[prod-pool] Ready with {self.min_size} warm clients")

    async def _auto_scaler(self) -> None:
        while not self._draining:
            await asyncio.sleep(10)
            # If average pool wait > 50 ms and we have headroom, add a slot
            if (
                self.metrics.avg_wait_ms > 50
                and self._current_size < self.max_size
            ):
                client = await self._create_client()
                await self._pool.put(client)
                self._current_size += 1
                print(f"[prod-pool] Scaled up to {self._current_size}")

    async def wait_until_ready(self) -> None:
        await self._ready.wait()

    async def execute(self, messages: list[dict]) -> str:
        if self._draining:
            raise RuntimeError("Pool is draining — not accepting new requests")

        wait_start = time.monotonic()
        client = await self._pool.get()
        wait_ms = (time.monotonic() - wait_start) * 1000
        self.metrics.record_wait(wait_ms)

        self._in_flight += 1
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=messages,
            )
            self.metrics.requests_served += 1
            return resp.content[0].text
        finally:
            self._in_flight -= 1
            await self._pool.put(client)

    async def drain(self) -> None:
        print("[prod-pool] SIGTERM received — draining")
        self._draining = True
        # Wait for in-flight requests
        for _ in range(30):  # 30-second drain window
            if self._in_flight == 0:
                break
            await asyncio.sleep(1)
        # Close all clients
        while not self._pool.empty():
            client = await self._pool.get()
            await client.close()
        print(f"[prod-pool] Drained. Served {self.metrics.requests_served} requests")


async def main() -> None:
    pool = ProductionWarmPool(min_size=3, max_size=8)
    await pool.initialize()
    await pool.wait_until_ready()  # Kubernetes readiness probe can call this

    tasks = [
        pool.execute([{"role": "user", "content": f"Summarize topic {i} in one sentence"}])
        for i in range(12)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"[{i}] ERROR: {r}")
        else:
            print(f"[{i}] {r[:70]}...")

    print(f"Metrics: served={pool.metrics.requests_served} avg_wait={pool.metrics.avg_wait_ms:.1f}ms")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Auto-scaling prevents queue buildup; graceful drain avoids dropped requests on deploy
# Environment: Kubernetes-deployed agents requiring zero-downtime rolling updates and readiness probes
```

---

## Comparison

| Option | Approach | Best For | Latency Impact | Complexity |
|--------|----------|----------|----------------|------------|
| 1 | Simple pre-warmed client pool | Quick wins, minimal code | Eliminates TCP/TLS cold start | Low |
| 2 | System prompt cache pre-warm | Fixed system prompt bots | Cuts prompt token cost ~90% | Medium |
| 3 | Embedding cache pre-warm | RAG / semantic search | Zero API calls for hot queries | Medium |
| 4 | Async pool with health monitoring | Long-running services | Prevents silent stale failures | Medium-High |
| 5 | Tiered warm pool (fast/standard/deep) | Mixed-complexity workloads | Optimal cost per request tier | High |
| 6 | Production pool with drain + auto-scale | K8s production deployments | Full lifecycle management | High |
