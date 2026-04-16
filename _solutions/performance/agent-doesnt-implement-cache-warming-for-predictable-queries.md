---
layout: solution
title: "Agent Doesn't Implement Cache Warming for Predictable Queries"
category: performance
description: "Pre-populate the response cache with answers to predictable, high-frequency queries before traffic arrives—eliminating cold-start latency and reducing model calls during peak load."
tags: [caching, cache-warming, performance, predictability, precomputation]
---

# Agent Doesn't Implement Cache Warming for Predictable Queries

## Problem

Response caches start cold when agents deploy or restart. The first users after each deployment experience full model latency on common queries that could have been pre-answered. Without warming, cache hit rates stay low during peak traffic periods despite a warm cache being achievable.

## Solution Options

### Option 1: Static Warm-Up List with TTL Cache

```python
import anthropic
import time
import hashlib
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# Predicted high-frequency queries to pre-cache
WARMUP_QUERIES = [
    "What are your pricing plans?",
    "How do I reset my password?",
    "What payment methods do you accept?",
    "How do I cancel my subscription?",
    "What is your refund policy?",
    "How do I contact support?",
    "What are your business hours?",
    "How do I upgrade my plan?"
]

@dataclass
class CacheEntry:
    response: str
    cached_at: float
    ttl_seconds: int
    hit_count: int = 0

    def is_expired(self) -> bool:
        return time.time() - self.cached_at > self.ttl_seconds

CACHE: dict[str, CacheEntry] = {}

def cache_key(query: str) -> str:
    return hashlib.md5(query.strip().lower().encode()).hexdigest()

def get_cached(query: str) -> str | None:
    key = cache_key(query)
    entry = CACHE.get(key)
    if entry and not entry.is_expired():
        entry.hit_count += 1
        return entry.response
    return None

def cache_response(query: str, response: str, ttl: int = 3600) -> None:
    CACHE[cache_key(query)] = CacheEntry(
        response=response,
        cached_at=time.time(),
        ttl_seconds=ttl
    )

def warm_cache(queries: list[str], model: str = "claude-haiku-4-5-20251001") -> int:
    warmed = 0
    print(f"Warming cache with {len(queries)} queries...")
    for query in queries:
        if get_cached(query):
            print(f"  [SKIP] Already cached: {query[:50]}")
            continue
        resp = client.messages.create(
            model=model,
            max_tokens=256,
            system="You are a helpful customer support assistant for Acme Corp.",
            messages=[{"role": "user", "content": query}]
        )
        cache_response(query, resp.content[0].text)
        warmed += 1
        print(f"  [WARMED] {query[:50]}")
    return warmed

def ask(query: str) -> tuple[str, bool]:
    cached = get_cached(query)
    if cached:
        return cached, True
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are a helpful customer support assistant.",
        messages=[{"role": "user", "content": query}]
    )
    response = resp.content[0].text
    cache_response(query, response)
    return response, False

# Warm on startup
warmed = warm_cache(WARMUP_QUERIES)
print(f"\nWarmed {warmed} entries\n")

# Simulate incoming requests
test_queries = [
    "What are your pricing plans?",           # should hit cache
    "How do I reset my password?",            # should hit cache
    "Can I pay with cryptocurrency?",         # cache miss — model call needed
]

for q in test_queries:
    t0 = time.perf_counter()
    response, was_cached = ask(q)
    latency = round((time.perf_counter() - t0) * 1000, 1)
    source = "CACHE" if was_cached else "MODEL"
    print(f"[{source} {latency}ms] {q}: {response[:60]}...")

total_hits = sum(e.hit_count for e in CACHE.values())
print(f"\nCache size: {len(CACHE)} entries | Total hits: {total_hits}")

# Expected Token Savings: cache hits eliminate model calls entirely; ~100% savings on cached queries
# Environment: customer support bots, FAQ agents, high-traffic consumer products
```

### Option 2: Usage-Pattern-Driven Warm-Up from Query History

