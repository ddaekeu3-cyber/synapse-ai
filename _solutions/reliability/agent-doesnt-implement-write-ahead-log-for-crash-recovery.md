---
title: "Agent Doesn't Implement Write-Ahead Log for Crash Recovery"
description: "How to use write-ahead logging (WAL) to ensure agent operations survive process crashes, enabling full recovery of in-flight tasks without data loss or duplicate execution."
date: 2025-01-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-write-ahead-log-for-crash-recovery
tags:
  - reliability
  - crash-recovery
  - write-ahead-log
  - durability
  - redo-log
  - idempotency
  - persistence
symptoms:
  - "Agent crashes mid-task and all progress is lost on restart"
  - "No way to tell which tool calls completed before a crash"
  - "Restarted agent replays already-completed steps causing duplicates"
  - "Long-running workflows must restart from scratch after any failure"
  - "No audit trail of what operations were attempted before the crash"
  - "State inconsistency after unexpected process termination"
---

## Why This Happens

Agents that hold all state in memory lose everything when their process dies. Even agents that persist state to a database can leave it in an inconsistent state if they write results *before* marking the operation as committed — or *after*, leaving a committed intent with no result. The write-ahead log (WAL) pattern, borrowed from database systems, solves this by writing a durable log entry *before* performing any state mutation. On recovery, the agent reads the log and either replays incomplete operations or skips already-completed ones.

Without WAL, crashes during tool execution cause either lost work (operation silently dropped) or duplicate work (operation replayed without knowing it already completed). Both outcomes corrupt agent state in production environments where long-running workflows span minutes or hours.

---

## Solution 1: Append-Only WAL with JSON Records

The simplest WAL appends newline-delimited JSON records to a log file. Each operation is logged as `INTENT` before execution and `COMMIT` after. On startup, the agent scans for uncommitted intents and decides whether to replay or skip them.

```python
import json
import os
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Optional

class WALRecordType(str, Enum):
    INTENT  = "INTENT"   # About to perform operation
    COMMIT  = "COMMIT"   # Operation completed successfully
    ABORT   = "ABORT"    # Operation explicitly failed/rolled back

class WALRecord:
    def __init__(
        self,
        record_type: WALRecordType,
        operation_id: str,
        operation: str,
        payload: Any = None,
        result: Any = None,
        timestamp: Optional[float] = None,
    ):
        self.record_type = record_type
        self.operation_id = operation_id
        self.operation = operation
        self.payload = payload
        self.result = result
        self.timestamp = timestamp or time.time()

    def to_json(self) -> str:
        return json.dumps({
            "type": self.record_type.value,
            "op_id": self.operation_id,
            "operation": self.operation,
            "payload": self.payload,
            "result": self.result,
            "ts": self.timestamp,
        })

    @classmethod
    def from_json(cls, line: str) -> "WALRecord":
        d = json.loads(line)
        return cls(
            record_type=WALRecordType(d["type"]),
            operation_id=d["op_id"],
            operation=d["operation"],
            payload=d.get("payload"),
            result=d.get("result"),
            timestamp=d["ts"],
        )


class WriteAheadLog:
    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.log_path, "a", buffering=1)  # line-buffered

    def write_intent(self, operation: str, payload: Any, operation_id: Optional[str] = None) -> str:
        op_id = operation_id or str(uuid.uuid4())
        record = WALRecord(WALRecordType.INTENT, op_id, operation, payload=payload)
        self._file.write(record.to_json() + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())  # durability guarantee
        return op_id

    def write_commit(self, operation_id: str, result: Any = None) -> None:
        record = WALRecord(WALRecordType.COMMIT, operation_id, "", result=result)
        self._file.write(record.to_json() + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def write_abort(self, operation_id: str, reason: str = "") -> None:
        record = WALRecord(WALRecordType.ABORT, operation_id, "", payload={"reason": reason})
        self._file.write(record.to_json() + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def read_all(self) -> list[WALRecord]:
        records = []
        if not self.log_path.exists():
            return records
        with open(self.log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(WALRecord.from_json(line))
                    except (json.JSONDecodeError, KeyError):
                        continue  # corrupted record — skip
        return records

    def get_uncommitted_intents(self) -> list[WALRecord]:
        """Return INTENT records that have no corresponding COMMIT or ABORT."""
        records = self.read_all()
        committed = {r.operation_id for r in records if r.record_type in (WALRecordType.COMMIT, WALRecordType.ABORT)}
        return [r for r in records if r.record_type == WALRecordType.INTENT and r.operation_id not in committed]

    def truncate_committed(self, keep_last: int = 100) -> None:
        """Compact the WAL by removing fully committed entries, keeping recent history."""
        records = self.read_all()
        committed_ids = {r.operation_id for r in records if r.record_type in (WALRecordType.COMMIT, WALRecordType.ABORT)}
        intent_ids_to_keep = {r.operation_id for r in records if r.record_type == WALRecordType.INTENT and r.operation_id not in committed_ids}

        # Keep uncommitted intents + last N committed pairs
        committed_pairs = [(r.operation_id, r) for r in records if r.operation_id in committed_ids]
        recent_committed_ids = {op_id for op_id, _ in committed_pairs[-keep_last:]}
        keep_ids = intent_ids_to_keep | recent_committed_ids

        surviving = [r for r in records if r.operation_id in keep_ids or r.record_type in (WALRecordType.COMMIT, WALRecordType.ABORT)]
        with open(self.log_path, "w") as f:
            for r in surviving:
                f.write(r.to_json() + "\n")
```

