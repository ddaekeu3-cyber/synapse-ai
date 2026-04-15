---
layout: solution
title: "Agent Doesn't Implement Progress Checkpointing for Long Tasks"
category: loop-stuck
description: "Agents that process large batches or multi-step pipelines without checkpointing lose all progress on failure and must restart from scratch — checkpointing lets them resume from the last saved state."
tags: [checkpointing, long-tasks, resumability, fault-tolerance, batch-processing, persistence]
---

# Agent Doesn't Implement Progress Checkpointing for Long Tasks

## Problem

A long-running agent processes 1,000 records, summarizes a 500-page document chapter by chapter, or runs a multi-day research pipeline. Without checkpointing, any failure — network error, context overflow, process crash, or token limit — forces a complete restart. The agent burns through tokens and time redoing completed work. With checkpointing, each completed step is persisted; the agent resumes from where it left off rather than from the beginning.

## Solutions

### Option 1: SQLite Checkpoint Store for Batch Processing

Persist per-item completion status in SQLite. On startup, skip already-completed items.

```python
import anthropic
import sqlite3
import json
import time
from datetime import datetime
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class CheckpointItem:
    item_id: str
    status: str  # "pending" | "in_progress" | "completed" | "failed"
    result: str | None = None
    attempts: int = 0
    last_updated: str = ""
    error: str | None = None

class BatchCheckpointer:
    def __init__(self, job_id: str, db_path: str = "checkpoints.db"):
        self.job_id = job_id
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                job_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                attempts INTEGER DEFAULT 0,
                error TEXT,
                last_updated TEXT,
                PRIMARY KEY (job_id, item_id)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS job_meta (
                job_id TEXT PRIMARY KEY,
                total_items INTEGER,
                started_at TEXT,
                resumed_at TEXT,
                completed_at TEXT,
                config TEXT
            )
        """)
        self.conn.commit()

    def register_items(self, item_ids: list[str], config: dict | None = None):
        """Register all items for this job. Safe to call multiple times (idempotent)."""
        now = datetime.now().isoformat()
        existing_ids = {
            r[0] for r in self.conn.execute(
                "SELECT item_id FROM checkpoints WHERE job_id = ?", (self.job_id,)
            ).fetchall()
        }
        new_ids = [i for i in item_ids if i not in existing_ids]
        self.conn.executemany(
            "INSERT INTO checkpoints (job_id, item_id, status, last_updated) VALUES (?, ?, 'pending', ?)",
            [(self.job_id, item_id, now) for item_id in new_ids]
        )
        # Upsert job meta
        self.conn.execute("""
            INSERT INTO job_meta (job_id, total_items, started_at, config)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                resumed_at = excluded.started_at
        """, (self.job_id, len(item_ids), now, json.dumps(config or {})))
        self.conn.commit()
        print(f"Job '{self.job_id}': {len(new_ids)} new items registered, {len(existing_ids)} already known")

    def get_pending(self, max_attempts: int = 3) -> list[str]:
        rows = self.conn.execute(
            "SELECT item_id FROM checkpoints WHERE job_id = ? AND status IN ('pending', 'in_progress') AND attempts < ?",
            (self.job_id, max_attempts)
        ).fetchall()
        return [r[0] for r in rows]

    def mark_in_progress(self, item_id: str):
        self.conn.execute(
            "UPDATE checkpoints SET status='in_progress', attempts=attempts+1, last_updated=? WHERE job_id=? AND item_id=?",
            (datetime.now().isoformat(), self.job_id, item_id)
        )
        self.conn.commit()

    def mark_completed(self, item_id: str, result: str):
        self.conn.execute(
            "UPDATE checkpoints SET status='completed', result=?, last_updated=? WHERE job_id=? AND item_id=?",
            (result, datetime.now().isoformat(), self.job_id, item_id)
        )
        self.conn.commit()

    def mark_failed(self, item_id: str, error: str):
        self.conn.execute(
            "UPDATE checkpoints SET status='failed', error=?, last_updated=? WHERE job_id=? AND item_id=?",
            (error, datetime.now().isoformat(), self.job_id, item_id)
        )
        self.conn.commit()

    def progress(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM checkpoints WHERE job_id=? GROUP BY status",
            (self.job_id,)
        ).fetchall()
        counts = dict(rows)
        total = sum(counts.values())
        completed = counts.get("completed", 0)
        return {
            "total": total,
            "completed": completed,
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "failed": counts.get("failed", 0),
            "pct": round(completed / max(total, 1) * 100, 1),
        }

def process_item(item_id: str, content: str) -> str:
    """Call the model to process one item."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": f"Summarize in one sentence: {content}"}],
    )
    return resp.content[0].text.strip()

def resumable_batch_job(
    job_id: str,
    items: dict[str, str],  # item_id → content
    db_path: str = ":memory:",
) -> dict:
    cp = BatchCheckpointer(job_id, db_path)
    cp.register_items(list(items.keys()), config={"model": "claude-haiku-4-5-20251001"})

    pending = cp.get_pending()
    print(f"Resuming: {len(pending)} pending items (skipping completed ones)")

    for item_id in pending:
        cp.mark_in_progress(item_id)
        try:
            result = process_item(item_id, items[item_id])
            cp.mark_completed(item_id, result)
            progress = cp.progress()
            print(f"  [{item_id}] done | {progress['pct']}% complete")
        except Exception as e:
            cp.mark_failed(item_id, str(e))
            print(f"  [{item_id}] FAILED: {e}")

    return cp.progress()

# Simulate a batch of items
items = {
    "doc_001": "Python is a high-level programming language known for its readability.",
    "doc_002": "Asyncio enables concurrent programming in Python using coroutines.",
    "doc_003": "FastAPI is a modern web framework for building APIs with Python.",
}

result = resumable_batch_job("summarize_job_v1", items)
print(f"\nFinal: {result}")
# Expected Token Savings: Zero re-processing of completed items on resume
# Environment: Batch summarization, data pipelines, nightly processing jobs
```

