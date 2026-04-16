---
title: "Agent Doesn't Implement Agent Pause and Resume on Demand"
description: "AI agents running long multi-step tasks have no mechanism for operators to pause execution mid-workflow—to inspect state, await a human decision, or hold during a maintenance window—without losing progress. Pause-and-resume allows an agent to serialize its current execution state to durable storage, halt cleanly, and restart from the exact same point later, even across process restarts."
date: 2025-02-21
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-agent-pause-and-resume-on-demand
tags:
  - pause-resume
  - human-in-the-loop
  - state-serialization
  - long-running-tasks
  - reliability
  - operator-control
  - workflow-control
symptoms:
  - "Operator must kill an agent mid-task to apply an emergency config change, losing 40 minutes of progress"
  - "No way to inject a human approval step into an in-flight multi-step workflow"
  - "Agent cannot be paused during a deployment window and resumed afterward"
  - "Long-running research agent loses all accumulated context when the server restarts for updates"
  - "No mechanism to throttle an agent that is spending too fast without aborting the task"
---

## Problem

Multi-step agent workflows that run for minutes or hours have no built-in suspension mechanism. When an operator needs to pause execution—to review intermediate results, await a human approval, throttle API spend, or survive a planned maintenance window—the only option is termination, discarding all accumulated progress. Pause-and-resume serializes the agent's current state (completed steps, accumulated context, tool results, pending queue) to durable storage at a well-defined checkpoint, allows the process to exit cleanly, and restores from that snapshot on the next invocation.

---

## Solution 1: PauseSignalHandler — Operator-Triggered Pause via Signal or Flag File

```python
import asyncio
import logging
import os
import signal
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PauseSignalHandler:
    """
    Watches for a pause request via SIGUSR1 or a flag file at
    {pause_file_path}. When triggered, sets a pause_requested flag
    that the agent checks at each tool call boundary. The agent then
    serializes state and exits cleanly.

    Usage:
        handler = PauseSignalHandler(pause_file="/tmp/agent-pause.flag")
        handler.install()

        # In the agent loop:
        if handler.pause_requested:
            await handler.acknowledge_pause()
            return  # agent serializes state before returning
    """

    def __init__(
        self,
        pause_file: str = "/tmp/agent-pause.flag",
        check_interval: float = 1.0,
    ):
        self._pause_file = Path(pause_file)
        self._check_interval = check_interval
        self._pause_requested = False
        self._paused_at: Optional[float] = None
        self._monitor_task: Optional[asyncio.Task] = None

    def install(self):
        """Install SIGUSR1 handler and start file monitor."""
        signal.signal(signal.SIGUSR1, self._signal_handler)
        logger.info("pause_handler_installed pause_file=%s", self._pause_file)

    def _signal_handler(self, signum, frame):
        logger.info("pause_signal_received sig=%d", signum)
        self._pause_requested = True
        self._paused_at = time.time()

    async def start_file_monitor(self):
        """Poll the pause flag file asynchronously."""
        async def _monitor():
            while True:
                if self._pause_file.exists():
                    if not self._pause_requested:
                        logger.info("pause_file_detected path=%s", self._pause_file)
                        self._pause_requested = True
                        self._paused_at = time.time()
                await asyncio.sleep(self._check_interval)
        self._monitor_task = asyncio.create_task(_monitor())

    @property
    def pause_requested(self) -> bool:
        return self._pause_requested

    async def acknowledge_pause(self):
        """Called by the agent after serializing state. Removes the flag file."""
        if self._pause_file.exists():
            self._pause_file.unlink(missing_ok=True)
        if self._monitor_task:
            self._monitor_task.cancel()
        logger.info("pause_acknowledged paused_at=%.0f", self._paused_at or time.time())

    def clear(self):
        """Reset pause state on resume."""
        self._pause_requested = False
        self._paused_at = None
```

---

## Solution 2: AgentStateSnapshot — Serializable Execution State

