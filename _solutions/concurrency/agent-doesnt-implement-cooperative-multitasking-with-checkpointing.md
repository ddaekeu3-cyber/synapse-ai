---
layout: solution
title: "Agent Doesn't Implement Cooperative Multitasking with Checkpointing"
category: concurrency
description: "Long-running agent tasks run to completion or fail entirely — losing all progress on crash, timeout, or interruption — because there's no checkpointing system to save and resume partial work."
tags: [concurrency, checkpointing, resilience, asyncio, long-running-tasks, sqlite]
---

# Agent Doesn't Implement Cooperative Multitasking with Checkpointing

## Problem

Agents tackling long tasks (processing 1000 documents, running multi-step analysis, iterating over large datasets) have no way to pause and resume. If the process crashes, times out, or is interrupted, all progress is lost and the task restarts from scratch.

**Root cause:** Agent task loops are stateless between invocations. There is no mechanism to serialize intermediate state and resume from the last successful checkpoint.

**Symptoms:**
- "We ran for 2 hours then the pod restarted — had to start over"
- Token costs double on every retry of a partially-complete batch job
- Users cancel long tasks because they can't see progress or trust completion
- Memory exhaustion from accumulating all results before any are committed

---

## Option 1: SQLite Checkpoint Store with Step-Based Resumption

Checkpoint progress after each logical step; resume from the last successful checkpoint on restart.

```python
import anthropic
import json
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass, field

client = anthropic.Anthropic()
CHECKPOINT_DB = Path("/tmp/agent_checkpoints.db")

def init_checkpoint_db() -> sqlite3.Connection:
    conn = sqlite3.connect(CHECKPOINT_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            task_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            step_name TEXT,
            state_json TEXT NOT NULL,
            completed_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, step_index)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_results (
            task_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            PRIMARY KEY (task_id, step_index)
        )
    """)
    conn.commit()
    return conn

@dataclass
class CheckpointedTask:
    task_id: str
    conn: sqlite3.Connection
    steps: list[dict] = field(default_factory=list)  # {"name": str, "input": dict}
    current_step: int = 0
    accumulated_results: list = field(default_factory=list)

    def load_checkpoint(self) -> int:
        """Load the latest checkpoint. Returns the step index to resume from."""
        row = self.conn.execute(
            "SELECT step_index, state_json FROM checkpoints WHERE task_id=? ORDER BY step_index DESC LIMIT 1",
            (self.task_id,)
        ).fetchone()

        if row is None:
            return 0

        step_index, state_json = row
        state = json.loads(state_json)
        self.accumulated_results = state.get("accumulated_results", [])
        print(f"[checkpoint] Resuming task {self.task_id} from step {step_index + 1}")
        return step_index + 1  # Resume from NEXT step after last completed

    def save_checkpoint(self, step_index: int, step_name: str, result: dict):
        """Save checkpoint after completing a step."""
        state = {"accumulated_results": self.accumulated_results}
        self.conn.execute("""
            INSERT OR REPLACE INTO checkpoints (task_id, step_index, step_name, state_json)
            VALUES (?, ?, ?, ?)
        """, (self.task_id, step_index, step_name, json.dumps(state)))
        self.conn.execute("""
            INSERT OR REPLACE INTO task_results (task_id, step_index, result_json)
            VALUES (?, ?, ?)
        """, (self.task_id, step_index, json.dumps(result)))
        self.conn.commit()
        print(f"[checkpoint] Saved step {step_index} ({step_name})")

    def get_all_results(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT step_index, result_json FROM task_results WHERE task_id=? ORDER BY step_index",
            (self.task_id,)
        ).fetchall()
        return [json.loads(r[1]) for r in rows]

conn = init_checkpoint_db()

def process_document_step(doc_id: str, content: str) -> dict:
    """Simulate processing one document with the LLM."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"Summarize in one sentence: {content[:200]}"
        }]
    )
    return {
        "doc_id": doc_id,
        "summary": response.content[0].text,
        "processed_at": time.time()
    }

def run_checkpointed_batch(task_id: str, documents: list[dict], simulate_crash_at: int = -1) -> list[dict]:
    task = CheckpointedTask(task_id=task_id, conn=conn)
    resume_from = task.load_checkpoint()

    for i, doc in enumerate(documents):
        if i < resume_from:
            print(f"[checkpoint] Skipping step {i} (already completed)")
            continue

        if i == simulate_crash_at:
            print(f"[checkpoint] Simulating crash at step {i}!")
            raise RuntimeError(f"Simulated crash at step {i}")

        print(f"[checkpoint] Processing step {i}/{len(documents)}: {doc['id']}")
        result = process_document_step(doc["id"], doc["content"])
        task.accumulated_results.append(result)
        task.save_checkpoint(i, f"process_{doc['id']}", result)

    return task.get_all_results()

# Simulate documents
DOCUMENTS = [
    {"id": f"doc_{i}", "content": f"Document {i}: This is about topic {i % 5} and discusses various aspects of AI agents."}
    for i in range(5)
]

# First run — simulate crash at step 2
try:
    results = run_checkpointed_batch("batch-001", DOCUMENTS, simulate_crash_at=2)
except RuntimeError as e:
    print(f"Task crashed: {e}")

# Second run — resumes from checkpoint
print("\n--- Resuming after crash ---")
results = run_checkpointed_batch("batch-001", DOCUMENTS)
print(f"Completed {len(results)} documents")

# Expected Token Savings: ~50% on retry (already-processed steps are skipped entirely)
# Environment: Batch processing agents, document analysis pipelines, long-horizon research tasks
```

