---
title: "Agent Doesn't Implement Retrieval Precision and Recall Tracking"
description: "Agents that use retrieval-augmented generation without measuring retrieval quality cannot distinguish whether wrong answers stem from a bad LLM response or bad retrieval: a document with the right answer was never fetched. Implement retrieval precision and recall tracking that compares retrieved documents against relevance judgements, computes per-query precision@k and recall@k, and surfaces retrieval quality trends over time."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-retrieval-precision-and-recall-tracking
tags: [retrieval, precision, recall, rag, relevance-judgement, information-retrieval, evaluation]
symptoms:
  - "Agent gives wrong answers but it's unclear whether retrieval or generation is at fault"
  - "No per-query precision or recall metrics — only end-to-end answer quality is measured"
  - "Retrieval system changes ship without regression tests on retrieval quality"
  - "No historical record of which queries consistently retrieve irrelevant documents"
  - "P@k or R@k cannot be computed because no relevance judgements are stored"
---

## Why This Happens

RAG pipelines are evaluated end-to-end: a correct final answer is treated as evidence that retrieval worked. This masks retrieval failures where the LLM salvaged a bad retrieval with parametric knowledge, and retrieval regressions where a config change reduced recall but the LLM masked it temporarily. Measuring retrieval quality requires storing relevance judgements — binary or graded labels that say whether a retrieved document was relevant to a query — and computing precision@k (fraction of top-k that are relevant) and recall@k (fraction of known-relevant documents that appear in top-k). Without this instrumentation, retrieval quality is invisible.

## Solution 1: Retrieval Query Record

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RetrievedDocument:
    doc_id: str
    content_snippet: str          # first 200 chars for logging
    retrieval_score: float        # cosine similarity or BM25 score
    rank: int                     # 1-indexed position in result list
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalQueryRecord:
    query_id: str
    query_text: str
    retrieved_docs: List[RetrievedDocument]
    k: int                        # number of documents retrieved
    retrieval_latency_ms: float
    index_name: str = ""
    session_id: str = ""
    timestamp: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def doc_ids_at_k(self, k: int) -> List[str]:
        return [d.doc_id for d in sorted(self.retrieved_docs, key=lambda d: d.rank)[:k]]
```

## Solution 2: Relevance Judgement Store

```python
import json
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Set


class RelevanceGrade(IntEnum):
    NOT_RELEVANT = 0
    PARTIALLY_RELEVANT = 1
    HIGHLY_RELEVANT = 2


@dataclass
class RelevanceJudgement:
    query_id: str
    doc_id: str
    grade: RelevanceGrade
    judged_by: str          # "human", "llm-judge", "click-signal"
    judged_at: float = 0.0


class RelevanceJudgementStore:
    """
    Stores relevance judgements keyed by (query_id, doc_id).
    Supports persisting to JSONL for offline analysis.
    """

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path) if path else None
        self._judgements: Dict[str, Dict[str, RelevanceJudgement]] = {}
        self._lock = Lock()
        if self._path and self._path.exists():
            self._load()

    def add(self, judgement: RelevanceJudgement) -> None:
        with self._lock:
            if judgement.query_id not in self._judgements:
                self._judgements[judgement.query_id] = {}
            judgement.judged_at = judgement.judged_at or time.time()
            self._judgements[judgement.query_id][judgement.doc_id] = judgement
            if self._path:
                with self._path.open("a") as f:
                    f.write(json.dumps({
                        "query_id": judgement.query_id,
                        "doc_id": judgement.doc_id,
                        "grade": judgement.grade.value,
                        "judged_by": judgement.judged_by,
                        "judged_at": judgement.judged_at,
                    }) + "\n")

    def relevant_doc_ids(self, query_id: str, min_grade: RelevanceGrade = RelevanceGrade.PARTIALLY_RELEVANT) -> Set[str]:
        with self._lock:
            return {
                doc_id
                for doc_id, j in self._judgements.get(query_id, {}).items()
                if j.grade >= min_grade
            }

    def has_judgements(self, query_id: str) -> bool:
        with self._lock:
            return bool(self._judgements.get(query_id))

    def _load(self) -> None:
        for line in self._path.read_text().splitlines():
            try:
                d = json.loads(line)
                j = RelevanceJudgement(
                    query_id=d["query_id"],
                    doc_id=d["doc_id"],
                    grade=RelevanceGrade(d["grade"]),
                    judged_by=d["judged_by"],
                    judged_at=d.get("judged_at", 0.0),
                )
                if j.query_id not in self._judgements:
                    self._judgements[j.query_id] = {}
                self._judgements[j.query_id][j.doc_id] = j
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
```

## Solution 3: Precision and Recall Calculator

```python
from typing import List, Optional, Set


