---
layout: solution
title: "Agent Sends Duplicate Messages in History"
category: context-window
description: "The same user message or tool result appears multiple times in the conversation array, wasting tokens and confusing the model."
tags: [context-window, token-cost, history, deduplication, conversation-management]
---

## Symptom

API calls grow progressively more expensive even though no new content is being added. Inspection of the `messages` array reveals the same user question repeated three times, tool results duplicated in consecutive turns, or the same assistant response pasted in at both turn 3 and turn 7. The model occasionally expresses confusion about contradictory information it sees in its own history.

## Root Cause

Duplicates enter conversation history through several common patterns: retry logic that appends the original message again before retrying, concurrent coroutines that both append to a shared list, rebuild-from-scratch logic that doesn't check what's already in the list, or serialisation/deserialisation bugs that double-append on resume. Without an explicit deduplication step the array grows unchecked.

## Fix

### Option 1 — Content-hash deduplication before each API call

```python
import hashlib
import json
import anthropic

client = anthropic.Anthropic()

def content_hash(message: dict) -> str:
    """Stable hash of role + serialised content."""
    key = json.dumps({"role": message["role"], "content": message["content"]}, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()

def deduplicate(messages: list[dict]) -> list[dict]:
    """Remove exact duplicates while preserving order of first occurrence."""
    seen: set[str] = set()
    unique: list[dict] = []
    for msg in messages:
        h = content_hash(msg)
        if h not in seen:
            seen.add(h)
            unique.append(msg)
    removed = len(messages) - len(unique)
    if removed:
        print(f"[dedup] removed {removed} duplicate message(s)")
    return unique

def chat(history: list[dict], user_message: str) -> str:
    history.append({"role": "user", "content": user_message})
    clean = deduplicate(history)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=clean,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply

# Simulate accidental duplication
history: list[dict] = []
history.append({"role": "user", "content": "What is Python?"})  # added once
history.append({"role": "user", "content": "What is Python?"})  # oops — duplicate
history.append({"role": "assistant", "content": "Python is a programming language."})
history.append({"role": "assistant", "content": "Python is a programming language."})  # duplicate

reply = chat(history, "Give me an example.")
print(reply)
```

**Expected Token Savings:** Each duplicate message wastes its full token count on every subsequent API call; removing 2–3 duplicates from a 20-turn history saves 5–15% of input tokens per call.
**Environment:** Any agent that rebuilds or appends to history across retries or restarts.

---

### Option 2 — Append-only history wrapper that rejects duplicates at write time

```python
import hashlib
import json
import anthropic

client = anthropic.Anthropic()

class DeduplicatingHistory:
    def __init__(self):
        self._messages: list[dict] = []
        self._hashes:   set[str]   = set()

    def _hash(self, msg: dict) -> str:
        key = json.dumps({"role": msg["role"], "content": msg.get("content", "")}, sort_keys=True)
        return hashlib.sha256(key.encode()).hexdigest()

    def append(self, role: str, content) -> bool:
        """Return True if appended, False if duplicate."""
        msg = {"role": role, "content": content}
        h   = self._hash(msg)
        if h in self._hashes:
            print(f"[history] duplicate rejected: {role}/{str(content)[:60]}")
            return False
        self._hashes.add(h)
        self._messages.append(msg)
        return True

    def messages(self) -> list[dict]:
        return list(self._messages)

    def __len__(self) -> int:
        return len(self._messages)


history = DeduplicatingHistory()

def chat(user_input: str) -> str:
    history.append("user", user_input)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=history.messages(),
    )
    reply = response.content[0].text
    history.append("assistant", reply)
    return reply

print(chat("Explain async/await in Python."))
# Simulate a retry that would re-append the same user message
history.append("user", "Explain async/await in Python.")   # rejected
print(f"History length: {len(history)}")                   # still correct
print(chat("Give a short code example."))
```

**Expected Token Savings:** Duplicates are blocked at write time; the error is detected immediately rather than discovered during a token audit.
**Environment:** High-reliability agents where retries or concurrent writes could cause duplicates; wrap the history list from day one.

---

### Option 3 — Tool-result deduplication by tool_use_id

