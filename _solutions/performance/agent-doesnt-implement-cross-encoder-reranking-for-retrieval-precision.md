---
title: "Agent Doesn't Implement Cross-Encoder Reranking for Retrieval Precision"
description: "AI agents that stop at bi-encoder vector retrieval return candidates ranked by approximate embedding similarity — a proxy for relevance that systematically misranks documents with different vocabulary. Cross-encoder reranking applies a second-stage model that reads the query and each candidate document together, producing precise relevance scores that improve NDCG@10 by 15–40% on typical RAG benchmarks at the cost of evaluating only the top-K candidates."
date: 2025-02-14
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-cross-encoder-reranking-for-retrieval-precision
tags:
  - reranking
  - cross-encoder
  - retrieval
  - rag
  - precision
  - two-stage
  - sentence-transformers
  - performance
symptoms:
  - "Agent retrieves semantically adjacent but factually wrong documents"
  - "Bi-encoder recall is high but NDCG@5 is low — top results are not the best ones"
  - "Agent cites a less relevant document when a more relevant one is present at rank 8"
  - "RAG hallucination rate is high despite broad retrieval coverage"
  - "Re-running retrieval with different phrasings returns different top-1 documents"
---

## Problem

Bi-encoders encode query and documents independently; similarity is a dot-product of two independent embeddings. This is fast but imprecise: the model cannot attend to how query tokens interact with document tokens. Cross-encoders process the concatenated `[query; document]` pair and produce a single relevance score — capturing token-level interactions that bi-encoders miss. The two-stage pattern uses the bi-encoder to cheaply retrieve top-50 candidates, then the cross-encoder to precisely rerank to top-5, combining the best of both.

---

## Solution 1: CrossEncoderReranker — Two-Stage Retrieval Pipeline

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from sentence_transformers import CrossEncoder
    _ST = True
except ImportError:
    _ST = False


@dataclass
class RankedDocument:
    doc_id: str
    text: str
    bi_encoder_score: float
    cross_encoder_score: Optional[float]
    final_rank: int
    metadata: Dict[str, Any] = None


class CrossEncoderReranker:
    """
    Two-stage retrieval: bi-encoder retrieves top-N candidates,
    cross-encoder reranks to top-K.

    Usage:
        reranker = CrossEncoderReranker(
            model_name="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_k=5,
            candidates=50,
        )
        results = await reranker.retrieve_and_rerank(
            query="how does SIGKILL differ from SIGTERM",
            retrieve_fn=vector_db.search,   # (query, top_n) -> List[RawDoc]
        )
    """

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
                 top_k: int = 5,
                 candidates: int = 50,
                 batch_size: int = 32):
        if not _ST:
            raise RuntimeError("pip install sentence-transformers")
        self._model = CrossEncoder(model_name)
        self._top_k = top_k
        self._candidates = candidates
        self._batch_size = batch_size

    async def retrieve_and_rerank(self, query: str,
                                   retrieve_fn: Callable) -> List[RankedDocument]:
        # Stage 1: cheap bi-encoder retrieval
        raw_results = await retrieve_fn(query, self._candidates)

        if not raw_results:
            return []

        # Stage 2: cross-encoder scoring
        pairs = [(query, doc["text"]) for doc in raw_results]
        scores = await asyncio.get_event_loop().run_in_executor(
            None, self._score_pairs, pairs
        )

        ranked = sorted(
            zip(raw_results, scores),
            key=lambda x: -x[1],
        )

        return [
            RankedDocument(
                doc_id=doc.get("doc_id", str(i)),
                text=doc["text"],
                bi_encoder_score=float(doc.get("score", 0.0)),
                cross_encoder_score=float(score),
                final_rank=i + 1,
                metadata=doc.get("metadata"),
            )
            for i, (doc, score) in enumerate(ranked[:self._top_k])
        ]

    def _score_pairs(self, pairs: List[Tuple[str, str]]) -> List[float]:
        scores = []
        for i in range(0, len(pairs), self._batch_size):
            batch = pairs[i:i + self._batch_size]
            batch_scores = self._model.predict(batch)
            scores.extend(batch_scores.tolist())
        return scores

    def rerank_sync(self, query: str,
                     documents: List[Dict[str, Any]]) -> List[RankedDocument]:
        """Synchronous rerank for use outside async context."""
        pairs = [(query, doc["text"]) for doc in documents]
        scores = self._score_pairs(pairs)
        ranked = sorted(zip(documents, scores), key=lambda x: -x[1])
        return [
            RankedDocument(
                doc_id=doc.get("doc_id", str(i)),
                text=doc["text"],
                bi_encoder_score=float(doc.get("score", 0.0)),
                cross_encoder_score=float(s),
                final_rank=i + 1,
                metadata=doc.get("metadata"),
            )
            for i, (doc, s) in enumerate(ranked[:self._top_k])
        ]
