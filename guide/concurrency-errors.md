---
layout: default
title: "AI Agent Concurrency Errors — Race Conditions, Deadlocks, and Async Fixes"
description: "Fix race conditions, deadlocks, and async errors in AI agents. Covers concurrent tool calls, shared state corruption, message ordering, and session isolation."
permalink: /guide/concurrency-errors
---

# AI Agent Concurrency Error Guide

Concurrency bugs in agents are the hardest to reproduce — they're timing-dependent and often don't appear until load increases. This guide covers the patterns that actually appear in production agent systems.

---

## Concurrency Failure Patterns

| Pattern | Symptom | Root Cause |
|---------|---------|-----------|
| Shared state corruption | Agent produces inconsistent output under load | Multiple requests write to same context |
| Duplicate message processing | Same user message triggers two agent responses | No deduplication on message receipt |
| Tool call race | Two tool calls interfere (e.g., both try to write the same file) | Parallel calls without resource locking |
| Deadlock | Agent appears stuck, no error, no output | Two operations waiting on each other |
| Session interleaving | User A's context bleeds into User B's session | Session state not isolated per user |

---

## Fix 1: Session Isolation Per User

The most critical concurrency fix — each user must have completely isolated state:

```yaml
# openclaw.config.yaml
sessions:
  isolation: strict         # Never share context between sessions
  storage: per_session      # Each session gets its own namespace
  id_source: user_id        # Session ID derived from authenticated user
```

Without session isolation, message A from User 1 can land in User 2's context if requests arrive concurrently. This is both a correctness bug and a data leak.

---

## Fix 2: Message Deduplication

Network retries cause the same message to arrive twice. Without deduplication, the agent processes it twice:

```python
import hashlib
from collections import deque

seen_message_ids = deque(maxlen=10000)  # Rolling window

def should_process(message):
    msg_id = message.get('id') or hashlib.md5(
        f"{message['user_id']}:{message['text']}:{message['timestamp']:.0f}".encode()
    ).hexdigest()
    if msg_id in seen_message_ids:
        return False
    seen_message_ids.append(msg_id)
    return True
```

For Telegram/Discord bots, this prevents duplicate responses when the platform retries delivery.

---

## Fix 3: Concurrency Limits Per Session

Without limits, a single user sending messages rapidly can spawn unlimited parallel agent threads:

```yaml
agent:
  concurrency:
    max_concurrent_per_session: 1   # Queue, don't drop
    max_concurrent_total: 50        # Global cap
    queue_timeout_ms: 30000         # Give up after 30s in queue
    overflow_action: return_busy_message
```

`max_concurrent_per_session: 1` is the most important setting — it ensures one user's messages are processed in order without interleaving.

---

## Fix 4: Tool Call Locking for Shared Resources

When parallel tool calls might touch the same resource:

```python
import asyncio

_file_locks = {}

async def write_file(path, content):
    if path not in _file_locks:
        _file_locks[path] = asyncio.Lock()

    async with _file_locks[path]:
        # Only one writer at a time per path
        with open(path, 'w') as f:
            f.write(content)
```

For database writes, use row-level locking or transactions instead of application-level locks.

---

## Fix 5: Idempotent Tool Design

Design tool calls to be safe to call twice:

```python
# BAD — not idempotent, double-call creates duplicate
def create_record(data):
    db.insert(data)  # Fails if record exists, or creates duplicate

# GOOD — idempotent via upsert
def create_or_update_record(data):
    db.upsert(data, conflict_column='external_id')  # Safe to call twice
```

All agent-facing tools should be idempotent. This makes the system safe to retry without manual deduplication at every layer.

---

## Fix 6: Async Context Propagation

A common bug: async code loses context when switching threads or tasks.

```python
import contextvars

session_context = contextvars.ContextVar('session_id')

async def handle_message(session_id, message):
    session_context.set(session_id)   # Set context for this async task
    await process(message)

async def process(message):
    sid = session_context.get()       # Available throughout async call chain
    # No need to pass session_id through every function call
```

Without `contextvars`, `session_id` can leak between async tasks if stored in a plain global.

---

## Fix 7: Deadlock Detection and Recovery

Deadlocks in agent systems usually happen when:
1. Tool A waits for Tool B to complete
2. Tool B is waiting for Tool A to release a resource

Prevention:
```yaml
agent:
  tool_call_timeout_ms: 30000  # Hard timeout prevents indefinite waits
  deadlock_detection:
    enabled: true
    check_interval_ms: 5000
    on_deadlock: kill_oldest_waiter
```

Detection: if a set of tool calls makes no progress for N seconds, log the call graph and kill the oldest waiter.

---

## Fix 8: Message Ordering Guarantees

For streaming platforms (Telegram, Discord, Slack), messages can arrive out of order under load:

```python
from collections import defaultdict
import heapq

class OrderedMessageQueue:
    def __init__(self):
        self.queues = defaultdict(list)  # {session_id: [(seq, message), ...]}
        self.next_seq = defaultdict(int)

    def push(self, session_id, seq, message):
        heapq.heappush(self.queues[session_id], (seq, message))

    def pop_in_order(self, session_id):
        """Yield messages in order, block on gaps"""
        queue = self.queues[session_id]
        while queue and queue[0][0] == self.next_seq[session_id]:
            seq, msg = heapq.heappop(queue)
            self.next_seq[session_id] += 1
            yield msg
```

---

## Concurrency Checklist

- [ ] Session isolation enforced (no shared state between users)
- [ ] Message deduplication implemented (hash-based or ID-based)
- [ ] `max_concurrent_per_session: 1` set (or explicit ordering mechanism)
- [ ] All tool calls to shared resources use locks or are idempotent
- [ ] Tool-level timeouts set (prevents indefinite waits → deadlock)
- [ ] Async context propagation uses `contextvars`, not globals
- [ ] Load tested with concurrent requests from multiple users

---

[← View all concurrency solutions](/synapse-ai/solutions/concurrency/)

**Related guides:**
- [Loop / Stuck Errors](/synapse-ai/guide/loop-stuck-errors) — deadlocks can look like stuck agents
- [Memory / Session Errors](/synapse-ai/guide/memory-session-errors) — session isolation is a memory concern too
- [Performance Errors](/synapse-ai/guide/performance-errors) — parallelism done right for speed, not bugs

<div class="cta-box">
  <h3>Detect race conditions before they reach production</h3>
  <p>SynapseAI documents concurrency patterns and fixes from real agent deployments.</p>
  <code>clawhub install synapse-ai</code>
</div>
