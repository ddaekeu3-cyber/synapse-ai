---
title: "Agent Doesn't Implement Incremental RAG Index Updates"
description: "Agents with RAG pipelines that rebuild the entire vector index whenever source documents change pay O(N) re-embedding cost for O(1) document additions — indexing thousands of unchanged documents to add one new one. Implement incremental RAG index updates that detect which documents are new, modified, or deleted and only re-embed the changed subset, reducing update latency and embedding cost proportionally to the change rate."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-incremental-rag-index-updates
tags: [rag, incremental-indexing, vector-index, embedding-efficiency, change-detection, index-maintenance]
symptoms:
  - "Adding one document to a 10K-document corpus triggers re-embedding of all 10K documents"
  - "RAG index update takes 30 minutes because it rebuilds from scratch on every run"
  - "Embedding API costs spike on each index refresh regardless of how much changed"
  - "No mechanism to detect which documents changed since the last indexing run"
  - "Index updates are batched daily because they are too expensive to run continuously"
---

## Why This Happens

Full re-indexing is the easiest implementation: clear the index and re-embed everything. It guarantees correctness but scales with total corpus size, not change size. For a 10K-document corpus with 10 daily updates, full re-indexing is 1000× more work than necessary. Incremental updates require a change detection layer: a manifest that records each document's content hash and embedding status. On each update run, new hashes are compared to the manifest, and only new or changed documents are embedded. Deleted documents are removed from the index without any embedding work.

## Solution 1: Document Manifest Entry

```python
import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class IndexStatus(str, Enum):
    PENDING = "pending"
    INDEXED = "indexed"
    DELETED = "deleted"
    FAILED = "failed"


@dataclass
class ManifestEntry:
    doc_id: str
    content_hash: str        # SHA-256 of document text
    status: IndexStatus = IndexStatus.PENDING
    indexed_at: Optional[float] = None
    chunk_ids: List[str] = field(default_factory=list)   # vector store chunk IDs
    metadata: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def hash_content(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()
```

## Solution 2: Document Change Detector

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ChangeSet:
    new_docs: List[Dict[str, Any]] = field(default_factory=list)
    modified_docs: List[Dict[str, Any]] = field(default_factory=list)
    deleted_doc_ids: List[str] = field(default_factory=list)
    unchanged_count: int = 0

    @property
    def total_changes(self) -> int:
        return len(self.new_docs) + len(self.modified_docs) + len(self.deleted_doc_ids)


class DocumentChangeDetector:
    """
    Compares a current document list against the persisted manifest
    to identify new, modified, and deleted documents.
    """

    def detect(
        self,
        current_documents: List[Dict[str, Any]],
        manifest: Dict[str, ManifestEntry],
        id_field: str = "id",
        text_field: str = "text",
    ) -> ChangeSet:
        change_set = ChangeSet()
        current_ids = set()

        for doc in current_documents:
            doc_id = str(doc.get(id_field, ""))
            text = doc.get(text_field, "")
            current_hash = ManifestEntry.hash_content(text)
            current_ids.add(doc_id)

            existing = manifest.get(doc_id)
            if existing is None:
                change_set.new_docs.append(doc)
            elif existing.content_hash != current_hash:
                change_set.modified_docs.append(doc)
            elif existing.status == IndexStatus.INDEXED:
                change_set.unchanged_count += 1
            else:
                change_set.new_docs.append(doc)   # previously failed — retry

        # Detect deletions
        for doc_id, entry in manifest.items():
            if doc_id not in current_ids and entry.status == IndexStatus.INDEXED:
                change_set.deleted_doc_ids.append(doc_id)

        return change_set
