---
title: "Agent Doesn't Implement Sparse Retrieval for Hybrid Search"
description: "AI agents that rely solely on dense vector search miss exact keyword matches, rare technical terms, and out-of-vocabulary tokens that BM25 sparse retrieval handles well. Hybrid search combines BM25 sparse scores with dense embedding scores using Reciprocal Rank Fusion or linear interpolation, consistently outperforming either approach alone by 10–30% on BEIR benchmarks."
date: 2025-02-14
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-sparse-retrieval-for-hybrid-search
tags:
  - sparse-retrieval
  - bm25
  - hybrid-search
  - rrf
  - dense-retrieval
  - rag
  - reciprocal-rank-fusion
  - performance
symptoms:
  - "Agent fails to retrieve documents containing rare technical terms or product codes"
  - "Dense-only retrieval misses exact string matches like error codes or function names"
  - "Recall drops on out-of-vocabulary queries that weren't in the embedding training corpus"
  - "Agent retrieves semantically similar but lexically different documents, missing exact answers"
  - "RAG pipeline retrieval recall is below 0.7 on keyword-heavy evaluation sets"
---

## Problem

Dense vector search retrieves documents that are *semantically* close to the query embedding. It fails on lexical matches: a query for `SIGKILL` or `CVE-2024-12345` may not retrieve the relevant document if those tokens were rare in the embedding model's training data. BM25 sparse retrieval computes term-frequency–inverse-document-frequency scores using exact token overlap — it excels precisely where dense search struggles. Hybrid search fuses both ranked lists so that documents matching either semantically or lexically rank at the top.

---

## Solution 1: BM25SparseRetriever — In-Process BM25 Index

```python
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Document:
    doc_id: str
    text: str
    metadata: Dict = field(default_factory=dict)


@dataclass
class ScoredDoc:
    doc_id: str
    score: float
    document: Optional[Document] = None


class BM25SparseRetriever:
    """
    In-process BM25 index over a document corpus.
    Suitable for corpora up to ~100k documents; use Elasticsearch
    or OpenSearch for larger deployments.

    Usage:
        retriever = BM25SparseRetriever(k1=1.5, b=0.75)
        retriever.index(docs)
        results = retriever.search("SIGKILL signal handler", top_k=20)
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: Dict[str, Document] = {}
        self._tf: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._df: Dict[str, int] = defaultdict(int)
        self._doc_lengths: Dict[str, int] = {}
        self._avg_dl: float = 0.0

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r"[a-zA-Z0-9_\-\.]+", text.lower())

    def index(self, docs: List[Document]):
        for doc in docs:
            self._docs[doc.doc_id] = doc
            tokens = self._tokenize(doc.text)
            self._doc_lengths[doc.doc_id] = len(tokens)
            seen = set()
            for token in tokens:
                self._tf[doc.doc_id][token] += 1
                if token not in seen:
                    self._df[token] += 1
                    seen.add(token)
        total_len = sum(self._doc_lengths.values())
        self._avg_dl = total_len / len(self._docs) if self._docs else 1.0

    def search(self, query: str, top_k: int = 10) -> List[ScoredDoc]:
        N = len(self._docs)
        query_tokens = self._tokenize(query)
        scores: Dict[str, float] = defaultdict(float)

        for token in query_tokens:
            df = self._df.get(token, 0)
            if df == 0:
                continue
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
            for doc_id, tf_map in self._tf.items():
                tf = tf_map.get(token, 0)
                if tf == 0:
                    continue
                dl = self._doc_lengths[doc_id]
                tf_norm = (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * dl / self._avg_dl)
                )
                scores[doc_id] += idf * tf_norm

        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return [
            ScoredDoc(doc_id=did, score=s, document=self._docs.get(did))
            for did, s in ranked
        ]

    def corpus_size(self) -> int:
        return len(self._docs)
```

---

## Solution 2: ReciprocalRankFusion — Merge Sparse and Dense Rankings

