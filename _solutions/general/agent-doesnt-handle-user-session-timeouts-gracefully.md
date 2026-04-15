---
layout: solution
title: "Agent Doesn't Handle User Session Timeouts Gracefully"
category: general
description: "Agent loses conversation context when a user session expires, forcing users to repeat themselves, or crashes with unhandled exceptions when resuming stale sessions."
tags: [general, session-management, persistence, recovery, user-experience]
---

## Symptom

A user returns after a break to find one of several failure modes:

- **Context wipe**: Agent has no memory of the conversation and asks the user to start over
- **Stale tool credentials**: Stored auth tokens have expired; tool calls crash with 401 errors
- **Mid-task interruption**: The agent was generating a long response when the session timed out; the user gets a truncated result or error on return
- **Loop on resume**: Agent tries to continue a task that can no longer proceed (e.g., an upload that timed out), enters a confused state

```python
# BROKEN: session context lives only in memory
class Agent:
    def __init__(self):
        self.history = []   # lost when server restarts or session expires

    def chat(self, message: str) -> str:
        self.history.append({"role": "user", "content": message})
        # ... API call ...
        # User comes back 2 hours later — self.history is empty
```

Root causes:
- Conversation history stored in memory (process-local, not persisted)
- No session expiry detection
- Tool credentials not refreshed on resume
- No checkpoint for long-running tasks
- User gets raw exception instead of a graceful recovery message

---

## Root Cause

Most agent implementations store conversation state in process memory. This is fine for short sessions but fails when:
1. The server process restarts (deployment, crash)
2. The user is idle past an inactivity timeout (load balancer, reverse proxy)
3. A long-running task spans multiple connection windows

Graceful timeout handling requires: persistent session storage, expiry detection, credential refresh, and a recovery flow that either restores context or explains the gap to the user.

---

## Fix

### Option 1 — SQLite-Backed Session Persistence

Persist conversation history to disk so sessions survive restarts and timeouts.