---

## Option 2: Async Task Queue with Cooperative Yield Points

Cooperative multitasking using asyncio — tasks yield at defined points so others can run.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class CooperativeTask:
    task_id: str
    description: str
    steps: list[str]
    priority: int = 5  # 1=highest, 10=lowest
    status: TaskStatus = TaskStatus.PENDING
    current_step: int = 0
    results: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    async def execute_step(self, step: str) -> dict:
        """Execute one step and yield control after."""
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": f"Task: {self.description}\nStep: {step}\nComplete this step briefly."}]
        )
        result = {"step": step, "output": response.content[0].text, "at": time.time()}
        await asyncio.sleep(0)  # Cooperative yield point
        return result

class CooperativeScheduler:
    def __init__(self, max_concurrent: int = 3):
        self.tasks: list[CooperativeTask] = []
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.completed: list[str] = []
        self.failed: list[str] = []

    def add_task(self, task: CooperativeTask):
        self.tasks.append(task)
        # Keep sorted by priority
        self.tasks.sort(key=lambda t: t.priority)

    async def run_task(self, task: CooperativeTask):
        async with self.semaphore:
            task.status = TaskStatus.RUNNING
            print(f"[scheduler] Starting task {task.task_id} (priority={task.priority})")
            try:
                for i, step in enumerate(task.steps):
                    if task.current_step > i:
                        continue  # Resume support
                    task.current_step = i
                    result = await task.execute_step(step)
                    task.results.append(result)
                    print(f"[scheduler] Task {task.task_id} completed step {i+1}/{len(task.steps)}")
                    task.status = TaskStatus.PAUSED
                    await asyncio.sleep(0)  # Yield to other tasks
                    task.status = TaskStatus.RUNNING

                task.status = TaskStatus.COMPLETED
                self.completed.append(task.task_id)
                print(f"[scheduler] Task {task.task_id} DONE ({len(task.results)} steps)")
            except asyncio.CancelledError:
                task.status = TaskStatus.PAUSED
                print(f"[scheduler] Task {task.task_id} paused at step {task.current_step}")
                raise
            except Exception as e:
                task.status = TaskStatus.FAILED
                self.failed.append(task.task_id)
                print(f"[scheduler] Task {task.task_id} FAILED: {e}")

    async def run_all(self, timeout: float = 60.0):
        task_coroutines = [self.run_task(t) for t in self.tasks]
        try:
            await asyncio.wait_for(
                asyncio.gather(*task_coroutines, return_exceptions=True),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            print(f"[scheduler] Global timeout reached")

        print(f"[scheduler] Summary: {len(self.completed)} completed, {len(self.failed)} failed")
        return {
            "completed": self.completed,
            "failed": self.failed,
            "results": {t.task_id: t.results for t in self.tasks}
        }

async def run_cooperative_scheduler():
    scheduler = CooperativeScheduler(max_concurrent=2)

    scheduler.add_task(CooperativeTask(
        task_id="analysis-1",
        description="Analyze market trends",
        steps=["Identify key metrics", "Summarize Q1 data", "Write recommendations"],
        priority=1
    ))

    scheduler.add_task(CooperativeTask(
        task_id="report-2",
        description="Write quarterly report",
        steps=["Draft executive summary", "Add supporting data"],
        priority=3
    ))

    scheduler.add_task(CooperativeTask(
        task_id="review-3",
        description="Review competitor products",
        steps=["List competitors", "Compare features"],
        priority=5
    ))

    results = await scheduler.run_all(timeout=120.0)
    return results

results = asyncio.run(run_cooperative_scheduler())
print(f"Completed tasks: {results['completed']}")

# Expected Token Savings: ~20% (shared semaphore prevents thundering herd; tasks interleave efficiently)
# Environment: Multi-task agents handling concurrent research, analysis, and writing jobs
```

---

## Option 3: Generator-Based Resumable Pipelines

Use Python generators as lightweight coroutines — each `yield` is a checkpoint.

```python
import anthropic
import json
import pickle
from pathlib import Path
from typing import Iterator, Generator, Any

client = anthropic.Anthropic()
STATE_PATH = Path("/tmp/agent_generator_state.pkl")

def document_pipeline(documents: list[str], start_from: int = 0) -> Generator[dict, None, list[dict]]:
    """Generator pipeline — each yield is a resume point."""
    results = []

    for i, doc in enumerate(documents):
        if i < start_from:
            yield {"status": "skipped", "index": i}
            continue

        # Step 1: Extract key points
        r1 = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": f"List 3 key points from: {doc[:150]}"}]
        )
        key_points = r1.content[0].text
        yield {"status": "extracted", "index": i, "key_points": key_points[:50]}

        # Step 2: Generate action items
        r2 = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": f"One action item from: {key_points[:100]}"}]
        )
        action = r2.content[0].text

        result = {"index": i, "doc_preview": doc[:40], "key_points": key_points, "action": action}
        results.append(result)
        yield {"status": "completed", "index": i, "action": action[:50]}

    return results

