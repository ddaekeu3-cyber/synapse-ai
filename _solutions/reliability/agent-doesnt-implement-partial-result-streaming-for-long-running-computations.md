---
title: "Agent Doesn't Implement Partial Result Streaming for Long-Running Computations"
description: "Agents that execute multi-step computations synchronously block the user until the entire result is ready. A data analysis job, a multi-document summarization, or a code generation task that takes 30 seconds produces no output for the first 29 seconds, then dumps everything at once. Implement partial result streaming that emits intermediate results as steps complete, keeps the user informed of progress, and delivers value progressively rather than all-at-once."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-partial-result-streaming-for-long-running-computations
tags: [partial-results, progressive-streaming, incremental-output, long-running-tasks, user-feedback, streaming-pipeline]
symptoms:
  - "User sees a spinner for 45 seconds then receives the full response at once"
  - "No progress indication during multi-step tool workflows"
  - "A timeout kills the request at 30s even though the first useful results were ready at 5s"
  - "The connection drops mid-computation and the user receives nothing instead of partial results"
  - "Long jobs produce no intermediate artifacts — if they fail, there is nothing to resume from"
---

## Why This Happens

Synchronous agent execution accumulates all results before returning. This is correct for short tasks but wrong for long ones: users prefer early partial information over a long wait for complete information. Partial result streaming requires restructuring the execution loop to emit results as soon as each step completes, format them for the transport layer (SSE, WebSocket, or async generator), and handle client disconnection gracefully without losing work that has already been computed.

## Solution 1: Partial Result Event

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class PartialResultType(str, Enum):
    PROGRESS = "progress"         # status update, no result data
    INTERMEDIATE = "intermediate" # partial data ready for consumption
    FINAL = "final"               # last result, computation complete
    ERROR = "error"               # step or task failed
    HEARTBEAT = "heartbeat"       # keep-alive, no data


@dataclass
class PartialResultEvent:
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str = ""
    event_type: PartialResultType = PartialResultType.PROGRESS
    step_name: str = ""
    step_index: int = 0
    total_steps: Optional[int] = None
    data: Any = None
    progress_pct: Optional[float] = None
    message: str = ""
    emitted_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        """Format as Server-Sent Events wire format."""
        import json
        payload = {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "type": self.event_type,
            "step": self.step_name,
            "step_index": self.step_index,
            "total_steps": self.total_steps,
            "progress_pct": self.progress_pct,
            "message": self.message,
            "data": self.data,
            "ts": self.emitted_at,
        }
        data_str = json.dumps(payload, default=str)
        return f"event: {self.event_type}\ndata: {data_str}\n\n"
```

## Solution 2: Streaming Step Executor

```python
import asyncio
import time
from typing import Any, AsyncGenerator, Callable, List, Optional


