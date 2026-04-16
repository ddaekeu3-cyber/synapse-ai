---
layout: solution
title: "Agent Doesn't Implement Request Replay for Debugging"
category: observability
description: "When an agent produces a wrong or unexpected output, developers cannot reproduce the exact conditions that caused it. Request replay captures the complete input state — messages, model, tools, parameters — and replays it identically, making bugs reproducible and debuggable."
tags: [observability, debugging, replay, testing, reproducibility, python]
---

## Problem

Agent bugs are notoriously hard to reproduce. The exact token sequence, tool results, and model parameters from a production failure are rarely captured, so developers debug against reconstructed approximations that don't trigger the same bug. Request replay records every field of every API call and can re-run it exactly — the same messages, same model, same temperature — reproducing the failure deterministically.

## Solutions

### Option 1: Request Logger with File-Based Replay

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

@dataclass
class RecordedRequest:
    request_id: str
    recorded_at: float
    model: str
    messages: list[dict]
    system: Optional[str]
    max_tokens: int
    temperature: Optional[float]
    tools: Optional[list[dict]]
    tool_choice: Optional[dict]
    response_text: str
    response_stop_reason: str
    input_tokens: int
    output_tokens: int
    tags: dict = field(default_factory=dict)

class RequestRecorder:
    def __init__(self, log_dir: str = "/tmp/agent_replay_logs"):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _log_path(self, request_id: str) -> Path:
        return self._dir / f"{request_id}.json"

    def create(self, model: str, messages: list[dict],
               system: Optional[str] = None, max_tokens: int = 1000,
               temperature: Optional[float] = None,
               tools: Optional[list] = None,
               tool_choice: Optional[dict] = None,
               tags: Optional[dict] = None) -> "RecordingClient":
        return RecordingClient(self, model=model, messages=messages,
                               system=system, max_tokens=max_tokens,
                               temperature=temperature, tools=tools,
                               tool_choice=tool_choice, tags=tags or {})

    def save(self, record: RecordedRequest) -> str:
        path = self._log_path(record.request_id)
        path.write_text(json.dumps(asdict(record), indent=2))
        return str(path)

    def load(self, request_id: str) -> Optional[RecordedRequest]:
        path = self._log_path(request_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return RecordedRequest(**data)

    def list_recent(self, limit: int = 10) -> list[str]:
        files = sorted(self._dir.glob("*.json"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        return [f.stem for f in files[:limit]]

    def replay(self, request_id: str,
               client: anthropic.Anthropic) -> Optional[tuple[str, str]]:
        """Replay a recorded request. Returns (response_text, stop_reason)."""
        record = self.load(request_id)
        if not record:
            print(f"[REPLAY] Not found: {request_id}")
            return None

        print(f"[REPLAY] Replaying {request_id}")
        print(f"  Recorded: {time.ctime(record.recorded_at)}")
        print(f"  Model: {record.model} | Tokens: {record.max_tokens}")
        print(f"  Original response: {record.response_text[:60]}")

        kwargs: dict[str, Any] = {
            "model": record.model,
            "messages": record.messages,
            "max_tokens": record.max_tokens,
        }
        if record.system:
            kwargs["system"] = record.system
        if record.temperature is not None:
            kwargs["temperature"] = record.temperature

        response = client.messages.create(**kwargs)
        new_text = response.content[0].text
        print(f"  Replayed response: {new_text[:60]}")

        match = record.response_text.strip()[:40] == new_text.strip()[:40]
        print(f"  Response similarity: {'similar' if match else 'different'}")
        return new_text, response.stop_reason

class RecordingClient:
    """Wraps an API call to record it for later replay."""
    def __init__(self, recorder: RequestRecorder, **kwargs):
        self._recorder = recorder
        self._kwargs = kwargs

    def execute(self, client: anthropic.Anthropic) -> tuple[str, str, str]:
        """Returns (response_text, stop_reason, request_id)."""
        request_id = str(uuid.uuid4())[:12]
        kwargs = dict(self._kwargs)

        api_kwargs: dict[str, Any] = {
            "model": kwargs["model"],
            "messages": kwargs["messages"],
            "max_tokens": kwargs["max_tokens"],
        }
        if kwargs.get("system"):
            api_kwargs["system"] = kwargs["system"]
        if kwargs.get("temperature") is not None:
            api_kwargs["temperature"] = kwargs["temperature"]

        response = client.messages.create(**api_kwargs)
        response_text = response.content[0].text

        record = RecordedRequest(
            request_id=request_id, recorded_at=time.time(),
            model=kwargs["model"], messages=kwargs["messages"],
            system=kwargs.get("system"), max_tokens=kwargs["max_tokens"],
            temperature=kwargs.get("temperature"),
            tools=kwargs.get("tools"), tool_choice=kwargs.get("tool_choice"),
            response_text=response_text, response_stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            tags=kwargs.get("tags", {}),
        )
        path = self._recorder.save(record)
        print(f"[RECORDED] {request_id} → {path}")
        return response_text, response.stop_reason, request_id

if __name__ == "__main__":
    client = anthropic.Anthropic()
    recorder = RequestRecorder()

    # Record
    recording = recorder.create(
        model="claude-haiku-4-5-20251001",
        messages=[{"role": "user", "content": "What is 17 × 23? Show your work."}],
        max_tokens=100,
        tags={"feature": "math", "user_id": "test-user"},
    )
    text, stop, req_id = recording.execute(client)
    print(f"Response: {text[:70]}")

    # Replay
    print(f"\nReplaying request {req_id}...")
    recorder.replay(req_id, client)

    # List recent
    print(f"\nRecent requests: {recorder.list_recent(5)}")

# Expected Token Savings: N/A — replay uses same tokens as original; saves debugging time
# Environment: pip install anthropic
```

### Option 2: SQLite-Backed Replay Store with Query and Diff

```python
import anthropic
import sqlite3
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional, Generator

@dataclass
class ReplayRecord:
    request_id: str
    session_id: str
    model: str
    messages_json: str
    kwargs_json: str
    response_text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    recorded_at: float
    tags_json: str = "{}"
    replay_count: int = 0

class SQLiteReplayStore:
    def __init__(self, db_path: str = "/tmp/agent_replay.db"):
        self._db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS replay_log (
                    request_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    kwargs_json TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    stop_reason TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    recorded_at REAL NOT NULL,
                    tags_json TEXT DEFAULT '{}',
                    replay_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON replay_log(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_recorded ON replay_log(recorded_at)")

    def record(self, session_id: str, model: str, messages: list,
               kwargs: dict, response_text: str, stop_reason: str,
               input_tokens: int, output_tokens: int,
               tags: Optional[dict] = None) -> str:
        rid = str(uuid.uuid4())[:12]
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO replay_log VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                (rid, session_id, model, json.dumps(messages),
                 json.dumps(kwargs), response_text, stop_reason,
                 input_tokens, output_tokens, time.time(),
                 json.dumps(tags or {}))
            )
        return rid

    def get(self, request_id: str) -> Optional[ReplayRecord]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM replay_log WHERE request_id=?", (request_id,)
            ).fetchone()
        return ReplayRecord(**dict(row)) if row else None

    def query(self, session_id: Optional[str] = None,
              model: Optional[str] = None, limit: int = 20) -> list[dict]:
        clauses, params = [], []
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if model:
            clauses.append("model=?")
            params.append(model)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT request_id, model, stop_reason, input_tokens, "
                f"output_tokens, recorded_at, replay_count FROM replay_log "
                f"{where} ORDER BY recorded_at DESC LIMIT ?", params
            ).fetchall()
        return [dict(r) for r in rows]

    def replay(self, request_id: str, client: anthropic.Anthropic) -> Optional[dict]:
        record = self.get(request_id)
        if not record:
            return None

        messages = json.loads(record.messages_json)
        kwargs = json.loads(record.kwargs_json)
        kwargs.pop("messages", None)  # Already in messages

        t0 = time.monotonic()
        response = client.messages.create(
            model=record.model,
            messages=messages,
            max_tokens=kwargs.get("max_tokens", 500),
            **({"system": kwargs["system"]} if kwargs.get("system") else {}),
        )
        latency_ms = (time.monotonic() - t0) * 1000
        new_text = response.content[0].text

        with self._conn() as conn:
            conn.execute("UPDATE replay_log SET replay_count=replay_count+1 WHERE request_id=?",
                         (request_id,))

        diff_start = next((i for i, (a, b) in enumerate(zip(record.response_text, new_text))
                           if a != b), min(len(record.response_text), len(new_text)))
        return {
            "request_id": request_id, "original": record.response_text,
            "replayed": new_text, "latency_ms": latency_ms,
            "first_diff_at": diff_start,
            "identical": record.response_text == new_text,
        }

def run_demo():
    client = anthropic.Anthropic()
    store = SQLiteReplayStore()
    session_id = str(uuid.uuid4())[:8]

    prompts = ["What is entropy?", "Define recursion.", "What is a quasar?"]
    request_ids = []

    for prompt in prompts:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        rid = store.record(
            session_id=session_id, model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": prompt}],
            kwargs={"max_tokens": 80},
            response_text=response.content[0].text,
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        request_ids.append(rid)
        print(f"[RECORDED] {rid}: {response.content[0].text[:50]}")

    print(f"\nReplaying {request_ids[0]}...")
    result = store.replay(request_ids[0], client)
    if result:
        print(f"  Original:  {result['original'][:60]}")
        print(f"  Replayed:  {result['replayed'][:60]}")
        print(f"  Identical: {result['identical']} | First diff at char {result['first_diff_at']}")

    print(f"\nSession requests: {store.query(session_id=session_id)}")

if __name__ == "__main__":
    run_demo()

# Expected Token Savings: N/A — replay enables faster debugging, reducing total debug-session token use
# Environment: pip install anthropic; sqlite3 is stdlib
```

### Option 3: Cassette-Style Request/Response Capture for Testing

```python
import anthropic
import json
import time
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any
from unittest.mock import MagicMock, patch

@dataclass
class CassetteEntry:
    request_hash: str
    model: str
    messages: list[dict]
    response_text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    recorded_at: float

class Cassette:
    """VCR-style cassette: records API calls, replays on subsequent runs.
    Tests run against recorded responses — no live API needed."""

    def __init__(self, name: str, cassette_dir: str = "/tmp/cassettes",
                 mode: str = "auto"):
        self._name = name
        self._path = Path(cassette_dir) / f"{name}.json"
        self._mode = mode  # "record" | "replay" | "auto"
        self._entries: list[CassetteEntry] = []
        self._play_index = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            self._load()

    def _hash_request(self, model: str, messages: list) -> str:
        content = json.dumps({"model": model, "messages": messages}, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _load(self) -> None:
        data = json.loads(self._path.read_text())
        self._entries = [CassetteEntry(**e) for e in data]
        print(f"[CASSETTE] Loaded {len(self._entries)} entries from {self._path}")

    def _save(self) -> None:
        import dataclasses
        self._path.write_text(json.dumps(
            [dataclasses.asdict(e) for e in self._entries], indent=2
        ))

    def record(self, model: str, messages: list, response_text: str,
               stop_reason: str, input_tokens: int, output_tokens: int) -> None:
        entry = CassetteEntry(
            request_hash=self._hash_request(model, messages),
            model=model, messages=messages,
            response_text=response_text, stop_reason=stop_reason,
            input_tokens=input_tokens, output_tokens=output_tokens,
            recorded_at=time.time(),
        )
        self._entries.append(entry)
        self._save()
        print(f"[CASSETTE] Recorded entry #{len(self._entries)}: {response_text[:40]}")

    def play(self, model: str, messages: list) -> Optional[CassetteEntry]:
        req_hash = self._hash_request(model, messages)
        # Try hash match first
        for entry in self._entries:
            if entry.request_hash == req_hash:
                print(f"[CASSETTE] Playing back hash-match: {entry.response_text[:40]}")
                return entry
        # Fall back to sequential
        if self._play_index < len(self._entries):
            entry = self._entries[self._play_index]
            self._play_index += 1
            print(f"[CASSETTE] Playing back sequential #{self._play_index}: "
                  f"{entry.response_text[:40]}")
            return entry
        return None

def create_cassette_client(client: anthropic.Anthropic,
                            cassette: Cassette) -> Any:
    """Returns a wrapped client that records/replays calls."""
    class CassetteClient:
        class messages:
            @staticmethod
            def create(model: str, messages: list, max_tokens: int = 500,
                       system: Optional[str] = None, **kwargs) -> Any:
                if cassette._mode == "replay" or (
                    cassette._mode == "auto" and cassette._entries
                ):
                    entry = cassette.play(model, messages)
                    if entry:
                        mock_response = MagicMock()
                        mock_response.content = [MagicMock(text=entry.response_text)]
                        mock_response.stop_reason = entry.stop_reason
                        mock_response.usage.input_tokens = entry.input_tokens
                        mock_response.usage.output_tokens = entry.output_tokens
                        return mock_response

                # Live call (record mode)
                api_kwargs: dict[str, Any] = {"model": model, "messages": messages,
                                               "max_tokens": max_tokens}
                if system:
                    api_kwargs["system"] = system
                response = client.messages.create(**api_kwargs)
                text = response.content[0].text
                cassette.record(model, messages, text, response.stop_reason,
                                response.usage.input_tokens, response.usage.output_tokens)
                return response
    return CassetteClient()

def run_with_cassette(cassette_name: str, record: bool = False):
    client = anthropic.Anthropic()
    cassette = Cassette(cassette_name, mode="record" if record else "auto")
    cc = create_cassette_client(client, cassette)

    prompts = ["What is the speed of light?", "Name three planets.", "Define entropy."]
    for prompt in prompts:
        response = cc.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        print(f"Response: {response.content[0].text[:60]}")

if __name__ == "__main__":
    print("=== First run (records if no cassette, replays if exists) ===")
    run_with_cassette("demo-cassette")

# Expected Token Savings: Cassette replay costs 0 tokens — all responses served from disk
# Environment: pip install anthropic
```

### Option 4: Async Replay with Diff Comparison

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class AsyncReplayEntry:
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    recorded_at: float = field(default_factory=time.time)
    model: str = ""
    messages: list = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)
    original_response: str = ""
    replayed_responses: list[dict] = field(default_factory=list)

class AsyncReplayStore:
    def __init__(self):
        self._entries: dict[str, AsyncReplayEntry] = {}

    def record(self, model: str, messages: list, response: str, **kwargs) -> str:
        entry = AsyncReplayEntry(model=model, messages=messages,
                                  kwargs=kwargs, original_response=response)
        self._entries[entry.entry_id] = entry
        return entry.entry_id

    async def replay(self, entry_id: str,
                      client: anthropic.AsyncAnthropic,
                      n_times: int = 3) -> dict:
        entry = self._entries.get(entry_id)
        if not entry:
            return {"error": f"Entry {entry_id} not found"}

        async def single_replay() -> dict:
            t0 = time.monotonic()
            r = await client.messages.create(
                model=entry.model,
                messages=entry.messages,
                max_tokens=entry.kwargs.get("max_tokens", 200),
            )
            return {
                "text": r.content[0].text,
                "latency_ms": (time.monotonic() - t0) * 1000,
                "tokens": r.usage.output_tokens,
            }

        print(f"[REPLAY] Running {n_times}× replay of {entry_id}...")
        results = await asyncio.gather(*[single_replay() for _ in range(n_times)])

        texts = [r["text"] for r in results]
        latencies = [r["latency_ms"] for r in results]
        token_counts = [r["tokens"] for r in results]

        # Find diffs from original
        diffs = []
        for i, text in enumerate(texts):
            first_diff = next((j for j, (a, b) in
                               enumerate(zip(entry.original_response, text)) if a != b),
                              min(len(entry.original_response), len(text)))
            diffs.append(first_diff)

        return {
            "entry_id": entry_id,
            "original": entry.original_response[:60],
            "replays": [t[:60] for t in texts],
            "avg_first_diff_char": sum(diffs) / len(diffs),
            "identical_to_original": [t == entry.original_response for t in texts],
            "avg_latency_ms": sum(latencies) / len(latencies),
            "avg_tokens": sum(token_counts) / len(token_counts),
        }

async def main():
    client = anthropic.AsyncAnthropic()
    store = AsyncReplayStore()

    # Record initial calls
    messages_list = [
        [{"role": "user", "content": "What is machine learning?"}],
        [{"role": "user", "content": "Name 3 colors."}],
    ]
    entry_ids = []
    for messages in messages_list:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=messages,
        )
        eid = store.record("claude-haiku-4-5-20251001", messages, r.content[0].text,
                           max_tokens=60)
        entry_ids.append(eid)
        print(f"[RECORDED] {eid}: {r.content[0].text[:50]}")

    # Replay in parallel
    replay_results = await asyncio.gather(*[
        store.replay(eid, client, n_times=2)
        for eid in entry_ids
    ])

    for result in replay_results:
        print(f"\n[REPLAY REPORT] {result['entry_id']}")
        print(f"  Original: {result['original']}")
        for i, rep in enumerate(result["replays"]):
            print(f"  Replay {i+1}: {rep}")
        print(f"  Avg first diff at char: {result['avg_first_diff_char']:.0f}")
        print(f"  Avg latency: {result['avg_latency_ms']:.0f}ms")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — async parallel replay reduces total debug wall-clock time
# Environment: pip install anthropic
```

### Option 5: Production Error Capture with One-Click Replay

```python
import anthropic
import json
import time
import uuid
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

@dataclass
class ErrorCapture:
    capture_id: str
    timestamp: float
    model: str
    messages: list[dict]
    kwargs: dict
    error_type: str
    error_message: str
    error_traceback: str
    user_id: Optional[str] = None
    environment: str = "production"
    replayed: bool = False

class ProductionErrorCapturer:
    def __init__(self, capture_dir: str = "/tmp/error_captures"):
        self._dir = Path(capture_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _save(self, capture: ErrorCapture) -> str:
        path = self._dir / f"{capture.capture_id}.json"
        data = {
            "capture_id": capture.capture_id,
            "timestamp": capture.timestamp,
            "model": capture.model,
            "messages": capture.messages,
            "kwargs": capture.kwargs,
            "error_type": capture.error_type,
            "error_message": capture.error_message,
            "error_traceback": capture.error_traceback,
            "user_id": capture.user_id,
            "environment": capture.environment,
            "replayed": capture.replayed,
        }
        path.write_text(json.dumps(data, indent=2))
        return str(path)

    def _load(self, capture_id: str) -> Optional[ErrorCapture]:
        path = self._dir / f"{capture_id}.json"
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        return ErrorCapture(**d)

    def capture_on_error(self, client: anthropic.Anthropic, model: str,
                          messages: list, user_id: Optional[str] = None,
                          **kwargs) -> str:
        try:
            response = client.messages.create(
                model=model, messages=messages, **kwargs
            )
            return response.content[0].text
        except Exception as e:
            capture = ErrorCapture(
                capture_id=str(uuid.uuid4())[:10],
                timestamp=time.time(),
                model=model, messages=messages,
                kwargs=kwargs,
                error_type=type(e).__name__,
                error_message=str(e),
                error_traceback=traceback.format_exc(),
                user_id=user_id,
            )
            path = self._save(capture)
            print(f"[ERROR CAPTURED] {capture.capture_id} → {path}")
            print(f"  Error: {type(e).__name__}: {str(e)[:60]}")
            raise

    def replay_captured_error(self, capture_id: str,
                               client: anthropic.Anthropic) -> Optional[dict]:
        capture = self._load(capture_id)
        if not capture:
            return None

        print(f"[REPLAY] Capture {capture_id}")
        print(f"  Original error: {capture.error_type}: {capture.error_message[:60]}")
        print(f"  Model: {capture.model}")
        print(f"  User: {capture.user_id or 'unknown'}")
        print(f"  Time: {time.ctime(capture.timestamp)}")

        try:
            response = client.messages.create(
                model=capture.model,
                messages=capture.messages,
                **capture.kwargs,
            )
            result = {
                "status": "success",
                "response": response.content[0].text[:100],
                "note": "Error may have been transient",
            }
        except Exception as e:
            result = {
                "status": "error",
                "error": f"{type(e).__name__}: {str(e)[:60]}",
                "note": "Error reproduced — likely a persistent bug",
            }

        capture.replayed = True
        self._save(capture)
        print(f"  Replay result: {result}")
        return result

    def list_errors(self, limit: int = 10) -> list[dict]:
        files = sorted(self._dir.glob("*.json"),
                       key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
        results = []
        for f in files:
            d = json.loads(f.read_text())
            results.append({"id": d["capture_id"], "error": d["error_type"],
                             "replayed": d["replayed"]})
        return results

if __name__ == "__main__":
    client = anthropic.Anthropic()
    capturer = ProductionErrorCapturer()
    capture_id = None

    # Normal call
    try:
        result = capturer.capture_on_error(
            client, "claude-haiku-4-5-20251001",
            [{"role": "user", "content": "What is the capital of France?"}],
            user_id="user-123", max_tokens=50,
        )
        print(f"Success: {result[:60]}")
    except Exception:
        pass

    # Simulate finding an error ID and replaying it
    errors = capturer.list_errors()
    if errors:
        print(f"\nCaptured errors: {errors}")

# Expected Token Savings: Precise error replay avoids broad re-testing — debug the exact failing case
# Environment: pip install anthropic
```

### Option 6: Multi-Turn Conversation Replay with State Diffing

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ConversationSnapshot:
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    session_id: str = ""
    recorded_at: float = field(default_factory=time.time)
    turns: list[dict] = field(default_factory=list)  # Full conversation history
    model: str = "claude-haiku-4-5-20251001"
    system: Optional[str] = None
    max_tokens: int = 200
    final_response: str = ""
    metadata: dict = field(default_factory=dict)

class ConversationReplayer:
    def __init__(self):
        self._snapshots: dict[str, ConversationSnapshot] = {}

    def record_turn(self, session_id: str, model: str,
                     messages: list[dict], response_text: str,
                     system: Optional[str] = None,
                     max_tokens: int = 200,
                     metadata: Optional[dict] = None) -> str:
        """Record a conversation turn, creating or updating the session snapshot."""
        # Find existing snapshot for this session or create new
        existing = next((s for s in self._snapshots.values()
                          if s.session_id == session_id), None)
        if existing:
            snapshot = existing
            snapshot.turns = messages + [{"role": "assistant", "content": response_text}]
            snapshot.final_response = response_text
        else:
            snapshot = ConversationSnapshot(
                session_id=session_id, model=model, system=system,
                max_tokens=max_tokens,
                turns=messages + [{"role": "assistant", "content": response_text}],
                final_response=response_text, metadata=metadata or {},
            )
            self._snapshots[snapshot.snapshot_id] = snapshot
        return snapshot.snapshot_id

    def replay_from_turn(self, snapshot_id: str, from_turn: int,
                          client: anthropic.Anthropic) -> Optional[list[dict]]:
        """Replay conversation from a specific turn to find where behavior diverged."""
        snapshot = self._snapshots.get(snapshot_id)
        if not snapshot:
            return None

        # Slice turns up to from_turn (user messages only)
        user_turns = [t for t in snapshot.turns if t["role"] == "user"]
        if from_turn >= len(user_turns):
            print(f"[REPLAY] from_turn {from_turn} >= {len(user_turns)} user turns")
            return None

        # Build message history up to the selected user turn
        messages_to_replay = []
        user_count = 0
        for turn in snapshot.turns:
            if turn["role"] == "user":
                user_count += 1
            messages_to_replay.append(turn)
            if user_count > from_turn:
                break
            if turn["role"] == "assistant":
                continue

        # Remove last assistant turn (we'll regenerate it)
        if messages_to_replay and messages_to_replay[-1]["role"] == "assistant":
            messages_to_replay = messages_to_replay[:-1]

        print(f"[REPLAY] Snapshot {snapshot_id} from turn {from_turn}")
        print(f"  Replaying {len(messages_to_replay)} messages")

        kwargs: dict = {"model": snapshot.model, "messages": messages_to_replay,
                        "max_tokens": snapshot.max_tokens}
        if snapshot.system:
            kwargs["system"] = snapshot.system

        response = client.messages.create(**kwargs)
        new_response = response.content[0].text

        # Find original response at this turn
        assistant_turns = [t for t in snapshot.turns if t["role"] == "assistant"]
        original = assistant_turns[from_turn]["content"] if from_turn < len(assistant_turns) else "N/A"

        first_diff = next((i for i, (a, b) in enumerate(zip(original, new_response)) if a != b),
                           min(len(original), len(new_response)))
        print(f"  Original:  {original[:60]}")
        print(f"  Replayed:  {new_response[:60]}")
        print(f"  First diff at char: {first_diff}")
        return [{"turn": from_turn, "original": original, "replayed": new_response,
                  "first_diff": first_diff}]

def run_multi_turn_demo():
    client = anthropic.Anthropic()
    replayer = ConversationReplayer()
    session_id = "session-demo-1"

    # Build a conversation
    messages: list[dict] = []
    turns = [
        "What is photosynthesis?",
        "How does it produce oxygen?",
        "What plants are most efficient at this?",
    ]

    snapshot_id = None
    for prompt in turns:
        messages.append({"role": "user", "content": prompt})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=messages,
        )
        text = response.content[0].text
        messages.append({"role": "assistant", "content": text})
        snapshot_id = replayer.record_turn(session_id, "claude-haiku-4-5-20251001",
                                            messages[:-1], text)
        print(f"[Turn] {prompt[:40]}: {text[:50]}")

    # Replay from turn 1 (second user message)
    if snapshot_id:
        print(f"\nReplaying from turn 1 (snapshot {snapshot_id}):")
        replayer.replay_from_turn(snapshot_id, from_turn=1, client=client)

if __name__ == "__main__":
    run_multi_turn_demo()

# Expected Token Savings: Turn-level replay narrows debug scope — only replay from suspect turn
# Environment: pip install anthropic
```

## Comparison

| Option | Storage | Replay Scope | Diff Support | Best For |
|--------|---------|-------------|-------------|----------|
| 1. File-based logger | JSONL files | Single request | Prefix match | Simple debugging |
| 2. SQLite store | Relational DB | Query by session | Char diff | Production systems |
| 3. Cassette (VCR-style) | JSON file | Hash or sequential | No | Unit test fixtures |
| 4. Async parallel | In-memory | Single request | Char diff | Async debugging |
| 5. Error capture | JSON files | Error-only | Error type | Production failures |
| 6. Multi-turn | In-memory | Turn-level slice | Char diff | Conversation bugs |
