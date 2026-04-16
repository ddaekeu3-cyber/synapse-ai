---
title: "Agent Doesn't Implement Agent State Snapshot for Post-Mortem Debugging"
description: "When agents fail in production, engineers have no capture of the full execution state at the moment of failure — only a stack trace or error log. Implement agent state snapshots that capture the complete context window, tool call history, memory contents, and variable state at the point of failure for offline post-mortem analysis."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-state-snapshot-for-post-mortem-debugging
tags: [post-mortem, debugging, state-snapshot, observability, failure-analysis, incident-response]
symptoms:
  - "Production agent failure produces only 'ValueError: unexpected token' with no context"
  - "Cannot reproduce the bug because the LLM context at failure time is unknown"
  - "Engineers spend hours reconstructing what the agent was doing from sparse logs"
  - "Tool call history at failure time is lost — no way to determine which tools ran"
  - "Memory and session state at crash time is unrecoverable after process restart"
---

## Why This Happens

Agents maintain complex runtime state: a growing context window, tool call history, extracted variables, intermediate results, and in-memory structures. Traditional application logging captures exceptions and log lines but not the full semantic state. When an agent fails after 20 tool calls with a corrupted context, the stack trace alone is useless. A state snapshot system captures everything needed for offline replay and root-cause analysis.

## Solution 1: Core Agent State Snapshot

```python
import json
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class ToolCallRecord:
    tool_name: str
    arguments: dict
    result: Any
    error: Optional[str]
    duration_ms: float
    timestamp: float
    seq: int

@dataclass
class AgentStateSnapshot:
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    user_id: str = ""
    captured_at: float = field(default_factory=time.time)
    trigger: str = ""           # "exception" | "timeout" | "manual" | "periodic"

    # LLM context
    messages: List[dict] = field(default_factory=list)
    system_prompt_hash: str = ""
    total_tokens_so_far: int = 0

    # Tool execution history
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    current_tool: Optional[str] = None

    # Agent memory & state
    working_memory: Dict[str, Any] = field(default_factory=dict)
    extracted_variables: Dict[str, Any] = field(default_factory=dict)
    agent_step: int = 0

    # Failure context (if triggered by exception)
    exception_type: Optional[str] = None
    exception_message: Optional[str] = None
    exception_traceback: Optional[str] = None

    # Environment
    model_id: str = ""
    agent_version: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        def default_serializer(obj):
            try:
                return str(obj)
            except Exception:
                return "<unserializable>"
        return json.dumps(
            {k: v for k, v in self.__dict__.items()},
            default=default_serializer,
            indent=2,
        )

    def summary(self) -> str:
        return (
            f"snapshot_id={self.snapshot_id} trigger={self.trigger} "
            f"session={self.session_id} steps={self.agent_step} "
            f"tools={len(self.tool_calls)} tokens={self.total_tokens_so_far} "
            f"exception={self.exception_type}"
        )
```

## Solution 2: Snapshot Capturer with Exception Hook

