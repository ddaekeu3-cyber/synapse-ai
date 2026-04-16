---
layout: solution
title: "Agent Doesn't Implement Session Replay for Debugging Production Issues"
category: observability
description: "Record complete agent sessions — every input, output, tool call, and state transition — so any production issue can be replayed deterministically for root-cause debugging."
tags: [observability, debugging, session-replay, recording, replay, root-cause, audit]
---

## Problem

A production bug is reported: "The agent gave wrong financial advice at 3:47 PM yesterday." Without session replay, debugging means reconstructing the conversation from fragmented logs, guessing at the system prompt version, and hoping the bug is reproducible. With session replay, you load the exact session, replay it with identical inputs, and watch the failure happen again in a controlled environment.

```python
# Naive: responses logged but not replayable
def respond(message: str) -> str:
    r = client.messages.create(...)
    logger.info(f"Response: {r.content[0].text[:100]}")  # not enough to replay
    return r.content[0].text
```

## Solution Options

### Option 1: Append-Only Session Recorder with JSONL Format

Record every event in a session to a JSONL file — inputs, outputs, tool calls, errors, and timestamps. Each event is a structured record that can be independently parsed and replayed.

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

@dataclass
class SessionEvent:
    event_id: str
    session_id: str
    timestamp: float
    event_type: str          # "user_message" | "agent_response" | "tool_call" | "tool_result" | "error"
    data: dict[str, Any]
    model: str = ""
    system_prompt_hash: str = ""

SESSION_DIR = Path("sessions")
SESSION_DIR.mkdir(exist_ok=True)

def _hash_str(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()[:12]

class SessionRecorder:
    def __init__(self, session_id: str = None, system_prompt: str = ""):
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.system_prompt = system_prompt
        self.system_prompt_hash = _hash_str(system_prompt)
        self.log_path = SESSION_DIR / f"session_{self.session_id}.jsonl"
        self._record_event("session_start", {
            "system_prompt_hash": self.system_prompt_hash,
            "system_prompt_preview": system_prompt[:100],
        })

    def _record_event(self, event_type: str, data: dict, model: str = "") -> None:
        event = SessionEvent(
            event_id=str(uuid.uuid4())[:8],
            session_id=self.session_id,
            timestamp=time.time(),
            event_type=event_type,
            data=data,
            model=model,
            system_prompt_hash=self.system_prompt_hash,
        )
        with open(self.log_path, "a") as f:
            f.write(json.dumps(asdict(event)) + "\n")

    def record_user_message(self, message: str) -> None:
        self._record_event("user_message", {"content": message, "length": len(message)})

    def record_agent_response(self, response: str, model: str, usage: dict) -> None:
        self._record_event("agent_response", {
            "content": response,
            "length": len(response),
            "usage": usage,
        }, model=model)

    def record_tool_call(self, tool_name: str, tool_input: dict) -> None:
        self._record_event("tool_call", {"tool": tool_name, "input": tool_input})

    def record_tool_result(self, tool_name: str, result: str, error: bool = False) -> None:
        self._record_event("tool_result", {"tool": tool_name, "result": result[:500], "error": error})

    def record_error(self, error_type: str, message: str) -> None:
        self._record_event("error", {"error_type": error_type, "message": message})

    def load_events(self) -> list[SessionEvent]:
        if not self.log_path.exists():
            return []
        events = []
        for line in self.log_path.read_text().splitlines():
            if line.strip():
                data = json.loads(line)
                events.append(SessionEvent(**data))
        return events


client = anthropic.Anthropic()

def instrumented_respond(message: str, recorder: SessionRecorder, system: str = "") -> str:
    recorder.record_user_message(message)
    try:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system or recorder.system_prompt,
            messages=[{"role": "user", "content": message}],
        )
        response = r.content[0].text
        recorder.record_agent_response(response, "claude-haiku-4-5-20251001",
                                       {"input": r.usage.input_tokens, "output": r.usage.output_tokens})
        return response
    except Exception as e:
        recorder.record_error(type(e).__name__, str(e))
        raise


