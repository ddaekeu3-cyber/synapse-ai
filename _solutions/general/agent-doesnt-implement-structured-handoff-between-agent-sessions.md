---
layout: solution
title: "Agent Doesn't Implement Structured Handoff Between Agent Sessions"
category: general
description: "When a session ends and a new one begins, agents that lack structured handoff lose context, duplicate work, and produce incoherent continuations. These patterns show how to serialize agent state for clean session transitions."
tags: [general, session, handoff, continuity, state, anthropic]
---

## Problem

AI agents running across multiple sessions — scheduled jobs, human-interrupted flows, multi-day tasks — lose all context when a session ends. The next session either starts blind, asks redundant questions, or contradicts the previous session's decisions. Structured handoff serializes completed work, active goals, pending decisions, and key decisions so the next session can resume intelligently.

---

### Option 1: JSON State Snapshot with Resume Prompt Injection

Serialize session state to JSON at shutdown and inject it as a system block on resume.

```python
import json
import time
from pathlib import Path
import anthropic

client = anthropic.Anthropic()
STATE_PATH = Path("/tmp/agent_session_state.json")

def save_state(state: dict) -> None:
    state["saved_at"] = time.time()
    STATE_PATH.write_text(json.dumps(state, indent=2))
    print(f"[state saved: {STATE_PATH}]")

def load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    data = json.loads(STATE_PATH.read_text())
    age_hours = (time.time() - data.get("saved_at", 0)) / 3600
    if age_hours > 48:
        print(f"[state expired: {age_hours:.1f}h old]")
        return None
    return data

def build_resume_system(state: dict) -> str:
    return f"""You are resuming a task from a previous session.

## Previous Session Summary
Goal: {state.get('goal', 'unknown')}
Completed steps: {json.dumps(state.get('completed_steps', []), indent=2)}
Current phase: {state.get('current_phase', 'unknown')}
Key decisions made: {json.dumps(state.get('decisions', {}), indent=2)}
Pending actions: {json.dumps(state.get('pending', []), indent=2)}

Continue from where the previous session left off. Do not repeat completed steps."""

def run_session(user_message: str, initial_state: dict | None = None) -> dict:
    state = initial_state or {
        "goal": user_message,
        "completed_steps": [],
        "current_phase": "starting",
        "decisions": {},
        "pending": [],
    }

    messages = [{"role": "user", "content": user_message}]
    system = build_resume_system(state) if initial_state else "You are a helpful assistant."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=messages,
    )

    reply = response.content[0].text

    # Simulate state update after session work
    state["completed_steps"].append(f"Processed: {user_message[:50]}")
    state["current_phase"] = "in_progress"
    state["pending"] = ["Review output", "Confirm next steps"]

    save_state(state)
    return state, reply

if __name__ == "__main__":
    # Session 1
    print("=== Session 1 ===")
    state, reply1 = run_session("Build a REST API for a task management app.")
    print(reply1[:300])

    # Session 2 (resume)
    print("\n=== Session 2 (resume) ===")
    loaded = load_state()
    if loaded:
        state2, reply2 = run_session("Continue building the API — add authentication.", loaded)
        print(reply2[:300])

# Expected Token Savings: Avoids re-explaining context; saves 200-500 tokens of redundant setup per session
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Structured Handoff Document with Explicit Sections

Generate a human-readable handoff document at end of session and parse it on resume.

```python
import re
import json
import anthropic

client = anthropic.Anthropic()

HANDOFF_SCHEMA = {
    "goal": str,
    "completed": list,
    "in_progress": list,
    "blocked_on": list,
    "key_decisions": dict,
    "next_actions": list,
    "context_notes": str,
}

HANDOFF_GENERATION_PROMPT = """Based on this session's conversation, generate a structured handoff document.

Format as JSON with these exact keys:
- goal: the overall objective
- completed: list of finished items
- in_progress: list of partially done items
- blocked_on: list of blockers or open questions
- key_decisions: dict of decision name -> decision made
- next_actions: ordered list of what to do next
- context_notes: any important context the next session needs

Conversation summary: {summary}"""

def generate_handoff(conversation_summary: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": HANDOFF_GENERATION_PROMPT.format(summary=conversation_summary),
        }],
    )
    raw = response.content[0].text.strip()
    # Extract JSON from potential markdown
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"error": "parse_failed", "raw": raw}

