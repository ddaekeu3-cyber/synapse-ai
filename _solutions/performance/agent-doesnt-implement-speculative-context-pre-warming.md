---
title: "Agent Doesn't Implement Speculative Context Pre-Warming"
description: "The first request to a cold agent pays full KV-cache construction cost. Speculative pre-warming pre-processes stable context (system prompt, tool schemas, background docs) before requests arrive, reducing time-to-first-token on every conversation."
difficulty: intermediate
category: performance
tags: [pre-warming, kv-cache, latency, cold-start, speculative, performance, throughput]
---

## Problem

When a new agent conversation begins, the LLM API must process the entire system prompt, tool definitions, and background context from scratch before generating the first token. For a 10,000-token system prompt, this adds 300–800ms of latency to every new conversation. Pre-warming eliminates this by processing stable content ahead of time.

```python
# Broken: cold start on every new conversation
async def new_conversation(user_message: str) -> str:
    # All 10,000 tokens of system prompt processed from scratch
    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=LARGE_SYSTEM_PROMPT,   # 10k tokens, cold every time
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text
```

---

## Solution 1: Prompt Cache Priming with Background Task

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

SYSTEM_PROMPT_BLOCKS = [
    {
        "type": "text",
        "text": "You are an expert AI assistant...\n[2000 tokens of persona]",
        "cache_control": {"type": "ephemeral"}
    },
    {
        "type": "text",
        "text": "[Tool documentation — 3000 tokens]",
        "cache_control": {"type": "ephemeral"}
    },
    {
        "type": "text",
        "text": "[Background knowledge base — 5000 tokens]",
        "cache_control": {"type": "ephemeral"}
    }
]

async def prime_prompt_cache():
    """
    Send a minimal 'warm-up' request that processes the full system prompt
    and stores it in Anthropic's prompt cache. TTL: 5 minutes.
    Subsequent requests within that window get ~90% token cost reduction.
    """
    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1,          # minimal output — just warming the cache
        system=SYSTEM_PROMPT_BLOCKS,
        messages=[{"role": "user", "content": "ping"}]
    )
    usage = response.usage
    created = getattr(usage, "cache_creation_input_tokens", 0)
    read = getattr(usage, "cache_read_input_tokens", 0)
    print(f"[CacheWarm] Created={created} Read={read} "
          f"Input={usage.input_tokens}")
    return created > 0  # True if cache was populated

async def cache_refresh_loop(interval: float = 240.0):
    """
    Re-prime the cache every `interval` seconds (before the 5-minute TTL expires).
    Run as a background task.
    """
    while True:
        try:
            warmed = await prime_prompt_cache()
            print(f"[CacheWarm] Refresh {'OK' if warmed else 'cache already warm'}")
        except Exception as e:
            print(f"[CacheWarm] Refresh failed: {e}")
        await asyncio.sleep(interval)

async def new_conversation_warm(user_message: str) -> str:
    """Every request benefits from pre-warmed cache."""
    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT_BLOCKS,   # reads from cache — near-zero cost
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text

# Startup sequence
async def startup():
    # 1. Warm cache before accepting traffic
    await prime_prompt_cache()
    # 2. Start background refresh loop
    asyncio.create_task(cache_refresh_loop(interval=240.0))
    print("[Startup] Cache warm, accepting traffic")
```

---

## Solution 2: Parallel Pre-Warming for Multiple System Prompt Variants

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Any

client = AsyncAnthropic()

@dataclass
class PromptVariant:
    variant_id: str
    system_blocks: list[dict]
    model: str = "claude-opus-4-6"
    warmed: bool = False
    last_warmed_at: float = 0.0

class MultiVariantCacheWarmer:
    """
    Pre-warm multiple prompt variants in parallel.
    Useful for A/B tested prompts, per-tenant system prompts,
    or multiple model tiers.
    """

    def __init__(self, variants: list[PromptVariant],
                 refresh_interval: float = 240.0):
        self._variants = {v.variant_id: v for v in variants}
        self._refresh_interval = refresh_interval

    async def warm_variant(self, variant: PromptVariant) -> bool:
        """Warm a single prompt variant."""
        import time
        try:
            response = await client.messages.create(
                model=variant.model,
                max_tokens=1,
                system=variant.system_blocks,
                messages=[{"role": "user", "content": "ping"}]
            )
            usage = response.usage
            created = getattr(usage, "cache_creation_input_tokens", 0)
            variant.warmed = True
            variant.last_warmed_at = time.monotonic()
            print(f"[MultiWarm] Variant '{variant.variant_id}' warmed "
                  f"({created} tokens cached)")
            return True
        except Exception as e:
            print(f"[MultiWarm] Failed to warm '{variant.variant_id}': {e}")
            return False

    async def warm_all(self) -> dict[str, bool]:
        """Warm all variants concurrently."""
        results = await asyncio.gather(*[
            self.warm_variant(v) for v in self._variants.values()
        ], return_exceptions=True)
        return {
            vid: (r is True)
            for vid, r in zip(self._variants, results)
        }

    async def refresh_loop(self):
        """Periodically refresh all variant caches."""
        while True:
            await asyncio.sleep(self._refresh_interval)
            import time
            now = time.monotonic()
            stale = [
                v for v in self._variants.values()
                if now - v.last_warmed_at > self._refresh_interval
            ]
            if stale:
                await asyncio.gather(*[self.warm_variant(v) for v in stale])

    def get_warmed_system(self, variant_id: str) -> list[dict] | None:
        v = self._variants.get(variant_id)
        if v and v.warmed:
            return v.system_blocks
        return None

# Usage
async def demo_multi_warm():
    warmer = MultiVariantCacheWarmer([
        PromptVariant("default", [{"type": "text", "text": "Default system...",
                                    "cache_control": {"type": "ephemeral"}}]),
        PromptVariant("haiku", [{"type": "text", "text": "Concise system...",
                                  "cache_control": {"type": "ephemeral"}}],
                       model="claude-haiku-4-5-20251001"),
    ])
    results = await warmer.warm_all()
    print(f"Warm results: {results}")
```