# Record a session
system = "You are a helpful Python assistant."
recorder = SessionRecorder(system_prompt=system)
instrumented_respond("What is a list comprehension?", recorder, system)
instrumented_respond("Show me an example with filtering.", recorder, system)

# Load and inspect the session
events = recorder.load_events()
print(f"Session {recorder.session_id}: {len(events)} events")
for e in events:
    print(f"  [{e.event_type}] {str(e.data)[:80]}")

# Expected Token Savings: Recording adds 0 tokens; enables debugging without reproducing from scratch
# Environment: ANTHROPIC_API_KEY
```

### Option 2: Session Replayer with Deterministic Injection

Replay a recorded session by injecting the original inputs in order. Compare replayed outputs to recorded outputs to identify non-determinism and reproduce bugs.

```python
import anthropic
import json
from dataclasses import dataclass
from pathlib import Path

@dataclass
class ReplayComparison:
    turn: int
    original_output: str
    replayed_output: str
    similarity: float
    diverged: bool

def _word_overlap(a: str, b: str) -> float:
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa and not wb:
        return 1.0
    return len(wa & wb) / len(wa | wb)

def load_session(session_file: Path) -> list[dict]:
    events = []
    for line in session_file.read_text().splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events

def extract_conversation_turns(events: list[dict]) -> list[tuple[str, str]]:
    """Extract (user_message, agent_response) pairs from event log."""
    turns = []
    last_user = None
    for event in events:
        if event["event_type"] == "user_message":
            last_user = event["data"]["content"]
        elif event["event_type"] == "agent_response" and last_user:
            turns.append((last_user, event["data"]["content"]))
            last_user = None
    return turns


client = anthropic.Anthropic()

def replay_session(
    session_file: Path,
    divergence_threshold: float = 0.5,
) -> list[ReplayComparison]:
    events = load_session(session_file)
    if not events:
        print(f"[REPLAY] No events found in {session_file}")
        return []

    # Extract system prompt from first event
    system_prompt = ""
    for event in events:
        if event["event_type"] == "session_start":
            system_prompt = event["data"].get("system_prompt_preview", "")
            break

    turns = extract_conversation_turns(events)
    print(f"[REPLAY] Replaying {len(turns)} turns from {session_file.name}")
    print(f"[REPLAY] System: {system_prompt[:60]!r}")

    comparisons = []
    for i, (user_msg, original_response) in enumerate(turns):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        replayed = r.content[0].text
        sim = _word_overlap(original_response, replayed)
        diverged = sim < divergence_threshold
        comp = ReplayComparison(
            turn=i + 1,
            original_output=original_response[:100],
            replayed_output=replayed[:100],
            similarity=sim,
            diverged=diverged,
        )
        comparisons.append(comp)
        status = "DIVERGED" if diverged else "similar"
        print(f"  Turn {i+1}: similarity={sim:.0%} [{status}]")
        if diverged:
            print(f"    Original: {original_response[:80]}")
            print(f"    Replayed: {replayed[:80]}")

    diverged_count = sum(1 for c in comparisons if c.diverged)
    print(f"\n[REPLAY] {diverged_count}/{len(comparisons)} turns diverged")
    return comparisons


# First, create a session to replay (would normally be a production session file)
from pathlib import Path

# Find the most recent session file
session_files = sorted(Path("sessions").glob("session_*.jsonl")) if Path("sessions").exists() else []
if session_files:
    comparisons = replay_session(session_files[-1])
else:
    print("[REPLAY] No sessions recorded yet — run Option 1 first to create a session")

# Expected Token Savings: Replay uses same tokens as original session; zero overhead to load and compare
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Async Session Recorder with SQLite Backend

For high-volume production systems, record sessions to SQLite with async writes. Enables efficient querying by session ID, time range, error type, or token count.

