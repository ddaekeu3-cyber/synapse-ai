---
layout: solution
title: "Agent Confuses User Context Across Sessions"
category: memory
description: "Shared conversation state leaks between users or sessions, causing the agent to respond with another user's preferences, history, or personal data."
tags: [memory, session-isolation, security, multi-tenant, data-privacy]
---

## Symptom

User B receives a response that references User A's name, preferences, or earlier conversation. A module-level history list accumulates messages from multiple sessions. After a server restart the agent greets a new user with the previous user's name. In multi-tenant deployments, one tenant's context bleeds into another tenant's responses.

## Root Cause

The simplest agent implementations store conversation history in a module-level variable, a class attribute shared across instances, or a global dict without per-user namespacing. When multiple users hit the same process — or the same session object is reused across requests — their messages are interleaved into a single history. The model then responds as if all those messages came from one person.

## Fix

### Option 1 — Per-request history: no state between calls

```python
import anthropic

client = anthropic.Anthropic()

def handle_request(user_id: str, message: str, history: list[dict] | None = None) -> tuple[str, list[dict]]:
    """
    Stateless handler: caller owns and passes the history.
    No module-level state — impossible to leak between users.
    """
    history = list(history or [])   # defensive copy
    history.append({"role": "user", "content": message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are helping user {user_id}. Stay focused on their specific request.",
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply, history

# Each user owns their own history object — never shared
user_a_history: list[dict] = []
user_b_history: list[dict] = []

reply_a, user_a_history = handle_request("alice", "My name is Alice.", user_a_history)
reply_b, user_b_history = handle_request("bob",   "I prefer dark mode.", user_b_history)

reply_a2, user_a_history = handle_request("alice", "What's my name?", user_a_history)
reply_b2, user_b_history = handle_request("bob",   "What's my UI preference?", user_b_history)

print(f"Alice: {reply_a2}")  # should say Alice
print(f"Bob:   {reply_b2}")  # should say dark mode
```

**Expected Token Savings:** No cross-session pollution means no incorrect answers requiring correction turns.
**Environment:** Stateless API handlers (FastAPI, Flask, Lambda); the simplest and most secure approach.

---

### Option 2 — Session store keyed by session_id

```python
import uuid
import anthropic

client = anthropic.Anthropic()

# Thread-safe in-memory session store
# In production: replace with Redis or a database
_sessions: dict[str, list[dict]] = {}

def get_or_create_session(session_id: str) -> list[dict]:
    if session_id not in _sessions:
        _sessions[session_id] = []
    return _sessions[session_id]

def chat(session_id: str, message: str) -> str:
    history = get_or_create_session(session_id)
    history.append({"role": "user", "content": message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply

def end_session(session_id: str) -> None:
    _sessions.pop(session_id, None)

# Simulate two concurrent sessions
session_a = str(uuid.uuid4())
session_b = str(uuid.uuid4())

print(chat(session_a, "My favourite colour is blue."))
print(chat(session_b, "My favourite colour is red."))
print(chat(session_a, "What's my favourite colour?"))  # must say blue
print(chat(session_b, "What's my favourite colour?"))  # must say red

end_session(session_a)
end_session(session_b)
print(f"Active sessions: {len(_sessions)}")
```

**Expected Token Savings:** Isolated histories prevent incorrect answers from leaked context; session cleanup prevents unbounded memory growth.
**Environment:** Web services, chatbots, any multi-user server process; session_id comes from the HTTP request header or cookie.

---

### Option 3 — User-scoped dataclass: explicit ownership

```python
import dataclasses
import anthropic

client = anthropic.Anthropic()

@dataclasses.dataclass
class UserSession:
    user_id:  str
    history:  list[dict]       = dataclasses.field(default_factory=list)
    metadata: dict             = dataclasses.field(default_factory=dict)

    def add_message(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})

    def system_prompt(self) -> str:
        prefs = self.metadata.get("preferences", {})
        return (
            f"You are assisting {self.user_id}. "
            + (f"Known preferences: {prefs}. " if prefs else "")
            + "Do not reference any other user's information."
        )

    def chat(self, message: str) -> str:
        self.add_message("user", message)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=self.system_prompt(),
            messages=self.history,
        )
        reply = response.content[0].text
        self.add_message("assistant", reply)
        return reply

# Each user gets their own session object — shared state is impossible
alice = UserSession(user_id="alice", metadata={"preferences": {"language": "Python", "style": "concise"}})
bob   = UserSession(user_id="bob",   metadata={"preferences": {"language": "Go",     "style": "verbose"}})

print(alice.chat("What language should I use?"))
print(bob.chat("What language should I use?"))
print(alice.chat("And what style do you recommend for me?"))
print(bob.chat("And what style do you recommend for me?"))
```

**Expected Token Savings:** Structured session objects make accidental sharing a type error; metadata-rich system prompts personalise responses without cross-user bleed.
**Environment:** Object-oriented services; dataclass makes session isolation explicit and auditable.

---

### Option 4 — Redis-backed session store with TTL

```python
import json
import anthropic

client = anthropic.Anthropic()

# Simulated Redis interface (replace with redis.Redis in production)
class FakeRedis:
    def __init__(self):
        self._store: dict[str, tuple[str, float]] = {}
        import time
        self._time = time.time

    def get(self, key: str) -> str | None:
        import time
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at < time.time():
            del self._store[key]
            return None
        return value

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        import time
        self._store[key] = (value, time.time() + ttl_seconds)

redis = FakeRedis()

SESSION_TTL = 3600  # 1 hour; inactive sessions expire automatically

def load_history(session_id: str) -> list[dict]:
    raw = redis.get(f"session:{session_id}")
    return json.loads(raw) if raw else []

def save_history(session_id: str, history: list[dict]) -> None:
    redis.setex(f"session:{session_id}", SESSION_TTL, json.dumps(history))

def chat(session_id: str, message: str) -> str:
    history = load_history(session_id)
    history.append({"role": "user", "content": message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    save_history(session_id, history)
    return reply

# Survives process restarts; each session_id is completely isolated
print(chat("sess-alice-001", "I'm working on a machine learning project."))
print(chat("sess-bob-001",   "I'm writing a Rust CLI tool."))
print(chat("sess-alice-001", "What project am I working on?"))  # ML project
print(chat("sess-bob-001",   "What am I building?"))            # Rust CLI
```