```python
import asyncio
import hashlib
import sys
import traceback
from typing import Optional

class AgentSnapshotCapturer:
    """
    Hooks into the agent execution loop to capture snapshots on:
    - Unhandled exceptions
    - Explicit checkpoints
    - Periodic intervals for long-running agents
    """

    def __init__(self, snapshot_store, model_id: str = "", agent_version: str = ""):
        self._store = snapshot_store
        self._model_id = model_id
        self._agent_version = agent_version

    def _build_snapshot(
        self,
        session_id: str,
        messages: list,
        tool_calls: list,
        working_memory: dict,
        agent_step: int,
        trigger: str,
        exc: Optional[Exception] = None,
        current_tool: Optional[str] = None,
        **metadata,
    ) -> AgentStateSnapshot:
        import hashlib
        sys_msg = next((m for m in messages if m.get("role") == "system"), None)
        sys_hash = hashlib.sha256(
            (sys_msg.get("content", "") if sys_msg else "").encode()
        ).hexdigest()[:16]

        snapshot = AgentStateSnapshot(
            session_id=session_id,
            captured_at=__import__("time").time(),
            trigger=trigger,
            messages=messages,
            system_prompt_hash=sys_hash,
            total_tokens_so_far=sum(
                len(m.get("content", "").split()) for m in messages
            ),
            tool_calls=tool_calls,
            current_tool=current_tool,
            working_memory=working_memory,
            agent_step=agent_step,
            model_id=self._model_id,
            agent_version=self._agent_version,
            metadata=metadata,
        )

        if exc is not None:
            snapshot.exception_type = type(exc).__name__
            snapshot.exception_message = str(exc)
            snapshot.exception_traceback = traceback.format_exc()

        return snapshot

    async def capture_on_exception(
        self, session_id: str, messages: list, tool_calls: list,
        working_memory: dict, agent_step: int, exc: Exception,
        current_tool: Optional[str] = None,
    ) -> AgentStateSnapshot:
        snapshot = self._build_snapshot(
            session_id=session_id, messages=messages, tool_calls=tool_calls,
            working_memory=working_memory, agent_step=agent_step,
            trigger="exception", exc=exc, current_tool=current_tool,
        )
        await self._store.save(snapshot)
        print(f"[snapshot] captured on exception: {snapshot.summary()}")
        return snapshot

    async def capture_checkpoint(
        self, session_id: str, messages: list, tool_calls: list,
        working_memory: dict, agent_step: int,
    ) -> AgentStateSnapshot:
        snapshot = self._build_snapshot(
            session_id=session_id, messages=messages, tool_calls=tool_calls,
            working_memory=working_memory, agent_step=agent_step,
            trigger="checkpoint",
        )
        await self._store.save(snapshot)
        return snapshot
```

## Solution 3: Snapshot Store with Retention Policy

```python
import asyncio
import json
import time
from typing import List, Optional

class SnapshotStore:
    """
    Persists snapshots to storage with configurable retention.
    Supports querying by session, exception type, and time range.
    """

    def __init__(self, db, object_storage=None, retention_days: int = 30):
        self._db = db
        self._object_storage = object_storage
        self._retention_days = retention_days

    async def save(self, snapshot: AgentStateSnapshot) -> None:
        # Store metadata in DB for fast queries
        await self._db.execute(
            """
            INSERT INTO agent_snapshots
              (snapshot_id, session_id, user_id, trigger, agent_step,
               exception_type, exception_message, total_tokens, captured_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            snapshot.snapshot_id, snapshot.session_id, snapshot.user_id,
            snapshot.trigger, snapshot.agent_step,
            snapshot.exception_type, snapshot.exception_message,
            snapshot.total_tokens_so_far, snapshot.captured_at,
        )
        # Store full payload in object storage or DB
        if self._object_storage:
            key = f"snapshots/{snapshot.session_id}/{snapshot.snapshot_id}.json"
            await self._object_storage.put(key, snapshot.to_json().encode())
        else:
            await self._db.execute(
                "INSERT INTO snapshot_payloads (snapshot_id, payload) VALUES ($1, $2)",
                snapshot.snapshot_id, snapshot.to_json(),
            )

    async def get(self, snapshot_id: str) -> Optional[AgentStateSnapshot]:
        if self._object_storage:
            rows = await self._db.fetchrow(
                "SELECT session_id FROM agent_snapshots WHERE snapshot_id = $1", snapshot_id
            )
            if not rows:
                return None
            key = f"snapshots/{rows['session_id']}/{snapshot_id}.json"
            data = await self._object_storage.get(key)
            return self._deserialize(data.decode()) if data else None
        else:
            row = await self._db.fetchrow(
                "SELECT payload FROM snapshot_payloads WHERE snapshot_id = $1", snapshot_id
            )
            return self._deserialize(row["payload"]) if row else None

    def _deserialize(self, json_str: str) -> AgentStateSnapshot:
        d = json.loads(json_str)
        tool_calls = [ToolCallRecord(**t) for t in d.pop("tool_calls", [])]
        snapshot = AgentStateSnapshot(**d)
        snapshot.tool_calls = tool_calls
        return snapshot

    async def query_by_exception(
        self, exception_type: str, limit: int = 50
    ) -> List[dict]:
        rows = await self._db.fetch(
            "SELECT snapshot_id, session_id, exception_message, agent_step, captured_at "
            "FROM agent_snapshots WHERE exception_type = $1 ORDER BY captured_at DESC LIMIT $2",
            exception_type, limit,
        )
        return [dict(r) for r in rows]

    async def purge_old(self) -> int:
        cutoff = time.time() - self._retention_days * 86400
        result = await self._db.execute(
            "DELETE FROM agent_snapshots WHERE captured_at < $1", cutoff
        )
        return int(str(result).split()[-1])
```