```python
import anthropic
import time
import hashlib
from collections import Counter
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# Simulated query history log (in production, read from your analytics DB)
HISTORICAL_QUERIES = [
    "How do I install the SDK?",
    "What is the rate limit?",
    "How do I install the SDK?",
    "How do I authenticate?",
    "What is the rate limit?",
    "How do I install the SDK?",
    "How do I install the SDK?",
    "How do I handle errors?",
    "What is the rate limit?",
    "How do I paginate results?",
    "How do I install the SDK?",
    "What models are available?",
    "How do I authenticate?",
    "How do I authenticate?",
    "What is the rate limit?",
]

@dataclass
class WarmupPlan:
    top_queries: list[tuple[str, int]]  # (query, frequency)
    total_queries: int
    coverage_pct: float

def build_warmup_plan(history: list[str], top_n: int = 5,
                       min_frequency: int = 2) -> WarmupPlan:
    counts = Counter(history)
    top = [(q, c) for q, c in counts.most_common(top_n) if c >= min_frequency]
    coverage = sum(c for _, c in top) / max(len(history), 1) * 100
    return WarmupPlan(top_queries=top, total_queries=len(history), coverage_pct=round(coverage, 1))

def execute_warmup_plan(plan: WarmupPlan, cache: dict,
                         model: str = "claude-haiku-4-5-20251001") -> None:
    print(f"Warming top {len(plan.top_queries)} queries ({plan.coverage_pct}% of historical traffic)")
    system = "You are a technical documentation assistant for a developer SDK."
    for query, freq in plan.top_queries:
        key = hashlib.md5(query.lower().encode()).hexdigest()
        if key in cache:
            print(f"  [SKIP x{freq}] {query[:50]}")
            continue
        resp = client.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": query}]
        )
        cache[key] = {"response": resp.content[0].text, "freq": freq, "ts": time.time()}
        print(f"  [WARMED x{freq}] {query[:50]}")

cache: dict = {}
plan = build_warmup_plan(HISTORICAL_QUERIES, top_n=5, min_frequency=2)
print(f"Plan: {[(q[:30], c) for q, c in plan.top_queries]}\n")
execute_warmup_plan(plan, cache)

print(f"\nCache ready: {len(cache)} entries covering {plan.coverage_pct}% of historical traffic")

# Expected Token Savings: warming top 5 queries that represent 70%+ of traffic = 70% model call reduction
# Environment: developer documentation bots, API support agents, product onboarding assistants
```

### Option 3: Async Parallel Cache Warming

```python
import anthropic
import asyncio
import time
import hashlib
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

WARMUP_BATCHES = {
    "onboarding": [
        "How do I get started?",
        "Where do I find my API key?",
        "What are the quickstart steps?",
    ],
    "billing": [
        "How does billing work?",
        "When am I charged?",
        "What happens if I exceed my quota?",
    ],
    "technical": [
        "What is the maximum context window?",
        "Which models support vision?",
        "How do I use streaming?",
    ]
}

@dataclass
class WarmResult:
    query: str
    category: str
    latency_ms: float
    tokens_used: int

async def warm_single(query: str, category: str, cache: dict) -> WarmResult:
    key = hashlib.md5(query.lower().encode()).hexdigest()
    t0 = time.perf_counter()
    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": query}]
    )
    latency = round((time.perf_counter() - t0) * 1000, 1)
    cache[key] = {"response": resp.content[0].text, "category": category, "ts": time.time()}
    return WarmResult(query=query, category=category, latency_ms=latency, tokens_used=resp.usage.output_tokens)

async def parallel_warm(batches: dict[str, list[str]], cache: dict) -> list[WarmResult]:
    print(f"Parallel-warming {sum(len(v) for v in batches.values())} queries across {len(batches)} categories...")
    t0 = time.perf_counter()

    tasks = []
    for category, queries in batches.items():
        for query in queries:
            tasks.append(warm_single(query, category, cache))

    results = await asyncio.gather(*tasks)
    total_time = round((time.perf_counter() - t0) * 1000, 0)
    print(f"Warming complete in {total_time}ms ({len(results)} queries)")
    return list(results)

async def main():
    cache: dict = {}
    results = await parallel_warm(WARMUP_BATCHES, cache)

    by_category: dict[str, list[WarmResult]] = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)

    print("\n=== Warm-up Stats ===")
    for cat, cat_results in by_category.items():
        avg_lat = sum(r.latency_ms for r in cat_results) / len(cat_results)
        print(f"  {cat}: {len(cat_results)} queries, avg={avg_lat:.0f}ms")

    print(f"\nCache ready: {len(cache)} entries")

asyncio.run(main())

# Expected Token Savings: parallel warming completes 9 queries in ~1x single-query time
# Environment: high-traffic deployments, deployment pipelines, scheduled cache refresh jobs
```