---

## Solution 3: Predictive Pre-Warming Based on User Patterns

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

@dataclass
class UserSessionPattern:
    user_id: str
    typical_system_prompt_id: str
    peak_hours: list[int]   # hours of day (0-23) user is most active
    avg_gap_minutes: float  # average minutes between conversations

class PredictiveWarmer:
    """
    Pre-warms caches based on predicted user activity.
    For a user who typically starts sessions at 9am, warms at 8:55am.
    """

    def __init__(self, patterns: list[UserSessionPattern],
                 warm_ahead_seconds: float = 120.0):
        self._patterns = {p.user_id: p for p in patterns}
        self._warm_ahead = warm_ahead_seconds
        self._warmed_prompts: dict[str, float] = {}  # prompt_id → last_warmed

    async def warm_for_user(self, user_id: str,
                             system_blocks_fn,  # (prompt_id) -> list[dict]
                             warm_fn):          # (system_blocks) -> None
        pattern = self._patterns.get(user_id)
        if not pattern:
            return
        prompt_id = pattern.typical_system_prompt_id
        last_warmed = self._warmed_prompts.get(prompt_id, 0.0)
        if time.monotonic() - last_warmed < 200.0:
            return  # recently warmed, skip
        blocks = system_blocks_fn(prompt_id)
        await warm_fn(blocks)
        self._warmed_prompts[prompt_id] = time.monotonic()
        print(f"[PredictiveWarm] Pre-warmed '{prompt_id}' for user '{user_id}'")

    def should_pre_warm_now(self, user_id: str) -> bool:
        """Check if the current time matches the user's predicted activity window."""
        import datetime
        pattern = self._patterns.get(user_id)
        if not pattern:
            return False
        now_hour = datetime.datetime.now().hour
        return now_hour in pattern.peak_hours

class ActivityBasedWarmer:
    """
    Observe incoming request patterns and pre-warm the most frequently
    used system prompts before traffic peaks.
    """

    def __init__(self, warm_fn, top_n: int = 3):
        self._usage_counts: dict[str, int] = defaultdict(int)
        self._warm_fn = warm_fn
        self._top_n = top_n
        self._prompt_registry: dict[str, list[dict]] = {}

    def record_usage(self, prompt_id: str):
        self._usage_counts[prompt_id] += 1

    def register_prompt(self, prompt_id: str, blocks: list[dict]):
        self._prompt_registry[prompt_id] = blocks

    async def warm_top_prompts(self) -> int:
        """Warm the N most-used prompts."""
        top = sorted(self._usage_counts, key=self._usage_counts.get,
                     reverse=True)[:self._top_n]
        warmed = 0
        for pid in top:
            blocks = self._prompt_registry.get(pid)
            if blocks:
                await self._warm_fn(blocks)
                warmed += 1
        return warmed

    async def scheduled_warm_loop(self, interval: float = 180.0):
        while True:
            await asyncio.sleep(interval)
            n = await self.warm_top_prompts()
            print(f"[ActivityWarm] Pre-warmed {n} prompts based on usage patterns")