```

---

## Solution 2: CachedCrossEncoderReranker — Score Caching for Repeated Pairs

```python
import hashlib
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple


class CachedCrossEncoderReranker:
    """
    Wraps CrossEncoderReranker with a query-document pair cache.
    In multi-turn conversations where the same documents are retrieved
    repeatedly (common in RAG), cross-encoder scoring is skipped for
    already-scored pairs.

    Usage:
        cached = CachedCrossEncoderReranker(
            reranker=CrossEncoderReranker(),
            cache_size=2000,
            ttl_s=600,
        )
        results = cached.rerank(query, documents)
    """

    def __init__(self, reranker: CrossEncoderReranker,
                 cache_size: int = 2000,
                 ttl_s: float = 600.0):
        self._reranker = reranker
        self._cache: OrderedDict[str, Tuple[float, float]] = OrderedDict()
        self._cache_size = cache_size
        self._ttl = ttl_s
        self._hits = 0
        self._misses = 0

    def _cache_key(self, query: str, text: str) -> str:
        raw = f"{query}\x00{text}"
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    def rerank(self, query: str,
                documents: List[Dict[str, Any]]) -> List[RankedDocument]:
        now = time.monotonic()
        uncached_idx = []
        uncached_pairs = []

        scores: List[Optional[float]] = [None] * len(documents)

        for i, doc in enumerate(documents):
            key = self._cache_key(query, doc["text"])
            entry = self._cache.get(key)
            if entry:
                score, ts = entry
                if now - ts < self._ttl:
                    scores[i] = score
                    self._hits += 1
                    self._cache.move_to_end(key)
                    continue
                del self._cache[key]
            uncached_idx.append(i)
            uncached_pairs.append((query, doc["text"]))
            self._misses += 1

        if uncached_pairs:
            new_scores = self._reranker._score_pairs(uncached_pairs)
            for i, (doc_idx, score) in enumerate(zip(uncached_idx, new_scores)):
                scores[doc_idx] = float(score)
                key = self._cache_key(query, documents[doc_idx]["text"])
                self._cache[key] = (float(score), now)
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)

        ranked = sorted(
            enumerate(zip(documents, scores)),
            key=lambda x: -(x[1][1] or -999),
        )
        top_k = self._reranker._top_k
        return [
            RankedDocument(
                doc_id=doc.get("doc_id", str(orig_i)),
                text=doc["text"],
                bi_encoder_score=float(doc.get("score", 0.0)),
                cross_encoder_score=float(score or 0.0),
                final_rank=rank + 1,
                metadata=doc.get("metadata"),
            )
            for rank, (orig_i, (doc, score)) in enumerate(ranked[:top_k])
        ]

    def cache_stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
            "cached_pairs": len(self._cache),
        }
```

---

## Solution 3: APIReranker — Cohere / Jina Rerank API Client

```python
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class APIReranker:
    """
    Reranks using Cohere Rerank API or Jina Reranker API.
    Avoids running a local cross-encoder model; suitable for
    serverless deployments where loading a 200 MB model is impractical.

    Usage:
        reranker = APIReranker(
            provider="cohere",
            api_key=os.environ["COHERE_API_KEY"],
            model="rerank-english-v3.0",
            top_k=5,
        )
        results = await reranker.rerank(query, documents)
    """

    def __init__(self, provider: str = "cohere",
                 api_key: str = "",
                 model: str = "rerank-english-v3.0",
                 top_k: int = 5):
        self._provider = provider
        self._api_key = api_key
        self._model = model
        self._top_k = top_k

    async def rerank(self, query: str,
                      documents: List[Dict[str, Any]]) -> List[RankedDocument]:
        if self._provider == "cohere":
            return await self._cohere_rerank(query, documents)
        elif self._provider == "jina":
            return await self._jina_rerank(query, documents)
        raise ValueError(f"Unknown provider: {self._provider}")

    async def _cohere_rerank(self, query: str,
                               documents: List[Dict]) -> List[RankedDocument]:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("pip install httpx")

        texts = [d["text"] for d in documents]
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.cohere.com/v1/rerank",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "query": query,
                    "documents": texts,
                    "top_n": self._top_k,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for rank, item in enumerate(data["results"], start=1):
            idx = item["index"]
            doc = documents[idx]
            results.append(RankedDocument(
                doc_id=doc.get("doc_id", str(idx)),
                text=doc["text"],
                bi_encoder_score=float(doc.get("score", 0.0)),
                cross_encoder_score=float(item["relevance_score"]),
                final_rank=rank,
                metadata=doc.get("metadata"),
            ))
        return results

    async def _jina_rerank(self, query: str,
                             documents: List[Dict]) -> List[RankedDocument]:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("pip install httpx")

        texts = [d["text"] for d in documents]
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.jina.ai/v1/rerank",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "query": query,
                    "documents": texts,
                    "top_n": self._top_k,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for rank, item in enumerate(data["results"], start=1):
            idx = item["index"]
            doc = documents[idx]
            results.append(RankedDocument(
                doc_id=doc.get("doc_id", str(idx)),
                text=doc["text"],
                bi_encoder_score=float(doc.get("score", 0.0)),
                cross_encoder_score=float(item["relevance_score"]),
                final_rank=rank,
                metadata=doc.get("metadata"),
            ))
        return results
