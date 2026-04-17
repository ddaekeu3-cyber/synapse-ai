---
title: "Agent Doesn't Implement Retrieval Precision and Recall Tracking"
description: "Agents with RAG pipelines that never measure retrieval quality cannot tell whether poor answers stem from bad retrieval or bad reasoning: if the right documents are not retrieved, no amount of LLM quality improvement will fix the output. Implement retrieval precision and recall tracking using implicit signals (document usage in responses), explicit signals (user feedback), and labeled evaluation sets to continuously measure and alert on retrieval quality degradation."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-retrieval-precision-recall-tracking
tags: [retrieval-quality, precision-recall, rag-metrics, document-usage, retrieval-evaluation, mrr-tracking]
symptoms:
  - "Agent gives wrong answers but LLM logs show correct reasoning — root cause is retrieval"
  - "No measurement of whether retrieved documents are actually used in responses"
  - "Retrieval index updated but no evaluation of whether quality improved or degraded"
  - "User feedback correlated to full response quality, not isolated to retrieval step"
  - "MRR and NDCG never computed — only retrieval latency tracked"
---

## Why This Happens

RAG quality is a product of retrieval quality and generation quality. Measuring only end-to-end response quality conflates the two: a good LLM can compensate for mediocre retrieval by reasoning from partial evidence, making retrieval problems invisible until they become severe. Retrieval precision and recall require ground truth — either labeled evaluation sets (offline) or implicit signals from agent behavior (online). Online signals include whether retrieved documents are cited in the response, whether the user rated the response positively after retrieval, and whether the agent requested additional retrieval (indicating the first retrieval was insufficient).

## Solution 1: Retrieval Evaluation Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RetrievalSignalSource(str, Enum):
    DOCUMENT_CITED = "document_cited"          # doc appeared in LLM response
    DOCUMENT_NOT_CITED = "document_not_cited"  # doc retrieved but not used
    USER_POSITIVE = "user_positive"            # user rated response positively
    USER_NEGATIVE = "user_negative"
    FOLLOW_UP_RETRIEVAL = "follow_up_retrieval"  # agent needed more docs
    LABELED_RELEVANT = "labeled_relevant"       # from offline eval set
    LABELED_IRRELEVANT = "labeled_irrelevant"


@dataclass
class RetrievedDocumentRecord:
    doc_id: str
    retrieval_rank: int          # 1-based position in result list
    retrieval_score: float
    reranking_score: Optional[float] = None
    cited_in_response: Optional[bool] = None
    signal: Optional[RetrievalSignalSource] = None


@dataclass
class RetrievalEvaluationRecord:
    query_id: str
    session_id: str
    query_text: str
    retrieved_docs: List[RetrievedDocumentRecord]
    total_candidates: int
    retrieval_latency_ms: float
    index_version: str = ""
    model_id: str = ""
    user_signal: Optional[RetrievalSignalSource] = None
    recorded_at: float = field(default_factory=time.time)
```

## Solution 2: Document Citation Extractor

```python
import re
from typing import List, Set


class DocumentCitationExtractor:
    """
    Determines which retrieved document IDs appear to be used in the
    LLM response. Uses heuristics: direct ID mentions, content overlap,
    and explicit citation markers like [1], [doc_id], or footnote refs.
    """

    CITATION_MARKER = re.compile(r"\[(\w[\w\-]*)\]|\[(\d+)\]|\(Source:\s*([^\)]+)\)")

    @classmethod
    def extract_cited_ids(
        cls,
        response_text: str,
        retrieved_doc_ids: List[str],
    ) -> Set[str]:
        cited: Set[str] = set()

        # Direct ID mentions
        for doc_id in retrieved_doc_ids:
            if doc_id in response_text:
                cited.add(doc_id)

        # Citation marker patterns
        for match in cls.CITATION_MARKER.finditer(response_text):
            ref = next(g for g in match.groups() if g is not None)
            # Check if ref matches or is an index into retrieved docs
            if ref in retrieved_doc_ids:
                cited.add(ref)
            elif ref.isdigit():
                idx = int(ref) - 1
                if 0 <= idx < len(retrieved_doc_ids):
                    cited.add(retrieved_doc_ids[idx])

        return cited

    @classmethod
    def compute_citation_fraction(
        cls,
        response_text: str,
        retrieved_doc_ids: List[str],
    ) -> float:
        if not retrieved_doc_ids:
            return 0.0
        cited = cls.extract_cited_ids(response_text, retrieved_doc_ids)
        return round(len(cited) / len(retrieved_doc_ids), 4)
