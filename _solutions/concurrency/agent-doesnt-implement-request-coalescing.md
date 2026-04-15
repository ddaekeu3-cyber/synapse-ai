---
layout: solution
title: "Agent doesn't implement request coalescing"
category: concurrency
description: "When multiple concurrent requests need the same data (config, user profile, knowledge base), the agent fires N separate API or LLM calls instead of coalescing them into one. This multiplies cost and latency proportionally to traffic."
tags: [concurrency, coalescing, deduplication, caching, asyncio, performance]
---

## Symptom

Under moderate load, a single shared resource — a user profile, a knowledge base fetch, a model routing decision — is fetched once per request. Ten concurrent requests to the same endpoint fire ten identical upstream calls. Costs and latency scale linearly with traffic even though the underlying data is identical.

## Root Cause

Each request handler independently calls the upstream source without checking whether another in-flight request is already fetching the same data. There is no shared "pending promise" or "in-flight registry" that allows a second caller to attach to an already-running fetch and share its result.

## Fix

Track in-flight requests by cache key. When a second request arrives for the same key while the first is still running, park it and attach it to the first request's result. The upstream source is called exactly once; all waiters receive the same result.

---

### Option 1 — `asyncio.Future`-based coalescer

```python
import anthropic
import asyncio
import hashlib

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# Maps cache_key → asyncio.Future[str]
_in_flight: dict[str, asyncio.Future[str]] = {}


def _make_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


async def coalesced_create(prompt: str) -> str:
    """
    If an identical prompt is already in flight, wait for its result.
    Otherwise, start a new request and let waiters attach to it.
    """
    key = _make_key(prompt)

    if key in _in_flight:
        # Another coroutine is already fetching this — attach and wait
        print(f"[coalesce] Attaching to in-flight request {key}")
        return await asyncio.shield(_in_flight[key])

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    _in_flight[key] = future

    try:
        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text
        future.set_result(result)
        print(f"[coalesce] Completed {key} — {len(result)} chars")
        return result

    except Exception as exc:
        future.set_exception(exc)
        raise

    finally:
        _in_flight.pop(key, None)


async def simulate_burst(prompt: str, n: int = 10) -> list[str]:
    """Fire N concurrent requests for the same prompt."""
    tasks = [coalesced_create(prompt) for _ in range(n)]
    return await asyncio.gather(*tasks)


async def main() -> None:
    results = await simulate_burst("What is the capital of France?", n=8)
    print(f"Got {len(results)} results, all identical: {len(set(results)) == 1}")


asyncio.run(main())
```

**Expected Token Savings:** N concurrent identical requests → 1 upstream call; for N=10, saves 90 % of model calls and associated input tokens.
**Environment:** Async agents under concurrent load where the same prompts or data lookups repeat across requests.

---

### Option 2 — LRU cache + coalescer for shared knowledge base lookups

```python
import anthropic
import asyncio
import time
from collections import OrderedDict

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

TTL_SECONDS = 60
MAX_CACHE_SIZE = 128


class CoalescingCache:
    def __init__(self, ttl: float = TTL_SECONDS, max_size: int = MAX_CACHE_SIZE) -> None:
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._in_flight: dict[str, asyncio.Future[str]] = {}
        self._ttl = ttl
        self._max_size = max_size

    def _is_fresh(self, key: str) -> bool:
        if key not in self._cache:
            return False
        _, ts = self._cache[key]
        return time.time() - ts < self._ttl

    async def get(self, key: str, fetch_fn) -> str:
        # Cache hit
        if self._is_fresh(key):
            self._cache.move_to_end(key)
            return self._cache[key][0]

        # In-flight coalescing
        if key in self._in_flight:
            return await asyncio.shield(self._in_flight[key])

        # New fetch
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._in_flight[key] = future

        try:
            value = await fetch_fn(key)
            ts = time.time()

            # Evict LRU if at capacity
            while len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)

            self._cache[key] = (value, ts)
            self._cache.move_to_end(key)
            future.set_result(value)
            return value

        except Exception as exc:
            future.set_exception(exc)
            raise

        finally:
            self._in_flight.pop(key, None)


_kb_cache = CoalescingCache(ttl=120)


async def fetch_knowledge(topic: str) -> str:
    """Expensive lookup — should be called at most once per TTL window."""
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are a concise knowledge base. Answer in 1–2 sentences.",
        messages=[{"role": "user", "content": f"What is {topic}?"}],
    )
    return response.content[0].text


async def run_agent(user_topic: str) -> str:
    kb_result = await _kb_cache.get(user_topic, fetch_knowledge)
    # Use kb_result as context for the main agent call...
    return kb_result


async def main() -> None:
    topics = ["Python asyncio"] * 6 + ["REST APIs"] * 4
    results = await asyncio.gather(*[run_agent(t) for t in topics])
    print(f"10 requests → {len(results)} results")
    # fetch_knowledge was called at most 2 times (once per unique topic)


asyncio.run(main())
```