class PrecisionRecallCalculator:
    """
    Computes precision@k and recall@k for a single retrieval query
    given the set of relevant document IDs.
    """

    @staticmethod
    def precision_at_k(retrieved_ids: List[str], relevant_ids: Set[str], k: int) -> float:
        top_k = retrieved_ids[:k]
        if not top_k:
            return 0.0
        hits = sum(1 for doc_id in top_k if doc_id in relevant_ids)
        return hits / len(top_k)

    @staticmethod
    def recall_at_k(retrieved_ids: List[str], relevant_ids: Set[str], k: int) -> float:
        if not relevant_ids:
            return 1.0   # undefined — treat as perfect when no relevant docs known
        top_k = retrieved_ids[:k]
        hits = sum(1 for doc_id in top_k if doc_id in relevant_ids)
        return hits / len(relevant_ids)

    @staticmethod
    def average_precision(retrieved_ids: List[str], relevant_ids: Set[str]) -> float:
        """
        Computes AP: average of precision values at positions where
        a relevant document was retrieved.
        """
        if not relevant_ids:
            return 0.0
        total = 0.0
        hits = 0
        for rank, doc_id in enumerate(retrieved_ids, start=1):
            if doc_id in relevant_ids:
                hits += 1
                total += hits / rank
        return total / len(relevant_ids)

    @staticmethod
    def reciprocal_rank(retrieved_ids: List[str], relevant_ids: Set[str]) -> float:
        for rank, doc_id in enumerate(retrieved_ids, start=1):
            if doc_id in relevant_ids:
                return 1.0 / rank
        return 0.0
```

## Solution 4: Per-Query Retrieval Evaluator

```python
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class RetrievalEvaluationResult:
    query_id: str
    k: int
    precision_at_k: float
    recall_at_k: float
    average_precision: float
    reciprocal_rank: float
    retrieved_count: int
    relevant_count: int
    evaluated_at: float


class PerQueryRetrievalEvaluator:
    """
    Evaluates a single retrieval query against stored relevance judgements.
    Returns None when no judgements exist for the query.
    """

    def __init__(
        self,
        judgement_store: RelevanceJudgementStore,
        calculator: PrecisionRecallCalculator,
        default_k: int = 5,
    ):
        self._store = judgement_store
        self._calc = calculator
        self._default_k = default_k

    def evaluate(
        self,
        record: RetrievalQueryRecord,
        k: Optional[int] = None,
    ) -> Optional[RetrievalEvaluationResult]:
        if not self._store.has_judgements(record.query_id):
            return None

        effective_k = k or self._default_k
        relevant_ids = self._store.relevant_doc_ids(record.query_id)
        retrieved_ids = record.doc_ids_at_k(len(record.retrieved_docs))

        return RetrievalEvaluationResult(
            query_id=record.query_id,
            k=effective_k,
            precision_at_k=self._calc.precision_at_k(retrieved_ids, relevant_ids, effective_k),
            recall_at_k=self._calc.recall_at_k(retrieved_ids, relevant_ids, effective_k),
            average_precision=self._calc.average_precision(retrieved_ids, relevant_ids),
            reciprocal_rank=self._calc.reciprocal_rank(retrieved_ids, relevant_ids),
            retrieved_count=len(retrieved_ids),
            relevant_count=len(relevant_ids),
            evaluated_at=time.time(),
        )