```

---

## Solution 4: RerankingLatencyTracker — Measure Two-Stage Pipeline Cost

```python
import logging
import statistics
import time
from collections import deque
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


class RerankingLatencyTracker:
    """
    Wraps any reranker and measures retrieval vs. reranking latency
    separately, surfacing whether cross-encoder cost is dominating
    total query time.

    Usage:
        tracker = RerankingLatencyTracker(reranker=cached_reranker)
        results = await tracker.rerank(query, documents)
        print(tracker.report())
    """

    def __init__(self, reranker, window: int = 200):
        self._reranker = reranker
        self._retrieval_ms: deque = deque(maxlen=window)
        self._rerank_ms: deque = deque(maxlen=window)

    async def retrieve_and_rerank(self, query: str,
                                   retrieve_fn: Callable) -> List[RankedDocument]:
        t0 = time.monotonic()
        candidates = await retrieve_fn(query, 50)
        retrieval_ms = (time.monotonic() - t0) * 1000
        self._retrieval_ms.append(retrieval_ms)

        t1 = time.monotonic()
        if hasattr(self._reranker, "rerank"):
            results = self._reranker.rerank(query, candidates)
        else:
            results = await self._reranker.retrieve_and_rerank(query, retrieve_fn)
        rerank_ms = (time.monotonic() - t1) * 1000
        self._rerank_ms.append(rerank_ms)

        return results

    def report(self) -> Dict[str, Any]:
        def _stats(samples):
            if not samples:
                return {}
            s = list(samples)
            return {
                "p50": round(statistics.median(s), 1),
                "p95": round(sorted(s)[int(len(s) * 0.95)], 1),
                "mean": round(statistics.mean(s), 1),
            }
        return {
            "retrieval_ms": _stats(self._retrieval_ms),
            "rerank_ms": _stats(self._rerank_ms),
            "samples": len(self._retrieval_ms),
        }
```

---

## Solution 5: AdaptiveReranker — Skip Reranking for Simple Queries

```python
import re
from typing import Any, Callable, Dict, List, Optional


class AdaptiveReranker:
    """
    Skips cross-encoder reranking for queries where bi-encoder results
    are already high-confidence (high top-1 score, low score variance)
    or where the query is short and exact-match-like.
    Reduces cross-encoder invocations by 30–50% without measurable
    precision loss on simple queries.

    Usage:
        adaptive = AdaptiveReranker(
            reranker=CrossEncoderReranker(),
            min_bi_score=0.85,
            max_candidates_variance=0.02,
        )
        results = adaptive.rerank_if_needed(query, candidates)
    """

    _SIMPLE_QUERY = re.compile(r"^\w[\w\s\-]{0,30}$")

    def __init__(self, reranker: CrossEncoderReranker,
                 min_bi_score: float = 0.85,
                 max_variance: float = 0.02):
        self._reranker = reranker
        self._min_score = min_bi_score
        self._max_variance = max_variance
        self._skipped = 0
        self._reranked = 0

    def _should_rerank(self, query: str,
                        candidates: List[Dict[str, Any]]) -> bool:
        if not candidates:
            return False
        # Short exact-match queries rarely benefit from reranking
        if self._SIMPLE_QUERY.match(query.strip()) and len(candidates) <= 5:
            return False
        scores = [float(c.get("score", 0.0)) for c in candidates]
        if scores[0] >= self._min_score:
            import statistics as _s
            if len(scores) > 1 and _s.variance(scores) < self._max_variance:
                return False
        return True

    def rerank_if_needed(self, query: str,
                          candidates: List[Dict[str, Any]]) -> List[RankedDocument]:
        if self._should_rerank(query, candidates):
            self._reranked += 1
            return self._reranker.rerank_sync(query, candidates)
        # Return candidates as-is, wrapped in RankedDocument
        self._skipped += 1
        return [
            RankedDocument(
                doc_id=d.get("doc_id", str(i)),
                text=d["text"],
                bi_encoder_score=float(d.get("score", 0.0)),
                cross_encoder_score=None,
                final_rank=i + 1,
                metadata=d.get("metadata"),
            )
            for i, d in enumerate(candidates[:self._reranker._top_k])
        ]

    def efficiency_report(self) -> Dict[str, Any]:
        total = self._reranked + self._skipped
        return {
            "total_queries": total,
            "reranked": self._reranked,
            "skipped": self._skipped,
            "skip_rate": round(self._skipped / total, 3) if total else 0.0,
        }
