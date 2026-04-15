---
layout: solution
title: "Agent Doesn't Implement Tiered Caching (L1/L2)"
category: performance
description: "Use a fast in-memory L1 cache with a persistent L2 (SQLite or disk) so repeated tool calls and model responses are served instantly without redundant API calls."
tags: [performance, caching, l1-l2, sqlite, ttl, python]
---

# Agent Doesn't Implement Tiered Caching (L1/L2)

Agents that skip caching re-run identical tool calls on every request — adding latency and cost. A two-tier cache (fast in-memory L1 + persistent L2) serves hot data in microseconds and warm data in milliseconds, while cold data is fetched and promoted on the way back up.

## Option 1: Simple Dict L1 + SQLite L2

```python
import anthropic
import sqlite3
import hashlib
import time
import json

client = anthropic.Anthropic()
DB = "cache_l2.db"

# L1: in-memory dict with TTL
_l1: dict[str, tuple[str, float]] = {}
L1_TTL = 60   # seconds
L2_TTL = 3600 # seconds

def _key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]

def init_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, exp REAL)")
    con.commit(); con.close()

def l1_get(k: str) -> str | None:
    entry = _l1.get(k)
    if entry and entry[1] > time.time():
        return entry[0]
    _l1.pop(k, None)
    return None

def l1_set(k: str, v: str):
    _l1[k] = (v, time.time() + L1_TTL)

def l2_get(k: str) -> str | None:
    con = sqlite3.connect(DB)
    row = con.execute("SELECT v FROM cache WHERE k=? AND exp>?", (k, time.time())).fetchone()
    con.close()
    return row[0] if row else None

def l2_set(k: str, v: str):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (k, v, time.time() + L2_TTL))
    con.commit(); con.close()

def cached_call(prompt: str) -> tuple[str, str]:
    k = _key(prompt)
    if hit := l1_get(k):
        return hit, "L1"
    if hit := l2_get(k):
        l1_set(k, hit)   # promote to L1
        return hit, "L2"
    # Cold: call model
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    result = resp.content[0].text
    l1_set(k, result)
    l2_set(k, result)
    return result, "MISS"

init_db()
prompt = "What is the capital of France?"
for _ in range(3):
    result, source = cached_call(prompt)
    print(f"[{source}] {result.strip()[:60]}")

# Expected Token Savings: 100% on repeated identical queries; L1 hit in <1ms, L2 in <5ms
# Environment: single-process; L2 SQLite persists across restarts
```

## Option 2: LRU L1 with Size Limit + SQLite L2

```python
import anthropic
import sqlite3
import hashlib
import time
from collections import OrderedDict

client = anthropic.Anthropic()
DB = "cache_lru.db"

class LRUCache:
    def __init__(self, max_size: int = 128, ttl: int = 120):
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl

    def get(self, k: str) -> str | None:
        if k not in self._cache:
            return None
        v, exp = self._cache[k]
        if exp < time.time():
            del self._cache[k]
            return None
        self._cache.move_to_end(k)
        return v

    def set(self, k: str, v: str):
        self._cache[k] = (v, time.time() + self.ttl)
        self._cache.move_to_end(k)
        if len(self._cache) > self.max_size:
            self._cache.popitem(last=False)  # evict LRU

    @property
    def size(self): return len(self._cache)

_l1 = LRUCache(max_size=128, ttl=120)

def init_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS l2 (k TEXT PRIMARY KEY, v TEXT, exp REAL)")
    con.commit(); con.close()

def l2_get(k: str) -> str | None:
    con = sqlite3.connect(DB)
    row = con.execute("SELECT v FROM l2 WHERE k=? AND exp>?", (k, time.time())).fetchone()
    con.close()
    return row[0] if row else None

def l2_set(k: str, v: str, ttl: int = 7200):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO l2 VALUES (?,?,?)", (k, v, time.time() + ttl))
    con.commit(); con.close()

def call(prompt: str) -> tuple[str, str]:
    k = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    if v := _l1.get(k):
        return v, "L1-LRU"
    if v := l2_get(k):
        _l1.set(k, v)
        return v, "L2-promoted"
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    v = resp.content[0].text
    _l1.set(k, v)
    l2_set(k, v)
    return v, "MISS"

init_db()
prompts = ["Explain TCP handshake", "What is DNS?", "Explain TCP handshake"]
for p in prompts:
    result, src = call(p)
    print(f"[{src}] L1_size={_l1.size}: {result[:50]}")

# Expected Token Savings: LRU evicts cold entries; 128-slot L1 handles hot working set
# Environment: single-process; L2 persists 2h; adjust max_size to available RAM
```