**Expected Token Savings:** 6 identical "Python asyncio" lookups → 1 actual model call; TTL prevents re-fetching within the window; LRU bounds memory.
**Environment:** Knowledge base or FAQ agents under traffic; the TTL window should match how often the underlying data changes.

---

### Option 3 — Shared config preloader with coalescing for startup race

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

_config_future: asyncio.Future[dict] | None = None
_config_lock = asyncio.Lock()


async def load_config_once() -> dict:
    """
    Load agent configuration exactly once, even if called concurrently.
    All callers that arrive while loading is in progress get the same result.
    """
    global _config_future

    async with _config_lock:
        if _config_future is None:
            loop = asyncio.get_running_loop()
            _config_future = loop.create_future()

            async def _do_load() -> None:
                try:
                    # Simulate: fetch config from an API or DB
                    response = await async_client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=128,
                        system="Return a JSON config object with keys: max_retries, timeout, model.",
                        messages=[{"role": "user", "content": "Generate default config."}],
                    )
                    import json, re
                    text = response.content[0].text
                    match = re.search(r"\{.*\}", text, re.DOTALL)
                    config = json.loads(match.group(0)) if match else {"max_retries": 3, "timeout": 30}
                    _config_future.set_result(config)  # type: ignore
                except Exception as exc:
                    _config_future.set_exception(exc)  # type: ignore

            asyncio.create_task(_do_load())

    # Wait for the single in-flight load (whether we started it or not)
    return await asyncio.shield(_config_future)  # type: ignore


async def run_agent(user_message: str) -> str:
    config = await load_config_once()   # all workers share this
    model = config.get("model", "claude-sonnet-4-6")

    response = await async_client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


async def main() -> None:
    # 20 workers all start simultaneously — config loads once
    workers = [run_agent(f"Request {i}") for i in range(20)]
    results = await asyncio.gather(*workers)
    print(f"Processed {len(results)} requests with 1 config load")


asyncio.run(main())
```

**Expected Token Savings:** 20 startup workers → 1 config load; particularly important for agents deployed on auto-scaling infrastructure where many instances start simultaneously.
**Environment:** Serverless or container-based agents with multiple worker coroutines starting in parallel on cold start.

---

### Option 4 — Per-user profile coalescer for multi-tenant agents

```python
import anthropic
import asyncio
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class UserProfile:
    user_id: str
    name: str
    preferences: dict


_profile_futures: dict[str, asyncio.Future[UserProfile]] = {}


async def get_user_profile(user_id: str) -> UserProfile:
    """Fetch user profile exactly once per user_id even under concurrent load."""
    if user_id in _profile_futures:
        return await asyncio.shield(_profile_futures[user_id])

    loop = asyncio.get_running_loop()
    future: asyncio.Future[UserProfile] = loop.create_future()
    _profile_futures[user_id] = future

    try:
        # Simulate: DB or profile API call
        await asyncio.sleep(0.05)  # I/O latency
        profile = UserProfile(
            user_id=user_id,
            name=f"User-{user_id}",
            preferences={"tone": "concise", "language": "en"},
        )
        future.set_result(profile)
        return profile

    except Exception as exc:
        future.set_exception(exc)
        raise

    finally:
        # Remove after a short window so future requests can re-fetch if needed
        async def _cleanup() -> None:
            await asyncio.sleep(5.0)
            _profile_futures.pop(user_id, None)
        asyncio.create_task(_cleanup())


async def run_agent_for_user(user_id: str, message: str) -> str:
    profile = await get_user_profile(user_id)

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"User: {profile.name}. Tone: {profile.preferences['tone']}.",
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text


async def main() -> None:
    # 5 simultaneous requests for the same user — profile fetched once
    tasks = [run_agent_for_user("user_42", f"Message {i}") for i in range(5)]
    await asyncio.gather(*tasks)
    print("5 messages processed with 1 profile fetch")