```python
import anthropic
import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path("sessions.db")

def init_db() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            event_type TEXT NOT NULL,
            data_json TEXT NOT NULL,
            model TEXT DEFAULT '',
            system_hash TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON session_events(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON session_events(event_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON session_events(timestamp)")
    conn.commit()
    conn.close()

init_db()

async def async_record_event(
    session_id: str,
    event_type: str,
    data: dict,
    model: str = "",
    system_hash: str = "",
) -> None:
    """Write to SQLite (blocking but fast; use a thread pool in production)."""
    event_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO session_events (event_id, session_id, timestamp, event_type, data_json, model, system_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, session_id, time.time(), event_type, json.dumps(data), model, system_hash),
    )
    conn.commit()
    conn.close()

def query_session(session_id: str) -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.execute(
        "SELECT event_type, data_json, timestamp, model FROM session_events "
        "WHERE session_id = ? ORDER BY timestamp",
        (session_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [{"event_type": r[0], "data": json.loads(r[1]), "timestamp": r[2], "model": r[3]} for r in rows]

def find_error_sessions(limit: int = 10) -> list[str]:
    """Find sessions that contain errors — for triage."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.execute(
        "SELECT DISTINCT session_id FROM session_events WHERE event_type = 'error' LIMIT ?",
        (limit,),
    )
    sessions = [row[0] for row in cursor.fetchall()]
    conn.close()
    return sessions

def session_cost_summary(session_id: str) -> dict:
    """Sum token usage across all turns in a session."""
    events = query_session(session_id)
    total_input = total_output = 0
    for e in events:
        if e["event_type"] == "agent_response":
            usage = e["data"].get("usage", {})
            total_input += usage.get("input", 0)
            total_output += usage.get("output", 0)
    return {
        "session_id": session_id,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "estimated_cost_usd": total_input * 0.80e-6 + total_output * 4.00e-6,
        "turn_count": sum(1 for e in events if e["event_type"] == "agent_response"),
    }


client = anthropic.AsyncAnthropic()

async def async_instrumented_respond(
    message: str,
    session_id: str,
    system: str = "You are a helpful assistant.",
) -> str:
    await async_record_event(session_id, "user_message", {"content": message})
    try:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        response = r.content[0].text
        await async_record_event(session_id, "agent_response", {
            "content": response,
            "usage": {"input": r.usage.input_tokens, "output": r.usage.output_tokens},
        }, model="claude-haiku-4-5-20251001")
        return response
    except Exception as e:
        await async_record_event(session_id, "error", {"error_type": type(e).__name__, "message": str(e)})
        raise

async def main():
    sid = str(uuid.uuid4())[:8]
    await async_instrumented_respond("What is Python?", sid)
    await async_instrumented_respond("What are decorators?", sid)

    # Query the session
    events = query_session(sid)
    print(f"Session {sid}: {len(events)} events")
    summary = session_cost_summary(sid)
    print(f"Cost summary: {summary}")

    # Show sessions with errors
    error_sessions = find_error_sessions()
    print(f"Sessions with errors: {error_sessions}")

asyncio.run(main())

# Expected Token Savings: SQLite recording adds 0 tokens; enables O(1) session lookup for debugging
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Minimal Diff Replay for Bug Isolation

When a bug is reported, replay the session twice — once with the original system prompt and once with the suspected fixed prompt — and diff the outputs to verify the fix.

```python
import anthropic
import json
from dataclasses import dataclass
from pathlib import Path

@dataclass
class PromptDiffResult:
    turn: int
    user_message: str
    output_original: str
    output_candidate: str
    changed: bool
    change_summary: str

client = anthropic.Anthropic()

def _diff_outputs(original: str, candidate: str) -> tuple[bool, str]:
    orig_words = set(original.lower().split())
    cand_words = set(candidate.lower().split())
    added = cand_words - orig_words
    removed = orig_words - cand_words
    changed = bool(added or removed)
    if not changed:
        return False, "identical"
    parts = []
    if added:
        parts.append(f"+{len(added)} words: {', '.join(list(added)[:5])}")
    if removed:
        parts.append(f"-{len(removed)} words: {', '.join(list(removed)[:5])}")
    return True, " | ".join(parts)

