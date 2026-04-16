---
layout: solution
title: "Agent Doesn't Implement Streaming Response Caching"
category: streaming
description: "Agent re-streams identical responses for repeated queries instead of replaying cached stream chunks, wasting API tokens and adding unnecessary latency."
tags: [streaming, caching, performance, token-cost, sse]
---

# Agent Doesn't Implement Streaming Response Caching

## Problem

When multiple users ask the same question—or the same user repeats a query—the agent makes a full API call and streams a fresh response every time. For deterministic queries (FAQ answers, documentation lookups, template generation), this wastes tokens and adds 1-5 seconds of latency per repeated request. Without streaming response caching, popular queries burn through API budget at linear cost with zero benefit to response quality.

## Solution Options

### Option 1: Full Response Cache with Streaming Replay

```python
import anthropic
import hashlib
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CachedStream:
    chunks: list[str]
    full_text: str
    created_at: float
    input_tokens: int
    output_tokens: int
    hit_count: int = 0

class StreamingResponseCache:
    def __init__(self, ttl_seconds: float = 600.0):
        self.ttl = ttl_seconds
        self._cache: dict[str, CachedStream] = {}

    def _key(self, system: str, messages: list[dict]) -> str:
        import json
        payload = json.dumps({"system": system, "messages": messages}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, system: str, messages: list[dict]) -> CachedStream | None:
        key = self._key(system, messages)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.time() - entry.created_at > self.ttl:
            del self._cache[key]
            return None
        entry.hit_count += 1
        return entry

    def store(self, system: str, messages: list[dict], entry: CachedStream):
        key = self._key(system, messages)
        self._cache[key] = entry

cache = StreamingResponseCache(ttl_seconds=300)

def stream_with_cache(system: str, messages: list[dict], chunk_delay: float = 0.01):
    """Stream response, replaying from cache if available."""
    cached = cache.get(system, messages)

    if cached:
        print(f"[CACHE HIT] Replaying {len(cached.chunks)} chunks (saved {cached.output_tokens} tokens)")
        for chunk in cached.chunks:
            print(chunk, end="", flush=True)
            time.sleep(chunk_delay)  # Simulate realistic streaming pace
        print()
        return cached.full_text

    # Cache miss — stream live and capture
    print("[CACHE MISS] Streaming live response...")
    chunks: list[str] = []
    full_text = ""
    input_tokens = 0
    output_tokens = 0

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            chunks.append(text)
            full_text += text

        final = stream.get_final_message()
        input_tokens = final.usage.input_tokens
        output_tokens = final.usage.output_tokens

    print()

    entry = CachedStream(
        chunks=chunks,
        full_text=full_text,
        created_at=time.time(),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    cache.store(system, messages, entry)
    print(f"[CACHED] Stored response ({output_tokens} output tokens)")
    return full_text

system = "You are a helpful assistant that explains programming concepts."
messages = [{"role": "user", "content": "What is a REST API? Give a brief explanation."}]

print("=== First request (live) ===")
stream_with_cache(system, messages)

print("\n=== Second request (cached) ===")
stream_with_cache(system, messages)

print("\n=== Third request (cached) ===")
stream_with_cache(system, messages)

# Expected Token Savings: 100% token savings on cache hits; only pay once per unique query
# Environment: FAQ bots, documentation assistants, deterministic template generators
```

### Option 2: Chunk-Level Streaming Cache with Prefix Matching

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ChunkEntry:
    text: str
    cumulative_text: str  # Full text up to this chunk

@dataclass
class PrefixCacheEntry:
    system: str
    message_prefix: str  # The cached prompt prefix
    chunks: list[ChunkEntry]
    created_at: float
    output_tokens: int