```python
import json
import anthropic

client = anthropic.Anthropic()

def deduplicate_tool_results(messages: list[dict]) -> list[dict]:
    """
    Tool results are identified by tool_use_id inside list-content messages.
    Remove turns where all tool_use_ids have already appeared.
    """
    seen_tool_ids: set[str] = set()
    cleaned: list[dict] = []

    for msg in messages:
        content = msg.get("content")

        if isinstance(content, list):
            # Filter out duplicate tool_result blocks
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id", "")
                    if tid in seen_tool_ids:
                        print(f"[dedup] duplicate tool_result for id={tid!r} removed")
                        continue
                    seen_tool_ids.add(tid)
                new_blocks.append(block)

            if new_blocks:
                cleaned.append({**msg, "content": new_blocks})
            # else: entire turn was duplicates — drop it
        else:
            cleaned.append(msg)

    return cleaned

# Build a history with a duplicated tool result (common in retry scenarios)
messages = [
    {"role": "user", "content": "What's the weather in Tokyo?"},
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "tu_001", "name": "get_weather", "input": {"city": "Tokyo"}}
    ]},
    # First tool result
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_001", "content": '{"temp": 22, "condition": "sunny"}'}
    ]},
    # Duplicate tool result (retry bug)
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_001", "content": '{"temp": 22, "condition": "sunny"}'}
    ]},
]

cleaned = deduplicate_tool_results(messages)
print(f"Before: {len(messages)} turns | After: {len(cleaned)} turns")

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=cleaned,
)
print(response.content[0].text)
```

**Expected Token Savings:** Duplicate tool results can be large (full API response bodies); removing one duplicate saves hundreds to thousands of tokens per call.
**Environment:** Tool-using agents with retry logic that re-appends tool results on failure.

---

### Option 4 — Sliding-window history with duplicate awareness

```python
import hashlib
import json
import anthropic

client = anthropic.Anthropic()

MAX_TURNS = 20  # keep at most N turns in the window

class SlidingWindow:
    def __init__(self, max_turns: int = MAX_TURNS):
        self._turns:    list[dict] = []
        self._max_turns = max_turns

    def _hash(self, msg: dict) -> str:
        return hashlib.md5(json.dumps(msg, sort_keys=True).encode()).hexdigest()

    def push(self, role: str, content) -> None:
        msg = {"role": role, "content": content}
        # Check for duplicate in recent window
        recent_hashes = {self._hash(m) for m in self._turns[-6:]}
        if self._hash(msg) in recent_hashes:
            print(f"[window] near-duplicate detected and skipped: {role}/{str(content)[:50]}")
            return
        self._turns.append(msg)
        # Trim to window size (always keep pairs to maintain alternating pattern)
        if len(self._turns) > self._max_turns:
            self._turns = self._turns[-self._max_turns:]

    def get(self) -> list[dict]:
        return list(self._turns)


window = SlidingWindow(max_turns=10)

def chat(msg: str) -> str:
    window.push("user", msg)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=window.get(),
    )
    reply = response.content[0].text
    window.push("assistant", reply)
    return reply

for turn in [
    "Tell me about black holes.",
    "Tell me about black holes.",  # duplicate — skipped
    "How do they form?",
    "What happens at the event horizon?",
]:
    print(f"User: {turn}")
    result = chat(turn)
    print(f"Agent: {result[:80]}\n")
```

**Expected Token Savings:** Combines window trimming and deduplication; keeps context at a fixed cost regardless of session length.
**Environment:** Interactive chat agents with long sessions; drop-in replacement for a plain `messages` list.

---

### Option 5 — Canonical serialisation to catch semantically identical messages

```python
import json
import hashlib
import anthropic

client = anthropic.Anthropic()

def canonical(obj) -> str:
    """
    Produce a stable string for any message content, including nested lists/dicts.
    Handles both string content and list content (tool calls, tool results).
    """
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, list):
        return json.dumps([canonical(item) for item in obj], sort_keys=True)
    if isinstance(obj, dict):
        return json.dumps({k: canonical(v) for k, v in sorted(obj.items())})
    return str(obj)

def msg_fingerprint(msg: dict) -> str:
    blob = canonical({"role": msg["role"], "content": msg.get("content", "")})
    return hashlib.sha256(blob.encode()).hexdigest()

class SmartHistory:
    def __init__(self):
        self._data:    list[dict] = []
        self._prints:  set[str]   = set()

    def add(self, msg: dict) -> None:
        fp = msg_fingerprint(msg)
        if fp not in self._prints:
            self._prints.add(fp)
            self._data.append(msg)

    def messages(self) -> list[dict]:
        return list(self._data)

history = SmartHistory()

# Simulate messages arriving from multiple sources, some duplicated
raw_messages = [
    {"role": "user",      "content": "List three Python web frameworks."},
    {"role": "user",      "content": "List three Python web frameworks."},   # exact dup
    {"role": "user",      "content": "  List three Python web frameworks."},  # whitespace dup
    {"role": "assistant", "content": "Flask, Django, FastAPI."},
    {"role": "assistant", "content": "Flask, Django, FastAPI."},              # exact dup
    {"role": "user",      "content": "Which is fastest?"},
]

for msg in raw_messages:
    history.add(msg)

print(f"Input: {len(raw_messages)} messages | Deduped: {len(history.messages())} messages")

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=history.messages(),
)
print(response.content[0].text)
```

