---
layout: solution
title: "Agent Doesn't Implement Tool Call Replay on Session Recovery"
category: tool-failure
description: "Replay incomplete or failed tool calls after a session crash or restart by persisting tool call state and resuming from the last successful checkpoint."
tags: [tool-replay, session-recovery, crash-recovery, persistence, idempotency, resilience]
---

# Agent Doesn't Implement Tool Call Replay on Session Recovery

When an agent crashes mid-task, it loses all in-flight tool calls. On restart it either repeats completed work (duplicate side effects) or skips it entirely (silent data loss). Tool call replay persists each call's intent and result to durable storage, so on recovery the agent can skip already-completed calls and retry only the ones that failed.

## Option 1: SQLite Journal with Idempotency Keys

```python
import sqlite3
import uuid
import json
import time
import anthropic

DB_PATH = "tool_journal.db"
client = anthropic.Anthropic()


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_calls (
            idempotency_key TEXT PRIMARY KEY,
            tool_name TEXT,
            tool_input TEXT,
            result TEXT,
            status TEXT,  -- 'pending' | 'completed' | 'failed'
            created_at REAL,
            completed_at REAL
        )
    """)
    conn.commit()
    return conn


def journal_tool_call(conn: sqlite3.Connection, key: str, name: str, input_data: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO tool_calls VALUES (?,?,?,NULL,'pending',?,NULL)",
        (key, name, json.dumps(input_data), time.time()),
    )
    conn.commit()


def complete_tool_call(conn: sqlite3.Connection, key: str, result: str) -> None:
    conn.execute(
        "UPDATE tool_calls SET status='completed', result=?, completed_at=? WHERE idempotency_key=?",
        (result, time.time(), key),
    )
    conn.commit()


def get_cached_result(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT result FROM tool_calls WHERE idempotency_key=? AND status='completed'",
        (key,),
    ).fetchone()
    return row[0] if row else None


# Simulated tools
def fetch_user_data(user_id: str) -> str:
    time.sleep(0.1)  # simulate latency
    return json.dumps({"user_id": user_id, "name": "Alice", "plan": "pro"})


def send_report(content: str) -> str:
    return f"Report sent: {content[:40]}..."


def run_agent_with_replay(session_id: str, user_prompt: str) -> str:
    conn = init_db()

    # Tool call 1: fetch user
    key1 = f"{session_id}:fetch_user:u42"
    cached = get_cached_result(conn, key1)
    if cached:
        user_data = cached
        print(f"[REPLAY] fetch_user — using cached result")
    else:
        journal_tool_call(conn, key1, "fetch_user", {"user_id": "u42"})
        user_data = fetch_user_data("u42")
        complete_tool_call(conn, key1, user_data)
        print(f"[EXEC] fetch_user — executed and journaled")

    # Tool call 2: send report
    key2 = f"{session_id}:send_report:u42"
    cached2 = get_cached_result(conn, key2)
    if cached2:
        print(f"[REPLAY] send_report — already sent, skipping")
        report_result = cached2
    else:
        journal_tool_call(conn, key2, "send_report", {"content": user_data})
        report_result = send_report(user_data)
        complete_tool_call(conn, key2, report_result)
        print(f"[EXEC] send_report — executed and journaled")

    conn.close()

    # LLM summarizes
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"{user_prompt}\n\nTool results:\n{user_data}\n{report_result}"}],
    )
    return r.content[0].text


if __name__ == "__main__":
    sid = "sess_abc123"
    print("=== First run ===")
    print(run_agent_with_replay(sid, "Summarize what was done for user u42."))
    print("\n=== Recovery run (simulates restart) ===")
    print(run_agent_with_replay(sid, "Summarize what was done for user u42."))

# Expected Token Savings: Replay skips re-execution; no extra LLM calls on recovery
# Environment: Python 3.9+, SQLite3; use stable idempotency keys (session+tool+input hash)
```

## Option 2: File-Based Checkpoint with Content-Addressable Keys