```

## Solution 3: Retrieval Metrics Calculator

```python
import math
from typing import Dict, List, Optional, Set


class RetrievalMetricsCalculator:
    """
    Computes standard IR metrics from retrieval records.
    Supports precision@K, recall@K, MRR, and NDCG@K.
    Uses citation signals as a proxy for relevance when labels unavailable.
    """

    @staticmethod
    def precision_at_k(
        retrieved_docs: List[RetrievedDocumentRecord],
        k: int,
        relevant_doc_ids: Optional[Set[str]] = None,
    ) -> float:
        top_k = retrieved_docs[:k]
        if not top_k:
            return 0.0
        if relevant_doc_ids is not None:
            relevant = sum(1 for d in top_k if d.doc_id in relevant_doc_ids)
        else:
            relevant = sum(1 for d in top_k if d.cited_in_response is True)
        return round(relevant / len(top_k), 4)

    @staticmethod
    def recall_at_k(
        retrieved_docs: List[RetrievedDocumentRecord],
        k: int,
        total_relevant: int,
        relevant_doc_ids: Optional[Set[str]] = None,
    ) -> float:
        if total_relevant == 0:
            return 0.0
        top_k = retrieved_docs[:k]
        if relevant_doc_ids is not None:
            retrieved_relevant = sum(1 for d in top_k if d.doc_id in relevant_doc_ids)
        else:
            retrieved_relevant = sum(1 for d in top_k if d.cited_in_response is True)
        return round(retrieved_relevant / total_relevant, 4)

    @staticmethod
    def mrr(
        retrieved_docs: List[RetrievedDocumentRecord],
        relevant_doc_ids: Optional[Set[str]] = None,
    ) -> float:
        for i, doc in enumerate(retrieved_docs):
            is_relevant = (
                doc.doc_id in relevant_doc_ids
                if relevant_doc_ids is not None
                else doc.cited_in_response is True
            )
            if is_relevant:
                return round(1.0 / (i + 1), 4)
        return 0.0

    @staticmethod
    def ndcg_at_k(
        retrieved_docs: List[RetrievedDocumentRecord],
        k: int,
        relevant_doc_ids: Optional[Set[str]] = None,
    ) -> float:
        top_k = retrieved_docs[:k]
        if not top_k:
            return 0.0

        def relevance(doc: RetrievedDocumentRecord) -> float:
            if relevant_doc_ids is not None:
                return 1.0 if doc.doc_id in relevant_doc_ids else 0.0
            return 1.0 if doc.cited_in_response is True else 0.0

        dcg = sum(
            relevance(doc) / math.log2(i + 2)
            for i, doc in enumerate(top_k)
        )
        ideal_relevances = sorted([relevance(d) for d in top_k], reverse=True)
        idcg = sum(
            rel / math.log2(i + 2)
            for i, rel in enumerate(ideal_relevances)
        )
        return round(dcg / max(idcg, 1e-10), 4)
```

## Solution 4: Retrieval Quality Store

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional


class RetrievalQualityStore:
    """
    Accumulates retrieval evaluation records and computes aggregate metrics.
    """

    def __init__(
        self,
        calculator: RetrievalMetricsCalculator,
        max_records: int = 50000,
    ):
        self._calc = calculator
        self._records: Deque[RetrievalEvaluationRecord] = deque(maxlen=max_records)
        self._lock = Lock()

    def record(self, record: RetrievalEvaluationRecord) -> None:
        with self._lock:
            self._records.append(record)

    def metrics_summary(self, window_seconds: float = 3600.0, k: int = 5) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.recorded_at >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "queries": 0}

        p_at_k = [self._calc.precision_at_k(r.retrieved_docs, k) for r in recent]
        mrr_vals = [self._calc.mrr(r.retrieved_docs) for r in recent]
        ndcg_vals = [self._calc.ndcg_at_k(r.retrieved_docs, k) for r in recent]
        citation_fracs = [
            sum(1 for d in r.retrieved_docs if d.cited_in_response is True) / max(len(r.retrieved_docs), 1)
            for r in recent
        ]
        positive_signals = sum(1 for r in recent if r.user_signal == RetrievalSignalSource.USER_POSITIVE)
        negative_signals = sum(1 for r in recent if r.user_signal == RetrievalSignalSource.USER_NEGATIVE)

        def mean(lst):
            return round(sum(lst) / max(len(lst), 1), 4)

        return {
            "window_seconds": window_seconds,
            "queries": len(recent),
            f"precision_at_{k}": mean(p_at_k),
            "mrr": mean(mrr_vals),
            f"ndcg_at_{k}": mean(ndcg_vals),
            "mean_citation_fraction": mean(citation_fracs),
            "positive_feedback": positive_signals,
            "negative_feedback": negative_signals,
        }
```