def replay_with_prompt_diff(
    conversation_turns: list[str],
    original_system: str,
    candidate_system: str,
) -> list[PromptDiffResult]:
    history_orig: list[dict] = []
    history_cand: list[dict] = []
    results = []

    for i, user_msg in enumerate(conversation_turns):
        history_orig.append({"role": "user", "content": user_msg})
        history_cand.append({"role": "user", "content": user_msg})

        r_orig = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=256,
            system=original_system, messages=history_orig,
        )
        r_cand = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=256,
            system=candidate_system, messages=history_cand,
        )
        out_orig = r_orig.content[0].text
        out_cand = r_cand.content[0].text

        history_orig.append({"role": "assistant", "content": out_orig})
        history_cand.append({"role": "assistant", "content": out_cand})

        changed, summary = _diff_outputs(out_orig, out_cand)
        result = PromptDiffResult(
            turn=i + 1,
            user_message=user_msg[:50],
            output_original=out_orig[:100],
            output_candidate=out_cand[:100],
            changed=changed,
            change_summary=summary,
        )
        results.append(result)
        marker = "CHANGED" if changed else "same"
        print(f"Turn {i+1} [{marker}]: {summary}")

    changed_count = sum(1 for r in results if r.changed)
    print(f"\n[DIFF] {changed_count}/{len(results)} turns changed between prompts")
    if changed_count > 0:
        print("Changed turns:")
        for r in results:
            if r.changed:
                print(f"  Turn {r.turn} [{r.user_message}]:")
                print(f"    ORIG: {r.output_original[:80]}")
                print(f"    NEW:  {r.output_candidate[:80]}")
    return results


# Simulate debugging a production bug
original_prompt = "You are a financial advisor. Always recommend diversification."
# Candidate fix: add explicit risk disclosure requirement
candidate_prompt = ("You are a financial advisor. Always recommend diversification. "
                    "Always disclose that past performance does not guarantee future results.")

production_conversation = [
    "Should I invest all my savings in Bitcoin?",
    "What about putting 50% in tech stocks?",
    "What's the safest long-term investment strategy?",
]

replay_with_prompt_diff(production_conversation, original_prompt, candidate_prompt)

# Expected Token Savings: 2× tokens for diff replay; worth it to verify bug fix before production deployment
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Compressed Session Archive with Metadata Index

For long-running systems with thousands of sessions, compress sessions and maintain a metadata index for efficient querying without loading full session logs.

