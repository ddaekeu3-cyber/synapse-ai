---
title: "Agent Doesn't Implement Parallel Retrieval with Result Merging"
description: "Agents that execute retrieval steps sequentially — first vector search, then keyword search, then structured database lookup — add the latency of each step together, even when all retrievals are independent and could run concurrently. Implement parallel retrieval that dispatches all independent retrieval operations simultaneously, waits for results with a bounded timeout, and merges the results into a ranked unified result set before context injection."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-parallel-retrieval-with-result-merging
tags: [parallel-retrieval, rag-performance, result-merging, hybrid-search, concurrent-retrieval, retrieval-latency]
symptoms:
  - "Vector search, keyword search, and database lookup run sequentially adding their latencies"
  - "Total retrieval time is the sum of individual step latencies instead of the maximum"
  - "Independent retrieval sources are never executed concurrently"
  - "No result merging — results from different sources are concatenated in arrival order"
  - "Slow retrieval sources hold up fast ones when running sequentially"
---

## Why This Happens

RAG pipelines often start as a single retrieval step and grow organically: a vector search is added, then a keyword fallback, then a structured database lookup, then a web search. Each step is added sequentially because it is easy and correct. The cost is that total retrieval latency becomes additive: 200ms + 150ms + 300ms = 650ms instead of the 300ms maximum if all three ran in parallel. Parallel retrieval requires explicitly modeling retrieval steps as concurrent tasks, collecting results as they complete, and merging them into a coherent ranked list before the slow path of context building begins.

## Solution 1: Retrieval Source

```python
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class RetrievalSourceType(str, Enum):
    VECTOR = "vector"
    KEYWORD = "keyword"
    DATABASE = "database"
    WEB = "web"
    CACHE = "cache"
    GRAPH = "graph"


@dataclass
class RetrievalSource:
    source_id: str
    source_type: RetrievalSourceType
    retrieve_fn: Callable         # async fn(query, **kwargs) -> List[RetrievalResult]
    weight: float = 1.0           # weight for score merging
    timeout_seconds: float = 5.0
    max_results: int = 10
    required: bool = False        # if True, failure raises; if False, skip silently


@dataclass
class RetrievalResult:
    content: str
    source_id: str
    score: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    item_id: str = ""
```

## Solution 2: Parallel Retrieval Dispatcher

```python
import asyncio
import time
from typing import Dict, List, Optional, Tuple


class ParallelRetrievalDispatcher:
    """
    Dispatches all retrieval sources concurrently and collects
    results as each completes. Sources that exceed their timeout
    are skipped rather than blocking the entire pipeline.
    """

    async def _fetch_one(
        self,
        source: RetrievalSource,
        query: str,
        kwargs: dict,
    ) -> Tuple[str, List[RetrievalResult], Optional[Exception], float]:
        start = time.time()
        try:
            results = await asyncio.wait_for(
                source.retrieve_fn(query, **kwargs),
                timeout=source.timeout_seconds,
            )
            latency_ms = round((time.time() - start) * 1000, 2)
            return source.source_id, results, None, latency_ms
        except Exception as exc:
            latency_ms = round((time.time() - start) * 1000, 2)
            return source.source_id, [], exc, latency_ms

    async def dispatch(
        self,
        sources: List[RetrievalSource],
        query: str,
        **kwargs,
    ) -> dict:
        tasks = [
            asyncio.create_task(self._fetch_one(src, query, kwargs))
            for src in sources
        ]

        all_results: Dict[str, List[RetrievalResult]] = {}
        errors: Dict[str, str] = {}
        latencies: Dict[str, float] = {}

        for coro in asyncio.as_completed(tasks):
            source_id, results, exc, latency_ms = await coro
            latencies[source_id] = latency_ms
            if exc is not None:
                errors[source_id] = str(exc)
                source = next((s for s in sources if s.source_id == source_id), None)
                if source and source.required:
                    raise RetrievalSourceError(source_id, str(exc))
            else:
                all_results[source_id] = results

        return {
            "results_by_source": all_results,
            "errors": errors,
            "latencies_ms": latencies,
            "wall_time_ms": round(max(latencies.values(), default=0), 2),
            "sequential_time_ms": round(sum(latencies.values()), 2),
        }


class RetrievalSourceError(Exception):
    def __init__(self, source_id: str, reason: str):
        super().__init__(f"required retrieval source '{source_id}' failed: {reason}")
        self.source_id = source_id
        self.reason = reason
```