```

## Solution 5: Retrieval Quality Trend Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class RetrievalQualityTrendTracker:
    """
    Accumulates per-query evaluation results and surfaces aggregate
    precision@k, recall@k, MAP, and MRR over sliding time windows.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._results: Deque[Tuple[float, RetrievalEvaluationResult]] = deque()
        self._lock = Lock()

    def record(self, result: RetrievalEvaluationResult) -> None:
        with self._lock:
            self._results.append((time.time(), result))
            if len(self._results) > self._max:
                self._results.popleft()

    def _recent(self, window_seconds: float) -> List[RetrievalEvaluationResult]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [r for ts, r in self._results if ts >= cutoff]

    def mean_average_precision(self, window_seconds: float = 3600.0) -> Optional[float]:
        results = self._recent(window_seconds)
        if not results:
            return None
        return round(sum(r.average_precision for r in results) / len(results), 4)

    def mean_reciprocal_rank(self, window_seconds: float = 3600.0) -> Optional[float]:
        results = self._recent(window_seconds)
        if not results:
            return None
        return round(sum(r.reciprocal_rank for r in results) / len(results), 4)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        results = self._recent(window_seconds)
        if not results:
            return {"window_seconds": window_seconds, "evaluated_queries": 0}

        return {
            "window_seconds": window_seconds,
            "evaluated_queries": len(results),
            "mean_precision_at_k": round(
                sum(r.precision_at_k for r in results) / len(results), 4
            ),
            "mean_recall_at_k": round(
                sum(r.recall_at_k for r in results) / len(results), 4
            ),
            "map": self.mean_average_precision(window_seconds),
            "mrr": self.mean_reciprocal_rank(window_seconds),
            "zero_recall_queries": sum(1 for r in results if r.recall_at_k == 0.0),
            "zero_recall_rate": round(
                sum(1 for r in results if r.recall_at_k == 0.0) / len(results), 4
            ),
        }
```

## Solution 6: Retrieval Precision and Recall Dashboard

```python
import time
from typing import Optional


class RetrievalPrecisionRecallDashboard:
    """
    Combines per-query evaluation, trend tracking, and low-recall
    query identification into a single operational report.
    """

    def __init__(
        self,
        evaluator: PerQueryRetrievalEvaluator,
        trend_tracker: RetrievalQualityTrendTracker,
        low_recall_threshold: float = 0.30,
    ):
        self._evaluator = evaluator
        self._tracker = trend_tracker
        self._threshold = low_recall_threshold
        self._low_recall_queries: list = []

    def observe(self, record: RetrievalQueryRecord) -> Optional[RetrievalEvaluationResult]:
        result = self._evaluator.evaluate(record)
        if result is None:
            return None
        self._tracker.record(result)
        if result.recall_at_k < self._threshold:
            self._low_recall_queries.append({
                "query_id": result.query_id,
                "recall_at_k": result.recall_at_k,
                "precision_at_k": result.precision_at_k,
                "relevant_count": result.relevant_count,
                "recorded_at": time.time(),
            })
            if len(self._low_recall_queries) > 200:
                self._low_recall_queries.pop(0)
        return result

    def render(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent_low = [
            q for q in self._low_recall_queries
            if q["recorded_at"] >= cutoff
        ]
        return {
            "generated_at": time.time(),
            "retrieval_quality": self._tracker.summary(window_seconds),
            "low_recall_queries": {
                "threshold": self._threshold,
                "count_in_window": len(recent_low),
                "examples": recent_low[-10:],
            },
        }
```

## Comparison

| Approach | Query Recording | Relevance Judgements | P@k / R@k | AP / RR | Trend & MAP/MRR |
|---|---|---|---|---|---|
| RetrievalQueryRecord | Yes | No | No | No | No |
| RelevanceJudgementStore | No | Yes (persist JSONL) | No | No | No |
| PrecisionRecallCalculator | No | No | Yes | Yes (AP, RR) | No |
| PerQueryRetrievalEvaluator | Via record | Via store | Yes | Yes | No |
| RetrievalQualityTrendTracker | No | No | Via results | Via results | Yes |
| RetrievalPrecisionRecallDashboard | No | No | No | No | Yes + low-recall |

**Best for production**: Seed `RelevanceJudgementStore` with LLM-generated judgements on a representative query set (run an LLM judge offline, write results to the JSONL file) — this gives immediate coverage without waiting for human labels. Track `zero_recall_rate` as your primary retrieval health metric: it directly measures queries where the answer was never retrieved, which is always fixable at the retrieval layer. Alert when `zero_recall_rate` exceeds 15% in a rolling hour window. Use `mean_average_precision` to compare embedding model or chunking strategy changes: a MAP regression after a dependency update is a clear signal to roll back the retrieval config, not tune the prompt.