```python
import hashlib
import json
import os
import time
import anthropic

CHECKPOINT_DIR = ".tool_checkpoints"
os.makedirs(CHECKPOINT_DIR, exist_ok=True)
client = anthropic.Anthropic()


def make_key(tool_name: str, tool_input: dict) -> str:
    payload = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_checkpoint(key: str) -> dict | None:
    path = os.path.join(CHECKPOINT_DIR, f"{key}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_checkpoint(key: str, tool_name: str, tool_input: dict, result: str) -> None:
    path = os.path.join(CHECKPOINT_DIR, f"{key}.json")
    with open(path, "w") as f:
        json.dump({
            "tool": tool_name,
            "input": tool_input,
            "result": result,
            "saved_at": time.time(),
        }, f)


def replay_or_execute(tool_name: str, tool_input: dict, executor) -> tuple[str, bool]:
    """Returns (result, was_replayed)."""
    key = make_key(tool_name, tool_input)
    cp = load_checkpoint(key)
    if cp:
        return cp["result"], True
    result = executor(**tool_input)
    save_checkpoint(key, tool_name, tool_input, result)
    return result, False


# Simulated tools
def read_config(path: str) -> str:
    return json.dumps({"path": path, "value": "production", "version": "2.1"})


def validate_schema(data: str) -> str:
    return json.dumps({"valid": True, "fields_checked": 5})


def run_pipeline(task: str) -> str:
    steps = []

    result1, replayed1 = replay_or_execute("read_config", {"path": "/etc/agent.conf"}, read_config)
    steps.append(f"{'[REPLAY]' if replayed1 else '[EXEC]'} read_config: {result1[:60]}")

    result2, replayed2 = replay_or_execute("validate_schema", {"data": result1}, validate_schema)
    steps.append(f"{'[REPLAY]' if replayed2 else '[EXEC]'} validate_schema: {result2}")

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Task: {task}\n\nResults:\n" + "\n".join(steps)}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print("=== Run 1 ===")
    print(run_pipeline("Validate and summarize the agent configuration."))
    print("\n=== Run 2 (replay from checkpoints) ===")
    print(run_pipeline("Validate and summarize the agent configuration."))

# Expected Token Savings: File checkpoints survive process restarts; zero re-execution cost
# Environment: Python 3.9+; store CHECKPOINT_DIR on persistent volume in containerized agents
```

## Option 3: Async Tool Queue with At-Least-Once Delivery

```python
import asyncio
import json
import sqlite3
import time
import uuid
import anthropic

DB_PATH = "tool_queue.db"
client = anthropic.AsyncAnthropic()


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_queue (
            id TEXT PRIMARY KEY,
            tool_name TEXT,
            tool_input TEXT,
            result TEXT,
            attempts INTEGER DEFAULT 0,
            status TEXT DEFAULT 'queued',
            created_at REAL,
            updated_at REAL
        )
    """)
    conn.commit()
    return conn


def enqueue(conn: sqlite3.Connection, tool_name: str, tool_input: dict) -> str:
    tid = str(uuid.uuid4())[:8]
    conn.execute(
        "INSERT OR IGNORE INTO tool_queue VALUES (?,?,?,NULL,0,'queued',?,?)",
        (tid, tool_name, json.dumps(tool_input), time.time(), time.time()),
    )
    conn.commit()
    return tid


def mark_complete(conn: sqlite3.Connection, tid: str, result: str) -> None:
    conn.execute(
        "UPDATE tool_queue SET status='done', result=?, updated_at=? WHERE id=?",
        (result, time.time(), tid),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, tid: str) -> None:
    conn.execute(
        "UPDATE tool_queue SET status='failed', attempts=attempts+1, updated_at=? WHERE id=?",
        (time.time(), tid),
    )
    conn.commit()


def get_pending(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, tool_name, tool_input FROM tool_queue WHERE status IN ('queued','failed') AND attempts < 3"
    ).fetchall()
    return [{"id": r[0], "tool": r[1], "input": json.loads(r[2])} for r in rows]


async def simulate_tool(tool_name: str, tool_input: dict) -> str:
    await asyncio.sleep(0.05)
    return json.dumps({"tool": tool_name, "input": tool_input, "ok": True})


async def process_queue(conn: sqlite3.Connection) -> dict[str, str]:
    pending = get_pending(conn)
    results: dict[str, str] = {}

    async def run_one(item: dict) -> None:
        try:
            result = await simulate_tool(item["tool"], item["input"])
            mark_complete(conn, item["id"], result)
            results[item["id"]] = result
            print(f"[QUEUE] {item['tool']} ({item['id']}) — done")
        except Exception as e:
            mark_failed(conn, item["id"])
            print(f"[QUEUE] {item['tool']} ({item['id']}) — failed: {e}")

    await asyncio.gather(*[run_one(item) for item in pending])

    # Also load already-completed results
    done_rows = conn.execute(
        "SELECT id, result FROM tool_queue WHERE status='done'"
    ).fetchall()
    for tid, result in done_rows:
        results[tid] = result

    return results


async def run_agent(session_id: str, prompt: str) -> str:
    conn = init_db()

    # Enqueue tool calls (idempotent: INSERT OR IGNORE)
    t1 = enqueue(conn, "fetch_config", {"env": "prod"})
    t2 = enqueue(conn, "fetch_metrics", {"window": "1h"})

    results = await process_queue(conn)
    conn.close()

    context = "\n".join(f"[{tid}] {res[:80]}" for tid, res in results.items())
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"{prompt}\n\nTool outputs:\n{context}"}],
    )
    return r.content[0].text


asyncio.run(run_agent("sess1", "Summarize the system configuration and recent metrics."))

# Expected Token Savings: Queue survives restarts; at-least-once delivery prevents data loss
# Environment: Python 3.11+, asyncio, SQLite3; set attempts < 3 to cap retry depth
```