asyncio.run(main())
```

**Expected Token Savings:** Profile fetches are I/O operations (no tokens), but the pattern prevents duplicate context-enrichment LLM calls; adapts naturally to any shared lookup.
**Environment:** Multi-tenant agents where the same user may have multiple concurrent sessions or webhook events arriving simultaneously.

---

### Option 5 — Semaphore + shared result for rate-limited upstream

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# At most 1 concurrent call to the rate-limited upstream
_rate_limited_semaphore = asyncio.Semaphore(1)
_result_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 30.0


async def fetch_rate_limited(key: str, prompt: str) -> str:
    """
    Only one call at a time to the rate-limited source.
    Concurrent waiters get the cached result once available.
    """
    # Check cache first
    if key in _result_cache:
        value, ts = _result_cache[key]
        if time.time() - ts < _CACHE_TTL:
            return value

    async with _rate_limited_semaphore:
        # Double-check after acquiring semaphore
        if key in _result_cache:
            value, ts = _result_cache[key]
            if time.time() - ts < _CACHE_TTL:
                return value

        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text
        _result_cache[key] = (result, time.time())
        return result


async def run_agent(query: str) -> str:
    # All queries share the same classification result within the TTL window
    classification = await fetch_rate_limited(
        key=f"classify:{query[:32]}",
        prompt=f"Classify this query in one word (task/question/greeting): {query}",
    )
    return f"[{classification.strip()}] {query}"


async def main() -> None:
    queries = ["What time is it?"] * 10
    t0 = time.perf_counter()
    results = await asyncio.gather(*[run_agent(q) for q in queries])
    elapsed = time.perf_counter() - t0
    print(f"10 queries → {len(set(r.split(']')[0] for r in results))} unique classifications in {elapsed:.2f}s")


asyncio.run(main())
```

**Expected Token Savings:** 10 identical classification calls → 1 actual call; subsequent callers within TTL window get the cached result instantly.
**Environment:** Classification or routing agents that evaluate the same query property for many requests; the semaphore prevents thundering herd on rate-limited upstreams.

---

### Option 6 — Batch coalescer: accumulate requests and flush as a single batch

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class PendingRequest:
    prompt: str
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


class BatchCoalescer:
    """
    Collect individual requests for a short window, then send them as a batch.
    Each caller gets its own result back, but all prompts are sent together.
    """

    def __init__(self, window_ms: float = 20.0, max_batch: int = 10) -> None:
        self._window = window_ms / 1000
        self._max_batch = max_batch
        self._pending: list[PendingRequest] = []
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def submit(self, prompt: str) -> str:
        req = PendingRequest(prompt=prompt)
        async with self._lock:
            self._pending.append(req)
            if len(self._pending) >= self._max_batch:
                await self._flush_now()
            elif self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_after_window())
        return await req.future

    async def _flush_after_window(self) -> None:
        await asyncio.sleep(self._window)
        async with self._lock:
            await self._flush_now()

    async def _flush_now(self) -> None:
        if not self._pending:
            return
        batch = self._pending[: self._max_batch]
        self._pending = self._pending[self._max_batch:]
        # Fire all batch requests concurrently
        asyncio.create_task(self._process_batch(batch))

    async def _process_batch(self, batch: list[PendingRequest]) -> None:
        responses = await asyncio.gather(*[
            async_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": req.prompt}],
            )
            for req in batch
        ], return_exceptions=True)

        for req, resp in zip(batch, responses):
            if isinstance(resp, Exception):
                req.future.set_exception(resp)
            else:
                req.future.set_result(resp.content[0].text)


_coalescer = BatchCoalescer(window_ms=15.0, max_batch=8)


async def run_agent(prompt: str) -> str:
    return await _coalescer.submit(prompt)


async def main() -> None:
    prompts = [f"Classify: message {i}" for i in range(12)]
    results = await asyncio.gather(*[run_agent(p) for p in prompts])
    print(f"12 requests processed in batches of ≤8")


# Comparison table
# | Option | Coalescing Strategy | Works For | TTL/Window |
# |--------|--------------------|-----------|----|
# | 1 Future registry | Identical prompt hash | Burst of identical requests | None (one-shot) |
# | 2 LRU + coalescer | Key + TTL | Repeated lookups with TTL | TTL per entry |
# | 3 Startup race | Single future per resource | Cold start parallelism | Permanent |
# | 4 Per-user profile | User ID key | Multi-session users | Short window |
# | 5 Semaphore + cache | Rate-limited upstream | High-traffic classification | TTL |
# | 6 Batch accumulator | Time window batching | High-frequency small requests | Window ms |

asyncio.run(main())
```

**Expected Token Savings:** Requests within the batching window are sent together, sharing connection overhead and avoiding per-request cold-start latency; effective for classification pipelines with high arrival rates.
**Environment:** High-frequency async agents processing events (webhooks, stream processors) where many small requests arrive in bursts; the window should be tuned to the burst pattern.
