---
layout: solution
title: "Agent Leaks Sensitive Data Across User Sessions"
category: memory
description: "Agent inadvertently includes data from one user's session in another user's response — names, addresses, account numbers, or conversation history bleed across session boundaries."
tags: [memory, security, session-isolation, pii, multi-tenant, privacy]
---

## Symptom

User B asks a routine question and receives a response that contains User A's name, address, or order history. Alternatively, the agent refers to "your previous question" but the question belongs to a different user. In logs, conversation histories contain interleaved messages from multiple users.

```
User B: "What is my order status?"
Agent:  "Hi Sarah! Your order #48291 shipped to 123 Maple St..."
# Sarah is User A — User B is receiving her data
```

## Root Cause

A shared `messages` list, global conversation store, or in-memory cache is not scoped per user. When multiple requests arrive concurrently or sequentially, the same list is appended to by different users. Context is accumulated globally instead of in isolated per-session containers.

## Fix

---

### Option 1 — Session-Scoped In-Memory Store with Strict ID Isolation

Each session gets its own list in a per-session dict keyed by UUID. No cross-session reads are possible by design — a missing key raises `KeyError` rather than falling back to a shared default.

```python
import uuid
import anthropic
from dataclasses import dataclass, field
from threading import Lock

@dataclass
class Session:
    session_id: str
    user_id: str
    messages: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

class SessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = Lock()

    def create(self, user_id: str) -> str:
        session_id = str(uuid.uuid4())
        with self._lock:
            self._sessions[session_id] = Session(
                session_id=session_id,
                user_id=user_id,
            )
        return session_id

    def get(self, session_id: str, user_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session {session_id} not found")
        if session.user_id != user_id:
            # Ownership mismatch — log and raise, never return data
            raise PermissionError(
                f"Session {session_id} does not belong to user {user_id}"
            )
        return session

    def delete(self, session_id: str, user_id: str):
        session = self.get(session_id, user_id)
        with self._lock:
            del self._sessions[session.session_id]

# Global store — sessions are isolated, the store itself is shared
store = SessionStore()
client = anthropic.Anthropic()

def chat(session_id: str, user_id: str, user_message: str) -> str:
    session = store.get(session_id, user_id)

    session.messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a helpful assistant.",
        messages=session.messages,
    )

    assistant_reply = response.content[0].text
    session.messages.append({"role": "assistant", "content": assistant_reply})

    return assistant_reply

# Demonstrate isolation
sid_alice = store.create("user-alice")
sid_bob = store.create("user-bob")

chat(sid_alice, "user-alice", "My name is Alice and I live in Seattle.")
chat(sid_bob, "user-bob", "What city do I live in?")

# Alice's context stays in sid_alice — Bob gets no information about Alice
try:
    store.get(sid_alice, "user-bob")  # Raises PermissionError
except PermissionError as e:
    print(f"Blocked: {e}")
```

**Expected Token Savings:** None — correctness fix, not a cost optimisation
**Environment:** `pip install anthropic`

---

### Option 2 — Redis-Backed Session Store with TTL and Key Namespacing

Store each session's history in Redis under a key namespaced by `session:{session_id}:messages`. TTL auto-expires stale sessions. Cross-user access is impossible because the session_id is a random UUID unknown to other users.

