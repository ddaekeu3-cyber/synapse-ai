---
title: "Agent Doesn't Implement Request Deduplication at the API Gateway Level"
description: "Detect and coalesce duplicate or near-duplicate agent requests before they reach the model—eliminating redundant API calls caused by retries, webhooks, or concurrent user actions and reducing costs proportionally."
difficulty: intermediate
category: reliability
tags: [reliability, deduplication, idempotency, caching, cost-optimization]
---

## Problem

Duplicate requests reach the LLM and are processed independently. Sources include: webhook systems delivering the same event twice, clients retrying after a timeout that succeeded, users double-clicking a button, or parallel agents receiving the same task. Without deduplication at the gateway, identical requests each consume tokens, incur latency, and may produce divergent responses for what should be a single operation.

## Solutions

### Option 1: Request Fingerprint Cache

Hash incoming requests and return cached responses for duplicates within a configurable window.

```python
import asyncio
import hashlib
import json
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class CacheEntry:
    response: str
    created_at: float
    hit_count: int = 0

class RequestFingerprintCache:
    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 1000):
        self._cache: dict[str, CacheEntry] = {}
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._total_deduped = 0

    def _fingerprint(self, messages: list[dict], model: str, max_tokens: int) -> str:
        payload = json.dumps({
            "messages": messages,
            "model": model,
            "max_tokens": max_tokens,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, fingerprint: str) -> str | None:
        entry = self._cache.get(fingerprint)
        if entry is None:
            return None
        if time.monotonic() - entry.created_at > self._ttl:
            del self._cache[fingerprint]
            return None
        entry.hit_count += 1
        self._total_deduped += 1
        return entry.response

    def put(self, fingerprint: str, response: str):
        if len(self._cache) >= self._max_entries:
            # Evict oldest entry
            oldest = min(self._cache.items(), key=lambda x: x[1].created_at)
            del self._cache[oldest[0]]
        self._cache[fingerprint] = CacheEntry(response=response, created_at=time.monotonic())

    def stats(self) -> dict:
        valid = {k: v for k, v in self._cache.items()
                 if time.monotonic() - v.created_at <= self._ttl}
        return {
            "cached_requests": len(valid),
            "total_deduplicated": self._total_deduped,
            "top_repeated": max(
                (v.hit_count for v in valid.values()), default=0
            ),
        }

class DeduplicatingGateway:
    def __init__(self, ttl_seconds: float = 300.0):
        self._cache = RequestFingerprintCache(ttl_seconds=ttl_seconds)
        self._inflight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def complete(
        self,
        messages: list[dict],
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 200,
    ) -> tuple[str, bool]:
        """Returns (response, was_deduplicated)."""
        fingerprint = self._cache._fingerprint(messages, model, max_tokens)

        # Check cache first
        cached = self._cache.get(fingerprint)
        if cached:
            return cached, True

        # Coalesce in-flight duplicates
        async with self._lock:
            if fingerprint in self._inflight:
                future = self._inflight[fingerprint]
            else:
                future = asyncio.get_event_loop().create_future()
                self._inflight[fingerprint] = future

                async def execute():
                    try:
                        response = await client.messages.create(
                            model=model,
                            max_tokens=max_tokens,
                            messages=messages,
                        )
                        text = response.content[0].text
                        self._cache.put(fingerprint, text)
                        future.set_result(text)
                    except Exception as e:
                        future.set_exception(e)
                    finally:
                        async with self._lock:
                            self._inflight.pop(fingerprint, None)

                asyncio.create_task(execute())

        result = await future
        was_deduped = fingerprint not in self._cache._cache  # Already consumed
        return result, False  # First request is not a duplicate

async def demo_fingerprint_cache():
    gateway = DeduplicatingGateway(ttl_seconds=60.0)

    messages = [{"role": "user", "content": "What is the capital of France?"}]

    # First request — hits the API
    r1, deduped = await gateway.complete(messages)
    print(f"Request 1 (deduped={deduped}): {r1.strip()[:60]}")

    # Duplicate — should hit cache
    r2, deduped = await gateway.complete(messages)
    print(f"Request 2 (deduped={deduped}): {r2.strip()[:60]}")

    # Different request — new API call
    messages2 = [{"role": "user", "content": "What is the capital of Germany?"}]
    r3, deduped = await gateway.complete(messages2)
    print(f"Request 3 (deduped={deduped}): {r3.strip()[:60]}")

    print(f"\nCache stats: {gateway._cache.stats()}")

asyncio.run(demo_fingerprint_cache())
```