```python
from collections import defaultdict
from typing import Dict, List, Optional


class ReciprocalRankFusion:
    """
    Combines multiple ranked lists using Reciprocal Rank Fusion (RRF).
    RRF score for document d: sum_over_lists( 1 / (k + rank(d)) )
    where k=60 dampens the contribution of very high ranks.

    Usage:
        rrf = ReciprocalRankFusion(k=60)
        fused = rrf.fuse(
            ranked_lists=[bm25_results, dense_results],
            weights=[1.0, 1.0],   # equal weight; increase dense for semantic bias
        )
    """

    def __init__(self, k: int = 60):
        self.k = k

    def fuse(self, ranked_lists: List[List[ScoredDoc]],
             weights: Optional[List[float]] = None) -> List[ScoredDoc]:
        if weights is None:
            weights = [1.0] * len(ranked_lists)
        assert len(weights) == len(ranked_lists)

        rrf_scores: Dict[str, float] = defaultdict(float)
        docs: Dict[str, Optional[Document]] = {}

        for ranked, weight in zip(ranked_lists, weights):
            for rank, item in enumerate(ranked, start=1):
                rrf_scores[item.doc_id] += weight / (self.k + rank)
                if item.document and item.doc_id not in docs:
                    docs[item.doc_id] = item.document

        ranked_fused = sorted(rrf_scores.items(), key=lambda x: -x[1])
        return [
            ScoredDoc(doc_id=did, score=score, document=docs.get(did))
            for did, score in ranked_fused
        ]

    def fuse_with_scores(self, ranked_lists: List[List[ScoredDoc]],
                          weights: Optional[List[float]] = None,
                          dense_weight: float = 0.5,
                          sparse_weight: float = 0.5) -> List[ScoredDoc]:
        """
        Linear score interpolation alternative to RRF.
        Normalises scores within each list to [0,1] then combines.
        """
        if weights is None:
            weights = [sparse_weight, dense_weight]

        combined: Dict[str, float] = defaultdict(float)
        docs: Dict[str, Optional[Document]] = {}

        for ranked, weight in zip(ranked_lists, weights):
            if not ranked:
                continue
            max_score = max(r.score for r in ranked) or 1.0
            for item in ranked:
                combined[item.doc_id] += weight * (item.score / max_score)
                if item.document:
                    docs[item.doc_id] = item.document

        ranked_out = sorted(combined.items(), key=lambda x: -x[1])
        return [
            ScoredDoc(doc_id=did, score=s, document=docs.get(did))
            for did, s in ranked_out
        ]
```

---

## Solution 3: HybridSearchEngine — BM25 + Dense Vector Search

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class HybridSearchEngine:
    """
    Combines a BM25SparseRetriever with an async dense vector search function
    into a single hybrid retrieval interface. Both retrievers run in parallel;
    results are fused with RRF.

    Usage:
        engine = HybridSearchEngine(
            sparse=BM25SparseRetriever(),
            dense_search_fn=my_vector_db.search,   # async (query, top_k) -> List[ScoredDoc]
        )
        engine.index_sparse(docs)

        results = await engine.search("CVE-2024-12345 buffer overflow", top_k=10)
    """

    def __init__(self, sparse: BM25SparseRetriever,
                 dense_search_fn: Callable,
                 sparse_weight: float = 1.0,
                 dense_weight: float = 1.0,
                 rrf_k: int = 60):
        self._sparse = sparse
        self._dense_fn = dense_search_fn
        self._rrf = ReciprocalRankFusion(k=rrf_k)
        self._sparse_w = sparse_weight
        self._dense_w = dense_weight

    def index_sparse(self, docs: List[Document]):
        self._sparse.index(docs)

    async def search(self, query: str, top_k: int = 10,
                     sparse_candidates: int = 50,
                     dense_candidates: int = 50) -> List[ScoredDoc]:
        # Run both retrievers in parallel
        sparse_task = asyncio.get_event_loop().run_in_executor(
            None, self._sparse.search, query, sparse_candidates
        )
        dense_task = self._dense_fn(query, dense_candidates)

        sparse_results, dense_results = await asyncio.gather(
            sparse_task, dense_task
        )

        fused = self._rrf.fuse(
            [sparse_results, dense_results],
            weights=[self._sparse_w, self._dense_w],
        )
        return fused[:top_k]

    def set_weights(self, sparse: float, dense: float):
        """Adjust fusion weights at runtime (e.g., based on query type)."""
        self._sparse_w = sparse
        self._dense_w = dense
```

---

## Solution 4: QueryTypeRouter — Adaptive Weight Selection

```python
import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class QueryProfile:
    query: str
    query_type: str          # "lexical" | "semantic" | "mixed"
    sparse_weight: float
    dense_weight: float
    rationale: str


class QueryTypeRouter:
    """
    Analyses a query and selects BM25/dense weights based on query characteristics.
    Lexical queries (error codes, IDs, exact phrases) get higher sparse weight.
    Semantic queries (concepts, natural language) get higher dense weight.

    Usage:
        router = QueryTypeRouter()
        profile = router.profile("SIGKILL signal handler python")
        results = await engine.search(
            query=profile.query,
            # engine.set_weights called by router:
        )
        router.apply(engine, profile)
        results = await engine.search(profile.query)
    """

    # Patterns indicating lexical/exact-match intent
    _LEXICAL_PATTERNS = [
        re.compile(r"\b[A-Z]{2,}_[A-Z0-9_]+\b"),   # ERROR_CODE, SIGNAL_NAME
        re.compile(r"\bCVE-\d{4}-\d+\b"),            # CVE IDs
        re.compile(r"\b[0-9a-f]{8,}\b"),             # hex hashes
        re.compile(r'"[^"]+"'),                       # quoted phrases
        re.compile(r"\b\d{3,}\b"),                   # numeric IDs
        re.compile(r"\b[a-z_]+\([^)]*\)"),           # function_name()
    ]

    def profile(self, query: str) -> QueryProfile:
        lexical_hits = sum(
            1 for p in self._LEXICAL_PATTERNS if p.search(query)
        )
        word_count = len(query.split())

        if lexical_hits >= 2 or (lexical_hits >= 1 and word_count <= 4):
            return QueryProfile(query, "lexical",
                                sparse_weight=2.0, dense_weight=0.5,
                                rationale=f"{lexical_hits} lexical pattern(s) detected")

        if word_count >= 8 or "?" in query or "how" in query.lower():
            return QueryProfile(query, "semantic",
                                sparse_weight=0.5, dense_weight=2.0,
                                rationale="long natural-language query")

        return QueryProfile(query, "mixed",
                            sparse_weight=1.0, dense_weight=1.0,
                            rationale="balanced")

    def apply(self, engine: HybridSearchEngine, profile: QueryProfile):
        engine.set_weights(profile.sparse_weight, profile.dense_weight)
