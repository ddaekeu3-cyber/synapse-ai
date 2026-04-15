---
layout: solution
title: "Agent Doesn't Checkpoint Long-Running Tasks — Loses All Progress on Failure"
category: general
description: "An agent processes 500 documents, fails on item 487, and restarts from scratch. No intermediate state was saved. Hours of API calls and compute are wasted. The fix is to checkpoint completed work to durable storage so restarts resume from the last safe point."
tags: [reliability, checkpointing, idempotency, resume, long-running, progress, durability, fault-tolerance]
---

# Agent Doesn't Checkpoint Long-Running Tasks — Loses All Progress on Failure

## Symptom
- Agent processes 450 of 500 items, crashes, restarts from item 1
- Long-running extraction pipeline fails at 90% completion — full re-run required
- API rate limit hit mid-batch — entire batch discarded, must retry everything
- Agent OOM-killed after 2 hours; no state saved; operator re-runs from scratch
- Partial results exist in memory but are lost when the process exits
- Multi-step workflow (fetch → analyze → transform → store) loses intermediate step results on any failure

## Root Cause
Long-running agent tasks treat all state as ephemeral in-memory structures. When the process terminates — for any reason (exception, OOM, timeout, rate limit, operator Ctrl-C) — all progress is lost. The fix is to write a checkpoint after each unit of committed work, so that any restart can load the checkpoint and skip already-completed items.

## Fix

### Option 1: File-based checkpoint — simplest durable progress tracking

```python
import anthropic
import json
import os
import time
from pathlib import Path

client = anthropic.Anthropic()

class FileCheckpoint:
    """
    Saves progress to a JSON file after each completed item.
    On restart, loads the checkpoint and skips completed items.
    """
    def __init__(self, checkpoint_path: str):
        self.path = Path(checkpoint_path)
        self._state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path) as f:
                data = json.load(f)
            print(f"[checkpoint] Resumed: {data['completed']} items already done")
            return data
        return {"completed": [], "results": {}, "started_at": time.time()}

    def is_done(self, item_id: str) -> bool:
        return item_id in self._state["completed"]

    def save(self, item_id: str, result: dict) -> None:
        self._state["completed"].append(item_id)
        self._state["results"][item_id] = result
        self._state["updated_at"] = time.time()
        # Atomic write: write to .tmp, then rename
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._state, f, indent=2)
        tmp.rename(self.path)

    @property
    def results(self) -> dict:
        return self._state["results"]

    def clear(self) -> None:
        """Call when the full job completes successfully."""
        self.path.unlink(missing_ok=True)


def analyze_document(doc_id: str, content: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Summarize in one sentence: {content[:500]}"}]
    )
    return {"doc_id": doc_id, "summary": response.content[0].text}


def process_documents_with_checkpoint(documents: list[dict], job_id: str) -> dict:
    """
    Process documents with file-based checkpointing.
    Safe to kill and restart at any time.
    """
    cp = FileCheckpoint(f"/tmp/job_{job_id}.checkpoint.json")

    for doc in documents:
        doc_id = doc["id"]

        if cp.is_done(doc_id):
            print(f"  skip {doc_id} (already done)")
            continue

        print(f"  processing {doc_id}...")
        result = analyze_document(doc_id, doc["content"])
        cp.save(doc_id, result)          # ← checkpoint after each item
        print(f"  ✓ {doc_id} saved")

    all_results = cp.results
    cp.clear()                           # ← clean up on success
    return all_results


# Usage:
documents = [{"id": f"doc_{i}", "content": f"Document {i} text..."} for i in range(100)]
results = process_documents_with_checkpoint(documents, job_id="extraction_20240115")
print(f"Completed: {len(results)} documents")
```

### Option 2: SQLite checkpoint — handles large jobs with query support