```python
import anthropic
import sqlite3
import json
import time
import uuid
from typing import Optional

client = anthropic.Anthropic()

class PersistentSession:
    """SQLite-backed conversation session that survives restarts."""

    SESSION_TTL_HOURS = 24

    def __init__(self, db_path: str = "sessions.db"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._setup()

    def _setup(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                history TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                last_active REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_user ON sessions(user_id);
        """)
        self.db.commit()

    def create(self, user_id: str, metadata: dict = None) -> str:
        session_id = str(uuid.uuid4())
        now = time.time()
        self.db.execute(
            "INSERT INTO sessions (session_id, user_id, history, created_at, last_active, metadata) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, user_id, "[]", now, now, json.dumps(metadata or {}))
        )
        self.db.commit()
        return session_id

    def load(self, session_id: str) -> Optional[dict]:
        """Load a session. Returns None if expired or not found."""
        row = self.db.execute(
            "SELECT history, last_active, metadata FROM sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()

        if not row:
            return None

        history, last_active, metadata = row
        age_hours = (time.time() - last_active) / 3600

        if age_hours > self.SESSION_TTL_HOURS:
            # Session expired — mark it but don't delete (allow partial recovery)
            return {"expired": True, "age_hours": age_hours, "history": json.loads(history)}

        return {
            "expired": False,
            "history": json.loads(history),
            "metadata": json.loads(metadata),
            "age_hours": age_hours,
        }

    def append(self, session_id: str, role: str, content: str):
        """Append a message to session history atomically."""
        row = self.db.execute(
            "SELECT history FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()

        if row:
            history = json.loads(row[0])
            history.append({"role": role, "content": content})
            self.db.execute(
                "UPDATE sessions SET history=?, last_active=? WHERE session_id=?",
                (json.dumps(history), time.time(), session_id)
            )
            self.db.commit()

    def touch(self, session_id: str):
        """Update last_active timestamp to prevent expiry during active use."""
        self.db.execute(
            "UPDATE sessions SET last_active=? WHERE session_id=?",
            (time.time(), session_id)
        )
        self.db.commit()

SESSIONS = PersistentSession()
SYSTEM = "You are a helpful assistant with persistent memory across sessions."

def resume_or_create(session_id: Optional[str], user_id: str) -> tuple[str, str]:
    """Resume an existing session or create a new one. Returns (session_id, welcome_msg)."""
    if session_id:
        session = SESSIONS.load(session_id)
        if session is None:
            # Session not found
            new_id = SESSIONS.create(user_id)
            return new_id, "I couldn't find your previous session. Starting fresh — how can I help?"

        if session.get("expired"):
            age_h = session["age_hours"]
            # Session expired but we have history — offer partial recovery
            new_id = SESSIONS.create(user_id)
            turn_count = len(session.get("history", []))
            return new_id, (
                f"Welcome back! Your previous session expired after {age_h:.0f} hours of inactivity "
                f"({turn_count // 2} exchanges). I've started a fresh session. "
                "Would you like me to pick up where we left off? Briefly summarize what we were working on."
            )

        # Valid session — resume silently
        return session_id, ""

    # New session
    new_id = SESSIONS.create(user_id)
    return new_id, ""

def chat(session_id: Optional[str], user_id: str, message: str) -> tuple[str, str]:
    """
    Chat with persistent session. Returns (response, active_session_id).
    """
    active_id, welcome = resume_or_create(session_id, user_id)

    if welcome:
        print(f"  [session resume] {welcome[:80]}")

    session = SESSIONS.load(active_id)
    history = session.get("history", []) if session and not session.get("expired") else []

    history.append({"role": "user", "content": message})
    SESSIONS.append(active_id, "user", message)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        messages=history,
    )
    reply = response.content[0].text

    SESSIONS.append(active_id, "assistant", reply)
    SESSIONS.touch(active_id)

    return reply, active_id

# Demo: simulate session across multiple "connections"
session_id = None

print("=== First session ===")
reply, session_id = chat(session_id, "user_123", "My name is Alice and I'm building a FastAPI app")
print(f"Agent: {reply[:150]}")

reply, session_id = chat(session_id, "user_123", "I need help with authentication")
print(f"Agent: {reply[:150]}")
print(f"Session ID: {session_id}")

print("\n=== Simulating disconnect/timeout — resuming ===")
reply, resumed_id = chat(session_id, "user_123", "What was I working on?")
print(f"Agent: {reply[:150]}")
print(f"Same session: {resumed_id == session_id}")
```

**Expected Token Savings:** Session persistence eliminates re-explanation overhead. Users who would re-explain 5-10 turns of context (~2,000-5,000 tokens) now resume seamlessly.

**Environment:** Python 3.9+, `sqlite3`, `anthropic>=0.40.0`.

---

### Option 2 — Token Refresh on Session Resume

Detect expired credentials and refresh them automatically before resuming tool-using sessions.