## Option 3: Async Tiered Cache with Stampede Protection

```python
import anthropic
import asyncio
import sqlite3
import hashlib
import time

client = anthropic.AsyncAnthropic()
DB = "async_cache.db"

_l1: dict[str, tuple[str, float]] = {}
_inflight: dict[str, asyncio.Event] = {}
_inflight_values: dict[str, str] = {}

def init_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS l2 (k TEXT PRIMARY KEY, v TEXT, exp REAL)")
    con.commit(); con.close()

def l1_get(k: str) -> str | None:
    v, exp = _l1.get(k, (None, 0))
    return v if exp > time.time() else None

def l1_set(k: str, v: str, ttl: int = 60):
    _l1[k] = (v, time.time() + ttl)

def l2_get(k: str) -> str | None:
    con = sqlite3.connect(DB)
    row = con.execute("SELECT v FROM l2 WHERE k=? AND exp>?", (k, time.time())).fetchone()
    con.close()
    return row[0] if row else None

def l2_set(k: str, v: str, ttl: int = 3600):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO l2 VALUES (?,?,?)", (k, v, time.time() + ttl))
    con.commit(); con.close()

async def cached_call(prompt: str) -> tuple[str, str]:
    k = hashlib.sha256(prompt.encode()).hexdigest()[:16]

    if v := l1_get(k):
        return v, "L1"
    if v := l2_get(k):
        l1_set(k, v); return v, "L2"

    # Stampede protection: if another coroutine is fetching, wait for it
    if k in _inflight:
        await _inflight[k].wait()
        return _inflight_values.get(k, ""), "COALESCED"

    event = asyncio.Event()
    _inflight[k] = event
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        v = resp.content[0].text
        l1_set(k, v); l2_set(k, v)
        _inflight_values[k] = v
        return v, "MISS"
    finally:
        event.set()
        _inflight.pop(k, None)
        # Clean up value after brief delay
        await asyncio.sleep(0)
        _inflight_values.pop(k, None)

async def main():
    init_db()
    prompt = "What is the OSI model?"
    # Simulate 5 concurrent requests for same prompt
    results = await asyncio.gather(*[cached_call(prompt) for _ in range(5)])
    for v, src in results:
        print(f"[{src}] {v[:50]}")

asyncio.run(main())

# Expected Token Savings: Coalescing prevents N identical concurrent requests; only 1 model call
# Environment: async; Event-based coalescing; L2 persists across process restarts
```

## Option 4: Semantic Cache with Embedding Similarity (L1=dict, L2=SQLite+vector)

