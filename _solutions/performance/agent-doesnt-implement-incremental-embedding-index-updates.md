---
title: "Agent Doesn't Implement Incremental Embedding Index Updates"
description: "Agents that rebuild their entire embedding index from scratch whenever new documents are added pay a cost proportional to the full corpus size on every update — a 100,000-document index rebuilt for each new addition wastes 99,999 embeddings already computed. Implement incremental embedding index updates that compute and insert only the new document embeddings, maintain an index delta log, and periodically compact deltas into the main index without full recomputation."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-incremental-embedding-index-updates
tags: [embedding-index, incremental-update, vector-search, index-maintenance, rag-optimization, delta-update]
symptoms:
  - "Full embedding index rebuild triggered every time a document is added or updated"
  - "Index update latency scales linearly with corpus size — unusable at 100k+ documents"
  - "Embedding API costs spike on every document ingestion due to full recomputation"
  - "New documents are not searchable until the full rebuild completes"
  - "No distinction between already-embedded and new documents during ingestion"
---

## Why This Happens

Vector indexes are typically built as batch operations: embed all documents, build the index structure, serialize. This model works for initial construction but is inefficient for maintenance — adding a single document should not require re-embedding the entire corpus. Incremental updates require tracking which documents have already been embedded (via a document registry with content hashes), computing embeddings only for new or changed documents, inserting them into the live index, and periodically merging accumulated deltas to maintain search quality without disrupting queries.

## Solution 1: Document Embedding Record

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class DocumentEmbeddingRecord:
    doc_id: str
    content_hash: str        # SHA-256 of document content
    embedded_at: float
    embedding_model: str
    vector_dim: int
    chunk_count: int = 1
    metadata: dict = field(default_factory=dict)

    @classmethod
    def compute_hash(cls, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()
```

## Solution 2: Embedding Registry

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Set


class EmbeddingRegistry:
    """
    Tracks which documents have been embedded and their content hashes.
    On incremental update, only documents with changed or absent hashes
    are sent to the embedding API.
    """

    def __init__(self, registry_path: str = "/tmp/embedding_registry.json"):
        self._path = Path(registry_path)
        self._lock = Lock()
        self._records: Dict[str, DocumentEmbeddingRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for doc_id, entry in data.items():
                self._records[doc_id] = DocumentEmbeddingRecord(**entry)
        except (json.JSONDecodeError, OSError, TypeError):
            pass

    def _save(self) -> None:
        records_dict = {
            doc_id: {
                "doc_id": r.doc_id,
                "content_hash": r.content_hash,
                "embedded_at": r.embedded_at,
                "embedding_model": r.embedding_model,
                "vector_dim": r.vector_dim,
                "chunk_count": r.chunk_count,
                "metadata": r.metadata,
            }
            for doc_id, r in self._records.items()
        }
        self._path.write_text(json.dumps(records_dict, indent=2))

    def needs_embedding(self, doc_id: str, content: str) -> bool:
        current_hash = DocumentEmbeddingRecord.compute_hash(content)
        existing = self._records.get(doc_id)
        return existing is None or existing.content_hash != current_hash

    def filter_new_or_changed(
        self, documents: Dict[str, str]  # doc_id -> content
    ) -> Dict[str, str]:
        return {
            doc_id: content
            for doc_id, content in documents.items()
            if self.needs_embedding(doc_id, content)
        }

    def record(self, rec: DocumentEmbeddingRecord) -> None:
        with self._lock:
            self._records[rec.doc_id] = rec
            self._save()

    def remove(self, doc_id: str) -> bool:
        with self._lock:
            if doc_id in self._records:
                del self._records[doc_id]
                self._save()
                return True
            return False

    def stats(self) -> dict:
        with self._lock:
            return {
                "total_indexed": len(self._records),
                "registry_path": str(self._path),
            }
```

## Solution 3: Incremental Index Updater

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


class IncrementalIndexUpdater:
    """
    Computes embeddings only for new or changed documents and
    inserts them into the live index. Tracks a delta log of
    pending insertions for compaction scheduling.
    """

    def __init__(
        self,
        registry: EmbeddingRegistry,
        embed_fn: Callable,         # async fn(texts: List[str]) -> List[List[float]]
        index_insert_fn: Callable,  # fn(doc_id: str, vector: List[float]) -> None
        embedding_model: str = "text-embedding-3-small",
        batch_size: int = 50,
    ):
        self._registry = registry
        self._embed = embed_fn
        self._insert = index_insert_fn
        self._model = embedding_model
        self._batch_size = batch_size
        self._delta_log: List[str] = []  # doc_ids added since last compaction

    async def update(
        self,
        documents: Dict[str, str],   # doc_id -> content
    ) -> dict:
        start = time.time()
        new_or_changed = self._registry.filter_new_or_changed(documents)

        if not new_or_changed:
            return {"new_embeddings": 0, "skipped": len(documents), "elapsed_ms": 0}

        # Batch embed only new/changed documents
        doc_ids = list(new_or_changed.keys())
        contents = list(new_or_changed.values())
        total_embedded = 0

        for i in range(0, len(doc_ids), self._batch_size):
            batch_ids = doc_ids[i: i + self._batch_size]
            batch_texts = contents[i: i + self._batch_size]

            vectors = await self._embed(batch_texts)

            for doc_id, vector, content in zip(batch_ids, vectors, batch_texts):
                self._insert(doc_id, vector)
                rec = DocumentEmbeddingRecord(
                    doc_id=doc_id,
                    content_hash=DocumentEmbeddingRecord.compute_hash(content),
                    embedded_at=time.time(),
                    embedding_model=self._model,
                    vector_dim=len(vector),
                )
                self._registry.record(rec)
                self._delta_log.append(doc_id)
                total_embedded += 1

        elapsed = round((time.time() - start) * 1000, 2)
        return {
            "new_embeddings": total_embedded,
            "skipped": len(documents) - total_embedded,
            "elapsed_ms": elapsed,
            "delta_log_size": len(self._delta_log),
        }

    def delta_log_size(self) -> int:
        return len(self._delta_log)

    def clear_delta_log(self) -> None:
        self._delta_log.clear()
```

## Solution 4: Index Compaction Scheduler

```python
import asyncio
import time
from typing import Callable, Optional


class IndexCompactionScheduler:
    """
    Triggers index compaction when the delta log exceeds a size threshold
    or a time interval elapses. Compaction merges delta insertions into
    the main index structure for optimal search performance.
    """

    def __init__(
        self,
        updater: IncrementalIndexUpdater,
        compaction_fn: Callable,          # async fn() -> None — rebuilds index from registry
        delta_threshold: int = 1000,
        time_threshold_seconds: float = 3600.0,
    ):
        self._updater = updater
        self._compact = compaction_fn
        self._delta_threshold = delta_threshold
        self._time_threshold = time_threshold_seconds
        self._last_compaction = time.time()
        self._compaction_count = 0

    def should_compact(self) -> bool:
        delta_trigger = self._updater.delta_log_size() >= self._delta_threshold
        time_trigger = time.time() - self._last_compaction >= self._time_threshold
        return delta_trigger or time_trigger

    async def compact_if_needed(self) -> Optional[dict]:
        if not self.should_compact():
            return None
        start = time.time()
        await self._compact()
        self._updater.clear_delta_log()
        self._last_compaction = time.time()
        self._compaction_count += 1
        return {
            "compaction_number": self._compaction_count,
            "elapsed_ms": round((time.time() - start) * 1000, 2),
        }

    def stats(self) -> dict:
        return {
            "compaction_count": self._compaction_count,
            "last_compaction_ago_seconds": round(
                time.time() - self._last_compaction, 1
            ),
            "current_delta_size": self._updater.delta_log_size(),
            "delta_threshold": self._delta_threshold,
        }
```

## Solution 5: Update Cost Tracker

```python
import time
from typing import List


class EmbeddingUpdateCostTracker:
    """
    Records the number of embeddings computed versus skipped per update run,
    quantifying the savings from incremental updates over full rebuilds.
    """

    def __init__(self, cost_per_1k_tokens: float = 0.00002):
        self._cost_per_1k = cost_per_1k_tokens
        self._runs: List[dict] = []
        self._recorded_at: List[float] = []

    def record(self, update_result: dict, avg_tokens_per_doc: int = 500) -> None:
        new_emb = update_result.get("new_embeddings", 0)
        skipped = update_result.get("skipped", 0)
        tokens_computed = new_emb * avg_tokens_per_doc
        tokens_saved = skipped * avg_tokens_per_doc
        self._runs.append({
            "new_embeddings": new_emb,
            "skipped": skipped,
            "tokens_computed": tokens_computed,
            "tokens_saved": tokens_saved,
            "cost_usd": round(tokens_computed / 1000 * self._cost_per_1k, 6),
            "cost_saved_usd": round(tokens_saved / 1000 * self._cost_per_1k, 6),
        })
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._runs, self._recorded_at) if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "runs": 0}
        return {
            "window_seconds": window_seconds,
            "runs": len(recent),
            "total_new_embeddings": sum(r["new_embeddings"] for r in recent),
            "total_skipped": sum(r["skipped"] for r in recent),
            "total_cost_usd": round(sum(r["cost_usd"] for r in recent), 4),
            "total_cost_saved_usd": round(sum(r["cost_saved_usd"] for r in recent), 4),
        }
