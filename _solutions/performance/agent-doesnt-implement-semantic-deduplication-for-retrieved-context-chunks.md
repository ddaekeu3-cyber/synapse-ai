---
title: "Agent Doesn't Implement Semantic Deduplication for Retrieved Context Chunks"
description: "RAG agents that inject all retrieved chunks into the context window without deduplication waste tokens on near-identical passages. When multiple documents contain the same paragraph, or when overlapping chunks from a single document are retrieved, the LLM receives redundant content that consumes context budget without adding information. Implement semantic deduplication that computes chunk similarity, collapses near-duplicate passages, and reports token savings."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-semantic-deduplication-for-retrieved-context-chunks
tags: [semantic-deduplication, rag, context-chunks, embedding-similarity, token-savings, context-window]
symptoms:
  - "Retrieved context contains the same paragraph three times from different source documents"
  - "Overlapping chunks from a single document fill the context with near-identical text"
  - "Context window hits the limit before all relevant chunks are injected"
  - "No similarity check between chunks before context assembly"
  - "LLM cites the same source multiple times because it appears multiple times in context"
---

## Why This Happens

Vector search retrieves the top-K chunks by similarity to the query, but similarity to the query does not imply dissimilarity to each other. Two chunks that both match the query strongly may be nearly identical to each other — an exact duplicate, a near-paraphrase, or overlapping sliding-window chunks from the same document. Without a pairwise similarity check at assembly time, all top-K chunks are injected verbatim. Semantic deduplication adds a post-retrieval pass that computes pairwise cosine similarity between chunk embeddings and removes any chunk that is too similar to a higher-ranked chunk already selected.