```python
import uuid
import json
import anthropic
import redis
from datetime import timedelta

SESSION_TTL = timedelta(hours=2)
KEY_PREFIX = "session"

r = redis.Redis(host="localhost", port=6379, decode_responses=True)
client = anthropic.Anthropic()

def _key(session_id: str) -> str:
    return f"{KEY_PREFIX}:{session_id}:messages"

def _owner_key(session_id: str) -> str:
    return f"{KEY_PREFIX}:{session_id}:owner"

def create_session(user_id: str) -> str:
    session_id = str(uuid.uuid4())
    pipe = r.pipeline()
    pipe.set(_owner_key(session_id), user_id, ex=SESSION_TTL)
    pipe.set(_key(session_id), json.dumps([]), ex=SESSION_TTL)
    pipe.execute()
    return session_id

def verify_owner(session_id: str, user_id: str):
    owner = r.get(_owner_key(session_id))
    if owner is None:
        raise KeyError(f"Session {session_id} expired or not found")
    if owner != user_id:
        raise PermissionError(f"Unauthorized access to session {session_id}")

def get_messages(session_id: str, user_id: str) -> list[dict]:
    verify_owner(session_id, user_id)
    raw = r.get(_key(session_id))
    return json.loads(raw) if raw else []

def save_messages(session_id: str, user_id: str, messages: list[dict]):
    verify_owner(session_id, user_id)
    pipe = r.pipeline()
    pipe.set(_key(session_id), json.dumps(messages), ex=SESSION_TTL)
    pipe.set(_owner_key(session_id), user_id, ex=SESSION_TTL)  # Refresh TTL
    pipe.execute()

def chat(session_id: str, user_id: str, user_message: str) -> str:
    messages = get_messages(session_id, user_id)
    messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a helpful assistant.",
        messages=messages,
    )

    reply = response.content[0].text
    messages.append({"role": "assistant", "content": reply})
    save_messages(session_id, user_id, messages)
    return reply

# Usage
sid = create_session("user-charlie")
print(chat(sid, "user-charlie", "My account number is 99887."))
print(chat(sid, "user-charlie", "What account number did I share?"))
```

**Expected Token Savings:** None — correctness fix; Redis TTL prevents unbounded memory growth
**Environment:** `pip install anthropic redis`

---

### Option 3 — PII Scrubbing Before Storage

Detect and redact PII (names, emails, phone numbers, credit cards) before storing messages in any session store. Even if isolation fails, scraped data reveals nothing sensitive.

```python
import re
import anthropic
from dataclasses import dataclass, field

PII_PATTERNS = [
    (re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b"), "[NAME]"),          # Full names
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b"), "[EMAIL]"),       # Email
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[PHONE]"),   # US phone
    (re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"), "[CARD]"),  # Card
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),                  # SSN
    (re.compile(r"\b\d{5}(?:-\d{4})?\b"), "[ZIP]"),                   # ZIP code
]

def scrub_pii(text: str) -> tuple[str, list[str]]:
    redacted = []
    for pattern, replacement in PII_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            redacted.extend(matches)
            text = pattern.sub(replacement, text)
    return text, redacted

@dataclass
class SecureSession:
    session_id: str
    user_id: str
    messages: list[dict] = field(default_factory=list)

sessions: dict[str, SecureSession] = {}

def secure_chat(session_id: str, user_id: str, raw_message: str) -> str:
    if session_id not in sessions:
        sessions[session_id] = SecureSession(session_id=session_id, user_id=user_id)

    session = sessions[session_id]
    if session.user_id != user_id:
        raise PermissionError("Session ownership mismatch")

    # Scrub before storing
    clean_message, found_pii = scrub_pii(raw_message)
    if found_pii:
        print(f"[AUDIT] Redacted PII from user {user_id}: {found_pii}")

    session.messages.append({"role": "user", "content": clean_message})

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a helpful assistant. Never repeat personal information verbatim.",
        messages=session.messages,
    )

    reply = response.content[0].text
    clean_reply, _ = scrub_pii(reply)
    session.messages.append({"role": "assistant", "content": clean_reply})

    return clean_reply

import uuid
sid = str(uuid.uuid4())
print(secure_chat(sid, "user-dana", "Hi, I'm Dana Smith, my email is dana@example.com"))
print(secure_chat(sid, "user-dana", "What's my email address?"))
```

**Expected Token Savings:** None — defence-in-depth fix; scrubbing adds negligible latency
**Environment:** `pip install anthropic`

---

### Option 4 — Stateless Sessions with Signed Tokens

Don't store session state server-side at all. Encode the full conversation history in a signed JWT-like token returned to the client. Each request includes the token, which is verified and decoded. No server-side store means no cross-user leakage vector.

