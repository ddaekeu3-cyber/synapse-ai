---
layout: solution
title: "Agent Mixes Up Users — Session State Shared Across Different Users"
category: concurrency
description: "Two users send messages concurrently. Agent responds to User A with context from User B's conversation, or User A's data appears in User B's session. Session isolation failure."
tags: [session-isolation, multi-user, data-leak, concurrency]
---

# Agent Mixes Up Users — Session State Shared Across Different Users

## Symptom
- User A receives a response that references User B's earlier message
- Agent addresses the wrong user by name
- User A can see User B's private data in the response
- Bug appears only under concurrent load — single-user testing passes
- Adding logs shows context from one user's session appearing in another's

## Root Cause
Session state stored in a global variable, module-level dict, or class attribute shared across all requests. Under concurrent load, Request A's state is read by Response B's handler. Common patterns that cause this:

```python
# BAD — module-level global, shared across all requests
current_context = []  # This gets mixed between users!

async def handle_message(user_id, message):
    current_context.append(message)  # Race condition
    return await agent.complete(current_context)
```

## Fix

### Option 1: Scope context to session ID (in-memory)

```python
from collections import defaultdict
import asyncio

# Per-session storage
_sessions = defaultdict(list)
_session_locks = defaultdict(asyncio.Lock)

async def handle_message(session_id: str, message: str):
    async with _session_locks[session_id]:
        _sessions[session_id].append({"role": "user", "content": message})
        response = await agent.complete(_sessions[session_id])
        _sessions[session_id].append({"role": "assistant", "content": response})
        return response
```

### Option 2: Use request-scoped context vars

```python
from contextvars import ContextVar

session_context: ContextVar[list] = ContextVar('session_context', default=None)

async def handle_request(session_id, message):
    # Each async task gets its own context
    ctx = session_context.get() or []
    ctx = ctx + [{"role": "user", "content": message}]
    session_context.set(ctx)

    response = await agent.complete(ctx)
    session_context.set(ctx + [{"role": "assistant", "content": response}])
    return response
```

### Option 3: Pass context explicitly, never use globals

```python
class AgentSession:
    def __init__(self, session_id: str, user_id: str):
        self.session_id = session_id
        self.user_id = user_id
        self.messages = []  # Isolated per instance

    async def send(self, message: str) -> str:
        self.messages.append({"role": "user", "content": message})
        response = await agent.complete(self.messages)
        self.messages.append({"role": "assistant", "content": response})
        return response

# One session instance per user — never shared
sessions = {}

async def handle_message(user_id, message):
    if user_id not in sessions:
        sessions[user_id] = AgentSession(session_id=str(uuid4()), user_id=user_id)
    return await sessions[user_id].send(message)
```

### Option 4: Validate in staging with concurrent requests

```python
import asyncio

async def test_session_isolation():
    """Send concurrent messages from two users, verify no cross-contamination"""
    user_a = asyncio.create_task(handle_message("user_a", "My name is Alice"))
    user_b = asyncio.create_task(handle_message("user_b", "My name is Bob"))

    resp_a, resp_b = await asyncio.gather(user_a, user_b)

    assert "Bob" not in resp_a, "User A response contains User B's name!"
    assert "Alice" not in resp_b, "User B response contains User A's name!"
    print("Session isolation test passed")
```

## Expected Token Savings
Debugging session isolation failures and data leaks: ~25,000 tokens
This fix: ~400 tokens

## Environment
- Any multi-user agent service with concurrent request handling
- Highest risk: async web frameworks (FastAPI, aiohttp) with shared state
- Also affects: multi-threaded Flask, any module-level mutable state
- Source: direct experience, common in agent API deployments