## Solution 3: Result Merger

```python
from typing import Dict, List


class RetrievalResultMerger:
    """
    Merges results from multiple sources into a single ranked list.
    Supports reciprocal rank fusion and weighted score merging.
    Deduplicates by content hash before ranking.
    """

    def __init__(self, rrf_k: int = 60):
        self._k = rrf_k   # reciprocal rank fusion constant

    def merge_rrf(
        self,
        results_by_source: Dict[str, List[RetrievalResult]],
        source_weights: Dict[str, float] = None,
        max_results: int = 20,
    ) -> List[RetrievalResult]:
        """Reciprocal Rank Fusion across sources."""
        import hashlib

        weights = source_weights or {}
        rrf_scores: Dict[str, float] = {}
        item_map: Dict[str, RetrievalResult] = {}

        for source_id, results in results_by_source.items():
            weight = weights.get(source_id, 1.0)
            for rank, result in enumerate(results):
                key = hashlib.sha256(result.content.encode()).hexdigest()[:16]
                rrf_contribution = weight / (self._k + rank + 1)
                rrf_scores[key] = rrf_scores.get(key, 0.0) + rrf_contribution
                if key not in item_map:
                    item_map[key] = result

        ranked = sorted(rrf_scores.items(), key=lambda kv: kv[1], reverse=True)
        merged = []
        for key, score in ranked[:max_results]:
            result = item_map[key]
            merged.append(RetrievalResult(
                content=result.content,
                source_id=result.source_id,
                score=round(score, 6),
                metadata={**result.metadata, "rrf_score": score},
                item_id=result.item_id,
            ))
        return merged

    def merge_weighted(
        self,
        results_by_source: Dict[str, List[RetrievalResult]],
        source_weights: Dict[str, float] = None,
        max_results: int = 20,
    ) -> List[RetrievalResult]:
        """Weighted score merge — normalizes scores per source then combines."""
        import hashlib

        weights = source_weights or {}
        score_map: Dict[str, float] = {}
        item_map: Dict[str, RetrievalResult] = {}

        for source_id, results in results_by_source.items():
            if not results:
                continue
            weight = weights.get(source_id, 1.0)
            max_score = max(r.score for r in results) or 1.0
            for result in results:
                key = hashlib.sha256(result.content.encode()).hexdigest()[:16]
                normalized = result.score / max_score
                score_map[key] = score_map.get(key, 0.0) + normalized * weight
                if key not in item_map:
                    item_map[key] = result

        ranked = sorted(score_map.items(), key=lambda kv: kv[1], reverse=True)
        return [item_map[key] for key, _ in ranked[:max_results]]
```

## Solution 4: Parallel Retrieval Pipeline