```python
import anthropic
import time
import json
from dataclasses import dataclass, field
from typing import Optional, Callable

client = anthropic.Anthropic()

@dataclass
class CredentialStore:
    """Manages potentially-expiring credentials for tool calls."""
    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp
    refresh_fn: Callable = None

    def is_expired(self, buffer_seconds: int = 60) -> bool:
        """Return True if token expires within buffer_seconds."""
        return time.time() + buffer_seconds >= self.expires_at

    def refresh(self) -> bool:
        """Attempt to refresh the access token. Returns success."""
        if self.refresh_fn is None:
            return False
        try:
            new_token, new_expiry = self.refresh_fn(self.refresh_token)
            self.access_token = new_token
            self.expires_at = new_expiry
            print(f"  [credentials] Refreshed. New expiry in {(new_expiry - time.time()):.0f}s")
            return True
        except Exception as e:
            print(f"  [credentials] Refresh failed: {e}")
            return False

def mock_token_refresh(refresh_token: str) -> tuple[str, float]:
    """Simulate a token refresh endpoint."""
    new_token = f"access_token_{int(time.time())}"
    expires_in = 3600  # 1 hour
    return new_token, time.time() + expires_in

class SessionWithCredentials:
    """Conversation session that auto-refreshes credentials on resume."""

    def __init__(self, session_id: str, history: list[dict], credentials: CredentialStore):
        self.session_id = session_id
        self.history = history
        self.credentials = credentials
        self.last_active = time.time()

    def is_stale(self, stale_after_seconds: int = 1800) -> bool:
        """Return True if session hasn't been used for stale_after_seconds."""
        return (time.time() - self.last_active) > stale_after_seconds

    def resume(self) -> str:
        """
        Prepare for resumption. Refresh credentials if needed.
        Returns a status message for the agent.
        """
        status_parts = []

        if self.is_stale():
            idle_minutes = (time.time() - self.last_active) / 60
            status_parts.append(f"Session was idle for {idle_minutes:.0f} minutes.")

        if self.credentials.is_expired():
            print("  [session] Credentials expired — attempting refresh...")
            if self.credentials.refresh():
                status_parts.append("API credentials were refreshed successfully.")
            else:
                status_parts.append(
                    "WARNING: API credentials could not be refreshed. "
                    "Some tool calls may fail. Please re-authenticate if needed."
                )

        self.last_active = time.time()
        return " ".join(status_parts) if status_parts else ""

def make_tool_call(credentials: CredentialStore, endpoint: str, params: dict) -> dict:
    """Make a tool call, checking credential freshness first."""
    if credentials.is_expired():
        if not credentials.refresh():
            return {"error": "Authentication required. Please re-authenticate."}

    # Simulate API call with current token
    print(f"  [tool] Calling {endpoint} with token {credentials.access_token[:20]}...")
    return {"endpoint": endpoint, "result": "data", "params": params}

# Setup
creds = CredentialStore(
    access_token="initial_token_abc",
    refresh_token="refresh_token_xyz",
    expires_at=time.time() + 10,  # expires in 10s for demo
    refresh_fn=mock_token_refresh,
)

session = SessionWithCredentials(
    session_id="sess_001",
    history=[
        {"role": "user", "content": "Help me fetch my user data"},
        {"role": "assistant", "content": "I'll fetch your user data now..."},
    ],
    credentials=creds,
)

# Simulate returning after a gap
print("=== User returns after timeout ===")
time.sleep(2)  # simulate time passing; token near expiry

resume_status = session.resume()
if resume_status:
    print(f"Resume status: {resume_status}")

# Tool call after resume — credentials refreshed automatically
result = make_tool_call(creds, "/api/users/me", {})
print(f"Tool result: {result}")

# Continue conversation
tools = [{"name": "get_user_data", "description": "Fetch user profile",
           "input_schema": {"type": "object", "properties": {}}}]

SYSTEM = "You are a helpful API assistant. If credentials were refreshed, mention it naturally."

messages = session.history.copy()
if resume_status:
    messages.append({
        "role": "user",
        "content": f"[System: {resume_status}] What did we accomplish before the break?"
    })
else:
    messages.append({"role": "user", "content": "Continue from where we left off"})

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=256,
    system=SYSTEM,
    messages=messages,
)
print(f"\nAgent: {response.content[0].text[:200]}")
```

**Expected Token Savings:** Credential auto-refresh prevents 401-triggered retry loops. Each failed auth cycle wastes 2-4 turns (~1,000 tokens).

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 3 — Checkpoint/Resume for Long-Running Tasks

Save task progress at checkpoints so long tasks can resume from the last successful step.

