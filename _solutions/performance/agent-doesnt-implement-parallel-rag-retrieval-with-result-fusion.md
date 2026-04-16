---
title: "Agent Doesn't Implement Parallel RAG Retrieval with Result Fusion"
description: "Agents that execute RAG retrieval sequentially — first vector search, then keyword search, then knowledge graph lookup — pay cumulative latency for each retrieval path. Implement parallel RAG retrieval that fires all retrieval strategies simultaneously, then applies Reciprocal Rank Fusion to merge and re-rank the combined result set, reducing total retrieval latency to the slowest single path while improving recall."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-parallel-rag-retrieval-with-result-fusion
tags: [rag, parallel-retrieval, reciprocal-rank-fusion, hybrid-search, retrieval-fusion, latency-reduction]
symptoms:
  - "RAG pipeline runs vector search, then BM25, then reranker sequentially — 2.4s total"
  - "Each retrieval strategy could run in parallel — serial execution is unnecessary"
  - "No mechanism to combine results from multiple retrievers into a unified ranked list"
  - "High-recall queries require multiple retrieval passes with growing latency"
  - "Retrieval latency grows linearly with the number of sources queried"
---

## Why This Happens

Sequential retrieval is the path of least resistance: call retriever A, wait, call retriever B, wait, combine. Each wait is unnecessary because retrievers are independent — they do not need each other's results to start. Parallel retrieval eliminates the intermediate waits, reducing total latency to max(retriever_latencies) instead of sum(retriever_latencies). Result fusion solves the combination problem: different retrievers produce different score scales and ranking signals, so raw scores cannot be directly compared. Reciprocal Rank Fusion (RRF) converts each result list into rank-based scores (1/(rank + k)) and sums them, producing a unified ranking that rewards documents appearing highly in multiple lists.

## Solution 1: Retrieval Result

```python
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class RetrievalResult:
    doc_id: str
    content: str
    score: float                    # raw score from the retriever
    rank: int                       # rank within this retriever's result list
    retriever_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.doc_id)
```

## Solution 2: Reciprocal Rank Fusion

```python
from collections import defaultdict
from typing import Dict, List


class ReciprocalRankFusion:
    """
    Merges multiple ranked result lists using Reciprocal Rank Fusion.
    RRF score for document d = sum over retrievers of 1 / (k + rank(d, retriever))
    where k=60 is the standard constant that dampens the impact of top ranks.
    """

    def __init__(self, k: int = 60):
        self._k = k

    def fuse(
        self,
        result_lists: Dict[str, List[RetrievalResult]],
        top_n: int = 20,
    ) -> List[Dict]:
        """
        result_lists: dict of retriever_name -> ranked list
        Returns fused list sorted by RRF score descending.
        """
        rrf_scores: Dict[str, float] = defaultdict(float)
        doc_contents: Dict[str, str] = {}
        doc_metadata: Dict[str, dict] = {}
        doc_retriever_ranks: Dict[str, Dict[str, int]] = defaultdict(dict)

        for retriever_name, results in result_lists.items():
            for result in results:
                rank = result.rank
                rrf_scores[result.doc_id] += 1.0 / (self._k + rank)
                doc_contents[result.doc_id] = result.content
                doc_metadata[result.doc_id] = result.metadata
                doc_retriever_ranks[result.doc_id][retriever_name] = rank

        sorted_docs = sorted(rrf_scores.items(), key=lambda x: -x[1])[:top_n]

        return [
            {
                "doc_id": doc_id,
                "content": doc_contents[doc_id],
                "rrf_score": round(score, 6),
                "retriever_ranks": doc_retriever_ranks[doc_id],
                "metadata": doc_metadata[doc_id],
                "appeared_in": len(doc_retriever_ranks[doc_id]),
            }
            for doc_id, score in sorted_docs
        ]
```

## Solution 3: Parallel Retrieval Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class ParallelRetrievalExecutor:
    """
    Fires multiple retrieval functions simultaneously and collects results.
    Each retriever is an async callable: (query, top_k) -> List[RetrievalResult].
    Failing retrievers are handled gracefully — their results are omitted.
    """

    def __init__(
        self,
        retrievers: Dict[str, Callable],
        timeout_seconds: float = 5.0,
        max_concurrent: int = 8,
    ):
        self._retrievers = retrievers
        self._timeout = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._latencies: Dict[str, List[float]] = {name: [] for name in retrievers}

    async def _call_retriever(
        self,
        name: str,
        fn: Callable,
        query: str,
        top_k: int,
    ) -> Optional[List[RetrievalResult]]:
        async with self._semaphore:
            start = time.time()
            try:
                results = await asyncio.wait_for(fn(query, top_k), timeout=self._timeout)
                latency = (time.time() - start) * 1000
                self._latencies[name].append(latency)
                # Assign ranks
                for i, r in enumerate(results):
                    r.rank = i + 1
                    r.retriever_name = name
                return results
            except (asyncio.TimeoutError, Exception):
                return None

    async def retrieve_all(
        self,
        query: str,
        top_k: int = 10,
    ) -> Dict[str, List[RetrievalResult]]:
        tasks = {
            name: asyncio.create_task(self._call_retriever(name, fn, query, top_k))
            for name, fn in self._retrievers.items()
        }

        results: Dict[str, List[RetrievalResult]] = {}
        for name, task in tasks.items():
            result = await task
            if result is not None:
                results[name] = result

        return results

    def latency_stats(self) -> Dict[str, dict]:
        stats = {}
        for name, latencies in self._latencies.items():
            if latencies:
                stats[name] = {
                    "mean_ms": round(sum(latencies) / len(latencies), 2),
                    "p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95)], 2),
                    "calls": len(latencies),
                }
        return stats