### Option 2: File-Based Checkpoint with Atomic Writes

For simpler deployments, persist checkpoints as JSON files with atomic writes to prevent corruption on crash.

```python
import anthropic
import json
import os
import tempfile
from pathlib import Path
from datetime import datetime

client = anthropic.Anthropic()

class FileCheckpointer:
    def __init__(self, checkpoint_dir: str, job_name: str):
        self.dir = Path(checkpoint_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file = self.dir / f"{job_name}.json"
        self.state = self._load()

    def _load(self) -> dict:
        if self.checkpoint_file.exists():
            try:
                data = json.loads(self.checkpoint_file.read_text())
                print(f"Resumed from checkpoint: {data.get('completed_count', 0)} items already done")
                return data
            except Exception:
                pass
        return {
            "completed": {},
            "failed": {},
            "completed_count": 0,
            "started_at": datetime.now().isoformat(),
        }

    def _save(self):
        """Atomic write: write to temp file then rename (prevents partial writes on crash)."""
        tmp = self.checkpoint_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.checkpoint_file)  # Atomic on POSIX systems

    def is_completed(self, item_id: str) -> bool:
        return item_id in self.state["completed"]

    def record_success(self, item_id: str, result: str):
        self.state["completed"][item_id] = {
            "result": result,
            "at": datetime.now().isoformat(),
        }
        self.state["completed_count"] = len(self.state["completed"])
        self._save()

    def record_failure(self, item_id: str, error: str):
        self.state["failed"][item_id] = {
            "error": error,
            "at": datetime.now().isoformat(),
        }
        self._save()

    def get_result(self, item_id: str) -> str | None:
        entry = self.state["completed"].get(item_id)
        return entry["result"] if entry else None

    def summary(self) -> dict:
        return {
            "completed": len(self.state["completed"]),
            "failed": len(self.state["failed"]),
            "started_at": self.state.get("started_at"),
        }

def resumable_pipeline(items: list[dict], checkpoint_dir: str = "/tmp/agent_checkpoints") -> list[dict]:
    cp = FileCheckpointer(checkpoint_dir, "pipeline_v1")
    results = []
    skipped = 0

    for item in items:
        item_id = item["id"]

        # Skip already-completed items
        if cp.is_completed(item_id):
            results.append({"id": item_id, "result": cp.get_result(item_id), "cached": True})
            skipped += 1
            continue

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": f"Classify sentiment: '{item['text']}'. Reply: positive/negative/neutral"}],
            )
            result = resp.content[0].text.strip()
            cp.record_success(item_id, result)
            results.append({"id": item_id, "result": result, "cached": False})
            print(f"  [{item_id}] {result}")
        except Exception as e:
            cp.record_failure(item_id, str(e))
            results.append({"id": item_id, "result": None, "error": str(e)})

    print(f"\nSkipped (already done): {skipped} | Processed now: {len(items) - skipped}")
    print(f"Checkpoint: {cp.summary()}")
    return results

items = [
    {"id": "r001", "text": "This product is amazing!"},
    {"id": "r002", "text": "Terrible experience, very disappointed."},
    {"id": "r003", "text": "It was okay, nothing special."},
]

results = resumable_pipeline(items)
# Run again to show resume behavior — all items will be skipped
print("\n--- Second run (simulating resume after crash) ---")
results2 = resumable_pipeline(items)
# Expected Token Savings: 100% for completed items on resume
# Environment: Sentiment analysis, classification pipelines, scripts without database deps
```