```python
import anthropic
import json
import time
import sqlite3
from dataclasses import dataclass, asdict
from typing import Optional, Any

client = anthropic.Anthropic()

@dataclass
class TaskCheckpoint:
    task_id: str
    task_type: str
    total_steps: int
    completed_steps: list[str]
    current_step: int
    step_results: dict[str, Any]
    started_at: float
    last_checkpoint: float
    status: str  # running, paused, complete, failed

class CheckpointStore:
    def __init__(self, db_path: str = ":memory:"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        self.db.commit()

    def save(self, checkpoint: TaskCheckpoint):
        self.db.execute(
            "INSERT OR REPLACE INTO checkpoints (task_id, data, updated_at) VALUES (?,?,?)",
            (checkpoint.task_id, json.dumps(asdict(checkpoint)), time.time())
        )
        self.db.commit()
        print(f"  [checkpoint] Saved: step {checkpoint.current_step}/{checkpoint.total_steps}")

    def load(self, task_id: str) -> Optional[TaskCheckpoint]:
        row = self.db.execute(
            "SELECT data FROM checkpoints WHERE task_id=?", (task_id,)
        ).fetchone()
        if row:
            data = json.loads(row[0])
            return TaskCheckpoint(**data)
        return None

CHECKPOINT_STORE = CheckpointStore()

async def resumable_task(
    task_id: str,
    steps: list[dict],
    resume_from_checkpoint: bool = True,
) -> dict:
    """
    Execute a multi-step task with checkpoint/resume capability.
    If interrupted, can resume from the last saved checkpoint.
    """
    # Try to load existing checkpoint
    checkpoint = None
    if resume_from_checkpoint:
        checkpoint = CHECKPOINT_STORE.load(task_id)

    if checkpoint and checkpoint.status == "running":
        resume_step = checkpoint.current_step
        print(f"  Resuming task from step {resume_step}/{checkpoint.total_steps}")
        completed = set(checkpoint.completed_steps)
        results = checkpoint.step_results
    else:
        # Fresh start
        resume_step = 0
        completed = set()
        results = {}
        checkpoint = TaskCheckpoint(
            task_id=task_id,
            task_type="multi_step",
            total_steps=len(steps),
            completed_steps=[],
            current_step=0,
            step_results={},
            started_at=time.time(),
            last_checkpoint=time.time(),
            status="running",
        )

    for i, step in enumerate(steps):
        if i < resume_step:
            continue  # skip already-completed steps

        step_name = step["name"]
        print(f"  Executing step {i+1}/{len(steps)}: {step_name}")

        try:
            # Simulate step execution
            time.sleep(0.1)
            step_result = {"step": step_name, "output": f"Result of {step_name}", "ok": True}
            results[step_name] = step_result
            completed.add(step_name)

            # Save checkpoint after each step
            checkpoint.current_step = i + 1
            checkpoint.completed_steps = list(completed)
            checkpoint.step_results = results
            checkpoint.last_checkpoint = time.time()
            CHECKPOINT_STORE.save(checkpoint)

        except Exception as e:
            print(f"  Step {step_name} failed: {e}")
            checkpoint.status = "paused"
            CHECKPOINT_STORE.save(checkpoint)
            return {"status": "paused", "failed_at": step_name, "resume_id": task_id}

    checkpoint.status = "complete"
    CHECKPOINT_STORE.save(checkpoint)
    return {"status": "complete", "results": results, "steps_completed": len(completed)}

def generate_with_task_checkpoint(user_request: str, task_id: str = None) -> str:
    """Agent that saves checkpoints during long tasks."""
    import asyncio

    if not task_id:
        import uuid
        task_id = str(uuid.uuid4())

    # Check if resuming
    existing = CHECKPOINT_STORE.load(task_id)
    if existing and existing.status == "running":
        resume_msg = (
            f"Found a paused task ({existing.current_step}/{existing.total_steps} steps done). "
            f"Resuming from step {existing.current_step + 1}."
        )
    else:
        resume_msg = "Starting fresh task."

    # Simulated multi-step task
    steps = [
        {"name": "parse_requirements"},
        {"name": "design_schema"},
        {"name": "generate_code"},
        {"name": "write_tests"},
        {"name": "generate_docs"},
    ]

    print(f"\n{resume_msg}")
    result = asyncio.run(resumable_task(task_id, steps))

    if result["status"] == "complete":
        # Generate final response using all results
        completed_summary = ", ".join(result["results"].keys())
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": (
                    f"Task complete. Completed steps: {completed_summary}. "
                    f"Summarize what was accomplished for: {user_request}"
                )
            }]
        )
        return response.content[0].text

    return f"Task paused at step {result.get('failed_at')}. Resume with task_id={task_id}"

# Demo: start a task
task_id = "task_demo_001"
print("=== Starting long task ===")
result = generate_with_task_checkpoint("Build a user auth module", task_id)
print(f"\nResult: {result[:200]}")

print("\n=== Simulating resume after timeout ===")
result2 = generate_with_task_checkpoint("Continue building user auth module", task_id)
print(f"Resume result: {result2[:200]}")
```