### Option 2: Idempotency Key Deduplication

Require callers to provide an idempotency key; deduplicate on that key regardless of payload differences.

```python
import asyncio
import time
import uuid
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class IdempotencyRecord:
    key: str
    response: str
    created_at: float
    request_id: str

class IdempotencyStore:
    def __init__(self, ttl_seconds: float = 3600.0):
        self._store: dict[str, IdempotencyRecord] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> IdempotencyRecord | None:
        record = self._store.get(key)
        if record is None:
            return None
        if time.monotonic() - record.created_at > self._ttl:
            del self._store[key]
            return None
        return record

    def put(self, key: str, response: str, request_id: str) -> IdempotencyRecord:
        record = IdempotencyRecord(
            key=key,
            response=response,
            created_at=time.monotonic(),
            request_id=request_id,
        )
        self._store[key] = record
        return record

    def count(self) -> int:
        now = time.monotonic()
        return sum(1 for r in self._store.values() if now - r.created_at <= self._ttl)

class IdempotentAgent:
    def __init__(self):
        self._store = IdempotencyStore(ttl_seconds=3600.0)
        self._in_progress: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._api_calls = 0
        self._deduped_calls = 0

    async def complete(
        self,
        messages: list[dict],
        idempotency_key: str | None = None,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 200,
    ) -> dict:
        idem_key = idempotency_key or str(uuid.uuid4())

        # Check if already processed
        existing = self._store.get(idem_key)
        if existing:
            self._deduped_calls += 1
            return {
                "response": existing.response,
                "idempotency_key": idem_key,
                "request_id": existing.request_id,
                "deduplicated": True,
                "original_request_id": existing.request_id,
            }

        # Check if currently in-progress (concurrent duplicate)
        async with self._lock:
            if idem_key in self._in_progress:
                event = self._in_progress[idem_key]
                await event.wait()
                existing = self._store.get(idem_key)
                if existing:
                    self._deduped_calls += 1
                    return {
                        "response": existing.response,
                        "idempotency_key": idem_key,
                        "request_id": existing.request_id,
                        "deduplicated": True,
                    }

            event = asyncio.Event()
            self._in_progress[idem_key] = event

        try:
            self._api_calls += 1
            request_id = str(uuid.uuid4())
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
            )
            text = response.content[0].text
            record = self._store.put(idem_key, text, request_id)
            return {
                "response": text,
                "idempotency_key": idem_key,
                "request_id": request_id,
                "deduplicated": False,
            }
        finally:
            async with self._lock:
                self._in_progress.pop(idem_key, None)
            event.set()

    def stats(self) -> dict:
        return {
            "api_calls": self._api_calls,
            "deduplicated": self._deduped_calls,
            "dedup_rate": f"{self._deduped_calls / max(self._api_calls + self._deduped_calls, 1):.0%}",
            "stored_keys": self._store.count(),
        }

async def demo_idempotency_keys():
    agent = IdempotentAgent()

    # First request with idempotency key
    idem_key = "task-abc-001"
    r1 = await agent.complete(
        [{"role": "user", "content": "Summarize the benefits of async programming."}],
        idempotency_key=idem_key
    )
    print(f"Request 1 (deduped={r1['deduplicated']}): {r1['response'].strip()[:60]}")

    # Retry with same key (webhook retry simulation)
    r2 = await agent.complete(
        [{"role": "user", "content": "Summarize the benefits of async programming."}],
        idempotency_key=idem_key
    )
    print(f"Request 2 (deduped={r2['deduplicated']}): {r2['response'].strip()[:60]}")

    # Different key — new API call
    r3 = await agent.complete(
        [{"role": "user", "content": "What is Python?"}],
        idempotency_key="task-xyz-002"
    )
    print(f"Request 3 (deduped={r3['deduplicated']}): {r3['response'].strip()[:60]}")

    print(f"\nStats: {agent.stats()}")

asyncio.run(demo_idempotency_keys())
```

### Option 3: Content-Hash Based Semantic Deduplication

Normalize requests before fingerprinting to catch semantically identical queries that differ only in whitespace or case.