### Option 3: Step-Level Checkpointing for Multi-Step Workflows

In a multi-step pipeline (plan → research → write → review), checkpoint after each step so partial progress is never lost.

```python
import anthropic
import json
import time
from pathlib import Path
from datetime import datetime
from enum import Enum

client = anthropic.Anthropic()

class Step(str, Enum):
    PLAN     = "plan"
    RESEARCH = "research"
    DRAFT    = "draft"
    REVIEW   = "review"
    DONE     = "done"

STEP_ORDER = [Step.PLAN, Step.RESEARCH, Step.DRAFT, Step.REVIEW, Step.DONE]

def next_step(current: Step) -> Step | None:
    idx = STEP_ORDER.index(current)
    if idx + 1 < len(STEP_ORDER):
        return STEP_ORDER[idx + 1]
    return None

class WorkflowCheckpoint:
    def __init__(self, workflow_id: str, checkpoint_path: str = "/tmp"):
        self.workflow_id = workflow_id
        self.path = Path(checkpoint_path) / f"workflow_{workflow_id}.json"
        self.state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            current_step = data.get("current_step", Step.PLAN)
            print(f"Resuming workflow '{self.workflow_id}' from step: {current_step}")
            return data
        return {
            "workflow_id": self.workflow_id,
            "current_step": Step.PLAN,
            "steps": {},
            "started_at": datetime.now().isoformat(),
        }

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    def is_step_done(self, step: Step) -> bool:
        return step in self.state["steps"]

    def get_step_result(self, step: Step) -> str | None:
        return self.state["steps"].get(step, {}).get("result")

    def complete_step(self, step: Step, result: str):
        self.state["steps"][step] = {
            "result": result,
            "completed_at": datetime.now().isoformat(),
        }
        # Advance current step pointer
        n = next_step(step)
        if n:
            self.state["current_step"] = n
        self._save()
        print(f"  ✓ Step '{step}' completed and checkpointed")

    def current_step(self) -> Step:
        return Step(self.state.get("current_step", Step.PLAN))

def run_step(step: Step, topic: str, prior_context: str = "") -> str:
    prompts = {
        Step.PLAN: f"Create a 3-point outline for an article about: {topic}",
        Step.RESEARCH: f"Given this outline:\n{prior_context}\nList 3 key facts to support each point.",
        Step.DRAFT: f"Write a 3-sentence article draft based on:\n{prior_context}",
        Step.REVIEW: f"Review this draft and suggest one improvement:\n{prior_context}",
    }
    prompt = prompts.get(step, f"Process: {topic}")
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()

def resumable_workflow(topic: str, workflow_id: str, checkpoint_path: str = "/tmp") -> dict:
    cp = WorkflowCheckpoint(workflow_id, checkpoint_path)
    steps_run = []

    for step in [Step.PLAN, Step.RESEARCH, Step.DRAFT, Step.REVIEW]:
        if cp.is_step_done(step):
            print(f"  ↷ Step '{step}' already done — skipping")
            continue

        # Build context from previous steps
        prior = cp.get_step_result(Step.PLAN) or ""
        if step in [Step.RESEARCH, Step.DRAFT, Step.REVIEW]:
            last_done = [s for s in STEP_ORDER if cp.is_step_done(s)]
            if last_done:
                prior = cp.get_step_result(last_done[-1]) or ""

        result = run_step(step, topic, prior)
        cp.complete_step(step, result)
        steps_run.append(step)

    return {
        "topic": topic,
        "steps_executed_this_run": steps_run,
        "final_output": cp.get_step_result(Step.REVIEW) or cp.get_step_result(Step.DRAFT),
        "all_steps": {s: cp.get_step_result(s) for s in [Step.PLAN, Step.RESEARCH, Step.DRAFT, Step.REVIEW]},
    }

result = resumable_workflow("Python async programming", "article_async_001")
print(f"\nSteps run this time: {result['steps_executed_this_run']}")
print(f"Final output: {result['final_output'][:200]}")

# Second run simulates resume — all steps already done
print("\n--- Resume run ---")
result2 = resumable_workflow("Python async programming", "article_async_001")
print(f"Steps run this time: {result2['steps_executed_this_run']} (empty = all resumed from checkpoint)")
# Expected Token Savings: Skip all completed steps on resume — can be 100% if only last step failed
# Environment: Multi-step content generation, research pipelines, document drafting agents
```