```python
import anthropic
import sqlite3
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

client = anthropic.Anthropic()

class ItemStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"

@dataclass
class CheckpointRecord:
    item_id: str
    status: ItemStatus
    result: dict | None
    error: str | None
    attempts: int
    updated_at: float

class SQLiteCheckpoint:
    """
    SQLite-backed checkpoint store.
    Supports large jobs, querying by status, retry counting.
    """
    def __init__(self, db_path: str, job_id: str):
        self.db_path = db_path
        self.job_id = job_id
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    job_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result TEXT,
                    error TEXT,
                    attempts INTEGER DEFAULT 0,
                    updated_at REAL,
                    PRIMARY KEY (job_id, item_id)
                )
            """)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def register_items(self, item_ids: list[str]) -> None:
        """Register all items upfront (idempotent)."""
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO checkpoints (job_id, item_id, status, updated_at) VALUES (?, ?, 'pending', ?)",
                [(self.job_id, item_id, time.time()) for item_id in item_ids]
            )

    def next_pending(self) -> str | None:
        """Claim the next pending item (atomic)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT item_id FROM checkpoints WHERE job_id=? AND status='pending' ORDER BY rowid LIMIT 1",
                (self.job_id,)
            ).fetchone()
            if not row:
                return None
            item_id = row["item_id"]
            conn.execute(
                "UPDATE checkpoints SET status='processing', updated_at=? WHERE job_id=? AND item_id=?",
                (time.time(), self.job_id, item_id)
            )
            return item_id

    def mark_done(self, item_id: str, result: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE checkpoints SET status='done', result=?, updated_at=? WHERE job_id=? AND item_id=?",
                (json.dumps(result), time.time(), self.job_id, item_id)
            )

    def mark_failed(self, item_id: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE checkpoints SET status='failed', error=?, attempts=attempts+1, updated_at=? WHERE job_id=? AND item_id=?",
                (error, time.time(), self.job_id, item_id)
            )

    def summary(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM checkpoints WHERE job_id=? GROUP BY status",
                (self.job_id,)
            ).fetchall()
        return {row["status"]: row["cnt"] for row in rows}

    def all_results(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT item_id, result FROM checkpoints WHERE job_id=? AND status='done'",
                (self.job_id,)
            ).fetchall()
        return {row["item_id"]: json.loads(row["result"]) for row in rows}


def run_job_with_sqlite_checkpoint(items: list[dict], job_id: str) -> dict:
    cp = SQLiteCheckpoint("/tmp/agent_jobs.db", job_id)
    cp.register_items([item["id"] for item in items])

    # Build lookup for content
    content_map = {item["id"]: item["content"] for item in items}

    while True:
        item_id = cp.next_pending()
        if item_id is None:
            break

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": f"Classify: {content_map[item_id][:300]}"}]
            )
            cp.mark_done(item_id, {"classification": response.content[0].text})
        except Exception as e:
            cp.mark_failed(item_id, str(e))

        summary = cp.summary()
        print(f"  Progress: {summary}")

    return cp.all_results()
```

### Option 3: Multi-step workflow checkpoint — save state between pipeline stages

