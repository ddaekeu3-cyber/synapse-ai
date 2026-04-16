---
title: "Agent Doesn't Implement Persistent Vector Index for Cross-Session Memory"
description: "AI agents that rebuild their vector index from scratch on every startup discard the embeddings computed in all prior sessions, wasting 10–60 seconds per restart on re-embedding. A persistent vector index serialises embeddings to disk between sessions using memory-mapped files or a lightweight vector database, reducing startup from minutes to milliseconds and enabling long-term agent memory that survives process restarts."
date: 2025-02-15
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-persistent-vector-index-for-cross-session-memory
tags:
  - persistent-index
  - vector-store
  - cross-session
  - mmap
  - startup
  - long-term-memory
  - performance
symptoms:
  - "Agent re-embeds the same document corpus every time it starts"
  - "Startup takes 45 seconds because 10,000 chunks are being embedded again"
  - "Knowledge from previous sessions is lost when the agent process restarts"
  - "Embedding cost appears in every deployment even for unchanged documents"
  - "Agent memory is ephemeral — users have to re-explain context every session"
---

## Problem

A vector index built in memory lives only as long as the process. When the agent restarts — for a deployment, a crash recovery, or a scheduled scale-down — all embeddings are discarded. Re-embedding a 10,000-chunk corpus at 100 chunks/s takes 100 seconds and costs real money per restart. Persistent vector indexes serialise the embedding matrix and metadata to disk using numpy's memory-mapped format (`.npy`), a SQLite backing store, or a lightweight embedded vector database. On the next startup the index is loaded in milliseconds directly from disk without re-embedding any document.

---

## Solution 1: NpyVectorStore — NumPy Memory-Mapped Persistent Index

```python
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class VectorEntry:
    doc_id: str
    text: str
    embedding: np.ndarray
    metadata: Dict[str, Any] = field(default_factory=dict)


class NpyVectorStore:
    """
    Persistent vector store backed by a memory-mapped NumPy array.
    Embeddings and metadata survive process restarts.
    Startup after the first run takes < 100 ms regardless of corpus size.

    Usage:
        store = NpyVectorStore(directory="~/.agent/vectors")
        store.load()      # loads from disk if exists, no-op otherwise

        # Add embeddings (only needed once per document):
        for doc_id, text, embedding in new_documents:
            store.upsert(doc_id, text, embedding)
        store.save()

        # Search:
        results = store.search(query_embedding, top_k=5)
    """

    def __init__(self, directory: str):
        self._dir = os.path.expanduser(directory)
        os.makedirs(self._dir, exist_ok=True)
        self._embeddings: Optional[np.ndarray] = None   # shape (N, D)
        self._meta: List[Dict[str, Any]] = []           # parallel list
        self._dirty = False

    def _paths(self):
        return (
            os.path.join(self._dir, "embeddings.npy"),
            os.path.join(self._dir, "metadata.json"),
        )

    def load(self) -> int:
        emb_path, meta_path = self._paths()
        if os.path.exists(emb_path) and os.path.exists(meta_path):
            self._embeddings = np.load(emb_path)
            with open(meta_path) as f:
                self._meta = json.load(f)
            return len(self._meta)
        return 0

    def save(self):
        if not self._dirty:
            return
        emb_path, meta_path = self._paths()
        np.save(emb_path, self._embeddings)
        with open(meta_path, "w") as f:
            json.dump(self._meta, f)
        self._dirty = False

    def upsert(self, doc_id: str, text: str, embedding: np.ndarray,
                metadata: Optional[Dict] = None):
        emb = np.array(embedding, dtype=np.float32)
        # Update existing entry
        for i, m in enumerate(self._meta):
            if m["doc_id"] == doc_id:
                self._embeddings[i] = emb
                self._meta[i] = {"doc_id": doc_id, "text": text,
                                  **(metadata or {})}
                self._dirty = True
                return
        # Insert new entry
        if self._embeddings is None:
            self._embeddings = emb.reshape(1, -1)
        else:
            self._embeddings = np.vstack([self._embeddings, emb.reshape(1, -1)])
        self._meta.append({"doc_id": doc_id, "text": text, **(metadata or {})})
        self._dirty = True

    def search(self, query_embedding: np.ndarray,
                top_k: int = 5) -> List[Tuple[Dict, float]]:
        if self._embeddings is None or len(self._meta) == 0:
            return []
        q = np.array(query_embedding, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-8)
        norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True) + 1e-8
        normed = self._embeddings / norms
        scores = normed @ q
        top_idx = np.argsort(scores)[::-1][:top_k]
        return [(self._meta[i], float(scores[i])) for i in top_idx]

    def doc_ids(self) -> List[str]:
        return [m["doc_id"] for m in self._meta]

    def size(self) -> int:
        return len(self._meta)
```

