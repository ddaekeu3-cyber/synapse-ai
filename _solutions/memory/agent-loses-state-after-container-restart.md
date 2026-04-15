---
layout: solution
title: "Agent Loses All State After Container Restart — Ephemeral Memory Problem"
category: memory
description: "Agent stores session state, user preferences, and task progress in memory (dict, list, class variables). Container restarts and all state is gone. Users have to repeat context. In-progress tasks are lost with no recovery path."
tags: [memory, persistence, container, restart, state, ephemeral, redis, sqlite]
---

# Agent Loses All State After Container Restart — Ephemeral Memory Problem

## Symptom
- Container restarts (OOM kill, deploy, crash) and all in-progress tasks are lost
- User resumes conversation but agent has no memory of previous turns
- Agent loses user preferences on every restart — always starts from defaults
- Task at step 7 of 10 — container restarts — must restart from step 1
- `dict`, `list`, and instance variable state is always empty on startup
- Long-running agent job loses hours of progress to a single OOM kill

## Root Cause
Python in-memory data structures (dicts, lists, class attributes) exist only in the process. When the process dies — container restart, OOM kill, deploy, timeout — all in-memory state is lost. Containers are designed to be ephemeral. Any state that matters must be written to persistent storage before the process exits.

## Fix

### Option 1: Redis for session state (fast, shared across replicas)

```python
import redis
import json
import os
from typing import Any

class RedisStateStore:
    """
    Persist agent state to Redis.
    Survives container restarts, works across multiple replicas.
    """

    def __init__(self, session_id: str, ttl_seconds: int = 86400):
        self.r = redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            decode_responses=True
        )
        self.key = f"agent:session:{session_id}"
        self.ttl = ttl_seconds

    def get(self, field: str, default: Any = None) -> Any:
        """Get a single state field"""
        value = self.r.hget(self.key, field)
        if value is None:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    def set(self, field: str, value: Any):
        """Set a single state field"""
        self.r.hset(self.key, field, json.dumps(value))
        self.r.expire(self.key, self.ttl)

    def get_all(self) -> dict:
        """Get entire session state"""
        raw = self.r.hgetall(self.key)
        return {k: json.loads(v) for k, v in raw.items()}

    def update(self, updates: dict):
        """Update multiple fields atomically"""
        pipe = self.r.pipeline()
        for k, v in updates.items():
            pipe.hset(self.key, k, json.dumps(v))
        pipe.expire(self.key, self.ttl)
        pipe.execute()

    def delete(self):
        self.r.delete(self.key)

# Usage:
session_id = "user_123_session_456"
state = RedisStateStore(session_id)

# Save progress that survives restarts:
state.set("current_step", 7)
state.set("task_goal", "Analyze Q4 sales data")
state.set("completed_steps", ["fetch_data", "clean_data", "aggregate"])

# On restart — state is still there:
step = state.get("current_step", default=1)
print(f"Resuming from step {step}")  # → 7, not 1
```

### Option 2: SQLite for local persistent state

```python
import sqlite3
import json
from pathlib import Path
from datetime import datetime

class SQLiteStateStore:
    """
    SQLite-based state persistence — works without Redis.
    Mount the SQLite file on a persistent volume in Docker.
    """

    def __init__(self, db_path: str = "/data/agent_state.db"):
        # Ensure parent directory exists (mounted volume)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent access
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS session_state (
                session_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (session_id, key)
            )
        """)
        self.conn.commit()

    def set(self, session_id: str, key: str, value):
        self.conn.execute("""
            INSERT OR REPLACE INTO session_state (session_id, key, value, updated_at)
            VALUES (?, ?, ?, ?)
        """, (session_id, key, json.dumps(value), datetime.utcnow().isoformat()))
        self.conn.commit()

    def get(self, session_id: str, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM session_state WHERE session_id=? AND key=?",
            (session_id, key)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def get_all(self, session_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT key, value FROM session_state WHERE session_id=?",
            (session_id,)
        ).fetchall()
        return {k: json.loads(v) for k, v in rows}

    def delete_session(self, session_id: str):
        self.conn.execute("DELETE FROM session_state WHERE session_id=?", (session_id,))
        self.conn.commit()

# Docker volume mount ensures SQLite file persists across container restarts:
# docker run -v /host/data:/data my-agent
store = SQLiteStateStore("/data/agent_state.db")
```

### Option 3: Checkpoint pattern — save state before any risky operation

