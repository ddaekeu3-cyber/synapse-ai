---
layout: solution
title: "Agent Doesn't Implement Audit Log for Agent Actions"
category: observability
description: "Agents that don't maintain an immutable audit trail make it impossible to reconstruct what happened, detect unauthorized actions, or satisfy compliance requirements. An audit log records who did what, when, and why."
tags: [observability, audit-log, compliance, security, tracing, python]
---

## Problem

Without an audit log, agent systems are black boxes. When something goes wrong — an unexpected deletion, a data leak, a compliance violation — there is no record to investigate. Audit logs are essential for security forensics, regulatory compliance (SOC 2, GDPR, HIPAA), and debugging complex multi-step agent workflows.

## Solutions

### Option 1: Structured Audit Logger with Immutable Append

```python
import anthropic
import json
import time
import uuid
import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

@dataclass
class AuditEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    actor: str = ""            # e.g. "agent:assistant", "user:alice"
    action: str = ""           # e.g. "tool.call", "message.send", "file.write"
    resource: str = ""         # e.g. "tool:read_file", "file:/etc/passwd"
    outcome: str = ""          # "success" | "failure" | "denied"
    detail: dict = field(default_factory=dict)
    prev_hash: str = ""        # Chain integrity

    def to_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

class AuditLogger:
    def __init__(self, log_path: str = "/tmp/agent_audit.jsonl"):
        self._path = Path(log_path)
        self._prev_hash = "GENESIS"
        self._session_id = str(uuid.uuid4())
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _hash(self, line: str) -> str:
        return hashlib.sha256(line.encode()).hexdigest()[:16]

    def log(self, actor: str, action: str, resource: str,
            outcome: str, detail: Optional[dict] = None) -> AuditEvent:
        event = AuditEvent(
            session_id=self._session_id,
            actor=actor, action=action, resource=resource,
            outcome=outcome, detail=detail or {},
            prev_hash=self._prev_hash,
        )
        line = event.to_line()
        self._prev_hash = self._hash(line)

        with self._path.open("a") as f:
            f.write(line + "\n")

        print(f"[AUDIT] {event.timestamp:.3f} {actor} {action} {resource} -> {outcome}")
        return event

    def verify_chain(self) -> tuple[bool, int]:
        """Returns (is_valid, records_checked)."""
        prev = "GENESIS"
        count = 0
        with self._path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if event["prev_hash"] != prev:
                    return False, count
                prev = self._hash(line)
                count += 1
        return True, count

def run_agent_with_audit():
    client = anthropic.Anthropic()
    audit = AuditLogger()
    session = str(uuid.uuid4())

    # Log session start
    audit.log("agent:assistant", "session.start", f"session:{session}", "success",
              {"model": "claude-haiku-4-5-20251001"})

    tools = [{"name": "get_weather", "description": "Get current weather",
               "input_schema": {"type": "object", "properties":
                   {"city": {"type": "string"}}, "required": ["city"]}}]

    # Log API call
    audit.log("agent:assistant", "api.call", "anthropic:messages", "pending",
              {"model": "claude-haiku-4-5-20251001", "tools": ["get_weather"]})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        tools=tools,
        messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
    )

    audit.log("agent:assistant", "api.call", "anthropic:messages", "success",
              {"stop_reason": response.stop_reason, "usage": dict(response.usage)})

    for block in response.content:
        if block.type == "tool_use":
            audit.log("agent:assistant", "tool.call", f"tool:{block.name}", "success",
                      {"input": block.input, "tool_use_id": block.id})

    audit.log("agent:assistant", "session.end", f"session:{session}", "success")

    valid, count = audit.verify_chain()
    print(f"\nChain integrity: {'VALID' if valid else 'TAMPERED'} ({count} records)")

if __name__ == "__main__":
    run_agent_with_audit()

# Expected Token Savings: N/A (observability pattern)
# Environment: pip install anthropic
```

### Option 2: SQLite Audit Store with Query and Export