```python
import json
import hmac
import hashlib
import base64
import time
import anthropic
from typing import Any

SECRET_KEY = b"change-this-to-a-real-secret-in-production"
TOKEN_TTL_SECONDS = 7200  # 2 hours

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _unb64(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)

def encode_token(user_id: str, messages: list[dict]) -> str:
    payload = {
        "user_id": user_id,
        "messages": messages,
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    payload_json = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = _b64(payload_json)
    sig = hmac.new(SECRET_KEY, payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"

def decode_token(token: str, expected_user_id: str) -> list[dict]:
    parts = token.split(".")
    if len(parts) != 2:
        raise ValueError("Malformed token")

    payload_b64, sig_b64 = parts
    expected_sig = hmac.new(SECRET_KEY, payload_b64.encode(), hashlib.sha256).digest()

    if not hmac.compare_digest(expected_sig, _unb64(sig_b64)):
        raise ValueError("Token signature invalid")

    payload = json.loads(_unb64(payload_b64))

    if payload["exp"] < time.time():
        raise ValueError("Token expired")

    if payload["user_id"] != expected_user_id:
        raise PermissionError("Token user_id mismatch")

    return payload["messages"]

def chat(token: str | None, user_id: str, user_message: str) -> tuple[str, str]:
    messages = []
    if token:
        messages = decode_token(token, user_id)  # Raises on tamper

    messages.append({"role": "user", "content": user_message})

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a helpful assistant.",
        messages=messages,
    )

    reply = response.content[0].text
    messages.append({"role": "assistant", "content": reply})

    new_token = encode_token(user_id, messages)
    return reply, new_token

# First turn — no token
reply1, tok1 = chat(None, "user-eve", "My name is Eve.")
print(reply1)

# Second turn — pass token back
reply2, tok2 = chat(tok1, "user-eve", "What's my name?")
print(reply2)

# Cross-user attempt fails
try:
    decode_token(tok2, "user-frank")
except PermissionError as e:
    print(f"Blocked: {e}")
```

**Expected Token Savings:** None — eliminates server-side storage infrastructure entirely
**Environment:** `pip install anthropic`

---

### Option 5 — Async Multi-Tenant Agent with Asyncio Isolation

In async environments, shared mutable state is especially dangerous. Use per-request context variables (`contextvars.ContextVar`) to store session state. Each coroutine has its own context — no sharing possible.

```python
import asyncio
import uuid
import anthropic
from contextvars import ContextVar
from dataclasses import dataclass, field

# ContextVar is isolated per-coroutine (and its descendants)
_current_session: ContextVar["AsyncSession"] = ContextVar("current_session")

@dataclass
class AsyncSession:
    session_id: str
    user_id: str
    messages: list[dict] = field(default_factory=list)

async def chat_turn(user_message: str) -> str:
    session = _current_session.get()
    session.messages.append({"role": "user", "content": user_message})

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are a helpful assistant.",
        messages=session.messages,
    )

    reply = response.content[0].text
    session.messages.append({"role": "assistant", "content": reply})
    return reply

async def user_conversation(user_id: str, turns: list[str]) -> list[str]:
    session = AsyncSession(
        session_id=str(uuid.uuid4()),
        user_id=user_id,
    )
    # Set in this coroutine's context — invisible to all other coroutines
    token = _current_session.set(session)
    try:
        results = []
        for turn in turns:
            reply = await chat_turn(turn)
            results.append(reply)
        return results
    finally:
        _current_session.reset(token)

async def main():
    # Run two user conversations concurrently — contexts are fully isolated
    alice_task = asyncio.create_task(
        user_conversation("alice", [
            "My secret code is ALPHA-99.",
            "What is my secret code?",
        ])
    )
    bob_task = asyncio.create_task(
        user_conversation("bob", [
            "What secret code does the other user have?",
        ])
    )

    alice_replies, bob_replies = await asyncio.gather(alice_task, bob_task)

    print("Alice:", alice_replies)
    print("Bob:", bob_replies)
    # Bob's reply will correctly say it doesn't know about any secret code

asyncio.run(main())
```

**Expected Token Savings:** None — correctness fix; async handling improves throughput
**Environment:** `pip install anthropic`

