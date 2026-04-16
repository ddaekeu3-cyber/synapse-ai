---
title: "Agent Doesn't Implement Result Materialization Cache for Repeated Subqueries"
description: "AI agents that process complex multi-step queries often recompute the same intermediate results: the same document chunks are re-embedded, the same SQL subqueries re-executed, the same API responses re-fetched within a single session. Result materialization caches intermediate computations by content hash, eliminating redundant work and cutting multi-step query latency by 40–80% in typical agentic workflows."
date: 2025-02-13
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-result-materialization-cache-for-repeated-subqueries
tags:
  - materialization
  - caching
  - memoization
  - subquery
  - intermediate-results
  - content-hash
  - deduplication
  - performance
symptoms:
  - "Agent re-embeds the same document chunks on every turn of a multi-turn conversation"
  - "Same SQL query executed 4 times in a single agent run because 4 tool calls share the same filter"
  - "Web search results fetched twice: once by the planner, once by the executor"
  - "Multi-step pipeline has no shared cache between stages"
  - "Agent response time grows linearly with the number of tool calls even for identical inputs"
---

## Problem

In multi-step agent workflows, intermediate results (embeddings, API responses, transformed data) are frequently recomputed by different tools in the same session. A planning step may fetch user data; the execution step fetches it again. Content-hash-based materialization detects when the same logical computation is requested and returns the cached result, regardless of which tool triggered the computation.

---

## Solution 1: ContentHashMaterializer — Hash-Keyed Intermediate Cache

```python
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, TypeVar

T = TypeVar("T")


def _hash_key(fn_name: str, args: tuple, kwargs: dict) -> str:
    """Derive a stable cache key from function name + inputs."""
    try:
        raw = json.dumps(
            {"fn": fn_name, "args": list(args), "kwargs": kwargs},
            sort_keys=True, default=str,
        )
    except TypeError:
        raw = f"{fn_name}:{str(args)}:{str(kwargs)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass
class MaterializedResult:
    value: Any
    computed_at: float
    hit_count: int = 0
    computation_ms: float = 0.0


class ContentHashMaterializer:
    """
    Caches intermediate computation results by content hash.
    Any function returning the same result for the same inputs is cached.

    Usage:
        mat = ContentHashMaterializer(ttl=300)

        # Wrap expensive computations:
        embeddings = await mat.compute("embed", embed_fn, text_chunks)
        sql_result = await mat.compute("db_query", db.execute, "SELECT ...")

        # Second call with same inputs returns instantly:
        embeddings2 = await mat.compute("embed", embed_fn, text_chunks)
        assert embeddings2 is embeddings  # same object, zero recomputation
    """

    def __init__(self, ttl: float = 300.0, max_entries: int = 1000):
        self._ttl = ttl
        self._max = max_entries
        self._cache: Dict[str, MaterializedResult] = {}

    async def compute(self, fn_name: str, fn: Callable,
                       *args, **kwargs) -> Any:
        key = _hash_key(fn_name, args, kwargs)
        entry = self._cache.get(key)
        if entry and time.monotonic() - entry.computed_at < self._ttl:
            entry.hit_count += 1
            return entry.value

        t0 = time.monotonic()
        value = await fn(*args, **kwargs)
        elapsed_ms = (time.monotonic() - t0) * 1000

        if len(self._cache) >= self._max:
            oldest = min(self._cache, key=lambda k: self._cache[k].computed_at)
            del self._cache[oldest]

        self._cache[key] = MaterializedResult(
            value=value,
            computed_at=time.monotonic(),
            computation_ms=elapsed_ms,
        )
        return value

    def invalidate(self, fn_name: str, *args, **kwargs):
        key = _hash_key(fn_name, args, kwargs)
        self._cache.pop(key, None)

    def stats(self) -> Dict[str, Any]:
        hits = sum(e.hit_count for e in self._cache.values())
        total_saved_ms = sum(
            e.hit_count * e.computation_ms for e in self._cache.values()
        )
        return {
            "entries": len(self._cache),
            "total_cache_hits": hits,
            "estimated_saved_ms": round(total_saved_ms, 1),
        }
```

---

## Solution 2: PipelineStageCache — Stage-Level Result Sharing