```python
import asyncio
import hashlib
import re
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

def normalize_text(text: str) -> str:
    """Normalize text to catch near-identical queries."""
    # Lowercase
    text = text.lower()
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    # Remove trailing punctuation that doesn't change meaning
    text = re.sub(r'[.!?]+$', '', text)
    # Remove common filler words that don't change intent
    # (Be conservative — only remove words that are definitely noise)
    text = re.sub(r'\bplease\b', '', text)
    text = re.sub(r'\bcan you\b', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def semantic_fingerprint(messages: list[dict]) -> str:
    """Create fingerprint from normalized message content."""
    normalized_parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            content = normalize_text(content)
        normalized_parts.append(f"{role}:{content}")
    combined = "|".join(normalized_parts)
    return hashlib.sha256(combined.encode()).hexdigest()

@dataclass
class NormalizedDeduplicator:
    ttl_seconds: float = 300.0
    _cache: dict[str, tuple[str, float]] = field(default_factory=dict)
    _hit_log: list[dict] = field(default_factory=list)

    def get(self, fingerprint: str) -> str | None:
        entry = self._cache.get(fingerprint)
        if entry is None:
            return None
        response, created_at = entry
        if time.monotonic() - created_at > self.ttl_seconds:
            del self._cache[fingerprint]
            return None
        return response

    def put(self, fingerprint: str, response: str):
        self._cache[fingerprint] = (response, time.monotonic())

async def normalized_complete(
    deduplicator: NormalizedDeduplicator,
    messages: list[dict],
    original_query: str,
) -> tuple[str, bool]:
    fingerprint = semantic_fingerprint(messages)
    cached = deduplicator.get(fingerprint)

    if cached:
        deduplicator._hit_log.append({
            "original_query": original_query[:60],
            "fingerprint": fingerprint[:12],
        })
        return cached, True

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=messages,
    )
    text = response.content[0].text
    deduplicator.put(fingerprint, text)
    return text, False

async def demo_semantic_dedup():
    dedup = NormalizedDeduplicator()

    # These should all map to the same fingerprint
    equivalent_queries = [
        "What is Python?",
        "what is python",
        "What is Python",
        "Can you tell me what is Python?",
        "Please  what is python.",  # Extra space, different phrasing
    ]

    api_calls = 0
    dedup_calls = 0

    for query in equivalent_queries:
        messages = [{"role": "user", "content": query}]
        response, deduped = await normalized_complete(dedup, messages, query)
        if deduped:
            dedup_calls += 1
        else:
            api_calls += 1
        print(f"[{'DEDUP' if deduped else 'API'}] '{query}': {response.strip()[:50]}")

    print(f"\nAPI calls: {api_calls} | Deduplicated: {dedup_calls}")
    print(f"Dedup rate: {dedup_calls / len(equivalent_queries):.0%}")

asyncio.run(demo_semantic_dedup())
```

### Option 4: Webhook Replay Protection

Detect and reject replayed webhook events using event IDs and a short-lived replay window.

```python
import asyncio
import hashlib
import time
import uuid
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class WebhookEvent:
    event_id: str
    payload: dict
    received_at: float = field(default_factory=time.monotonic)

class ReplayProtectionStore:
    """Tracks processed event IDs to prevent duplicate processing."""

    def __init__(self, window_seconds: float = 300.0):
        self._seen: dict[str, float] = {}  # event_id -> processed_at
        self._window = window_seconds
        self._replays_blocked = 0

    def _prune(self):
        cutoff = time.monotonic() - self._window
        expired = [k for k, v in self._seen.items() if v < cutoff]
        for k in expired:
            del self._seen[k]

    def is_replay(self, event_id: str) -> bool:
        self._prune()
        if event_id in self._seen:
            self._replays_blocked += 1
            return True
        return False

    def mark_processed(self, event_id: str):
        self._seen[event_id] = time.monotonic()

    def stats(self) -> dict:
        self._prune()
        return {
            "active_event_ids": len(self._seen),
            "replays_blocked": self._replays_blocked,
        }

class WebhookProcessor:
    def __init__(self):
        self._replay_store = ReplayProtectionStore(window_seconds=300.0)
        self._processed: list[dict] = []

    async def handle_event(self, event: WebhookEvent) -> dict:
        if self._replay_store.is_replay(event.event_id):
            return {
                "status": "duplicate",
                "event_id": event.event_id,
                "message": "Event already processed — replay rejected",
            }

        # Process the event
        user_message = event.payload.get("message", "")
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": user_message}]
        )
        result = response.content[0].text

        self._replay_store.mark_processed(event.event_id)
        self._processed.append({
            "event_id": event.event_id,
            "result": result[:60],
        })

        return {
            "status": "processed",
            "event_id": event.event_id,
            "result": result,
        }

    def stats(self) -> dict:
        return {
            "processed_events": len(self._processed),
            "replay_protection": self._replay_store.stats(),
        }

async def demo_webhook_replay_protection():
    processor = WebhookProcessor()

    # Create events — some duplicates
    events = [
        WebhookEvent("evt-001", {"message": "Summarize Python's key features."}),
        WebhookEvent("evt-002", {"message": "What is asyncio?"}),
        WebhookEvent("evt-001", {"message": "Summarize Python's key features."}),  # Replay!
        WebhookEvent("evt-003", {"message": "What is a decorator?"}),
        WebhookEvent("evt-002", {"message": "What is asyncio?"}),  # Replay!
    ]

    for event in events:
        result = await processor.handle_event(event)
        status = result["status"].upper()
        msg = result.get("result", result.get("message", ""))[:60]
        print(f"[{status}] {event.event_id}: {msg}")

    print(f"\nStats: {processor.stats()}")

asyncio.run(demo_webhook_replay_protection())
```