```

## Solution 3: Incremental Index Manifest Store

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Dict, Optional


class IncrementalIndexManifestStore:
    """
    Persists the document manifest to a JSON file.
    Supports batch updates and entry retrieval by doc_id.
    """

    def __init__(self, path: str = "/tmp/rag_manifest.json"):
        self._path = Path(path)
        self._lock = Lock()

    def load(self) -> Dict[str, ManifestEntry]:
        with self._lock:
            if not self._path.exists():
                return {}
            try:
                raw = json.loads(self._path.read_text())
                return {
                    doc_id: ManifestEntry(
                        doc_id=doc_id,
                        content_hash=data["content_hash"],
                        status=IndexStatus(data["status"]),
                        indexed_at=data.get("indexed_at"),
                        chunk_ids=data.get("chunk_ids", []),
                        metadata=data.get("metadata", {}),
                    )
                    for doc_id, data in raw.items()
                }
            except (json.JSONDecodeError, KeyError, OSError):
                return {}

    def save_entry(self, entry: ManifestEntry) -> None:
        with self._lock:
            raw = self._load_raw()
            raw[entry.doc_id] = {
                "content_hash": entry.content_hash,
                "status": entry.status.value,
                "indexed_at": entry.indexed_at,
                "chunk_ids": entry.chunk_ids,
                "metadata": entry.metadata,
            }
            self._path.write_text(json.dumps(raw, indent=2))

    def mark_deleted(self, doc_id: str) -> None:
        with self._lock:
            raw = self._load_raw()
            if doc_id in raw:
                raw[doc_id]["status"] = IndexStatus.DELETED.value
            self._path.write_text(json.dumps(raw, indent=2))

    def _load_raw(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def stats(self) -> dict:
        manifest = self.load()
        by_status: dict = {}
        for entry in manifest.values():
            by_status[entry.status.value] = by_status.get(entry.status.value, 0) + 1
        return {"total_documents": len(manifest), "by_status": by_status}
```

## Solution 4: Incremental Index Updater

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class IncrementalIndexUpdater:
    """
    Applies a detected ChangeSet to the vector index:
    embeds and upserts new/modified documents, deletes removed ones.
    Updates the manifest after each operation.
    """

    def __init__(
        self,
        manifest_store: IncrementalIndexManifestStore,
        embed_fn: Callable[[List[str]], List[Any]],
        upsert_fn: Callable[[List[Dict[str, Any]]], List[str]],
        delete_fn: Callable[[List[str]], None],
        text_field: str = "text",
        id_field: str = "id",
        batch_size: int = 50,
    ):
        self._store = manifest_store
        self._embed = embed_fn
        self._upsert = upsert_fn
        self._delete = delete_fn
        self._text_field = text_field
        self._id_field = id_field
        self._batch_size = batch_size

    async def apply(self, change_set: ChangeSet) -> dict:
        start = time.time()
        embedded = 0
        deleted = 0
        failed = 0

        # Process new and modified documents in batches
        to_process = change_set.new_docs + change_set.modified_docs
        for i in range(0, len(to_process), self._batch_size):
            batch = to_process[i:i + self._batch_size]
            texts = [doc[self._text_field] for doc in batch]
            try:
                vectors = await self._embed(texts)
                records = [
                    {"id": str(doc[self._id_field]), "vector": vec, **doc}
                    for doc, vec in zip(batch, vectors)
                ]
                chunk_ids = await self._upsert(records)
                for doc, chunk_id in zip(batch, chunk_ids or []):
                    entry = ManifestEntry(
                        doc_id=str(doc[self._id_field]),
                        content_hash=ManifestEntry.hash_content(doc[self._text_field]),
                        status=IndexStatus.INDEXED,
                        indexed_at=time.time(),
                        chunk_ids=[chunk_id] if chunk_id else [],
                    )
                    self._store.save_entry(entry)
                    embedded += 1
            except Exception:
                failed += len(batch)

        # Process deletions
        if change_set.deleted_doc_ids:
            try:
                await self._delete(change_set.deleted_doc_ids)
                for doc_id in change_set.deleted_doc_ids:
                    self._store.mark_deleted(doc_id)
                    deleted += 1
            except Exception:
                failed += len(change_set.deleted_doc_ids)

        return {
            "embedded": embedded,
            "deleted": deleted,
            "failed": failed,
            "unchanged": change_set.unchanged_count,
            "elapsed_ms": round((time.time() - start) * 1000, 2),
        }