## Solution 4: Snapshot-Instrumented Agent Loop

```python
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, List

class SnapshotInstrumentedAgent:
    """
    Agent loop that automatically captures snapshots on exceptions
    and at configurable step intervals.
    """

    def __init__(
        self,
        capturer: AgentSnapshotCapturer,
        checkpoint_every_n_steps: int = 10,
    ):
        self._capturer = capturer
        self._checkpoint_every = checkpoint_every_n_steps
        self._messages: List[dict] = []
        self._tool_calls: List[ToolCallRecord] = []
        self._working_memory: dict = {}
        self._step = 0

    def add_message(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})

    async def call_tool(self, tool_name: str, tool_fn, **kwargs) -> object:
        import time
        t0 = time.monotonic()
        error = None
        result = None
        try:
            result = await tool_fn(**kwargs)
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            self._tool_calls.append(ToolCallRecord(
                tool_name=tool_name,
                arguments=kwargs,
                result=result,
                error=error,
                duration_ms=(time.monotonic() - t0) * 1000,
                timestamp=time.time(),
                seq=len(self._tool_calls),
            ))
        return result

    async def step(self, session_id: str) -> None:
        self._step += 1
        if self._step % self._checkpoint_every == 0:
            await self._capturer.capture_checkpoint(
                session_id=session_id,
                messages=list(self._messages),
                tool_calls=list(self._tool_calls),
                working_memory=dict(self._working_memory),
                agent_step=self._step,
            )

    @asynccontextmanager
    async def run_with_snapshot(self, session_id: str) -> AsyncIterator["SnapshotInstrumentedAgent"]:
        try:
            yield self
        except Exception as exc:
            await self._capturer.capture_on_exception(
                session_id=session_id,
                messages=list(self._messages),
                tool_calls=list(self._tool_calls),
                working_memory=dict(self._working_memory),
                agent_step=self._step,
                exc=exc,
            )
            raise
```

## Solution 5: Snapshot Replay Engine for Debugging

