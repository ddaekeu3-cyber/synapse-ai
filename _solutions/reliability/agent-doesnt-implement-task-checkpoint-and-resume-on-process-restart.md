---
layout: solution
title: "Agent Doesn't Implement Task Checkpoint and Resume on Process Restart"
category: reliability
description: "Persist task state at key milestones so that if the agent process crashes or is restarted, it can resume from the last checkpoint rather than starting over from scratch."
tags: [reliability, checkpoint, resume, persistence, crash-recovery, long-running, fault-tolerance]
---

# Agent Doesn't Implement Task Checkpoint and Resume on Process Restart

## Problem

Long-running agent tasks — multi-step pipelines, batch processing jobs, complex reasoning chains — are vulnerable to process crashes, container restarts, OOM kills, and planned maintenance. Without checkpointing, every restart means starting over from scratch: re-spending tokens, re-calling external APIs, re-processing already-completed steps, and leaving users waiting. With checkpointing, a restart becomes a resume.

## Solutions

### Option 1: File-Based JSON Checkpoint per Task Step

Save a JSON checkpoint file after each completed step; on startup, detect and resume from the latest checkpoint.

```python
import anthropic
import json
import os
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict

client = anthropic.Anthropic()

CHECKPOINT_DIR = Path("/tmp/agent_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)


@dataclass
class TaskCheckpoint:
    task_id: str
    completed_steps: list[str]
    step_results: dict[str, str]
    current_step: int
    total_steps: int
    started_at: float
    updated_at: float = field(default_factory=time.time)
    status: str = "in_progress"   # in_progress | complete | failed

    def save(self) -> None:
        self.updated_at = time.time()
        path = CHECKPOINT_DIR / f"{self.task_id}.json"
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, task_id: str) -> "TaskCheckpoint | None":
        path = CHECKPOINT_DIR / f"{task_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return cls(**data)

    def mark_complete(self) -> None:
        self.status = "complete"
        self.save()

    def is_step_done(self, step_name: str) -> bool:
        return step_name in self.completed_steps

    def record_step(self, step_name: str, result: str) -> None:
        self.completed_steps.append(step_name)
        self.step_results[step_name] = result
        self.current_step += 1
        self.save()


def process_step(step_name: str, prompt: str, prior_results: dict) -> str:
    context = "\n".join(f"{k}: {v[:100]}" for k, v in prior_results.items())
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Prior results:\n{context}\n\nStep: {prompt}",
        }],
    )
    return resp.content[0].text


def run_pipeline(task_id: str, steps: list[tuple[str, str]]) -> dict:
    """Run a multi-step pipeline, resuming from checkpoint if available."""
    checkpoint = TaskCheckpoint.load(task_id)
    if checkpoint:
        print(f"[{task_id}] Resuming from step {checkpoint.current_step}/{len(steps)}")
    else:
        checkpoint = TaskCheckpoint(
            task_id=task_id,
            completed_steps=[],
            step_results={},
            current_step=0,
            total_steps=len(steps),
            started_at=time.time(),
        )
        print(f"[{task_id}] Starting fresh pipeline ({len(steps)} steps)")

    for step_name, prompt in steps:
        if checkpoint.is_step_done(step_name):
            print(f"  [SKIP] {step_name} (already completed)")
            continue

        print(f"  [RUN ] {step_name}")
        result = process_step(step_name, prompt, checkpoint.step_results)
        checkpoint.record_step(step_name, result)
        print(f"  [DONE] {step_name}: {result[:60]}...")

    checkpoint.mark_complete()
    return checkpoint.step_results


if __name__ == "__main__":
    TASK_ID = "research_pipeline_001"
    STEPS = [
        ("gather_facts",    "List 3 key facts about photosynthesis."),
        ("analyze",         "Analyze the efficiency of photosynthesis vs solar panels."),
        ("summarize",       "Write a one-paragraph summary of the analysis."),
        ("recommendations", "Give 2 research recommendations based on the analysis."),
    ]

    results = run_pipeline(TASK_ID, STEPS)
    print(f"\nFinal results keys: {list(results.keys())}")

    # simulate a re-run (all steps already checkpointed — should skip all)
    print("\n--- Simulating restart ---")
    results2 = run_pipeline(TASK_ID, STEPS)
    print("All steps were skipped (resumed from checkpoint).")

    # cleanup
    (CHECKPOINT_DIR / f"{TASK_ID}.json").unlink(missing_ok=True)

# Expected Token Savings: On restart, skips all completed steps — saves 100% of already-spent tokens
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: SQLite-Backed Checkpoint Store with Transaction Safety

Use SQLite for atomic checkpoint writes, supporting concurrent agents and safe power-loss recovery.

```python
import anthropic
import sqlite3
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