```python
import anthropic
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

client = anthropic.Anthropic()

@dataclass
class PipelineState:
    """Tracks progress through a multi-step pipeline."""
    job_id: str
    stage: str          # "fetch" | "analyze" | "transform" | "store"
    stage_index: int    # which item within the current stage
    fetch_results: dict[str, Any]
    analyze_results: dict[str, Any]
    transform_results: dict[str, Any]
    completed_at: str | None = None

class PipelineCheckpoint:
    def __init__(self, job_id: str):
        self.path = Path(f"/tmp/pipeline_{job_id}.json")
        self.state = self._load(job_id)

    def _load(self, job_id: str) -> PipelineState:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            print(f"[pipeline] Resuming from stage={data['stage']} idx={data['stage_index']}")
            return PipelineState(**data)
        return PipelineState(
            job_id=job_id,
            stage="fetch",
            stage_index=0,
            fetch_results={},
            analyze_results={},
            transform_results={}
        )

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2))
        tmp.rename(self.path)

    def advance_stage(self, next_stage: str):
        self.state.stage = next_stage
        self.state.stage_index = 0
        self._save()

    def record_fetch(self, item_id: str, raw: str):
        self.state.fetch_results[item_id] = raw
        self.state.stage_index += 1
        self._save()

    def record_analysis(self, item_id: str, analysis: dict):
        self.state.analyze_results[item_id] = analysis
        self.state.stage_index += 1
        self._save()

    def record_transform(self, item_id: str, transformed: dict):
        self.state.transform_results[item_id] = transformed
        self.state.stage_index += 1
        self._save()

    def mark_complete(self):
        self.state.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._save()


def run_pipeline(item_ids: list[str], job_id: str) -> list[dict]:
    """
    Multi-stage pipeline that checkpoints after each item in each stage.
    Can be interrupted at any point and resumed exactly.
    """
    cp = PipelineCheckpoint(job_id)
    s = cp.state

    # Stage 1: Fetch
    if s.stage == "fetch":
        pending = item_ids[s.stage_index:]
        for item_id in pending:
            # Simulate fetch
            raw_content = f"raw content for {item_id}"
            cp.record_fetch(item_id, raw_content)
        cp.advance_stage("analyze")

    # Stage 2: Analyze (LLM call per item)
    if s.stage == "analyze":
        done_ids = set(s.analyze_results.keys())
        for item_id in item_ids:
            if item_id in done_ids:
                continue
            raw = s.fetch_results[item_id]
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": f"Extract key topics from: {raw}"}]
            )
            cp.record_analysis(item_id, {"topics": response.content[0].text})
        cp.advance_stage("transform")

    # Stage 3: Transform
    if s.stage == "transform":
        done_ids = set(s.transform_results.keys())
        for item_id in item_ids:
            if item_id in done_ids:
                continue
            analysis = s.analyze_results[item_id]
            transformed = {
                "id": item_id,
                "topics": analysis["topics"].split(", "),
                "processed_at": time.time()
            }
            cp.record_transform(item_id, transformed)
        cp.advance_stage("done")

    cp.mark_complete()
    return list(s.transform_results.values())
```

### Option 4: Redis checkpoint — distributed jobs, multiple workers

```python
import anthropic
import json
import time
import hashlib
from typing import Iterator

client = anthropic.Anthropic()

# Redis checkpoint for distributed multi-worker jobs.
# Requires: pip install redis
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

class RedisCheckpoint:
    """
    Redis-backed checkpoint for distributed agent jobs.
    Multiple workers can safely process the same job concurrently.
    """
    def __init__(self, redis_url: str, job_id: str, ttl_seconds: int = 86400):
        if not REDIS_AVAILABLE:
            raise ImportError("pip install redis")
        self.r = redis.from_url(redis_url)
        self.job_id = job_id
        self.ttl = ttl_seconds
        self.pending_key = f"job:{job_id}:pending"
        self.done_key = f"job:{job_id}:done"
        self.result_key = f"job:{job_id}:results"
        self.failed_key = f"job:{job_id}:failed"

    def enqueue(self, items: list[dict]) -> int:
        """Populate the work queue (idempotent via done set)."""
        pipe = self.r.pipeline()
        enqueued = 0
        done_ids = self.r.smembers(self.done_key)
        done_ids_str = {d.decode() for d in done_ids}

        for item in items:
            item_id = item["id"]
            if item_id not in done_ids_str:
                pipe.lpush(self.pending_key, json.dumps(item))
                enqueued += 1
        pipe.expire(self.pending_key, self.ttl)
        pipe.execute()
        print(f"[redis-cp] Enqueued {enqueued} items ({len(done_ids_str)} already done)")
        return enqueued

    def claim_item(self, timeout: int = 5) -> dict | None:
        """Atomically claim an item (blocking pop)."""
        result = self.r.brpop(self.pending_key, timeout=timeout)
        if result is None:
            return None
        _, raw = result
        return json.loads(raw)

    def complete_item(self, item_id: str, result: dict) -> None:
        pipe = self.r.pipeline()
        pipe.sadd(self.done_key, item_id)
        pipe.hset(self.result_key, item_id, json.dumps(result))
        pipe.expire(self.done_key, self.ttl)
        pipe.expire(self.result_key, self.ttl)
        pipe.execute()

    def fail_item(self, item_id: str, error: str, requeue: bool = True) -> None:
        self.r.hset(self.failed_key, item_id, error)
        if requeue:
            self.r.lpush(self.pending_key, json.dumps({"id": item_id, "_retry": True}))

    def all_results(self) -> dict:
        raw = self.r.hgetall(self.result_key)
        return {k.decode(): json.loads(v) for k, v in raw.items()}

    def stats(self) -> dict:
        return {
            "pending": self.r.llen(self.pending_key),
            "done": self.r.scard(self.done_key),
            "failed": self.r.hlen(self.failed_key),
        }


def worker_loop(redis_url: str, job_id: str) -> None:
    """
    Worker that claims and processes items from a Redis-backed queue.
    Safe to run multiple instances concurrently.
    """
    cp = RedisCheckpoint(redis_url, job_id)

    while True:
        item = cp.claim_item(timeout=5)
        if item is None:
            print("[worker] Queue empty, exiting")
            break

        item_id = item["id"]
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": f"Process: {item.get('content', '')}"}]
            )
            cp.complete_item(item_id, {"output": response.content[0].text})
            print(f"[worker] ✓ {item_id} | stats: {cp.stats()}")
        except Exception as e:
            print(f"[worker] ✗ {item_id}: {e}")
            cp.fail_item(item_id, str(e), requeue=True)
```