```python
import anthropic
import sqlite3
import json
import time
import uuid
from contextlib import contextmanager
from typing import Optional, Generator

class SQLiteAuditStore:
    def __init__(self, db_path: str = "/tmp/agent_audit.db"):
        self.db_path = db_path
        self._session_id = str(uuid.uuid4())
        self._init_db()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    ip_address TEXT,
                    correlation_id TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON audit_log(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_actor ON audit_log(actor)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_action ON audit_log(action)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON audit_log(timestamp)")

    def record(self, actor: str, action: str, resource: str, outcome: str,
               detail: Optional[dict] = None, correlation_id: Optional[str] = None) -> str:
        event_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO audit_log
                   (event_id, session_id, timestamp, actor, action, resource, outcome,
                    detail_json, correlation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, self._session_id, time.time(), actor, action, resource,
                 outcome, json.dumps(detail or {}), correlation_id)
            )
        print(f"[AUDIT] {actor} | {action} | {resource} | {outcome}")
        return event_id

    def query(self, actor: Optional[str] = None, action_prefix: Optional[str] = None,
              since: Optional[float] = None, limit: int = 100) -> list[dict]:
        clauses = []
        params = []
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if action_prefix:
            clauses.append("action LIKE ?")
            params.append(f"{action_prefix}%")
        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ?",
                params
            ).fetchall()
        return [dict(r) for r in rows]

    def export_jsonl(self, path: str, since: Optional[float] = None) -> int:
        rows = self.query(since=since, limit=0)  # limit=0 means no cap via query
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        return len(rows)

def demo():
    client = anthropic.Anthropic()
    audit = SQLiteAuditStore()
    corr = str(uuid.uuid4())[:8]

    audit.record("user:alice", "session.create", "agent:assistant", "success",
                 {"user_agent": "CLI/1.0"}, correlation_id=corr)

    audit.record("agent:assistant", "message.receive", "conversation", "success",
                 {"token_count": 12}, correlation_id=corr)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": "What is the capital of France?"}],
    )
    audit.record("agent:assistant", "api.call", "anthropic:messages", "success",
                 {"input_tokens": response.usage.input_tokens,
                  "output_tokens": response.usage.output_tokens}, correlation_id=corr)

    audit.record("agent:assistant", "message.send", "conversation", "success",
                 {"preview": response.content[0].text[:40]}, correlation_id=corr)

    # Query recent agent actions
    events = audit.query(actor="agent:assistant", action_prefix="api", limit=10)
    print(f"\nLast API calls: {len(events)}")
    for e in events:
        print(f"  {e['action']} -> {e['outcome']} ({json.loads(e['detail_json'])})")

    exported = audit.export_jsonl("/tmp/audit_export.jsonl")
    print(f"Exported {exported} records to /tmp/audit_export.jsonl")

if __name__ == "__main__":
    demo()

# Expected Token Savings: N/A (observability pattern)
# Environment: pip install anthropic; sqlite3 is stdlib
```

### Option 3: Async Audit Pipeline with Batched Writes

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class AuditRecord:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = field(default_factory=time.time)
    actor: str = ""
    action: str = ""
    resource: str = ""
    outcome: str = ""
    latency_ms: Optional[float] = None
    meta: dict = field(default_factory=dict)

class AsyncAuditPipeline:
    """Non-blocking audit: events go into a queue; a background writer flushes batches."""

    def __init__(self, log_path: str = "/tmp/async_audit.jsonl",
                 batch_size: int = 10, flush_interval: float = 2.0):
        self._queue: asyncio.Queue[AuditRecord] = asyncio.Queue(maxsize=10000)
        self._log_path = Path(log_path)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._running = False
        self._writer_task: Optional[asyncio.Task] = None
        self._flushed = 0

    async def start(self) -> None:
        self._running = True
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        self._running = False
        if self._writer_task:
            self._writer_task.cancel()
            # Flush remaining
            await self._flush_all()

    async def emit(self, actor: str, action: str, resource: str,
                   outcome: str, latency_ms: Optional[float] = None,
                   meta: Optional[dict] = None) -> None:
        record = AuditRecord(actor=actor, action=action, resource=resource,
                             outcome=outcome, latency_ms=latency_ms, meta=meta or {})
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            print("[AUDIT WARNING] Queue full, dropping event")

    async def _flush_all(self) -> None:
        batch = []
        while not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if batch:
            await self._write_batch(batch)

    async def _write_batch(self, batch: list[AuditRecord]) -> None:
        lines = "\n".join(json.dumps({
            "event_id": r.event_id, "ts": r.ts, "actor": r.actor,
            "action": r.action, "resource": r.resource, "outcome": r.outcome,
            "latency_ms": r.latency_ms, "meta": r.meta,
        }) for r in batch)
        async with asyncio.Lock():
            with self._log_path.open("a") as f:
                f.write(lines + "\n")
        self._flushed += len(batch)

    async def _writer_loop(self) -> None:
        while self._running:
            batch = []
            deadline = time.monotonic() + self._flush_interval
            while len(batch) < self._batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    record = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(record)
                except asyncio.TimeoutError:
                    break
            if batch:
                await self._write_batch(batch)
                print(f"[AUDIT] Flushed {len(batch)} records (total={self._flushed})")