### Option 4: Predictive Warm-Up Based on User Journey Stage

```python
import anthropic
import time
import hashlib
from dataclasses import dataclass

client = anthropic.Anthropic()

# Predicted follow-up queries at each journey stage
JOURNEY_PREDICTIONS = {
    "signup": [
        "How do I verify my email?",
        "What plan should I choose?",
        "How do I invite team members?",
    ],
    "first_api_call": [
        "How do I handle authentication errors?",
        "What does status code 429 mean?",
        "How do I test in sandbox mode?",
    ],
    "approaching_limit": [
        "How do I upgrade my plan?",
        "What happens when I hit the limit?",
        "Can I get a temporary limit increase?",
    ],
    "trial_expiring": [
        "What is included in the paid plan?",
        "Will my data be deleted?",
        "How do I add a payment method?",
    ]
}

@dataclass
class JourneyCache:
    entries: dict = None
    stage_warmed: set = None

    def __post_init__(self):
        self.entries = {}
        self.stage_warmed = set()

    def key(self, q: str) -> str:
        return hashlib.md5(q.lower().encode()).hexdigest()

    def get(self, q: str) -> str | None:
        entry = self.entries.get(self.key(q))
        if entry and time.time() - entry["ts"] < 7200:
            entry["hits"] = entry.get("hits", 0) + 1
            return entry["response"]
        return None

    def set(self, q: str, response: str, stage: str) -> None:
        self.entries[self.key(q)] = {"response": response, "ts": time.time(), "stage": stage, "hits": 0}

def warm_for_stage(cache: JourneyCache, stage: str) -> None:
    if stage in cache.stage_warmed:
        return
    queries = JOURNEY_PREDICTIONS.get(stage, [])
    system = "You are a helpful onboarding assistant for a developer platform."
    print(f"[WARM] Stage '{stage}': {len(queries)} queries")
    for q in queries:
        if not cache.get(q):
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": q}]
            )
            cache.set(q, resp.content[0].text, stage)
    cache.stage_warmed.add(stage)

def handle_user_event(cache: JourneyCache, event: str, query: str) -> str:
    # Trigger warm-up for predicted next stage
    next_stages = {
        "user_signed_up": "signup",
        "made_first_api_call": "first_api_call",
        "reached_80pct_quota": "approaching_limit",
        "trial_7_days_left": "trial_expiring",
    }
    if predicted_stage := next_stages.get(event):
        warm_for_stage(cache, predicted_stage)

    cached = cache.get(query)
    if cached:
        return cached
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": query}]
    )
    cache.set(query, resp.content[0].text, "live")
    return resp.content[0].text

cache = JourneyCache()

# Simulate user events and subsequent questions
events = [
    ("user_signed_up", "How do I verify my email?"),
    ("made_first_api_call", "What does status code 429 mean?"),
    ("reached_80pct_quota", "How do I upgrade my plan?"),
]

for event, question in events:
    reply = handle_user_event(cache, event, question)
    print(f"Event: {event}\nQ: {question}\nA: {reply[:80]}...\n")

hit_count = sum(e.get("hits", 0) for e in cache.entries.values())
print(f"Cache: {len(cache.entries)} entries, {hit_count} hits total")

# Expected Token Savings: proactive warming means first post-event query always hits cache
# Environment: onboarding flows, trial-to-paid conversion funnels, lifecycle-driven agents
```

