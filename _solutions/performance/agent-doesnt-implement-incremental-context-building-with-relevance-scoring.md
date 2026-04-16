---
title: "Agent Doesn't Implement Incremental Context Building with Relevance Scoring"
description: "Agents that inject all retrieved tool results into the context regardless of relevance fill the context window with low-signal content and crowd out high-relevance material. When five documents are retrieved but only two are relevant to the current query, all five consume context tokens equally. Implement incremental context building that scores each candidate for relevance to the current query, ranks them, and adds items greedily until the context budget is exhausted — ensuring the most relevant content is always included."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-incremental-context-building-with-relevance-scoring
tags: [context-building, relevance-scoring, greedy-selection, token-budget, rag-optimization, context-prioritization]
symptoms:
  - "Retrieved documents injected in retrieval order regardless of relevance to the query"
  - "Irrelevant tool results fill the context and push out relevant content"
  - "Context window fills with 5 mediocre documents when 2 high-quality ones would suffice"
  - "No ranking of candidates before context injection"
  - "Token budget exhausted before the most relevant document is added"
---

## Why This Happens

RAG pipelines retrieve a fixed number of documents and inject them in order. Retrieval score is often ignored after retrieval: the top-5 documents are treated equally even if document 1 has a similarity of 0.95 and document 5 has 0.62. Incremental context building requires a per-candidate relevance score, a greedy selection loop that picks the highest-scoring candidate that fits within the remaining budget, and a minimum score threshold below which candidates are excluded regardless of available space.

## Solution 1: Context Candidate

```python
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ContextCandidate:
    content: str
    source: str                      # tool name, document ID, etc.
    relevance_score: float           # 0.0–1.0
    token_estimate: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.token_estimate == 0:
            self.token_estimate = max(1, int(len(self.content) / 4.0))
```

## Solution 2: Relevance Scorer

```python
import math
import re
from typing import List, Set


class BM25RelevanceScorer:
    """
    Lightweight BM25-inspired relevance scorer using term frequency
    and document frequency. No external dependencies required.
    Suitable for re-ranking a small set of retrieved candidates.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self._k1 = k1
        self._b = b

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r"\b[a-zA-Z0-9]{2,}\b", text.lower())

    def score(self, query: str, documents: List[ContextCandidate]) -> List[ContextCandidate]:
        query_terms = set(self._tokenize(query))
        if not query_terms:
            return documents

        doc_tokens = [self._tokenize(d.content) for d in documents]
        avg_len = sum(len(t) for t in doc_tokens) / max(len(doc_tokens), 1)

        df: dict = {}
        for tokens in doc_tokens:
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1

        n = len(documents)
        scored = []

        for doc, tokens in zip(documents, doc_tokens):
            tf_map: dict = {}
            for t in tokens:
                tf_map[t] = tf_map.get(t, 0) + 1

            score = 0.0
            dl = len(tokens)
            for term in query_terms:
                if term not in tf_map:
                    continue
                tf = tf_map[term]
                idf = math.log((n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1)
                numerator = tf * (self._k1 + 1)
                denominator = tf + self._k1 * (1 - self._b + self._b * dl / max(avg_len, 1))
                score += idf * numerator / denominator

            # Normalize to 0–1 using retrieval score as a prior
            bm25_normalized = min(1.0, score / max(len(query_terms) * 3, 1))
            blended = 0.6 * doc.relevance_score + 0.4 * bm25_normalized
            new_doc = ContextCandidate(
                content=doc.content,
                source=doc.source,
                relevance_score=round(blended, 4),
                token_estimate=doc.token_estimate,
                metadata=doc.metadata,
            )
            scored.append(new_doc)

        return sorted(scored, key=lambda d: -d.relevance_score)
```

## Solution 3: Greedy Context Selector

```python
from typing import List, Tuple


class GreedyContextSelector:
    """
    Selects context candidates greedily by relevance score until
    the token budget is exhausted or the minimum score threshold
    is not met. Always includes mandatory items first.
    """

    def __init__(
        self,
        min_relevance_score: float = 0.40,
        min_items: int = 1,
    ):
        self._min_score = min_relevance_score
        self._min_items = min_items

    def select(
        self,
        candidates: List[ContextCandidate],
        token_budget: int,
    ) -> Tuple[List[ContextCandidate], dict]:
        sorted_candidates = sorted(candidates, key=lambda c: -c.relevance_score)
        selected = []
        remaining_budget = token_budget
        skipped_low_score = 0
        skipped_no_budget = 0

        for i, candidate in enumerate(sorted_candidates):
            if candidate.token_estimate > remaining_budget:
                skipped_no_budget += 1
                continue
            if candidate.relevance_score < self._min_score and len(selected) >= self._min_items:
                skipped_low_score += 1
                continue
            selected.append(candidate)
            remaining_budget -= candidate.token_estimate

        return selected, {
            "selected": len(selected),
            "skipped_low_score": skipped_low_score,
            "skipped_no_budget": skipped_no_budget,
            "tokens_used": token_budget - remaining_budget,
            "tokens_remaining": remaining_budget,
            "avg_relevance": round(
                sum(c.relevance_score for c in selected) / max(len(selected), 1), 4
            ),
        }
```

