---
title: "Agent Doesn't Implement Memory Warming for Frequent Patterns"
description: "Cold-starting every session forces agents to re-retrieve the same memories repeatedly. Memory warming pre-loads frequently accessed facts, user preferences, and recurring context into the session before the first query, eliminating per-turn retrieval latency for common cases."
difficulty: intermediate
category: memory
tags: [memory, warming, caching, performance, latency, session-startup]
---

## Problem

Every session begins cold — the agent knows nothing about the user or their typical tasks. For returning users, this means re-fetching the same preferences, domain knowledge, and recurring context on every session start and often on every turn. Memory warming solves this by predicting what will be needed and pre-loading it before the first query arrives, turning O(N retrievals per session) into O(1) at startup.

```python
# BAD: cold retrieval on every turn — same memories fetched repeatedly
async def handle_turn(user_id: str, message: str) -> str:
    preferences = await memory.search(user_id, message)  # always fetches
    context = await memory.get_recent(user_id)           # always fetches
    return await call_model(preferences + context + message)
```

## Solution 1: Access-Frequency-Based Pre-loading

Track retrieval frequency and pre-load the most-accessed memories on session start.

```python
import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
FREQ_FILE = Path("/tmp/memory_access_freq.json")

class FrequencyTracker:
    def __init__(self):
        self._counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._last_access: dict[str, dict[str, float]] = defaultdict(dict)
        self._load()

    def _load(self):
        if FREQ_FILE.exists():
            try:
                data = json.loads(FREQ_FILE.read_text())
                for user_id, memories in data.get("counts", {}).items():
                    self._counts[user_id].update(memories)
            except Exception:
                pass

    def _save(self):
        FREQ_FILE.write_text(json.dumps({"counts": {
            uid: dict(mems) for uid, mems in self._counts.items()
        }}))

    def record_access(self, user_id: str, memory_key: str):
        self._counts[user_id][memory_key] += 1
        self._last_access[user_id][memory_key] = time.time()
        if sum(self._counts[user_id].values()) % 10 == 0:
            self._save()

    def top_memories(self, user_id: str, n: int = 10) -> list[str]:
        user_counts = self._counts.get(user_id, {})
        return sorted(user_counts, key=lambda k: user_counts[k], reverse=True)[:n]

tracker = FrequencyTracker()

# Simulated memory store
MEMORY_STORE: dict[str, dict[str, str]] = {
    "user-001": {
        "preferred_language": "Python",
        "timezone": "UTC-8",
        "coding_style": "prefers functional patterns",
        "recent_project": "building a RAG pipeline",
        "expertise": "senior backend engineer",
        "communication_style": "concise, technical",
        "team": "Platform Engineering",
        "tools": "Anthropic SDK, FastAPI, PostgreSQL",
    }
}

async def fetch_memory(user_id: str, key: str) -> str | None:
    await asyncio.sleep(0.01)  # simulate DB latency
    return MEMORY_STORE.get(user_id, {}).get(key)

class WarmMemorySession:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.warm_cache: dict[str, str] = {}
        self.warm_time: float = 0

    async def warm(self, top_n: int = 8) -> int:
        start = time.time()
        top_keys = tracker.top_memories(self.user_id, n=top_n)

        if not top_keys:
            # Cold user: pre-load all available memories
            top_keys = list(MEMORY_STORE.get(self.user_id, {}).keys())[:top_n]

        results = await asyncio.gather(*[
            fetch_memory(self.user_id, key) for key in top_keys
        ])
        for key, value in zip(top_keys, results):
            if value is not None:
                self.warm_cache[key] = value

        self.warm_time = time.time() - start
        print(f"[Warm] Loaded {len(self.warm_cache)} memories in {self.warm_time*1000:.1f}ms")
        return len(self.warm_cache)

    async def get(self, key: str) -> str | None:
        if key in self.warm_cache:
            tracker.record_access(self.user_id, key)
            return self.warm_cache[key]
        # Cache miss — fetch and record for future warming
        value = await fetch_memory(self.user_id, key)
        if value:
            tracker.record_access(self.user_id, key)
            self.warm_cache[key] = value
        return value

    def as_context(self) -> str:
        return "\n".join(f"{k}: {v}" for k, v in self.warm_cache.items())

async def handle_session(user_id: str, messages: list[str]) -> list[str]:
    session = WarmMemorySession(user_id)
    await session.warm(top_n=8)

    responses = []
    for message in messages:
        context = session.as_context()
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=f"User context:\n{context}",
            messages=[{"role": "user", "content": message}]
        )
        output = response.content[0].text if response.content else ""
        responses.append(output)

    return responses

async def main():
    # Simulate access history
    for _ in range(5):
        tracker.record_access("user-001", "preferred_language")
    for _ in range(3):
        tracker.record_access("user-001", "coding_style")

    responses = await handle_session(
        "user-001",
        ["What language should I use for this task?", "How should I structure my code?"]
    )
    for r in responses:
        print(r[:150])

asyncio.run(main())
```