```

---

## Solution 5: SparseIndexUpdater — Incremental Document Ingestion

```python
import asyncio
import logging
import time
from typing import List, Optional

logger = logging.getLogger(__name__)


class SparseIndexUpdater:
    """
    Batches incoming documents and rebuilds the BM25 index incrementally.
    Avoids full index rebuilds on every document addition by buffering
    additions and rebuilding only when a flush threshold is reached.

    Usage:
        updater = SparseIndexUpdater(retriever, flush_every=500)

        # Ingest documents from a stream:
        await updater.add(new_doc)
        await updater.add(another_doc)

        # Or trigger flush manually:
        await updater.flush()
    """

    def __init__(self, retriever: BM25SparseRetriever,
                 flush_every: int = 500,
                 flush_interval_s: float = 60.0):
        self._retriever = retriever
        self._flush_every = flush_every
        self._flush_interval = flush_interval_s
        self._buffer: List[Document] = []
        self._last_flush = time.monotonic()
        self._total_indexed = 0

    async def add(self, doc: Document):
        self._buffer.append(doc)
        age = time.monotonic() - self._last_flush
        if len(self._buffer) >= self._flush_every or age > self._flush_interval:
            await self.flush()

    async def flush(self):
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()
        await asyncio.get_event_loop().run_in_executor(
            None, self._retriever.index, batch
        )
        self._total_indexed += len(batch)
        self._last_flush = time.monotonic()
        logger.info("bm25_flush docs=%d total=%d", len(batch), self._total_indexed)

    def stats(self) -> dict:
        return {
            "indexed": self._retriever.corpus_size(),
            "buffered": len(self._buffer),
            "total_ingested": self._total_indexed,
        }
```

---

## Solution 6: HybridRAGTool — Agent Tool Integration

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class RAGResult:
    query: str
    documents: List[Dict[str, Any]]
    retrieval_method: str
    sparse_weight: float
    dense_weight: float


class HybridRAGTool:
    """
    Drop-in agent tool that performs hybrid BM25 + dense retrieval
    with automatic query profiling and result formatting.

    Usage:
        tool = HybridRAGTool(engine, router)

        # Register as agent tool:
        result = await tool.retrieve(
            query="SIGKILL not caught by try/except in subprocess",
            top_k=5,
        )
        # result.documents contains ranked, deduplicated chunks
    """

    def __init__(self, engine: HybridSearchEngine,
                 router: Optional[QueryTypeRouter] = None):
        self._engine = engine
        self._router = router or QueryTypeRouter()

    async def retrieve(self, query: str,
                        top_k: int = 5) -> RAGResult:
        profile = self._router.profile(query)
        self._router.apply(self._engine, profile)

        results = await self._engine.search(query, top_k=top_k)

        docs = []
        for r in results:
            doc_entry: Dict[str, Any] = {
                "doc_id": r.doc_id,
                "score": round(r.score, 4),
            }
            if r.document:
                doc_entry["text"] = r.document.text
                doc_entry["metadata"] = r.document.metadata
            docs.append(doc_entry)

        return RAGResult(
            query=query,
            documents=docs,
            retrieval_method=profile.query_type,
            sparse_weight=profile.sparse_weight,
            dense_weight=profile.dense_weight,
        )

    async def index(self, docs: List[Document]):
        await asyncio.get_event_loop().run_in_executor(
            None, self._engine.index_sparse, docs
        )
```

---

## Comparison

| Approach | Sparse | Dense | Fusion | Auto-Weight | Incremental |
|---|---|---|---|---|---|
| **BM25SparseRetriever** | Yes | No | No | No | No |
| **ReciprocalRankFusion** | Merge | Merge | RRF + Linear | No | No |
| **HybridSearchEngine** | Yes | Yes | RRF | No | No |
| **QueryTypeRouter** | Via engine | Via engine | Adaptive | Yes | No |
| **SparseIndexUpdater** | Yes | No | No | No | Yes |
| **HybridRAGTool** | Yes | Yes | RRF + adaptive | Yes | No |

**Key insight**: use RRF rather than score-based interpolation as the default fusion strategy — it is robust to score scale differences between BM25 and cosine similarity. Set `k=60` (standard) and apply `QueryTypeRouter` to shift weights toward sparse for queries containing exact identifiers or quoted phrases, and toward dense for long natural-language questions. Even a 50/50 blend typically outperforms pure dense retrieval by 10–20% recall on technical corpora.