```

## Solution 4: Hybrid RAG Pipeline

```python
import time
from typing import Any, Dict, List, Optional


class HybridRAGPipeline:
    """
    Combines parallel retrieval with RRF fusion into a single
    callable pipeline. Returns fused, re-ranked results ready
    for context injection.
    """

    def __init__(
        self,
        executor: ParallelRetrievalExecutor,
        fusion: ReciprocalRankFusion,
        top_k_per_retriever: int = 10,
        top_n_fused: int = 20,
    ):
        self._executor = executor
        self._fusion = fusion
        self._top_k = top_k_per_retriever
        self._top_n = top_n_fused
        self._pipeline_latencies: List[float] = []

    async def retrieve(self, query: str) -> dict:
        start = time.time()
        raw_results = await self._executor.retrieve_all(query, self._top_k)
        fused = self._fusion.fuse(raw_results, self._top_n)
        latency_ms = round((time.time() - start) * 1000, 2)
        self._pipeline_latencies.append(latency_ms)

        return {
            "query": query,
            "fused_results": fused,
            "retriever_counts": {name: len(results) for name, results in raw_results.items()},
            "total_latency_ms": latency_ms,
            "retrievers_used": list(raw_results.keys()),
        }

    def pipeline_stats(self) -> dict:
        if not self._pipeline_latencies:
            return {}
        sorted_lat = sorted(self._pipeline_latencies)
        return {
            "calls": len(sorted_lat),
            "mean_ms": round(sum(sorted_lat) / len(sorted_lat), 2),
            "p95_ms": round(sorted_lat[int(len(sorted_lat) * 0.95)], 2),
        }
```

## Solution 5: Retrieval Coverage Analyzer

```python
from typing import Dict, List


class RetrievalCoverageAnalyzer:
    """
    Analyzes how much each retriever contributes to final fused results.
    Identifies retrievers with low contribution that may not justify their latency.
    """

    def analyze(self, fused_results: List[dict], top_n: int = 10) -> dict:
        top_results = fused_results[:top_n]
        retriever_contribution: Dict[str, int] = {}

        for result in top_results:
            for retriever_name in result.get("retriever_ranks", {}):
                retriever_contribution[retriever_name] = (
                    retriever_contribution.get(retriever_name, 0) + 1
                )

        multi_retriever = sum(1 for r in top_results if r.get("appeared_in", 0) > 1)

        return {
            "top_n": top_n,
            "retriever_contribution": dict(
                sorted(retriever_contribution.items(), key=lambda x: -x[1])
            ),
            "multi_retriever_hits": multi_retriever,
            "multi_retriever_pct": round(multi_retriever / max(top_n, 1) * 100, 1),
        }
```

## Solution 6: Parallel RAG Dashboard

```python
import time


class ParallelRAGDashboard:
    def __init__(
        self,
        pipeline: HybridRAGPipeline,
        executor: ParallelRetrievalExecutor,
    ):
        self._pipeline = pipeline
        self._executor = executor

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "pipeline_stats": self._pipeline.pipeline_stats(),
            "per_retriever_latency": self._executor.latency_stats(),
        }
```

## Comparison

| Approach | Parallel Execution | RRF Fusion | Latency Tracking | Coverage Analysis | Dashboard |
|---|---|---|---|---|---|
| ParallelRetrievalExecutor | Yes (asyncio) | No | Yes (per-retriever) | No | No |
| ReciprocalRankFusion | No | Yes (k=60) | No | No | No |
| HybridRAGPipeline | Via executor | Via RRF | Yes (pipeline) | No | No |
| RetrievalCoverageAnalyzer | No | No | No | Yes | No |
| ParallelRAGDashboard | No | No | No | No | Yes |

**Best for production**: Use `k=60` (the standard RRF constant) unless you have strong evidence that a different value improves your specific evaluation metric — the constant is empirically robust across domains. Set `top_k_per_retriever=10` and `top_n_fused=20` as starting points; RRF degrades gracefully when some retrievers return fewer results. Run `RetrievalCoverageAnalyzer` weekly: a retriever contributing to fewer than 10% of top-10 results is a latency cost without quality benefit and should be removed. Monitor `p95_ms` from `pipeline_stats` — if it regresses after adding a new retriever, the new retriever's P95 latency is the bottleneck, not the parallelism.