## Solution 4: Incremental Context Builder

```python
from typing import Any, List, Optional


class IncrementalContextBuilder:
    """
    Orchestrates relevance scoring, greedy selection, and context
    assembly for RAG pipelines. Produces a final context string
    and a full selection report.
    """

    def __init__(
        self,
        scorer: BM25RelevanceScorer,
        selector: GreedyContextSelector,
        section_separator: str = "\n\n---\n\n",
    ):
        self._scorer = scorer
        self._selector = selector
        self._separator = section_separator

    def build(
        self,
        query: str,
        candidates: List[ContextCandidate],
        token_budget: int,
    ) -> dict:
        if not candidates:
            return {"context": "", "selection_report": {}, "selected_count": 0}

        # Step 1: Re-rank by relevance to query
        ranked = self._scorer.score(query, candidates)

        # Step 2: Greedy token-budget selection
        selected, report = self._selector.select(ranked, token_budget)

        # Step 3: Assemble context
        context_parts = [
            f"[Source: {c.source} | Relevance: {c.relevance_score:.2f}]\n{c.content}"
            for c in selected
        ]
        context = self._separator.join(context_parts)

        return {
            "context": context,
            "selected_count": len(selected),
            "selection_report": report,
            "selected_sources": [c.source for c in selected],
            "all_scores": [
                {"source": c.source, "score": c.relevance_score}
                for c in ranked
            ],
        }
```

## Solution 5: Context Quality Monitor

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class ContextQualityMonitor:
    """
    Tracks selection quality metrics over time to detect
    relevance score drift or budget pressure patterns.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, build_result: dict) -> None:
        report = build_result.get("selection_report", {})
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "selected": build_result.get("selected_count", 0),
                "avg_relevance": report.get("avg_relevance", 0.0),
                "skipped_low_score": report.get("skipped_low_score", 0),
                "skipped_no_budget": report.get("skipped_no_budget", 0),
            })

    def summary(self) -> dict:
        cutoff = time.time() - self._window
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"requests": 0}

        return {
            "requests": len(recent),
            "avg_selected_items": round(
                sum(r["selected"] for r in recent) / len(recent), 2
            ),
            "avg_relevance_score": round(
                sum(r["avg_relevance"] for r in recent) / len(recent), 4
            ),
            "budget_pressure_rate": round(
                sum(1 for r in recent if r["skipped_no_budget"] > 0) / len(recent), 4
            ),
            "low_relevance_skip_rate": round(
                sum(1 for r in recent if r["skipped_low_score"] > 0) / len(recent), 4
            ),
        }
```

## Solution 6: Context Building Dashboard

```python
import time


class IncrementalContextBuildingDashboard:
    """
    Renders selection quality, relevance trends, and budget pressure
    for optimization decisions.
    """

    def __init__(
        self,
        builder: IncrementalContextBuilder,
        monitor: ContextQualityMonitor,
    ):
        self._builder = builder
        self._monitor = monitor

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "config": {
                "min_relevance_score": self._builder._selector._min_score,
                "min_items": self._builder._selector._min_items,
            },
            "quality_summary_1h": self._monitor.summary(),
        }
```

## Comparison

| Approach | Relevance Scoring | Greedy Selection | Token Budget | Quality Monitoring | Dashboard |
|---|---|---|---|---|---|
| BM25RelevanceScorer | Yes (BM25 + blend) | No | No | No | No |
| GreedyContextSelector | Via scores | Yes | Yes | No | No |
| IncrementalContextBuilder | Via scorer | Via selector | Via selector | No | No |
| ContextQualityMonitor | No | No | No | Yes | No |
| IncrementalContextBuildingDashboard | No | No | No | Via monitor | Yes |

**Best for production**: Set `min_relevance_score=0.40` and `min_items=1` — always include at least one document even if its relevance is below threshold, so the agent has some grounding. Blend the retrieval score (vector similarity from the RAG pipeline) at 0.6 weight with the BM25 re-rank at 0.4 weight: the retrieval score captures semantic similarity, BM25 captures keyword overlap, and together they reduce false positives from either alone. Monitor `budget_pressure_rate` — when above 0.20 it means the context budget is too small for the number of retrieved candidates and either the budget or the retrieval count should be adjusted.
