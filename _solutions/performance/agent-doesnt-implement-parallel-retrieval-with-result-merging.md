---
title: "Agent Doesn't Implement Parallel Retrieval with Result Merging"
description: "Agents that retrieve context from multiple sources sequentially — first the knowledge base, then the web, then the database — spend the sum of all retrieval latencies waiting before any result is available. Implement parallel retrieval that fires all source queries simultaneously, collects results as they arrive, merges and deduplicates across sources, and applies a cross-source relevance ranking to produce the best combined result set within a fixed latency budget."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-parallel-retrieval-with-result-merging
tags: [parallel-retrieval, result-merging, multi-source-rag, latency-reduction, cross-source-ranking, retrieval-fusion]
symptoms:
  - "Retrieval from three sources takes 3 × source latency sequentially instead of max(source latencies)"
  - "A slow retrieval source blocks faster sources from contributing their results"
  - "No deduplication across sources — the same document retrieved from two sources appears twice in context"
  - "Results from different sources are concatenated in source order, not relevance order"
  - "No latency budget — retrieval waits indefinitely for all sources to respond"
---

## Why This Happens

Multi-source retrieval is added incrementally: a knowledge base is integrated first, then web search, then a structured database. Each integration adds a sequential step. The cumulative latency grows linearly with the number of sources even though the queries are independent and could run in parallel. Parallel retrieval requires a fan-out mechanism, a latency budget that caps how long any single source can delay the result, and a merge step that combines results from all sources that responded within the budget.

## Solution 1: Retrieval Source Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List


class SourceKind(str, Enum):
    VECTOR_STORE = "vector_store"
    WEB_SEARCH = "web_search"
    STRUCTURED_DB = "structured_db"
    DOCUMENT_STORE = "document_store"
    KNOWLEDGE_GRAPH = "knowledge_graph"


@dataclass
class RetrievalSource:
    source_id: str
    kind: SourceKind
    retrieve_fn: Callable            # async fn(query, top_k) -> List[RetrievedItem]
    timeout_seconds: float = 5.0
    weight: float = 1.0              # relative importance for score normalization
    max_results: int = 10
    tags: List[str] = field(default_factory=list)
```

## Solution 2: Retrieved Item

```python
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class RetrievedItem:
    content: str
    source_id: str
    item_id: str = ""
    score: float = 1.0               # raw relevance score from source
    normalized_score: float = 0.0    # cross-source normalized score
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_kind: Optional[SourceKind] = None

    def __post_init__(self):
        if not self.item_id:
            import hashlib
            self.item_id = hashlib.sha256(self.content.encode()).hexdigest()[:16]
```

## Solution 3: Parallel Retrieval Engine

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class SourceRetrievalResult:
    source_id: str
    items: List[RetrievedItem]
    latency_ms: float
    timed_out: bool = False
    error: Optional[str] = None
    succeeded: bool = True


class ParallelRetrievalEngine:
    """
    Fans out retrieval queries to all registered sources simultaneously.
    Sources that exceed the latency budget are abandoned; their partial
    results are not included in the merged result.
    """

    def __init__(self, latency_budget_seconds: float = 5.0):
        self._budget = latency_budget_seconds

    async def retrieve(
        self,
        sources: List[RetrievalSource],
        query: str,
        top_k: int = 10,
    ) -> List[SourceRetrievalResult]:
        tasks = {
            source.source_id: asyncio.create_task(
                self._retrieve_from_source(source, query, top_k)
            )
            for source in sources
        }

        # Wait up to budget, then cancel remaining
        deadline = time.monotonic() + self._budget
        results = []

        done, pending = await asyncio.wait(
            tasks.values(),
            timeout=self._budget,
        )

        for task in pending:
            task.cancel()

        for source in sources:
            task = tasks[source.source_id]
            if task in done:
                try:
                    result = task.result()
                    results.append(result)
                except Exception as exc:
                    results.append(SourceRetrievalResult(
                        source_id=source.source_id,
                        items=[],
                        latency_ms=0.0,
                        error=str(exc),
                        succeeded=False,
                    ))
            else:
                results.append(SourceRetrievalResult(
                    source_id=source.source_id,
                    items=[],
                    latency_ms=self._budget * 1000,
                    timed_out=True,
                    succeeded=False,
                ))

        return results

    async def _retrieve_from_source(
        self, source: RetrievalSource, query: str, top_k: int
    ) -> SourceRetrievalResult:
        start = time.monotonic()
        try:
            items = await asyncio.wait_for(
                source.retrieve_fn(query, top_k),
                timeout=min(source.timeout_seconds, self._budget),
            )
            for item in items:
                item.source_id = source.source_id
                item.source_kind = source.kind
            return SourceRetrievalResult(
                source_id=source.source_id,
                items=items,
                latency_ms=round((time.monotonic() - start) * 1000, 2),
            )
        except asyncio.TimeoutError:
            return SourceRetrievalResult(
                source_id=source.source_id,
                items=[],
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                timed_out=True,
                succeeded=False,
            )
```