class StreamingStepExecutor:
    """
    Executes a list of named steps and yields a PartialResultEvent after each.
    Steps are async callables; their return values are emitted as INTERMEDIATE results.
    Progress percentage is computed from step position.
    """

    def __init__(self, task_id: str, heartbeat_interval_seconds: float = 5.0):
        self._task_id = task_id
        self._heartbeat_interval = heartbeat_interval_seconds
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    async def execute(
        self,
        steps: List[tuple],   # [(step_name, step_fn, step_args, step_kwargs)]
    ) -> AsyncGenerator[PartialResultEvent, None]:
        total = len(steps)
        last_heartbeat = time.time()

        for i, (step_name, step_fn, step_args, step_kwargs) in enumerate(steps):
            if self._cancelled:
                yield PartialResultEvent(
                    task_id=self._task_id,
                    event_type=PartialResultType.ERROR,
                    step_name=step_name,
                    step_index=i,
                    total_steps=total,
                    message="Task cancelled",
                )
                return

            # Emit progress before step
            yield PartialResultEvent(
                task_id=self._task_id,
                event_type=PartialResultType.PROGRESS,
                step_name=step_name,
                step_index=i,
                total_steps=total,
                progress_pct=round(i / total * 100, 1),
                message=f"Starting: {step_name}",
            )

            try:
                result = await step_fn(*step_args, **step_kwargs)

                is_final = (i == total - 1)
                yield PartialResultEvent(
                    task_id=self._task_id,
                    event_type=PartialResultType.FINAL if is_final else PartialResultType.INTERMEDIATE,
                    step_name=step_name,
                    step_index=i,
                    total_steps=total,
                    data=result,
                    progress_pct=round((i + 1) / total * 100, 1),
                    message=f"Completed: {step_name}",
                )

            except Exception as exc:
                yield PartialResultEvent(
                    task_id=self._task_id,
                    event_type=PartialResultType.ERROR,
                    step_name=step_name,
                    step_index=i,
                    total_steps=total,
                    message=str(exc)[:200],
                    data={"error": str(exc), "step": step_name},
                )
                return

            # Heartbeat if step took a long time
            if time.time() - last_heartbeat > self._heartbeat_interval:
                yield PartialResultEvent(
                    task_id=self._task_id,
                    event_type=PartialResultType.HEARTBEAT,
                    message="alive",
                )
                last_heartbeat = time.time()
```

## Solution 3: Result Accumulator

```python
from typing import Any, Dict, List, Optional


class PartialResultAccumulator:
    """
    Accumulates intermediate results from a streaming execution.
    Allows reconnecting clients to receive all results emitted so far.
    Stores the last N events per task for replay on reconnect.
    """

    def __init__(self, max_events_per_task: int = 100):
        self._max = max_events_per_task
        self._events: Dict[str, List[PartialResultEvent]] = {}
        self._final_reached: Dict[str, bool] = {}

    def record(self, event: PartialResultEvent) -> None:
        task_id = event.task_id
        if task_id not in self._events:
            self._events[task_id] = []
        self._events[task_id].append(event)
        if len(self._events[task_id]) > self._max:
            self._events[task_id].pop(0)
        if event.event_type == PartialResultType.FINAL:
            self._final_reached[task_id] = True

    def replay(self, task_id: str) -> List[PartialResultEvent]:
        return list(self._events.get(task_id, []))

    def is_complete(self, task_id: str) -> bool:
        return self._final_reached.get(task_id, False)

    def intermediate_results(self, task_id: str) -> List[Any]:
        events = self._events.get(task_id, [])
        return [
            e.data for e in events
            if e.event_type in (PartialResultType.INTERMEDIATE, PartialResultType.FINAL)
            and e.data is not None
        ]
```

## Solution 4: SSE Stream Handler

```python
import asyncio
from typing import Any, AsyncGenerator, Callable, Optional


