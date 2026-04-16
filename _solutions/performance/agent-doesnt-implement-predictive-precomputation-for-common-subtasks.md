---
title: "Agent Doesn't Implement Predictive Precomputation for Common Subtasks"
description: "How to identify recurring subtasks in agent workflows and precompute their results speculatively — using pattern mining, Markov chains, and background computation pipelines — so common work is ready before it is requested."
date: 2025-01-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-predictive-precomputation-for-common-subtasks
tags:
  - performance
  - precomputation
  - speculative-execution
  - caching
  - prediction
  - workflow-optimization
  - latency-reduction
symptoms:
  - "Every agent session recomputes the same tool call sequences from scratch"
  - "Common multi-step workflows always take full wall-clock time despite identical inputs"
  - "No background work happens during idle periods between user turns"
  - "Agent always waits for embedding generation even though the same text was embedded yesterday"
  - "Tool call graphs show identical subtree patterns across thousands of sessions"
  - "High P99 latency on first tool call of a session because nothing is preloaded"
---

## Why This Happens

Agent workflows exhibit strong regularity: most users who ask "summarize this document" follow up with "translate to Spanish", most code-review requests end with "generate a PR description", and most customer support sessions begin with the same authentication and history lookup steps. These patterns are predictable from prior traffic, yet agents compute them fresh every time.

Predictive precomputation mines these patterns from historical tool call sequences, predicts what computations are likely needed next, and kicks them off speculatively in the background. If the prediction is correct, the result is ready instantly. If wrong, the speculative work is discarded with no correctness impact — only wasted compute, which is bounded by the prediction accuracy.

---

## Solution 1: Tool Call Sequence Pattern Miner

Extract frequent tool call sequences from historical sessions to identify predictable subtask chains.

```python
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ToolCallEvent:
    tool_name: str
    args_hash: str      # Hash of arguments for dedup
    session_id: str
    timestamp: float
    result_hash: str = ""  # Hash of result for cache key

class SequencePatternMiner:
    """
    Mines frequent N-gram sequences of tool calls from historical session data.
    Produces "if tool A was called, tool B is likely next" predictions.
    """

    def __init__(self, n: int = 3, min_frequency: int = 10):
        self.n = n
        self.min_frequency = min_frequency
        self._ngram_counts: Counter = Counter()
        self._total_sequences: int = 0

    def ingest_session(self, events: list[ToolCallEvent]) -> None:
        """Add a sequence of tool call events from one session."""
        tool_names = [e.tool_name for e in events]
        for i in range(len(tool_names)):
            for length in range(1, self.n + 1):
                if i + length <= len(tool_names):
                    ngram = tuple(tool_names[i:i + length])
                    self._ngram_counts[ngram] += 1
        self._total_sequences += 1

    def get_successors(self, prefix: tuple[str, ...], top_k: int = 3) -> list[tuple[str, float]]:
        """
        Given a prefix of tool calls, return likely next tools with probabilities.
        Returns [(tool_name, probability), ...] sorted by probability.
        """
        prefix_count = self._ngram_counts.get(prefix, 0)
        if prefix_count < self.min_frequency:
            return []

        candidates: dict[str, int] = {}
        for ngram, count in self._ngram_counts.items():
            if len(ngram) == len(prefix) + 1 and ngram[:len(prefix)] == prefix:
                candidates[ngram[-1]] = count

        if not candidates:
            return []

        total = sum(candidates.values())
        sorted_candidates = sorted(candidates.items(), key=lambda x: -x[1])[:top_k]
        return [(tool, count / total) for tool, count in sorted_candidates]

    def get_common_prefixes(self, min_probability: float = 0.7) -> list[tuple[tuple, str, float]]:
        """
        Return (prefix, predicted_next_tool, probability) for high-confidence predictions.
        These are candidates for precomputation triggers.
        """
        results = []
        seen_prefixes = set()
        for ngram in self._ngram_counts:
            if len(ngram) < 2:
                continue
            prefix = ngram[:-1]
            if prefix in seen_prefixes:
                continue
            seen_prefixes.add(prefix)
            successors = self.get_successors(prefix, top_k=1)
            if successors and successors[0][1] >= min_probability:
                results.append((prefix, successors[0][0], successors[0][1]))
        return sorted(results, key=lambda x: -x[2])
```

---

## Solution 2: Markov Chain Predictor

A first-order Markov model that predicts the next tool call given the current one, enabling efficient single-step precomputation.