### Option 4: Context-Overflow Safe Checkpointing

Automatically checkpoint before the context window fills, then start a fresh session that loads the checkpoint as its initial context.

```python
import anthropic
import json
from datetime import datetime
from pathlib import Path

client = anthropic.Anthropic()

CONTEXT_LIMIT_TOKENS = 200_000  # claude-sonnet-4-6 context limit
CHECKPOINT_THRESHOLD = 0.75     # Save checkpoint when 75% full

def count_tokens_approx(messages: list[dict]) -> int:
    """Rough estimate: 1 token ≈ 4 chars."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(str(block.get("text", ""))) // 4
    return total

class ContextCheckpointer:
    def __init__(self, session_id: str, checkpoint_dir: str = "/tmp"):
        self.session_id = session_id
        self.dir = Path(checkpoint_dir)
        self.dir.mkdir(exist_ok=True)
        self.checkpoint_file = self.dir / f"session_{session_id}.json"

    def save(self, messages: list[dict], summary: str, turn_count: int):
        data = {
            "session_id": self.session_id,
            "summary": summary,
            "last_messages": messages[-4:],  # Keep last 4 messages for continuity
            "turn_count": turn_count,
            "saved_at": datetime.now().isoformat(),
        }
        tmp = self.checkpoint_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.checkpoint_file)
        print(f"  Checkpoint saved: {turn_count} turns, summary={len(summary)} chars")

    def load(self) -> dict | None:
        if self.checkpoint_file.exists():
            return json.loads(self.checkpoint_file.read_text())
        return None

def summarize_conversation(messages: list[dict]) -> str:
    """Compress conversation history into a dense summary."""
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content'][:200] if isinstance(m['content'], str) else '[complex content]'}"
        for m in messages[-10:]
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"Summarize this conversation preserving all key decisions, facts, and context:\n\n{transcript}"
        }]
    )
    return resp.content[0].text

def long_running_chat(session_id: str, initial_topic: str, n_turns: int = 10) -> dict:
    cp = ContextCheckpointer(session_id)
    checkpoint_data = cp.load()

    # Restore from checkpoint or start fresh
    if checkpoint_data:
        system = (
            f"You are continuing a long conversation. Summary of what happened:\n\n"
            f"{checkpoint_data['summary']}\n\n"
            f"Resume from where we left off."
        )
        messages = checkpoint_data["last_messages"]
        turn_offset = checkpoint_data["turn_count"]
        print(f"Resumed from checkpoint at turn {turn_offset}")
    else:
        system = f"You are helping with: {initial_topic}"
        messages = []
        turn_offset = 0

    for turn in range(n_turns):
        actual_turn = turn_offset + turn + 1
        user_msg = f"Turn {actual_turn}: continue exploring {initial_topic}, aspect {actual_turn}."

        messages.append({"role": "user", "content": user_msg})

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=messages,
        )
        reply = resp.content[0].text
        messages.append({"role": "assistant", "content": reply})

        # Check if we're approaching context limit
        estimated_tokens = count_tokens_approx(messages)
        fill_pct = estimated_tokens / CONTEXT_LIMIT_TOKENS

        if fill_pct > CHECKPOINT_THRESHOLD:
            summary = summarize_conversation(messages)
            cp.save(messages, summary, actual_turn)
            # Start fresh context with summary
            system = f"You are continuing a long conversation.\n\nSummary:\n{summary}"
            messages = messages[-2:]  # Keep last exchange for continuity
            print(f"  Context refreshed at turn {actual_turn} ({fill_pct:.0%} full)")

    return {
        "turns_completed": n_turns,
        "final_turn": turn_offset + n_turns,
        "last_reply": reply[:100],
    }

result = long_running_chat("async_session_001", "Python async programming patterns", n_turns=5)
print(f"Completed {result['turns_completed']} turns")
# Expected Token Savings: Prevents context overflow that forces full restart
# Environment: Long-running research sessions, document review, extended coding assistance
```

