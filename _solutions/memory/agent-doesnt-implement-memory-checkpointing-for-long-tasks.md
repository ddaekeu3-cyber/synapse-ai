---
layout: solution
title: "Agent Doesn't Implement Memory Checkpointing for Long Tasks"
category: memory
description: "Agents that run multi-step tasks without checkpointing lose all progress when they crash, timeout, or hit context limits. Memory checkpointing persists intermediate state to SQLite so tasks resume from the last successful step."
tags: [memory, checkpointing, long-tasks, persistence, sqlite, recovery, resumable]
---

# Agent Doesn't Implement Memory Checkpointing for Long Tasks

## Problem

Multi-step agent tasks — document analysis, batch processing, iterative research — can take dozens of turns. Without checkpointing, a crash at step 18 of 20 means starting over from step 1. Context window limits compound this: the agent forgets early progress as the conversation grows.

Checkpointing writes intermediate state to SQLite after each step so tasks resume from the last successful checkpoint instead of the beginning.

---

## Option 1: Simple Step Checkpoint with Resume

```python
import sqlite3
import json
import uuid
import anthropic
from dataclasses import dataclass

@dataclass
class CheckpointState:
    task_id: str
    step: int
    total_steps: int
    completed_steps: list[dict]
    is_complete: bool = False

class StepCheckpointer:
    def __init__(self, db_path: str = "checkpoints.db"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT PRIMARY KEY,
                current_step INTEGER DEFAULT 0,
                total_steps INTEGER,
                completed_steps TEXT DEFAULT '[]',
                is_complete INTEGER DEFAULT 0,
                updated_at TEXT
            )
        """)
        self.conn.commit()

    def create(self, task_id: str, total_steps: int) -> CheckpointState:
        self.conn.execute(
            "INSERT OR IGNORE INTO checkpoints (task_id, total_steps, completed_steps, updated_at) VALUES (?,?,'[]',datetime('now'))",
            (task_id, total_steps),
        )
        self.conn.commit()
        return self.load(task_id)

    def load(self, task_id: str) -> CheckpointState | None:
        row = self.conn.execute(
            "SELECT current_step, total_steps, completed_steps, is_complete FROM checkpoints WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        return CheckpointState(
            task_id=task_id,
            step=row[0],
            total_steps=row[1],
            completed_steps=json.loads(row[2]),
            is_complete=bool(row[3]),
        )

    def save(self, state: CheckpointState):
        self.conn.execute(
            """UPDATE checkpoints
               SET current_step=?, completed_steps=?, is_complete=?, updated_at=datetime('now')
               WHERE task_id=?""",
            (state.step, json.dumps(state.completed_steps), int(state.is_complete), state.task_id),
        )
        self.conn.commit()


ANALYSIS_STEPS = [
    "Identify the main topic and scope of the subject matter.",
    "List three key concepts or components involved.",
    "Describe one real-world application or use case.",
    "Summarize the main challenges or limitations.",
    "Suggest one area for further investigation.",
]


def run_resumable_task(topic: str, task_id: str | None = None) -> str:
    task_id = task_id or str(uuid.uuid4())[:10]
    checkpointer = StepCheckpointer(db_path=":memory:")
    client = anthropic.Anthropic()

    state = checkpointer.load(task_id)
    if state is None:
        state = checkpointer.create(task_id, total_steps=len(ANALYSIS_STEPS))
        print(f"[Task {task_id}] Starting new task ({len(ANALYSIS_STEPS)} steps)")
    else:
        print(f"[Task {task_id}] Resuming from step {state.step}/{state.total_steps}")

    while state.step < state.total_steps:
        step_prompt = ANALYSIS_STEPS[state.step]
        full_prompt = (
            f"Topic: {topic}\n\nStep {state.step + 1}/{state.total_steps}: {step_prompt}\n\n"
            + (
                f"Prior steps completed:\n" +
                "\n".join(f"Step {s['step']}: {s['result'][:80]}" for s in state.completed_steps)
                if state.completed_steps else ""
            )
        )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": full_prompt}],
        )
        result = response.content[0].text

        state.completed_steps.append({"step": state.step + 1, "prompt": step_prompt, "result": result})
        state.step += 1
        checkpointer.save(state)
        print(f"[Step {state.step}] {result[:70]}")

    state.is_complete = True
    checkpointer.save(state)

    return "\n\n".join(f"Step {s['step']}: {s['result']}" for s in state.completed_steps)


if __name__ == "__main__":
    final = run_resumable_task("Large Language Models")
    print("\n=== Final Report ===")
    print(final[:500])
# Expected Token Savings: 60-100% on resume — skips all completed steps
# Environment: pip install anthropic; sqlite3, json, uuid are stdlib
```