```python
import time
from typing import Dict, List, Optional


class ParallelRetrievalPipeline:
    """
    Combines dispatcher and merger into a single pipeline call.
    Returns merged results and retrieval telemetry.
    """

    def __init__(
        self,
        dispatcher: ParallelRetrievalDispatcher,
        merger: RetrievalResultMerger,
        sources: List[RetrievalSource],
    ):
        self._dispatcher = dispatcher
        self._merger = merger
        self._sources = sources
        self._queries_total = 0
        self._total_latency_ms = 0.0

    async def retrieve(
        self,
        query: str,
        max_results: int = 20,
        merge_strategy: str = "rrf",
        **kwargs,
    ) -> dict:
        self._queries_total += 1
        start = time.time()

        dispatch_result = await self._dispatcher.dispatch(
            self._sources, query, **kwargs
        )

        source_weights = {s.source_id: s.weight for s in self._sources}

        if merge_strategy == "weighted":
            merged = self._merger.merge_weighted(
                dispatch_result["results_by_source"],
                source_weights,
                max_results,
            )
        else:
            merged = self._merger.merge_rrf(
                dispatch_result["results_by_source"],
                source_weights,
                max_results,
            )

        total_ms = round((time.time() - start) * 1000, 2)
        self._total_latency_ms += total_ms
        sequential_ms = dispatch_result["sequential_time_ms"]

        return {
            "results": merged,
            "result_count": len(merged),
            "total_latency_ms": total_ms,
            "sequential_latency_ms": sequential_ms,
            "parallelism_gain_ms": round(sequential_ms - total_ms, 2),
            "sources_queried": len(dispatch_result["results_by_source"]),
            "sources_errored": len(dispatch_result["errors"]),
            "errors": dispatch_result["errors"],
        }

    def stats(self) -> dict:
        return {
            "queries_total": self._queries_total,
            "avg_latency_ms": round(
                self._total_latency_ms / max(self._queries_total, 1), 2
            ),
        }
```

## Solution 5: Retrieval Latency Profiler

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List


class RetrievalLatencyProfiler:
    """
    Accumulates per-source latency observations to identify
    which retrieval sources are slowest and most error-prone.
    """

    def __init__(self):
        self._latencies: Dict[str, List[float]] = defaultdict(list)
        self._errors: Dict[str, int] = defaultdict(int)
        self._lock = Lock()

    def record(self, source_id: str, latency_ms: float, is_error: bool = False) -> None:
        with self._lock:
            self._latencies[source_id].append(latency_ms)
            if is_error:
                self._errors[source_id] += 1
            if len(self._latencies[source_id]) > 1000:
                self._latencies[source_id].pop(0)

    def summary(self) -> dict:
        with self._lock:
            result = {}
            for source_id, lats in self._latencies.items():
                sorted_lats = sorted(lats)
                n = len(sorted_lats)
                result[source_id] = {
                    "p50_ms": sorted_lats[n // 2] if n else None,
                    "p95_ms": sorted_lats[min(int(n * 0.95), n - 1)] if n else None,
                    "p99_ms": sorted_lats[min(int(n * 0.99), n - 1)] if n else None,
                    "error_count": self._errors[source_id],
                    "sample_count": n,
                }
        return result
```

## Solution 6: Parallel Retrieval Dashboard

```python
import time


class ParallelRetrievalDashboard:
    """
    Combines pipeline stats and per-source latency profiles
    into a single observability view.
    """

    def __init__(
        self,
        pipeline: ParallelRetrievalPipeline,
        profiler: RetrievalLatencyProfiler,
    ):
        self._pipeline = pipeline
        self._profiler = profiler

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "pipeline_stats": self._pipeline.stats(),
            "source_latencies": self._profiler.summary(),
        }
```

## Comparison

| Approach | Concurrent Dispatch | Timeout Per Source | RRF Merging | Weighted Merging | Latency Profiling |
|---|---|---|---|---|---|
| ParallelRetrievalDispatcher | Yes (as_completed) | Yes (per-source) | No | No | No |
| RetrievalResultMerger | No | No | Yes (k=60) | Yes (normalized) | No |
| ParallelRetrievalPipeline | Via dispatcher | Via dispatcher | Via merger | Via merger | No |
| RetrievalLatencyProfiler | No | No | No | No | Yes (P50/P95/P99) |
| ParallelRetrievalDashboard | No | No | No | No | Yes (combined) |

**Best for production**: Use Reciprocal Rank Fusion (RRF) as the default merge strategy — it is robust to different score scales across retrieval systems (cosine similarity from vector search vs BM25 from keyword search) and does not require score normalization. Set `required=False` for supplementary sources (web search, knowledge graph) so a slow external service does not block the primary vector results. Track `parallelism_gain_ms` per query: consistently near zero means sources have sequential dependencies that need to be identified, while gains of 200ms+ confirm that parallelization is delivering meaningful latency savings.