### Option 5: Idempotent Tool Calls with Result Caching

Cache tool call results so that if an agent re-runs (after failure), it doesn't re-execute expensive external tool calls.

```python
import anthropic
import json
import hashlib
import sqlite3
import time
from datetime import datetime

client = anthropic.Anthropic()

class ToolResultCache:
    """Cache tool call results for idempotent replay."""

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_cache (
                cache_key TEXT PRIMARY KEY,
                tool_name TEXT,
                args_json TEXT,
                result TEXT,
                cached_at TEXT,
                hits INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()

    def _make_key(self, tool_name: str, args: dict) -> str:
        canonical = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def get(self, tool_name: str, args: dict) -> str | None:
        key = self._make_key(tool_name, args)
        row = self.conn.execute(
            "SELECT result FROM tool_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row:
            self.conn.execute("UPDATE tool_cache SET hits = hits + 1 WHERE cache_key = ?", (key,))
            self.conn.commit()
            return row[0]
        return None

    def set(self, tool_name: str, args: dict, result: str):
        key = self._make_key(tool_name, args)
        self.conn.execute("""
            INSERT OR REPLACE INTO tool_cache (cache_key, tool_name, args_json, result, cached_at)
            VALUES (?, ?, ?, ?, ?)
        """, (key, tool_name, json.dumps(args), result, datetime.now().isoformat()))
        self.conn.commit()

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT COUNT(*), SUM(hits) FROM tool_cache"
        ).fetchone()
        return {"entries": rows[0], "total_cache_hits": rows[1] or 0}

# Simulate expensive tools
def slow_search(query: str) -> str:
    time.sleep(0.1)  # Simulate network latency
    return f"Results for '{query}': [result_A, result_B, result_C]"

def expensive_analysis(data: str) -> str:
    time.sleep(0.1)
    return f"Analysis of '{data[:20]}...': sentiment=positive, confidence=0.92"

TOOLS = [
    {
        "name": "search",
        "description": "Search for information.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "analyze",
        "description": "Analyze data.",
        "input_schema": {"type": "object", "properties": {"data": {"type": "string"}}, "required": ["data"]},
    },
]

cache = ToolResultCache()

def execute_tool_cached(name: str, args: dict) -> str:
    cached = cache.get(name, args)
    if cached:
        print(f"  CACHE HIT: {name}({args})")
        return cached

    print(f"  CACHE MISS: executing {name}({args})")
    if name == "search":
        result = slow_search(args.get("query", ""))
    elif name == "analyze":
        result = expensive_analysis(args.get("data", ""))
    else:
        result = json.dumps({"error": "unknown tool"})

    cache.set(name, args, result)
    return result

def agent_with_tool_cache(question: str) -> str:
    messages = [{"role": "user", "content": question}]

    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "")

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = execute_tool_cached(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": results})

q = "Search for Python async patterns and analyze the results."
print("=== First run ===")
answer1 = agent_with_tool_cache(q)
print(f"Answer: {answer1[:100]}")

print("\n=== Resume run (same tools, cache hits) ===")
answer2 = agent_with_tool_cache(q)
print(f"Answer: {answer2[:100]}")
print(f"\nCache stats: {cache.stats()}")
# Expected Token Savings: No re-execution of expensive tools on retry/resume
# Environment: Multi-step research agents, ETL pipelines, agents with rate-limited tools
```

### Option 6: Checkpoint-Aware Progress Dashboard

Display real-time progress and ETA for long-running jobs. Helps operators decide whether to wait, restart, or investigate.