```

---

## Solution 4: Tiered Warming — Static + Dynamic Layers

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class TieredPromptCache:
    """
    Split system prompt into stable (warm once) and dynamic (per-session) parts.
    Stable part: persona, tool definitions, background docs
    Dynamic part: user-specific context, today's date, session info
    """
    stable_blocks: list[dict]   # cached, refreshed every 4 minutes
    dynamic_template: str       # formatted per-request, NOT cached

    def build_system(self, **dynamic_vars) -> list[dict]:
        """Combine pre-warmed stable blocks with fresh dynamic content."""
        dynamic_text = self.dynamic_template.format(**dynamic_vars)
        return self.stable_blocks + [{
            "type": "text",
            "text": dynamic_text
            # No cache_control — dynamic content is unique per request
        }]

async def warm_stable_layer(prompt_cache: TieredPromptCache,
                              model: str = "claude-opus-4-6") -> dict:
    """Pre-process only the stable layer."""
    response = await client.messages.create(
        model=model,
        max_tokens=1,
        system=prompt_cache.stable_blocks,
        messages=[{"role": "user", "content": "ping"}]
    )
    usage = response.usage
    return {
        "cached_tokens": getattr(usage, "cache_creation_input_tokens", 0),
        "read_tokens": getattr(usage, "cache_read_input_tokens", 0),
    }

async def new_session_with_tiered_cache(
    prompt_cache: TieredPromptCache,
    user_message: str,
    user_id: str,
    model: str = "claude-opus-4-6"
) -> str:
    """Start a conversation using pre-warmed stable layer + fresh dynamic layer."""
    import datetime
    system = prompt_cache.build_system(
        user_id=user_id,
        today=datetime.date.today().isoformat(),
        session_start=datetime.datetime.now().strftime("%H:%M"),
    )
    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text

# Example construction
def build_agent_prompt_cache() -> TieredPromptCache:
    stable = [
        {
            "type": "text",
            "text": "You are a highly capable AI assistant...\n[persona: 1500 tokens]",
            "cache_control": {"type": "ephemeral"}
        },
        {
            "type": "text",
            "text": "[Tool documentation: 3000 tokens]",
            "cache_control": {"type": "ephemeral"}
        },
        {
            "type": "text",
            "text": "[Knowledge base: 4000 tokens]",
            "cache_control": {"type": "ephemeral"}
        }
    ]
    dynamic_template = (
        "Current date: {today}\n"
        "Session started: {session_start}\n"
        "User ID: {user_id}\n"
        "Please greet the user and assist them with their requests."
    )
    return TieredPromptCache(stable_blocks=stable,
                              dynamic_template=dynamic_template)
```

---

## Solution 5: Pre-Warm Pool with Request-Aware Routing

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class WarmSlot:
    """A pre-warmed context slot ready to serve a new request."""
    slot_id: str
    prompt_variant_id: str
    warmed_at: float = field(default_factory=time.monotonic)
    in_use: bool = False
    ttl: float = 240.0  # seconds before considered stale

    @property
    def is_stale(self) -> bool:
        return time.monotonic() - self.warmed_at > self.ttl

class WarmSlotPool:
    """
    Maintains a pool of pre-warmed context slots.
    When a new conversation arrives, it claims a warm slot immediately
    (avoiding cold start) and the pool refills in the background.
    """

    def __init__(self, variant_id: str, pool_size: int = 3,
                 warm_fn=None):
        self._variant_id = variant_id
        self._pool_size = pool_size
        self._warm_fn = warm_fn
        self._slots: list[WarmSlot] = []
        self._lock = asyncio.Lock()
        self._fill_event = asyncio.Event()

    async def initialize(self):
        """Pre-fill pool at startup."""
        import uuid
        for i in range(self._pool_size):
            await self._warm_fn(self._variant_id)
            self._slots.append(WarmSlot(
                slot_id=str(uuid.uuid4())[:8],
                prompt_variant_id=self._variant_id
            ))
        print(f"[WarmPool] Initialized {self._pool_size} slots "
              f"for '{self._variant_id}'")

    async def claim(self) -> WarmSlot | None:
        """Claim a warm slot (non-blocking). Returns None if pool exhausted."""
        async with self._lock:
            available = [s for s in self._slots
                         if not s.in_use and not s.is_stale]
            if not available:
                return None
            slot = available[0]
            slot.in_use = True
            return slot

    async def release(self, slot: WarmSlot):
        """Return a slot to the pool and trigger background refill."""
        async with self._lock:
            self._slots.remove(slot)
        # Refill one slot asynchronously
        asyncio.create_task(self._refill_one())

    async def _refill_one(self):
        """Add one fresh warm slot to the pool."""
        import uuid
        try:
            await self._warm_fn(self._variant_id)
            async with self._lock:
                if len(self._slots) < self._pool_size:
                    self._slots.append(WarmSlot(
                        slot_id=str(uuid.uuid4())[:8],
                        prompt_variant_id=self._variant_id,
                    ))
        except Exception as e:
            print(f"[WarmPool] Refill failed: {e}")

    def stats(self) -> dict:
        return {
            "variant": self._variant_id,
            "pool_size": len(self._slots),
            "available": sum(1 for s in self._slots
                             if not s.in_use and not s.is_stale),
            "in_use": sum(1 for s in self._slots if s.in_use),
            "stale": sum(1 for s in self._slots if s.is_stale),
        }