```python
import json
import math
from collections import defaultdict

class MarkovToolPredictor:
    """
    First-order Markov chain for predicting the next tool call.
    Trained on historical tool call sequences; predicts with confidence scores.
    """

    def __init__(self, smoothing: float = 0.01):
        self._transitions: dict[str, Counter] = defaultdict(Counter)
        self._total: dict[str, int] = defaultdict(int)
        self._smoothing = smoothing
        self._vocabulary: set[str] = set()

    def train(self, sequences: list[list[str]]) -> None:
        """Train on lists of tool call name sequences."""
        for seq in sequences:
            for i in range(len(seq) - 1):
                current, next_tool = seq[i], seq[i + 1]
                self._transitions[current][next_tool] += 1
                self._total[current] += 1
                self._vocabulary.add(current)
                self._vocabulary.add(next_tool)

    def predict(self, current_tool: str, top_k: int = 3) -> list[tuple[str, float]]:
        """
        Predict likely next tools after `current_tool`.
        Returns [(tool_name, probability), ...] sorted by probability.
        """
        if current_tool not in self._transitions:
            return []

        transitions = self._transitions[current_tool]
        total = self._total[current_tool]
        vocab_size = len(self._vocabulary)

        # Laplace smoothing for unseen transitions
        probs = {
            tool: (count + self._smoothing) / (total + self._smoothing * vocab_size)
            for tool, count in transitions.items()
        }
        return sorted(probs.items(), key=lambda x: -x[1])[:top_k]

    def confidence(self, current_tool: str, next_tool: str) -> float:
        """Return probability of `next_tool` following `current_tool`."""
        predictions = dict(self.predict(current_tool, top_k=100))
        return predictions.get(next_tool, 0.0)

    def save(self, path: str) -> None:
        data = {k: dict(v) for k, v in self._transitions.items()}
        with open(path, "w") as f:
            json.dump({"transitions": data, "total": dict(self._total)}, f)

    def load(self, path: str) -> None:
        with open(path) as f:
            data = json.load(f)
        self._transitions = defaultdict(Counter, {k: Counter(v) for k, v in data["transitions"].items()})
        self._total = defaultdict(int, data["total"])
        for k, v in self._transitions.items():
            self._vocabulary.update(v.keys())
            self._vocabulary.add(k)
```

---

## Solution 3: Background Precomputation Engine

Run predicted tool calls in the background during idle time. If the prediction is confirmed, return the cached result instantly.

```python
import asyncio
import time
import logging
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

@dataclass
class PrecomputedResult:
    tool_name: str
    args_hash: str
    result: Any
    computed_at: float
    ttl: float
    hit_count: int = 0

class PrecomputationEngine:
    """
    Speculatively executes predicted tool calls in the background.
    Results are cached and served instantly if the prediction was correct.
    """

    def __init__(
        self,
        predictor: MarkovToolPredictor,
        tool_registry: dict[str, Callable],
        max_concurrent: int = 3,
        min_confidence: float = 0.6,
        result_ttl: float = 120.0,
    ):
        self._predictor = predictor
        self._registry = tool_registry
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._min_confidence = min_confidence
        self._result_ttl = result_ttl
        self._cache: dict[str, PrecomputedResult] = {}
        self._stats = {"precomputed": 0, "hits": 0, "misses": 0, "wasted": 0}

    def _cache_key(self, tool_name: str, args_hash: str) -> str:
        return f"{tool_name}:{args_hash}"

    async def after_tool_call(
        self,
        tool_name: str,
        predicted_args_builder: Callable[[str], Optional[dict]],
    ) -> None:
        """
        Called after a tool completes. Speculatively kick off predicted successors.
        predicted_args_builder(tool_name) -> args dict or None if can't predict args.
        """
        predictions = self._predictor.predict(tool_name, top_k=2)
        for next_tool, confidence in predictions:
            if confidence < self._min_confidence:
                break
            args = predicted_args_builder(next_tool)
            if args is None:
                continue
            asyncio.create_task(
                self._precompute(next_tool, args, confidence)
            )

    async def _precompute(self, tool_name: str, args: dict, confidence: float) -> None:
        """Execute a tool speculatively in the background."""
        args_hash = self._hash_args(args)
        key = self._cache_key(tool_name, args_hash)

        if key in self._cache:
            return  # Already cached

        async with self._semaphore:
            handler = self._registry.get(tool_name)
            if handler is None:
                return
            try:
                start = time.monotonic()
                result = await handler(**args)
                duration = time.monotonic() - start
                self._cache[key] = PrecomputedResult(
                    tool_name=tool_name,
                    args_hash=args_hash,
                    result=result,
                    computed_at=time.monotonic(),
                    ttl=self._result_ttl,
                )
                self._stats["precomputed"] += 1
                logger.debug("Precomputed %s in %.1fms (confidence=%.0f%%)",
                             tool_name, duration * 1000, confidence * 100)
            except Exception as exc:
                logger.debug("Precomputation failed for %s: %s", tool_name, exc)

    def get_precomputed(self, tool_name: str, args: dict) -> Optional[Any]:
        """Retrieve a precomputed result. Returns None on miss."""
        key = self._cache_key(tool_name, self._hash_args(args))
        result = self._cache.get(key)

        if result is None:
            self._stats["misses"] += 1
            return None

        if time.monotonic() - result.computed_at > result.ttl:
            del self._cache[key]
            self._stats["misses"] += 1
            return None

        result.hit_count += 1
        self._stats["hits"] += 1
        logger.debug("Precomputation HIT for %s", tool_name)
        return result.result

    @staticmethod
    def _hash_args(args: dict) -> str:
        import hashlib, json
        return hashlib.sha256(
            json.dumps(args, sort_keys=True).encode()
        ).hexdigest()[:16]

    def evict_stale(self) -> int:
        now = time.monotonic()
        stale = [k for k, v in self._cache.items() if now - v.computed_at > v.ttl]
        for k in stale:
            if self._cache[k].hit_count == 0:
                self._stats["wasted"] += 1
            del self._cache[k]
        return len(stale)

    def stats(self) -> dict:
        total = self._stats["hits"] + self._stats["misses"]
        return {
            **self._stats,
            "hit_rate": self._stats["hits"] / total if total else 0.0,
            "cache_size": len(self._cache),
        }
```