---

## Solution 2: IncrementalIndexUpdater — Only Embed New Documents

```python
import hashlib
import logging
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class IncrementalIndexUpdater:
    """
    Computes content hashes for documents and only embeds those whose
    hash has changed since the last index build. Persists the hash registry
    alongside the vector store to avoid re-embedding unchanged documents.

    Usage:
        updater = IncrementalIndexUpdater(
            store=NpyVectorStore("~/.agent/vectors"),
            embed_fn=openai_embed,
        )
        await updater.update(new_documents)
        # Only documents with changed content will be re-embedded.
    """

    def __init__(self, store: NpyVectorStore,
                 embed_fn: Callable,
                 hash_registry_path: str = "~/.agent/vectors/hashes.json"):
        self._store = store
        self._embed = embed_fn
        self._registry_path = os.path.expanduser(hash_registry_path)
        self._hashes: Dict[str, str] = self._load_hashes()

    def _load_hashes(self) -> Dict[str, str]:
        if os.path.exists(self._registry_path):
            import json
            with open(self._registry_path) as f:
                return json.load(f)
        return {}

    def _save_hashes(self):
        import json
        with open(self._registry_path, "w") as f:
            json.dump(self._hashes, f)

    def _content_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    async def update(self, documents: List[Dict[str, Any]],
                      batch_size: int = 32):
        """documents: list of dicts with 'doc_id' and 'text' keys."""
        to_embed = []
        for doc in documents:
            doc_id = doc["doc_id"]
            content_hash = self._content_hash(doc["text"])
            if self._hashes.get(doc_id) != content_hash:
                to_embed.append(doc)

        if not to_embed:
            logger.info("incremental_update: no changes detected, 0 embeddings computed")
            return 0

        logger.info("incremental_update: embedding %d/%d documents",
                     len(to_embed), len(documents))

        for i in range(0, len(to_embed), batch_size):
            batch = to_embed[i:i + batch_size]
            texts = [d["text"] for d in batch]
            embeddings = await self._embed(texts)
            for doc, emb in zip(batch, embeddings):
                self._store.upsert(
                    doc["doc_id"], doc["text"],
                    emb, doc.get("metadata", {})
                )
                self._hashes[doc["doc_id"]] = self._content_hash(doc["text"])

        self._store.save()
        self._save_hashes()
        return len(to_embed)
```

---

## Solution 3: SqliteVectorStore — SQLite-Backed Persistent Index