## Solution 5: Retrieval Quality Alert Manager

```python
import time
from typing import Callable, Dict, List


class RetrievalQualityAlertManager:
    """
    Fires alerts when retrieval quality metrics drop below thresholds.
    """

    def __init__(
        self,
        store: RetrievalQualityStore,
        alert_fn: Callable[[dict], None],
        mrr_threshold: float = 0.50,
        precision_threshold: float = 0.40,
        cooldown_s: float = 1800.0,
        k: int = 5,
    ):
        self._store = store
        self._alert_fn = alert_fn
        self._mrr_threshold = mrr_threshold
        self._precision_threshold = precision_threshold
        self._cooldown = cooldown_s
        self._k = k
        self._last_alerts: Dict[str, float] = {}

    def check(self) -> List[dict]:
        summary = self._store.metrics_summary(window_seconds=3600.0, k=self._k)
        if summary.get("queries", 0) < 20:
            return []

        fired = []
        now = time.time()
        checks = [
            ("mrr", summary.get("mrr", 1.0), self._mrr_threshold),
            (f"precision_at_{self._k}", summary.get(f"precision_at_{self._k}", 1.0), self._precision_threshold),
        ]
        for metric_name, value, threshold in checks:
            if value < threshold:
                last = self._last_alerts.get(metric_name, 0.0)
                if now - last < self._cooldown:
                    continue
                self._last_alerts[metric_name] = now
                alert = {
                    "event": "retrieval_quality_degradation",
                    "metric": metric_name,
                    "value": value,
                    "threshold": threshold,
                    "ts": now,
                }
                try:
                    self._alert_fn(alert)
                except Exception:
                    pass
                fired.append(alert)
        return fired
```

## Solution 6: Retrieval Quality Dashboard

```python
import time


class RetrievalQualityDashboard:
    """
    Combines metrics summary, quality trends, and alert history.
    """

    def __init__(
        self,
        store: RetrievalQualityStore,
        alert_manager: RetrievalQualityAlertManager,
    ):
        self._store = store
        self._alert_manager = alert_manager

    def render(self, k: int = 5) -> dict:
        return {
            "generated_at": time.time(),
            "last_hour": self._store.metrics_summary(window_seconds=3600.0, k=k),
            "last_24h": self._store.metrics_summary(window_seconds=86400.0, k=k),
            "recent_alerts": [
                {"metric": m, "last_fired_ago_s": round(time.time() - ts, 0)}
                for m, ts in self._alert_manager._last_alerts.items()
            ],
        }
```

## Comparison

| Approach | Citation Extraction | P@K / MRR / NDCG | User Signals | Alert Firing | Trend Comparison |
|---|---|---|---|---|---|
| DocumentCitationExtractor | Yes (regex + ID match) | No | No | No | No |
| RetrievalMetricsCalculator | No | Yes (all four) | No | No | No |
| RetrievalQualityStore | Via calculator | Via calculator | Yes | No | Yes (windows) |
| RetrievalQualityAlertManager | No | Via store | Via store | Yes | No |
| RetrievalQualityDashboard | No | No | No | Via manager | Yes |

**Best for production**: Start with citation-based implicit relevance (document cited in response = relevant) before building an expensive labeled eval set — citation fraction provides a useful proxy signal with zero labeling cost. Track MRR as the primary retrieval SLO metric (target ≥ 0.6 for most RAG applications) and alert when it drops below 0.5 over the past hour. Compare metrics before and after index updates using `RetrievalQualityStore.metrics_summary()` with different time windows. If MRR drops sharply after an index change, roll back the index rather than the LLM — retrieval is almost always the root cause of sudden quality regressions.