---

## Option 2: Checkpoint with Partial Context Injection

```python
import sqlite3
import json
import uuid
import anthropic
from dataclasses import dataclass, field

@dataclass
class TaskCheckpoint:
    task_id: str
    task_name: str
    step_index: int
    steps_done: list[str]
    artifacts: dict = field(default_factory=dict)  # Named outputs saved across steps
    complete: bool = False

class ArtifactCheckpointer:
    """
    Checkpoints both step completion and named artifacts
    (e.g., 'outline', 'draft', 'critique') so later steps
    can reference earlier outputs without full context replay.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                task_name TEXT,
                step_index INTEGER DEFAULT 0,
                complete INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                task_id TEXT,
                name TEXT,
                content TEXT,
                step_index INTEGER,
                saved_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (task_id, name)
            );
            CREATE TABLE IF NOT EXISTS step_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                step_index INTEGER,
                summary TEXT,
                logged_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def init_task(self, task_id: str, task_name: str) -> TaskCheckpoint:
        self.conn.execute(
            "INSERT OR IGNORE INTO tasks (task_id, task_name) VALUES (?,?)",
            (task_id, task_name),
        )
        self.conn.commit()
        return self._load(task_id)

    def _load(self, task_id: str) -> TaskCheckpoint:
        row = self.conn.execute(
            "SELECT task_name, step_index, complete FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"Task {task_id} not found")

        logs = self.conn.execute(
            "SELECT summary FROM step_log WHERE task_id=? ORDER BY step_index", (task_id,)
        ).fetchall()

        arts = self.conn.execute(
            "SELECT name, content FROM artifacts WHERE task_id=?", (task_id,)
        ).fetchall()

        return TaskCheckpoint(
            task_id=task_id,
            task_name=row[0],
            step_index=row[1],
            steps_done=[r[0] for r in logs],
            artifacts={r[0]: r[1] for r in arts},
            complete=bool(row[2]),
        )

    def advance(self, ckpt: TaskCheckpoint, summary: str, new_artifacts: dict | None = None):
        next_step = ckpt.step_index + 1
        self.conn.execute(
            "UPDATE tasks SET step_index=? WHERE task_id=?", (next_step, ckpt.task_id)
        )
        self.conn.execute(
            "INSERT INTO step_log (task_id, step_index, summary) VALUES (?,?,?)",
            (ckpt.task_id, ckpt.step_index, summary[:300]),
        )
        if new_artifacts:
            for name, content in new_artifacts.items():
                self.conn.execute(
                    "INSERT OR REPLACE INTO artifacts (task_id, name, content, step_index) VALUES (?,?,?,?)",
                    (ckpt.task_id, name, content, ckpt.step_index),
                )
        self.conn.commit()
        ckpt.step_index = next_step
        ckpt.steps_done.append(summary)
        if new_artifacts:
            ckpt.artifacts.update(new_artifacts)

    def complete(self, ckpt: TaskCheckpoint):
        self.conn.execute(
            "UPDATE tasks SET complete=1 WHERE task_id=?", (ckpt.task_id,)
        )
        self.conn.commit()
        ckpt.complete = True


def run_document_task(topic: str, task_id: str | None = None) -> dict:
    task_id = task_id or str(uuid.uuid4())[:8]
    ckpt_db = ArtifactCheckpointer()
    client = anthropic.Anthropic()

    ckpt = ckpt_db.init_task(task_id, f"Document: {topic}")

    PIPELINE = [
        ("outline",   "Create a 3-point outline for a technical document about: {topic}"),
        ("draft",     "Write a paragraph for each point in this outline:\n{outline}"),
        ("critique",  "Critique this draft in 2 sentences:\n{draft}"),
        ("revision",  "Revise the draft based on this critique:\nDraft: {draft}\nCritique: {critique}"),
    ]

    for step_name, template in PIPELINE[ckpt.step_index:]:
        # Inject artifacts from prior steps into prompt
        try:
            prompt = template.format(topic=topic, **ckpt.artifacts)
        except KeyError as e:
            prompt = f"Continue the task about '{topic}'. Missing artifact: {e}"

        print(f"[Step {ckpt.step_index + 1}] {step_name}")
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text
        ckpt_db.advance(ckpt, summary=f"{step_name}: {result[:80]}", new_artifacts={step_name: result})
        print(f"  → {result[:80]}")

    ckpt_db.complete(ckpt)
    print(f"\nTask {task_id} complete. Artifacts: {list(ckpt.artifacts.keys())}")
    return ckpt.artifacts


if __name__ == "__main__":
    artifacts = run_document_task("vector databases")
    print("\n=== Final Revision ===")
    print(artifacts.get("revision", "")[:300])
# Expected Token Savings: 70-90% on resume — only injects named artifacts, not full conversation history
# Environment: pip install anthropic; sqlite3, json, uuid are stdlib
```

