---
layout: solution
title: "Agent Doesn't Implement Tool Call Audit Trail"
category: tool-failure
description: "Agents that don't record tool calls make debugging, compliance, and cost attribution impossible. These patterns capture a complete, queryable audit trail of every tool invocation including inputs, outputs, latency, and errors."
tags: [audit, logging, tool-calls, observability, compliance, debugging]
---

# Agent Doesn't Implement Tool Call Audit Trail

## The Problem

When an agent silently calls tools — searches the web, queries a database, writes to files, sends messages — and nothing is recorded, you lose:

- **Debugging**: can't reproduce a failure because you don't know what tools were called
- **Compliance**: regulated industries require a record of all automated actions
- **Cost attribution**: can't identify which workflows drive API costs
- **Security**: can't audit whether an agent accessed data it shouldn't have

An audit trail records every tool invocation with its inputs, outputs, status, latency, and caller context before any result is returned to the model.

---

## Option 1: In-Memory Audit Log with Session Grouping

Capture all tool calls in memory, grouped by session, with structured metadata.

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

client = anthropic.Anthropic()

@dataclass
class ToolAuditEntry:
    audit_id: str
    session_id: str
    turn_number: int
    tool_name: str
    tool_use_id: str
    inputs: dict
    output: str
    status: str  # "success" | "error" | "timeout"
    latency_ms: int
    timestamp: str
    error_message: str | None = None

@dataclass
class AuditLog:
    entries: list[ToolAuditEntry] = field(default_factory=list)

    def record(self, entry: ToolAuditEntry):
        self.entries.append(entry)

    def get_session(self, session_id: str) -> list[ToolAuditEntry]:
        return [e for e in self.entries if e.session_id == session_id]

    def get_by_tool(self, tool_name: str) -> list[ToolAuditEntry]:
        return [e for e in self.entries if e.tool_name == tool_name]

    def session_summary(self, session_id: str) -> dict:
        session_entries = self.get_session(session_id)
        if not session_entries:
            return {}
        return {
            "session_id": session_id,
            "total_tool_calls": len(session_entries),
            "tools_used": list({e.tool_name for e in session_entries}),
            "total_latency_ms": sum(e.latency_ms for e in session_entries),
            "errors": [e for e in session_entries if e.status == "error"],
            "timeline": [
                {"tool": e.tool_name, "status": e.status, "latency_ms": e.latency_ms}
                for e in session_entries
            ]
        }

# Global audit log
audit_log = AuditLog()

def audited_tool_executor(
    tool_name: str,
    tool_use_id: str,
    tool_input: dict,
    session_id: str,
    turn_number: int,
    actual_executor: callable
) -> str:
    """Wrap any tool execution with audit recording."""
    start = time.monotonic()
    status = "success"
    output = ""
    error_message = None

    try:
        output = actual_executor(tool_name, tool_input)
    except Exception as e:
        status = "error"
        error_message = str(e)
        output = f"Error: {e}"

    latency_ms = int((time.monotonic() - start) * 1000)

    entry = ToolAuditEntry(
        audit_id=str(uuid.uuid4())[:8],
        session_id=session_id,
        turn_number=turn_number,
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        inputs=tool_input,
        output=output[:500],  # Truncate large outputs
        status=status,
        latency_ms=latency_ms,
        timestamp=datetime.utcnow().isoformat(),
        error_message=error_message
    )
    audit_log.record(entry)

    return output

def mock_tool_executor(tool_name: str, tool_input: dict) -> str:
    """Simulated tool execution."""
    time.sleep(0.05)  # Simulate latency
    if tool_name == "web_search":
        return f"Results for: {tool_input.get('query', '')}"
    elif tool_name == "calculator":
        expr = tool_input.get("expression", "0")
        return str(eval(expr, {"__builtins__": {}}))
    return f"Tool {tool_name} executed with {tool_input}"

TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    },
    {
        "name": "calculator",
        "description": "Evaluate math expressions",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"]
        }
    }
]