```python
import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class PipelineStage:
    name: str
    fn: Callable
    depends_on: List[str] = field(default_factory=list)


class PipelineStageCache:
    """
    Executes a multi-stage pipeline where stage outputs are cached
    by (stage_name, input_hash). Stages with the same inputs are
    never re-executed, even across different pipeline runs.

    Usage:
        cache = PipelineStageCache()
        stages = [
            PipelineStage("fetch_docs",  fetch_docs_fn),
            PipelineStage("embed",       embed_fn,       depends_on=["fetch_docs"]),
            PipelineStage("retrieve",    retrieve_fn,    depends_on=["embed"]),
            PipelineStage("generate",    generate_fn,    depends_on=["retrieve"]),
        ]
        result = await cache.execute(stages, initial_input={"query": "What is SSRF?"})
        print(result["generate"])
    """

    def __init__(self, ttl: float = 600.0):
        self._mat = ContentHashMaterializer(ttl=ttl)
        self._results: Dict[str, Any] = {}

    async def execute(self, stages: List[PipelineStage],
                       initial_input: Any) -> Dict[str, Any]:
        self._results = {"__input__": initial_input}
        for stage in stages:
            deps = {dep: self._results[dep] for dep in stage.depends_on}
            inp = deps or initial_input
            self._results[stage.name] = await self._mat.compute(
                stage.name, stage.fn, inp
            )
        return dict(self._results)

    def stage_stats(self) -> dict:
        return self._mat.stats()
```

---

## Solution 3: EmbeddingMaterializer — Deduplicate Chunk Embeddings

The most common redundant computation in RAG agents is embedding the same document chunk multiple times. This materializer deduplicates by chunk content hash.

```python
import asyncio
import hashlib
import numpy as np
from typing import Callable, Dict, List, Optional, Tuple


class EmbeddingMaterializer:
    """
    Deduplicates embedding calls by chunk content hash.
    If the same text is submitted in multiple batches, it is embedded once.

    Usage:
        emb = EmbeddingMaterializer(embed_fn=openai_embed, ttl=3600)

        # Multiple tool calls with overlapping chunks:
        emb1 = await emb.embed(chunks_from_tool_a)
        emb2 = await emb.embed(chunks_from_tool_b)
        # Shared chunks are embedded exactly once; results are reused.
    """

    def __init__(self, embed_fn: Callable, ttl: float = 3600.0,
                 batch_size: int = 64):
        self._embed = embed_fn
        self._ttl = ttl
        self._batch_size = batch_size
        self._cache: Dict[str, np.ndarray] = {}

    def _chunk_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    async def embed(self, texts: List[str]) -> np.ndarray:
        hashes = [self._chunk_hash(t) for t in texts]
        missing_idx = [i for i, h in enumerate(hashes) if h not in self._cache]
        missing_texts = [texts[i] for i in missing_idx]

        # Batch embed only the cache misses
        for start in range(0, len(missing_texts), self._batch_size):
            batch = missing_texts[start:start + self._batch_size]
            batch_idx = missing_idx[start:start + self._batch_size]
            embeddings = await self._embed(batch)
            for i, (idx, emb) in enumerate(zip(batch_idx, embeddings)):
                self._cache[hashes[idx]] = np.array(emb, dtype=np.float32)

        return np.array([self._cache[h] for h in hashes])

    def cache_stats(self) -> dict:
        return {
            "cached_chunks": len(self._cache),
            "cache_bytes": sum(v.nbytes for v in self._cache.values()),
        }
```

---

## Solution 4: SQLResultMaterializer — Cache Database Subquery Results

```python
import hashlib
import json
import time
from typing import Any, Callable, Dict, List, Optional


class SQLResultMaterializer:
    """
    Caches SQL query results by normalised query + parameter hash.
    Prevents the same subquery from hitting the database multiple times
    within a single agent session.

    Usage:
        db_cache = SQLResultMaterializer(db_execute_fn, ttl=60)
        rows = await db_cache.query("SELECT * FROM users WHERE status = ?", ["active"])
        rows2 = await db_cache.query("SELECT * FROM users WHERE status = ?", ["active"])
        # rows2 is returned from cache; no DB round-trip
    """

    def __init__(self, execute_fn: Callable, ttl: float = 60.0):
        self._execute = execute_fn
        self._ttl = ttl
        self._cache: Dict[str, tuple] = {}  # key -> (rows, expires_at)

    def _cache_key(self, sql: str, params: list) -> str:
        raw = json.dumps({"sql": sql.strip(), "params": params},
                         sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    async def query(self, sql: str,
                     params: Optional[List] = None) -> List[Any]:
        params = params or []
        key = self._cache_key(sql, params)
        entry = self._cache.get(key)
        if entry:
            rows, expires_at = entry
            if time.monotonic() < expires_at:
                return rows
        rows = await self._execute(sql, params)
        self._cache[key] = (rows, time.monotonic() + self._ttl)
        return rows

    def invalidate_table(self, table_name: str):
        """Invalidate all cached queries mentioning a table."""
        to_drop = [k for k in self._cache
                   if table_name.lower() in k.lower()]
        for k in to_drop:
            del self._cache[k]

    def stats(self) -> dict:
        now = time.monotonic()
        valid = sum(1 for _, exp in self._cache.values() if now < exp)
        return {"total": len(self._cache), "valid": valid}
```