## Option 4: Deterministic Replay Log (Event Sourcing Style)

```python
import json
import time
import hashlib
import anthropic
from pathlib import Path

LOG_PATH = Path("tool_replay_log.jsonl")
client = anthropic.Anthropic()


def load_replay_log() -> dict[str, str]:
    """Load completed tool calls keyed by deterministic event ID."""
    completed: dict[str, str] = {}
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "tool_completed":
                completed[event["event_id"]] = event["result"]
    return completed


def append_event(event: dict) -> None:
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")


def event_id(tool_name: str, tool_input: dict) -> str:
    raw = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def replay_tool(tool_name: str, tool_input: dict, executor, replay_cache: dict[str, str]) -> str:
    eid = event_id(tool_name, tool_input)
    if eid in replay_cache:
        print(f"[REPLAY] {tool_name} ({eid}) — skipped")
        return replay_cache[eid]

    append_event({"type": "tool_started", "event_id": eid, "tool": tool_name,
                  "input": tool_input, "ts": time.time()})
    result = executor(**tool_input)
    append_event({"type": "tool_completed", "event_id": eid, "tool": tool_name,
                  "result": result, "ts": time.time()})
    replay_cache[eid] = result
    print(f"[EXEC] {tool_name} ({eid}) — logged")
    return result


# Tools
def list_files(directory: str) -> str:
    return json.dumps(["file1.py", "file2.py", "README.md"])


def count_lines(filename: str) -> str:
    return json.dumps({"filename": filename, "lines": 120})


def run_analysis(prompt: str) -> str:
    replay_cache = load_replay_log()

    files = replay_tool("list_files", {"directory": "/src"}, list_files, replay_cache)
    lines = replay_tool("count_lines", {"filename": "file1.py"}, count_lines, replay_cache)

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"{prompt}\n\nFiles: {files}\nLines: {lines}"}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print("=== Run 1 (fresh) ===")
    print(run_analysis("Summarize the source code structure."))
    print("\n=== Run 2 (replays from log) ===")
    print(run_analysis("Summarize the source code structure."))

# Expected Token Savings: Event log replay avoids re-executing tools; append-only log is crash-safe
# Environment: Python 3.9+; rotate LOG_PATH daily or per-session to bound file size
```

## Option 5: Redis-Backed Tool Call Cache with TTL

```python
import json
import hashlib
import time
import anthropic

# Simulated Redis with dict (replace with redis.Redis() in production)
_REDIS_STUB: dict[str, tuple[str, float]] = {}


def redis_set(key: str, value: str, ttl: int = 3600) -> None:
    _REDIS_STUB[key] = (value, time.time() + ttl)


def redis_get(key: str) -> str | None:
    entry = _REDIS_STUB.get(key)
    if entry and entry[1] > time.time():
        return entry[0]
    if key in _REDIS_STUB:
        del _REDIS_STUB[key]
    return None


client = anthropic.Anthropic()


def tool_cache_key(tool_name: str, tool_input: dict) -> str:
    raw = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
    return f"tool:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def cached_tool_call(tool_name: str, tool_input: dict, executor, ttl: int = 3600) -> tuple[str, bool]:
    key = tool_cache_key(tool_name, tool_input)
    cached = redis_get(key)
    if cached:
        return cached, True
    result = executor(**tool_input)
    redis_set(key, result, ttl=ttl)
    return result, False


# Tools
def fetch_exchange_rate(from_currency: str, to_currency: str) -> str:
    return json.dumps({"from": from_currency, "to": to_currency, "rate": 1.08, "ts": time.time()})


def fetch_account_balance(account_id: str) -> str:
    return json.dumps({"account_id": account_id, "balance": 10420.50, "currency": "USD"})


def run_financial_agent(prompt: str) -> str:
    rate, r1_replayed = cached_tool_call(
        "fetch_exchange_rate", {"from_currency": "USD", "to_currency": "EUR"},
        fetch_exchange_rate, ttl=300,
    )
    balance, r2_replayed = cached_tool_call(
        "fetch_account_balance", {"account_id": "acc_999"},
        fetch_account_balance, ttl=60,
    )

    print(f"[CACHE] exchange_rate={'HIT' if r1_replayed else 'MISS'}")
    print(f"[CACHE] account_balance={'HIT' if r2_replayed else 'MISS'}")

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"{prompt}\n\nRate: {rate}\nBalance: {balance}"}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print("=== Call 1 ===")
    print(run_financial_agent("What is my balance in EUR?"))
    print("\n=== Call 2 (cache hits) ===")
    print(run_financial_agent("What is my balance in EUR?"))

# Expected Token Savings: Cached tool results avoid re-fetching; TTL prevents stale data
# Environment: Python 3.9+; replace _REDIS_STUB with redis.Redis(host=..., port=6379)
```

