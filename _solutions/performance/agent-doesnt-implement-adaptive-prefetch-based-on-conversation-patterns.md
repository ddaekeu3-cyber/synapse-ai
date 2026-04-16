---
title: "Agent Doesn't Implement Adaptive Prefetch Based on Conversation Patterns"
description: "AI agents that wait for each user turn before loading context, retrieving documents, or warming caches add unnecessary latency to every response. Learn six adaptive prefetch patterns that predict what the agent will need next and load it proactively."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-adaptive-prefetch-based-on-conversation-patterns
tags: [prefetch, prediction, caching, latency, streaming, performance]
symptoms:
  - "Every response has 200-500ms of document retrieval latency before the LLM even starts"
  - "Vector search runs synchronously during each turn, blocking the response"
  - "Tool schemas are loaded fresh on every call even though they rarely change"
  - "Context documents are fetched one-by-one as the agent discovers it needs them"
  - "The agent re-fetches the same reference documents on every turn of a multi-turn conversation"
---

## The Problem

Most agents are purely reactive: they wait for a user message, then fetch required context, then call the LLM. This means every response includes the full latency of context retrieval — often 200-800ms of vector search, document loading, or API calls — before the model can even start generating.

Adaptive prefetch predicts what context the agent will need before it's explicitly requested, and starts loading it in parallel with (or ahead of) user input. The prediction is based on conversation history, query patterns, topic continuity, and explicit intent signals.

```python
# ❌ Reactive: full latency on every turn
async def respond(message: str):
    docs = await vector_search(message)  # 300ms wait
    context = await load_documents(docs)  # 200ms wait
    return await llm.generate(context, message)  # Total: 500ms+ before LLM starts

# ✓ Prefetch: context ready before user finishes typing
prefetcher.on_user_typing(partial_message)  # Start prefetch immediately
async def respond(message: str):
    docs = await prefetcher.get_prefetched()  # < 10ms, already loaded
    return await llm.generate(docs, message)
```

---

## Solution 1: Intent-Prediction Prefetch

As the user types (or immediately after each turn completes), predict their next likely intent and prefetch relevant documents for that predicted intent.