---

## Solution 2: WAL-Backed Agent with Automatic Recovery

Wrap agent tool execution behind WAL write gates so every operation is recoverable.

```python
import asyncio
import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

class WALBackedAgent:
    """
    Agent that logs every tool call to WAL before and after execution.
    On startup, replays any uncommitted operations from previous sessions.
    """

    def __init__(self, wal: WriteAheadLog, idempotency_store: dict[str, Any] | None = None):
        self.wal = wal
        self._results: dict[str, Any] = idempotency_store or {}

    async def recover(self) -> int:
        """
        Called on startup. Replays uncommitted WAL entries.
        Returns the number of operations replayed.
        """
        pending = self.wal.get_uncommitted_intents()
        if not pending:
            return 0

        logger.warning("WAL recovery: found %d uncommitted operations", len(pending))
        replayed = 0
        for record in pending:
            logger.info("Replaying operation %s: %s", record.operation_id, record.operation)
            try:
                handler = self._get_handler(record.operation)
                if handler:
                    result = await handler(record.payload, record.operation_id)
                    self.wal.write_commit(record.operation_id, result)
                    replayed += 1
                else:
                    logger.error("No handler for operation '%s', aborting", record.operation)
                    self.wal.write_abort(record.operation_id, "no handler")
            except Exception as exc:
                logger.error("Recovery failed for %s: %s", record.operation_id, exc)
                self.wal.write_abort(record.operation_id, str(exc))

        return replayed

    def _get_handler(self, operation: str) -> Callable | None:
        # Registry of recoverable operations
        return getattr(self, f"_execute_{operation}", None)

    async def execute(
        self,
        operation: str,
        payload: Any,
        operation_id: str | None = None,
    ) -> Any:
        """
        Execute an operation with WAL protection.
        If operation_id already has a result (idempotency), returns cached result.
        """
        # Check idempotency cache first
        if operation_id and operation_id in self._results:
            logger.debug("Idempotent skip: %s already committed", operation_id)
            return self._results[operation_id]

        # Write intent
        op_id = self.wal.write_intent(operation, payload, operation_id=operation_id)

        # Execute
        try:
            handler = self._get_handler(operation)
            if handler is None:
                raise ValueError(f"Unknown operation: {operation}")
            result = await handler(payload, op_id)
        except Exception as exc:
            self.wal.write_abort(op_id, str(exc))
            raise

        # Write commit
        self.wal.write_commit(op_id, result)
        self._results[op_id] = result
        return result

    # --- Example operation handlers ---

    async def _execute_call_tool(self, payload: dict, op_id: str) -> Any:
        tool_name = payload["tool"]
        args = payload.get("args", {})
        logger.info("[%s] Calling tool: %s(%s)", op_id, tool_name, args)
        await asyncio.sleep(0.1)  # simulate tool execution
        return {"tool": tool_name, "result": "ok", "ts": time.time()}

    async def _execute_save_memory(self, payload: dict, op_id: str) -> Any:
        key = payload["key"]
        value = payload["value"]
        logger.info("[%s] Saving memory: %s", op_id, key)
        return {"saved": key}


# --- Bootstrap with recovery ---

async def start_agent_with_recovery():
    wal = WriteAheadLog("/tmp/agent.wal")
    agent = WALBackedAgent(wal)

    recovered = await agent.recover()
    if recovered:
        logger.info("Recovered %d operations from WAL", recovered)

    # Normal operation
    await agent.execute("call_tool", {"tool": "search", "args": {"q": "AI agents"}})
    await agent.execute("save_memory", {"key": "search_result", "value": "found 42 results"})
```