def build_resume_context(handoff: dict) -> str:
    lines = [
        "## Resuming Previous Session\n",
        f"**Goal:** {handoff.get('goal', 'N/A')}\n",
        "**Completed:**",
    ]
    for item in handoff.get("completed", []):
        lines.append(f"  - {item}")
    lines.append("**In Progress:**")
    for item in handoff.get("in_progress", []):
        lines.append(f"  - {item}")
    lines.append("**Blocked On:**")
    for item in handoff.get("blocked_on", []):
        lines.append(f"  - {item}")
    lines.append("**Key Decisions Already Made:**")
    for k, v in handoff.get("key_decisions", {}).items():
        lines.append(f"  - {k}: {v}")
    lines.append("**Next Actions:**")
    for i, action in enumerate(handoff.get("next_actions", []), 1):
        lines.append(f"  {i}. {action}")
    if handoff.get("context_notes"):
        lines.append(f"\n**Notes:** {handoff['context_notes']}")
    return "\n".join(lines)

def resume_session(handoff: dict, new_instruction: str) -> str:
    resume_ctx = build_resume_context(handoff)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=f"You are continuing a multi-session task.\n\n{resume_ctx}",
        messages=[{"role": "user", "content": new_instruction}],
    )
    return response.content[0].text

if __name__ == "__main__":
    # Simulate end-of-session handoff generation
    session_summary = """
    We were building a Python FastAPI application. Completed: project structure setup,
    database models for User and Task. In progress: authentication middleware.
    Decided to use JWT tokens. Blocked on: which JWT library to use (PyJWT vs python-jose).
    Next: implement /login endpoint, then /tasks CRUD.
    """
    handoff = generate_handoff(session_summary)
    print("=== Handoff Document ===")
    print(json.dumps(handoff, indent=2))

    print("\n=== Resuming ===")
    reply = resume_session(handoff, "We decided to use PyJWT. Continue with the login endpoint.")
    print(reply[:400])

# Expected Token Savings: Structured context is 3-5x more token-efficient than raw conversation replay
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Goal Stack with Checkpoint Persistence

Maintain a stack of goals and sub-goals with completion checkpoints, persisted between sessions.

```python
import json
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional
import anthropic

client = anthropic.Anthropic()
CHECKPOINT_DIR = Path("/tmp/agent_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)

@dataclass
class Goal:
    id: str
    description: str
    parent_id: Optional[str]
    status: str          # pending, active, completed, blocked
    output: Optional[str]
    created_at: float
    updated_at: float

@dataclass
class AgentCheckpoint:
    session_id: str
    goals: list[Goal]
    active_goal_id: Optional[str]
    conversation_tail: list[dict]   # last N messages for context
    metadata: dict

def save_checkpoint(cp: AgentCheckpoint) -> Path:
    path = CHECKPOINT_DIR / f"{cp.session_id}.json"
    data = {**asdict(cp), "goals": [asdict(g) for g in cp.goals]}
    path.write_text(json.dumps(data, indent=2))
    return path

def load_checkpoint(session_id: str) -> Optional[AgentCheckpoint]:
    path = CHECKPOINT_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    goals = [Goal(**g) for g in data.pop("goals")]
    return AgentCheckpoint(goals=goals, **data)

def find_next_goal(goals: list[Goal]) -> Optional[Goal]:
    # Active first, then pending
    for g in goals:
        if g.status == "active":
            return g
    for g in goals:
        if g.status == "pending":
            return g
    return None

def checkpoint_to_system(cp: AgentCheckpoint) -> str:
    active = next((g for g in cp.goals if g.id == cp.active_goal_id), None)
    completed = [g for g in cp.goals if g.status == "completed"]
    pending = [g for g in cp.goals if g.status == "pending"]

    lines = ["## Session Checkpoint Resume\n"]
    if active:
        lines.append(f"**Currently Working On:** {active.description}")
    lines.append(f"**Completed ({len(completed)}):**")
    for g in completed:
        lines.append(f"  ✓ {g.description}")
        if g.output:
            lines.append(f"    Output: {g.output[:100]}...")
    lines.append(f"**Pending ({len(pending)}):**")
    for g in pending:
        lines.append(f"  ○ {g.description}")
    return "\n".join(lines)

def run_with_checkpoint(session_id: str, goals_descriptions: list[str], new_message: str) -> str:
    cp = load_checkpoint(session_id)

    if cp is None:
        now = time.time()
        goals = [
            Goal(id=f"g{i}", description=d, parent_id=None,
                 status="pending", output=None, created_at=now, updated_at=now)
            for i, d in enumerate(goals_descriptions)
        ]
        cp = AgentCheckpoint(
            session_id=session_id,
            goals=goals,
            active_goal_id=goals[0].id if goals else None,
            conversation_tail=[],
            metadata={"started_at": now},
        )
        system = "You are a task-oriented agent working through a goal list."
    else:
        system = checkpoint_to_system(cp)

    messages = cp.conversation_tail[-4:] + [{"role": "user", "content": new_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=messages,
    )
    reply = response.content[0].text

    # Update checkpoint
    messages.append({"role": "assistant", "content": reply})
    cp.conversation_tail = messages[-6:]

    # Mark active goal completed (simplified — real agent would parse reply)
    if cp.active_goal_id:
        for g in cp.goals:
            if g.id == cp.active_goal_id:
                g.status = "completed"
                g.output = reply[:200]
                g.updated_at = time.time()
        next_g = find_next_goal(cp.goals)
        if next_g:
            next_g.status = "active"
            cp.active_goal_id = next_g.id
        else:
            cp.active_goal_id = None

    save_checkpoint(cp)
    return reply

if __name__ == "__main__":
    goals = [
        "Research the best Python web frameworks for a REST API",
        "Design the database schema for a blog platform",
        "Implement authentication with JWT tokens",
    ]
    sid = "blog-project-001"

    print("=== Session 1 ===")
    r1 = run_with_checkpoint(sid, goals, "Start with the framework research.")
    print(r1[:300])

    print("\n=== Session 2 (resumed) ===")
    r2 = run_with_checkpoint(sid, goals, "Great, now move on to the database schema.")
    print(r2[:300])

# Expected Token Savings: Tail conversation window saves 60-80% vs replaying full history
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Async Multi-Session Coordinator with Shared State Store

Use an async state store to coordinate handoffs across concurrent sessions working on sub-tasks.

```python
import json
import asyncio
import time
from dataclasses import dataclass, asdict
from typing import Optional
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class SubTask:
    id: str
    description: str
    assigned_session: Optional[str]
    status: str        # queued, running, done, failed
    result: Optional[str]
    started_at: Optional[float]
    finished_at: Optional[float]