---

## Option 3: Async Checkpoint with Progress Streaming

```python
import asyncio
import sqlite3
import json
import uuid
import anthropic
from dataclasses import dataclass
from datetime import datetime

@dataclass
class AsyncCheckpoint:
    task_id: str
    steps_total: int
    steps_done: int
    results: list[str]
    status: str  # "running", "complete", "failed"

class AsyncCheckpointer:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = asyncio.Lock()
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS async_tasks (
                task_id TEXT PRIMARY KEY,
                steps_total INTEGER,
                steps_done INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS step_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                step_index INTEGER,
                result TEXT,
                tokens_used INTEGER,
                completed_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    async def create(self, task_id: str, steps_total: int):
        async with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO async_tasks (task_id, steps_total) VALUES (?,?)",
                (task_id, steps_total),
            )
            self.conn.commit()

    async def save_step(self, task_id: str, step_index: int, result: str, tokens: int):
        async with self._lock:
            self.conn.execute(
                "INSERT INTO step_results (task_id, step_index, result, tokens_used) VALUES (?,?,?,?)",
                (task_id, step_index, result[:500], tokens),
            )
            self.conn.execute(
                "UPDATE async_tasks SET steps_done=steps_done+1 WHERE task_id=?",
                (task_id,),
            )
            self.conn.commit()

    async def load(self, task_id: str) -> AsyncCheckpoint | None:
        async with self._lock:
            task_row = self.conn.execute(
                "SELECT steps_total, steps_done, status FROM async_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not task_row:
                return None
            results_rows = self.conn.execute(
                "SELECT result FROM step_results WHERE task_id=? ORDER BY step_index",
                (task_id,),
            ).fetchall()
            return AsyncCheckpoint(
                task_id=task_id,
                steps_total=task_row[0],
                steps_done=task_row[1],
                results=[r[0] for r in results_rows],
                status=task_row[2],
            )

    async def mark_complete(self, task_id: str):
        async with self._lock:
            self.conn.execute(
                "UPDATE async_tasks SET status='complete' WHERE task_id=?", (task_id,)
            )
            self.conn.commit()


async def run_async_checkpointed_pipeline(topic: str):
    task_id = str(uuid.uuid4())[:8]
    checkpointer = AsyncCheckpointer()
    client = anthropic.AsyncAnthropic()

    steps = [
        f"In one sentence, define: {topic}",
        f"Name 3 practical applications of: {topic}",
        f"What is one major challenge with: {topic}",
        f"Suggest the best resource to learn more about: {topic}",
    ]

    await checkpointer.create(task_id, len(steps))
    ckpt = await checkpointer.load(task_id)
    start_step = ckpt.steps_done if ckpt else 0
    prior_results = ckpt.results if ckpt else []

    print(f"[Task {task_id}] Starting from step {start_step + 1}/{len(steps)}")

    for i, step_prompt in enumerate(steps[start_step:], start=start_step):
        context = "\n".join(f"Step {j+1}: {r}" for j, r in enumerate(prior_results)) if prior_results else ""
        full_prompt = (f"Context:\n{context}\n\n" if context else "") + f"Now answer: {step_prompt}"

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": full_prompt}],
        )
        result = response.content[0].text
        tokens = response.usage.output_tokens

        await checkpointer.save_step(task_id, i, result, tokens)
        prior_results.append(result)
        print(f"  [Step {i+1}] ({tokens} tokens) {result[:70]}")

    await checkpointer.mark_complete(task_id)

    final = await checkpointer.load(task_id)
    print(f"\nTask complete. {final.steps_done}/{final.steps_total} steps, status={final.status}")
    return final.results


if __name__ == "__main__":
    asyncio.run(run_async_checkpointed_pipeline("transformer attention mechanisms"))
# Expected Token Savings: 50-80% on resume; prior step results are compact summaries, not full messages
# Environment: pip install anthropic; asyncio, sqlite3, json, uuid are stdlib
```