## Solution 2: Predictive Warming from Session Intent

Infer the likely session topic from the first message and warm topic-relevant memories.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Memory organized by topic
TOPIC_MEMORY_MAP: dict[str, list[str]] = {
    "coding": ["preferred_language", "coding_style", "recent_project", "tools", "expertise"],
    "communication": ["communication_style", "team", "timezone"],
    "planning": ["recent_project", "team", "expertise", "timezone"],
    "debugging": ["preferred_language", "tools", "recent_project", "coding_style"],
    "general": ["preferred_language", "expertise", "communication_style"],
}

async def predict_topic(first_message: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{
            "role": "user",
            "content": (
                f"Classify this message into one topic: coding, communication, planning, debugging, general.\n"
                f"Output only the topic name.\n\nMessage: {first_message}"
            )
        }]
    )
    topic = response.content[0].text.strip().lower()
    return topic if topic in TOPIC_MEMORY_MAP else "general"

async def warm_for_topic(user_id: str, topic: str) -> dict[str, str]:
    keys = TOPIC_MEMORY_MAP.get(topic, TOPIC_MEMORY_MAP["general"])
    user_memories = MEMORY_STORE_2.get(user_id, {})
    return {k: v for k, v in user_memories.items() if k in keys}

MEMORY_STORE_2: dict[str, dict[str, str]] = {
    "user-001": {
        "preferred_language": "Python",
        "coding_style": "functional, type-annotated",
        "recent_project": "async task queue",
        "tools": "asyncio, pydantic, FastAPI",
        "expertise": "8 years Python, 3 years distributed systems",
        "communication_style": "direct, technical",
        "team": "Backend Platform",
        "timezone": "PST",
    }
}

async def predictive_warm_session(user_id: str, first_message: str, remaining_messages: list[str]) -> list[str]:
    # Predict and warm simultaneously with first message processing
    topic_task = asyncio.create_task(predict_topic(first_message))
    topic = await topic_task

    warm_cache = await warm_for_topic(user_id, topic)
    print(f"[Predictive Warm] Topic: {topic}, loaded: {list(warm_cache.keys())}")

    all_messages = [first_message] + remaining_messages
    responses = []
    context = "\n".join(f"{k}: {v}" for k, v in warm_cache.items())

    for message in all_messages:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=f"User context:\n{context}",
            messages=[{"role": "user", "content": message}]
        )
        responses.append(response.content[0].text if response.content else "")

    return responses

async def main():
    responses = await predictive_warm_session(
        "user-001",
        "I have a bug in my async code",
        ["The error is RuntimeError: Task attached to a different loop"]
    )
    for i, r in enumerate(responses):
        print(f"[Response {i+1}]: {r[:200]}")