---

## Solution 3: Structured WAL with Sequence Numbers and Checksums

Production WALs add sequence numbers for ordering and checksums for corruption detection.

```python
import hashlib
import struct
from dataclasses import dataclass
from typing import Iterator

@dataclass
class WALEntry:
    sequence: int
    record_type: str
    operation_id: str
    operation: str
    payload_bytes: bytes
    checksum: int

    def verify(self) -> bool:
        data = f"{self.sequence}:{self.record_type}:{self.operation_id}:{self.operation}:".encode() + self.payload_bytes
        expected = int(hashlib.md5(data).hexdigest()[:8], 16)
        return self.checksum == expected

    @classmethod
    def create(cls, sequence: int, record_type: str, operation_id: str, operation: str, payload: bytes) -> "WALEntry":
        data = f"{sequence}:{record_type}:{operation_id}:{operation}:".encode() + payload
        checksum = int(hashlib.md5(data).hexdigest()[:8], 16)
        return cls(sequence, record_type, operation_id, operation, payload, checksum)


class BinaryWAL:
    """
    Binary WAL with fixed-size header per record for efficient sequential reads.
    Record layout: [seq:8][type:1][op_id:36][op_len:2][op:var][payload_len:4][payload:var][checksum:4]
    """

    HEADER_FMT = ">Q B 36s H"  # seq(8) + type(1) + op_id(36) + op_len(2)
    HEADER_SIZE = struct.calcsize(HEADER_FMT)

    def __init__(self, path: str):
        self.path = path
        self._seq = self._read_max_sequence() + 1
        self._fd = open(path, "ab")

    def _read_max_sequence(self) -> int:
        max_seq = 0
        for entry in self.scan():
            if entry.sequence > max_seq:
                max_seq = entry.sequence
        return max_seq

    def append(self, record_type: str, operation_id: str, operation: str, payload: bytes) -> int:
        seq = self._seq
        self._seq += 1
        entry = WALEntry.create(seq, record_type, operation_id, operation, payload)

        op_bytes = operation.encode("utf-8")
        header = struct.pack(
            self.HEADER_FMT,
            seq,
            ord(record_type[0]),
            operation_id.encode("utf-8").ljust(36)[:36],
            len(op_bytes),
        )
        checksum_bytes = struct.pack(">I", entry.checksum)
        payload_len = struct.pack(">I", len(payload))

        record_bytes = header + op_bytes + payload_len + payload + checksum_bytes
        self._fd.write(record_bytes)
        self._fd.flush()
        os.fsync(self._fd.fileno())
        return seq

    def scan(self) -> Iterator[WALEntry]:
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            while True:
                header_data = f.read(self.HEADER_SIZE)
                if not header_data or len(header_data) < self.HEADER_SIZE:
                    break
                seq, type_byte, op_id_bytes, op_len = struct.unpack(self.HEADER_FMT, header_data)
                op_bytes = f.read(op_len)
                payload_len_bytes = f.read(4)
                if len(payload_len_bytes) < 4:
                    break
                payload_len = struct.unpack(">I", payload_len_bytes)[0]
                payload = f.read(payload_len)
                checksum_bytes = f.read(4)
                if len(checksum_bytes) < 4:
                    break
                checksum = struct.unpack(">I", checksum_bytes)[0]
                entry = WALEntry(
                    sequence=seq,
                    record_type=chr(type_byte),
                    operation_id=op_id_bytes.rstrip(b"\x00").decode("utf-8"),
                    operation=op_bytes.decode("utf-8"),
                    payload_bytes=payload,
                    checksum=checksum,
                )
                if entry.verify():
                    yield entry
                else:
                    logger.warning("Corrupted WAL entry at sequence %d — skipping", seq)
```

---

## Solution 4: WAL with Segment Rotation

Large WALs are split into fixed-size segments. Fully committed segments are archived or deleted, keeping the active segment small.