### Option 5: Scheduled Cache Refresh with Staleness Detection

```python
import anthropic
import time
import hashlib
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic()

KNOWN_QUERIES = [
    {"query": "What are the current API rate limits?", "ttl": 3600, "priority": "high"},
    {"query": "What models are available?", "ttl": 86400, "priority": "high"},
    {"query": "How does streaming work?", "ttl": 86400, "priority": "medium"},
    {"query": "What is the maximum file upload size?", "ttl": 86400, "priority": "low"},
]

@dataclass
class CacheEntry:
    response: str
    cached_at: float
    ttl: int
    refresh_count: int = 0
    hit_count: int = 0

    def is_stale(self) -> bool:
        return time.time() - self.cached_at > self.ttl

    def staleness_pct(self) -> float:
        age = time.time() - self.cached_at
        return min(100.0, age / self.ttl * 100)

class RefreshingCache:
    def __init__(self):
        self.store: dict[str, CacheEntry] = {}
        self.system = "You are a helpful technical assistant."

    def _key(self, q: str) -> str:
        return hashlib.md5(q.strip().lower().encode()).hexdigest()

    def _fetch(self, query: str, ttl: int) -> CacheEntry:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=self.system,
            messages=[{"role": "user", "content": query}]
        )
        return CacheEntry(response=resp.content[0].text, cached_at=time.time(), ttl=ttl)

    def warm(self, entries: list[dict]) -> dict[str, str]:
        results = {}
        for e in sorted(entries, key=lambda x: {"high": 0, "medium": 1, "low": 2}[x["priority"]]):
            key = self._key(e["query"])
            current = self.store.get(key)
            if current and not current.is_stale():
                results[e["query"]] = f"FRESH (staleness={current.staleness_pct():.0f}%)"
                continue
            entry = self._fetch(e["query"], e["ttl"])
            if current:
                entry.refresh_count = current.refresh_count + 1
            self.store[key] = entry
            results[e["query"]] = f"WARMED (ttl={e['ttl']}s, priority={e['priority']})"
        return results

    def get(self, query: str, ttl: int = 3600) -> tuple[str, bool]:
        key = self._key(query)
        entry = self.store.get(key)
        if entry and not entry.is_stale():
            entry.hit_count += 1
            return entry.response, True
        new_entry = self._fetch(query, ttl)
        if entry:
            new_entry.refresh_count = entry.refresh_count + 1
        self.store[key] = new_entry
        return new_entry.response, False

    def health_report(self) -> None:
        print("\n=== Cache Health ===")
        for key, entry in self.store.items():
            status = "STALE" if entry.is_stale() else f"{entry.staleness_pct():.0f}% used"
            print(f"  {key[:8]}... hits={entry.hit_count} refreshes={entry.refresh_count} [{status}]")

cache = RefreshingCache()
print("Initial warm-up:")
results = cache.warm(KNOWN_QUERIES)
for q, status in results.items():
    print(f"  {q[:50]}: {status}")

# Simulate requests
for q in ["What are the current API rate limits?", "What models are available?", "What is WebSockets support?"]:
    response, hit = cache.get(q)
    print(f"\n[{'HIT' if hit else 'MISS'}] {q}: {response[:60]}...")

cache.health_report()

# Expected Token Savings: refresh only stale entries; hit rate approaches 100% for known queries
# Environment: documentation bots, scheduled maintenance windows, cache-first architectures
```

### Option 6: Multi-Tier Warm-Up with L1/L2 Cache Population