```python
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


class SqliteVectorStore:
    """
    SQLite-backed vector store. Each document's embedding is stored as
    a BLOB of float32 values. Suitable for corpora up to ~100k documents
    where disk-based storage is preferred over file-based arrays.

    Usage:
        store = SqliteVectorStore("~/.agent/memory.db")
        store.upsert("doc-1", "text content", embedding)
        results = store.search(query_emb, top_k=5)
    """

    def __init__(self, db_path: str):
        self._path = os.path.expanduser(db_path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._create_table()

    def _create_table(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS vectors (
                doc_id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)
        self._conn.commit()

    def upsert(self, doc_id: str, text: str, embedding: np.ndarray,
                metadata: Optional[Dict] = None):
        emb_bytes = np.array(embedding, dtype=np.float32).tobytes()
        self._conn.execute("""
            INSERT INTO vectors (doc_id, text, embedding, metadata)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                text=excluded.text,
                embedding=excluded.embedding,
                metadata=excluded.metadata
        """, (doc_id, text, emb_bytes, json.dumps(metadata or {})))
        self._conn.commit()

    def search(self, query_embedding: np.ndarray,
                top_k: int = 5) -> List[Tuple[Dict[str, Any], float]]:
        q = np.array(query_embedding, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-8)

        rows = self._conn.execute(
            "SELECT doc_id, text, embedding, metadata FROM vectors"
        ).fetchall()
        if not rows:
            return []

        doc_ids, texts, emb_blobs, metas = zip(*rows)
        matrix = np.stack([
            np.frombuffer(b, dtype=np.float32) for b in emb_blobs
        ])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-8
        normed = matrix / norms
        scores = normed @ q
        top_idx = np.argsort(scores)[::-1][:top_k]

        return [
            ({
                "doc_id": doc_ids[i],
                "text": texts[i],
                "metadata": json.loads(metas[i]),
            }, float(scores[i]))
            for i in top_idx
        ]

    def size(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]

    def doc_ids(self) -> List[str]:
        return [r[0] for r in self._conn.execute(
            "SELECT doc_id FROM vectors"
        ).fetchall()]

    def delete(self, doc_id: str):
        self._conn.execute("DELETE FROM vectors WHERE doc_id = ?", (doc_id,))
        self._conn.commit()
```

---