```python
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class AgentStateSnapshot:
    """
    Captures the full resumable state of an agent mid-execution.
    All fields must be JSON-serializable. Tool results are stored
    as their text representations to avoid re-fetching on resume.

    Usage:
        snapshot = AgentStateSnapshot.capture(
            task_id="task-001",
            completed_steps=["search", "summarize"],
            pending_steps=["write_report", "send_email"],
            accumulated_context=messages,
            tool_results=results_cache,
        )
        snapshot.save("/var/lib/agent/snapshots/task-001.json")

        # On resume:
        snapshot = AgentStateSnapshot.load("/var/lib/agent/snapshots/task-001.json")
    """

    task_id: str
    paused_at: float
    completed_steps: List[str] = field(default_factory=list)
    pending_steps: List[str] = field(default_factory=list)
    accumulated_context: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: Dict[str, Any] = field(default_factory=dict)
    agent_variables: Dict[str, Any] = field(default_factory=dict)
    resume_hint: str = ""  # human-readable note for the operator
    schema_version: int = 1

    @classmethod
    def capture(
        cls,
        task_id: str,
        completed_steps: List[str],
        pending_steps: List[str],
        accumulated_context: List[Dict],
        tool_results: Optional[Dict] = None,
        agent_variables: Optional[Dict] = None,
        resume_hint: str = "",
    ) -> "AgentStateSnapshot":
        return cls(
            task_id=task_id,
            paused_at=time.time(),
            completed_steps=completed_steps,
            pending_steps=pending_steps,
            accumulated_context=accumulated_context,
            tool_results=tool_results or {},
            agent_variables=agent_variables or {},
            resume_hint=resume_hint,
        )

    def save(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(self), f, indent=2, default=str)
        import os
        os.replace(tmp, p)

    @classmethod
    def load(cls, path: str) -> "AgentStateSnapshot":
        with open(path) as f:
            data = json.load(f)
        return cls(**data)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.paused_at

    def is_stale(self, max_age_seconds: float = 86400) -> bool:
        return self.age_seconds > max_age_seconds
```

---

## Solution 3: PausableAgentLoop — Tool-Call Boundary Pause Checks

```python
import asyncio
import logging
from typing import Any, Callable, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


class PausableAgentLoop:
    """
    Wraps an agent's step-execution loop with pause checkpoints at each
    tool call boundary. When pause_handler.pause_requested is True, the
    loop serializes state and yields control to the caller instead of
    continuing to the next step.

    Usage:
        loop = PausableAgentLoop(
            pause_handler=handler,
            snapshot_fn=lambda state: state.save("/tmp/snapshot.json"),
        )
        async for step_result in loop.run(steps, execute_step_fn, initial_state):
            log(step_result)
        if loop.paused:
            logger.info("Agent paused at step %d", loop.completed_count)
    """

    def __init__(
        self,
        pause_handler: Any,
        snapshot_fn: Optional[Callable] = None,
    ):
        self._handler = pause_handler
        self._snapshot_fn = snapshot_fn
        self._completed: List[str] = []
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def completed_count(self) -> int:
        return len(self._completed)

    async def run(
        self,
        steps: List[str],
        execute_step: Callable,
        context: Dict[str, Any],
    ):
        """
        Execute steps one by one, yielding results.
        Checks for pause between each step.
        """
        pending = list(steps)

        while pending:
            # Pause checkpoint
            if self._handler.pause_requested:
                logger.info(
                    "agent_pausing completed=%d pending=%d",
                    len(self._completed), len(pending),
                )
                self._paused = True
                if self._snapshot_fn:
                    snapshot = AgentStateSnapshot.capture(
                        task_id=context.get("task_id", "unknown"),
                        completed_steps=list(self._completed),
                        pending_steps=list(pending),
                        accumulated_context=context.get("messages", []),
                        agent_variables={k: v for k, v in context.items()
                                          if k not in ("messages",)},
                    )
                    self._snapshot_fn(snapshot)
                await self._handler.acknowledge_pause()
                return

            step = pending.pop(0)
            logger.info("agent_step_start step=%s", step)
            try:
                result = await execute_step(step, context) \
                    if asyncio.iscoroutinefunction(execute_step) \
                    else execute_step(step, context)
                self._completed.append(step)
                context.setdefault("step_results", {})[step] = result
                logger.info("agent_step_complete step=%s", step)
                yield step, result
            except Exception as exc:
                logger.error("agent_step_failed step=%s error=%s", step, exc)
                raise
```

---

## Solution 4: SnapshotStore — Multi-Agent Snapshot Registry