**Expected Token Savings:** Checkpointing allows skipping already-completed steps on resume, saving proportional token costs (e.g., 3/5 steps done = 60% of generation cost saved on resume).

**Environment:** Python 3.9+, `asyncio`, `sqlite3`, `anthropic>=0.40.0`.

---

### Option 4 — Graceful Degradation with Context Summary

When full history can't be restored, generate a compact context summary to maintain continuity.

```python
import anthropic
import json

client = anthropic.Anthropic()

def summarize_session(history: list[dict], goal: str = "") -> str:
    """Create a compact context summary for session recovery."""
    if not history:
        return ""

    history_text = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {str(m['content'])[:200]}"
        for m in history[-20:]  # last 20 messages max
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="Summarize this conversation for session recovery. Be concise.",
        messages=[{
            "role": "user",
            "content": (
                f"Conversation to summarize:\n{history_text}\n\n"
                "Create a 3-5 sentence summary covering:\n"
                "1. What was the user's main goal?\n"
                "2. What was accomplished?\n"
                "3. What was the last thing discussed?\n"
                "4. What likely comes next?"
            )
        }]
    )
    return response.content[0].text

def recover_session(
    partial_history: list[dict] | None,
    user_message: str,
    system_prompt: str,
) -> str:
    """
    Attempt to continue a conversation despite session loss.
    Degrades gracefully: full history → summary → fresh start.
    """
    if partial_history and len(partial_history) > 0:
        # Have some history — summarize and continue
        summary = summarize_session(partial_history)
        print(f"  [recovery] Using summarized context: {summary[:80]}...")

        recovery_system = (
            f"{system_prompt}\n\n"
            f"## Session Context (recovered from previous session)\n{summary}\n\n"
            "The user is continuing from a previous session. "
            "Acknowledge the context naturally if relevant."
        )
        messages = [{"role": "user", "content": user_message}]
    else:
        # No history at all — fresh start with acknowledgment
        print("  [recovery] No history available — fresh start")
        recovery_system = system_prompt
        messages = [{
            "role": "user",
            "content": (
                f"{user_message}\n\n"
                "(Note: I may have been working on something before — "
                "if context is unclear, please ask me what we were doing.)"
            )
        }]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=recovery_system,
        messages=messages,
    )
    return response.content[0].text

# Simulate partial history recovery
partial_history = [
    {"role": "user", "content": "I'm building a REST API with FastAPI"},
    {"role": "assistant", "content": "Great! Let's start with the project structure..."},
    {"role": "user", "content": "Show me the database models first"},
    {"role": "assistant", "content": "Here are the SQLAlchemy models:\n```python\nclass User(Base):\n    id = Column(Integer, primary_key=True)\n    email = Column(String, unique=True)\n```"},
    {"role": "user", "content": "What about the auth endpoints?"},
    {"role": "assistant", "content": "For auth endpoints, we'll create /register, /login, and /refresh..."},
]

SYSTEM = "You are a Python backend development assistant."

print("=== Recovery with partial history ===")
reply = recover_session(
    partial_history,
    "Sorry I had to leave — where were we?",
    SYSTEM,
)
print(f"Agent: {reply[:300]}")

print("\n=== Recovery with no history ===")
reply2 = recover_session(None, "I need to continue working on my API", SYSTEM)
print(f"Agent: {reply2[:200]}")
```

**Expected Token Savings:** Haiku summary (~150 tokens) replaces full history re-injection (~2,000-5,000 tokens). 90%+ token savings on context recovery.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 5 — Idle Detection with Proactive Save

Detect idle periods and proactively save state before a timeout would occur.