### Option 5: Idempotent operation IDs — prevent duplicate work on retry

```python
import anthropic
import hashlib
import json
import time
from pathlib import Path

client = anthropic.Anthropic()

class IdempotentProcessor:
    """
    Assigns a deterministic operation ID to each unit of work.
    Re-running with the same inputs returns the cached result without calling the LLM again.
    Useful when you can't control retry logic in the caller.
    """
    def __init__(self, cache_dir: str = "/tmp/idempotent_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _operation_id(self, operation_type: str, **kwargs) -> str:
        """Deterministic ID from operation type + inputs."""
        payload = json.dumps({"type": operation_type, **kwargs}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def _cache_path(self, op_id: str) -> Path:
        return self.cache_dir / f"{op_id}.json"

    def get_cached(self, op_id: str) -> dict | None:
        path = self._cache_path(op_id)
        if path.exists():
            return json.loads(path.read_text())
        return None

    def cache_result(self, op_id: str, result: dict) -> None:
        path = self._cache_path(op_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result))
        tmp.rename(path)

    def analyze(self, text: str, task: str) -> dict:
        """
        Idempotent analysis — same text+task always returns the same result.
        LLM is only called once per unique (text, task) pair.
        """
        op_id = self._operation_id("analyze", text=text[:200], task=task)
        cached = self.get_cached(op_id)
        if cached:
            print(f"  [cache hit] op={op_id}")
            return cached

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Task: {task}\n\nText: {text}"}]
        )
        result = {
            "op_id": op_id,
            "output": response.content[0].text,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "computed_at": time.time()
        }
        self.cache_result(op_id, result)
        print(f"  [computed] op={op_id}")
        return result


processor = IdempotentProcessor()

# First run: calls LLM
r1 = processor.analyze("The quick brown fox...", task="sentiment analysis")
# Second run (crash recovery): returns cache instantly
r2 = processor.analyze("The quick brown fox...", task="sentiment analysis")
assert r1["output"] == r2["output"]   # same result, no LLM call
```

### Option 6: Checkpoint with progress reporting and ETA