```python
import asyncio
import time
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable
from collections import defaultdict


@dataclass
class PrefetchCandidate:
    query: str
    confidence: float       # 0-1: how likely this query will be needed
    fetched: bool = False
    result: Any = None
    fetched_at: float | None = None
    fetch_latency_ms: float = 0.0


class IntentPredictionPrefetcher:
    """
    Predicts the user's next likely query from conversation context
    and prefetches results in the background.
    """

    def __init__(self, fetch_fn: Callable, max_prefetch: int = 3):
        """
        fetch_fn: async fn(query: str) -> Any
        """
        self._fetch_fn = fetch_fn
        self.max_prefetch = max_prefetch
        self._candidates: dict[str, PrefetchCandidate] = {}
        self._active_fetches: dict[str, asyncio.Task] = {}
        self._conversation_topics: list[str] = []
        self._hit_count = 0
        self._miss_count = 0

    def _predict_queries(self, last_turn: str, context: list[str]) -> list[PrefetchCandidate]:
        """
        Heuristic: predict follow-up queries based on last turn.
        In production, replace with a lightweight classifier or LLM call.
        """
        import re
        candidates = []

        # 1. Continuation: if user asked about X, they might ask for details about X
        nouns = re.findall(r'\b[A-Z][a-z]{3,}\b', last_turn)  # Capitalized words
        for noun in nouns[:2]:
            candidates.append(PrefetchCandidate(
                query=f"{noun} details",
                confidence=0.4,
            ))

        # 2. Topic continuation: if conversation has been about a topic, stay on it
        if self._conversation_topics:
            last_topic = self._conversation_topics[-1]
            candidates.append(PrefetchCandidate(
                query=f"{last_topic} follow up",
                confidence=0.5,
            ))

        # 3. Common follow-up patterns
        if "how" in last_turn.lower():
            candidates.append(PrefetchCandidate(
                query=last_turn.replace("how", "why"),
                confidence=0.3,
            ))

        return sorted(candidates, key=lambda c: -c.confidence)[:self.max_prefetch]

    async def _prefetch_one(self, candidate: PrefetchCandidate):
        key = hashlib.md5(candidate.query.encode()).hexdigest()[:8]
        start = time.monotonic()
        try:
            result = await self._fetch_fn(candidate.query)
            candidate.result = result
            candidate.fetched = True
            candidate.fetched_at = time.time()
            candidate.fetch_latency_ms = (time.monotonic() - start) * 1000
        except Exception as e:
            candidate.fetched = True
            candidate.result = None

    async def prefetch_after_turn(self, last_message: str, conversation: list[dict]):
        """Called after each turn completes to prefetch for the next turn."""
        context = [m.get("content", "") for m in conversation[-5:]]
        candidates = self._predict_queries(last_message, context)

        # Update topic tracking
        import re
        words = re.findall(r'\b\w{5,}\b', last_message.lower())
        if words:
            self._conversation_topics.append(words[0])
            self._conversation_topics = self._conversation_topics[-5:]

        # Start background fetches
        for candidate in candidates:
            key = hashlib.md5(candidate.query.encode()).hexdigest()[:8]
            if key not in self._active_fetches or self._active_fetches[key].done():
                self._candidates[key] = candidate
                self._active_fetches[key] = asyncio.create_task(
                    self._prefetch_one(candidate)
                )

    async def get_best_prefetch(self, actual_query: str, timeout_ms: float = 50.0) -> Any:
        """
        Find the best matching prefetched result for the actual query.
        Returns None if no match found within timeout.
        """
        # Simple: find candidate with highest query overlap
        actual_words = set(actual_query.lower().split())
        best_key = None
        best_overlap = 0.0

        for key, candidate in self._candidates.items():
            cand_words = set(candidate.query.lower().split())
            overlap = len(actual_words & cand_words) / max(len(actual_words | cand_words), 1)
            if overlap > best_overlap:
                best_overlap = overlap
                best_key = key

        if best_key and best_overlap > 0.3:
            candidate = self._candidates[best_key]
            task = self._active_fetches.get(best_key)

            if candidate.fetched:
                self._hit_count += 1
                return candidate.result

            if task and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=timeout_ms / 1000)
                    if candidate.fetched:
                        self._hit_count += 1
                        return candidate.result
                except asyncio.TimeoutError:
                    pass

        self._miss_count += 1
        return None

    def stats(self) -> dict:
        total = self._hit_count + self._miss_count
        return {
            "hit_count": self._hit_count,
            "miss_count": self._miss_count,
            "hit_rate": self._hit_count / max(total, 1),
            "active_prefetches": sum(1 for t in self._active_fetches.values() if not t.done()),
        }
```

---

## Solution 2: Conversation Pattern Memoizer

Learn recurring conversation patterns and pre-warm caches for the documents, tool outputs, and embeddings most likely to be needed given the current conversation state.