```python
import anthropic
import time
import hashlib
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class TieredCacheEntry:
    response: str
    tier: str  # "l1" or "l2"
    cached_at: float
    ttl: int
    access_count: int = 0

class TieredWarmupCache:
    """L1: hot queries (short TTL, kept in fast memory)
       L2: warm queries (longer TTL, evicted to slower store)"""

    def __init__(self):
        self.l1: dict[str, TieredCacheEntry] = {}  # hot, short TTL
        self.l2: dict[str, TieredCacheEntry] = {}  # warm, long TTL
        self.L1_TTL = 900     # 15 min
        self.L2_TTL = 86400   # 24 hours
        self.L1_MAX = 20
        self.system = "You are a helpful assistant."

    def _key(self, q: str) -> str:
        return hashlib.md5(q.lower().strip().encode()).hexdigest()

    def _evict_l1_if_needed(self) -> None:
        if len(self.l1) >= self.L1_MAX:
            # Evict least accessed entry
            lru_key = min(self.l1, key=lambda k: self.l1[k].access_count)
            evicted = self.l1.pop(lru_key)
            evicted.tier = "l2"
            self.l2[lru_key] = evicted

    def warm_l1(self, queries: list[str]) -> int:
        """Warm the hot tier with highest-priority queries."""
        warmed = 0
        for q in queries:
            key = self._key(q)
            if key in self.l1:
                continue
            self._evict_l1_if_needed()
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=self.system,
                messages=[{"role": "user", "content": q}]
            )
            self.l1[key] = TieredCacheEntry(resp.content[0].text, "l1", time.time(), self.L1_TTL)
            warmed += 1
        return warmed

    def warm_l2(self, queries: list[str]) -> int:
        """Warm the warm tier with secondary queries."""
        warmed = 0
        for q in queries:
            key = self._key(q)
            if key in self.l1 or key in self.l2:
                continue
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=self.system,
                messages=[{"role": "user", "content": q}]
            )
            self.l2[key] = TieredCacheEntry(resp.content[0].text, "l2", time.time(), self.L2_TTL)
            warmed += 1
        return warmed

    def get(self, q: str) -> tuple[str | None, str]:
        key = self._key(q)
        now = time.time()
        for tier_name, tier in [("L1", self.l1), ("L2", self.l2)]:
            if key in tier:
                entry = tier[key]
                if now - entry.cached_at < entry.ttl:
                    entry.access_count += 1
                    if tier_name == "L2" and entry.access_count >= 3:
                        # Promote to L1
                        self._evict_l1_if_needed()
                        entry.tier = "l1"
                        self.l1[key] = tier.pop(key)
                        return entry.response, "L2→L1 PROMOTED"
                    return entry.response, tier_name
        return None, "MISS"

HOT_QUERIES = ["How do I authenticate?", "What is the rate limit?", "How do I use streaming?"]
WARM_QUERIES = ["What file formats are supported?", "How do I paginate?", "What is the SLA?"]

cache = TieredWarmupCache()
l1_count = cache.warm_l1(HOT_QUERIES)
l2_count = cache.warm_l2(WARM_QUERIES)
print(f"Warmed: L1={l1_count}, L2={l2_count}")

for q in HOT_QUERIES + ["What file formats are supported?", "New uncached question"]:
    response, tier = cache.get(q)
    if response:
        print(f"[{tier}] {q[:45]}: {response[:50]}...")
    else:
        print(f"[{tier}] {q[:45]}: needs model call")

# Expected Token Savings: L1 serves hottest queries with <1ms latency; L2 extends coverage 5x
# Environment: high-traffic agents, multi-layer caching infrastructure, edge-deployed agents
```

## Comparison

| Option | Warm-Up Strategy | Dynamic | Tier Support | Best For |
|--------|-----------------|---------|--------------|----------|
| 1 | Static query list | No | No | Simple FAQ bots |
| 2 | Usage-pattern driven | No | No | Data-informed warm-up |
| 3 | Async parallel | No | No | Fast deployment warm-up |
| 4 | User journey stage | Yes | No | Lifecycle-aware agents |
| 5 | Scheduled refresh | Yes | No | Staleness-sensitive caches |
| 6 | Multi-tier L1/L2 | No | Yes | High-traffic production |