def run_audited_agent(user_message: str, session_id: str | None = None) -> str:
    """Agent with full tool call audit trail."""
    session_id = session_id or str(uuid.uuid4())[:8]
    messages = [{"role": "user", "content": user_message}]
    turn = 0

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason != "tool_use":
            text_blocks = [b for b in response.content if hasattr(b, "text")]
            return text_blocks[0].text if text_blocks else ""

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = audited_tool_executor(
                    tool_name=block.name,
                    tool_use_id=block.id,
                    tool_input=block.input,
                    session_id=session_id,
                    turn_number=turn,
                    actual_executor=mock_tool_executor
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
        turn += 1

# Usage
session = "sess_demo"
run_audited_agent("Search for Python best practices and calculate 2**10", session)

summary = audit_log.session_summary(session)
print(f"Session {session} audit:")
print(f"  Tool calls: {summary.get('total_tool_calls', 0)}")
print(f"  Tools used: {summary.get('tools_used', [])}")
print(f"  Timeline:")
for t in summary.get("timeline", []):
    print(f"    {t['tool']}: {t['status']} ({t['latency_ms']}ms)")

# Expected Token Savings: Audit adds zero tokens to model context; pure observability layer
# Environment: any production agent, compliance-sensitive workflows, debugging pipelines
```

---

## Option 2: SQLite Persistent Audit Trail

Persist all tool calls to SQLite for long-term storage, querying, and reporting.

```python
import anthropic
import sqlite3
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

client = anthropic.Anthropic()

AUDIT_DB = "tool_audit.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(AUDIT_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_audit_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS tool_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                agent_id TEXT,
                turn_number INTEGER,
                tool_name TEXT NOT NULL,
                tool_use_id TEXT NOT NULL,
                inputs_json TEXT NOT NULL,
                output TEXT,
                status TEXT NOT NULL,
                latency_ms INTEGER,
                error_message TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                timestamp TEXT NOT NULL,
                user_id TEXT
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_session ON tool_audit(session_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_tool ON tool_audit(tool_name)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON tool_audit(timestamp)")

def record_tool_call(
    session_id: str,
    tool_name: str,
    tool_use_id: str,
    inputs: dict,
    output: str,
    status: str,
    latency_ms: int,
    error_message: str | None = None,
    agent_id: str = "default",
    turn_number: int = 0,
    user_id: str | None = None
):
    with get_db() as db:
        db.execute("""
            INSERT INTO tool_audit
            (audit_id, session_id, agent_id, turn_number, tool_name, tool_use_id,
             inputs_json, output, status, latency_ms, error_message, timestamp, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(uuid.uuid4()),
            session_id, agent_id, turn_number, tool_name, tool_use_id,
            json.dumps(inputs), output[:2000], status, latency_ms, error_message,
            datetime.utcnow().isoformat(), user_id
        ))

def query_audit(
    session_id: str | None = None,
    tool_name: str | None = None,
    status: str | None = None,
    since_hours: int | None = None,
    limit: int = 50
) -> list[dict]:
    """Flexible audit query with optional filters."""
    conditions = []
    params = []

    if session_id:
        conditions.append("session_id = ?")
        params.append(session_id)
    if tool_name:
        conditions.append("tool_name = ?")
        params.append(tool_name)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if since_hours:
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(hours=since_hours)).isoformat()
        conditions.append("timestamp >= ?")
        params.append(cutoff)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    with get_db() as db:
        rows = db.execute(
            f"SELECT * FROM tool_audit {where} ORDER BY timestamp DESC LIMIT ?",
            params
        ).fetchall()
        return [dict(r) for r in rows]

def get_tool_stats() -> list[dict]:
    """Aggregate stats per tool."""
    with get_db() as db:
        rows = db.execute("""
            SELECT
                tool_name,
                COUNT(*) as total_calls,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as errors,
                AVG(latency_ms) as avg_latency_ms,
                MAX(latency_ms) as max_latency_ms
            FROM tool_audit
            GROUP BY tool_name
            ORDER BY total_calls DESC
        """).fetchall()
        return [dict(r) for r in rows]

def audited_call(
    tool_name: str,
    tool_use_id: str,
    inputs: dict,
    session_id: str,
    turn: int,
    executor: callable
) -> str:
    start = time.monotonic()
    status = "success"
    output = ""
    error_message = None
    try:
        output = executor(tool_name, inputs)
    except Exception as e:
        status = "error"
        error_message = str(e)
        output = f"Error: {e}"
    latency_ms = int((time.monotonic() - start) * 1000)

    record_tool_call(
        session_id=session_id,
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        inputs=inputs,
        output=output,
        status=status,
        latency_ms=latency_ms,
        error_message=error_message,
        turn_number=turn
    )
    return output

def mock_executor(tool_name: str, inputs: dict) -> str:
    time.sleep(0.02)
    if tool_name == "web_search":
        return f"Results for: {inputs.get('query')}"
    return f"Executed {tool_name}"

TOOLS = [{
    "name": "web_search",
    "description": "Search the web",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
}]

def run_agent(message: str, session_id: str) -> str:
    messages = [{"role": "user", "content": message}]
    turn = 0
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages
        )
        if resp.stop_reason != "tool_use":
            texts = [b for b in resp.content if hasattr(b, "text")]
            return texts[0].text if texts else ""

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = audited_call(block.name, block.id, block.input, session_id, turn, mock_executor)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": results})
        turn += 1

# Usage
init_audit_db()
run_agent("Search for climate change news", "session_001")
run_agent("Search for Python tutorials", "session_002")

# Query audit
print("Recent tool calls:")
for entry in query_audit(limit=5):
    print(f"  [{entry['timestamp'][:19]}] {entry['tool_name']}: {entry['status']} ({entry['latency_ms']}ms)")

print("\nTool statistics:")
for stat in get_tool_stats():
    print(f"  {stat['tool_name']}: {stat['total_calls']} calls, "
          f"{stat['errors']} errors, avg {stat['avg_latency_ms']:.0f}ms")

# Expected Token Savings: Zero token overhead; enables cost attribution queries across all sessions
# Environment: production agents, SOC 2 audits, cost dashboards, multi-session debugging
```

---

## Option 3: Structured JSONL Audit Logger

Append structured JSONL records to a rotating log file — compatible with log aggregation systems.

```python
import anthropic
import json
import time
import uuid
import gzip
import os
from datetime import datetime
from pathlib import Path

client = anthropic.Anthropic()

LOG_DIR = Path("audit_logs")
LOG_DIR.mkdir(exist_ok=True)

def get_log_file() -> Path:
    """Return today's log file path."""
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    return LOG_DIR / f"tool_audit_{date_str}.jsonl"

def write_audit_record(record: dict):
    """Append audit record to JSONL log file."""
    log_file = get_log_file()
    with open(log_file, "a") as f:
        f.write(json.dumps(record) + "\n")

def rotate_logs(keep_days: int = 7):
    """Compress and remove old log files."""
    cutoff = time.time() - (keep_days * 86400)
    for log_file in LOG_DIR.glob("tool_audit_*.jsonl"):
        if log_file.stat().st_mtime < cutoff:
            # Compress old log
            gz_path = log_file.with_suffix(".jsonl.gz")
            with open(log_file, "rb") as f_in:
                with gzip.open(gz_path, "wb") as f_out:
                    f_out.write(f_in.read())
            log_file.unlink()

def audit_tool_call(
    tool_name: str,
    tool_use_id: str,
    inputs: dict,
    session_id: str,
    turn: int,
    executor: callable,
    metadata: dict | None = None
) -> str:
    """Execute tool and write JSONL audit record."""
    started_at = datetime.utcnow().isoformat()
    start = time.monotonic()
    status = "success"
    output = ""
    error = None

    try:
        output = executor(tool_name, inputs)
    except Exception as e:
        status = "error"
        error = {"type": type(e).__name__, "message": str(e)}
        output = f"Error: {e}"

    latency_ms = int((time.monotonic() - start) * 1000)

    record = {
        "v": 1,                           # Schema version
        "audit_id": str(uuid.uuid4()),
        "session_id": session_id,
        "turn": turn,
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "inputs": inputs,
        "output_preview": output[:300],
        "output_len": len(output),
        "status": status,
        "error": error,
        "latency_ms": latency_ms,
        "started_at": started_at,
        "completed_at": datetime.utcnow().isoformat(),
        **(metadata or {})
    }
    write_audit_record(record)
    return output

def read_audit_log(date_str: str | None = None) -> list[dict]:
    """Read audit records from a specific date (default: today)."""
    if date_str is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

    log_file = LOG_DIR / f"tool_audit_{date_str}.jsonl"
    if not log_file.exists():
        return []

    records = []
    with open(log_file) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records

def mock_executor(tool_name: str, inputs: dict) -> str:
    time.sleep(0.03)
    return f"Result for {tool_name}: {list(inputs.values())[0] if inputs else ''}"

TOOLS = [
    {"name": "web_search", "description": "Search web",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "summarize", "description": "Summarize text",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}
]

def run_agent(message: str, session_id: str, user_id: str = "anonymous") -> str:
    messages = [{"role": "user", "content": message}]
    turn = 0
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages
        )
        if resp.stop_reason != "tool_use":
            texts = [b for b in resp.content if hasattr(b, "text")]
            return texts[0].text if texts else ""

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = audit_tool_call(
                    tool_name=block.name,
                    tool_use_id=block.id,
                    inputs=block.input,
                    session_id=session_id,
                    turn=turn,
                    executor=mock_executor,
                    metadata={"user_id": user_id, "agent_version": "1.2.0"}
                )
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": results})
        turn += 1

# Usage
run_agent("Search for AI news and summarize", "sess_abc", user_id="user_42")

# Read back audit log
records = read_audit_log()
print(f"Audit records written today: {len(records)}")
for r in records:
    print(f"  [{r['started_at'][:19]}] {r['tool_name']} ({r['status']}, {r['latency_ms']}ms)")

log_file = get_log_file()
print(f"\nLog file: {log_file} ({log_file.stat().st_size} bytes)")
print("[Compatible with: Datadog, Splunk, Elasticsearch, CloudWatch Logs]")

# Expected Token Savings: JSONL logging adds ~0ms latency; log shipping to aggregation system enables free-tier querying
# Environment: cloud deployments, log aggregation pipelines, ELK/Datadog/Splunk integrations
```

---

## Option 4: Tool Call Replay Recorder

Record tool call sequences to enable deterministic replay for debugging failed sessions.

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

client = anthropic.Anthropic()

REPLAY_DIR = Path("replay_recordings")
REPLAY_DIR.mkdir(exist_ok=True)

@dataclass
class ReplayRecording:
    session_id: str
    user_message: str
    model: str
    tool_calls: list[dict] = field(default_factory=list)
    final_response: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_message": self.user_message,
            "model": self.model,
            "tool_calls": self.tool_calls,
            "final_response": self.final_response,
            "created_at": self.created_at
        }

    def save(self):
        path = REPLAY_DIR / f"{self.session_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, session_id: str) -> "ReplayRecording":
        path = REPLAY_DIR / f"{session_id}.json"
        data = json.loads(path.read_text())
        rec = cls(
            session_id=data["session_id"],
            user_message=data["user_message"],
            model=data["model"]
        )
        rec.tool_calls = data["tool_calls"]
        rec.final_response = data["final_response"]
        rec.created_at = data["created_at"]
        return rec

def record_and_run(
    user_message: str,
    tools: list[dict],
    executor: callable,
    model: str = "claude-haiku-4-5-20251001"
) -> tuple[str, ReplayRecording]:
    """Run agent while recording all tool calls for replay."""
    session_id = str(uuid.uuid4())[:8]
    recording = ReplayRecording(
        session_id=session_id,
        user_message=user_message,
        model=model,
        created_at=__import__("datetime").datetime.utcnow().isoformat()
    )

    messages = [{"role": "user", "content": user_message}]
    turn = 0

    while True:
        resp = client.messages.create(
            model=model,
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if resp.stop_reason != "tool_use":
            texts = [b for b in resp.content if hasattr(b, "text")]
            recording.final_response = texts[0].text if texts else ""
            recording.save()
            return recording.final_response, recording

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                start = time.monotonic()
                output = executor(block.name, block.input)
                latency_ms = int((time.monotonic() - start) * 1000)

                # Record for replay
                recording.tool_calls.append({
                    "turn": turn,
                    "tool_use_id": block.id,
                    "tool_name": block.name,
                    "inputs": block.input,
                    "output": output,
                    "latency_ms": latency_ms
                })

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output
                })

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": results})
        turn += 1

def replay_session(session_id: str, new_executor: callable | None = None) -> dict:
    """
    Replay a recorded session. Uses recorded outputs by default.
    Pass new_executor to re-run tools with real calls (for comparison).
    """
    recording = ReplayRecording.load(session_id)
    print(f"Replaying session {session_id}")
    print(f"  Original message: {recording.user_message}")
    print(f"  Recorded tool calls: {len(recording.tool_calls)}")

    replay_results = []
    for i, call in enumerate(recording.tool_calls):
        print(f"\n  Turn {call['turn']}: {call['tool_name']}")
        print(f"    Inputs: {call['inputs']}")
        print(f"    Recorded output: {call['output'][:100]}")

        if new_executor:
            new_output = new_executor(call["tool_name"], call["inputs"])
            match = new_output == call["output"]
            print(f"    New output: {new_output[:100]}")
            print(f"    Outputs match: {match}")
            replay_results.append({**call, "new_output": new_output, "match": match})
        else:
            replay_results.append(call)

    return {
        "session_id": session_id,
        "original_response": recording.final_response,
        "tool_calls_replayed": len(replay_results),
        "replay_results": replay_results
    }

def mock_executor(tool_name: str, inputs: dict) -> str:
    return f"[TOOL:{tool_name}] {list(inputs.values())[0][:50] if inputs else ''}"

TOOLS = [{
    "name": "web_search",
    "description": "Search the web",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
}]

# Record a session
response, recording = record_and_run(
    "Search for the latest news on quantum computing",
    TOOLS,
    mock_executor
)
print(f"Session recorded: {recording.session_id}")
print(f"Tool calls captured: {len(recording.tool_calls)}")

# Replay it
print("\n" + "="*50)
replay = replay_session(recording.session_id)
print(f"\nReplay complete: {replay['tool_calls_replayed']} calls replayed")

# Expected Token Savings: Replay reuses recorded outputs; debug without re-running expensive tool calls
# Environment: debugging complex agent sessions, regression testing, incident post-mortems
```

---

## Option 5: Async Audit Sink with Batch Flushing

Non-blocking audit writes using an async queue that batches records for efficient I/O.

```python
import anthropic
import asyncio
import json
import time
import uuid
from datetime import datetime
from collections import deque

client = anthropic.AsyncAnthropic()

class AsyncAuditSink:
    """Non-blocking audit sink that batches writes via asyncio queue."""

    def __init__(self, flush_interval_s: float = 2.0, batch_size: int = 10):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._buffer: deque = deque()
        self._flush_interval = flush_interval_s
        self._batch_size = batch_size
        self._flushed_count = 0
        self._task: asyncio.Task | None = None

    async def start(self):
        """Start background flush task."""
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self):
        """Flush remaining records and stop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()

    async def record(self, entry: dict):
        """Non-blocking record — put to queue immediately."""
        entry["queued_at"] = datetime.utcnow().isoformat()
        await self._queue.put(entry)

    async def _flush_loop(self):
        """Background task: drain queue and flush in batches."""
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                break

    async def _flush(self):
        """Drain queue into buffer, then write batch."""
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                self._buffer.append(item)
            except asyncio.QueueEmpty:
                break

        if not self._buffer:
            return

        batch = []
        while self._buffer and len(batch) < self._batch_size:
            batch.append(self._buffer.popleft())

        await self._write_batch(batch)
        self._flushed_count += len(batch)

    async def _write_batch(self, batch: list[dict]):
        """Write a batch of audit records (simulated I/O)."""
        # In production: write to DB, S3, or log aggregation service
        await asyncio.sleep(0.001)  # Simulate async I/O
        for record in batch:
            pass  # Would write here

    @property
    def stats(self) -> dict:
        return {
            "queued": self._queue.qsize(),
            "buffered": len(self._buffer),
            "flushed": self._flushed_count
        }

# Global async sink
audit_sink = AsyncAuditSink(flush_interval_s=1.0, batch_size=20)

async def audited_tool_execute(
    tool_name: str,
    tool_use_id: str,
    inputs: dict,
    session_id: str,
    executor: callable
) -> str:
    """Execute tool and non-blockingly enqueue audit record."""
    start = time.monotonic()
    status = "success"
    output = ""
    error = None

    try:
        output = await executor(tool_name, inputs)
    except Exception as e:
        status = "error"
        error = str(e)
        output = f"Error: {e}"

    latency_ms = int((time.monotonic() - start) * 1000)

    # Non-blocking audit record
    await audit_sink.record({
        "audit_id": str(uuid.uuid4()),
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "inputs": inputs,
        "output_preview": output[:200],
        "status": status,
        "error": error,
        "latency_ms": latency_ms,
        "timestamp": datetime.utcnow().isoformat()
    })

    return output

async def mock_async_executor(tool_name: str, inputs: dict) -> str:
    await asyncio.sleep(0.02)
    return f"Result: {list(inputs.values())[0] if inputs else tool_name}"

TOOLS = [{
    "name": "web_search",
    "description": "Search web",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
}]

async def run_async_agent(message: str, session_id: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages
        )
        if resp.stop_reason != "tool_use":
            texts = [b for b in resp.content if hasattr(b, "text")]
            return texts[0].text if texts else ""

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = await audited_tool_execute(
                    block.name, block.id, block.input, session_id, mock_async_executor
                )
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": results})

async def main():
    await audit_sink.start()

    # Run concurrent sessions
    sessions = [
        run_async_agent("Search for Python", f"sess_{i}")
        for i in range(3)
    ]
    results = await asyncio.gather(*sessions)

    await asyncio.sleep(1.5)  # Let flush cycle run
    await audit_sink.stop()

    print(f"Audit sink stats: {audit_sink.stats}")
    print(f"Responses: {[r[:50] for r in results]}")

asyncio.run(main())

# Expected Token Savings: Async audit adds <1ms to tool latency; enables high-throughput agents without I/O bottleneck
# Environment: high-throughput async agents, concurrent session pools, latency-sensitive production
```

---

## Option 6: Risk-Scored Audit with Alerting

Score each tool call by risk level and trigger alerts for high-risk actions.

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

client = anthropic.Anthropic()

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

# Risk scoring rules per tool
TOOL_RISK_RULES = {
    "web_search": {"base_risk": RiskLevel.LOW, "score": 1},
    "calculator": {"base_risk": RiskLevel.LOW, "score": 1},
    "file_reader": {"base_risk": RiskLevel.MEDIUM, "score": 3},
    "file_writer": {"base_risk": RiskLevel.HIGH, "score": 7},
    "database_query": {"base_risk": RiskLevel.HIGH, "score": 8},
    "send_email": {"base_risk": RiskLevel.CRITICAL, "score": 10},
    "delete_record": {"base_risk": RiskLevel.CRITICAL, "score": 10},
    "api_call": {"base_risk": RiskLevel.MEDIUM, "score": 5},
}

@dataclass
class RiskedAuditEntry:
    audit_id: str
    session_id: str
    tool_name: str
    tool_use_id: str
    inputs: dict
    output: str
    status: str
    latency_ms: int
    risk_level: RiskLevel
    risk_score: int
    input_risk_factors: list[str]
    timestamp: str
    alert_triggered: bool = False

def score_tool_inputs(tool_name: str, inputs: dict) -> tuple[int, list[str]]:
    """Add risk points based on input characteristics."""
    extra_score = 0
    factors = []

    inputs_str = json.dumps(inputs).lower()

    if any(w in inputs_str for w in ["delete", "drop", "truncate", "destroy"]):
        extra_score += 5
        factors.append("destructive_keyword")

    if any(w in inputs_str for w in ["password", "secret", "token", "key", "credential"]):
        extra_score += 4
        factors.append("sensitive_data_access")

    if any(w in inputs_str for w in ["/etc/", "~/.ssh", "~/.aws", "/root"]):
        extra_score += 6
        factors.append("system_path_access")

    if any(w in inputs_str for w in ["select *", "all users", "all records"]):
        extra_score += 3
        factors.append("bulk_data_access")

    return extra_score, factors

def calculate_risk(tool_name: str, inputs: dict) -> tuple[RiskLevel, int, list[str]]:
    """Calculate full risk score for a tool call."""
    base = TOOL_RISK_RULES.get(tool_name, {"base_risk": RiskLevel.MEDIUM, "score": 5})
    base_score = base["score"]

    input_score, factors = score_tool_inputs(tool_name, inputs)
    total_score = base_score + input_score

    if total_score >= 10:
        level = RiskLevel.CRITICAL
    elif total_score >= 7:
        level = RiskLevel.HIGH
    elif total_score >= 4:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW

    return level, total_score, factors

def trigger_alert(entry: RiskedAuditEntry):
    """Fire alert for high-risk tool calls."""
    print(f"\n{'='*50}")
    print(f"[AUDIT ALERT] {entry.risk_level.upper()} RISK TOOL CALL")
    print(f"  Tool: {entry.tool_name}")
    print(f"  Session: {entry.session_id}")
    print(f"  Risk score: {entry.risk_score}")
    print(f"  Risk factors: {entry.input_risk_factors}")
    print(f"  Inputs: {json.dumps(entry.inputs)[:200]}")
    print(f"  Time: {entry.timestamp}")
    print(f"{'='*50}\n")
    # In production: send to PagerDuty, Slack, SIEM, etc.

audit_entries: list[RiskedAuditEntry] = []

def risk_audited_tool_call(
    tool_name: str,
    tool_use_id: str,
    inputs: dict,
    session_id: str,
    executor: callable,
    alert_threshold: RiskLevel = RiskLevel.HIGH
) -> str:
    risk_level, risk_score, factors = calculate_risk(tool_name, inputs)
    start = time.monotonic()
    status = "success"
    output = ""
    try:
        output = executor(tool_name, inputs)
    except Exception as e:
        status = "error"
        output = str(e)
    latency_ms = int((time.monotonic() - start) * 1000)

    risk_order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
    should_alert = risk_order.index(risk_level) >= risk_order.index(alert_threshold)

    entry = RiskedAuditEntry(
        audit_id=str(uuid.uuid4())[:8],
        session_id=session_id,
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        inputs=inputs,
        output=output[:300],
        status=status,
        latency_ms=latency_ms,
        risk_level=risk_level,
        risk_score=risk_score,
        input_risk_factors=factors,
        timestamp=datetime.utcnow().isoformat(),
        alert_triggered=should_alert
    )
    audit_entries.append(entry)

    if should_alert:
        trigger_alert(entry)

    return output

def mock_executor(tool_name: str, inputs: dict) -> str:
    return f"Executed {tool_name}"

# Demo: simulate various tool calls with different risk levels
test_calls = [
    ("web_search", {"query": "python tutorials"}),
    ("file_writer", {"path": "/tmp/output.txt", "content": "hello"}),
    ("database_query", {"sql": "SELECT * FROM users"}),
    ("delete_record", {"table": "orders", "id": "12345"}),
    ("file_reader", {"path": "/etc/passwd", "mode": "read"}),
]

session = "sess_risk_demo"
for tool_name, inputs in test_calls:
    risk_audited_tool_call(tool_name, str(uuid.uuid4())[:8], inputs, session, mock_executor)

print("\nAudit summary:")
for e in audit_entries:
    alert = " ⚠️ ALERT" if e.alert_triggered else ""
    print(f"  {e.tool_name}: {e.risk_level} (score={e.risk_score}){alert}")
    if e.input_risk_factors:
        print(f"    Factors: {e.input_risk_factors}")

# Expected Token Savings: Risk scoring is pure Python — zero LLM tokens; catches dangerous calls before they cause damage
# Environment: autonomous agents with destructive tools, compliance requirements, security-sensitive deployments
```

---

## Comparison

| Option | Storage | Query Support | Replay | Alerting | Best For |
|--------|---------|--------------|--------|----------|----------|
| 1. In-Memory | RAM | In-process only | No | No | Dev/testing, single session |
| 2. SQLite | Disk | SQL queries | No | No | Production with audit requirements |
| 3. JSONL File | Disk | grep/jq/ELK | No | No | Log aggregation pipelines |
| 4. Replay Recorder | Disk (JSON) | No | Yes | No | Debugging, regression testing |
| 5. Async Sink | Queue + I/O | Via sink destination | No | No | High-throughput async agents |
| 6. Risk-Scored | In-Memory/Any | Filtered by risk | No | Yes | Security-sensitive, autonomous agents |

**Recommended defaults:**
- **Production standard** → Option 2 (SQLite) + Option 3 (JSONL for log shipping)
- **Debugging/post-mortems** → Option 4 (replay recorder)
- **High-throughput async** → Option 5 (async sink)
- **Security-critical agents** → Option 6 (risk-scored with alerting)
- **Zero-setup start** → Option 1 (in-memory)