```python
import asyncio
import time
import json
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConversationPattern:
    pattern_id: str
    trigger_phrases: list[str]      # What triggers this pattern
    prefetch_queries: list[str]     # What to prefetch when triggered
    observed_count: int = 0
    last_triggered: float = 0.0
    avg_hit_rate: float = 0.0


class ConversationPatternMemoizer:
    """
    Learns recurring conversation patterns over time.
    When a pattern is recognized, immediately prefetches what previous
    conversations needed after that point.
    """

    def __init__(self, fetch_fn, min_pattern_frequency: int = 3):
        self._fetch_fn = fetch_fn
        self.min_freq = min_pattern_frequency
        self._patterns: dict[str, ConversationPattern] = {}
        self._sequence_log: list[dict] = []   # Log of (message, fetched_queries)
        self._cache: dict[str, tuple[Any, float]] = {}  # query → (result, expires_at)
        self.cache_ttl = 300.0

    def _fingerprint(self, message: str) -> str:
        """Create a normalized fingerprint from a message."""
        import re
        words = sorted(set(re.findall(r'\b\w{4,}\b', message.lower())))[:5]
        return " ".join(words)

    def record_observation(self, message: str, subsequent_queries: list[str]):
        """
        After a conversation turn, record what queries were actually made.
        This builds the pattern database over time.
        """
        fp = self._fingerprint(message)
        if fp not in self._patterns:
            self._patterns[fp] = ConversationPattern(
                pattern_id=fp,
                trigger_phrases=[message[:50]],
                prefetch_queries=[],
            )
        pattern = self._patterns[fp]
        pattern.observed_count += 1
        pattern.last_triggered = time.time()

        # Update prefetch queries with newly observed ones
        for q in subsequent_queries:
            if q not in pattern.prefetch_queries:
                pattern.prefetch_queries.append(q)

    async def prefetch_for_message(self, message: str) -> list[str]:
        """
        If message matches a known pattern, prefetch its associated queries.
        Returns list of queries that were prefetched.
        """
        fp = self._fingerprint(message)
        pattern = self._patterns.get(fp)

        if not pattern or pattern.observed_count < self.min_freq:
            return []

        prefetched = []
        tasks = []
        for query in pattern.prefetch_queries:
            cache_key = hashlib.md5(query.encode()).hexdigest()[:12]
            cached = self._cache.get(cache_key)
            if cached and time.time() < cached[1]:
                continue  # Already cached

            tasks.append((query, cache_key, asyncio.create_task(self._fetch(query, cache_key))))
            prefetched.append(query)

        return prefetched

    async def _fetch(self, query: str, cache_key: str):
        try:
            result = await self._fetch_fn(query)
            self._cache[cache_key] = (result, time.time() + self.cache_ttl)
        except Exception:
            pass

    def get_cached(self, query: str) -> Any:
        key = hashlib.md5(query.encode()).hexdigest()[:12]
        cached = self._cache.get(key)
        if cached and time.time() < cached[1]:
            return cached[0]
        return None

    def pattern_stats(self) -> list[dict]:
        return sorted([
            {
                "pattern_id": p.pattern_id[:30],
                "observed_count": p.observed_count,
                "prefetch_queries": len(p.prefetch_queries),
            }
            for p in self._patterns.values()
            if p.observed_count >= self.min_freq
        ], key=lambda x: -x["observed_count"])[:10]
```

---

## Solution 3: Streaming Context Loader with Parallel Prefetch

Instead of waiting for all context to load before starting generation, stream context documents in parallel with the LLM call, injecting them as they arrive.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import AsyncIterator, Any


@dataclass
class ContextDocument:
    doc_id: str
    content: str
    relevance_score: float
    loaded_at: float
    load_latency_ms: float