```

---

## Solution 6: Cost Savings Calculator and Warm Scheduler

```python
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class CacheWarmingMetrics:
    """Track the economic impact of pre-warming."""
    total_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    tokens_saved: int = 0
    warm_cost_tokens: int = 0  # tokens spent on warm-up pings

    # Pricing (per million tokens, USD)
    full_input_cost_per_mtok: float = 15.0   # Claude Opus input
    cache_read_cost_per_mtok: float = 1.50   # 90% discount
    cache_write_cost_per_mtok: float = 18.75 # 25% surcharge

    def record_request(self, system_tokens: int, was_cached: bool):
        self.total_requests += 1
        if was_cached:
            self.cache_hits += 1
            self.tokens_saved += system_tokens
        else:
            self.cache_misses += 1

    def record_warm_ping(self, tokens_written: int):
        self.warm_cost_tokens += tokens_written

    @property
    def hit_rate(self) -> float:
        return self.cache_hits / max(1, self.total_requests)

    def cost_analysis(self) -> dict:
        """Compare actual cost vs hypothetical no-warming cost."""
        system_tokens = self.tokens_saved + (self.cache_misses * 8500)  # avg system size

        # Cost without warming: every request pays full price
        no_warm_cost = system_tokens / 1_000_000 * self.full_input_cost_per_mtok

        # Cost with warming: hits pay cache_read, misses pay cache_write for pings
        warm_cost = (
            self.tokens_saved / 1_000_000 * self.cache_read_cost_per_mtok +
            self.warm_cost_tokens / 1_000_000 * self.cache_write_cost_per_mtok
        )

        return {
            "total_requests": self.total_requests,
            "cache_hit_rate": round(self.hit_rate, 3),
            "tokens_saved_via_cache": self.tokens_saved,
            "cost_without_warming_usd": round(no_warm_cost, 4),
            "cost_with_warming_usd": round(warm_cost, 4),
            "net_savings_usd": round(no_warm_cost - warm_cost, 4),
            "roi_pct": round(
                (no_warm_cost - warm_cost) / max(0.0001, no_warm_cost) * 100, 1
            ),
        }

class AdaptiveWarmScheduler:
    """
    Adjusts warm-up frequency based on observed hit rates.
    High hit rate → keep current interval.
    Low hit rate → warm more frequently (cache TTL may be expiring).
    """

    def __init__(self, warm_fn, metrics: CacheWarmingMetrics,
                 min_interval: float = 60.0,
                 max_interval: float = 240.0):
        self._warm_fn = warm_fn
        self._metrics = metrics
        self._min_interval = min_interval
        self._max_interval = max_interval
        self._current_interval = max_interval

    async def run(self):
        while True:
            await asyncio.sleep(self._current_interval)
            await self._warm_fn()
            # Adjust interval based on hit rate
            if self._metrics.hit_rate < 0.8:
                # Hit rate too low — warm more frequently
                self._current_interval = max(
                    self._min_interval,
                    self._current_interval * 0.8
                )
            else:
                # Good hit rate — can warm less frequently
                self._current_interval = min(
                    self._max_interval,
                    self._current_interval * 1.1
                )
            print(f"[AdaptiveWarm] Hit rate={self._metrics.hit_rate:.1%}, "
                  f"next warm in {self._current_interval:.0f}s")
```

---

## Comparison

| Solution | Scope | Complexity | Handles Multi-Variant | Predictive | Cost Tracking | Best For |
|---|---|---|---|---|---|---|
| 1. Single prompt refresh loop | One prompt | Low | No | No | No | Simple single-variant agents |
| 2. Parallel multi-variant warming | Many prompts | Med | Yes | No | No | A/B test or multi-tenant agents |
| 3. Predictive + activity-based | User-specific | Med | Yes | Yes | No | Known traffic patterns |
| 4. Tiered stable/dynamic | One prompt | Low | No | No | No | Mixed static+dynamic system prompts |
| 5. Warm slot pool | One variant | High | Partial | No | No | Extremely latency-sensitive |
| 6. Cost calculator + adaptive scheduler | Any | Med | No | Partial | Yes | ROI measurement, dynamic scheduling |

**Key principle**: the Anthropic prompt cache TTL is ~5 minutes. A background refresh every 4 minutes ensures near-100% cache hit rate for high-traffic agents. For low-traffic agents (< 1 req/min), pre-warming is counterproductive — cache will expire between requests and you pay write surcharge for nothing. The adaptive scheduler (solution 6) automatically finds the right interval by observing hit rates.