```python
import anthropic
import json
import time
from pathlib import Path
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class JobProgress:
    job_id: str
    total: int
    completed_ids: list[str] = field(default_factory=list)
    results: dict = field(default_factory=dict)
    timing: list[float] = field(default_factory=list)  # seconds per item
    started_at: float = field(default_factory=time.time)
    error_count: int = 0

class CheckpointedJob:
    """
    Checkpointed job runner with progress reporting, ETA estimation,
    and per-item timing for performance analysis.
    """
    def __init__(self, job_id: str, items: list[dict]):
        self.items = items
        self.checkpoint_path = Path(f"/tmp/{job_id}.progress.json")
        self.progress = self._load_or_init(job_id, len(items))

    def _load_or_init(self, job_id: str, total: int) -> JobProgress:
        if self.checkpoint_path.exists():
            data = json.loads(self.checkpoint_path.read_text())
            p = JobProgress(**data)
            pct = len(p.completed_ids) / p.total * 100
            print(f"[resume] {len(p.completed_ids)}/{p.total} ({pct:.1f}%) already done")
            return p
        return JobProgress(job_id=job_id, total=total)

    def _save(self):
        import dataclasses
        tmp = self.checkpoint_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(self.progress), indent=2))
        tmp.rename(self.checkpoint_path)

    def _eta_string(self) -> str:
        if len(self.progress.timing) < 2:
            return "calculating..."
        avg_seconds = sum(self.progress.timing[-10:]) / len(self.progress.timing[-10:])
        remaining = self.progress.total - len(self.progress.completed_ids)
        eta_seconds = avg_seconds * remaining
        if eta_seconds < 60:
            return f"{eta_seconds:.0f}s"
        elif eta_seconds < 3600:
            return f"{eta_seconds/60:.1f}min"
        else:
            return f"{eta_seconds/3600:.1f}h"

    def run(self, process_fn) -> dict:
        done_ids = set(self.progress.completed_ids)

        for item in self.items:
            item_id = item["id"]
            if item_id in done_ids:
                continue

            t0 = time.time()
            try:
                result = process_fn(item)
                elapsed = time.time() - t0

                self.progress.completed_ids.append(item_id)
                self.progress.results[item_id] = result
                self.progress.timing.append(elapsed)
                self._save()

                done = len(self.progress.completed_ids)
                pct = done / self.progress.total * 100
                print(f"  [{done}/{self.progress.total} {pct:.1f}%] {item_id} ({elapsed:.2f}s) ETA: {self._eta_string()}")

            except Exception as e:
                self.progress.error_count += 1
                self._save()
                print(f"  [error] {item_id}: {e} (errors: {self.progress.error_count})")

        return self.progress.results


def llm_process(item: dict) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarize: {item['content'][:300]}"}]
    )
    return {"summary": response.content[0].text}


job = CheckpointedJob(
    job_id="doc_analysis_20240115",
    items=[{"id": f"doc_{i}", "content": f"Document content {i}"} for i in range(50)]
)
results = job.run(llm_process)
print(f"Done: {len(results)} results")
```

## Checkpoint Strategy Comparison

| Strategy | Best For | Durability | Distributed | Overhead |
|---|---|---|---|---|
| File-based (Option 1) | Simple sequential jobs | High (atomic rename) | No | Minimal |
| SQLite (Option 2) | Large jobs, retry logic | High (WAL mode) | No | Low |
| Multi-step pipeline (Option 3) | Stage-based workflows | High | No | Medium |
| Redis (Option 4) | Distributed workers | High (with persistence) | Yes | Medium |
| Idempotent IDs (Option 5) | Uncontrolled callers | High | Partial | Minimal |
| Progress + ETA (Option 6) | Long jobs needing monitoring | High | No | Low |

## Expected Token Savings
Resume from checkpoint saves all tokens already spent on completed items. A 1000-item job that fails at item 900 wastes 90% of its spend without checkpointing. With checkpointing, only the 100 remaining items need processing — 90% token recovery on restart.

## Environment
- Any agent processing > ~20 items in a loop; any multi-stage pipeline; any job running longer than 5 minutes; jobs with external dependencies (databases, APIs, file systems) that can fail mid-run; environments where process interruption is possible (serverless with timeouts, spot instances, operator restarts); the file-based approach (Option 1) is sufficient for single-machine sequential jobs; use Redis (Option 4) when multiple workers or horizontal scaling is needed