## Solution 4: Cross-Source Result Merger

```python
import re
from typing import List, Set


class CrossSourceResultMerger:
    """
    Deduplicates and merges retrieved items from multiple sources.
    Normalizes scores across sources using the source weight, then
    ranks by normalized score descending.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.85,
        max_merged_results: int = 20,
    ):
        self._similarity_threshold = similarity_threshold
        self._max_results = max_merged_results

    def merge(
        self,
        source_results: List[SourceRetrievalResult],
        sources: List[RetrievalSource],
    ) -> List[RetrievedItem]:
        source_weights = {s.source_id: s.weight for s in sources}

        all_items: List[RetrievedItem] = []
        for result in source_results:
            if not result.succeeded or not result.items:
                continue
            items = result.items
            max_score = max((i.score for i in items), default=1.0)
            weight = source_weights.get(result.source_id, 1.0)
            for item in items:
                norm = (item.score / max(max_score, 1e-9)) * weight
                item.normalized_score = round(norm, 6)
            all_items.extend(items)

        deduplicated = self._deduplicate(all_items)
        ranked = sorted(deduplicated, key=lambda i: i.normalized_score, reverse=True)
        return ranked[:self._max_results]

    def _deduplicate(self, items: List[RetrievedItem]) -> List[RetrievedItem]:
        kept: List[RetrievedItem] = []
        for item in sorted(items, key=lambda i: i.normalized_score, reverse=True):
            is_dup = any(
                self._jaccard(item.content, k.content) >= self._similarity_threshold
                for k in kept
            )
            if not is_dup:
                kept.append(item)
        return kept

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        def shingles(text: str) -> set:
            t = re.sub(r"\s+", " ", text.lower())[:500]
            return {t[i:i+5] for i in range(max(1, len(t) - 4))}
        sa, sb = shingles(a), shingles(b)
        return len(sa & sb) / max(len(sa | sb), 1)
```

## Solution 5: Retrieval Latency Budget Monitor

```python
import time
from typing import List


class RetrievalLatencyBudgetMonitor:
    """
    Tracks retrieval events to measure how often sources exceed the budget
    and what fraction of the budget is used on average.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        source_results: List[SourceRetrievalResult],
        budget_seconds: float,
        merged_count: int,
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        timed_out = [r for r in source_results if r.timed_out]
        succeeded = [r for r in source_results if r.succeeded]
        max_latency_ms = max((r.latency_ms for r in source_results), default=0)
        self._records.append({
            "ts": time.time(),
            "sources": len(source_results),
            "succeeded": len(succeeded),
            "timed_out": len(timed_out),
            "max_latency_ms": max_latency_ms,
            "budget_ms": budget_seconds * 1000,
            "budget_used_pct": round(max_latency_ms / (budget_seconds * 1000) * 100, 1),
            "merged_count": merged_count,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "retrievals": 0}
        timeout_events = sum(r["timed_out"] for r in recent)
        total_source_calls = sum(r["sources"] for r in recent)
        return {
            "window_seconds": window_seconds,
            "retrievals": len(recent),
            "source_timeout_rate": round(timeout_events / max(total_source_calls, 1), 4),
            "avg_budget_used_pct": round(
                sum(r["budget_used_pct"] for r in recent) / len(recent), 1
            ),
            "avg_merged_results": round(
                sum(r["merged_count"] for r in recent) / len(recent), 1
            ),
        }
```

## Solution 6: Parallel Retrieval Dashboard

```python
import time


class ParallelRetrievalDashboard:
    def __init__(self, monitor: RetrievalLatencyBudgetMonitor):
        self._monitor = monitor

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "retrieval_stats_1h": self._monitor.summary(3600.0),
        }
```

## Comparison

| Approach | Fan-Out Execution | Latency Budget | Cross-Source Dedup | Score Normalization | Timeout Tracking |
|---|---|---|---|---|---|
| ParallelRetrievalEngine | Yes (asyncio.gather) | Yes (wait timeout) | No | No | Yes |
| CrossSourceResultMerger | No | No | Yes (Jaccard) | Yes (weight × norm) | No |
| RetrievalLatencyBudgetMonitor | No | No | No | No | Yes (rate) |

**Best for production**: Set `latency_budget_seconds` to your P95 target for total retrieval latency — if your SLO is 2 seconds for context assembly, budget 1.5 seconds for parallel retrieval. Use `source.timeout_seconds` to give individual sources a per-source limit that is shorter than the global budget; a slow source that uses the full 5-second budget defeats the point of parallelism. Set `source.weight` to reflect data quality: a curated knowledge base should outweight uncurated web results for most queries. Monitor `source_timeout_rate` per source — a source that times out on more than 10% of requests should be investigated for performance issues rather than simply increasing its timeout.