```python
import anthropic
import json
import time
import threading
from typing import Optional, Callable

client = anthropic.Anthropic()

class IdleAwareSession:
    """
    Session that detects idle periods and saves state proactively.
    Calls a save callback when idle threshold is approached.
    """

    def __init__(
        self,
        session_id: str,
        save_fn: Callable,
        idle_warning_seconds: int = 300,  # warn at 5 minutes
        idle_save_seconds: int = 600,     # save at 10 minutes
    ):
        self.session_id = session_id
        self.save_fn = save_fn
        self.idle_warning = idle_warning_seconds
        self.idle_save = idle_save_seconds
        self.history: list[dict] = []
        self.last_activity = time.monotonic()
        self.saved = False
        self._lock = threading.Lock()
        self._monitor_thread = threading.Thread(target=self._monitor_idle, daemon=True)
        self._monitor_thread.start()

    def _monitor_idle(self):
        while True:
            time.sleep(30)
            with self._lock:
                idle_time = time.monotonic() - self.last_activity
                if idle_time >= self.idle_save and not self.saved:
                    print(f"  [idle monitor] {idle_time:.0f}s idle — saving session state")
                    self.save_fn(self.session_id, self.history)
                    self.saved = True
                elif idle_time >= self.idle_warning and not self.saved:
                    print(f"  [idle monitor] Warning: {idle_time:.0f}s idle — will save soon")

    def touch(self):
        with self._lock:
            self.last_activity = time.monotonic()
            self.saved = False  # reset save flag on new activity

    def add_message(self, role: str, content: str):
        with self._lock:
            self.history.append({"role": role, "content": content})
        self.touch()

    def save_now(self):
        """Immediately save current state."""
        with self._lock:
            self.save_fn(self.session_id, self.history)
            self.saved = True
            print(f"  [session] Saved {len(self.history)} messages")

# Simulated save backend (in production: database)
_saved_sessions: dict[str, list] = {}

def save_to_store(session_id: str, history: list):
    _saved_sessions[session_id] = history.copy()

def load_from_store(session_id: str) -> Optional[list]:
    return _saved_sessions.get(session_id)

# Usage
session = IdleAwareSession(
    session_id="user_abc_session",
    save_fn=save_to_store,
    idle_warning_seconds=5,   # short for demo
    idle_save_seconds=10,
)

SYSTEM = "You are a helpful coding assistant."

def chat_with_idle_awareness(message: str) -> str:
    session.add_message("user", message)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=SYSTEM,
        messages=session.history,
    )
    reply = response.content[0].text
    session.add_message("assistant", reply)

    # Proactively save after each exchange (belt-and-suspenders)
    session.save_now()
    return reply

# Demo conversation
print("Starting session...")
r1 = chat_with_idle_awareness("Help me write a Python decorator for caching")
print(f"Agent: {r1[:150]}\n")

r2 = chat_with_idle_awareness("Make it handle async functions too")
print(f"Agent: {r2[:150]}\n")

# Simulate user going idle
print("User is idle... (idle monitor would trigger in 10s)")
time.sleep(1)  # short wait for demo

# Check if state was saved
saved = load_from_store("user_abc_session")
print(f"Session saved: {bool(saved)} ({len(saved) if saved else 0} messages)")
```

**Expected Token Savings:** Proactive saving has no token cost — it prevents loss of accumulated context that would cost 1,000-5,000 tokens to re-establish.

**Environment:** Python 3.9+, `threading`, `anthropic>=0.40.0`.

---

### Option 6 — User-Facing Timeout Notification with Resume Link

Notify users before timeout and provide a one-click resume path.