asyncio.run(main())
```

## Solution 3: Tiered Memory Cache with TTL

Maintain a multi-tier cache: hot (always loaded), warm (loaded on demand, kept for session), cold (fetched per-query).

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class CacheEntry:
    value: str
    loaded_at: float = field(default_factory=time.time)
    access_count: int = 0
    tier: str = "warm"  # "hot" | "warm" | "cold"

    def is_stale(self, ttl_seconds: float) -> bool:
        return time.time() - self.loaded_at > ttl_seconds

class TieredMemoryCache:
    HOT_TTL = 3600      # 1 hour — always loaded
    WARM_TTL = 600      # 10 min — kept during session
    COLD_TTL = 60       # 1 min — short-lived fetches

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._cache: dict[str, CacheEntry] = {}
        self._hot_keys: set[str] = set()

    def mark_hot(self, keys: list[str]):
        self._hot_keys.update(keys)

    async def preload_hot(self):
        """Load all hot keys at session start in parallel."""
        missing_hot = [k for k in self._hot_keys if k not in self._cache]
        if not missing_hot:
            return
        values = await asyncio.gather(*[self._fetch(k) for k in missing_hot])
        for key, value in zip(missing_hot, values):
            if value:
                self._cache[key] = CacheEntry(value=value, tier="hot")
        print(f"[Hot Tier] Preloaded: {list(self._hot_keys)}")

    async def _fetch(self, key: str) -> str | None:
        await asyncio.sleep(0.005)  # simulate latency
        return ALL_MEMORIES.get(self.user_id, {}).get(key)

    async def get(self, key: str) -> str | None:
        entry = self._cache.get(key)
        ttl = self.HOT_TTL if key in self._hot_keys else self.WARM_TTL
        if entry and not entry.is_stale(ttl):
            entry.access_count += 1
            return entry.value

        value = await self._fetch(key)
        if value:
            tier = "hot" if key in self._hot_keys else "warm"
            self._cache[key] = CacheEntry(value=value, tier=tier)
            return value
        return None

    def stats(self) -> dict:
        return {
            "total_cached": len(self._cache),
            "hot": sum(1 for e in self._cache.values() if e.tier == "hot"),
            "warm": sum(1 for e in self._cache.values() if e.tier == "warm"),
            "total_accesses": sum(e.access_count for e in self._cache.values()),
        }

ALL_MEMORIES: dict[str, dict[str, str]] = {
    "user-001": {
        "name": "Alice",
        "preferred_language": "Python",
        "timezone": "PST",
        "coding_style": "functional",
        "recent_project": "API rate limiter",
        "team": "Platform",
        "tools": "FastAPI, Redis, asyncio",
        "expertise": "distributed systems",
        "communication_style": "technical",
    }
}

async def session_with_tiered_cache(user_id: str, messages: list[str]) -> list[str]:
    cache = TieredMemoryCache(user_id)
    # Define hot keys — always needed for this user type
    cache.mark_hot(["name", "preferred_language", "timezone", "coding_style"])
    await cache.preload_hot()

    responses = []
    for message in messages:
        # Fetch additional warm keys relevant to this message
        extra_keys = ["recent_project", "tools"] if "project" in message.lower() or "code" in message.lower() else []
        extra_values = await asyncio.gather(*[cache.get(k) for k in extra_keys])
        extra_context = "\n".join(
            f"{k}: {v}" for k, v in zip(extra_keys, extra_values) if v
        )

        hot_context = "\n".join(
            f"{k}: {v}" async for k, v in
            ((k, await cache.get(k)) for k in cache._hot_keys)
            if v  # type: ignore
        )
        # Actually build context synchronously from cache (already loaded)
        hot_context = "\n".join(
            f"{k}: {cache._cache[k].value}"
            for k in cache._hot_keys
            if k in cache._cache
        )
        context = hot_context + ("\n" + extra_context if extra_context else "")

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=f"User context:\n{context}",
            messages=[{"role": "user", "content": message}]
        )
        responses.append(response.content[0].text if response.content else "")

    print(f"[Cache Stats] {cache.stats()}")
    return responses

async def main():
    responses = await session_with_tiered_cache(
        "user-001",
        ["Hi, what should I work on today?", "Show me a code example for my current project"]
    )
    for i, r in enumerate(responses):
        print(f"\n[Turn {i+1}]: {r[:200]}")

asyncio.run(main())
```

## Solution 4: Background Async Warming During First Response

Start warming in the background while the first response is being generated.