```python
import json
from pathlib import Path
from datetime import datetime

class TaskCheckpointer:
    """
    Save task progress at each step.
    On restart, load checkpoint and resume from last known state.
    """

    def __init__(self, task_id: str, checkpoint_dir: str = "/data/checkpoints"):
        self.task_id = task_id
        self.dir = Path(checkpoint_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{task_id}.json"
        self._state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            state = json.loads(self.path.read_text())
            print(
                f"Checkpoint loaded for {self.task_id}: "
                f"step {state.get('current_step')}/{state.get('total_steps')}"
            )
            return state
        return {}

    def save(self, **kwargs):
        """Save current state — call after each significant step"""
        self._state.update(kwargs)
        self._state["saved_at"] = datetime.utcnow().isoformat()
        # Atomic write — write to temp then rename
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(self.path)

    def get(self, key: str, default=None):
        return self._state.get(key, default)

    def is_step_done(self, step_name: str) -> bool:
        return step_name in self._state.get("completed_steps", [])

    def mark_step_done(self, step_name: str):
        done = self._state.get("completed_steps", [])
        if step_name not in done:
            done.append(step_name)
        self.save(completed_steps=done)

    def clear(self):
        self.path.unlink(missing_ok=True)
        self._state = {}

cp = TaskCheckpointer("analysis_task_abc123")

async def run_analysis(data_sources: list):
    total = len(data_sources)
    start_idx = cp.get("current_index", 0)  # Resume from checkpoint

    if start_idx > 0:
        print(f"Resuming from index {start_idx}/{total}")

    for i in range(start_idx, total):
        source = data_sources[i]
        await process_source(source)
        # Save after each item — restart resumes from next item
        cp.save(current_index=i + 1, last_processed=source["id"])

    cp.clear()  # Clean up on success
    print("Analysis complete")
```

### Option 4: Environment variable for stateless configuration

```python
import os
from pydantic import BaseSettings

class AgentConfig(BaseSettings):
    """
    Configuration that survives restarts via environment variables.
    State that should persist: use persistent storage (Redis/SQLite).
    Configuration that should persist: use environment variables.
    """

    # These survive restarts via env vars:
    model_name: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    agent_persona: str = "helpful assistant"
    output_dir: str = "/data/output"

    # These come from secrets manager:
    api_key: str
    database_url: str

    class Config:
        env_file = ".env"
        env_prefix = "AGENT_"

# Docker-compose passes env vars that survive restarts:
# services:
#   agent:
#     environment:
#       - AGENT_MODEL_NAME=claude-sonnet-4-6
#       - AGENT_OUTPUT_DIR=/data/output

config = AgentConfig()
```

### Option 5: Graceful shutdown — flush state before exit

```python
import signal
import asyncio
import atexit

class GracefulAgent:
    """
    Agent that saves state on shutdown signals before container stops.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.state = {}
        self._running = True

        # Register shutdown handlers
        signal.signal(signal.SIGTERM, self._on_shutdown)
        signal.signal(signal.SIGINT, self._on_shutdown)
        atexit.register(self._flush_state)

    def _on_shutdown(self, signum, frame):
        """Called when container receives SIGTERM (graceful shutdown)"""
        print(f"Shutdown signal received ({signum}). Saving state...")
        self._flush_state()
        self._running = False

    def _flush_state(self):
        """Write current state to persistent storage"""
        if self.state:
            store = SQLiteStateStore()
            for key, value in self.state.items():
                store.set(self.session_id, key, value)
            print(f"State flushed: {len(self.state)} keys saved")

    def update_state(self, **kwargs):
        self.state.update(kwargs)
        # Optional: write-through for maximum safety
        self._flush_state()

# Docker STOPSIGNAL is SIGTERM by default (15 seconds before SIGKILL)
# Agent has 15 seconds to save state on graceful shutdown
```

### Option 6: Docker persistent volume configuration

```yaml
# docker-compose.yml — persist state across container restarts
services:
  agent:
    image: my-agent:latest
    environment:
      - REDIS_HOST=redis
      - STATE_DB_PATH=/data/agent_state.db
    volumes:
      - agent-data:/data          # Persists SQLite, checkpoints, output
      - agent-logs:/app/logs      # Persists log files
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    volumes:
      - redis-data:/data          # Persists Redis data (--appendonly yes)
    command: redis-server --appendonly yes  # Write-ahead log for durability

volumes:
  agent-data:
    driver: local
  redis-data:
    driver: local
  agent-logs:
    driver: local

# State in /data/ and redis-data survive: restarts, redeploys, OOM kills
# State in container memory: lost on any process exit
```

## State Persistence Options

| Storage | Survives restart | Multi-replica | Latency | Best for |
|---|---|---|---|---|
| Python dict/list | No | No | 0ms | Never rely on it |
| SQLite + volume | Yes | Single-writer | ~1ms | Single-instance state |
| Redis | Yes | Yes | ~1ms | Multi-replica sessions |
| PostgreSQL | Yes | Yes | ~5ms | Structured state + history |
| File + volume | Yes | No | ~5ms | Checkpoints, large blobs |
| Env vars | Yes (config only) | Yes | 0ms | Configuration, not dynamic state |

## Expected Token Savings
User repeats context after restart × 10 restarts: ~50,000 tokens
Persistent state: user never needs to repeat context

## Environment
- Any agent deployed in containers (Docker/k8s) with session state or long-running tasks
- Source: direct experience; ephemeral state is the most common production surprise when moving from local dev to containerized deployment
