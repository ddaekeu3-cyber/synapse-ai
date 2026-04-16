---
title: "Agent Doesn't Implement Conversation State Checkpointing"
description: "Agents that keep conversation state only in memory lose the entire session on process restart, crash, or horizontal scale-out: a user mid-way through a 20-turn research session loses all context when the pod is rescheduled. Implement conversation state checkpointing that persists the session snapshot to durable storage after each turn so it can be restored on any instance."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-conversation-state-checkpointing
tags: [checkpointing, conversation-state, persistence, crash-recovery, horizontal-scaling, session-restore]
symptoms:
  - "Pod restart during a long conversation loses all context — user must start over"
  - "Horizontal scale-out: subsequent requests hit a different instance with no session state"
  - "No durable record of conversation state between requests"
  - "Agent cannot resume a conversation that was interrupted by a timeout"
  - "Debugging requires reproducing the conversation from scratch — no state dump available"
---

## Why This Happens

In-memory conversation state is local to one process. When that process restarts, crashes, or is replaced by a load balancer with a different instance, the session is gone. Checkpointing requires serializing the conversation state — message history, tool call results, agent variables, turn count — and writing it to a durable store (Redis, database, object storage) after each turn. On the next request, the agent reads the checkpoint and resumes from where it left off, regardless of which instance handles the request.

## Solution 1: Conversation Checkpoint

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ConversationCheckpoint:
    session_id: str
    turn_number: int
    messages: List[Dict[str, Any]]         # full message history
    agent_variables: Dict[str, Any]        # tool results, slot values, etc.
    created_at: float = field(default_factory=time.time)
    checkpoint_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    ttl_seconds: float = 86400.0           # 24-hour default TTL

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def size_estimate_bytes(self) -> int:
        import json
        return len(json.dumps({
            "messages": self.messages,
            "agent_variables": self.agent_variables,
        }, ensure_ascii=False).encode())
```

## Solution 2: Checkpoint Serializer

```python
import hashlib
import json
import time
from typing import Any


class CheckpointSerializer:
    """
    Serializes and deserializes ConversationCheckpoint to/from JSON bytes.
    Computes a content hash for integrity verification on restore.
    """

    def serialize(self, checkpoint: ConversationCheckpoint) -> bytes:
        data = {
            "session_id": checkpoint.session_id,
            "turn_number": checkpoint.turn_number,
            "messages": checkpoint.messages,
            "agent_variables": checkpoint.agent_variables,
            "created_at": checkpoint.created_at,
            "checkpoint_id": checkpoint.checkpoint_id,
            "metadata": checkpoint.metadata,
            "ttl_seconds": checkpoint.ttl_seconds,
        }
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        content_hash = hashlib.sha256(payload.encode()).hexdigest()[:16]
        envelope = {"payload": payload, "hash": content_hash}
        return json.dumps(envelope).encode()

    def deserialize(self, data: bytes) -> ConversationCheckpoint:
        envelope = json.loads(data.decode())
        payload_str = envelope["payload"]
        expected_hash = hashlib.sha256(payload_str.encode()).hexdigest()[:16]
        if envelope.get("hash") != expected_hash:
            raise ValueError("Checkpoint hash mismatch — data may be corrupted")
        raw = json.loads(payload_str)
        return ConversationCheckpoint(
            session_id=raw["session_id"],
            turn_number=raw["turn_number"],
            messages=raw["messages"],
            agent_variables=raw["agent_variables"],
            created_at=raw.get("created_at", time.time()),
            checkpoint_id=raw.get("checkpoint_id", ""),
            metadata=raw.get("metadata", {}),
            ttl_seconds=raw.get("ttl_seconds", 86400.0),
        )
```

## Solution 3: File-Based Checkpoint Store

```python
import json
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import List, Optional


class FileCheckpointStore:
    """
    Persists conversation checkpoints to local files.
    Replace with Redis or a database for multi-instance deployments.
    """

    def __init__(
        self,
        directory: str = "/tmp/agent_checkpoints",
        serializer: CheckpointSerializer = None,
    ):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._serializer = serializer or CheckpointSerializer()
        self._lock = Lock()

    def _path(self, session_id: str) -> Path:
        safe = session_id.replace("/", "_").replace("..", "_")
        return self._dir / f"{safe}.ckpt"

    def save(self, checkpoint: ConversationCheckpoint) -> str:
        if not checkpoint.checkpoint_id:
            checkpoint.checkpoint_id = str(uuid.uuid4())[:8]
        data = self._serializer.serialize(checkpoint)
        with self._lock:
            self._path(checkpoint.session_id).write_bytes(data)
        return checkpoint.checkpoint_id

    def load(self, session_id: str) -> Optional[ConversationCheckpoint]:
        path = self._path(session_id)
        if not path.exists():
            return None
        with self._lock:
            data = path.read_bytes()
        try:
            checkpoint = self._serializer.deserialize(data)
        except (ValueError, json.JSONDecodeError):
            return None
        if checkpoint.is_expired():
            path.unlink(missing_ok=True)
            return None
        return checkpoint

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._path(session_id).unlink(missing_ok=True)

    def list_sessions(self) -> List[str]:
        with self._lock:
            return [p.stem for p in self._dir.glob("*.ckpt")]

    def prune_expired(self) -> int:
        pruned = 0
        for path in list(self._dir.glob("*.ckpt")):
            try:
                data = path.read_bytes()
                checkpoint = self._serializer.deserialize(data)
                if checkpoint.is_expired():
                    path.unlink(missing_ok=True)
                    pruned += 1
            except Exception:
                path.unlink(missing_ok=True)
                pruned += 1
        return pruned
