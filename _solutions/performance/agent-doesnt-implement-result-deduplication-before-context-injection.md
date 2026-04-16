---
title: "Agent Doesn't Implement Result Deduplication Before Context Injection"
description: "Agents that inject all tool results into the LLM context without deduplication waste tokens on redundant content: the same document retrieved from three different search queries, the same company record returned by two lookup tools, the same news article from overlapping date ranges. Implement result deduplication that detects near-duplicate content before context injection, keeps only the highest-quality copy, and reports token savings."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-result-deduplication-before-context-injection
tags: [deduplication, context-injection, token-efficiency, near-duplicate, result-filtering, rag-optimization]
symptoms:
  - "Same document appears twice in context from different retrieval queries"
  - "Context window fills up with redundant content before all tools have contributed"
  - "LLM produces repetitive answers because it sees the same fact stated three times"
  - "No measurement of how much context space is consumed by duplicate content"
  - "Tool results are injected in arrival order with no content-based overlap check"
---

## Why This Happens

Multiple tool calls often return overlapping content: a web search and a knowledge-base lookup may both return the same article; two date-range queries may retrieve the same document. Without deduplication, every returned item is injected into the context. The LLM receives the same fact multiple times, consuming tokens and sometimes producing answers that echo the repetition. Deduplication requires a content fingerprint (exact hash for identical content, MinHash or cosine similarity for near-duplicates) and a keep/drop decision based on quality scores when duplicates are found.

## Solution 1: Result Item

```python
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ResultItem:
    content: str
    source_tool: str
    item_id: str = ""
    quality_score: float = 1.0       # higher = prefer this copy
    token_count: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.item_id:
            import hashlib
            self.item_id = hashlib.sha256(self.content.encode()).hexdigest()[:16]
```

## Solution 2: Exact Duplicate Detector

```python
import hashlib
from typing import Dict, List, Tuple


class ExactDuplicateDetector:
    """
    Detects byte-identical result items using SHA-256 content hashes.
    When duplicates are found, keeps the item with the highest quality_score.
    """

    def deduplicate(self, items: List[ResultItem]) -> Tuple[List[ResultItem], int]:
        """
        Returns (deduplicated_items, dropped_count).
        """
        seen: Dict[str, ResultItem] = {}
        for item in items:
            key = hashlib.sha256(item.content.encode()).hexdigest()
            if key not in seen:
                seen[key] = item
            else:
                existing = seen[key]
                if item.quality_score > existing.quality_score:
                    seen[key] = item

        deduped = list(seen.values())
        dropped = len(items) - len(deduped)
        return deduped, dropped
```

## Solution 3: Near-Duplicate Detector

```python
import re
from typing import Dict, List, Set, Tuple


class ShingleNearDuplicateDetector:
    """
    Uses character-level shingles and Jaccard similarity to detect
    near-duplicate content. Two items are considered near-duplicates
    if their Jaccard similarity exceeds the threshold.
    """

    def __init__(self, shingle_size: int = 5, similarity_threshold: float = 0.80):
        self._k = shingle_size
        self._threshold = similarity_threshold

    def _shingles(self, text: str) -> Set[str]:
        normalized = re.sub(r"\s+", " ", text.lower().strip())
        if len(normalized) < self._k:
            return {normalized}
        return {normalized[i:i + self._k] for i in range(len(normalized) - self._k + 1)}

    def _jaccard(self, a: Set[str], b: Set[str]) -> float:
        if not a and not b:
            return 1.0
        intersection = len(a & b)
        union = len(a | b)
        return intersection / union if union else 0.0

    def deduplicate(
        self, items: List[ResultItem]
    ) -> Tuple[List[ResultItem], List[Tuple[str, str, float]]]:
        """
        Returns (deduplicated_items, list_of_(kept_id, dropped_id, similarity)).
        Keeps the item with the higher quality_score when near-duplicates are found.
        """
        shingles = [(item, self._shingles(item.content)) for item in items]
        dropped_ids: Set[str] = set()
        duplicate_log: List[Tuple[str, str, float]] = []

        for i in range(len(shingles)):
            if shingles[i][0].item_id in dropped_ids:
                continue
            for j in range(i + 1, len(shingles)):
                if shingles[j][0].item_id in dropped_ids:
                    continue
                sim = self._jaccard(shingles[i][1], shingles[j][1])
                if sim >= self._threshold:
                    item_i, item_j = shingles[i][0], shingles[j][0]
                    if item_i.quality_score >= item_j.quality_score:
                        kept, dropped = item_i, item_j
                    else:
                        kept, dropped = item_j, item_i
                    dropped_ids.add(dropped.item_id)
                    duplicate_log.append((kept.item_id, dropped.item_id, round(sim, 4)))

        deduped = [item for item, _ in shingles if item.item_id not in dropped_ids]
        return deduped, duplicate_log
```

## Solution 4: Deduplication Pipeline