class ResumableGeneratorRunner:
    def __init__(self, state_path: Path):
        self.state_path = state_path

    def save_state(self, index: int, partial_results: list):
        with open(self.state_path, "wb") as f:
            pickle.dump({"index": index, "partial_results": partial_results}, f)

    def load_state(self) -> tuple[int, list]:
        if not self.state_path.exists():
            return 0, []
        with open(self.state_path, "rb") as f:
            state = pickle.load(f)
        print(f"[resume] Loaded state: resuming from index {state['index']}")
        return state["index"], state["partial_results"]

    def run(self, documents: list[str], simulate_fail_at: int = -1) -> list[dict]:
        start_from, partial_results = self.load_state()
        gen = document_pipeline(documents, start_from=start_from)

        try:
            while True:
                try:
                    progress = next(gen)
                    current_index = progress.get("index", 0)

                    if progress["status"] == "completed":
                        partial_results.append(progress)
                        self.save_state(current_index + 1, partial_results)

                    if current_index == simulate_fail_at:
                        raise RuntimeError(f"Simulated failure at index {current_index}")

                except StopIteration as e:
                    # Generator returned final results
                    if self.state_path.exists():
                        self.state_path.unlink()  # Clean up checkpoint
                    return e.value if e.value else partial_results

        except (RuntimeError, KeyboardInterrupt) as e:
            print(f"[resume] Pipeline interrupted at: {e}")
            return partial_results