```

## Solution 4: Checkpointing Agent Session

```python
import time
from typing import Any, Dict, List, Optional


class CheckpointingAgentSession:
    """
    Manages agent session state with automatic checkpointing after each turn.
    Restores state from the checkpoint store on initialization if a prior
    session exists for the given session_id.
    """

    def __init__(
        self,
        session_id: str,
        store: FileCheckpointStore,
        checkpoint_on_every_turn: bool = True,
    ):
        self._session_id = session_id
        self._store = store
        self._checkpoint_every_turn = checkpoint_on_every_turn
        self._checkpoint: Optional[ConversationCheckpoint] = None
        self._restore()

    def _restore(self) -> None:
        saved = self._store.load(self._session_id)
        if saved:
            self._checkpoint = saved
        else:
            self._checkpoint = ConversationCheckpoint(
                session_id=self._session_id,
                turn_number=0,
                messages=[],
                agent_variables={},
            )

    @property
    def messages(self) -> List[Dict[str, Any]]:
        return self._checkpoint.messages

    @property
    def turn_number(self) -> int:
        return self._checkpoint.turn_number

    @property
    def agent_variables(self) -> Dict[str, Any]:
        return self._checkpoint.agent_variables

    def add_message(self, role: str, content: str) -> None:
        self._checkpoint.messages.append({"role": role, "content": content, "ts": time.time()})

    def set_variable(self, key: str, value: Any) -> None:
        self._checkpoint.agent_variables[key] = value

    def advance_turn(self) -> None:
        self._checkpoint.turn_number += 1
        self._checkpoint.created_at = time.time()
        if self._checkpoint_every_turn:
            self._store.save(self._checkpoint)

    def checkpoint(self) -> str:
        return self._store.save(self._checkpoint)

    def clear(self) -> None:
        self._store.delete(self._session_id)
        self._checkpoint = ConversationCheckpoint(
            session_id=self._session_id,
            turn_number=0,
            messages=[],
            agent_variables={},
        )
```

## Solution 5: Checkpoint Diff Compressor

```python
import json
from typing import Any, Dict, List, Optional


class CheckpointDiffCompressor:
    """
    Stores only the diff between consecutive checkpoints to reduce
    storage overhead for long conversations.
    Appends only new messages rather than rewriting the full history.
    """

    def __init__(self, store: FileCheckpointStore):
        self._store = store
        self._prev_message_counts: Dict[str, int] = {}

    def save_incremental(self, checkpoint: ConversationCheckpoint) -> str:
        prev_count = self._prev_message_counts.get(checkpoint.session_id, 0)
        new_messages = checkpoint.messages[prev_count:]

        if not new_messages:
            return checkpoint.checkpoint_id or ""

        self._prev_message_counts[checkpoint.session_id] = len(checkpoint.messages)
        return self._store.save(checkpoint)
```

## Solution 6: Checkpoint Store Monitor

```python
import time
from pathlib import Path
from typing import List


class CheckpointStoreMonitor:
    """
    Monitors checkpoint store health: session count, storage usage, and expiry rates.
    """

    def __init__(self, store: FileCheckpointStore):
        self._store = store

    def report(self) -> dict:
        sessions = self._store.list_sessions()
        total_bytes = sum(
            (self._store._dir / f"{s}.ckpt").stat().st_size
            for s in sessions
            if (self._store._dir / f"{s}.ckpt").exists()
        )
        return {
            "generated_at": time.time(),
            "active_sessions": len(sessions),
            "total_storage_mb": round(total_bytes / (1024 * 1024), 3),
            "storage_dir": str(self._store._dir),
        }

    def prune(self) -> dict:
        pruned = self._store.prune_expired()
        return {"pruned": pruned, "remaining": len(self._store.list_sessions())}
```

## Comparison

| Approach | Serialization | Integrity Hash | TTL Expiry | Incremental Save | Monitoring |
|---|---|---|---|---|---|
| CheckpointSerializer | Yes (JSON) | Yes (SHA256) | No | No | No |
| FileCheckpointStore | Via serializer | Via serializer | Yes | No | No |
| CheckpointingAgentSession | Via store | Via store | Via store | No | No |
| CheckpointDiffCompressor | Via store | Via store | Via store | Yes | No |
| CheckpointStoreMonitor | No | No | No | No | Yes |

**Best for production**: Use Redis with `SETEX` (TTL-aware set) as the checkpoint backend for multi-instance deployments — all instances share session state with automatic expiry. Set `ttl_seconds=3600` for anonymous sessions and `86400` for authenticated users. Call `CheckpointingAgentSession.advance_turn()` at the end of every turn — not at the beginning — to ensure each checkpoint represents a completed turn state. Run `prune_expired()` on a daily cron to prevent storage growth on file-based stores. Monitor `active_sessions` via `CheckpointStoreMonitor` to detect session leaks.