```python
import asyncio
import time
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

MEMORY_DB: dict[str, dict[str, str]] = {
    "user-001": {
        "name": "Alice",
        "preferred_language": "Python",
        "expertise": "backend systems",
        "recent_project": "distributed cache",
        "tools": "Redis, asyncio, Docker",
        "team": "Platform Engineering",
        "coding_style": "type-annotated, functional",
        "timezone": "PST",
    }
}

async def fetch_all_memories(user_id: str) -> dict[str, str]:
    await asyncio.sleep(0.05)  # simulate DB round trip
    return MEMORY_DB.get(user_id, {})

async def generate_first_response(message: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": message}]
    )
    return response.content[0].text if response.content else ""

async def session_with_background_warming(
    user_id: str,
    messages: list[str]
) -> list[str]:
    if not messages:
        return []

    # Start first response and memory warming concurrently
    first_msg = messages[0]
    start = time.time()

    first_response_task = asyncio.create_task(generate_first_response(first_msg))
    warming_task = asyncio.create_task(fetch_all_memories(user_id))

    first_response = await first_response_task
    warm_memories = await warming_task

    warm_time = time.time() - start
    print(f"[Background Warm] Completed in {warm_time*1000:.1f}ms, {len(warm_memories)} memories loaded")

    context = "\n".join(f"{k}: {v}" for k, v in warm_memories.items())
    responses = [first_response]

    # Remaining messages use full warm context
    for message in messages[1:]:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=f"User context:\n{context}",
            messages=[{"role": "user", "content": message}]
        )
        responses.append(response.content[0].text if response.content else "")

    return responses

async def main():
    responses = await session_with_background_warming(
        "user-001",
        [
            "What's the best way to implement a cache?",
            "What language and tools should I use?",
        ]
    )
    for i, r in enumerate(responses):
        print(f"\n[Turn {i+1}]: {r[:200]}")

asyncio.run(main())
```

## Solution 5: Persona Snapshot for Instant Warming

Pre-compute and store a compressed persona snapshot so warming is a single fast read.

```python
import asyncio
import json
import time
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
SNAPSHOT_DIR = Path("/tmp/persona_snapshots")
SNAPSHOT_DIR.mkdir(exist_ok=True)
SNAPSHOT_TTL = 3600  # 1 hour

async def build_persona_snapshot(user_id: str, raw_memories: dict) -> str:
    """Compress raw memories into a concise persona description."""
    memories_text = "\n".join(f"- {k}: {v}" for k, v in raw_memories.items())
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Compress these user facts into a 3-4 sentence persona summary "
                f"that captures the most important context for an AI assistant.\n\n{memories_text}"
            )
        }]
    )
    return response.content[0].text if response.content else memories_text

def save_snapshot(user_id: str, snapshot: str):
    path = SNAPSHOT_DIR / f"{user_id}.json"
    path.write_text(json.dumps({"snapshot": snapshot, "created_at": time.time()}))

def load_snapshot(user_id: str) -> str | None:
    path = SNAPSHOT_DIR / f"{user_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    age = time.time() - data.get("created_at", 0)
    if age > SNAPSHOT_TTL:
        path.unlink()
        return None
    return data["snapshot"]

RAW_MEMORIES: dict[str, dict] = {
    "user-001": {
        "name": "Alice",
        "role": "Senior Backend Engineer",
        "preferred_language": "Python",
        "expertise": "distributed systems, async programming",
        "current_project": "building a rate limiter service",
        "coding_style": "functional, type-annotated, test-first",
        "team": "Platform Engineering at Acme Corp",
        "tools": "FastAPI, Redis, PostgreSQL, Docker",
        "timezone": "PST (UTC-8)",
        "communication_style": "direct and technical, skips basics",
    }
}

async def warm_from_snapshot(user_id: str) -> str:
    snapshot = load_snapshot(user_id)
    if snapshot:
        print(f"[Snapshot] Cache hit — instant warm")
        return snapshot

    print(f"[Snapshot] Cache miss — building from raw memories")
    raw = RAW_MEMORIES.get(user_id, {})
    snapshot = await build_persona_snapshot(user_id, raw)
    save_snapshot(user_id, snapshot)
    return snapshot

async def handle_session_with_snapshot(user_id: str, messages: list[str]) -> list[str]:
    persona = await warm_from_snapshot(user_id)
    system = f"User persona:\n{persona}"
    responses = []

    for message in messages:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": message}]
        )
        responses.append(response.content[0].text if response.content else "")

    return responses

async def main():
    responses = await handle_session_with_snapshot(
        "user-001",
        ["What's the best data structure for my rate limiter?"]
    )
    print(responses[0][:300])

asyncio.run(main())
```

## Solution 6: Collaborative Pre-warming with Usage Prediction

Analyze past session patterns to predict which memories will be needed and pre-warm them across multiple users efficiently.