client = anthropic.Anthropic()

DB_PATH = "/tmp/agent_tasks.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_steps (
                task_id     TEXT NOT NULL,
                step_name   TEXT NOT NULL,
                step_index  INTEGER NOT NULL,
                result      TEXT,
                completed   INTEGER DEFAULT 0,
                completed_at REAL,
                PRIMARY KEY (task_id, step_name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id     TEXT PRIMARY KEY,
                status      TEXT DEFAULT 'in_progress',
                created_at  REAL,
                updated_at  REAL,
                metadata    TEXT
            )
        """)


init_db()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_task(task_id: str, steps: list[str], metadata: dict = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(task_id, created_at, updated_at, metadata) VALUES(?,?,?,?)",
            (task_id, time.time(), time.time(), json.dumps(metadata or {})),
        )
        for i, name in enumerate(steps):
            conn.execute(
                "INSERT OR IGNORE INTO task_steps(task_id, step_name, step_index) VALUES(?,?,?)",
                (task_id, name, i),
            )


def get_pending_steps(task_id: str) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT step_name, step_index FROM task_steps WHERE task_id=? AND completed=0 ORDER BY step_index",
            (task_id,),
        ).fetchall()


def get_step_results(task_id: str) -> dict[str, str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT step_name, result FROM task_steps WHERE task_id=? AND completed=1",
            (task_id,),
        ).fetchall()
    return {r["step_name"]: r["result"] for r in rows}


def save_step(task_id: str, step_name: str, result: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE task_steps SET completed=1, result=?, completed_at=? WHERE task_id=? AND step_name=?",
            (result, time.time(), task_id, step_name),
        )
        conn.execute(
            "UPDATE tasks SET updated_at=? WHERE task_id=?",
            (time.time(), task_id),
        )


def mark_task_done(task_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE tasks SET status='complete', updated_at=? WHERE task_id=?",
            (time.time(), task_id),
        )


def run_pipeline(task_id: str, steps: list[tuple[str, str]]) -> dict[str, str]:
    step_names = [s[0] for s in steps]
    step_map   = dict(steps)
    ensure_task(task_id, step_names)

    pending = get_pending_steps(task_id)
    completed = get_step_results(task_id)

    if not pending:
        print(f"[{task_id}] All steps already complete — nothing to do")
        return completed

    skipped = len(step_names) - len(pending)
    if skipped:
        print(f"[{task_id}] Resuming: {skipped} steps already done, {len(pending)} remaining")

    for row in pending:
        step_name = row["step_name"]
        prompt    = step_map[step_name]
        context   = "\n".join(f"{k}: {v[:80]}" for k, v in completed.items())

        print(f"  [RUN ] {step_name}")
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Context:\n{context}\n\nTask: {prompt}"}],
        )
        result = resp.content[0].text
        save_step(task_id, step_name, result)
        completed[step_name] = result
        print(f"  [DONE] {step_name}: {result[:60]}...")

    mark_task_done(task_id)
    return completed


if __name__ == "__main__":
    TASK_ID = "analysis_" + uuid.uuid4().hex[:8]
    STEPS = [
        ("research",   "List 3 facts about neural networks."),
        ("critique",   "Identify limitations of neural networks."),
        ("conclusion", "Summarize in 2 sentences."),
    ]
    results = run_pipeline(TASK_ID, STEPS)
    print(f"\nAll steps complete: {list(results.keys())}")

    # re-run same task — should skip all steps
    print("\n--- Restart simulation ---")
    results2 = run_pipeline(TASK_ID, STEPS)

# Expected Token Savings: SQLite atomicity prevents partial checkpoint corruption on crash
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Async Checkpoint with Conversation History Replay

Checkpoint not just step results but the full conversation history, enabling exact replay of the agent's reasoning state.

```python
import anthropic
import asyncio
import json
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict

client = anthropic.AsyncAnthropic()

CHECKPOINT_DIR = Path("/tmp/conv_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)


@dataclass
class ConversationCheckpoint:
    task_id: str
    messages: list[dict]
    tool_results_cache: dict[str, str]
    turn_count: int
    status: str = "in_progress"
    saved_at: float = field(default_factory=time.time)

    def save(self) -> None:
        self.saved_at = time.time()
        path = CHECKPOINT_DIR / f"{self.task_id}.json"
        path.write_text(json.dumps(asdict(self)))

    @classmethod
    def load(cls, task_id: str) -> "ConversationCheckpoint | None":
        path = CHECKPOINT_DIR / f"{self.task_id}.json" if False else CHECKPOINT_DIR / f"{task_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return cls(**data)

    def add_message(self, role: str, content) -> None:
        self.messages.append({"role": role, "content": content if isinstance(content, str) else str(content)})
        self.turn_count += 1
        self.save()

    def cache_tool_result(self, tool_id: str, result: str) -> None:
        self.tool_results_cache[tool_id] = result
        self.save()


TOOLS = [
    {
        "name": "search",
        "description": "Search for information on a topic",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "summarize",
        "description": "Summarize a body of text",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
]

SEARCH_RESULTS = {
    "photosynthesis":    "Photosynthesis converts light energy into chemical energy stored in glucose.",
    "cellular respiration": "Cellular respiration breaks down glucose to release ATP for cellular use.",
    "chloroplast":       "Chloroplasts contain chlorophyll which absorbs light for photosynthesis.",
}


async def run_tool(name: str, inputs: dict, cache: dict, tool_id: str) -> str:
    if tool_id in cache:
        print(f"  [CACHE] {name} (from checkpoint)")
        return cache[tool_id]
    if name == "search":
        result = SEARCH_RESULTS.get(inputs.get("query", ""), "No results found.")
    elif name == "summarize":
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": f"Summarize: {inputs['text'][:300]}"}],
        )
        result = resp.content[0].text
    else:
        result = "Unknown tool"
    return result


async def agent_task(task_id: str, goal: str) -> str:
    checkpoint = ConversationCheckpoint.load(task_id)
    if checkpoint:
        print(f"[{task_id}] Resuming from turn {checkpoint.turn_count}")
        messages = checkpoint.messages
        cache = checkpoint.tool_results_cache
    else:
        checkpoint = ConversationCheckpoint(task_id=task_id, messages=[], tool_results_cache={}, turn_count=0)
        messages = [{"role": "user", "content": goal}]
        checkpoint.save()
        cache = {}

    for _ in range(8):
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            final = next((b.text for b in resp.content if hasattr(b, "text")), "")
            checkpoint.status = "complete"
            checkpoint.save()
            return final

        if resp.stop_reason == "tool_use":
            content_serializable = [
                {"type": b.type, "id": getattr(b, "id", None), "name": getattr(b, "name", None),
                 "input": getattr(b, "input", None), "text": getattr(b, "text", None)}
                for b in resp.content
            ]
            checkpoint.messages.append({"role": "assistant", "content": str(resp.content)})

            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = await run_tool(block.name, block.input, cache, block.id)
                    checkpoint.cache_tool_result(block.id, result)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": tool_results})
            checkpoint.turn_count += 1
            checkpoint.save()

    return "Max turns reached"


async def main() -> None:
    task_id = "bio_research_001"
    goal = "Research photosynthesis and cellular respiration, then summarize the key differences."
    result = await agent_task(task_id, goal)
    print(f"Result: {result[:200]}")

    # cleanup
    (CHECKPOINT_DIR / f"{task_id}.json").unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Tool results cached in checkpoint — no re-execution of expensive calls
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Idempotent Step Execution with Content-Addressed Cache

Hash step inputs to create a content-addressed cache; identical inputs always return cached results, making retries free.

```python
import anthropic
import hashlib
import json
import os
import time
from pathlib import Path

client = anthropic.Anthropic()

CACHE_DIR = Path("/tmp/step_cache")
CACHE_DIR.mkdir(exist_ok=True)


def step_hash(step_name: str, inputs: dict) -> str:
    content = json.dumps({"step": step_name, "inputs": inputs}, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def load_cached_result(step_name: str, inputs: dict) -> str | None:
    h = step_hash(step_name, inputs)
    path = CACHE_DIR / f"{step_name}_{h}.json"
    if path.exists():
        data = json.loads(path.read_text())
        if time.time() - data["cached_at"] < 86400:   # 24h TTL
            return data["result"]
    return None


def cache_result(step_name: str, inputs: dict, result: str) -> None:
    h = step_hash(step_name, inputs)
    path = CACHE_DIR / f"{step_name}_{h}.json"
    path.write_text(json.dumps({"result": result, "cached_at": time.time()}))


def run_step(step_name: str, inputs: dict, use_cache: bool = True) -> tuple[str, bool]:
    """Returns (result, was_cached)."""
    if use_cache:
        cached = load_cached_result(step_name, inputs)
        if cached is not None:
            return cached, True

    prompt = inputs.get("prompt", "")
    context = inputs.get("context", "")
    full_prompt = f"{context}\n\n{prompt}" if context else prompt

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": full_prompt}],
    )
    result = resp.content[0].text
    cache_result(step_name, inputs, result)
    return result, False


def run_pipeline(task_name: str, steps: list[dict]) -> list[dict]:
    """Run pipeline with idempotent, content-addressed steps."""
    results = []
    prior_context = ""

    for step in steps:
        name   = step["name"]
        prompt = step["prompt"]
        inputs = {"prompt": prompt, "context": prior_context[:500]}

        result, was_cached = run_step(name, inputs)
        status = "cached" if was_cached else "computed"
        print(f"  [{status:8s}] {name}: {result[:60]}...")

        results.append({"step": name, "result": result, "cached": was_cached})
        prior_context += f"\n{name}: {result[:200]}"

    return results


if __name__ == "__main__":
    STEPS = [
        {"name": "define_topic",   "prompt": "Define machine learning in one sentence."},
        {"name": "list_types",     "prompt": "List 3 types of machine learning."},
        {"name": "give_example",   "prompt": "Give a real-world example of supervised learning."},
        {"name": "summarize",      "prompt": "Summarize everything discussed about machine learning."},
    ]

    print("=== First run (all computed) ===")
    run_pipeline("ml_overview", STEPS)

    print("\n=== Second run (all cached — simulates restart) ===")
    run_pipeline("ml_overview", STEPS)

    # cleanup
    for p in CACHE_DIR.glob("*.json"):
        p.unlink()

# Expected Token Savings: Identical step inputs never re-billed; restarts are nearly free
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Checkpoint with Compensating Actions for Partial Failures

When a step fails after partial side effects, record compensating actions that can be replayed to undo partial work before retrying.

```python
import anthropic
import json
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Callable

client = anthropic.Anthropic()

CHECKPOINT_DIR = Path("/tmp/saga_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)


@dataclass
class SagaStep:
    name: str
    forward: Callable
    compensate: Callable | None = None


@dataclass
class SagaCheckpoint:
    saga_id: str
    completed: list[str] = field(default_factory=list)
    compensated: list[str] = field(default_factory=list)
    results: dict = field(default_factory=dict)
    status: str = "in_progress"

    def save(self) -> None:
        path = CHECKPOINT_DIR / f"{self.saga_id}.json"
        path.write_text(json.dumps(asdict(self)))

    @classmethod
    def load(cls, saga_id: str) -> "SagaCheckpoint | None":
        path = CHECKPOINT_DIR / f"{saga_id}.json"
        if not path.exists():
            return None
        return cls(**json.loads(path.read_text()))


def llm_step(prompt: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def run_saga(saga_id: str, steps: list[SagaStep]) -> dict:
    checkpoint = SagaCheckpoint.load(saga_id) or SagaCheckpoint(saga_id=saga_id)
    results = checkpoint.results

    try:
        for step in steps:
            if step.name in checkpoint.completed:
                print(f"  [SKIP] {step.name} (already done)")
                continue

            print(f"  [RUN ] {step.name}")
            result = step.forward(results)
            results[step.name] = result
            checkpoint.completed.append(step.name)
            checkpoint.results = results
            checkpoint.save()
            print(f"  [DONE] {step.name}: {str(result)[:60]}")

        checkpoint.status = "complete"
        checkpoint.save()
        return results

    except Exception as e:
        print(f"\n[SAGA FAILED at step] Error: {e}")
        checkpoint.status = "compensating"
        checkpoint.save()

        # run compensating actions in reverse order for completed steps
        for step in reversed(steps):
            if step.name not in checkpoint.completed:
                continue
            if step.name in checkpoint.compensated:
                continue
            if step.compensate:
                print(f"  [COMP] {step.name} — rolling back")
                step.compensate(results)
                checkpoint.compensated.append(step.name)
                checkpoint.save()

        checkpoint.status = "compensated"
        checkpoint.save()
        raise


if __name__ == "__main__":
    SAGA_ID = "order_processing_001"

    published_articles = []
    reserved_inventory = []

    steps = [
        SagaStep(
            name="generate_content",
            forward=lambda r: llm_step("Write a 2-sentence product description for a wireless keyboard."),
        ),
        SagaStep(
            name="reserve_inventory",
            forward=lambda r: (reserved_inventory.append("keyboard_001"), "reserved:keyboard_001")[1],
            compensate=lambda r: (reserved_inventory.remove("keyboard_001") if "keyboard_001" in reserved_inventory else None),
        ),
        SagaStep(
            name="publish_listing",
            forward=lambda r: (published_articles.append(r["generate_content"]), "published")[1],
            compensate=lambda r: published_articles.clear(),
        ),
        SagaStep(
            name="send_notification",
            forward=lambda r: llm_step(f"Draft a 1-sentence launch announcement for: {r.get('generate_content', '')[:100]}"),
        ),
    ]

    try:
        results = run_saga(SAGA_ID, steps)
        print(f"\nSaga complete: {list(results.keys())}")
    except Exception as e:
        print(f"Saga rolled back: {e}")
    finally:
        (CHECKPOINT_DIR / f"{SAGA_ID}.json").unlink(missing_ok=True)

# Expected Token Savings: On failure, compensating actions undo partial work; clean retry from start
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Distributed Checkpoint with Redis-Compatible In-Memory Store

Production-ready checkpoint store backed by an in-memory dict (swap for Redis in production) with TTL and atomic updates.

```python
import anthropic
import json
import time
import threading
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# In-memory store simulating Redis (replace with redis.Redis() in production)
_STORE: dict[str, tuple[str, float]] = {}   # key → (value, expires_at)
_STORE_LOCK = threading.Lock()

CHECKPOINT_TTL = 7 * 86400   # 7 days


def store_set(key: str, value: str, ttl: float = CHECKPOINT_TTL) -> None:
    with _STORE_LOCK:
        _STORE[key] = (value, time.time() + ttl)


def store_get(key: str) -> str | None:
    with _STORE_LOCK:
        entry = _STORE.get(key)
        if not entry:
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            del _STORE[key]
            return None
        return value


def store_delete(key: str) -> None:
    with _STORE_LOCK:
        _STORE.pop(key, None)


@dataclass
class DistributedCheckpoint:
    task_id: str
    step_results: dict = field(default_factory=dict)
    completed_steps: list = field(default_factory=list)
    total_steps: int = 0
    version: int = 0

    def save(self) -> None:
        self.version += 1
        data = {
            "step_results":     self.step_results,
            "completed_steps":  self.completed_steps,
            "total_steps":      self.total_steps,
            "version":          self.version,
            "updated_at":       time.time(),
        }
        store_set(f"checkpoint:{self.task_id}", json.dumps(data))

    @classmethod
    def load(cls, task_id: str) -> "DistributedCheckpoint | None":
        raw = store_get(f"checkpoint:{task_id}")
        if not raw:
            return None
        data = json.loads(raw)
        cp = cls(task_id=task_id)
        cp.step_results    = data["step_results"]
        cp.completed_steps = data["completed_steps"]
        cp.total_steps     = data["total_steps"]
        cp.version         = data["version"]
        print(f"  [LOADED] checkpoint v{cp.version} with {len(cp.completed_steps)} completed steps")
        return cp

    def record(self, step_name: str, result: str) -> None:
        self.step_results[step_name]  = result
        self.completed_steps.append(step_name)
        self.save()

    def is_done(self, step_name: str) -> bool:
        return step_name in self.completed_steps


def execute_step(step_name: str, prompt: str, context: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nTask: {prompt}"}],
    )
    return resp.content[0].text


def run_with_distributed_checkpoint(task_id: str, steps: list[tuple[str, str]]) -> dict:
    cp = DistributedCheckpoint.load(task_id)
    if cp:
        print(f"[RESUME] task={task_id} from step {len(cp.completed_steps)}/{len(steps)}")
    else:
        cp = DistributedCheckpoint(task_id=task_id, total_steps=len(steps))
        cp.save()
        print(f"[START ] task={task_id} ({len(steps)} steps)")

    context = "\n".join(f"{k}: {v[:80]}" for k, v in cp.step_results.items())

    for step_name, prompt in steps:
        if cp.is_done(step_name):
            print(f"  [SKIP ] {step_name}")
            continue
        print(f"  [RUN  ] {step_name}")
        result = execute_step(step_name, prompt, context)
        cp.record(step_name, result)
        context += f"\n{step_name}: {result[:80]}"
        print(f"  [SAVED] {step_name} → checkpoint v{cp.version}")

    store_delete(f"checkpoint:{task_id}")
    return cp.step_results


if __name__ == "__main__":
    TASK_ID = "climate_analysis_007"
    STEPS = [
        ("causes",        "List 3 main causes of climate change."),
        ("effects",       "Describe 3 major effects of climate change."),
        ("solutions",     "Propose 3 actionable solutions."),
        ("conclusion",    "Write a 2-sentence conclusion tying it all together."),
    ]

    print("=== Run 1 ===")
    run_with_distributed_checkpoint(TASK_ID, STEPS)

    # simulate crash after step 2 by manually saving partial checkpoint
    print("\n=== Simulating crash — partial checkpoint ===")
    partial = DistributedCheckpoint(task_id=TASK_ID + "_crash")
    partial.record("causes",  "Fossil fuels, deforestation, industrial emissions.")
    partial.record("effects", "Rising sea levels, extreme weather, biodiversity loss.")

    print("=== Run 2 (resuming from crash) ===")
    run_with_distributed_checkpoint(TASK_ID + "_crash", STEPS)

# Expected Token Savings: Redis TTL manages storage; atomic updates prevent partial checkpoint corruption
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Storage | Concurrency Safe | Rollback Support | TTL | Best For |
|--------|---------|-----------------|-----------------|-----|----------|
| 1 | JSON file | No | No | No | Simple single-process agents |
| 2 | SQLite | Yes | No | No | Multi-agent with shared DB |
| 3 | JSON file | No | No | No | Conversation history replay |
| 4 | Content-addressed file cache | No | No | Yes (24h) | Idempotent, repeatable pipelines |
| 5 | JSON file + saga | Partial | Yes | No | Distributed transactions with rollback |
| 6 | Redis-compatible store | Yes | No | Yes (7d) | Production distributed systems |