**Expected Token Savings:** TTL auto-expires old sessions; no manual cleanup code needed; prevents unbounded session accumulation.
**Environment:** Horizontally-scaled services (multiple API server replicas); Redis is the standard shared-session store for distributed systems.

---

### Option 5 — Middleware that injects and validates session_id

```python
import uuid
import hashlib
import anthropic

client = anthropic.Anthropic()

_sessions: dict[str, dict] = {}

class SessionMiddleware:
    """Validates that each request carries a legitimate session_id."""

    @staticmethod
    def create_session(user_id: str) -> str:
        session_id = hashlib.sha256(f"{user_id}:{uuid.uuid4()}".encode()).hexdigest()[:32]
        _sessions[session_id] = {"user_id": user_id, "history": []}
        return session_id

    @staticmethod
    def get_session(session_id: str) -> dict | None:
        return _sessions.get(session_id)

    @staticmethod
    def validate(session_id: str, claimed_user_id: str) -> bool:
        """Prevent session hijacking: session_id must match the claimed user."""
        session = _sessions.get(session_id)
        return session is not None and session["user_id"] == claimed_user_id

middleware = SessionMiddleware()

def handle_chat(session_id: str, user_id: str, message: str) -> str:
    # Validate session ownership before processing
    if not middleware.validate(session_id, user_id):
        raise PermissionError(f"Session {session_id!r} does not belong to user {user_id!r}")

    session = middleware.get_session(session_id)
    history = session["history"]
    history.append({"role": "user", "content": message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply

# Normal flow
sid_alice = middleware.create_session("alice")
sid_bob   = middleware.create_session("bob")

print(handle_chat(sid_alice, "alice", "My name is Alice."))
print(handle_chat(sid_bob,   "bob",   "My name is Bob."))
print(handle_chat(sid_alice, "alice", "What's my name?"))

# Attempt to use Alice's session as Bob — blocked
try:
    handle_chat(sid_alice, "bob", "What's my name?")
except PermissionError as e:
    print(f"[security] {e}")
```

**Expected Token Savings:** Prevents context injection attacks where a malicious user provides another user's session_id; security error is cheaper than a data breach.
**Environment:** Public-facing multi-user APIs; session validation should be the first check in every request handler.

---

### Option 6 — Audit log: detect and alert on cross-session anomalies

```python
import re
import anthropic

client = anthropic.Anthropic()

_sessions: dict[str, dict] = {}
_audit_log: list[dict]     = []

# Known user-specific markers to watch for in cross-session contexts
def extract_user_markers(text: str) -> set[str]:
    """Extract names, emails, and IDs that could identify a specific user."""
    names  = re.findall(r"\b[A-Z][a-z]{2,}\b", text)
    emails = re.findall(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", text)
    return set(names + emails)

def audit_response(session_id: str, user_id: str, response_text: str) -> None:
    session  = _sessions.get(session_id, {})
    expected = session.get("known_markers", set())
    found    = extract_user_markers(response_text)
    leaked   = found - expected

    if leaked:
        event = {
            "type":       "potential_leak",
            "session_id": session_id,
            "user_id":    user_id,
            "markers":    list(leaked),
            "snippet":    response_text[:200],
        }
        _audit_log.append(event)
        print(f"[AUDIT ALERT] potential cross-session leak in session {session_id!r}: {leaked}")

def chat(session_id: str, user_id: str, message: str) -> str:
    if session_id not in _sessions:
        _sessions[session_id] = {"user_id": user_id, "history": [], "known_markers": set()}

    session  = _sessions[session_id]
    history  = session["history"]

    # Track user-specific markers introduced in this session
    new_markers = extract_user_markers(message)
    session["known_markers"].update(new_markers)

    history.append({"role": "user", "content": message})
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})

    audit_response(session_id, user_id, reply)
    return reply

# Simulate potential cross-session leak scenario
chat("s1", "alice", "Hi, I'm Alice Johnson, alice@example.com")
chat("s1", "alice", "Remember my name.")

# A different session — should not reference Alice
chat("s2", "bob", "Who was the last person you spoke to?")  # should not leak Alice's info

print(f"\nAudit events: {len(_audit_log)}")
```

**Expected Token Savings:** Audit layer catches leaks before they compound across sessions; early detection is cheaper than incident response.
**Environment:** GDPR-sensitive deployments, healthcare, finance; run as a monitoring layer alongside other isolation techniques.

---

## Comparison

| Option | Isolation Level | Persistence | Multi-Process Safe | Best For |
|---|---|---|---|---|
| 1. Stateless per-request | Total | No | Yes | Simple API handlers, Lambda |
| 2. In-memory session store | Per-session | No | No | Single-process services |
| 3. User dataclass | Per-object | No | No | OOP services, explicit ownership |
| 4. Redis-backed TTL | Per-session | Yes | Yes | Horizontally-scaled deployments |
| 5. Session middleware | Per-session + auth | Optional | Optional | Public APIs with security requirements |
| 6. Audit log | Monitoring layer | Yes | Yes | Compliance, GDPR, healthcare |