## Solution 1: Context Chunk

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ContextChunk:
    chunk_id: str
    text: str
    source: str
    score: float                    # retrieval relevance score (higher = more relevant)
    embedding: Optional[List[float]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    token_estimate: int = 0

    def __post_init__(self) -> None:
        if self.token_estimate == 0:
            self.token_estimate = max(1, len(self.text) // 4)
```

## Solution 2: Cosine Similarity Calculator

```python
import math
from typing import List, Optional


class CosineSimilarityCalculator:
    """
    Computes cosine similarity between embedding vectors.
    Falls back to character-level Jaccard similarity when embeddings are unavailable.
    """

    @staticmethod
    def cosine(a: List[float], b: List[float]) -> float:
        if len(a) != len(b):
            raise ValueError(f"Embedding dimension mismatch: {len(a)} vs {len(b)}")
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return round(dot / (norm_a * norm_b), 6)

    @staticmethod
    def jaccard_text(a: str, b: str, ngram_size: int = 3) -> float:
        """Character n-gram Jaccard similarity as fallback when no embeddings."""
        def ngrams(text: str) -> set:
            text = text.lower()
            return {text[i:i + ngram_size] for i in range(len(text) - ngram_size + 1)}
        sa, sb = ngrams(a), ngrams(b)
        if not sa and not sb:
            return 1.0
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    @classmethod
    def similarity(cls, chunk_a: ContextChunk, chunk_b: ContextChunk) -> float:
        if chunk_a.embedding and chunk_b.embedding:
            return cls.cosine(chunk_a.embedding, chunk_b.embedding)
        return cls.jaccard_text(chunk_a.text, chunk_b.text)
```

## Solution 3: Semantic Deduplicator

```python
from typing import List, Tuple


class SemanticDeduplicator:
    """
    Removes near-duplicate chunks from a ranked list.
    Processes chunks in score order (highest first).
    A chunk is deduplicated if its similarity to any already-selected chunk
    exceeds similarity_threshold.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.85,
        calculator: Optional[CosineSimilarityCalculator] = None,
    ):
        self._threshold = similarity_threshold
        self._calc = calculator or CosineSimilarityCalculator()

    def deduplicate(
        self,
        chunks: List[ContextChunk],
    ) -> Tuple[List[ContextChunk], "DeduplicationReport"]:
        """
        Returns (deduplicated_chunks, report).
        Chunks must be pre-sorted by descending score.
        """
        selected: List[ContextChunk] = []
        removed: List[Tuple[ContextChunk, ContextChunk, float]] = []  # (removed, kept_cause, sim)

        for candidate in chunks:
            duplicate_of = None
            max_sim = 0.0

            for kept in selected:
                sim = self._calc.similarity(candidate, kept)
                if sim > max_sim:
                    max_sim = sim
                if sim >= self._threshold:
                    duplicate_of = kept
                    break

            if duplicate_of is None:
                selected.append(candidate)
            else:
                removed.append((candidate, duplicate_of, max_sim))

        report = DeduplicationReport(
            original_count=len(chunks),
            deduplicated_count=len(selected),
            removed_count=len(removed),
            removed_pairs=[
                {
                    "removed_id": r.chunk_id,
                    "kept_id": k.chunk_id,
                    "similarity": round(s, 4),
                    "removed_source": r.source,
                    "kept_source": k.source,
                }
                for r, k, s in removed
            ],
            tokens_saved=sum(r.token_estimate for r, _, _ in removed),
            threshold=self._threshold,
        )
        return selected, report


from dataclasses import dataclass, field as dc_field


@dataclass
class DeduplicationReport:
    original_count: int
    deduplicated_count: int
    removed_count: int
    removed_pairs: list
    tokens_saved: int
    threshold: float

    def reduction_pct(self) -> float:
        return round(self.removed_count / max(self.original_count, 1) * 100, 1)
```

## Solution 4: Token-Budget-Aware Context Assembler

```python
from typing import List, Optional, Tuple


class TokenBudgetContextAssembler:
    """
    Assembles the final context string from deduplicated chunks,
    respecting a token budget. Inserts source citations and
    reports the final token count and chunk composition.
    """

    def __init__(
        self,
        deduplicator: SemanticDeduplicator,
        token_budget: int = 4096,
        cite_sources: bool = True,
    ):
        self._deduplicator = deduplicator
        self._budget = token_budget
        self._cite = cite_sources

    def assemble(
        self,
        chunks: List[ContextChunk],
    ) -> Tuple[str, dict]:
        sorted_chunks = sorted(chunks, key=lambda c: -c.score)
        deduped, report = self._deduplicator.deduplicate(sorted_chunks)

        selected_for_context: List[ContextChunk] = []
        tokens_used = 0

        for chunk in deduped:
            if tokens_used + chunk.token_estimate > self._budget:
                break
            selected_for_context.append(chunk)
            tokens_used += chunk.token_estimate

        parts = []
        for i, chunk in enumerate(selected_for_context, 1):
            if self._cite:
                parts.append(f"[Source {i}: {chunk.source}]\n{chunk.text}")
            else:
                parts.append(chunk.text)

        context_text = "\n\n".join(parts)
        assembly_report = {
            "original_chunks": report.original_count,
            "after_deduplication": report.deduplicated_count,
            "after_budget_trim": len(selected_for_context),
            "tokens_used": tokens_used,
            "token_budget": self._budget,
            "tokens_saved_by_dedup": report.tokens_saved,
            "reduction_pct": report.reduction_pct(),
            "removed_pairs": report.removed_pairs,
        }
        return context_text, assembly_report
```

## Solution 5: Deduplication Effectiveness Tracker

```python
import time
from collections import deque
from typing import Deque, List


class DeduplicationEffectivenessTracker:
    """
    Tracks deduplication effectiveness across retrieval calls.
    Identifies whether the similarity threshold needs tuning:
    too high → near-duplicates still appear; too low → unique chunks are dropped.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._events: Deque[dict] = deque()

    def record(self, report: DeduplicationReport) -> None:
        self._events.append({
            "ts": time.time(),
            "original": report.original_count,
            "removed": report.removed_count,
            "tokens_saved": report.tokens_saved,
            "threshold": report.threshold,
        })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        while self._events and self._events[0]["ts"] < cutoff:
            self._events.popleft()

    def stats(self) -> dict:
        self._trim()
        if not self._events:
            return {"calls": 0}
        total_calls = len(self._events)
        total_original = sum(e["original"] for e in self._events)
        total_removed = sum(e["removed"] for e in self._events)
        total_saved = sum(e["tokens_saved"] for e in self._events)
        avg_removal_rate = total_removed / max(total_original, 1)

        alerts = []
        if avg_removal_rate < 0.05 and total_calls > 20:
            alerts.append({
                "type": "low_dedup_rate",
                "message": (
                    f"Only {avg_removal_rate:.1%} of chunks are being removed. "
                    "Either your corpus has little duplication or the threshold is too high."
                ),
            })
        if avg_removal_rate > 0.50 and total_calls > 20:
            alerts.append({
                "type": "high_dedup_rate",
                "message": (
                    f"{avg_removal_rate:.1%} of chunks are being removed. "
                    "Threshold may be too low — unique relevant chunks could be dropped."
                ),
            })

        return {
            "calls": total_calls,
            "avg_removal_rate": round(avg_removal_rate, 4),
            "total_tokens_saved": total_saved,
            "avg_tokens_saved_per_call": round(total_saved / total_calls, 1),
            "alerts": alerts,
        }
```

## Solution 6: Deduplication Dashboard

```python
import time


class SemanticDeduplicationDashboard:
    """
    Combines assembly stats and effectiveness tracking into one report.
    """

    def __init__(self, tracker: DeduplicationEffectivenessTracker):
        self._tracker = tracker

    def render(self) -> dict:
        stats = self._tracker.stats()
        return {
            "generated_at": time.time(),
            "effectiveness": stats,
            "healthy": len(stats.get("alerts", [])) == 0,
            "recommendations": stats.get("alerts", []),
        }
```

## Comparison

| Approach | Similarity Metric | Embedding Fallback | Budget Enforcement | Effectiveness Tracking | Dashboard |
|---|---|---|---|---|---|
| CosineSimilarityCalculator | Cosine | Jaccard n-gram | No | No | No |
| SemanticDeduplicator | Via calculator | Via calculator | No | No | No |
| TokenBudgetContextAssembler | Via deduplicator | Via deduplicator | Yes | No | No |
| DeduplicationEffectivenessTracker | No | No | No | Yes | No |
| SemanticDeduplicationDashboard | No | No | No | Via tracker | Yes |

**Best for production**: Set `similarity_threshold=0.85` as a starting point — this eliminates near-exact duplicates while keeping paraphrased content from different sources. If your retrieval returns overlapping sliding-window chunks from the same document (e.g., chunks at character offsets 0–500, 250–750, 500–1000), lower the threshold to 0.75 to catch those partial overlaps. Use the Jaccard fallback only in development; in production, store embeddings alongside chunks at index time so the cosine path is always available. Monitor `avg_removal_rate`: a healthy rate for a diverse corpus is 10–30%; above 40% suggests either the retrieval K is too large or the threshold is too aggressive.