```python
from typing import List, Optional


class DeduplicationPipeline:
    """
    Runs exact deduplication first (cheap), then near-duplicate
    detection (more expensive) on the survivors.
    Reports total tokens saved.
    """

    def __init__(
        self,
        exact_detector: ExactDuplicateDetector,
        near_detector: Optional[ShingleNearDuplicateDetector] = None,
        tokens_per_char: float = 0.25,   # rough estimate for token counting
    ):
        self._exact = exact_detector
        self._near = near_detector
        self._tokens_per_char = tokens_per_char

    def _estimate_tokens(self, items: List[ResultItem]) -> int:
        return int(sum(
            item.token_count if item.token_count is not None
            else len(item.content) * self._tokens_per_char
            for item in items
        ))

    def run(self, items: List[ResultItem]) -> dict:
        original_count = len(items)
        original_tokens = self._estimate_tokens(items)

        # Stage 1: exact
        after_exact, exact_dropped = self._exact.deduplicate(items)

        # Stage 2: near-duplicate
        near_dropped_count = 0
        near_log = []
        if self._near is not None:
            after_near, near_log = self._near.deduplicate(after_exact)
            near_dropped_count = len(after_exact) - len(after_near)
            final_items = after_near
        else:
            final_items = after_exact

        final_tokens = self._estimate_tokens(final_items)
        tokens_saved = original_tokens - final_tokens

        return {
            "items": final_items,
            "original_count": original_count,
            "final_count": len(final_items),
            "exact_dropped": exact_dropped,
            "near_dropped": near_dropped_count,
            "total_dropped": original_count - len(final_items),
            "original_tokens_est": original_tokens,
            "final_tokens_est": final_tokens,
            "tokens_saved_est": tokens_saved,
            "near_duplicate_pairs": near_log,
        }
```

## Solution 5: Cross-Tool Deduplication Coordinator

```python
from typing import Any, Dict, List


class CrossToolDeduplicationCoordinator:
    """
    Collects result items from multiple tools and runs the deduplication
    pipeline before any content is injected into the LLM context.
    """

    def __init__(self, pipeline: DeduplicationPipeline):
        self._pipeline = pipeline
        self._pending: List[ResultItem] = []
        self._pipeline_runs = 0
        self._total_tokens_saved = 0

    def add_tool_results(
        self,
        tool_name: str,
        results: List[Any],
        quality_scores: Optional[List[float]] = None,
        content_extractor=None,
    ) -> None:
        """
        results: list of raw result objects from a tool
        content_extractor: callable(result) -> str; defaults to str()
        """
        extractor = content_extractor or str
        scores = quality_scores or [1.0] * len(results)
        for i, result in enumerate(results):
            content = extractor(result)
            score = scores[i] if i < len(scores) else 1.0
            self._pending.append(ResultItem(
                content=content,
                source_tool=tool_name,
                quality_score=score,
            ))

    def flush(self) -> dict:
        """Run deduplication on all pending items and return the report."""
        report = self._pipeline.run(self._pending)
        self._pending = []
        self._pipeline_runs += 1
        self._total_tokens_saved += report["tokens_saved_est"]
        return report

    def stats(self) -> dict:
        return {
            "pipeline_runs": self._pipeline_runs,
            "total_tokens_saved_est": self._total_tokens_saved,
        }
```

## Solution 6: Deduplication Savings Monitor

```python
import time
from typing import List


class DeduplicationSavingsMonitor:
    """
    Accumulates deduplication reports over time and surfaces
    which tool pairs produce the most duplicate content.
    """

    def __init__(self):
        self._reports: List[dict] = []
        self._recorded_at: List[float] = []

    def record(self, report: dict) -> None:
        self._reports.append(report)
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._reports, self._recorded_at)
            if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "runs": 0}

        total_saved = sum(r["tokens_saved_est"] for r in recent)
        total_original = sum(r["original_tokens_est"] for r in recent)
        total_dropped = sum(r["total_dropped"] for r in recent)
        avg_drop_rate = sum(
            r["total_dropped"] / max(r["original_count"], 1) for r in recent
        ) / len(recent)

        return {
            "window_seconds": window_seconds,
            "runs": len(recent),
            "total_tokens_saved_est": total_saved,
            "total_original_tokens_est": total_original,
            "avg_dedup_rate": round(avg_drop_rate, 4),
            "savings_pct": round(total_saved / max(total_original, 1) * 100, 1),
        }
```

## Comparison

| Approach | Exact Dedup | Near-Duplicate | Quality-Based Keep | Token Savings Report | Cross-Tool |
|---|---|---|---|---|---|
| ExactDuplicateDetector | Yes (SHA-256) | No | Yes | No | No |
| ShingleNearDuplicateDetector | No | Yes (Jaccard) | Yes | No | No |
| DeduplicationPipeline | Via exact | Via near | Yes | Yes | No |
| CrossToolDeduplicationCoordinator | Via pipeline | Via pipeline | Via pipeline | Via pipeline | Yes |
| DeduplicationSavingsMonitor | No | No | No | Yes (aggregate) | No |

**Best for production**: Run exact deduplication always — it is O(n) and eliminates the most common case of byte-identical documents returned by overlapping queries. Enable near-duplicate detection (`similarity_threshold=0.85`) for RAG pipelines where documents are chunked differently across retrievals. Set `quality_score` to the retrieval score or recency weight so the best copy is kept when duplicates are found. Monitor `savings_pct` via `DeduplicationSavingsMonitor`: consistently above 20% means retrieval diversity is low and query strategies should be diversified rather than relying on deduplication to compensate.