```python
import glob
from pathlib import Path

class SegmentedWAL:
    """WAL that rotates into numbered segment files once a size threshold is reached."""

    SEGMENT_SIZE_LIMIT = 10 * 1024 * 1024  # 10 MB

    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._current_segment = self._find_or_create_active_segment()
        self._wal = WriteAheadLog(str(self._current_segment))

    def _find_or_create_active_segment(self) -> Path:
        segments = sorted(self.dir.glob("wal-*.log"))
        if segments:
            return segments[-1]
        return self.dir / "wal-000001.log"

    def _maybe_rotate(self) -> None:
        size = self._current_segment.stat().st_size if self._current_segment.exists() else 0
        if size >= self.SEGMENT_SIZE_LIMIT:
            self._wal.close()
            # Next segment number
            num = int(self._current_segment.stem.split("-")[1]) + 1
            self._current_segment = self.dir / f"wal-{num:06d}.log"
            self._wal = WriteAheadLog(str(self._current_segment))

    def write_intent(self, operation: str, payload: Any, operation_id: str | None = None) -> str:
        self._maybe_rotate()
        return self._wal.write_intent(operation, payload, operation_id)

    def write_commit(self, operation_id: str, result: Any = None) -> None:
        self._wal.write_commit(operation_id, result)

    def write_abort(self, operation_id: str, reason: str = "") -> None:
        self._wal.write_abort(operation_id, reason)

    def archive_committed_segments(self) -> list[str]:
        """Archive segments where every INTENT has a COMMIT or ABORT."""
        archived = []
        segments = sorted(self.dir.glob("wal-*.log"))
        active = self._current_segment

        for seg in segments:
            if seg == active:
                continue
            wal = WriteAheadLog(str(seg))
            uncommitted = wal.get_uncommitted_intents()
            if not uncommitted:
                archive_path = seg.with_suffix(".log.archived")
                seg.rename(archive_path)
                archived.append(str(archive_path))
        return archived

    def get_all_uncommitted_intents(self) -> list[WALRecord]:
        """Scan all segments for uncommitted intents (for full recovery)."""
        all_pending = []
        for seg in sorted(self.dir.glob("wal-*.log")):
            wal = WriteAheadLog(str(seg))
            all_pending.extend(wal.get_uncommitted_intents())
        return all_pending
```

---

## Solution 5: Distributed WAL with Replication

For multi-process agents, WAL entries are replicated to a secondary store to survive not just process crashes but host failures.

```python
import asyncio
import aiohttp
from typing import Optional

class ReplicatedWAL:
    """
    WAL that writes locally and replicates to a remote WAL service.
    Quorum write: both local + remote must acknowledge for durability.
    """

    def __init__(self, local_wal: WriteAheadLog, replica_url: str, quorum: int = 2):
        self.local = local_wal
        self.replica_url = replica_url
        self.quorum = quorum
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def write_intent_replicated(
        self,
        operation: str,
        payload: Any,
        operation_id: Optional[str] = None,
        timeout: float = 2.0,
    ) -> str:
        op_id = str(uuid.uuid4()) if operation_id is None else operation_id

        # Write to both local and replica concurrently
        local_task = asyncio.create_task(
            asyncio.to_thread(self.local.write_intent, operation, payload, op_id)
        )
        remote_task = asyncio.create_task(
            self._remote_write_intent(op_id, operation, payload, timeout)
        )

        done, pending = await asyncio.wait(
            [local_task, remote_task],
            return_when=asyncio.ALL_COMPLETED,
        )

        successes = sum(1 for t in done if not t.exception())
        if successes < self.quorum:
            failures = [str(t.exception()) for t in done if t.exception()]
            raise RuntimeError(f"WAL quorum write failed ({successes}/{self.quorum}): {failures}")

        return op_id

    async def _remote_write_intent(self, op_id: str, operation: str, payload: Any, timeout: float) -> None:
        session = await self._ensure_session()
        async with session.post(
            f"{self.replica_url}/wal/intent",
            json={"op_id": op_id, "operation": operation, "payload": payload},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()

    async def write_commit_replicated(self, operation_id: str, result: Any = None) -> None:
        local_task = asyncio.create_task(
            asyncio.to_thread(self.local.write_commit, operation_id, result)
        )
        remote_task = asyncio.create_task(
            self._remote_write_commit(operation_id, result)
        )
        await asyncio.gather(local_task, remote_task, return_exceptions=True)

    async def _remote_write_commit(self, op_id: str, result: Any) -> None:
        session = await self._ensure_session()
        async with session.post(
            f"{self.replica_url}/wal/commit",
            json={"op_id": op_id, "result": result},
            timeout=aiohttp.ClientTimeout(total=2.0),
        ) as resp:
            resp.raise_for_status()
```

---

## Solution 6: WAL-Integrated Workflow Engine