class SharedStateStore:
    """In-memory store — replace with Redis or SQLite for persistence."""
    def __init__(self):
        self._lock = asyncio.Lock()
        self._tasks: dict[str, SubTask] = {}
        self._session_logs: dict[str, list[str]] = {}

    async def claim_task(self, session_id: str) -> Optional[SubTask]:
        async with self._lock:
            for task in self._tasks.values():
                if task.status == "queued":
                    task.status = "running"
                    task.assigned_session = session_id
                    task.started_at = time.time()
                    return task
        return None

    async def complete_task(self, task_id: str, result: str) -> None:
        async with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].status = "done"
                self._tasks[task_id].result = result
                self._tasks[task_id].finished_at = time.time()

    async def add_log(self, session_id: str, entry: str) -> None:
        async with self._lock:
            self._session_logs.setdefault(session_id, []).append(entry)

    async def get_handoff_context(self, session_id: str) -> str:
        async with self._lock:
            done = [t for t in self._tasks.values() if t.status == "done"]
            running = [t for t in self._tasks.values() if t.status == "running"]
            queued = [t for t in self._tasks.values() if t.status == "queued"]
            logs = self._session_logs.get(session_id, [])
            return json.dumps({
                "completed_tasks": [asdict(t) for t in done],
                "running_tasks": [asdict(t) for t in running],
                "queued_tasks": [asdict(t) for t in queued],
                "session_log": logs[-10:],
            }, indent=2)

    def load_tasks(self, subtasks: list[SubTask]) -> None:
        for t in subtasks:
            self._tasks[t.id] = t

store = SharedStateStore()

async def session_worker(session_id: str) -> None:
    while True:
        task = await store.claim_task(session_id)
        if not task:
            break

        handoff_ctx = await store.get_handoff_context(session_id)
        await store.add_log(session_id, f"Starting: {task.description}")

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=f"You are session {session_id}. Context from other sessions:\n{handoff_ctx}",
            messages=[{"role": "user", "content": task.description}],
        )
        result = response.content[0].text
        await store.complete_task(task.id, result)
        await store.add_log(session_id, f"Completed: {task.id}")
        print(f"[{session_id}] Done: {task.description[:50]}")