```

---

## Solution 6: HybridRerankPipeline — BM25 + Dense + Cross-Encoder

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class HybridRerankPipeline:
    """
    Full three-stage retrieval pipeline:
    1. BM25 sparse retrieval (top-30)
    2. Dense vector retrieval (top-30)
    3. RRF fusion -> cross-encoder rerank to top-K

    Achieves the highest recall + precision by combining lexical coverage,
    semantic coverage, and precise relevance scoring.

    Usage:
        pipeline = HybridRerankPipeline(
            bm25=BM25SparseRetriever(),
            dense_fn=vector_db.search,
            reranker=CrossEncoderReranker(top_k=5),
        )
        pipeline.index(docs)
        results = await pipeline.query("CVE-2024-12345 buffer overflow exploit")
    """

    def __init__(self, bm25, dense_fn: Callable,
                 reranker: CrossEncoderReranker,
                 sparse_candidates: int = 30,
                 dense_candidates: int = 30):
        self._bm25 = bm25
        self._dense_fn = dense_fn
        self._reranker = reranker
        self._sparse_n = sparse_candidates
        self._dense_n = dense_candidates
        self._rrf_k = 60

    def index(self, docs):
        self._bm25.index(docs)

    async def query(self, query: str) -> List[RankedDocument]:
        sparse_task = asyncio.get_event_loop().run_in_executor(
            None, self._bm25.search, query, self._sparse_n
        )
        dense_task = self._dense_fn(query, self._dense_n)
        sparse_results, dense_results = await asyncio.gather(sparse_task, dense_task)

        # RRF fusion
        rrf_scores: Dict[str, float] = {}
        doc_map: Dict[str, Any] = {}

        for rank, item in enumerate(sparse_results, 1):
            rrf_scores[item.doc_id] = rrf_scores.get(item.doc_id, 0) + 1 / (self._rrf_k + rank)
            if item.document:
                doc_map[item.doc_id] = {"doc_id": item.doc_id,
                                         "text": item.document.text,
                                         "score": item.score}

        for rank, item in enumerate(dense_results, 1):
            rrf_scores[item.get("doc_id", "")] = (
                rrf_scores.get(item.get("doc_id", ""), 0) + 1 / (self._rrf_k + rank)
            )
            doc_map[item.get("doc_id", "")] = item

        fused = sorted(doc_map.values(),
                        key=lambda d: -rrf_scores.get(d.get("doc_id", ""), 0))

        # Cross-encoder rerank the fused candidates
        return self._reranker.rerank_sync(query, fused)
```

---

## Comparison

| Approach | Local Model | API | Caching | Adaptive Skip | Three-Stage |
|---|---|---|---|---|---|
| **CrossEncoderReranker** | Yes | No | No | No | No |
| **CachedCrossEncoderReranker** | Yes | No | Yes | No | No |
| **APIReranker** | No | Cohere/Jina | No | No | No |
| **RerankingLatencyTracker** | Wrapper | Wrapper | No | No | No |
| **AdaptiveReranker** | Yes | No | No | Yes | No |
| **HybridRerankPipeline** | Yes | No | No | No | Yes |

**Key insight**: always fetch more candidates (50–100) from the bi-encoder than you plan to return (5–10), then rerank. The cross-encoder only needs to score the top-50 candidates, not the full corpus, so its cost is bounded. Use `CachedCrossEncoderReranker` in multi-turn conversations where the same documents recur, and `AdaptiveReranker` to skip reranking on short exact-match queries — these two optimisations together can reduce cross-encoder invocations by 40–60% without measurable precision loss.