A complete workflow engine that uses WAL to checkpoint multi-step workflows and resume from the last successful step.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class WorkflowStep:
    name: str
    handler: Callable[[Any], Awaitable[Any]]
    idempotent: bool = True

@dataclass
class WorkflowState:
    workflow_id: str
    completed_steps: list[str] = field(default_factory=list)
    step_results: dict[str, Any] = field(default_factory=dict)
    failed: bool = False
    error: str = ""

class WALWorkflowEngine:
    """
    Multi-step workflow engine backed by WAL.
    On restart, resumes from the last committed step.
    """

    def __init__(self, wal: WriteAheadLog):
        self.wal = wal
        self._workflows: dict[str, WorkflowState] = {}

    def _load_workflow_state(self, workflow_id: str) -> WorkflowState:
        """Reconstruct workflow state from WAL records."""
        all_records = self.wal.read_all()
        state = WorkflowState(workflow_id=workflow_id)

        committed_ids = {r.operation_id for r in all_records if r.record_type == WALRecordType.COMMIT}

        for r in all_records:
            if (
                r.record_type == WALRecordType.INTENT
                and r.operation_id in committed_ids
                and isinstance(r.payload, dict)
                and r.payload.get("workflow_id") == workflow_id
            ):
                step_name = r.payload.get("step_name", "")
                # Find commit result
                for c in all_records:
                    if c.operation_id == r.operation_id and c.record_type == WALRecordType.COMMIT:
                        state.completed_steps.append(step_name)
                        state.step_results[step_name] = c.result
                        break

        return state

    async def run_workflow(
        self,
        workflow_id: str,
        steps: list[WorkflowStep],
        initial_input: Any = None,
    ) -> WorkflowState:
        state = self._load_workflow_state(workflow_id)
        current_input = initial_input

        # Pass results forward through pipeline
        for i, step in enumerate(steps):
            if step.name in state.completed_steps:
                current_input = state.step_results[step.name]
                logger.info("[%s] Skipping completed step: %s", workflow_id, step.name)
                continue

            op_id = f"{workflow_id}:{step.name}"
            self.wal.write_intent(
                "workflow_step",
                {"workflow_id": workflow_id, "step_name": step.name, "step_index": i},
                operation_id=op_id,
            )

            try:
                result = await step.handler(current_input)
                self.wal.write_commit(op_id, result)
                state.completed_steps.append(step.name)
                state.step_results[step.name] = result
                current_input = result
                logger.info("[%s] Completed step: %s", workflow_id, step.name)
            except Exception as exc:
                self.wal.write_abort(op_id, str(exc))
                state.failed = True
                state.error = f"Step '{step.name}' failed: {exc}"
                logger.error("[%s] Step failed: %s — %s", workflow_id, step.name, exc)
                break

        return state


# --- Usage: agent workflow that survives crashes ---

async def demo_workflow():
    wal = WriteAheadLog("/tmp/agent-workflow.wal")
    engine = WALWorkflowEngine(wal)

    steps = [
        WorkflowStep("fetch_data", lambda _: asyncio.sleep(0.1) or {"rows": 100}),
        WorkflowStep("process",    lambda d: asyncio.sleep(0.1) or {**d, "processed": True}),
        WorkflowStep("store",      lambda d: asyncio.sleep(0.1) or {"stored": True}),
    ]

    # First run (or crash recovery — skips completed steps automatically)
    state = await engine.run_workflow("wf-001", steps)
    print(f"Completed: {state.completed_steps}")
```

---

## Comparison

| Solution | Durability | Recovery Granularity | Replication | Best For |
|---|---|---|---|---|
| Append-Only JSON WAL | fsync per record | Per operation | None | Simple agents, single process |
| WAL-Backed Agent | fsync per record | Per tool call | None | General tool-calling agents |
| Binary WAL with Checksums | fsync + checksum | Per record | None | High-throughput agents |
| Segmented WAL | Segment rotation | Per operation | None | Long-running agents with compaction |
| Replicated WAL | Quorum write | Per operation | Remote replica | Multi-process / distributed |
| WAL Workflow Engine | fsync per step | Per workflow step | None | Multi-step workflows with resume |

**Choose append-only JSON WAL** for simplicity when single-process durability suffices. **Choose binary WAL** when write throughput matters and you need corruption detection. **Choose segmented WAL** for long-running agents where log compaction prevents unbounded disk growth. **Choose replicated WAL** for distributed deployments where host failure is a real concern. **Use the workflow engine** to get automatic step-resume semantics without managing WAL records directly.