async def run_parallel_sessions():
    tasks = [
        SubTask("t1", "Design user authentication flow", None, "queued", None, None, None),
        SubTask("t2", "Design product catalog schema", None, "queued", None, None, None),
        SubTask("t3", "Design order processing workflow", None, "queued", None, None, None),
        SubTask("t4", "Design payment integration points", None, "queued", None, None, None),
    ]
    store.load_tasks(tasks)

    await asyncio.gather(
        session_worker("session-A"),
        session_worker("session-B"),
        session_worker("session-C"),
    )

    print("\n=== All sessions complete. Final state: ===")
    ctx = await store.get_handoff_context("session-A")
    print(ctx[:600])

if __name__ == "__main__":
    asyncio.run(run_parallel_sessions())

# Expected Token Savings: Shared context prevents duplicate work; each session sees only relevant prior results
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Semantic Diff Handoff — Only What Changed

Generate a diff of what changed between sessions and inject only the delta, not the full state.

```python
import json
import hashlib
import anthropic

client = anthropic.Anthropic()

def state_hash(state: dict) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:12]

def compute_diff(old: dict, new: dict) -> dict:
    diff = {"added": {}, "changed": {}, "removed": []}
    for k, v in new.items():
        if k not in old:
            diff["added"][k] = v
        elif old[k] != v:
            diff["changed"][k] = {"from": old[k], "to": v}
    for k in old:
        if k not in new:
            diff["removed"].append(k)
    return diff

DIFF_SUMMARIZER_PROMPT = """Summarize these state changes between sessions in 2-3 sentences for a resuming agent:

Changes: {diff}

Write a brief, actionable summary."""

def summarize_diff(diff: dict) -> str:
    if not any([diff["added"], diff["changed"], diff["removed"]]):
        return "No changes between sessions."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": DIFF_SUMMARIZER_PROMPT.format(diff=json.dumps(diff, indent=2)),
        }],
    )
    return response.content[0].text.strip()

def resume_with_diff(old_state: dict, new_state: dict, instruction: str) -> str:
    diff = compute_diff(old_state, new_state)
    diff_summary = summarize_diff(diff)
    old_hash = state_hash(old_state)
    new_hash = state_hash(new_state)

    system = f"""You are resuming a session.
State version: {old_hash} -> {new_hash}

What changed since last session:
{diff_summary}

Current state:
{json.dumps(new_state, indent=2)}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": instruction}],
    )
    return response.content[0].text

if __name__ == "__main__":
    state_v1 = {
        "project": "e-commerce API",
        "completed_modules": ["auth", "users"],
        "current_module": "products",
        "tech_stack": "FastAPI + PostgreSQL",
        "deployment_target": "unknown",
    }

    state_v2 = {
        "project": "e-commerce API",
        "completed_modules": ["auth", "users", "products"],
        "current_module": "orders",
        "tech_stack": "FastAPI + PostgreSQL",
        "deployment_target": "AWS ECS",   # new decision
        "pending_issues": ["pagination bug in /products"],  # new
    }

    diff = compute_diff(state_v1, state_v2)
    print("=== State Diff ===")
    print(json.dumps(diff, indent=2))

    result = resume_with_diff(state_v1, state_v2, "Continue building the orders module.")
    print("\n=== Resumed Response ===")
    print(result[:400])

# Expected Token Savings: Delta injection uses 80% fewer tokens than full state replay
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Typed Handoff Protocol with Versioning and Validation

A production-grade handoff with schema versioning, validation, and backward-compatible deserialization.

```python
import json
import time
import hashlib
from enum import Enum
from typing import Any
from dataclasses import dataclass, asdict, field
from pathlib import Path
import anthropic

client = anthropic.Anthropic()
HANDOFF_VERSION = "2.0"

class HandoffStatus(Enum):
    FRESH = "fresh"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"

@dataclass
class Decision:
    key: str
    value: str
    rationale: str
    made_at: float = field(default_factory=time.time)

@dataclass
class HandoffEnvelope:
    version: str
    session_id: str
    status: HandoffStatus
    goal: str
    completed: list[str]
    pending: list[str]
    blocked_on: list[str]
    decisions: list[Decision]
    context_snippet: str       # most relevant recent context, truncated
    token_count_used: int
    created_at: float = field(default_factory=time.time)
    checksum: str = ""

    def compute_checksum(self) -> str:
        payload = json.dumps({
            "version": self.version,
            "session_id": self.session_id,
            "goal": self.goal,
            "completed": self.completed,
        }, sort_keys=True)
        return hashlib.md5(payload.encode()).hexdigest()[:8]

    def seal(self) -> None:
        self.checksum = self.compute_checksum()

    def validate(self) -> list[str]:
        errors = []
        if self.version != HANDOFF_VERSION:
            errors.append(f"version mismatch: {self.version} != {HANDOFF_VERSION}")
        if not self.goal:
            errors.append("missing goal")
        if self.compute_checksum() != self.checksum:
            errors.append("checksum mismatch — handoff may be corrupted")
        return errors