```python
import anthropic
import json
import time
import math
from datetime import datetime, timedelta
from pathlib import Path

client = anthropic.Anthropic()

class ProgressDashboard:
    def __init__(self, job_id: str, total_items: int):
        self.job_id = job_id
        self.total = total_items
        self.completed = 0
        self.failed = 0
        self.start_time = time.monotonic()
        self.completion_times: list[float] = []
        self.checkpoint_path = Path(f"/tmp/job_{job_id}_progress.json")

    def record_completion(self, elapsed: float, success: bool):
        if success:
            self.completed += 1
            self.completion_times.append(elapsed)
        else:
            self.failed += 1
        self._save()

    def _save(self):
        data = {
            "job_id": self.job_id,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "updated_at": datetime.now().isoformat(),
        }
        tmp = self.checkpoint_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.checkpoint_path)

    @property
    def eta_seconds(self) -> float | None:
        if len(self.completion_times) < 2:
            return None
        avg_per_item = sum(self.completion_times) / len(self.completion_times)
        remaining = self.total - self.completed - self.failed
        return avg_per_item * remaining

    def render(self) -> str:
        processed = self.completed + self.failed
        pct = processed / max(self.total, 1) * 100
        bar_len = 30
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)

        elapsed = time.monotonic() - self.start_time
        eta = self.eta_seconds
        eta_str = f"{timedelta(seconds=int(eta))}" if eta else "calculating..."

        return (
            f"Job: {self.job_id}\n"
            f"[{bar}] {pct:.1f}%\n"
            f"Completed: {self.completed}/{self.total} | Failed: {self.failed}\n"
            f"Elapsed: {timedelta(seconds=int(elapsed))} | ETA: {eta_str}"
        )

def run_with_dashboard(job_id: str, items: list[dict]) -> dict:
    # Load existing progress (resume support)
    checkpoint = Path(f"/tmp/job_{job_id}_progress.json")
    completed_ids: set[str] = set()

    if checkpoint.exists():
        prev = json.loads(checkpoint.read_text())
        print(f"Found existing job: {prev.get('completed', 0)} already done")
        # In real use, store completed IDs and skip them
        # For demo, we just track counts

    dashboard = ProgressDashboard(job_id, len(items))
    results = {}

    for item in items:
        item_id = item["id"]
        if item_id in completed_ids:
            dashboard.completed += 1
            continue

        t0 = time.monotonic()
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=50,
                messages=[{"role": "user", "content": f"Score sentiment 1-5: '{item['text']}'. Reply with number only."}]
            )
            result = resp.content[0].text.strip()
            results[item_id] = result
            dashboard.record_completion(time.monotonic() - t0, success=True)
        except Exception as e:
            results[item_id] = None
            dashboard.record_completion(time.monotonic() - t0, success=False)

        # Print dashboard every item
        print(f"\r{dashboard.render()}", end="", flush=True)

    print(f"\n\n{dashboard.render()}")
    return {"results": results, "completed": dashboard.completed, "failed": dashboard.failed}

items = [
    {"id": f"item_{i:03d}", "text": f"Sample text number {i} for sentiment analysis"}
    for i in range(5)
]

result = run_with_dashboard("sentiment_job_001", items)
print(f"\nResults: {result['completed']} done, {result['failed']} failed")
# Expected Token Savings: Dashboard prevents premature restarts that waste completed work
# Environment: Long batch jobs, overnight processing, multi-hour agent tasks
```

## Comparison Table

| Option | Persistence | Resume Granularity | Overhead | Best For |
|--------|------------|-------------------|----------|----------|
| 1: SQLite Batch Checkpointer | SQLite | Per item | DB write/read | Large batch processing jobs |
| 2: Atomic File Checkpoint | JSON file | Per item | File I/O | Simple scripts, no DB dependency |
| 3: Step-Level Checkpointing | JSON file | Per workflow step | File I/O | Multi-step pipeline workflows |
| 4: Context-Overflow Safe | JSON file | Per N turns | Summarization API call | Long multi-turn conversations |
| 5: Tool Result Caching | SQLite | Per tool call | DB write/read | Agents with expensive/rate-limited tools |
| 6: Progress Dashboard | JSON file | Per item + ETA | File I/O | Operator visibility, overnight jobs |