class SSEStreamHandler:
    """
    Wraps a streaming executor and produces SSE-formatted output.
    Handles client disconnection by cancelling the task gracefully.
    Supports reconnection by replaying buffered events from the accumulator.
    """

    def __init__(
        self,
        executor: StreamingStepExecutor,
        accumulator: PartialResultAccumulator,
    ):
        self._executor = executor
        self._accumulator = accumulator

    async def stream(
        self,
        steps: list,
        last_event_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Yields SSE-formatted strings.
        If last_event_id is provided, replay buffered events first.
        """
        task_id = self._executor._task_id

        # Replay buffered events for reconnecting clients
        if last_event_id:
            for event in self._accumulator.replay(task_id):
                yield event.to_sse()
            if self._accumulator.is_complete(task_id):
                return

        async for event in self._executor.execute(steps):
            self._accumulator.record(event)
            yield event.to_sse()

            if event.event_type in (PartialResultType.FINAL, PartialResultType.ERROR):
                break
```

## Solution 5: Partial Result Checkpoint Persister

```python
import json
import os
import time
from typing import Any, Dict, List, Optional


class PartialResultCheckpointPersister:
    """
    Persists completed step results to disk so that if the process restarts,
    completed work is not lost and the task can resume from the last checkpoint.
    """

    def __init__(self, checkpoint_dir: str = "/tmp/agent_partial_checkpoints"):
        self._dir = checkpoint_dir
        os.makedirs(checkpoint_dir, exist_ok=True)

    def _path(self, task_id: str) -> str:
        return os.path.join(self._dir, f"{task_id}.json")

    def save_step(self, task_id: str, step_name: str, result: Any) -> None:
        path = self._path(task_id)
        data = self._load_raw(task_id)
        data["steps"][step_name] = {
            "result": result,
            "saved_at": time.time(),
        }
        data["updated_at"] = time.time()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, default=str)
        os.replace(tmp, path)

    def get_completed_steps(self, task_id: str) -> Dict[str, Any]:
        data = self._load_raw(task_id)
        return {name: s["result"] for name, s in data.get("steps", {}).items()}

    def _load_raw(self, task_id: str) -> dict:
        path = self._path(task_id)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {"task_id": task_id, "steps": {}, "created_at": time.time(), "updated_at": time.time()}

    def cleanup(self, task_id: str) -> None:
        path = self._path(task_id)
        if os.path.exists(path):
            os.remove(path)
```

## Solution 6: Streaming Task Monitor

```python
import time
from typing import Dict


class StreamingTaskMonitor:
    """
    Tracks active streaming tasks and their progress.
    Detects stalled tasks that have not emitted an event for too long.
    """

    def __init__(self, stall_threshold_seconds: float = 30.0):
        self._stall_threshold = stall_threshold_seconds
        self._tasks: Dict[str, dict] = {}

    def register(self, task_id: str, total_steps: int) -> None:
        self._tasks[task_id] = {
            "total_steps": total_steps,
            "completed_steps": 0,
            "last_event_at": time.time(),
            "started_at": time.time(),
            "done": False,
        }

    def on_event(self, event: PartialResultEvent) -> None:
        task = self._tasks.get(event.task_id)
        if not task:
            return
        task["last_event_at"] = time.time()
        if event.event_type == PartialResultType.INTERMEDIATE:
            task["completed_steps"] += 1
        elif event.event_type in (PartialResultType.FINAL, PartialResultType.ERROR):
            task["done"] = True
            task["completed_steps"] = task["total_steps"]

    def stalled_tasks(self) -> list:
        now = time.time()
        return [
            {"task_id": tid, "idle_seconds": round(now - t["last_event_at"], 1)}
            for tid, t in self._tasks.items()
            if not t["done"] and (now - t["last_event_at"]) > self._stall_threshold
        ]

    def summary(self) -> dict:
        active = sum(1 for t in self._tasks.values() if not t["done"])
        stalled = self.stalled_tasks()
        return {
            "active_tasks": active,
            "total_registered": len(self._tasks),
            "stalled_tasks": len(stalled),
            "stalled": stalled,
        }
```

## Comparison

| Approach | Step-by-Step Streaming | SSE Formatting | Reconnect Support | Checkpoint | Stall Detection |
|---|---|---|---|---|---|
| StreamingStepExecutor | Yes | No | No | No | No |
| PartialResultAccumulator | No | No | Yes (replay) | No | No |
| SSEStreamHandler | Via executor | Yes | Via accumulator | No | No |
| PartialResultCheckpointPersister | No | No | No | Yes (disk) | No |
| StreamingTaskMonitor | No | No | No | No | Yes |

**Best for production**: Emit a `PROGRESS` event before each step and an `INTERMEDIATE` event after — this gives clients two signals per step (start + completion). Use `PartialResultAccumulator` to buffer the last 100 events per task so clients that reconnect after a brief network interruption receive the results they missed. Pair with `PartialResultCheckpointPersister` for tasks that take more than 60 seconds — disk checkpoints protect against process restarts. Monitor `StreamingTaskMonitor.stalled_tasks()` in your health endpoint: a task that has not emitted an event for 30 seconds is almost certainly deadlocked and should be cancelled and requeued.