async def main():
    client = anthropic.AsyncAnthropic()
    audit = AsyncAuditPipeline(batch_size=5, flush_interval=1.0)
    await audit.start()

    async def process(prompt: str, task_id: str) -> str:
        t0 = time.monotonic()
        await audit.emit("agent:assistant", "task.start", f"task:{task_id}", "pending")
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": prompt}],
            )
            latency = (time.monotonic() - t0) * 1000
            await audit.emit("agent:assistant", "api.call", "anthropic:messages",
                             "success", latency_ms=latency,
                             meta={"tokens": resp.usage.output_tokens})
            return resp.content[0].text
        except Exception as e:
            await audit.emit("agent:assistant", "api.call", "anthropic:messages",
                             "failure", meta={"error": str(e)})
            raise

    prompts = ["Name a color.", "Name an animal.", "Name a planet."]
    results = await asyncio.gather(*[
        process(p, f"t{i}") for i, p in enumerate(prompts)
    ])
    for r in results:
        print(f"[RESULT] {r[:60]}")

    await asyncio.sleep(1.5)  # Allow flush
    await audit.stop()
    print(f"\nTotal flushed: {audit._flushed}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A (observability pattern)
# Environment: pip install anthropic
```

### Option 4: Compliance Audit Log with Field Masking and Retention

```python
import anthropic
import json
import time
import uuid
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Patterns for sensitive data masking
SENSITIVE_PATTERNS = [
    (re.compile(r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b'), "[CARD-REDACTED]"),
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'), "[EMAIL-REDACTED]"),
    (re.compile(r'bearer\s+[A-Za-z0-9._\-]+', re.IGNORECASE), "bearer [TOKEN-REDACTED]"),
    (re.compile(r'"api_key"\s*:\s*"[^"]{8,}"'), '"api_key": "[KEY-REDACTED]"'),
]

def mask_sensitive(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, replacement in SENSITIVE_PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {k: mask_sensitive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_sensitive(v) for v in value]
    return value

@dataclass
class ComplianceEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    session_id: str = ""
    actor: str = ""
    action: str = ""
    resource: str = ""
    outcome: str = ""
    detail: dict = field(default_factory=dict)
    compliance_tags: list[str] = field(default_factory=list)  # e.g. ["SOC2", "GDPR"]
    data_classification: str = "internal"  # public | internal | confidential | restricted
    retention_years: int = 7

class ComplianceAuditLogger:
    def __init__(self, log_dir: str = "/tmp/compliance_audit"):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._session_id = str(uuid.uuid4())

    def _log_file(self) -> Path:
        date = time.strftime("%Y-%m-%d")
        return self._dir / f"audit-{date}.jsonl"

    def record(self, actor: str, action: str, resource: str,
               outcome: str, detail: Optional[dict] = None,
               compliance_tags: Optional[list[str]] = None,
               data_classification: str = "internal") -> ComplianceEvent:
        masked_detail = mask_sensitive(detail or {})
        event = ComplianceEvent(
            session_id=self._session_id,
            actor=actor, action=action, resource=resource,
            outcome=outcome, detail=masked_detail,
            compliance_tags=compliance_tags or [],
            data_classification=data_classification,
        )
        line = json.dumps({
            "event_id": event.event_id, "timestamp": event.timestamp,
            "session_id": event.session_id, "actor": event.actor,
            "action": event.action, "resource": event.resource,
            "outcome": event.outcome, "detail": event.detail,
            "compliance_tags": event.compliance_tags,
            "data_classification": event.data_classification,
            "retention_years": event.retention_years,
        })
        with self._log_file().open("a") as f:
            f.write(line + "\n")
        print(f"[COMPLIANCE:{data_classification.upper()}] {actor} {action} {resource} -> {outcome}")
        return event

def demo():
    client = anthropic.Anthropic()
    audit = ComplianceAuditLogger()

    # Simulate user with sensitive data in request
    user_input = "My credit card 4111 1111 1111 1111 expires soon. Help me."

    audit.record("user:bob", "message.send", "agent:assistant", "received",
                 detail={"content": user_input, "user_id": "bob-123"},
                 compliance_tags=["PCI-DSS", "SOC2"],
                 data_classification="confidential")

    # Redact before sending to LLM
    safe_input = mask_sensitive(user_input)
    audit.record("agent:assistant", "content.sanitize", "user-input", "success",
                 detail={"original_length": len(user_input), "safe_length": len(safe_input)},
                 compliance_tags=["PCI-DSS"])

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": safe_input}],
    )

    audit.record("agent:assistant", "api.call", "anthropic:messages", "success",
                 detail={"input_tokens": response.usage.input_tokens,
                         "output_tokens": response.usage.output_tokens},
                 compliance_tags=["SOC2"],
                 data_classification="internal")

    print(f"\nResponse: {response.content[0].text[:80]}")
    print(f"Audit log: {audit._log_file()}")

if __name__ == "__main__":
    demo()

# Expected Token Savings: N/A (compliance/observability pattern)
# Environment: pip install anthropic
```

### Option 5: Distributed Audit with Correlation Chain

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, ContextManager
from contextlib import contextmanager

@dataclass
class AuditContext:
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    span_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_span_id: Optional[str] = None
    actor: str = ""
    service: str = "agent"

    def child(self, child_actor: Optional[str] = None) -> "AuditContext":
        return AuditContext(
            trace_id=self.trace_id,
            parent_span_id=self.span_id,
            actor=child_actor or self.actor,
            service=self.service,
        )

class DistributedAuditLogger:
    def __init__(self):
        self._events: list[dict] = []

    def record(self, ctx: AuditContext, action: str, resource: str,
               outcome: str, detail: Optional[dict] = None,
               duration_ms: Optional[float] = None) -> None:
        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": time.time(),
            "trace_id": ctx.trace_id,
            "span_id": ctx.span_id,
            "parent_span_id": ctx.parent_span_id,
            "actor": ctx.actor,
            "service": ctx.service,
            "action": action,
            "resource": resource,
            "outcome": outcome,
            "detail": detail or {},
            "duration_ms": duration_ms,
        }
        self._events.append(event)
        print(f"[AUDIT] trace={ctx.trace_id[:8]} span={ctx.span_id[:8]} "
              f"parent={str(ctx.parent_span_id)[:8] if ctx.parent_span_id else 'root'} "
              f"| {ctx.actor} {action} -> {outcome}")

    def trace_tree(self, trace_id: str) -> list[dict]:
        return [e for e in self._events if e["trace_id"] == trace_id]

async def orchestrate(prompt: str, audit: DistributedAuditLogger) -> str:
    root_ctx = AuditContext(actor="orchestrator")
    client = anthropic.AsyncAnthropic()

    audit.record(root_ctx, "task.start", "orchestrator", "pending",
                 detail={"prompt_length": len(prompt)})

    # Sub-agent context
    agent_ctx = root_ctx.child("agent:assistant")

    t0 = time.monotonic()
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        dur = (time.monotonic() - t0) * 1000
        audit.record(agent_ctx, "api.call", "anthropic:messages", "success",
                     detail={"tokens": response.usage.output_tokens}, duration_ms=dur)
        result = response.content[0].text
    except Exception as e:
        dur = (time.monotonic() - t0) * 1000
        audit.record(agent_ctx, "api.call", "anthropic:messages", "failure",
                     detail={"error": str(e)}, duration_ms=dur)
        raise

    audit.record(root_ctx, "task.complete", "orchestrator", "success",
                 detail={"result_length": len(result)},
                 duration_ms=(time.monotonic() - t0) * 1000)

    return result

async def main():
    audit = DistributedAuditLogger()
    result = await orchestrate("Explain photosynthesis in one sentence.", audit)
    print(f"\nResult: {result[:80]}")

    # Show trace
    trace_id = audit._events[0]["trace_id"]
    tree = audit.trace_tree(trace_id)
    print(f"\nTrace {trace_id[:8]}: {len(tree)} spans")
    for e in tree:
        indent = "  " if e["parent_span_id"] else ""
        print(f"  {indent}{e['actor']} {e['action']} -> {e['outcome']} "
              f"({e['duration_ms']:.1f}ms)" if e["duration_ms"] else
              f"  {indent}{e['actor']} {e['action']} -> {e['outcome']}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A (observability pattern)
# Environment: pip install anthropic
```

### Option 6: Audit Log with Tamper-Evident Merkle Chain

```python
import anthropic
import json
import time
import uuid
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

def sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()

@dataclass
class MerkleAuditRecord:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    actor: str = ""
    action: str = ""
    resource: str = ""
    outcome: str = ""
    detail: dict = field(default_factory=dict)
    record_hash: str = ""      # hash of this record's content
    chain_hash: str = ""       # hash(prev_chain_hash + record_hash)

class TamperEvidentAuditLog:
    """Each record's chain_hash depends on all prior records.
    Any modification or insertion breaks the chain."""

    def __init__(self, log_path: str = "/tmp/merkle_audit.jsonl"):
        self._path = Path(log_path)
        self._chain_hash = "0" * 64  # Genesis
        self._count = 0

    def append(self, actor: str, action: str, resource: str,
               outcome: str, detail: Optional[dict] = None) -> MerkleAuditRecord:
        record = MerkleAuditRecord(
            actor=actor, action=action, resource=resource,
            outcome=outcome, detail=detail or {},
        )
        # Hash the record content (excluding chain fields)
        content = json.dumps({
            "event_id": record.event_id, "timestamp": record.timestamp,
            "actor": record.actor, "action": record.action,
            "resource": record.resource, "outcome": record.outcome,
            "detail": record.detail,
        }, sort_keys=True)
        record.record_hash = sha256(content)
        record.chain_hash = sha256(self._chain_hash + record.record_hash)
        self._chain_hash = record.chain_hash
        self._count += 1

        line = json.dumps({
            "event_id": record.event_id, "timestamp": record.timestamp,
            "actor": record.actor, "action": record.action,
            "resource": record.resource, "outcome": record.outcome,
            "detail": record.detail, "record_hash": record.record_hash,
            "chain_hash": record.chain_hash,
        })
        with self._path.open("a") as f:
            f.write(line + "\n")

        print(f"[MERKLE] #{self._count} {actor} {action} -> {outcome} "
              f"chain={record.chain_hash[:12]}...")
        return record

    def verify(self) -> tuple[bool, str]:
        prev_chain = "0" * 64
        count = 0
        with self._path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                content = json.dumps({
                    k: rec[k] for k in
                    ["event_id","timestamp","actor","action","resource","outcome","detail"]
                }, sort_keys=True)
                expected_record_hash = sha256(content)
                expected_chain_hash = sha256(prev_chain + expected_record_hash)

                if rec["record_hash"] != expected_record_hash:
                    return False, f"Record #{count+1} content tampered"
                if rec["chain_hash"] != expected_chain_hash:
                    return False, f"Chain broken at record #{count+1}"
                prev_chain = rec["chain_hash"]
                count += 1
        return True, f"All {count} records verified"

def demo():
    client = anthropic.Anthropic()
    audit = TamperEvidentAuditLog()

    audit.append("system", "audit.init", "log", "success",
                 {"version": "1.0", "genesis": True})

    audit.append("user:alice", "session.start", "agent", "success",
                 {"user_agent": "Python/3.11"})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": "What is 7 × 8?"}],
    )
    audit.append("agent:assistant", "api.call", "anthropic:messages", "success",
                 {"input_tokens": response.usage.input_tokens,
                  "output_tokens": response.usage.output_tokens})

    audit.append("agent:assistant", "message.send", "user:alice", "success",
                 {"answer": response.content[0].text[:40]})

    audit.append("user:alice", "session.end", "agent", "success")

    ok, msg = audit.verify()
    print(f"\nVerification: {'PASS' if ok else 'FAIL'} — {msg}")

if __name__ == "__main__":
    demo()

# Expected Token Savings: N/A (security/compliance pattern)
# Environment: pip install anthropic
```

## Comparison

| Option | Storage | Tamper Detection | Compliance Fields | Best For |
|--------|---------|-----------------|-------------------|----------|
| 1. Append + Hash chain | JSONL file | SHA-256 chain | Basic | Dev/staging |
| 2. SQLite | Relational DB | None | Full queryable | Production single-node |
| 3. Async pipeline | JSONL batched | None | Minimal | High-throughput |
| 4. Compliance + masking | JSONL daily | None | PCI/GDPR/SOC2 | Regulated environments |
| 5. Distributed trace | In-memory | None | Trace/span | Multi-agent systems |
| 6. Merkle chain | JSONL | Merkle proof | Full | Forensic/compliance |