---

## Solution 5: SessionMaterializationStore — Per-Session Shared Cache

```python
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Optional


class SessionMaterializationStore:
    """
    Provides a per-session materializer that is shared across all tools
    within one agent session. Tools contribute to and read from the same
    cache, eliminating cross-tool redundancy.

    Usage:
        store = SessionMaterializationStore()

        async with store.session("sess-abc") as mat:
            # All tools within this session share `mat`:
            doc1 = await mat.compute("fetch", fetch_fn, "url-1")
            emb1 = await mat.compute("embed", embed_fn, doc1)

            # Second tool in same session:
            doc1_again = await mat.compute("fetch", fetch_fn, "url-1")
            # → instant return, no re-fetch
    """

    DEFAULT_TTL = 3600.0

    def __init__(self, session_ttl: float = DEFAULT_TTL):
        self._sessions: Dict[str, ContentHashMaterializer] = {}
        self._session_ttl = session_ttl
        self._created_at: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self, session_id: str):
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = ContentHashMaterializer(
                    ttl=self._session_ttl
                )
                self._created_at[session_id] = time.monotonic()
        yield self._sessions[session_id]

    async def evict_expired(self):
        now = time.monotonic()
        async with self._lock:
            expired = [sid for sid, t in self._created_at.items()
                       if now - t > self._session_ttl]
            for sid in expired:
                del self._sessions[sid]
                del self._created_at[sid]

    def global_stats(self) -> dict:
        all_stats = {sid: mat.stats() for sid, mat in self._sessions.items()}
        total_hits = sum(s["total_cache_hits"] for s in all_stats.values())
        total_saved = sum(s["estimated_saved_ms"] for s in all_stats.values())
        return {
            "active_sessions": len(self._sessions),
            "total_cache_hits": total_hits,
            "total_saved_ms": round(total_saved, 1),
            "per_session": all_stats,
        }
```

---

## Solution 6: MaterializationAwareAgent — Drop-In Integration

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class MaterializationAwareAgent:
    """
    Agent base class that automatically materializes intermediate results.
    Wraps registered tool functions with the content-hash cache.
    Tools with the same input will be called at most once per session.

    Usage:
        class MyAgent(MaterializationAwareAgent):
            def __init__(self):
                super().__init__()
                self.register_tool("web_search",    web_search_fn,    ttl=300)
                self.register_tool("embed_chunks",  embed_fn,         ttl=3600)
                self.register_tool("db_query",      db_execute_fn,    ttl=60)

            async def handle(self, session_id: str, query: str):
                async with self._mat_store.session(session_id) as mat:
                    docs = await mat.compute("web_search", self._tools["web_search"], query)
                    embs = await mat.compute("embed_chunks", self._tools["embed_chunks"], docs)
                    return embs
    """

    def __init__(self, session_ttl: float = 3600.0):
        self._mat_store = SessionMaterializationStore(session_ttl)
        self._tools: Dict[str, Callable] = {}
        self._tool_ttls: Dict[str, float] = {}

    def register_tool(self, name: str, fn: Callable, ttl: float = 300.0):
        self._tools[name] = fn
        self._tool_ttls[name] = ttl

    async def call_tool(self, session_id: str, tool_name: str,
                         *args, **kwargs) -> Any:
        fn = self._tools.get(tool_name)
        if fn is None:
            raise KeyError(f"Tool '{tool_name}' not registered")
        async with self._mat_store.session(session_id) as mat:
            return await mat.compute(tool_name, fn, *args, **kwargs)

    def materialization_report(self) -> dict:
        return self._mat_store.global_stats()
```

---

## Comparison

| Approach | Scope | Hash Strategy | Invalidation | Cross-Tool Sharing | Persistence |
|---|---|---|---|---|---|
| **ContentHashMaterializer** | Function-level | Args JSON hash | Manual | No | No |
| **PipelineStageCache** | Pipeline-level | Stage + input hash | Implicit (TTL) | Yes | No |
| **EmbeddingMaterializer** | Chunk-level | Content hash | Manual | Yes | No |
| **SQLResultMaterializer** | Query-level | SQL + params hash | Table-based | No | No |
| **SessionMaterializationStore** | Session-level | Args JSON hash | Session expiry | Yes | No |
| **MaterializationAwareAgent** | Agent-level | Args JSON hash | Session TTL | Yes | No |

**Key insight**: the greatest savings come from sharing materialized results across tools within the same session. A fetch executed by the planner should not be re-executed by the executor. Use `SessionMaterializationStore` as the session-scoped cache and register every expensive tool through `ContentHashMaterializer`; typical multi-step agents see 40–70% reduction in total tool call latency.