### Option 5: Concurrent Request Coalescing

When multiple requests arrive simultaneously with the same content, execute only one and share the result.

```python
import asyncio
import hashlib
import json
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

class RequestCoalescer:
    """
    When multiple identical requests arrive concurrently,
    execute only one and broadcast the result to all waiters.
    """

    def __init__(self):
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._stats = {"executed": 0, "coalesced": 0}

    def _key(self, messages: list[dict], model: str, max_tokens: int) -> str:
        payload = json.dumps({"m": messages, "mo": model, "mt": max_tokens}, sort_keys=True)
        return hashlib.md5(payload.encode()).hexdigest()

    async def complete(
        self,
        messages: list[dict],
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 200,
        request_id: str = "?",
    ) -> tuple[str, bool]:
        """Returns (response, was_coalesced)."""
        key = self._key(messages, model, max_tokens)

        async with self._lock:
            if key in self._pending:
                # Piggyback on in-flight request
                future = self._pending[key]
                self._stats["coalesced"] += 1
                coalesced = True
            else:
                future = asyncio.get_event_loop().create_future()
                self._pending[key] = future
                coalesced = False
                self._stats["executed"] += 1

        if coalesced:
            result = await asyncio.shield(future)
            return result, True

        # Execute the actual request
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
            )
            text = response.content[0].text
            future.set_result(text)
            return text, False
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            async with self._lock:
                self._pending.pop(key, None)

    def stats(self) -> dict:
        total = self._stats["executed"] + self._stats["coalesced"]
        return {
            **self._stats,
            "total_requests": total,
            "coalesce_rate": f"{self._stats['coalesced'] / max(total, 1):.0%}",
        }

async def demo_coalescing():
    coalescer = RequestCoalescer()
    messages = [{"role": "user", "content": "What is Python?"}]

    # Launch 10 concurrent identical requests — only 1 should hit the API
    async def make_request(req_id: str) -> str:
        text, coalesced = await coalescer.complete(messages, request_id=req_id)
        return f"[{'coalesced' if coalesced else 'executed'}] {req_id}: {text.strip()[:40]}"

    tasks = [make_request(f"req-{i}") for i in range(10)]
    results = await asyncio.gather(*tasks)

    for r in results:
        print(r)

    print(f"\nStats: {coalescer.stats()}")
    print(f"API calls: 1 for 10 requests = {1/10:.0%} of naive cost")

asyncio.run(demo_coalescing())
```

### Option 6: Tiered Deduplication with TTL Zones

Apply different TTL windows for different request types—long TTL for stable factual queries, short TTL for dynamic ones.