---

## Solution 4: Session-Level Prewarming Pipeline

On session start, execute a standard "warm-up" sequence of common first-turn tool calls before the user's first message arrives.

```python
from dataclasses import dataclass
from typing import Callable, Awaitable

@dataclass
class WarmupTask:
    name: str
    tool_fn: Callable[[], Awaitable[Any]]
    cache_key: str
    priority: int = 1  # Lower = higher priority

class SessionPrewarmer:
    """
    Pre-executes a set of common first-turn tool calls as soon as a session opens,
    before the user has typed their first message.
    Results are available immediately when the first turn starts.
    """

    def __init__(self, max_concurrent: int = 4, timeout: float = 5.0):
        self._warmup_tasks: list[WarmupTask] = []
        self._max_concurrent = max_concurrent
        self._timeout = timeout
        self._cache: dict[str, tuple[Any, float]] = {}

    def register(self, task: WarmupTask) -> None:
        self._warmup_tasks.append(task)
        self._warmup_tasks.sort(key=lambda t: t.priority)

    async def warm(self, session_id: str) -> dict[str, Any]:
        """
        Execute all registered warmup tasks concurrently.
        Returns a dict of {cache_key: result} for tasks that completed within timeout.
        """
        semaphore = asyncio.Semaphore(self._max_concurrent)
        results: dict[str, Any] = {}

        async def run_task(task: WarmupTask) -> None:
            async with semaphore:
                try:
                    result = await asyncio.wait_for(task.tool_fn(), timeout=self._timeout)
                    results[task.cache_key] = result
                    self._cache[f"{session_id}:{task.cache_key}"] = (result, time.monotonic())
                except asyncio.TimeoutError:
                    logger.debug("Warmup task %s timed out for session %s", task.name, session_id)
                except Exception as exc:
                    logger.debug("Warmup task %s failed: %s", task.name, exc)

        await asyncio.gather(*[run_task(t) for t in self._warmup_tasks])
        return results

    def get(self, session_id: str, cache_key: str, max_age: float = 30.0) -> Optional[Any]:
        key = f"{session_id}:{cache_key}"
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.monotonic() - ts > max_age:
            del self._cache[key]
            return None
        return value

    def clear_session(self, session_id: str) -> None:
        stale = [k for k in self._cache if k.startswith(f"{session_id}:")]
        for k in stale:
            del self._cache[k]
```

---

## Solution 5: Incremental Context Prebuilder

Precompute the context (embeddings, retrieved chunks, tool schemas) that will be needed for the next likely user message.

```python
import asyncio
from typing import Optional

class IncrementalContextPrebuilder:
    """
    After each agent turn, speculatively pre-builds context for the likely next turn.
    Uses the conversation trajectory to predict what context will be needed.
    """

    def __init__(
        self,
        embed_fn: Callable[[str], Awaitable[list[float]]],
        retrieval_fn: Callable[[list[float]], Awaitable[list[dict]]],
        predictor: MarkovToolPredictor,
    ):
        self._embed = embed_fn
        self._retrieve = retrieval_fn
        self._predictor = predictor
        self._prebuilt: dict[str, Any] = {}

    async def prebuild_after_turn(
        self,
        session_id: str,
        last_tool: str,
        conversation_tail: str,
    ) -> None:
        """
        Kick off background context pre-building after a completed turn.
        """
        predictions = self._predictor.predict(last_tool, top_k=1)
        if not predictions or predictions[0][1] < 0.65:
            return

        next_tool, confidence = predictions[0]
        asyncio.create_task(
            self._build_context(session_id, next_tool, conversation_tail)
        )

    async def _build_context(
        self, session_id: str, next_tool: str, context_text: str
    ) -> None:
        try:
            embedding = await self._embed(context_text)
            chunks = await self._retrieve(embedding)
            cache_key = f"{session_id}:{next_tool}"
            self._prebuilt[cache_key] = {
                "embedding": embedding,
                "retrieved_chunks": chunks,
                "built_at": time.monotonic(),
                "for_tool": next_tool,
            }
            logger.debug("Pre-built context for %s in session %s", next_tool, session_id)
        except Exception as exc:
            logger.debug("Context pre-build failed: %s", exc)

    def get_prebuilt(self, session_id: str, tool_name: str, max_age: float = 60.0) -> Optional[dict]:
        key = f"{session_id}:{tool_name}"
        ctx = self._prebuilt.get(key)
        if ctx is None:
            return None
        if time.monotonic() - ctx["built_at"] > max_age:
            del self._prebuilt[key]
            return None
        return ctx
```