class StreamingContextLoader:
    """
    Loads context documents in parallel and yields them as they arrive.
    The caller can start constructing the prompt as soon as the first
    document is available rather than waiting for all.
    """

    def __init__(self, loaders: list, max_concurrent: int = 5):
        """
        loaders: list of async callables, each returning a ContextDocument
        """
        self._loaders = loaders
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def _load_one(self, loader_fn, doc_id: str) -> ContextDocument:
        async with self._semaphore:
            start = time.monotonic()
            try:
                content = await loader_fn()
                latency = (time.monotonic() - start) * 1000
                return ContextDocument(
                    doc_id=doc_id,
                    content=content,
                    relevance_score=1.0,
                    loaded_at=time.time(),
                    load_latency_ms=latency,
                )
            except Exception as e:
                return ContextDocument(
                    doc_id=doc_id,
                    content="",
                    relevance_score=0.0,
                    loaded_at=time.time(),
                    load_latency_ms=(time.monotonic() - start) * 1000,
                )

    async def load_streaming(
        self,
        doc_specs: list[tuple[str, Any]],  # [(doc_id, loader_fn), ...]
        timeout_per_doc: float = 2.0,
    ) -> AsyncIterator[ContextDocument]:
        """Yield documents as they complete, highest priority first."""
        tasks = {
            doc_id: asyncio.create_task(self._load_one(loader_fn, doc_id))
            for doc_id, loader_fn in doc_specs
        }

        pending = set(tasks.values())
        while pending:
            done, pending = await asyncio.wait(
                pending,
                timeout=timeout_per_doc,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                try:
                    doc = task.result()
                    if doc.content:
                        yield doc
                except Exception:
                    pass

        # Handle any remaining timed-out tasks
        for doc_id, task in tasks.items():
            if not task.done():
                task.cancel()

    async def load_with_budget(
        self,
        doc_specs: list[tuple[str, Any]],
        token_budget: int,
        tokens_per_char: float = 0.25,
    ) -> list[ContextDocument]:
        """Load documents until token budget is exhausted."""
        docs = []
        used_tokens = 0
        async for doc in self.load_streaming(doc_specs):
            doc_tokens = int(len(doc.content) * tokens_per_char)
            if used_tokens + doc_tokens > token_budget:
                break
            docs.append(doc)
            used_tokens += doc_tokens
        return docs
```

---

## Solution 4: Embedding Prefetch Cache

Pre-compute and cache embeddings for documents the agent is likely to retrieve, so vector similarity search can run against pre-embedded documents without waiting for embedding computation.

```python
import asyncio
import time
import hashlib
import numpy as np
from dataclasses import dataclass, field
from typing import Any
import anthropic


@dataclass
class CachedEmbedding:
    text_hash: str
    embedding: list[float]
    text_preview: str
    created_at: float
    access_count: int = 0


class EmbeddingPrefetchCache:
    """
    Pre-computes and caches embeddings for documents that are likely
    to be retrieved in the near future.
    Eliminates embedding latency from the hot path.
    """

    def __init__(
        self,
        embedding_fn,           # async fn(texts: list[str]) -> list[list[float]]
        max_cache_size: int = 5000,
        prefetch_threshold: float = 0.7,
    ):
        self._embed_fn = embedding_fn
        self.max_size = max_cache_size
        self.threshold = prefetch_threshold
        self._cache: dict[str, CachedEmbedding] = {}
        self._prefetch_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._worker_task: asyncio.Task | None = None
        self._stats = {"hits": 0, "misses": 0, "prefetched": 0}

    def _text_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    async def get_embedding(self, text: str) -> list[float]:
        """Get embedding — from cache if available, else compute and cache."""
        key = self._text_hash(text)
        cached = self._cache.get(key)
        if cached:
            cached.access_count += 1
            self._stats["hits"] += 1
            return cached.embedding

        self._stats["misses"] += 1
        embeddings = await self._embed_fn([text])
        embedding = embeddings[0]
        self._store(key, text, embedding)
        return embedding

    def _store(self, key: str, text: str, embedding: list[float]):
        if len(self._cache) >= self.max_size:
            # Evict least-accessed entry
            lru_key = min(self._cache, key=lambda k: self._cache[k].access_count)
            del self._cache[lru_key]
        self._cache[key] = CachedEmbedding(
            text_hash=key,
            embedding=embedding,
            text_preview=text[:50],
            created_at=time.time(),
        )

    async def prefetch(self, texts: list[str]):
        """Pre-compute embeddings for documents expected to be needed soon."""
        to_embed = [t for t in texts if self._text_hash(t) not in self._cache]
        if not to_embed:
            return

        # Batch embedding call
        batch_size = 20
        for i in range(0, len(to_embed), batch_size):
            batch = to_embed[i:i + batch_size]
            try:
                embeddings = await self._embed_fn(batch)
                for text, emb in zip(batch, embeddings):
                    key = self._text_hash(text)
                    self._store(key, text, emb)
                    self._stats["prefetched"] += 1
            except Exception as e:
                print(f"[embed_prefetch] Batch failed: {e}")

    def similarity_search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """
        Find most similar cached embeddings to the query.
        Runs entirely in-memory — no round-trip needed.
        """
        if not self._cache:
            return []

        qv = np.array(query_embedding)
        qv_norm = qv / (np.linalg.norm(qv) or 1.0)

        keys = list(self._cache.keys())
        matrix = np.array([self._cache[k].embedding for k in keys])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        normed = matrix / np.where(norms == 0, 1, norms)

        scores = normed @ qv_norm
        top_indices = np.argsort(scores)[::-1][:top_k]

        return [
            (self._cache[keys[i]].text_preview, float(scores[i]))
            for i in top_indices
            if float(scores[i]) >= self.threshold
        ]

    def stats(self) -> dict:
        total = self._stats["hits"] + self._stats["misses"]
        return {
            **self._stats,
            "cache_size": len(self._cache),
            "hit_rate": self._stats["hits"] / max(total, 1),
        }
```

---

## Solution 5: Topic-Continuity Prefetch

In multi-turn conversations, the topic usually stays consistent across turns. Pre-load documents related to the current conversation topic as soon as a topic is detected, so they're ready for subsequent turns.

```python
import asyncio
import time
import re
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Any


@dataclass
class TopicState:
    topic: str
    confidence: float
    first_seen: float
    documents_prefetched: list[str] = field(default_factory=list)
    turn_count: int = 0


class TopicContinuityPrefetcher:
    """
    Detects conversation topics and prefetches related documents
    proactively, so subsequent turns on the same topic don't wait.
    """

    TOPIC_KEYWORDS = {
        "pricing": ["cost", "price", "billing", "plan", "subscription", "fee"],
        "technical": ["error", "bug", "code", "api", "integration", "deploy"],
        "refund": ["refund", "return", "money back", "cancel", "dispute"],
        "onboarding": ["setup", "start", "install", "configure", "getting started"],
        "performance": ["slow", "latency", "speed", "fast", "optimize", "timeout"],
    }

    def __init__(self, document_fetcher):
        """document_fetcher: async fn(topic: str) -> list[dict]"""
        self._fetcher = document_fetcher
        self._current_topic: TopicState | None = None
        self._prefetch_cache: dict[str, tuple[list, float]] = {}  # topic → (docs, expires)
        self._prefetch_tasks: dict[str, asyncio.Task] = {}
        self._stats = {"topic_switches": 0, "prefetch_hits": 0, "prefetch_misses": 0}

    def _detect_topic(self, message: str) -> tuple[str | None, float]:
        """Detect the primary topic of a message. Returns (topic, confidence)."""
        message_lower = message.lower()
        topic_scores = defaultdict(float)

        for topic, keywords in self.TOPIC_KEYWORDS.items():
            for kw in keywords:
                if kw in message_lower:
                    topic_scores[topic] += 1.0 / len(keywords)

        if not topic_scores:
            return None, 0.0

        best_topic = max(topic_scores, key=topic_scores.__getitem__)
        return best_topic, topic_scores[best_topic]

    async def _prefetch_topic(self, topic: str):
        """Background: fetch and cache documents for a topic."""
        try:
            docs = await self._fetcher(topic)
            self._prefetch_cache[topic] = (docs, time.time() + 600)  # 10 min TTL
        except Exception as e:
            print(f"[topic_prefetch] Failed to prefetch '{topic}': {e}")

    def on_message(self, message: str):
        """
        Called when a new user message arrives.
        Detects topic and triggers background prefetch.
        """
        topic, confidence = self._detect_topic(message)
        if not topic or confidence < 0.3:
            return

        # Check if topic changed
        if self._current_topic and self._current_topic.topic != topic:
            self._stats["topic_switches"] += 1

        if self._current_topic is None or self._current_topic.topic != topic:
            self._current_topic = TopicState(
                topic=topic, confidence=confidence, first_seen=time.time()
            )
        else:
            self._current_topic.turn_count += 1
            self._current_topic.confidence = max(
                self._current_topic.confidence, confidence
            )

        # Start prefetch if not already done for this topic
        cached = self._prefetch_cache.get(topic)
        if cached and time.time() < cached[1]:
            return  # Already have fresh cache

        task = self._prefetch_tasks.get(topic)
        if not task or task.done():
            self._prefetch_tasks[topic] = asyncio.create_task(
                self._prefetch_topic(topic)
            )

    async def get_topic_docs(self, message: str, timeout_ms: float = 100.0) -> list[dict]:
        """Get prefetched docs for current topic, waiting up to timeout."""
        topic, _ = self._detect_topic(message)
        if not topic:
            self._stats["prefetch_misses"] += 1
            return []

        # Check cache
        cached = self._prefetch_cache.get(topic)
        if cached and time.time() < cached[1]:
            self._stats["prefetch_hits"] += 1
            return cached[0]

        # Wait for prefetch task
        task = self._prefetch_tasks.get(topic)
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout_ms / 1000)
                cached = self._prefetch_cache.get(topic)
                if cached:
                    self._stats["prefetch_hits"] += 1
                    return cached[0]
            except asyncio.TimeoutError:
                pass

        self._stats["prefetch_misses"] += 1
        return []

    def stats(self) -> dict:
        return {
            **self._stats,
            "current_topic": self._current_topic.topic if self._current_topic else None,
            "cached_topics": len(self._prefetch_cache),
        }
