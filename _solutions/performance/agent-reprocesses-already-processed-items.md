---
layout: solution
title: "Agent Reprocesses Already-Processed Items in Batch — Wasted Compute"
category: performance
description: "Agent runs a batch job over 10,000 items. On restart (after crash or timeout), it starts over from item 1. All previously processed items are re-run. A 6-hour job must restart from scratch every time it fails."
tags: [batch, checkpoint, idempotency, restart, resume, progress, performance, deduplication]
---

# Agent Reprocesses Already-Processed Items in Batch — Wasted Compute

## Symptom
- Batch job fails at item 8,000 of 10,000 — restart means 8,000 items re-processed
- Agent emails all users again because it restarted mid-batch
- Daily job takes 6 hours; any failure means 6 more hours to restart
- No way to resume from where the job left off
- Restarted job creates duplicate records for already-processed items
- `processed_count` resets to 0 on every restart

## Root Cause
Batch jobs without checkpointing are all-or-nothing: they succeed completely or restart from the beginning. Without persisting which items have been processed, a restart replays everything. This is expensive for long-running jobs and dangerous if processing has side effects (emails, charges, database writes).

## Fix

### Option 1: SQLite checkpoint — track processed items by ID

```python
import sqlite3
from pathlib import Path
from datetime import datetime

class BatchCheckpoint:
    """
    SQLite-based checkpoint for batch jobs.
    Tracks which item IDs have been successfully processed.
    Survives crashes, restarts, and partial failures.
    """

    def __init__(self, job_name: str, db_path: str = "batch_checkpoints.db"):
        self.job_name = job_name
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS processed_items (
                job_name TEXT NOT NULL,
                item_id TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                status TEXT DEFAULT 'success',
                PRIMARY KEY (job_name, item_id)
            )
        """)
        self.db.commit()

    def is_done(self, item_id: str) -> bool:
        """Check if this item was already successfully processed"""
        row = self.db.execute(
            "SELECT status FROM processed_items WHERE job_name=? AND item_id=? AND status='success'",
            (self.job_name, str(item_id))
        ).fetchone()
        return row is not None

    def mark_done(self, item_id: str, status: str = "success"):
        self.db.execute("""
            INSERT OR REPLACE INTO processed_items (job_name, item_id, processed_at, status)
            VALUES (?, ?, ?, ?)
        """, (self.job_name, str(item_id), datetime.utcnow().isoformat(), status))
        self.db.commit()

    def stats(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*), SUM(status='success'), SUM(status='failed') "
            "FROM processed_items WHERE job_name=?",
            (self.job_name,)
        ).fetchone()
        return {"total": row[0], "success": row[1] or 0, "failed": row[2] or 0}

checkpoint = BatchCheckpoint("daily_user_export")

async def run_batch_with_checkpoint(items: list, process_fn) -> dict:
    """Process items, skipping already-done ones on restart"""
    stats = checkpoint.stats()
    print(f"Resuming: {stats['success']} already done, {stats['failed']} failed")

    skipped = success = failed = 0
    for item in items:
        item_id = str(item["id"])

        if checkpoint.is_done(item_id):
            skipped += 1
            continue  # Skip — already processed successfully

        try:
            await process_fn(item)
            checkpoint.mark_done(item_id, "success")
            success += 1
        except Exception as e:
            print(f"Failed item {item_id}: {e}")
            checkpoint.mark_done(item_id, "failed")
            failed += 1

    return {"skipped": skipped, "success": success, "failed": failed}
```

### Option 2: File-based checkpoint for simple batch scripts

```python
import json
from pathlib import Path

class FileCheckpoint:
    """
    Simple file-based checkpoint — stores processed IDs in a JSON set.
    Good for single-machine batch scripts without a database.
    """

    def __init__(self, checkpoint_path: str):
        self.path = Path(checkpoint_path)
        self._done: set[str] = set()
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self._done = set(data.get("done", []))
            print(f"Checkpoint loaded: {len(self._done)} items already processed")

    def is_done(self, item_id: str) -> bool:
        return str(item_id) in self._done

    def mark_done(self, item_id: str):
        self._done.add(str(item_id))
        self._save()

    def _save(self):
        self.path.write_text(json.dumps({"done": list(self._done)}, indent=2))

cp = FileCheckpoint("job_progress.json")

for item in all_items:
    if cp.is_done(item["id"]):
        continue  # Skip — resume after crash
    process(item)
    cp.mark_done(item["id"])

# On crash at item 5000:
# Restart → loads job_progress.json → skips first 5000 → resumes at 5001
```

### Option 3: Paginated job with offset persistence