```python
import anthropic
import json
import time
import uuid

client = anthropic.Anthropic()

# Session store (in production: Redis with TTL)
_sessions: dict[str, dict] = {}

def create_resume_token(session_id: str, history: list[dict]) -> str:
    """Create a shareable resume token that encodes session state."""
    token = str(uuid.uuid4()).replace("-", "")[:16]
    _sessions[token] = {
        "session_id": session_id,
        "history": history,
        "created_at": time.time(),
        "ttl_hours": 48,
    }
    return token

def load_from_resume_token(token: str) -> dict | None:
    """Load session state from a resume token."""
    data = _sessions.get(token)
    if not data:
        return None
    age_hours = (time.time() - data["created_at"]) / 3600
    if age_hours > data["ttl_hours"]:
        del _sessions[token]
        return None
    return data

def generate_timeout_warning(session_id: str, history: list[dict], idle_minutes: int) -> str:
    """Generate a user-friendly timeout warning message."""
    resume_token = create_resume_token(session_id, history)
    turn_count = len(history) // 2

    # Generate a brief summary of what was accomplished
    if history:
        last_user = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"),
            "the conversation"
        )
        context_hint = f"Last discussed: {str(last_user)[:80]}..."
    else:
        context_hint = "No conversation history to summarize."

    return (
        f"⏰ Your session has been idle for {idle_minutes} minutes.\n\n"
        f"Progress saved: {turn_count} exchanges, {context_hint}\n\n"
        f"To resume: use resume code **{resume_token}**\n"
        f"(Valid for 48 hours)\n\n"
        f"Or start a new session — I'll acknowledge the gap."
    )

def resume_from_token(token: str, new_message: str) -> tuple[str, bool]:
    """
    Resume a session using a resume token.
    Returns (response, was_resumed).
    """
    data = load_from_resume_token(token)

    if not data:
        return (
            "I couldn't find that resume token — it may have expired. "
            "Could you briefly remind me what we were working on?",
            False
        )

    history = data["history"]
    session_id = data["session_id"]
    age_hours = (time.time() - data["created_at"]) / 3600

    print(f"  [resume] Restored {len(history)} messages, {age_hours:.1f}h old")

    # Build a recovery context
    summary_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": (
                f"In 2 sentences, summarize this conversation:\n"
                + "\n".join(f"{m['role']}: {str(m['content'])[:100]}" for m in history[-6:])
            )
        }]
    )
    summary = summary_response.content[0].text

    # Continue with recovered context
    recovery_history = history + [{"role": "user", "content": new_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=(
            f"You are resuming a conversation after {age_hours:.0f} hours. "
            f"Context: {summary} "
            "Acknowledge the resume naturally and continue helping."
        ),
        messages=recovery_history,
    )
    return response.content[0].text, True

# Demo
history = [
    {"role": "user", "content": "Help me optimize my PostgreSQL queries"},
    {"role": "assistant", "content": "I'll help optimize your queries. Let's start with EXPLAIN ANALYZE..."},
    {"role": "user", "content": "The users table has 10M rows and queries are slow"},
    {"role": "assistant", "content": "For 10M rows, we need proper indexing. First, let's check existing indexes..."},
]

session_id = "sess_user_abc"

# Simulate timeout warning
warning = generate_timeout_warning(session_id, history, idle_minutes=30)
print("=== Timeout Warning Sent to User ===")
print(warning)

# Extract resume token from warning
token = [w for w in warning.split() if len(w) == 16 and w.replace("*", "").isalnum()]
if token:
    resume_token = token[0].strip("*")
    print(f"\n=== User resumes with token: {resume_token} ===")
    response, resumed = resume_from_token(resume_token, "OK I'm back. What should we do next?")
    print(f"Agent: {response[:250]}")
    print(f"Successfully resumed: {resumed}")
```

**Expected Token Savings:** Resume tokens allow users to return without re-explaining context. Prevents 5-15 minutes of re-orientation per session (estimated 2,000-8,000 tokens).

**Environment:** Python 3.9+, `uuid`, `anthropic>=0.40.0`.

---

## Comparison

| Option | Persistence | Credential Refresh | Long Task Support | User-Facing |
|--------|------------|-------------------|------------------|-------------|
| 1 — SQLite Persistence | Full history | No | No | No |
| 2 — Token Refresh | In-memory | Yes | No | No |
| 3 — Checkpoint/Resume | Step-level | No | Yes | No |
| 4 — Context Summary | Summarized | No | No | No |
| 5 — Idle Detection | Full history | No | No | Partial |
| 6 — Resume Token | Full history | No | No | Yes |

**Start with Option 1** (SQLite persistence) for any production agent — it's the foundational fix. Add **Option 2** (credential refresh) if your agent uses OAuth or short-lived tokens. Use **Option 3** (checkpointing) for long-running generation tasks. Add **Option 6** (resume tokens) for user-facing applications where abandonment rates matter.