---

### Option 6 — Audit Log with Cross-Session Anomaly Detection

Add an audit layer that logs all messages with user_id and detects anomalies: if a response contains tokens that appeared only in another user's session, flag and suppress it.

```python
import re
import uuid
import hashlib
import anthropic
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class AuditEntry:
    timestamp: str
    session_id: str
    user_id: str
    role: str
    content_hash: str
    token_count: int
    flagged: bool = False
    flag_reason: str = ""

class CrossSessionAuditLayer:
    def __init__(self):
        # Maps token -> set of user_ids that used it
        self._token_owners: dict[str, set[str]] = defaultdict(set)
        self._sessions: dict[str, dict] = {}
        self._audit_log: list[AuditEntry] = []

    def _fingerprint_tokens(self, text: str) -> set[str]:
        # Extract meaningful tokens (words 5+ chars) as fingerprints
        return {w.lower() for w in re.findall(r"\b\w{5,}\b", text)}

    def register_message(self, session_id: str, user_id: str, role: str, content: str):
        tokens = self._fingerprint_tokens(content)
        for tok in tokens:
            self._token_owners[tok].add(user_id)

        entry = AuditEntry(
            timestamp=datetime.utcnow().isoformat(),
            session_id=session_id,
            user_id=user_id,
            role=role,
            content_hash=hashlib.sha256(content.encode()).hexdigest()[:12],
            token_count=len(tokens),
        )
        self._audit_log.append(entry)

    def check_response(self, session_id: str, user_id: str, response: str) -> tuple[bool, str]:
        tokens = self._fingerprint_tokens(response)
        for tok in tokens:
            owners = self._token_owners.get(tok, set())
            other_owners = owners - {user_id}
            if other_owners:
                reason = f"Token '{tok}' previously seen from user(s): {other_owners}"
                return False, reason
        return True, ""

audit = CrossSessionAuditLayer()
sessions: dict[str, dict] = {}
client = anthropic.Anthropic()

def audited_chat(session_id: str, user_id: str, user_message: str) -> str:
    if session_id not in sessions:
        sessions[session_id] = {"user_id": user_id, "messages": []}

    sess = sessions[session_id]
    if sess["user_id"] != user_id:
        raise PermissionError("Session owner mismatch")

    audit.register_message(session_id, user_id, "user", user_message)
    sess["messages"].append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are a helpful assistant.",
        messages=sess["messages"],
    )
    reply = response.content[0].text

    ok, reason = audit.check_response(session_id, user_id, reply)
    if not ok:
        print(f"[SECURITY] Cross-session data detected for {user_id}: {reason}")
        reply = "I'm sorry, I encountered an issue processing your request. Please try again."
    else:
        audit.register_message(session_id, user_id, "assistant", reply)
        sess["messages"].append({"role": "assistant", "content": reply})

    return reply

# Demonstrate audit detection
sid_g = str(uuid.uuid4())
sid_h = str(uuid.uuid4())

audited_chat(sid_g, "user-grace", "My project codename is NIGHTHAWK.")
print(audited_chat(sid_h, "user-henry", "Tell me about project NIGHTHAWK."))
# Flagged: NIGHTHAWK token was registered to user-grace
```

**Expected Token Savings:** None — security monitoring layer; adds ~1ms per response
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Isolation Strength | Implementation | Stateless | Best For |
|--------|-------------------|----------------|-----------|----------|
| In-Memory Scoped Store | High | Low | No | Single-process servers |
| Redis + TTL | Very High | Medium | No | Multi-process / distributed |
| PII Scrubbing | Medium (defence-in-depth) | Low | No | Compliance-first deployments |
| Signed Tokens | Very High | Medium | Yes | Serverless / edge |
| ContextVar Async | Very High | Low | No | Async Python servers |
| Audit Layer | Medium (detection) | Medium | No | Compliance monitoring |

**Recommended starting point:** Option 1 (in-memory) for single-process, Option 2 (Redis) for distributed deployments. Add Option 3 (PII scrubbing) as an additional layer in any case.