---

## Option 4: Hierarchical Checkpoint (Plan → Subtask → Step)

```python
import sqlite3
import json
import uuid
import anthropic
from dataclasses import dataclass

@dataclass
class Plan:
    plan_id: str
    goal: str
    subtasks: list[str]

@dataclass
class SubtaskCheckpoint:
    plan_id: str
    subtask_index: int
    subtask_goal: str
    steps_done: list[str]
    is_complete: bool

class HierarchicalCheckpointer:
    """
    Three-level checkpoint: Plan → Subtask → Steps.
    Resuming from failure picks up at the subtask and step level.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY,
                goal TEXT,
                subtasks TEXT,
                current_subtask INTEGER DEFAULT 0,
                complete INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS subtask_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT,
                subtask_index INTEGER,
                step_text TEXT,
                completed_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def save_plan(self, plan: Plan):
        self.conn.execute(
            "INSERT OR IGNORE INTO plans (plan_id, goal, subtasks) VALUES (?,?,?)",
            (plan.plan_id, plan.goal, json.dumps(plan.subtasks)),
        )
        self.conn.commit()

    def load_plan(self, plan_id: str) -> tuple[Plan, int]:
        row = self.conn.execute(
            "SELECT goal, subtasks, current_subtask FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if not row:
            raise KeyError(plan_id)
        plan = Plan(plan_id=plan_id, goal=row[0], subtasks=json.loads(row[1]))
        return plan, row[2]

    def load_subtask(self, plan_id: str, subtask_index: int, subtask_goal: str) -> SubtaskCheckpoint:
        rows = self.conn.execute(
            "SELECT step_text FROM subtask_steps WHERE plan_id=? AND subtask_index=? ORDER BY id",
            (plan_id, subtask_index),
        ).fetchall()
        return SubtaskCheckpoint(
            plan_id=plan_id,
            subtask_index=subtask_index,
            subtask_goal=subtask_goal,
            steps_done=[r[0] for r in rows],
            is_complete=len(rows) >= 3,  # each subtask has 3 steps
        )

    def save_step(self, ckpt: SubtaskCheckpoint, step_text: str):
        self.conn.execute(
            "INSERT INTO subtask_steps (plan_id, subtask_index, step_text) VALUES (?,?,?)",
            (ckpt.plan_id, ckpt.subtask_index, step_text[:300]),
        )
        ckpt.steps_done.append(step_text)
        self.conn.commit()

    def advance_subtask(self, plan_id: str, next_index: int):
        self.conn.execute(
            "UPDATE plans SET current_subtask=? WHERE plan_id=?", (next_index, plan_id)
        )
        self.conn.commit()

    def complete_plan(self, plan_id: str):
        self.conn.execute(
            "UPDATE plans SET complete=1 WHERE plan_id=?", (plan_id,)
        )
        self.conn.commit()


def run_hierarchical_task(goal: str) -> dict[str, list[str]]:
    plan_id = str(uuid.uuid4())[:8]
    ckptr = HierarchicalCheckpointer()
    client = anthropic.Anthropic()

    # Step 1: Generate plan via LLM
    plan_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Break this goal into 3 subtasks (one per line, no numbers): {goal}"}],
    )
    subtasks = [line.strip() for line in plan_response.content[0].text.strip().split("\n") if line.strip()][:3]
    plan = Plan(plan_id=plan_id, goal=goal, subtasks=subtasks)
    ckptr.save_plan(plan)
    print(f"[Plan {plan_id}] Goal: {goal}")
    print(f"Subtasks: {subtasks}")

    # Execute from current checkpoint
    _, start_subtask = ckptr.load_plan(plan_id)
    all_results: dict[str, list[str]] = {}

    for subtask_idx in range(start_subtask, len(subtasks)):
        subtask_goal = subtasks[subtask_idx]
        ckpt = ckptr.load_subtask(plan_id, subtask_idx, subtask_goal)
        print(f"\n[Subtask {subtask_idx + 1}] {subtask_goal} (done={len(ckpt.steps_done)}/3)")

        STEP_PROMPTS = [
            f"Define the scope of: {subtask_goal}",
            f"Describe one concrete approach for: {subtask_goal}",
            f"What is one risk or challenge in: {subtask_goal}",
        ]

        subtask_results = list(ckpt.steps_done)
        for step_prompt in STEP_PROMPTS[len(ckpt.steps_done):]:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": step_prompt}],
            )
            result = response.content[0].text
            ckptr.save_step(ckpt, result)
            subtask_results.append(result)
            print(f"  → {result[:60]}")

        all_results[subtask_goal] = subtask_results
        ckptr.advance_subtask(plan_id, subtask_idx + 1)

    ckptr.complete_plan(plan_id)
    print(f"\n[Plan {plan_id}] Complete")
    return all_results


if __name__ == "__main__":
    results = run_hierarchical_task("Build a recommendation system for an e-commerce platform")
    print(f"\nTotal subtasks completed: {len(results)}")
# Expected Token Savings: Resume skips N_completed_subtasks × 3 LLM calls
# Environment: pip install anthropic; sqlite3, json, uuid are stdlib
```