---

## Solution 6: Prediction Accuracy Tracker

Monitor prediction accuracy and automatically tune the minimum confidence threshold.

```python
from dataclasses import dataclass
import statistics

@dataclass
class PredictionRecord:
    predicted_tool: str
    actual_tool: str
    confidence: float
    was_correct: bool
    timestamp: float

class PredictionAccuracyTracker:
    """
    Tracks prediction accuracy to tune the precomputation engine's confidence threshold.
    Automatically adjusts min_confidence to balance hit rate vs. wasted compute.
    """

    def __init__(
        self,
        engine: PrecomputationEngine,
        target_hit_rate: float = 0.7,
        adjustment_interval: int = 100,  # adjust every N predictions
    ):
        self._engine = engine
        self._target_hit_rate = target_hit_rate
        self._interval = adjustment_interval
        self._records: list[PredictionRecord] = []
        self._current_threshold = engine._min_confidence

    def record(self, predicted_tool: str, actual_tool: str, confidence: float) -> None:
        was_correct = predicted_tool == actual_tool
        self._records.append(PredictionRecord(
            predicted_tool=predicted_tool,
            actual_tool=actual_tool,
            confidence=confidence,
            was_correct=was_correct,
            timestamp=time.time(),
        ))

        if len(self._records) % self._interval == 0:
            self._adjust_threshold()

    def _adjust_threshold(self) -> None:
        recent = self._records[-self._interval:]
        correct = sum(1 for r in recent if r.was_correct)
        accuracy = correct / len(recent) if recent else 0.0

        if accuracy < self._target_hit_rate - 0.05:
            # Too many wrong predictions — raise threshold
            new_threshold = min(0.95, self._current_threshold + 0.05)
        elif accuracy > self._target_hit_rate + 0.05:
            # Very accurate — lower threshold to precompute more
            new_threshold = max(0.3, self._current_threshold - 0.02)
        else:
            return

        if new_threshold != self._current_threshold:
            logger.info(
                "Auto-tuning precomputation threshold: %.2f -> %.2f (accuracy=%.0f%%)",
                self._current_threshold, new_threshold, accuracy * 100,
            )
            self._current_threshold = new_threshold
            self._engine._min_confidence = new_threshold

    def accuracy_report(self) -> dict:
        if not self._records:
            return {}
        correct = sum(1 for r in self._records if r.was_correct)
        confidences = [r.confidence for r in self._records]
        return {
            "total_predictions": len(self._records),
            "accuracy": correct / len(self._records),
            "current_threshold": self._current_threshold,
            "avg_confidence": statistics.mean(confidences),
            "engine_hit_rate": self._engine.stats().get("hit_rate", 0.0),
        }
```

---

## Comparison

| Solution | Prediction Basis | Latency Saved | Compute Cost | Best For |
|---|---|---|---|---|
| Sequence Pattern Miner | N-gram frequency | Medium | Low (offline) | Identifying precomputation opportunities |
| Markov Chain Predictor | 1st-order transitions | Medium | Low (online) | Single-step next-tool prediction |
| Precomputation Engine | Any predictor | High | Medium (background) | Caching predicted tool results |
| Session Prewarmer | Fixed warmup list | High (first turn) | Low | Cold start latency |
| Context Prebuilder | Markov prediction | High (retrieval) | Medium | RAG context preloading |
| Accuracy Tracker | Empirical feedback | N/A | None | Auto-tuning confidence thresholds |

**Start with the Markov chain predictor** trained on your production tool call logs — even 1,000 sessions provides enough data for meaningful predictions. **Deploy the precomputation engine** with a conservative confidence threshold (0.7+) to avoid wasting compute. **Use the session prewarmer** for guaranteed first-turn latency improvements on well-known session patterns. **Add the accuracy tracker** to auto-tune the threshold over time rather than manually calibrating it.