```

## Solution 6: Incremental Index Dashboard

```python
import time


class IncrementalEmbeddingIndexDashboard:
    """
    Combines registry stats, delta log status, compaction schedule,
    and cost tracking into a single index health view.
    """

    def __init__(
        self,
        registry: EmbeddingRegistry,
        compaction_scheduler: IndexCompactionScheduler,
        cost_tracker: EmbeddingUpdateCostTracker,
    ):
        self._registry = registry
        self._scheduler = compaction_scheduler
        self._cost = cost_tracker

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "registry": self._registry.stats(),
            "compaction": self._scheduler.stats(),
            "compaction_needed": self._scheduler.should_compact(),
            "cost_24h": self._cost.summary(86400.0),
        }
```

## Comparison

| Approach | Change Detection | Incremental Embed | Batch Embedding | Compaction | Cost Tracking |
|---|---|---|---|---|---|
| EmbeddingRegistry | Yes (SHA-256) | No | No | No | No |
| IncrementalIndexUpdater | Via registry | Yes | Yes | No (logs) | No |
| IndexCompactionScheduler | No | No | No | Yes | No |
| EmbeddingUpdateCostTracker | No | No | No | No | Yes |
| IncrementalEmbeddingIndexDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Persist `EmbeddingRegistry` to Redis or a database rather than a local file — container restarts will re-embed the entire corpus if the registry is lost. Set `delta_threshold=500` and `time_threshold_seconds=3600` for compaction: this ensures the index is compacted at most once per hour or every 500 new documents, whichever comes first, balancing search quality against compaction overhead. Use content hashes rather than modification timestamps to detect changes — timestamps can be unreliable across file system copies or ETL pipelines that touch mtime without changing content. Monitor `total_cost_saved_usd` in `EmbeddingUpdateCostTracker`: at 100k documents with 90% skip rate, incremental updates typically reduce embedding API costs by 10-50× compared to full rebuilds.