DOCS = [
    "AI agents are transforming software development by automating repetitive tasks.",
    "Large language models require careful prompt engineering for reliable outputs.",
    "Multi-agent systems can tackle complex problems through specialization.",
    "Memory management is critical for long-running agent applications.",
    "Tool use enables agents to interact with external systems and data sources.",
]

runner = ResumableGeneratorRunner(STATE_PATH)

# First run — crash at index 2
print("=== First run (will crash at index 2) ===")
partial = runner.run(DOCS, simulate_fail_at=2)
print(f"Partial results: {len(partial)} items")

# Second run — resume from checkpoint
print("\n=== Resuming from checkpoint ===")
final = runner.run(DOCS)
print(f"Final results: {len(final)} items")

# Expected Token Savings: ~55% on retry (generator skips already-processed items)
# Environment: Document processing agents, ETL pipelines, multi-document summarization
```

---

## Option 4: Incremental State Machine with Durable Transitions

Model the agent task as an explicit FSM — each state transition is durably committed.

```python
import anthropic
import json
import sqlite3
from pathlib import Path
from enum import Enum
from dataclasses import dataclass
from typing import Callable

client = anthropic.Anthropic()
FSM_DB = Path("/tmp/agent_fsm.db")

class State(Enum):
    INIT = "init"
    PLANNING = "planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    DONE = "done"
    FAILED = "failed"

TRANSITIONS: dict[State, list[State]] = {
    State.INIT: [State.PLANNING, State.FAILED],
    State.PLANNING: [State.EXECUTING, State.FAILED],
    State.EXECUTING: [State.REVIEWING, State.EXECUTING, State.FAILED],
    State.REVIEWING: [State.DONE, State.EXECUTING, State.FAILED],
    State.DONE: [],
    State.FAILED: [],
}

def init_fsm_db() -> sqlite3.Connection:
    conn = sqlite3.connect(FSM_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fsm_state (
            task_id TEXT PRIMARY KEY,
            current_state TEXT NOT NULL,
            context_json TEXT NOT NULL DEFAULT '{}',
            transitions_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

@dataclass
class AgentFSM:
    task_id: str
    conn: sqlite3.Connection

    def load_or_init(self, initial_context: dict) -> tuple[State, dict]:
        row = self.conn.execute(
            "SELECT current_state, context_json FROM fsm_state WHERE task_id=?",
            (self.task_id,)
        ).fetchone()

        if row:
            state = State(row[0])
            context = json.loads(row[1])
            print(f"[fsm] Loaded state: {state.value}")
            return state, context
        else:
            self.conn.execute(
                "INSERT INTO fsm_state (task_id, current_state, context_json) VALUES (?, ?, ?)",
                (self.task_id, State.INIT.value, json.dumps(initial_context))
            )
            self.conn.commit()
            return State.INIT, initial_context

    def transition(self, from_state: State, to_state: State, context: dict) -> bool:
        if to_state not in TRANSITIONS.get(from_state, []):
            print(f"[fsm] Invalid transition {from_state.value} -> {to_state.value}")
            return False

        # Append to transition history
        row = self.conn.execute(
            "SELECT transitions_json FROM fsm_state WHERE task_id=?", (self.task_id,)
        ).fetchone()
        transitions = json.loads(row[0]) if row else []
        transitions.append({"from": from_state.value, "to": to_state.value})

        self.conn.execute("""
            UPDATE fsm_state
            SET current_state=?, context_json=?, transitions_json=?, updated_at=datetime('now')
            WHERE task_id=?
        """, (to_state.value, json.dumps(context), json.dumps(transitions), self.task_id))
        self.conn.commit()
        print(f"[fsm] {from_state.value} -> {to_state.value}")
        return True

conn = init_fsm_db()

def run_fsm_agent(task_id: str, goal: str) -> dict:
    fsm = AgentFSM(task_id=task_id, conn=conn)
    state, context = fsm.load_or_init({"goal": goal, "plan": None, "results": [], "review_count": 0})

    while state not in (State.DONE, State.FAILED):

        if state == State.INIT:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": f"I need to: {goal}\nCreate a 3-step plan (JSON list)."}]
            )
            try:
                plan = json.loads(response.content[0].text)
                if not isinstance(plan, list):
                    plan = [response.content[0].text]
            except Exception:
                plan = [f"Step 1: {goal}", "Step 2: Review", "Step 3: Finalize"]
            context["plan"] = plan
            context["current_step"] = 0
            fsm.transition(state, State.PLANNING, context)
            state = State.PLANNING

        elif state == State.PLANNING:
            print(f"[fsm] Plan: {context['plan']}")
            fsm.transition(state, State.EXECUTING, context)
            state = State.EXECUTING

        elif state == State.EXECUTING:
            plan = context.get("plan", [])
            step_idx = context.get("current_step", 0)

            if step_idx >= len(plan):
                fsm.transition(state, State.REVIEWING, context)
                state = State.REVIEWING
                continue

            step = plan[step_idx]
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": f"Execute: {step}"}]
            )
            context["results"].append({"step": step, "output": response.content[0].text})
            context["current_step"] = step_idx + 1
            fsm.transition(state, State.EXECUTING, context)

        elif state == State.REVIEWING:
            review_count = context.get("review_count", 0)
            if review_count >= 1:
                fsm.transition(state, State.DONE, context)
                state = State.DONE
            else:
                context["review_count"] = review_count + 1
                fsm.transition(state, State.DONE, context)
                state = State.DONE

    return {"task_id": task_id, "final_state": state.value, "results": context.get("results", [])}