**Expected Token Savings:** Catches whitespace and formatting variations that byte-equal comparison misses; prevents "soft duplicates" from clogging history.
**Environment:** Agents that normalise or sanitise messages before storage; multi-source ingestion pipelines.

---

### Option 6 — Periodic history audit and repair

```python
import hashlib
import json
import anthropic

client = anthropic.Anthropic()

def audit_history(messages: list[dict]) -> dict:
    """Return a report of history health."""
    fingerprints: list[str] = []
    duplicates:   list[int] = []
    role_errors:  list[int] = []

    prev_role = None
    for i, msg in enumerate(messages):
        fp = hashlib.md5(json.dumps(msg, sort_keys=True).encode()).hexdigest()

        if fp in fingerprints:
            duplicates.append(i)
        fingerprints.append(fp)

        role = msg.get("role")
        if prev_role and role == prev_role and role in {"user", "assistant"}:
            role_errors.append(i)
        prev_role = role

    return {
        "total":      len(messages),
        "duplicates": duplicates,
        "role_errors": role_errors,
        "healthy":    not duplicates and not role_errors,
    }

def repair_history(messages: list[dict]) -> list[dict]:
    """Remove duplicates and merge consecutive same-role messages."""
    # Step 1: remove exact duplicates
    seen:    set[str]   = set()
    unique:  list[dict] = []
    for msg in messages:
        fp = hashlib.md5(json.dumps(msg, sort_keys=True).encode()).hexdigest()
        if fp not in seen:
            seen.add(fp)
            unique.append(msg)

    # Step 2: merge consecutive same-role messages (common after dedup)
    merged: list[dict] = []
    for msg in unique:
        if merged and merged[-1]["role"] == msg["role"]:
            prev = merged[-1]
            prev_c = prev["content"] if isinstance(prev["content"], str) else ""
            curr_c = msg["content"]  if isinstance(msg["content"],  str) else ""
            merged[-1] = {"role": msg["role"], "content": f"{prev_c}\n{curr_c}".strip()}
        else:
            merged.append(msg)

    return merged

# Simulate a corrupted history
bad_history = [
    {"role": "user",      "content": "What is Docker?"},
    {"role": "user",      "content": "What is Docker?"},       # duplicate
    {"role": "assistant", "content": "Docker is a container platform."},
    {"role": "assistant", "content": "It packages apps with their dependencies."},  # consecutive assistant
    {"role": "user",      "content": "How do I install it?"},
]

report = audit_history(bad_history)
print(f"Audit: {report}")

if not report["healthy"]:
    fixed = repair_history(bad_history)
    print(f"\nRepaired history ({len(bad_history)} → {len(fixed)} messages):")
    for m in fixed:
        print(f"  {m['role']}: {m['content'][:80]}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=fixed,
    )
    print(f"\nResponse: {response.content[0].text[:120]}")
```

**Expected Token Savings:** Periodic audit catches accumulating duplicates before they dominate token cost; repair runs once and restores correctness.
**Environment:** Long-running agents or those resumed from serialised state; run audit on history load and after each batch of operations.

---

## Comparison

| Option | Detection Point | Handles Tool Results | Async Safe | Best For |
|---|---|---|---|---|
| 1. Pre-call hash | Before API call | No | No | Simple retrofit to existing agents |
| 2. Write-time guard | On append | No | No | New agents — catch duplicates early |
| 3. Tool-result dedup | Before API call | Yes | No | Tool-using agents with retry logic |
| 4. Sliding window | On push | No | No | Long sessions with window trimming |
| 5. Canonical serialisation | On append | Yes | No | Multi-source ingestion, soft duplicates |
| 6. Audit + repair | Periodic / on load | Partial | No | Resumed sessions, corrupted history |