```python
import json
from pathlib import Path

class PaginatedBatchRunner:
    """
    Process items in pages, persisting the last completed page offset.
    On restart, continues from the last completed page.
    """

    def __init__(self, job_name: str, page_size: int = 100):
        self.job_name = job_name
        self.page_size = page_size
        self.state_path = Path(f".{job_name}_state.json")
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            print(f"Resuming from offset {state['offset']} ({state['processed']} processed)")
            return state
        return {"offset": 0, "processed": 0, "completed": False}

    def _save_state(self):
        self.state_path.write_text(json.dumps(self._state, indent=2))

    async def run(self, fetch_page_fn, process_item_fn) -> dict:
        """
        fetch_page_fn(offset, limit) → list of items
        process_item_fn(item) → None
        """
        if self._state.get("completed"):
            print(f"Job '{self.job_name}' already completed. Delete state file to re-run.")
            return self._state

        while True:
            offset = self._state["offset"]
            page = await fetch_page_fn(offset, self.page_size)

            if not page:
                # No more items
                self._state["completed"] = True
                self._save_state()
                print(f"Job complete. Processed {self._state['processed']} items.")
                return self._state

            for item in page:
                await process_item_fn(item)
                self._state["processed"] += 1

            # Advance offset only after full page is processed
            self._state["offset"] = offset + len(page)
            self._save_state()
            print(f"Page done. Offset: {self._state['offset']}, Total: {self._state['processed']}")

runner = PaginatedBatchRunner("user_migration", page_size=200)
await runner.run(
    fetch_page_fn=lambda offset, limit: db.query("SELECT * FROM users LIMIT ? OFFSET ?", limit, offset),
    process_item_fn=migrate_user
)
```

### Option 4: Redis-based distributed checkpoint for parallel workers

```python
import redis

redis_client = redis.Redis()

class RedisCheckpoint:
    """
    Distributed checkpoint for parallel batch workers.
    Multiple workers can process different items without duplication.
    """

    def __init__(self, job_name: str):
        self.done_key = f"batch:{job_name}:done"
        self.failed_key = f"batch:{job_name}:failed"
        self.claimed_key = f"batch:{job_name}:claimed"

    def claim(self, item_id: str) -> bool:
        """Atomically claim an item for processing. Returns True if claimed by this worker."""
        return bool(redis_client.sadd(self.claimed_key, str(item_id)))

    def mark_done(self, item_id: str):
        redis_client.sadd(self.done_key, str(item_id))
        redis_client.srem(self.claimed_key, str(item_id))

    def mark_failed(self, item_id: str):
        redis_client.sadd(self.failed_key, str(item_id))
        redis_client.srem(self.claimed_key, str(item_id))

    def is_done(self, item_id: str) -> bool:
        return bool(redis_client.sismember(self.done_key, str(item_id)))

    def pending_items(self, all_ids: list[str]) -> list[str]:
        """Return items not yet processed or claimed"""
        done = redis_client.smembers(self.done_key)
        claimed = redis_client.smembers(self.claimed_key)
        skip = done | claimed
        return [i for i in all_ids if str(i).encode() not in skip]

    def stats(self) -> dict:
        return {
            "done": redis_client.scard(self.done_key),
            "failed": redis_client.scard(self.failed_key),
            "in_progress": redis_client.scard(self.claimed_key),
        }

cp = RedisCheckpoint("nightly_sync")

async def worker(items: list):
    for item in items:
        if not cp.claim(str(item["id"])):
            continue  # Another worker claimed this item
        try:
            await process(item)
            cp.mark_done(str(item["id"]))
        except Exception as e:
            cp.mark_failed(str(item["id"]))
```

### Option 5: Streaming processor with automatic deduplication

```python
from datetime import datetime

class DeduplicatingProcessor:
    """
    Process items exactly once by tracking a content hash of each item.
    Content-addressed: same item content = same hash = skip.
    """

    def __init__(self, job_name: str):
        self.checkpoint = BatchCheckpoint(job_name)

    def _content_hash(self, item: dict) -> str:
        import hashlib, json
        # Hash the item content — same data = same hash
        content = json.dumps(item, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def process_stream(self, items, process_fn) -> dict:
        """
        Process a stream of items — skip duplicates by content hash.
        Useful when items don't have stable IDs.
        """
        processed = skipped = failed = 0

        for item in items:
            item_hash = self._content_hash(item)

            if self.checkpoint.is_done(item_hash):
                skipped += 1
                continue

            try:
                await process_fn(item)
                self.checkpoint.mark_done(item_hash)
                processed += 1
            except Exception as e:
                print(f"Failed {item_hash}: {e}")
                self.checkpoint.mark_done(item_hash, "failed")
                failed += 1

            # Progress logging
            if (processed + skipped + failed) % 100 == 0:
                print(f"Progress: {processed} done, {skipped} skipped, {failed} failed")

        return {"processed": processed, "skipped": skipped, "failed": failed}
```

## Checkpoint Strategy Comparison

| Strategy | Overhead | Crash safety | Parallel safe | Best for |
|---|---|---|---|---|
| SQLite checkpoint | Low | High | No (WAL mode helps) | Single-machine batch |
| File-based JSON | Minimal | Medium | No | Simple scripts |
| Paginated offset | Low | Medium | No | API pagination |
| Redis set | Low | High | Yes | Distributed workers |
| Content hash | Low | High | Yes | No stable IDs |

## Expected Token Savings
6-hour job restarts from scratch after crash = 6 more hours × 3 restarts: ~180,000+ tokens
Checkpoint resumes from failure point: 0 reprocessing

## Environment
- Any agent running batch jobs over large datasets; critical for nightly pipelines, migration scripts, and multi-hour processing jobs
- Source: direct experience; batch restart without checkpointing is the most expensive recoverable failure in agent pipelines