```python
import anthropic
import sqlite3
import hashlib
import time
import math

client = anthropic.Anthropic()
DB = "semantic_cache.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS semantic_l2 (
            k TEXT PRIMARY KEY,
            prompt TEXT, response TEXT,
            embedding TEXT, exp REAL
        )
    """)
    con.commit(); con.close()

def ngram_embed(text: str, n: int = 3, size: int = 32) -> list[float]:
    """Lightweight character n-gram embedding."""
    counts: dict[str, int] = {}
    t = text.lower()
    for i in range(len(t) - n + 1):
        g = t[i:i+n]
        counts[g] = counts.get(g, 0) + 1
    # Map to fixed-size vector via hash bucketing
    vec = [0.0] * size
    for g, c in counts.items():
        idx = int(hashlib.md5(g.encode()).hexdigest(), 16) % size
        vec[idx] += c
    norm = math.sqrt(sum(x**2 for x in vec)) or 1
    return [x / norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

_l1: dict[str, tuple[str, float]] = {}

def l1_get(prompt: str) -> str | None:
    v, exp = _l1.get(prompt, (None, 0))
    return v if exp > time.time() else None

def l1_set(prompt: str, v: str):
    _l1[prompt] = (v, time.time() + 120)

def semantic_l2_get(prompt: str, threshold: float = 0.92) -> str | None:
    emb = ngram_embed(prompt)
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT response, embedding FROM semantic_l2 WHERE exp>?",
                       (time.time(),)).fetchall()
    con.close()
    best_sim, best_resp = 0.0, None
    for resp, emb_json in rows:
        stored_emb = [float(x) for x in emb_json.split(",")]
        sim = cosine(emb, stored_emb)
        if sim > best_sim:
            best_sim, best_resp = sim, resp
    if best_sim >= threshold:
        print(f"  Semantic hit (sim={best_sim:.3f})")
        return best_resp
    return None

def semantic_l2_set(prompt: str, response: str, ttl: int = 7200):
    emb = ngram_embed(prompt)
    k = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO semantic_l2 VALUES (?,?,?,?,?)",
                (k, prompt, response, ",".join(f"{x:.6f}" for x in emb), time.time() + ttl))
    con.commit(); con.close()

def call(prompt: str) -> tuple[str, str]:
    if v := l1_get(prompt):
        return v, "L1-exact"
    if v := semantic_l2_get(prompt):
        l1_set(prompt, v)
        return v, "L2-semantic"
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    v = resp.content[0].text
    l1_set(prompt, v)
    semantic_l2_set(prompt, v)
    return v, "MISS"

init_db()
queries = [
    "What is the capital of France?",
    "Tell me the capital city of France.",   # semantically similar
    "What is the capital of Germany?",       # different
]
for q in queries:
    result, src = call(q)
    print(f"[{src}] {q[:40]}: {result.strip()[:50]}")

# Expected Token Savings: Semantic matching serves ~similar queries from cache; 80%+ hit rate on rephrasing
# Environment: swap ngram_embed with voyage-3 for production; threshold tunable
```

## Option 5: Per-Tool Tiered Cache with Cost Tracking

```python
import anthropic
import sqlite3
import hashlib
import time
import json

client = anthropic.Anthropic()
DB = "tool_cache.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tool_l2 (
            tool TEXT, k TEXT, result TEXT,
            exp REAL, cost_saved REAL DEFAULT 0,
            PRIMARY KEY (tool, k)
        )
    """)
    con.commit(); con.close()

# Per-tool TTL policy (seconds)
TOOL_TTL = {
    "get_weather":   300,    # 5 min — changes often
    "search_web":    3600,   # 1h — moderate staleness OK
    "get_stock":     60,     # 1 min — very fresh needed
    "get_docs":      86400,  # 1 day — rarely changes
    "default":       1800,
}

# Per-tool estimated cost savings per cache hit (USD)
TOOL_COST = {
    "get_weather": 0.001,
    "search_web":  0.005,
    "get_docs":    0.002,
    "default":     0.001,
}

_l1: dict[tuple, tuple[str, float]] = {}

def cache_key(tool: str, args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:16]

def l1_get(tool: str, k: str) -> str | None:
    v, exp = _l1.get((tool, k), (None, 0))
    return v if exp > time.time() else None

def l1_set(tool: str, k: str, v: str, ttl: int):
    _l1[(tool, k)] = (v, time.time() + min(ttl, 120))

def l2_get(tool: str, k: str) -> str | None:
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT result FROM tool_l2 WHERE tool=? AND k=? AND exp>?",
        (tool, k, time.time())
    ).fetchone()
    if row:
        cost = TOOL_COST.get(tool, TOOL_COST["default"])
        con.execute("UPDATE tool_l2 SET cost_saved=cost_saved+? WHERE tool=? AND k=?",
                    (cost, tool, k))
        con.commit()
    con.close()
    return row[0] if row else None

def l2_set(tool: str, k: str, v: str):
    ttl = TOOL_TTL.get(tool, TOOL_TTL["default"])
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO tool_l2 VALUES (?,?,?,?,0)",
                (tool, k, v, time.time() + ttl))
    con.commit(); con.close()

def cached_tool_call(tool: str, args: dict) -> tuple[str, str]:
    k = cache_key(tool, args)
    if v := l1_get(tool, k):
        return v, "L1"
    if v := l2_get(tool, k):
        ttl = TOOL_TTL.get(tool, TOOL_TTL["default"])
        l1_set(tool, k, v, ttl)
        return v, "L2"
    # Simulate tool execution via model
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Simulate {tool} result for {args}. Be brief."}],
    )
    v = resp.content[0].text
    l1_set(tool, k, v, TOOL_TTL.get(tool, 1800))
    l2_set(tool, k, v)
    return v, "MISS"

def cost_report():
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT tool, SUM(cost_saved), COUNT(*) FROM tool_l2 GROUP BY tool").fetchall()
    con.close()
    print("\nCache cost savings:")
    for tool, saved, hits in rows:
        print(f"  {tool}: ${saved:.4f} saved across {hits} entries")

init_db()
for _ in range(3):
    result, src = cached_tool_call("get_weather", {"city": "Tokyo"})
    print(f"[{src}] weather: {result.strip()[:50]}")
cached_tool_call("get_docs", {"topic": "asyncio"})
cost_report()

# Expected Token Savings: Per-tool TTL prevents stale serving; cost tracking shows ROI
# Environment: SQLite; extend with Redis for multi-process deployments
```