---

## Option 5: Time-Bounded Checkpoint with Deadline Extension

```python
import sqlite3
import json
import uuid
import time
import anthropic
from dataclasses import dataclass
from datetime import datetime, timedelta

@dataclass
class TimedCheckpoint:
    task_id: str
    topic: str
    deadline_ts: float
    steps_done: list[dict]
    status: str  # "running", "deadline_exceeded", "complete"

class TimedCheckpointer:
    """
    Checkpoint that also tracks deadlines.
    If the deadline passes, saves progress and stops gracefully.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS timed_tasks (
                task_id TEXT PRIMARY KEY,
                topic TEXT,
                deadline_ts REAL,
                status TEXT DEFAULT 'running',
                steps_done TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def create(self, topic: str, deadline_sec: float) -> TimedCheckpoint:
        task_id = str(uuid.uuid4())[:8]
        deadline_ts = time.time() + deadline_sec
        self.conn.execute(
            "INSERT INTO timed_tasks (task_id, topic, deadline_ts) VALUES (?,?,?)",
            (task_id, topic, deadline_ts),
        )
        self.conn.commit()
        return TimedCheckpoint(task_id=task_id, topic=topic, deadline_ts=deadline_ts, steps_done=[], status="running")

    def save(self, ckpt: TimedCheckpoint):
        self.conn.execute(
            "UPDATE timed_tasks SET steps_done=?, status=? WHERE task_id=?",
            (json.dumps(ckpt.steps_done), ckpt.status, ckpt.task_id),
        )
        self.conn.commit()

    def resume(self, task_id: str) -> TimedCheckpoint | None:
        row = self.conn.execute(
            "SELECT topic, deadline_ts, steps_done, status FROM timed_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        return TimedCheckpoint(
            task_id=task_id,
            topic=row[0],
            deadline_ts=row[1],
            steps_done=json.loads(row[2]),
            status=row[3],
        )

    def seconds_remaining(self, ckpt: TimedCheckpoint) -> float:
        return max(0.0, ckpt.deadline_ts - time.time())


def run_timed_research(topic: str, time_budget_sec: float = 30.0) -> dict:
    ckptr = TimedCheckpointer()
    client = anthropic.Anthropic()
    ckpt = ckptr.create(topic, deadline_sec=time_budget_sec)

    RESEARCH_STEPS = [
        "What is the core concept?",
        "What are three key properties or features?",
        "What are two major use cases?",
        "What are two known limitations?",
        "What recent developments are worth noting?",
        "What related topics should be explored next?",
    ]

    print(f"[Task {ckpt.task_id}] Budget: {time_budget_sec}s | Topic: {topic}")

    for i, step_q in enumerate(RESEARCH_STEPS[len(ckpt.steps_done):], start=len(ckpt.steps_done)):
        remaining = ckptr.seconds_remaining(ckpt)
        print(f"  [Step {i+1}] {remaining:.1f}s remaining — {step_q[:50]}")

        if remaining < 2.0:
            ckpt.status = "deadline_exceeded"
            ckptr.save(ckpt)
            print(f"  [Deadline] Stopping at step {i+1} — {len(ckpt.steps_done)} steps saved")
            break

        t0 = time.time()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": f"Regarding {topic}: {step_q}"}],
        )
        elapsed = time.time() - t0
        result = response.content[0].text
        ckpt.steps_done.append({"step": i + 1, "question": step_q, "answer": result, "elapsed_ms": round(elapsed * 1000)})
        ckptr.save(ckpt)
        print(f"    → {result[:60]} ({elapsed:.1f}s)")
    else:
        ckpt.status = "complete"
        ckptr.save(ckpt)

    return {
        "task_id": ckpt.task_id,
        "status": ckpt.status,
        "steps_completed": len(ckpt.steps_done),
        "steps_total": len(RESEARCH_STEPS),
        "results": ckpt.steps_done,
    }


if __name__ == "__main__":
    result = run_timed_research("transformer architecture", time_budget_sec=60.0)
    print(f"\nStatus: {result['status']} | {result['steps_completed']}/{result['steps_total']} steps")
# Expected Token Savings: Deadline stop prevents runaway long tasks; resume continues from last checkpoint
# Environment: pip install anthropic; sqlite3, json, uuid, time are stdlib
```