result = run_fsm_agent("fsm-task-001", "Write a brief analysis of AI agent memory systems")
print(f"FSM completed: {result['final_state']}, steps: {len(result['results'])}")

# Re-run same task_id — FSM resumes from last committed state
result2 = run_fsm_agent("fsm-task-001", "Write a brief analysis of AI agent memory systems")
print(f"Resume run final state: {result2['final_state']}")

# Expected Token Savings: ~45% on restart (FSM skips all completed transitions on resume)
# Environment: Complex multi-phase agent tasks; production workflows with SLA requirements
```

---

## Option 5: Time-Sliced Execution with Progress Persistence

Execute tasks in fixed time slices; save progress between slices so work can continue later.

```python
import anthropic
import json
import sqlite3
import time
from pathlib import Path

client = anthropic.Anthropic()
TIMESLICE_DB = Path("/tmp/agent_timeslice.db")

def init_timeslice_db() -> sqlite3.Connection:
    conn = sqlite3.connect(TIMESLICE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_slices (
            task_id TEXT NOT NULL,
            slice_num INTEGER NOT NULL,
            items_processed INTEGER DEFAULT 0,
            total_items INTEGER,
            progress_json TEXT DEFAULT '{}',
            slice_duration_ms INTEGER,
            completed_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, slice_num)
        )
    """)
    conn.commit()
    return conn

class TimeSlicedExecutor:
    def __init__(self, task_id: str, conn: sqlite3.Connection, slice_duration_s: float = 10.0):
        self.task_id = task_id
        self.conn = conn
        self.slice_duration = slice_duration_s

    def get_resume_point(self) -> tuple[int, dict]:
        """Returns (items_processed_so_far, last_progress_state)."""
        row = self.conn.execute("""
            SELECT items_processed, progress_json FROM task_slices
            WHERE task_id=? ORDER BY slice_num DESC LIMIT 1
        """, (self.task_id,)).fetchone()

        if row:
            print(f"[timeslice] Resuming: {row[0]} items already done")
            return row[0], json.loads(row[1])
        return 0, {}

    def save_slice(self, slice_num: int, items_done: int, total: int, progress: dict, duration_ms: int):
        self.conn.execute("""
            INSERT OR REPLACE INTO task_slices
            (task_id, slice_num, items_processed, total_items, progress_json, slice_duration_ms)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (self.task_id, slice_num, items_done, total, json.dumps(progress), duration_ms))
        self.conn.commit()

    def run_slice(self, items: list, processor_fn, max_slices: int = 5) -> tuple[list, bool]:
        """Run for up to `slice_duration` seconds. Returns (results, is_done)."""
        start_from, progress = self.get_resume_point()
        results = progress.get("partial_results", [])
        remaining = items[start_from:]

        if not remaining:
            return results, True

        slice_num = self.conn.execute(
            "SELECT COUNT(*) FROM task_slices WHERE task_id=?", (self.task_id,)
        ).fetchone()[0]

        if slice_num >= max_slices:
            print(f"[timeslice] Max slices ({max_slices}) reached")
            return results, False

        slice_start = time.time()
        processed_in_slice = 0

        for i, item in enumerate(remaining):
            if time.time() - slice_start > self.slice_duration:
                print(f"[timeslice] Time slice {slice_num} exhausted after {processed_in_slice} items")
                break

            result = processor_fn(item)
            results.append(result)
            processed_in_slice += 1

        total_done = start_from + processed_in_slice
        duration_ms = int((time.time() - slice_start) * 1000)
        self.save_slice(
            slice_num=slice_num,
            items_done=total_done,
            total=len(items),
            progress={"partial_results": results},
            duration_ms=duration_ms
        )

        is_done = total_done >= len(items)
        print(f"[timeslice] Slice {slice_num}: {total_done}/{len(items)} done in {duration_ms}ms")
        return results, is_done