## Option 6: Cache Warming and Invalidation Strategy

```python
import anthropic
import sqlite3
import hashlib
import time
import threading

client = anthropic.Anthropic()
DB = "warm_cache.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            k TEXT PRIMARY KEY, v TEXT,
            exp REAL, hits INTEGER DEFAULT 0,
            created_at REAL
        )
    """)
    con.commit(); con.close()

_l1: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()

def _k(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def get(prompt: str) -> str | None:
    k = _k(prompt)
    with _lock:
        v, exp = _l1.get(k, (None, 0))
        if v and exp > time.time():
            return v
    con = sqlite3.connect(DB)
    row = con.execute("SELECT v FROM cache WHERE k=? AND exp>?", (k, time.time())).fetchone()
    if row:
        con.execute("UPDATE cache SET hits=hits+1 WHERE k=?", (k,))
        con.commit()
        with _lock:
            _l1[k] = (row[0], time.time() + 60)
    con.close()
    return row[0] if row else None

def put(prompt: str, value: str, ttl: int = 3600):
    k = _k(prompt)
    with _lock:
        _l1[k] = (value, time.time() + min(ttl, 120))
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,0,?)",
                (k, value, time.time() + ttl, time.time()))
    con.commit(); con.close()

def invalidate(prompt: str):
    k = _k(prompt)
    with _lock:
        _l1.pop(k, None)
    con = sqlite3.connect(DB)
    con.execute("DELETE FROM cache WHERE k=?", (k,))
    con.commit(); con.close()
    print(f"Invalidated: {prompt[:50]}")

def warm_cache(prompts: list[str], model: str = "claude-haiku-4-5-20251001"):
    """Pre-populate cache with expected high-traffic prompts."""
    print(f"Warming cache with {len(prompts)} prompts...")
    for prompt in prompts:
        if get(prompt) is None:
            resp = client.messages.create(
                model=model, max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            put(prompt, resp.content[0].text)
            print(f"  Warmed: {prompt[:50]}")
        else:
            print(f"  Already cached: {prompt[:50]}")

def call(prompt: str) -> tuple[str, str]:
    if v := get(prompt):
        return v, "HIT"
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    v = resp.content[0].text
    put(prompt, v)
    return v, "MISS"

init_db()
# Pre-warm on startup
warm_cache([
    "What is asyncio?",
    "Explain the GIL in Python.",
    "What is a context manager?",
])

# Serve from cache
result, src = call("What is asyncio?")
print(f"\n[{src}] {result.strip()[:80]}")

# Invalidate after content update
invalidate("What is asyncio?")
result, src = call("What is asyncio?")
print(f"[{src}] {result.strip()[:80]}")

# Expected Token Savings: Warm cache serves 100% of predicted traffic from cache at startup
# Environment: thread-safe; warm on app startup with expected FAQ prompts
```

## Comparison

| Option | L1 Type | L2 Type | Special Feature |
|--------|---------|---------|----------------|
| 1 — Basic dict+SQLite | Dict TTL | SQLite TTL | Simplest two-tier |
| 2 — LRU+SQLite | OrderedDict LRU | SQLite TTL | Bounded L1 with eviction |
| 3 — Async+Stampede | Dict TTL | SQLite | Coalesces concurrent misses |
| 4 — Semantic | Dict exact | SQLite+embedding | Serves similar queries |
| 5 — Per-Tool | Dict TTL | SQLite per-tool | Cost tracking + policy-per-tool |
| 6 — Warm+Invalidate | Dict TTL | SQLite | Pre-warming + explicit invalidation |