```python
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
USAGE_LOG = Path("/tmp/memory_usage_log.jsonl")

def log_memory_usage(user_id: str, session_id: str, keys_used: list[str]):
    with USAGE_LOG.open("a") as f:
        f.write(json.dumps({
            "user_id": user_id,
            "session_id": session_id,
            "keys": keys_used,
            "hour_of_day": time.localtime().tm_hour,
            "timestamp": time.time()
        }) + "\n")

def predict_next_session_keys(user_id: str, top_n: int = 6) -> list[str]:
    """Analyze past sessions to predict which keys will be needed."""
    if not USAGE_LOG.exists():
        return []
    user_usage: Counter = Counter()
    for line in USAGE_LOG.read_text().splitlines():
        try:
            entry = json.loads(line)
            if entry["user_id"] == user_id:
                for key in entry["keys"]:
                    user_usage[key] += 1
        except Exception:
            continue
    return [k for k, _ in user_usage.most_common(top_n)]

class PredictiveWarmPool:
    """Pre-warm memories for multiple users in batch at session-start time."""

    def __init__(self):
        self._warmed: dict[str, dict[str, str]] = {}

    async def pre_warm_batch(self, user_ids: list[str]) -> dict[str, int]:
        """Called at system startup or cron — warms memories for expected users."""
        warm_counts = {}
        tasks = {uid: asyncio.create_task(self._warm_user(uid)) for uid in user_ids}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for uid, result in zip(tasks.keys(), results):
            if isinstance(result, int):
                warm_counts[uid] = result
        return warm_counts

    async def _warm_user(self, user_id: str) -> int:
        predicted = predict_next_session_keys(user_id, top_n=6)
        if not predicted:
            predicted = list(FULL_MEMORIES.get(user_id, {}).keys())[:6]

        user_mems = FULL_MEMORIES.get(user_id, {})
        self._warmed[user_id] = {k: user_mems[k] for k in predicted if k in user_mems}
        return len(self._warmed[user_id])

    def get_warm_context(self, user_id: str) -> str:
        memories = self._warmed.get(user_id, {})
        return "\n".join(f"{k}: {v}" for k, v in memories.items())

FULL_MEMORIES: dict[str, dict[str, str]] = {
    "user-001": {
        "name": "Alice", "preferred_language": "Python",
        "expertise": "backend", "project": "cache service",
        "tools": "Redis, asyncio", "team": "Platform",
    },
    "user-002": {
        "name": "Bob", "preferred_language": "Go",
        "expertise": "frontend", "project": "dashboard",
        "tools": "React, GraphQL", "team": "Product",
    },
}

# Global warm pool (in practice: populated by cron or startup hook)
warm_pool = PredictiveWarmPool()

async def main():
    # Simulate pre-warming at system startup
    warm_counts = await warm_pool.pre_warm_batch(["user-001", "user-002"])
    print(f"[Pool] Pre-warmed: {warm_counts}")

    # Simulate logging past usage to seed predictions
    log_memory_usage("user-001", "sess-prev", ["preferred_language", "tools", "project"])

    # Handle a session — context is instantly available
    context = warm_pool.get_warm_context("user-001")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"User context:\n{context}",
        messages=[{"role": "user", "content": "What should I use for my project?"}]
    )
    print(f"\n[Session Response]: {response.content[0].text[:300]}")

asyncio.run(main())
```

## Comparison

| Approach | Cold Start Latency | Memory Freshness | Complexity | Best For |
|---|---|---|---|---|
| Frequency-Based Pre-load | Near-zero | Session-scoped | Low | Returning users with usage history |
| Predictive from Intent | Low (1 classify call) | Per-intent | Medium | Diverse session types |
| Tiered Cache (Hot/Warm/Cold) | Zero for hot | TTL-controlled | Medium | High-traffic production |
| Background Warming | Zero for first turn | Per-session | Low | First-message latency matters |
| Persona Snapshot | Zero (cache hit) | TTL-controlled | Medium | Stable user personas |
| Collaborative Pool | Zero (pre-warmed) | Pre-session | High | Multi-user systems with predictable traffic |

**Rule of thumb**: Start with background warming (low complexity, zero first-turn cost), add persona snapshots for stable users, and graduate to a pre-warm pool once you have usage data to make accurate predictions.