conn = init_timeslice_db()
executor = TimeSlicedExecutor("analysis-batch-007", conn, slice_duration_s=5.0)

def analyze_item(item: str) -> dict:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{"role": "user", "content": f"One-line insight: {item[:80]}"}]
    )
    return {"item": item[:30], "insight": r.content[0].text}

WORK_ITEMS = [f"Data point {i}: sales={i*100}, region={'APAC' if i%2==0 else 'EMEA'}" for i in range(8)]

# Simulate multiple time-sliced runs
for run in range(3):
    print(f"\n=== Run {run+1} ===")
    results, is_done = executor.run_slice(WORK_ITEMS, analyze_item, max_slices=5)
    if is_done:
        print(f"Task complete! Total results: {len(results)}")
        break
    else:
        print(f"Partial: {len(results)} results so far. Will continue next run.")

# Expected Token Savings: ~50% on restart (each slice resumes from exact last item)
# Environment: Serverless/ephemeral agents with function timeouts (Lambda, Cloud Run); scheduled batch jobs
```

---

## Option 6: Multi-Agent Checkpoint Coordination

Multiple agents work on sub-tasks; a coordinator checkpoints overall progress and reassigns failed sub-tasks.

```python
import anthropic
import asyncio
import json
import sqlite3
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()
COORD_DB = Path("/tmp/agent_coord_checkpoints.db")