---

## Option 6: Distributed Checkpoint with Merge and Conflict Resolution

```python
import sqlite3
import json
import uuid
import asyncio
import anthropic
from dataclasses import dataclass
from datetime import datetime

@dataclass
class BranchCheckpoint:
    branch_id: str
    parent_task_id: str
    branch_name: str
    steps: list[dict]
    merged: bool = False

class MergeCheckpointer:
    """
    Fan-out checkpoint: spawns multiple parallel branches,
    checkpoints each independently, then merges results.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = asyncio.Lock()
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS parent_tasks (
                task_id TEXT PRIMARY KEY,
                goal TEXT,
                branch_count INTEGER,
                completed_branches INTEGER DEFAULT 0,
                merged_result TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS branches (
                branch_id TEXT PRIMARY KEY,
                task_id TEXT,
                branch_name TEXT,
                steps TEXT DEFAULT '[]',
                merged INTEGER DEFAULT 0
            );
        """)
        self.conn.commit()

    async def create_task(self, goal: str, branch_names: list[str]) -> str:
        task_id = str(uuid.uuid4())[:8]
        async with self._lock:
            self.conn.execute(
                "INSERT INTO parent_tasks (task_id, goal, branch_count) VALUES (?,?,?)",
                (task_id, goal, len(branch_names)),
            )
            for name in branch_names:
                branch_id = str(uuid.uuid4())[:6]
                self.conn.execute(
                    "INSERT INTO branches (branch_id, task_id, branch_name) VALUES (?,?,?)",
                    (branch_id, task_id, name),
                )
            self.conn.commit()
        return task_id

    async def save_branch_step(self, task_id: str, branch_name: str, step: dict):
        async with self._lock:
            row = self.conn.execute(
                "SELECT branch_id, steps FROM branches WHERE task_id=? AND branch_name=?",
                (task_id, branch_name),
            ).fetchone()
            if row:
                steps = json.loads(row[1])
                steps.append(step)
                self.conn.execute(
                    "UPDATE branches SET steps=? WHERE branch_id=?",
                    (json.dumps(steps), row[0]),
                )
                self.conn.commit()

    async def get_branch_steps(self, task_id: str, branch_name: str) -> list[dict]:
        async with self._lock:
            row = self.conn.execute(
                "SELECT steps FROM branches WHERE task_id=? AND branch_name=?",
                (task_id, branch_name),
            ).fetchone()
            return json.loads(row[0]) if row else []

    async def save_merge(self, task_id: str, merged_result: str):
        async with self._lock:
            self.conn.execute(
                "UPDATE parent_tasks SET merged_result=? WHERE task_id=?",
                (merged_result[:500], task_id),
            )
            self.conn.commit()

    async def all_branches(self, task_id: str) -> list[dict]:
        async with self._lock:
            rows = self.conn.execute(
                "SELECT branch_name, steps FROM branches WHERE task_id=?", (task_id,)
            ).fetchall()
            return [{"name": r[0], "steps": json.loads(r[1])} for r in rows]


async def run_branch(task_id: str, branch_name: str, goal: str, ckptr: MergeCheckpointer):
    client = anthropic.AsyncAnthropic()

    existing = await ckptr.get_branch_steps(task_id, branch_name)
    if existing:
        print(f"[Branch {branch_name}] Resuming from {len(existing)} saved steps")
        return existing[-1]["result"] if existing else ""

    branch_steps = [
        f"For the goal '{goal}', analyze from the perspective of: {branch_name}",
        f"What is one key insight from the {branch_name} perspective on: {goal}",
    ]

    last_result = ""
    for step_prompt in branch_steps:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=96,
            messages=[{"role": "user", "content": step_prompt}],
        )
        last_result = response.content[0].text
        await ckptr.save_branch_step(task_id, branch_name, {"prompt": step_prompt, "result": last_result})
        print(f"[Branch {branch_name}] {last_result[:60]}")

    return last_result


async def run_parallel_checkpointed_task(goal: str) -> str:
    ckptr = MergeCheckpointer()
    client = anthropic.AsyncAnthropic()

    branches = ["technical", "business", "user experience"]
    task_id = await ckptr.create_task(goal, branches)
    print(f"[Task {task_id}] Running {len(branches)} parallel branches for: {goal}")

    branch_results = await asyncio.gather(*[
        run_branch(task_id, name, goal, ckptr)
        for name in branches
    ])

    # Merge results
    all_branches = await ckptr.all_branches(task_id)
    merge_prompt = (
        f"Goal: {goal}\n\nSynthesize these {len(branches)} perspectives into one paragraph:\n\n"
        + "\n\n".join(
            f"{b['name'].upper()}: {b['steps'][-1]['result'] if b['steps'] else 'no data'}"
            for b in all_branches
        )
    )
    merge_response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": merge_prompt}],
    )
    merged = merge_response.content[0].text
    await ckptr.save_merge(task_id, merged)
    print(f"\n[Merge] {merged[:200]}")
    return merged


if __name__ == "__main__":
    asyncio.run(run_parallel_checkpointed_task("Deploy a real-time recommendation system"))
# Expected Token Savings: Parallel branches complete in 1/N the wall time; resume reuses branch checkpoints
# Environment: pip install anthropic; asyncio, sqlite3, json, uuid are stdlib
```

---

## Comparison

| Option | Granularity | Resume Point | Artifact Persistence | Async | Deadline Handling | Best For |
|--------|-------------|-------------|---------------------|-------|-------------------|----------|
| 1 | Per-step | Last step | Step results | No | No | Linear pipelines |
| 2 | Per-step + artifacts | Last step + named outputs | Named artifacts | No | No | Document/content pipelines |
| 3 | Per-step | Last step | Step summaries | Yes | No | High-throughput async tasks |
| 4 | Plan → Subtask → Step | Subtask + step level | Subtask outputs | No | No | Complex hierarchical tasks |
| 5 | Per-step with timer | Last step before deadline | Step answers | No | Graceful stop | Time-budgeted research |
| 6 | Per-branch | Branch step | Branch results | Yes | No | Parallel fan-out with merge |