```python
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SnapshotStore:
    """
    Manages snapshots for multiple concurrent agent tasks in a directory.
    Provides list, load, delete operations for operator tooling (CLI,
    dashboard) to inspect and manage paused agents.

    Usage:
        store = SnapshotStore("/var/lib/agent/snapshots")
        store.save(snapshot)
        paused = store.list_paused()
        snap = store.load("task-001")
        store.delete("task-001")
    """

    def __init__(self, base_dir: str):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        safe = task_id.replace("/", "_").replace("..", "_")
        return self._base / f"{safe}.json"

    def save(self, snapshot: AgentStateSnapshot):
        path = self._path(snapshot.task_id)
        snapshot.save(str(path))
        logger.info("snapshot_saved task_id=%s path=%s", snapshot.task_id, path)

    def load(self, task_id: str) -> Optional[AgentStateSnapshot]:
        path = self._path(task_id)
        if not path.exists():
            return None
        return AgentStateSnapshot.load(str(path))

    def delete(self, task_id: str):
        path = self._path(task_id)
        if path.exists():
            path.unlink()
            logger.info("snapshot_deleted task_id=%s", task_id)

    def list_paused(self) -> List[Dict]:
        result = []
        for p in sorted(self._base.glob("*.json")):
            try:
                with open(p) as f:
                    data = json.load(f)
                result.append({
                    "task_id": data.get("task_id"),
                    "paused_at": data.get("paused_at"),
                    "completed_steps": len(data.get("completed_steps", [])),
                    "pending_steps": len(data.get("pending_steps", [])),
                    "resume_hint": data.get("resume_hint", ""),
                    "path": str(p),
                })
            except Exception as exc:
                logger.warning("snapshot_read_error path=%s error=%s", p, exc)
        return result

    def exists(self, task_id: str) -> bool:
        return self._path(task_id).exists()
```

---

## Solution 5: HumanApprovalGate — Pause Pending Human Review

```python
import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class HumanApprovalGate:
    """
    Pauses agent execution at designated checkpoints requiring human
    approval before continuing. Stores the pending decision in a shared
    store (Redis, file, DB) and polls for the decision at a configurable
    interval. Operator approves or rejects via a separate CLI or dashboard.

    Usage:
        gate = HumanApprovalGate(
            store=approval_store,     # dict-like with get/set
            agent_id="agent-A",
            timeout=3600,             # max wait: 1 hour
        )
        approved = await gate.request_approval(
            checkpoint="before_send_email",
            context={"recipient": "ceo@acme.com", "subject": "Q4 Report"},
        )
        if not approved:
            raise RuntimeError("Human rejected email send")
    """

    POLL_INTERVAL = 5.0

    def __init__(
        self,
        store: Any,
        agent_id: str = "",
        timeout: float = 3600.0,
        notify_fn: Optional[Callable] = None,
    ):
        self._store = store
        self._agent_id = agent_id
        self._timeout = timeout
        self._notify = notify_fn

    def _key(self, checkpoint: str) -> str:
        return f"approval:{self._agent_id}:{checkpoint}"

    async def request_approval(self, checkpoint: str, context: Dict[str, Any]) -> bool:
        key = self._key(checkpoint)
        payload = {
            "agent_id": self._agent_id,
            "checkpoint": checkpoint,
            "context": context,
            "requested_at": time.time(),
            "status": "pending",
        }
        await self._store_set(key, payload)
        logger.info("approval_requested agent=%s checkpoint=%s", self._agent_id, checkpoint)

        if self._notify:
            try:
                await self._notify(payload) if asyncio.iscoroutinefunction(self._notify) \
                    else self._notify(payload)
            except Exception as exc:
                logger.warning("approval_notify_failed error=%s", exc)

        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            record = await self._store_get(key)
            if record and record.get("status") in ("approved", "rejected"):
                approved = record["status"] == "approved"
                logger.info("approval_decision agent=%s checkpoint=%s approved=%s",
                             self._agent_id, checkpoint, approved)
                await self._store_delete(key)
                return approved
            await asyncio.sleep(self.POLL_INTERVAL)

        logger.warning("approval_timeout agent=%s checkpoint=%s", self._agent_id, checkpoint)
        await self._store_delete(key)
        return False

    async def _store_set(self, key: str, value: Dict):
        if asyncio.iscoroutinefunction(getattr(self._store, "set", None)):
            await self._store.set(key, value)
        else:
            self._store[key] = value

    async def _store_get(self, key: str) -> Optional[Dict]:
        if asyncio.iscoroutinefunction(getattr(self._store, "get", None)):
            return await self._store.get(key)
        return self._store.get(key)

    async def _store_delete(self, key: str):
        if asyncio.iscoroutinefunction(getattr(self._store, "delete", None)):
            await self._store.delete(key)
        else:
            self._store.pop(key, None)
```