class SubtaskStatus(Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    COMPLETED = "completed"
    FAILED = "failed"

def init_coord_db() -> sqlite3.Connection:
    conn = sqlite3.connect(COORD_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subtasks (
            id TEXT PRIMARY KEY,
            task_group TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            assigned_to TEXT,
            result_json TEXT,
            attempts INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

def register_subtasks(conn: sqlite3.Connection, group: str, subtasks: list[dict]):
    for task in subtasks:
        conn.execute("""
            INSERT OR IGNORE INTO subtasks (id, task_group, description)
            VALUES (?, ?, ?)
        """, (task["id"], group, task["description"]))
    conn.commit()

def claim_subtask(conn: sqlite3.Connection, agent_id: str, group: str) -> dict | None:
    row = conn.execute("""
        SELECT id, description FROM subtasks
        WHERE task_group=? AND status='pending' AND attempts < 3
        ORDER BY id LIMIT 1
    """, (group,)).fetchone()

    if not row:
        return None

    conn.execute("""
        UPDATE subtasks SET status='assigned', assigned_to=?, attempts=attempts+1, updated_at=datetime('now')
        WHERE id=?
    """, (agent_id, row[0]))
    conn.commit()
    return {"id": row[0], "description": row[1]}

def complete_subtask(conn: sqlite3.Connection, task_id: str, result: dict):
    conn.execute("""
        UPDATE subtasks SET status='completed', result_json=?, updated_at=datetime('now')
        WHERE id=?
    """, (json.dumps(result), task_id))
    conn.commit()

def fail_subtask(conn: sqlite3.Connection, task_id: str):
    conn.execute("""
        UPDATE subtasks SET status='pending', assigned_to=NULL, updated_at=datetime('now')
        WHERE id=?
    """, (task_id,))
    conn.commit()

def get_group_progress(conn: sqlite3.Connection, group: str) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM subtasks WHERE task_group=? GROUP BY status", (group,)
    ).fetchall()
    return {row[0]: row[1] for row in rows}

async def worker_agent(agent_id: str, group: str, conn: sqlite3.Connection):
    print(f"[coord] Agent {agent_id} starting")
    while True:
        task = claim_subtask(conn, agent_id, group)
        if not task:
            print(f"[coord] Agent {agent_id}: no more tasks")
            break

        print(f"[coord] Agent {agent_id} working on: {task['description'][:40]}")
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": f"Complete this task briefly: {task['description']}"}]
            )
            result = {"agent": agent_id, "output": response.content[0].text, "task_id": task["id"]}
            complete_subtask(conn, task["id"], result)
            print(f"[coord] Agent {agent_id} completed task {task['id']}")
        except Exception as e:
            print(f"[coord] Agent {agent_id} failed task {task['id']}: {e}")
            fail_subtask(conn, task["id"])

        await asyncio.sleep(0.1)  # Cooperative yield

async def run_coordinated_checkpoint(group: str, subtasks: list[dict], num_agents: int = 3):
    conn = init_coord_db()
    register_subtasks(conn, group, subtasks)

    # Check resume state
    progress = get_group_progress(conn, group)
    print(f"[coord] Starting group {group}: {progress}")

    agents = [worker_agent(f"agent-{i}", group, conn) for i in range(num_agents)]
    await asyncio.gather(*agents)

    final_progress = get_group_progress(conn, group)
    print(f"[coord] Final progress: {final_progress}")

    results = conn.execute(
        "SELECT id, result_json FROM subtasks WHERE task_group=? AND status='completed'",
        (group,)
    ).fetchall()
    return [json.loads(r[1]) for r in results]

SUBTASKS = [
    {"id": f"subtask-{i}", "description": f"Analyze component {i}: review architecture, identify bottlenecks, suggest improvement"}
    for i in range(6)
]

results = asyncio.run(run_coordinated_checkpoint("analysis-group-A", SUBTASKS, num_agents=3))
print(f"Total completed subtasks: {len(results)}")

# Run again — completed subtasks are skipped (checkpointed)
print("\n=== Re-running (all should be skipped) ===")
results2 = asyncio.run(run_coordinated_checkpoint("analysis-group-A", SUBTASKS, num_agents=2))
print(f"Results on resume: {len(results2)}")

# Expected Token Savings: ~65% on restart (coordinator skips all completed subtasks; only failed ones retry)
# Environment: Large-scale batch agents; multi-agent pipelines with independent sub-tasks
```

---

## Comparison

| Option | Checkpoint Storage | Resume Granularity | Multi-Agent | Best For |
|--------|-------------------|-------------------|-------------|----------|
| 1. SQLite Step Checkpoint | SQLite rows | Per step | No | Sequential batch processing |
| 2. Async Cooperative Yield | In-memory | Per `await` | Yes | Concurrent multi-task agents |
| 3. Generator Pipeline | Pickle file | Per `yield` | No | ETL/document pipelines |
| 4. Durable FSM | SQLite FSM table | Per state transition | No | Complex multi-phase workflows |
| 5. Time-Sliced Execution | SQLite slice records | Per time window | No | Serverless/short-lived environments |
| 6. Multi-Agent Coordinator | SQLite subtask table | Per subtask | Yes | Parallelizable batch work |