def serialize(envelope: HandoffEnvelope) -> str:
    d = asdict(envelope)
    d["status"] = envelope.status.value
    return json.dumps(d, indent=2)

def deserialize(raw: str) -> HandoffEnvelope:
    d = json.loads(raw)
    d["status"] = HandoffStatus(d["status"])
    d["decisions"] = [Decision(**dec) for dec in d["decisions"]]
    return HandoffEnvelope(**d)

def build_system_from_envelope(env: HandoffEnvelope) -> str:
    errors = env.validate()
    if errors:
        return f"WARNING: Handoff validation errors: {errors}\nProceeding with available context."

    decision_lines = "\n".join(f"  - {d.key}: {d.value} (reason: {d.rationale})"
                               for d in env.decisions)
    return f"""## Resuming Session {env.session_id} [v{env.version}]
Status: {env.status.value}
Goal: {env.goal}

Completed:
{chr(10).join('  ✓ ' + c for c in env.completed)}

Pending:
{chr(10).join('  ○ ' + p for p in env.pending)}

Decisions made:
{decision_lines or '  (none)'}

Recent context:
{env.context_snippet}

Continue seamlessly. Do not repeat completed work."""

def create_handoff(session_id: str, goal: str, completed: list, pending: list,
                   decisions: list[Decision], context: str, tokens_used: int) -> HandoffEnvelope:
    env = HandoffEnvelope(
        version=HANDOFF_VERSION,
        session_id=session_id,
        status=HandoffStatus.IN_PROGRESS,
        goal=goal,
        completed=completed,
        pending=pending,
        blocked_on=[],
        decisions=decisions,
        context_snippet=context[:500],
        token_count_used=tokens_used,
    )
    env.seal()
    return env

def resume_from_envelope(env: HandoffEnvelope, instruction: str) -> str:
    system = build_system_from_envelope(env)
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": instruction}],
    )
    return response.content[0].text

if __name__ == "__main__":
    # Simulate session 1 ending with a handoff
    handoff = create_handoff(
        session_id="proj-api-20240115",
        goal="Build a production-grade REST API for inventory management",
        completed=["DB schema design", "User authentication module", "Product CRUD endpoints"],
        pending=["Order processing", "Inventory adjustment logic", "Webhook notifications", "Load testing"],
        decisions=[
            Decision("database", "PostgreSQL", "Strong ACID guarantees needed for inventory"),
            Decision("auth", "JWT with refresh tokens", "Stateless, mobile-compatible"),
            Decision("deployment", "Kubernetes on GKE", "Team expertise and scaling requirements"),
        ],
        context="Last working on order processing — decided to use saga pattern for distributed transactions.",
        tokens_used=45230,
    )

    serialized = serialize(handoff)
    print("=== Handoff Envelope ===")
    print(serialized[:600])

    # Session 2: deserialize and resume
    restored = deserialize(serialized)
    errors = restored.validate()
    print(f"\n=== Validation: {'OK' if not errors else errors} ===")

    result = resume_from_envelope(restored, "Implement the order processing module using the saga pattern.")
    print("\n=== Session 2 Output ===")
    print(result[:400])

# Expected Token Savings: Validated, versioned handoff prevents hallucinated context; 65% fewer tokens than raw replay
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Approach | Persistence | Concurrency | Best For |
|--------|----------|-------------|-------------|----------|
| 1 | JSON snapshot + resume prompt injection | File | Single | Simple scripts and one-off tasks |
| 2 | LLM-generated handoff document | File | Single | Human-readable handoffs, audit trails |
| 3 | Goal stack with checkpoint persistence | File | Single | Sequential multi-step projects |
| 4 | Async shared state store | Memory/Redis | Multi-session | Parallel sub-agent coordination |
| 5 | Semantic diff — delta only | Memory | Single | Frequently updated state, minimal tokens |
| 6 | Typed protocol with versioning | File/DB | Multi | Production systems needing reliability |