```

## Solution 5: Incremental Update Scheduler

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class IncrementalUpdateScheduler:
    """
    Periodically runs incremental index updates by fetching current
    documents, detecting changes, and applying them. Tracks update
    efficiency over time.
    """

    def __init__(
        self,
        detector: DocumentChangeDetector,
        updater: IncrementalIndexUpdater,
        manifest_store: IncrementalIndexManifestStore,
        fetch_documents_fn: Callable[[], List[Dict[str, Any]]],
        interval_seconds: float = 300.0,
    ):
        self._detector = detector
        self._updater = updater
        self._store = manifest_store
        self._fetch = fetch_documents_fn
        self._interval = interval_seconds
        self._run_count = 0
        self._total_embedded = 0
        self._total_unchanged = 0

    async def run_once(self) -> dict:
        documents = self._fetch()
        manifest = self._store.load()
        change_set = self._detector.detect(documents, manifest)
        result = await self._updater.apply(change_set)
        self._run_count += 1
        self._total_embedded += result["embedded"]
        self._total_unchanged += result["unchanged"]
        return {**result, "change_set_size": change_set.total_changes}

    async def start_loop(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval)

    def efficiency_stats(self) -> dict:
        total = self._total_embedded + self._total_unchanged
        return {
            "run_count": self._run_count,
            "total_embedded": self._total_embedded,
            "total_unchanged": self._total_unchanged,
            "incremental_efficiency": round(
                self._total_unchanged / max(total, 1), 4
            ),
        }
```

## Solution 6: Incremental RAG Index Dashboard

```python
import time


class IncrementalRAGIndexDashboard:
    """
    Combines manifest stats, update efficiency, and scheduler history
    into a single operational report for RAG index management.
    """

    def __init__(
        self,
        manifest_store: IncrementalIndexManifestStore,
        scheduler: IncrementalUpdateScheduler,
    ):
        self._store = manifest_store
        self._scheduler = scheduler

    def render(self) -> dict:
        manifest_stats = self._store.stats()
        efficiency = self._scheduler.efficiency_stats()
        return {
            "generated_at": time.time(),
            "manifest": manifest_stats,
            "update_efficiency": efficiency,
            "health": {
                "incremental_efficiency_pct": round(efficiency["incremental_efficiency"] * 100, 1),
                "note": (
                    "high efficiency — most documents unchanged per run"
                    if efficiency["incremental_efficiency"] > 0.90 else
                    "low efficiency — consider increasing update interval"
                ),
            },
        }
```

## Comparison

| Approach | Change Detection | Manifest Persistence | Batch Embedding | Delete Handling | Scheduler |
|---|---|---|---|---|---|
| DocumentChangeDetector | Yes (hash diff) | No | No | Yes (detect) | No |
| IncrementalIndexManifestStore | No | Yes (JSON) | No | Yes (mark) | No |
| IncrementalIndexUpdater | No | Via store | Yes | Yes (delete_fn) | No |
| IncrementalUpdateScheduler | Via detector | Via store | Via updater | Via updater | Yes |
| IncrementalRAGIndexDashboard | No | No | No | No | No |

**Best for production**: Use `content_hash` (SHA-256 of document text) as the change signal rather than modification timestamps — timestamps can be unreliable across file systems and import pipelines, but content hashes are deterministic and portable. Set `batch_size=50` for embedding — most embedding APIs support up to 2048 inputs but 50 provides a good balance between API call overhead and error recovery granularity. Monitor `incremental_efficiency`: above 0.90 means 90% of documents are unchanged per run — an excellent result. Below 0.50 suggests documents are changing rapidly and the incremental approach provides less benefit; increase update interval or pre-filter changes by document type.