```

---

## Solution 6: Adaptive Prefetch Orchestrator

A unified orchestrator that combines intent prediction, pattern memoization, topic continuity, and embedding prefetch — measuring which prefetch strategies produce the most hits and adapting weights accordingly.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class PrefetchResult:
    source: str             # Which prefetcher provided the result
    data: Any
    latency_ms: float       # Time to retrieve (< 10ms = true prefetch hit)
    was_prefetched: bool    # True if data was ready before the request


class AdaptivePrefetchOrchestrator:
    """
    Coordinates all prefetch strategies and learns which ones are most effective.
    Routes prefetch requests to the highest-performing strategy based on
    recent hit rates.
    """

    def __init__(self, fetch_fn: Callable):
        self._fetch_fn = fetch_fn
        self._intent_prefetcher = IntentPredictionPrefetcher(fetch_fn)
        self._pattern_memoizer = ConversationPatternMemoizer(fetch_fn)
        self._topic_prefetcher = TopicContinuityPrefetcher(fetch_fn)
        self._strategy_weights = {
            "intent": 0.33,
            "pattern": 0.33,
            "topic": 0.33,
        }
        self._strategy_hits: dict[str, int] = {"intent": 0, "pattern": 0, "topic": 0}
        self._strategy_calls: dict[str, int] = {"intent": 0, "pattern": 0, "topic": 0}
        self._last_weight_update = time.time()

    def on_user_message(self, message: str, conversation: list[dict]):
        """
        Call this as soon as a user message arrives (even mid-typing).
        All prefetch strategies start their background work in parallel.
        """
        self._topic_prefetcher.on_message(message)
        # Schedule other prefetch operations
        asyncio.create_task(self._intent_prefetcher.prefetch_after_turn(
            message, conversation
        ))
        asyncio.create_task(self._pattern_memoizer.prefetch_for_message(message))

    async def get(self, query: str, timeout_ms: float = 80.0) -> PrefetchResult:
        """
        Get the best available prefetched result.
        Tries strategies in order of their historical hit rate.
        """
        # Sort strategies by weight (hit rate)
        strategies = sorted(
            self._strategy_weights, key=lambda s: -self._strategy_weights[s]
        )

        start = time.monotonic()
        for strategy in strategies:
            self._strategy_calls[strategy] += 1
            result = None

            if strategy == "pattern":
                result = self._pattern_memoizer.get_cached(query)
            elif strategy == "topic":
                result = await self._topic_prefetcher.get_topic_docs(
                    query, timeout_ms=timeout_ms * 0.4
                )
            elif strategy == "intent":
                result = await self._intent_prefetcher.get_best_prefetch(
                    query, timeout_ms=timeout_ms * 0.4
                )

            if result:
                latency = (time.monotonic() - start) * 1000
                self._strategy_hits[strategy] += 1
                self._maybe_update_weights()
                return PrefetchResult(
                    source=strategy,
                    data=result,
                    latency_ms=latency,
                    was_prefetched=latency < 20,
                )

        # All prefetch strategies missed — fetch directly
        start_fetch = time.monotonic()
        data = await self._fetch_fn(query)
        return PrefetchResult(
            source="direct_fetch",
            data=data,
            latency_ms=(time.monotonic() - start_fetch) * 1000,
            was_prefetched=False,
        )

    def _maybe_update_weights(self):
        """Adjust strategy weights based on recent hit rates (every 100 calls)."""
        total_calls = sum(self._strategy_calls.values())
        if total_calls < 100 or time.time() - self._last_weight_update < 60:
            return

        for strategy in self._strategy_weights:
            calls = self._strategy_calls[strategy]
            hits = self._strategy_hits[strategy]
            hit_rate = hits / max(calls, 1)
            # Smooth update: 70% old weight + 30% new hit rate
            self._strategy_weights[strategy] = (
                0.7 * self._strategy_weights[strategy] + 0.3 * hit_rate
            )

        # Normalize weights
        total = sum(self._strategy_weights.values())
        if total > 0:
            for s in self._strategy_weights:
                self._strategy_weights[s] /= total

        self._last_weight_update = time.time()
        print(f"[prefetch] Updated weights: {self._strategy_weights}")

    def overall_stats(self) -> dict:
        total_calls = sum(self._strategy_calls.values())
        total_hits = sum(self._strategy_hits.values())
        return {
            "overall_prefetch_hit_rate": total_hits / max(total_calls, 1),
            "strategy_weights": self._strategy_weights,
            "strategy_stats": {
                s: {
                    "calls": self._strategy_calls[s],
                    "hits": self._strategy_hits[s],
                    "hit_rate": self._strategy_hits[s] / max(self._strategy_calls[s], 1),
                }
                for s in self._strategy_weights
            },
        }
```