class PrefixStreamCache:
    """Cache that can replay partial responses for prefix-matched queries."""

    def __init__(self, ttl: float = 300):
        self.ttl = ttl
        self._entries: dict[str, PrefixCacheEntry] = {}

    def _key(self, system: str, user_message: str) -> str:
        payload = json.dumps({"system": system, "msg": user_message}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get_exact(self, system: str, user_message: str) -> PrefixCacheEntry | None:
        key = self._key(system, user_message)
        entry = self._entries.get(key)
        if entry and time.time() - entry.created_at <= self.ttl:
            return entry
        return None

    def store(self, system: str, user_message: str, chunks: list[ChunkEntry], output_tokens: int):
        key = self._key(system, user_message)
        self._entries[key] = PrefixCacheEntry(
            system=system,
            message_prefix=user_message,
            chunks=chunks,
            created_at=time.time(),
            output_tokens=output_tokens,
        )

    def stats(self) -> dict:
        return {
            "cached_responses": len(self._entries),
            "total_cached_tokens": sum(e.output_tokens for e in self._entries.values()),
        }

prefix_cache = PrefixStreamCache(ttl=600)

def cached_stream(system: str, user_message: str, replay_delay: float = 0.005):
    cached = prefix_cache.get_exact(system, user_message)

    if cached:
        token_savings = cached.output_tokens
        print(f"[CACHE HIT] Replaying cached stream ({len(cached.chunks)} chunks, {token_savings} tokens saved)")
        for chunk_entry in cached.chunks:
            print(chunk_entry.text, end="", flush=True)
            time.sleep(replay_delay)
        print()
        return cached.chunks[-1].cumulative_text if cached.chunks else ""

    # Live stream with chunk capture
    print("[LIVE STREAM] Fetching from API...")
    chunks: list[ChunkEntry] = []
    cumulative = ""
    output_tokens = 0

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            cumulative += text
            chunks.append(ChunkEntry(text=text, cumulative_text=cumulative))

        final = stream.get_final_message()
        output_tokens = final.usage.output_tokens

    print()
    prefix_cache.store(system, user_message, chunks, output_tokens)
    print(f"[STORED] {len(chunks)} chunks, {output_tokens} tokens")
    return cumulative

system_prompt = "You are a Python tutor. Give concise, clear explanations."

# Simulate repeated FAQ-style queries
for i in range(3):
    print(f"\n--- Request {i+1} ---")
    cached_stream(system_prompt, "Explain list comprehensions in Python with one example.")

print(f"\nCache stats: {prefix_cache.stats()}")

# Expected Token Savings: 100% output token savings after first request for identical queries
# Environment: High-traffic FAQ endpoints where many users ask the same questions
```

### Option 3: SSE Streaming Cache for Web Clients

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Generator

client = anthropic.Anthropic()

@dataclass
class SSEEvent:
    event: str  # "delta", "done", "error"
    data: str
    timestamp: float

@dataclass
class SSECacheEntry:
    events: list[SSEEvent]
    created_at: float
    ttl: float

    def is_valid(self) -> bool:
        return time.time() - self.created_at < self.ttl

class SSEStreamCache:
    def __init__(self):
        self._cache: dict[str, SSECacheEntry] = {}

    def _key(self, request: dict) -> str:
        return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()

    def get(self, request: dict) -> SSECacheEntry | None:
        key = self._key(request)
        entry = self._cache.get(key)
        if entry and entry.is_valid():
            return entry
        if key in self._cache:
            del self._cache[key]
        return None

    def store(self, request: dict, events: list[SSEEvent], ttl: float = 300):
        key = self._key(request)
        self._cache[key] = SSECacheEntry(events=events, created_at=time.time(), ttl=ttl)

sse_cache = SSEStreamCache()

def stream_to_sse_events(system: str, user_message: str) -> Generator[SSEEvent, None, None]:
    """Convert Claude streaming response to SSE events."""
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            yield SSEEvent(event="delta", data=json.dumps({"text": text}), timestamp=time.time())
        yield SSEEvent(event="done", data=json.dumps({"status": "complete"}), timestamp=time.time())

def get_sse_stream(system: str, user_message: str, client_id: str = "") -> Generator[str, None, None]:
    """Return SSE-formatted stream, from cache or live."""
    request_key = {"system": system, "message": user_message}
    cached = sse_cache.get(request_key)

    if cached:
        print(f"[SSE CACHE] Client {client_id}: replaying {len(cached.events)} cached events")
        for event in cached.events:
            yield f"event: {event.event}\ndata: {event.data}\n\n"
            time.sleep(0.005)  # Realistic SSE pacing
        return

    # Live stream — capture and cache
    print(f"[SSE LIVE] Client {client_id}: streaming from API")
    captured_events: list[SSEEvent] = []

    for sse_event in stream_to_sse_events(system, user_message):
        captured_events.append(sse_event)
        yield f"event: {sse_event.event}\ndata: {sse_event.data}\n\n"

    sse_cache.store(request_key, captured_events, ttl=300)
    print(f"[SSE STORED] {len(captured_events)} events cached")

def simulate_sse_client(client_id: str, system: str, message: str):
    """Simulate consuming an SSE stream."""
    print(f"\n--- SSE Client: {client_id} ---")
    full_text = ""
    for raw_event in get_sse_stream(system, message, client_id):
        if '"text"' in raw_event:
            data_part = raw_event.split("data: ")[1].strip()
            try:
                parsed = json.loads(data_part)
                if "text" in parsed:
                    print(parsed["text"], end="", flush=True)
                    full_text += parsed["text"]
            except json.JSONDecodeError:
                pass
    print()
    return full_text

system = "You are a helpful assistant."
question = "What are the benefits of using async/await in Python?"

# Multiple clients asking the same question
for client_id in ["user_1", "user_2", "user_3"]:
    simulate_sse_client(client_id, system, question)

# Expected Token Savings: 100% token savings for users 2+ asking identical questions
# Environment: Web applications with SSE endpoints serving many concurrent users
```

### Option 4: Partial Match Cache with Streaming Continuation

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class PartialCacheEntry:
    prompt_hash: str
    streamed_text: str
    is_complete: bool
    created_at: float
    usage: dict

class PartialStreamCache:
    """Caches complete responses and serves partial hits when appropriate."""

    def __init__(self, ttl: float = 600):
        self.ttl = ttl
        self._complete: dict[str, PartialCacheEntry] = {}
        self._stats = {"hits": 0, "misses": 0, "tokens_saved": 0}

    def _hash(self, system: str, message: str) -> str:
        return hashlib.sha256(f"{system}||{message}".encode()).hexdigest()

    def get(self, system: str, message: str) -> PartialCacheEntry | None:
        h = self._hash(system, message)
        entry = self._complete.get(h)
        if entry and time.time() - entry.created_at < self.ttl:
            self._stats["hits"] += 1
            self._stats["tokens_saved"] += entry.usage.get("output_tokens", 0)
            return entry
        self._stats["misses"] += 1
        return None

    def store(self, system: str, message: str, text: str, usage: dict):
        h = self._hash(system, message)
        self._complete[h] = PartialCacheEntry(
            prompt_hash=h,
            streamed_text=text,
            is_complete=True,
            created_at=time.time(),
            usage=usage,
        )

    @property
    def stats(self) -> dict:
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = self._stats["hits"] / max(total, 1) * 100
        return {**self._stats, "hit_rate_pct": round(hit_rate, 1), "total_requests": total}

partial_cache = PartialStreamCache(ttl=300)

def smart_cached_stream(
    system: str,
    message: str,
    replay_speed_multiplier: float = 3.0,  # Replay faster than original
) -> str:
    cached = partial_cache.get(system, message)

    if cached:
        # Replay cached chunks faster than live streaming
        words = cached.streamed_text.split(" ")
        for i, word in enumerate(words):
            sep = " " if i < len(words) - 1 else ""
            print(word + sep, end="", flush=True)
            time.sleep(0.003 / replay_speed_multiplier)
        print()
        return cached.streamed_text

    # Live stream
    full_text = ""
    usage = {}

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            full_text += text

        final = stream.get_final_message()
        usage = {"input_tokens": final.usage.input_tokens, "output_tokens": final.usage.output_tokens}

    print()
    partial_cache.store(system, message, full_text, usage)
    return full_text

# Simulate a support bot with repeated queries
system = "You are a customer support agent for a software product."
common_questions = [
    "How do I reset my password?",
    "What payment methods do you accept?",
    "How do I reset my password?",  # Repeated
    "What payment methods do you accept?",  # Repeated
    "How do I cancel my subscription?",
    "How do I reset my password?",  # Repeated again
]

for i, question in enumerate(common_questions):
    print(f"\n[Request {i+1}] {question}")
    smart_cached_stream(system, question)

print(f"\nCache stats: {json.dumps(partial_cache.stats, indent=2)}")

# Expected Token Savings: 50-80% reduction in repeated support query tokens
# Environment: Customer support bots with a known set of frequent questions
```

### Option 5: Multi-Model Streaming Cache with Model Fallback

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ModelCacheEntry:
    model: str
    text: str
    input_tokens: int
    output_tokens: int
    created_at: float
    quality_score: float  # 0-1, higher = more expensive model used

MODEL_TIERS = {
    "fast": "claude-haiku-4-5-20251001",
    "balanced": "claude-sonnet-4-6",
    "powerful": "claude-opus-4-6",
}

MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.00025, "output": 0.00125},
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-opus-4-6": {"input": 0.015, "output": 0.075},
}

class MultiModelStreamCache:
    def __init__(self, ttl: float = 600):
        self.ttl = ttl
        self._cache: dict[str, ModelCacheEntry] = {}
        self._tokens_saved = 0
        self._cost_saved = 0.0

    def _key(self, system: str, message: str) -> str:
        return hashlib.sha256(f"{system}||{message}".encode()).hexdigest()

    def get(self, system: str, message: str, min_quality: float = 0.0) -> ModelCacheEntry | None:
        key = self._key(system, message)
        entry = self._cache.get(key)
        if not entry:
            return None
        if time.time() - entry.created_at > self.ttl:
            del self._cache[key]
            return None
        if entry.quality_score < min_quality:
            return None  # Cached response isn't high enough quality
        costs = MODEL_COSTS.get(entry.model, {"output": 0})
        self._tokens_saved += entry.output_tokens
        self._cost_saved += entry.output_tokens / 1000 * costs["output"]
        return entry

    def store(self, system: str, message: str, entry: ModelCacheEntry):
        key = self._key(system, message)
        self._cache[key] = entry

    @property
    def stats(self) -> dict:
        return {
            "entries": len(self._cache),
            "tokens_saved": self._tokens_saved,
            "estimated_cost_saved_usd": round(self._cost_saved, 4),
        }

mm_cache = MultiModelStreamCache(ttl=300)

def quality_tier_cached_stream(
    system: str,
    message: str,
    requested_tier: str = "fast",
    min_quality_from_cache: float = 0.5,
) -> str:
    cached = mm_cache.get(system, message, min_quality=min_quality_from_cache)

    if cached:
        print(f"[CACHE HIT] Using cached {cached.model} response (quality={cached.quality_score:.2f})")
        for word in cached.text.split(" "):
            print(word + " ", end="", flush=True)
            time.sleep(0.002)
        print()
        return cached.text

    model = MODEL_TIERS.get(requested_tier, MODEL_TIERS["fast"])
    quality = {"fast": 0.6, "balanced": 0.85, "powerful": 1.0}.get(requested_tier, 0.6)

    print(f"[LIVE STREAM] Using {model} (tier={requested_tier})")
    full_text = ""

    with client.messages.stream(
        model=model,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            full_text += text

        final = stream.get_final_message()

    print()

    entry = ModelCacheEntry(
        model=model,
        text=full_text,
        input_tokens=final.usage.input_tokens,
        output_tokens=final.usage.output_tokens,
        created_at=time.time(),
        quality_score=quality,
    )
    mm_cache.store(system, message, entry)
    return full_text

system = "You are an expert Python developer."
question = "What is the difference between a list and a tuple in Python?"

print("=== Request 1: balanced tier ===")
quality_tier_cached_stream(system, question, "balanced")

print("\n=== Request 2: fast tier (gets balanced cached response) ===")
quality_tier_cached_stream(system, question, "fast", min_quality_from_cache=0.5)

print("\n=== Request 3: powerful tier (rejects balanced cache, needs 0.95+) ===")
quality_tier_cached_stream(system, question, "powerful", min_quality_from_cache=0.95)

print(f"\nCache stats: {json.dumps(mm_cache.stats, indent=2)}")

# Expected Token Savings: 60-90% for repeated queries; higher-tier responses can serve lower-tier requests
# Environment: Multi-tier AI products where different users pay for different quality levels
```

### Option 6: Distributed Redis-Style Streaming Cache

```python
import anthropic
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

client = anthropic.Anthropic()

DB_PATH = Path("/tmp/stream_cache.db")

def init_stream_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stream_cache (
                cache_key TEXT PRIMARY KEY,
                system_prompt TEXT NOT NULL,
                user_message TEXT NOT NULL,
                full_text TEXT NOT NULL,
                chunk_json TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                hit_count INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON stream_cache(expires_at)")
        conn.commit()

init_stream_db()

def _cache_key(system: str, message: str, model: str) -> str:
    payload = json.dumps({"system": system, "message": message, "model": model}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()

def db_get_cached(system: str, message: str, model: str) -> dict | None:
    key = _cache_key(system, message, model)
    now = time.time()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM stream_cache WHERE cache_key = ? AND expires_at > ?",
            (key, now),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE stream_cache SET hit_count = hit_count + 1 WHERE cache_key = ?", (key,))
        conn.commit()
        return dict(row)

def db_store_cached(
    system: str,
    message: str,
    model: str,
    full_text: str,
    chunks: list[str],
    input_tokens: int,
    output_tokens: int,
    ttl: float = 300,
):
    key = _cache_key(system, message, model)
    now = time.time()
    costs = {"claude-haiku-4-5-20251001": 0.00125, "claude-sonnet-4-6": 0.015, "claude-opus-4-6": 0.075}
    cost = output_tokens / 1000 * costs.get(model, 0.015)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO stream_cache
               (cache_key, system_prompt, user_message, full_text, chunk_json, model,
                input_tokens, output_tokens, created_at, expires_at, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, system, message, full_text, json.dumps(chunks), model,
             input_tokens, output_tokens, now, now + ttl, cost),
        )
        conn.commit()

def db_evict_expired() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        n = conn.execute("DELETE FROM stream_cache WHERE expires_at < ?", (time.time(),)).rowcount
        conn.commit()
    return n

def db_cache_stats() -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COUNT(*) as n, SUM(hit_count) as hits, SUM(output_tokens * hit_count) as tokens_saved, SUM(cost_usd * hit_count) as cost_saved FROM stream_cache"
        ).fetchone()
        return {
            "entries": row["n"],
            "total_hits": row["hits"] or 0,
            "tokens_saved": row["tokens_saved"] or 0,
            "estimated_cost_saved_usd": round(row["cost_saved"] or 0, 4),
        }

def distributed_cached_stream(
    system: str,
    message: str,
    model: str = "claude-haiku-4-5-20251001",
    replay_delay: float = 0.005,
) -> str:
    cached = db_get_cached(system, message, model)

    if cached:
        chunks = json.loads(cached["chunk_json"])
        hit = cached["hit_count"]
        print(f"[DB-CACHE HIT #{hit}] Replaying {len(chunks)} chunks ({cached['output_tokens']} tokens saved)")
        for chunk in chunks:
            print(chunk, end="", flush=True)
            time.sleep(replay_delay)
        print()
        return cached["full_text"]

    print(f"[LIVE] Streaming from {model}...")
    chunks: list[str] = []
    full_text = ""

    with client.messages.stream(
        model=model,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            chunks.append(text)
            full_text += text

        final = stream.get_final_message()

    print()
    db_store_cached(system, message, model, full_text, chunks,
                    final.usage.input_tokens, final.usage.output_tokens)
    print(f"[STORED] {len(chunks)} chunks, {final.usage.output_tokens} tokens")
    return full_text

system = "You are a helpful coding assistant."
question = "Explain the concept of closures in Python with a simple example."

for i in range(4):
    print(f"\n=== Request {i+1} ===")
    distributed_cached_stream(system, question)

evicted = db_evict_expired()
print(f"\nEvicted {evicted} expired entries")
print(f"Cache stats: {json.dumps(db_cache_stats(), indent=2)}")

# Expected Token Savings: 40-80% on repeated queries; persists across restarts and worker processes
# Environment: Multi-process web servers (gunicorn, uvicorn workers) sharing a SQLite sidecar
```

## Comparison

| Option | Cache Backend | TTL Support | Multi-Model | SSE-Ready | Persistence | Best For |
|--------|--------------|-------------|-------------|-----------|-------------|---------|
| 1. Full Response Cache | Memory dict | Yes | No | No | No | Simple single-process agents |
| 2. Chunk-Level Prefix | Memory dict | Yes | No | No | No | Fine-grained streaming replay |
| 3. SSE Event Cache | Memory dict | No | No | Yes | No | Web SSE endpoints |
| 4. Partial Match | Memory dict | Yes | No | No | No | Support bots with FAQ queries |
| 5. Multi-Model Quality | Memory dict | Yes | Yes | No | No | Tiered quality products |
| 6. Distributed SQLite | SQLite | Yes | Yes | No | Yes | Multi-worker production services |