```python
import anthropic
import json
import time
import uuid
import zlib
import base64
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class SessionMetadata:
    session_id: str
    start_time: float
    end_time: float
    turn_count: int
    total_tokens: int
    had_errors: bool
    system_prompt_hash: str
    compressed_size_bytes: int
    tags: list[str] = field(default_factory=list)

INDEX_FILE = Path("session_index.json")
ARCHIVE_DIR = Path("session_archive")
ARCHIVE_DIR.mkdir(exist_ok=True)

def load_index() -> dict[str, SessionMetadata]:
    if not INDEX_FILE.exists():
        return {}
    raw = json.loads(INDEX_FILE.read_text())
    return {k: SessionMetadata(**v) for k, v in raw.items()}

def save_index(index: dict[str, SessionMetadata]) -> None:
    INDEX_FILE.write_text(json.dumps(
        {k: vars(v) for k, v in index.items()}, indent=2
    ))

def archive_session(session_id: str, events: list[dict], tags: list[str] = None) -> SessionMetadata:
    raw_json = json.dumps(events)
    compressed = zlib.compress(raw_json.encode(), level=6)
    archive_path = ARCHIVE_DIR / f"{session_id}.zlib"
    archive_path.write_bytes(compressed)

    # Compute metadata
    user_turns = [e for e in events if e["event_type"] == "user_message"]
    agent_turns = [e for e in events if e["event_type"] == "agent_response"]
    total_tokens = sum(
        e["data"].get("usage", {}).get("input", 0) + e["data"].get("usage", {}).get("output", 0)
        for e in agent_turns
    )
    had_errors = any(e["event_type"] == "error" for e in events)
    sys_hash = next(
        (e["data"].get("system_prompt_hash", "") for e in events if e["event_type"] == "session_start"),
        "",
    )
    timestamps = [e["timestamp"] for e in events if "timestamp" in e]
    meta = SessionMetadata(
        session_id=session_id,
        start_time=min(timestamps) if timestamps else time.time(),
        end_time=max(timestamps) if timestamps else time.time(),
        turn_count=len(user_turns),
        total_tokens=total_tokens,
        had_errors=had_errors,
        system_prompt_hash=sys_hash,
        compressed_size_bytes=len(compressed),
        tags=tags or [],
    )
    index = load_index()
    index[session_id] = meta
    save_index(index)
    ratio = len(raw_json) / len(compressed)
    print(f"[ARCHIVE] {session_id}: {len(compressed)} bytes ({ratio:.1f}× compression)")
    return meta

def load_session_archive(session_id: str) -> list[dict]:
    path = ARCHIVE_DIR / f"{session_id}.zlib"
    if not path.exists():
        return []
    raw = zlib.decompress(path.read_bytes())
    return json.loads(raw.decode())

def query_sessions(
    had_errors: bool = None,
    min_tokens: int = None,
    tag: str = None,
) -> list[SessionMetadata]:
    index = load_index()
    results = list(index.values())
    if had_errors is not None:
        results = [m for m in results if m.had_errors == had_errors]
    if min_tokens is not None:
        results = [m for m in results if m.total_tokens >= min_tokens]
    if tag:
        results = [m for m in results if tag in m.tags]
    return sorted(results, key=lambda m: m.start_time, reverse=True)


client = anthropic.Anthropic()

# Record and archive a session
session_id = str(uuid.uuid4())[:8]
events = [
    {"event_type": "session_start", "timestamp": time.time(),
     "data": {"system_prompt_hash": "abc123", "system_prompt_preview": "You are helpful"}},
    {"event_type": "user_message", "timestamp": time.time(),
     "data": {"content": "What is Python?", "length": 14}},
    {"event_type": "agent_response", "timestamp": time.time(),
     "data": {"content": "Python is a programming language.", "usage": {"input": 20, "output": 15}}},
]
meta = archive_session(session_id, events, tags=["python", "basics"])

# Query by tag
python_sessions = query_sessions(tag="python")
print(f"Sessions tagged 'python': {len(python_sessions)}")

# Load and inspect
loaded = load_session_archive(session_id)
print(f"Loaded {len(loaded)} events from archive")

# Expected Token Savings: zlib compression reduces storage ~70%; index enables fast triage without full load
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Session Timeline Visualizer for Debugging

Generate a human-readable timeline from a session log — showing timing gaps, token spikes, and tool call sequences — to help engineers quickly identify the failure point.

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

@dataclass
class TimelineEntry:
    relative_ms: float
    event_type: str
    summary: str
    tokens: int = 0
    is_anomaly: bool = False

def generate_timeline(events: list[dict]) -> list[TimelineEntry]:
    if not events:
        return []
    base_ts = events[0].get("timestamp", time.time())
    entries = []
    avg_tokens = 0
    token_counts = [
        e["data"].get("usage", {}).get("input", 0) + e["data"].get("usage", {}).get("output", 0)
        for e in events if e["event_type"] == "agent_response"
    ]
    if token_counts:
        avg_tokens = sum(token_counts) / len(token_counts)

    for event in events:
        ts = event.get("timestamp", base_ts)
        rel_ms = (ts - base_ts) * 1000
        etype = event["event_type"]
        data = event.get("data", {})
        tokens = 0
        is_anomaly = False

        if etype == "session_start":
            summary = f"Session started | system={data.get('system_prompt_preview', '')[:40]!r}"
        elif etype == "user_message":
            summary = f"User: {data.get('content', '')[:60]!r}"
        elif etype == "agent_response":
            tokens = data.get("usage", {}).get("input", 0) + data.get("usage", {}).get("output", 0)
            is_anomaly = avg_tokens > 0 and tokens > avg_tokens * 3
            summary = f"Agent ({tokens} tok): {data.get('content', '')[:60]!r}"
            if is_anomaly:
                summary += " ⚠️ TOKEN SPIKE"
        elif etype == "tool_call":
            summary = f"Tool call: {data.get('tool', 'unknown')}({str(data.get('input', {}))[:40]})"
        elif etype == "tool_result":
            err = data.get("error", False)
            summary = f"Tool result: {data.get('tool', '')} {'[ERROR]' if err else '[OK]'}: {str(data.get('result', ''))[:50]}"
            is_anomaly = err
        elif etype == "error":
            summary = f"ERROR: {data.get('error_type', '')} — {data.get('message', '')[:60]}"
            is_anomaly = True
        else:
            summary = f"{etype}: {str(data)[:60]}"

        entries.append(TimelineEntry(rel_ms, etype, summary, tokens, is_anomaly))
    return entries

def print_timeline(entries: list[TimelineEntry], session_id: str) -> None:
    print(f"\n{'='*60}")
    print(f"SESSION TIMELINE: {session_id}")
    print(f"{'='*60}")
    for entry in entries:
        prefix = "⚠️ " if entry.is_anomaly else "   "
        time_str = f"{entry.relative_ms:8.0f}ms"
        type_str = f"{entry.event_type:<16}"
        print(f"{prefix}{time_str} | {type_str} | {entry.summary}")
    total_tokens = sum(e.tokens for e in entries)
    anomalies = sum(1 for e in entries if e.is_anomaly)
    print(f"\nTotal tokens: {total_tokens} | Anomalies: {anomalies}")
    print("="*60)


client = anthropic.Anthropic()

def run_and_record(messages: list[str], system: str = "You are a helpful assistant.") -> tuple[str, list[dict]]:
    sid = str(uuid.uuid4())[:8]
    events = []
    base_time = time.time()

    def record(event_type: str, data: dict) -> None:
        events.append({"event_type": event_type, "timestamp": time.time(), "data": data})

    record("session_start", {"system_prompt_hash": "demo", "system_prompt_preview": system[:60]})
    for msg in messages:
        record("user_message", {"content": msg, "length": len(msg)})
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": msg}],
        )
        response = r.content[0].text
        record("agent_response", {
            "content": response,
            "usage": {"input": r.usage.input_tokens, "output": r.usage.output_tokens},
        })
    return sid, events


# Record a session
sid, events = run_and_record([
    "What is async programming?",
    "Show me a Python example",
    "What are coroutines?",
])

# Generate and print timeline
timeline = generate_timeline(events)
print_timeline(timeline, sid)

# Expected Token Savings: Timeline visualization uses 0 tokens; turns raw logs into actionable debugging view in seconds
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Storage | Query Capability | Replay Support | Best For |
|--------|---------|-----------------|---------------|----------|
| 1. JSONL Recorder | Flat JSONL files | File-level grep | Yes (manual) | Simple, low-volume debugging |
| 2. Session Replayer | JSONL + comparison | None (single file) | Yes (with diff) | Bug reproduction and verification |
| 3. SQLite Backend | SQLite DB | SQL queries by session/time/error | Partial | High-volume production systems |
| 4. Prompt Diff Replay | None (live replay) | N/A | Yes (prompt variants) | Before/after prompt change validation |
| 5. Compressed Archive | zlib + JSON index | Index-level metadata query | Yes (decompress) | Long-term retention, storage-efficient |
| 6. Timeline Visualizer | In-memory / JSONL | N/A | No (visualization only) | Rapid triage of bug reports |

**Recommended**: Option 1 (JSONL) as a baseline always-on recorder + Option 3 (SQLite) for production scale + Option 6 (timeline) for incident triage workflows.