## Option 6: Full Recovery Protocol with State Machine

```python
import json
import sqlite3
import time
import anthropic
from enum import Enum

DB_PATH = "recovery_state.db"
client = anthropic.Anthropic()


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_steps (
            pipeline_id TEXT,
            step_name TEXT,
            status TEXT DEFAULT 'pending',
            result TEXT,
            error TEXT,
            started_at REAL,
            finished_at REAL,
            PRIMARY KEY (pipeline_id, step_name)
        )
    """)
    conn.commit()
    return conn


def get_step(conn: sqlite3.Connection, pid: str, step: str) -> dict | None:
    row = conn.execute(
        "SELECT status, result FROM pipeline_steps WHERE pipeline_id=? AND step_name=?",
        (pid, step),
    ).fetchone()
    return {"status": row[0], "result": row[1]} if row else None


def start_step(conn: sqlite3.Connection, pid: str, step: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO pipeline_steps VALUES (?,?,?,NULL,NULL,?,NULL)",
        (pid, step, StepStatus.RUNNING.value, time.time()),
    )
    conn.commit()


def finish_step(conn: sqlite3.Connection, pid: str, step: str, result: str) -> None:
    conn.execute(
        "UPDATE pipeline_steps SET status=?, result=?, finished_at=? WHERE pipeline_id=? AND step_name=?",
        (StepStatus.DONE.value, result, time.time(), pid, step),
    )
    conn.commit()


def fail_step(conn: sqlite3.Connection, pid: str, step: str, error: str) -> None:
    conn.execute(
        "UPDATE pipeline_steps SET status=?, error=?, finished_at=? WHERE pipeline_id=? AND step_name=?",
        (StepStatus.FAILED.value, error, time.time(), pid, step),
    )
    conn.commit()


def execute_step(conn: sqlite3.Connection, pid: str, step_name: str, executor) -> str:
    existing = get_step(conn, pid, step_name)
    if existing and existing["status"] == StepStatus.DONE.value:
        print(f"[SKIP] {step_name} — already done")
        return existing["result"]
    start_step(conn, pid, step_name)
    try:
        result = executor()
        finish_step(conn, pid, step_name, result)
        print(f"[EXEC] {step_name} — completed")
        return result
    except Exception as e:
        fail_step(conn, pid, step_name, str(e))
        raise


def run_pipeline(pipeline_id: str, goal: str) -> str:
    conn = init_db()

    step1 = execute_step(conn, pipeline_id, "load_data",
                         lambda: json.dumps({"records": 1000, "source": "db"}))
    step2 = execute_step(conn, pipeline_id, "transform_data",
                         lambda: json.dumps({"transformed": 1000, "schema": "v2"}))
    step3 = execute_step(conn, pipeline_id, "validate_output",
                         lambda: json.dumps({"valid": True, "errors": 0}))

    conn.close()
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"{goal}\nResults: {step1}, {step2}, {step3}"}],
    )
    return r.content[0].text


if __name__ == "__main__":
    pid = "pipeline_2026_04_16"
    print("=== Run 1 ===")
    print(run_pipeline(pid, "Summarize data pipeline results."))
    print("\n=== Recovery Run (all steps skipped) ===")
    print(run_pipeline(pid, "Summarize data pipeline results."))

# Expected Token Savings: State machine skips completed steps; LLM only called once per pipeline
# Environment: Python 3.9+, SQLite3; use unique pipeline_id per logical task instance
```

## Comparison

| Option | Storage | Idempotency | TTL | Recovery Scope | Best For |
|--------|---------|-------------|-----|----------------|----------|
| 1. SQLite Journal | SQLite | Explicit key | No | Per session | Simple session recovery |
| 2. File Checkpoints | JSON files | Content hash | No | Persistent | Long-running batch jobs |
| 3. Async Queue | SQLite | Auto UUID | No | At-least-once | High-throughput agents |
| 4. Event Sourcing | JSONL log | Content hash | No | Append-only | Audit + deterministic replay |
| 5. Redis Cache | Redis/dict | Content hash | Yes | Short-term | High-frequency tool calls |
| 6. State Machine | SQLite | Step name | No | Full pipeline | Multi-step pipeline recovery |