```python
from typing import AsyncIterator, List, Optional

class SnapshotReplayEngine:
    """
    Loads a snapshot and replays the agent execution up to any step.
    Allows debuggers to re-run tool calls with patched implementations
    to test fixes against the exact failing context.
    """

    def __init__(self, snapshot: AgentStateSnapshot):
        self._snapshot = snapshot

    def context_at_step(self, step: int) -> List[dict]:
        """Return the message history as it existed at step N."""
        messages = []
        tool_seq = 0
        for msg in self._snapshot.messages:
            messages.append(msg)
            if msg.get("role") == "tool":
                tool_seq += 1
                if tool_seq >= step:
                    break
        return messages

    def tool_calls_up_to(self, step: int) -> List[ToolCallRecord]:
        return self._snapshot.tool_calls[:step]

    async def replay_from_step(
        self,
        step: int,
        patched_tools: Optional[dict] = None,
        llm_client=None,
    ) -> AsyncIterator[str]:
        """
        Re-runs the agent from a given step with optional tool patches.
        Useful for verifying a fix: patch the failing tool and replay.
        """
        messages = self.context_at_step(step)
        if llm_client is None:
            raise ValueError("llm_client required for replay")

        patched = patched_tools or {}
        while True:
            response = await llm_client.complete(messages=messages)
            yield response.content

            if response.finish_reason != "tool_calls":
                break

            for tool_call in response.tool_calls:
                fn = patched.get(tool_call.name)
                if fn:
                    result = await fn(**tool_call.arguments)
                else:
                    result = f"[replayed original result for {tool_call.name}]"
                messages.append({"role": "tool", "content": str(result)})

    def print_summary(self) -> None:
        s = self._snapshot
        print(f"\n=== Agent State Snapshot ===")
        print(f"  ID:          {s.snapshot_id}")
        print(f"  Session:     {s.session_id}")
        print(f"  Captured:    {s.captured_at}")
        print(f"  Trigger:     {s.trigger}")
        print(f"  Steps:       {s.agent_step}")
        print(f"  Messages:    {len(s.messages)}")
        print(f"  Tool calls:  {len(s.tool_calls)}")
        print(f"  Tokens:      {s.total_tokens_so_far}")
        if s.exception_type:
            print(f"  Exception:   {s.exception_type}: {s.exception_message}")
            print(f"  Traceback:\n{s.exception_traceback}")
```

## Solution 6: Snapshot-Based Regression Test Generator

```python
from typing import List

class SnapshotRegressionTestGenerator:
    """
    Converts production failure snapshots into automated regression tests.
    Each snapshot becomes a test case that asserts the fixed behavior.
    """

    def generate_test(self, snapshot: AgentStateSnapshot, expected_output: str) -> str:
        """Returns Python pytest code for a regression test from a snapshot."""
        tool_mocks = "\n".join([
            f"    mock_tools['{tc.tool_name}'] = AsyncMock(return_value={tc.result!r})"
            for tc in snapshot.tool_calls
        ])
        return f"""
# Auto-generated regression test from snapshot {snapshot.snapshot_id}
# Trigger: {snapshot.trigger} at step {snapshot.agent_step}
# Exception: {snapshot.exception_type}: {snapshot.exception_message}

import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_regression_{snapshot.snapshot_id[:8]}():
    \"\"\"Regression test for failure: {snapshot.exception_type} in session {snapshot.session_id}.\"\"\"
    mock_tools = {{}}
{tool_mocks}

    agent = create_agent(tools=mock_tools)
    for msg in {snapshot.messages!r}:
        if msg['role'] == 'user':
            response = await agent.run(msg['content'])

    assert {expected_output!r} in response, (
        f"Regression: expected output not found. Got: {{response!r}}"
    )
"""

    def generate_test_suite(
        self, snapshots: List[AgentStateSnapshot], expected_outputs: List[str]
    ) -> str:
        tests = [self.generate_test(s, o) for s, o in zip(snapshots, expected_outputs)]
        return "\n\n".join(tests)
```

## Comparison

| Approach | Capture Trigger | Storage | Replay Support | Test Generation |
|---|---|---|---|---|
| AgentStateSnapshot | Manual / any | Serializable JSON | Via ReplayEngine | Via TestGenerator |
| AgentSnapshotCapturer | Exception / checkpoint | Via store | No | No |
| SnapshotStore | On save | DB + object storage | Via load + replay | No |
| SnapshotInstrumentedAgent | Auto (exception + interval) | Via capturer | No | No |
| SnapshotReplayEngine | N/A (load from store) | N/A | Yes (full replay) | No |
| SnapshotRegressionTestGenerator | N/A (load from store) | N/A | Indirect | Yes (pytest) |

**Best for production**: Use `SnapshotInstrumentedAgent` to wrap every production agent loop — it auto-captures on any exception and at configurable step intervals. Store snapshots in `SnapshotStore` (metadata in DB, payload in S3). Use `SnapshotReplayEngine` for offline debugging and `SnapshotRegressionTestGenerator` to convert flaky production failures into permanent regression tests.