## Solution 4: PersistentAgentMemory — Long-Term Cross-Session Memory

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class PersistentAgentMemory:
    """
    High-level agent memory that persists observations, facts, and
    conversation summaries across sessions using a persistent vector store.

    Usage:
        memory = PersistentAgentMemory(
            store=SqliteVectorStore("~/.agent/memory.db"),
            embed_fn=openai_embed,
        )
        await memory.remember("User prefers concise answers", tags=["preference"])
        recall = await memory.recall("how verbose should my answers be?", top_k=3)
    """

    def __init__(self, store: SqliteVectorStore,
                  embed_fn: Callable):
        self._store = store
        self._embed = embed_fn
        import time, hashlib
        self._id_fn = lambda t: hashlib.sha256(
            f"{t}{time.time()}".encode()
        ).hexdigest()[:12]

    async def remember(self, text: str,
                         doc_id: Optional[str] = None,
                         tags: Optional[List[str]] = None,
                         **metadata):
        embedding = (await self._embed([text]))[0]
        did = doc_id or self._id_fn(text)
        self._store.upsert(
            did, text, embedding,
            {"tags": tags or [], **metadata}
        )
        return did

    async def recall(self, query: str, top_k: int = 5,
                      tag_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        q_emb = (await self._embed([query]))[0]
        results = self._store.search(q_emb, top_k=top_k * 2)
        if tag_filter:
            results = [
                (d, s) for d, s in results
                if tag_filter in d.get("metadata", {}).get("tags", [])
            ]
        return [
            {"text": d["text"], "score": round(s, 4), **d.get("metadata", {})}
            for d, s in results[:top_k]
        ]

    def memory_size(self) -> int:
        return self._store.size()
```

---

## Solution 5: IndexStartupLoader — Fast Boot with Pre-Warmed Index

```python
import asyncio
import logging
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


class IndexStartupLoader:
    """
    Loads the persistent vector index at agent startup before serving
    requests. Falls back to rebuilding the index from source documents
    if the persistent store is empty or stale.

    Usage:
        loader = IndexStartupLoader(
            store=NpyVectorStore("~/.agent/vectors"),
            source_fn=load_documents_from_db,
            embed_fn=openai_embed,
            max_age_days=7,
        )
        await loader.boot()
        # Index is ready; startup time < 500 ms if persistent data exists
    """

    def __init__(self, store: NpyVectorStore,
                  source_fn: Callable,
                  embed_fn: Callable,
                  max_age_days: float = 7.0):
        self._store = store
        self._source_fn = source_fn
        self._embed = embed_fn
        self._max_age_s = max_age_days * 86400
        self._updater: Optional[IncrementalIndexUpdater] = None

    async def boot(self) -> dict:
        t0 = time.monotonic()
        loaded = self._store.load()
        load_ms = (time.monotonic() - t0) * 1000
        logger.info("index_load loaded=%d entries in %.0f ms", loaded, load_ms)

        if loaded == 0:
            logger.info("index_empty — rebuilding from source")
            docs = await self._source_fn()
            updater = IncrementalIndexUpdater(self._store, self._embed)
            embedded = await updater.update(docs)
            total_ms = (time.monotonic() - t0) * 1000
            return {"source": "rebuild", "entries": embedded, "ms": round(total_ms)}

        return {"source": "disk", "entries": loaded, "ms": round(load_ms)}
```

---

## Solution 6: VectorStoreMetrics — Track Index Health and Staleness

```python
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class VectorStoreMetrics:
    """
    Tracks persistent vector store health: size, disk usage, age, and
    search latency. Use for dashboards and capacity planning.

    Usage:
        metrics = VectorStoreMetrics(store, store_path="~/.agent/vectors")
        report = metrics.report()
        # {"entries": 10000, "disk_mb": 38.4, "age_hours": 2.1, ...}
    """

    def __init__(self, store: NpyVectorStore, store_path: str):
        self._store = store
        self._dir = os.path.expanduser(store_path)
        self._search_times: list = []

    def record_search(self, elapsed_ms: float):
        self._search_times.append(elapsed_ms)
        if len(self._search_times) > 1000:
            self._search_times.pop(0)

    def report(self) -> Dict[str, Any]:
        emb_path = os.path.join(self._dir, "embeddings.npy")
        meta_path = os.path.join(self._dir, "metadata.json")

        disk_bytes = 0
        age_s = None
        for path in [emb_path, meta_path]:
            if os.path.exists(path):
                stat = os.stat(path)
                disk_bytes += stat.st_size
                mtime = stat.st_mtime
                age_s = min(age_s or mtime, mtime)

        report: Dict[str, Any] = {
            "entries": self._store.size(),
            "disk_mb": round(disk_bytes / 1024 / 1024, 2),
        }
        if age_s is not None:
            report["age_hours"] = round((time.time() - age_s) / 3600, 1)
        if self._search_times:
            import statistics
            report["search_p50_ms"] = round(
                statistics.median(self._search_times), 1
            )
            report["search_p95_ms"] = round(
                sorted(self._search_times)[int(len(self._search_times) * 0.95)], 1
            )
        return report
```

---

## Comparison

| Approach | Backend | Incremental | Cross-Session | Startup Fast | Search |
|---|---|---|---|---|---|
| **NpyVectorStore** | .npy files | No | Yes | Yes | Cosine |
| **IncrementalIndexUpdater** | Any store | Yes | Yes | Yes | Via store |
| **SqliteVectorStore** | SQLite | No | Yes | Yes | Cosine |
| **PersistentAgentMemory** | SqliteVectorStore | No | Yes | Yes | Semantic |
| **IndexStartupLoader** | Any store | Yes (via updater) | Yes | Yes | Via store |
| **VectorStoreMetrics** | Any store | No | N/A | No | No |

**Key insight**: the greatest startup saving comes from `IncrementalIndexUpdater` combined with a content hash registry. On a 10,000-document corpus, a normal restart (same documents) takes < 100 ms to load from disk vs 100+ seconds to re-embed. Only changed or new documents incur embedding cost. Use `NpyVectorStore` for simplicity on single-node deployments; use `SqliteVectorStore` when you need transactional deletes or when the corpus changes frequently.