---

## Solution 6: ResumableAgentOrchestrator — Full Pause/Resume Lifecycle

```python
import asyncio
import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ResumableAgentOrchestrator:
    """
    Combines PauseSignalHandler, SnapshotStore, and PausableAgentLoop
    into a single orchestration layer. On startup, checks for an existing
    snapshot (resume mode) or begins fresh (new task). On pause, saves
    snapshot and exits. Operator restarts the process to resume.

    Usage:
        orch = ResumableAgentOrchestrator(
            task_id="daily-research-001",
            snapshot_dir="/var/lib/agent/snapshots",
            execute_step_fn=execute_research_step,
            pause_file="/tmp/agent-pause.flag",
        )
        await orch.run(steps=["search", "analyze", "draft", "review", "publish"])
    """

    def __init__(
        self,
        task_id: str,
        snapshot_dir: str,
        execute_step_fn: Callable,
        pause_file: str = "/tmp/agent-pause.flag",
    ):
        self._task_id = task_id
        self._store = SnapshotStore(snapshot_dir)
        self._execute = execute_step_fn
        self._pause_handler = PauseSignalHandler(pause_file=pause_file)

    async def run(self, steps: List[str], initial_context: Optional[Dict] = None):
        self._pause_handler.install()
        await self._pause_handler.start_file_monitor()

        # Check for existing snapshot
        snapshot = self._store.load(self._task_id)
        if snapshot:
            if snapshot.is_stale(max_age_seconds=7 * 86400):
                logger.warning("snapshot_stale task_id=%s age_s=%.0f — starting fresh",
                                self._task_id, snapshot.age_seconds)
                self._store.delete(self._task_id)
                snapshot = None
            else:
                logger.info(
                    "snapshot_found task_id=%s completed=%d pending=%d",
                    self._task_id,
                    len(snapshot.completed_steps),
                    len(snapshot.pending_steps),
                )

        if snapshot:
            remaining_steps = snapshot.pending_steps
            context = {
                "task_id": self._task_id,
                "messages": snapshot.accumulated_context,
                **snapshot.agent_variables,
                "step_results": snapshot.tool_results,
            }
        else:
            remaining_steps = list(steps)
            context = {**(initial_context or {}), "task_id": self._task_id}

        loop = PausableAgentLoop(
            pause_handler=self._pause_handler,
            snapshot_fn=self._store.save,
        )

        async for step, result in loop.run(remaining_steps, self._execute, context):
            logger.info("orchestrator_step_done step=%s", step)

        if loop.paused:
            logger.info("orchestrator_paused task_id=%s — restart to resume", self._task_id)
        else:
            self._store.delete(self._task_id)
            logger.info("orchestrator_complete task_id=%s", self._task_id)
```

---

## Comparison

| Approach | Pause Trigger | State Serialization | Human Approval | Resume Detection | Multi-Agent | Integrated |
|---|---|---|---|---|---|---|
| **PauseSignalHandler** | SIGUSR1 + file | No | No | No | No | No |
| **AgentStateSnapshot** | N/A | Yes (JSON) | No | No | No | No |
| **PausableAgentLoop** | Via handler | Via callback | No | No | No | No |
| **SnapshotStore** | N/A | N/A | No | Yes | Yes | No |
| **HumanApprovalGate** | Checkpoint | No | Yes | No | No | No |
| **ResumableAgentOrchestrator** | Signal + file | Yes | No | Yes | No | Yes |

**Key insight**: the minimum viable implementation is `AgentStateSnapshot.capture()` + `snapshot.save()` called in a `finally` block of the agent entrypoint, combined with a startup check for an existing snapshot file. This gives pause-and-resume across process restarts with under 50 lines of code. Add `PauseSignalHandler` with `SIGUSR1` for operator-triggered pauses without process termination. For human-in-the-loop workflows, `HumanApprovalGate` with a Redis backend lets a separate operator dashboard approve or reject decisions at specific checkpoints, converting fully-autonomous agents into supervised workflows when needed.