```python
import asyncio
import hashlib
import json
import re
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from enum import Enum

client = AsyncAnthropic()

class RequestStability(Enum):
    STATIC = "static"       # Timeless facts — long TTL (hours)
    DYNAMIC = "dynamic"     # Changes over time — short TTL (minutes)
    EPHEMERAL = "ephemeral" # Real-time — no dedup

TTL_BY_STABILITY = {
    RequestStability.STATIC: 3600.0,    # 1 hour
    RequestStability.DYNAMIC: 300.0,    # 5 minutes
    RequestStability.EPHEMERAL: 0.0,    # No cache
}

DYNAMIC_PATTERNS = [
    r"\b(today|now|current|latest|recent|this (week|month|year))\b",
    r"\b(price|stock|rate|weather)\b",
    r"\b(trending|breaking|news)\b",
]

EPHEMERAL_PATTERNS = [
    r"\b(right now|this second|live|real.?time)\b",
    r"\b(currently happening|at this moment)\b",
]

def classify_stability(query: str) -> RequestStability:
    lower = query.lower()
    for pattern in EPHEMERAL_PATTERNS:
        if re.search(pattern, lower):
            return RequestStability.EPHEMERAL
    for pattern in DYNAMIC_PATTERNS:
        if re.search(pattern, lower):
            return RequestStability.DYNAMIC
    return RequestStability.STATIC

@dataclass
class TieredCacheEntry:
    response: str
    created_at: float
    stability: RequestStability

class TieredDeduplicator:
    def __init__(self):
        self._cache: dict[str, TieredCacheEntry] = {}
        self._stats: dict[str, int] = {"hits": 0, "misses": 0, "skipped": 0}

    def _fingerprint(self, messages: list[dict]) -> str:
        payload = json.dumps(messages, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _ttl_for(self, stability: RequestStability) -> float:
        return TTL_BY_STABILITY[stability]

    def get(self, fingerprint: str) -> str | None:
        entry = self._cache.get(fingerprint)
        if entry is None:
            return None
        ttl = self._ttl_for(entry.stability)
        if ttl == 0 or time.monotonic() - entry.created_at > ttl:
            del self._cache[fingerprint]
            return None
        self._stats["hits"] += 1
        return entry.response

    def put(self, fingerprint: str, response: str, stability: RequestStability):
        if stability == RequestStability.EPHEMERAL:
            return  # Never cache ephemeral
        self._cache[fingerprint] = TieredCacheEntry(
            response=response,
            created_at=time.monotonic(),
            stability=stability,
        )

    async def complete(self, messages: list[dict]) -> tuple[str, bool, RequestStability]:
        query = messages[-1].get("content", "") if messages else ""
        stability = classify_stability(query)

        if stability == RequestStability.EPHEMERAL:
            self._stats["skipped"] += 1
        else:
            fingerprint = self._fingerprint(messages)
            cached = self.get(fingerprint)
            if cached:
                return cached, True, stability
            self._stats["misses"] += 1

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=messages,
        )
        text = response.content[0].text

        if stability != RequestStability.EPHEMERAL:
            fingerprint = self._fingerprint(messages)
            self.put(fingerprint, text, stability)

        return text, False, stability

    def stats(self) -> dict:
        active = {
            s.value: sum(1 for e in self._cache.values() if e.stability == s)
            for s in RequestStability
        }
        return {
            "cache_hits": self._stats["hits"],
            "cache_misses": self._stats["misses"],
            "ephemeral_skipped": self._stats["skipped"],
            "cached_by_tier": active,
        }

async def demo_tiered_dedup():
    dedup = TieredDeduplicator()

    test_cases = [
        ("What is Python?", "static"),           # Timeless
        ("What is Python?", "static-dup"),       # Should dedup
        ("What's the current Bitcoin price?", "dynamic"),
        ("What's the current Bitcoin price?", "dynamic-dup"),  # Short TTL
        ("What's happening right now in tech?", "ephemeral"),  # Never cached
    ]

    for query, label in test_cases:
        messages = [{"role": "user", "content": query}]
        text, cached, stability = await dedup.complete(messages)
        src = "CACHE" if cached else "API"
        print(f"[{src}|{stability.value}] {label}: {text.strip()[:50]}")

    print(f"\nStats: {dedup.stats()}")

asyncio.run(demo_tiered_dedup())
```

## Comparison

| Approach | Dedup Trigger | Concurrency Safe | TTL Control | Best For |
|---|---|---|---|---|
| Request Fingerprint Cache | Content hash | Yes (Lock) | Single TTL | General-purpose gateway |
| Idempotency Key | Caller-provided key | Yes (Event) | Single TTL | Webhook/API consumers |
| Semantic Normalization | Normalized hash | Yes | Single TTL | User-facing chatbots |
| Webhook Replay Protection | Event ID | Yes | Rolling window | Webhook processors |
| Concurrent Request Coalescing | Exact-match hash | Yes (Future) | In-flight only | High-concurrency burst |
| Tiered TTL by Stability | Content + classification | Yes | Per-type TTL | Mixed-stability workloads |

**Choose Idempotency Keys** when you control the client and can generate stable keys—it's the most explicit and correct approach. **Choose Request Fingerprint Cache** when deduplication must be transparent to callers. **Choose Concurrent Request Coalescing** for high-traffic APIs where many identical requests arrive in short windows (e.g., multiple users asking the same trending question simultaneously). **Choose Tiered TTL** when your workload mixes timeless facts (safe to cache for hours) with dynamic information (cache only briefly).