---

## Comparison

| Pattern | Latency Reduction | Prediction Method | Overhead | Best For |
|---|---|---|---|---|
| Intent prediction prefetch | 60-80% on hit | Heuristic / classifier | Low | Single-turn Q&A with predictable follow-ups |
| Pattern memoizer | 70-90% on hit | Historical replay | Low | Repeat conversation patterns (FAQ, support) |
| Streaming context loader | 40-60% (pipeline) | None (parallel load) | Very low | Multi-document context loading |
| Embedding prefetch cache | 80-95% on hit | Pre-computed | Medium | High-traffic agents with stable document sets |
| Topic continuity prefetch | 65-85% on hit | NLP topic detection | Low | Multi-turn conversations that stay on topic |
| Adaptive orchestrator | Highest overall | Adaptive weighting | Medium | Production agents wanting automatic optimization |

**Recommendations:**
- Start with **streaming context loader** (Solution 3) — it's zero-risk and reduces latency by parallelizing what would otherwise be sequential loads.
- Add **embedding prefetch cache** (Solution 4) for any agent doing vector search — pre-computed embeddings eliminate the most expensive per-query operation.
- Use **topic continuity prefetch** (Solution 5) for conversational agents where topics persist across turns.
- Deploy the **adaptive orchestrator** (Solution 6) in production — it automatically discovers which prefetch strategy works best for your specific query distribution.
- Measure prefetch hit rate as a primary KPI; a well-tuned prefetch system should achieve > 60% hit rate, reducing median retrieval latency from 300-500ms to < 20ms.
