---
layout: solution
title: "Duplicate Messages in Conversation History — Context Bloat"
category: context-window
description: "Agent accidentally adds the same message twice to conversation history. Tool results are included twice. System prompt is duplicated. Each duplicate wastes tokens and can confuse the model about the current state."
tags: [context-window, duplicate, history, messages, deduplication, token-cost, bloat]
---

# Duplicate Messages in Conversation History — Context Bloat

## Symptom
- Same user message appears twice in a row in the conversation history
- Tool results are included once as a tool_result block and again as an assistant message
- System message appears both in the `system` parameter and again in the `messages` array
- After 10 turns, context has 20 messages instead of 10 — every message duplicated
- API call costs 2× expected because of duplicated history
- Model behaves strangely because it sees contradictory duplicate instructions

## Root Cause
Message accumulation bugs are common: appending to history before and after an API call, not checking for duplicates before appending, including the response in history AND constructing the next prompt from the same response, or serializing/deserializing history incorrectly. Framework bugs can also cause duplicates when messages are added in event handlers that fire twice.

## Fix

### Option 1: Deduplicate history on every append

```python
import hashlib
import json

def message_hash(message: dict) -> str:
    """Stable hash for a message — used for deduplication"""
    content = message.get("content", "")
    if isinstance(content, list):
        content = json.dumps(content, sort_keys=True)
    return hashlib.sha256(
        f"{message.get('role', '')}:{str(content)}".encode()
    ).hexdigest()[:16]

class DeduplicatingHistory:
    """
    Conversation history that silently ignores duplicate messages.
    """

    def __init__(self):
        self._messages: list[dict] = []
        self._seen_hashes: set[str] = set()

    def append(self, message: dict) -> bool:
        """
        Add message to history. Returns True if added, False if duplicate.
        """
        h = message_hash(message)
        if h in self._seen_hashes:
            print(
                f"Duplicate message detected and dropped: "
                f"{message.get('role')} — {str(message.get('content', ''))[:50]}"
            )
            return False

        self._seen_hashes.add(h)
        self._messages.append(message)
        return True

    def extend(self, messages: list[dict]):
        for msg in messages:
            self.append(msg)

    def to_list(self) -> list[dict]:
        return list(self._messages)

    def __len__(self):
        return len(self._messages)

history = DeduplicatingHistory()
history.append({"role": "user", "content": "Hello"})
history.append({"role": "user", "content": "Hello"})  # Silently dropped
history.append({"role": "assistant", "content": "Hi there!"})
print(len(history))  # → 2, not 3
```

### Option 2: Validate no consecutive same-role messages

```python
def validate_message_history(messages: list[dict]) -> list[str]:
    """
    Validate conversation history for common duplication problems.
    Returns list of issues found.
    """
    issues = []

    for i in range(len(messages) - 1):
        current = messages[i]
        next_msg = messages[i + 1]

        # Check for identical consecutive messages
        if (current.get("role") == next_msg.get("role") and
                current.get("content") == next_msg.get("content")):
            issues.append(
                f"Message {i} and {i+1} are identical: "
                f"{current.get('role')}: {str(current.get('content', ''))[:50]}"
            )

        # Check for consecutive same-role messages (usually wrong)
        if current.get("role") == next_msg.get("role"):
            # Consecutive user messages are sometimes valid (tool results)
            # but consecutive assistant messages are almost always wrong
            if current.get("role") == "assistant":
                issues.append(
                    f"Consecutive assistant messages at positions {i} and {i+1}"
                )

    # Check if history starts correctly
    if messages and messages[0].get("role") not in ("user", "system"):
        issues.append(f"History should start with 'user' message, got '{messages[0].get('role')}'")

    return issues

# Before each API call:
issues = validate_message_history(history)
if issues:
    for issue in issues:
        print(f"HISTORY WARNING: {issue}")
```

### Option 3: Atomic history update — build next turn atomically

```python
import anthropic
from copy import deepcopy

client = anthropic.Anthropic()

async def chat_turn(
    history: list[dict],
    user_message: str,
    system: str = ""
) -> tuple[str, list[dict]]:
    """
    Process one conversation turn atomically.
    Returns (response_text, updated_history).
    Never mutates the input history — returns a new list.
    """
    # Build the messages to send (don't mutate history yet)
    messages_to_send = history + [{"role": "user", "content": user_message}]

    # Make API call
    response = client.messages.create(
        model="claude-sonnet-4-6",
        system=system,
        messages=messages_to_send,
        max_tokens=1024
    )

    response_text = response.content[0].text

    # Build new history atomically — only update once we have a successful response
    new_history = deepcopy(history)
    new_history.append({"role": "user", "content": user_message})
    new_history.append({"role": "assistant", "content": response_text})

    return response_text, new_history

# Usage — history is always in a consistent state
history = []
response, history = await chat_turn(history, "Hello")
response, history = await chat_turn(history, "How are you?")
# history grows by exactly 2 messages per turn, never more
```

### Option 4: Structural deduplication — remove adjacent duplicates

```python
def deduplicate_history(messages: list[dict]) -> list[dict]:
    """
    Remove duplicate messages from history.
    Handles: exact duplicates, consecutive identical messages.
    """
    if not messages:
        return messages

    # Step 1: Remove exact consecutive duplicates
    deduped = [messages[0]]
    for msg in messages[1:]:
        if msg != deduped[-1]:
            deduped.append(msg)

    # Step 2: Remove non-consecutive exact duplicates by content hash
    seen = set()
    result = []
    for msg in deduped:
        h = message_hash(msg)
        if h not in seen:
            seen.add(h)
            result.append(msg)
        else:
            print(f"Removed non-consecutive duplicate: {msg.get('role')}: {str(msg.get('content',''))[:40]}")

    original_count = len(messages)
    if len(result) < original_count:
        print(f"History deduplicated: {original_count} → {len(result)} messages")

    return result

# Apply before each API call as a safety net:
clean_history = deduplicate_history(raw_history)
response = client.messages.create(messages=clean_history, ...)
```

### Option 5: History size monitoring with dedup trigger

```python
def check_history_health(messages: list[dict]) -> dict:
    """
    Analyze conversation history for anomalies.
    """
    unique_hashes = set(message_hash(m) for m in messages)
    role_counts = {}
    for m in messages:
        role = m.get("role", "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1

    # Expected: roughly equal user and assistant counts
    user_count = role_counts.get("user", 0)
    assistant_count = role_counts.get("assistant", 0)
    imbalance = abs(user_count - assistant_count) > 2

    duplicate_count = len(messages) - len(unique_hashes)

    return {
        "total_messages": len(messages),
        "unique_messages": len(unique_hashes),
        "duplicate_count": duplicate_count,
        "has_duplicates": duplicate_count > 0,
        "role_counts": role_counts,
        "role_imbalance": imbalance,
        "estimated_tokens": sum(
            len(str(m.get("content", ""))) // 4
            for m in messages
        ),
        "health": "ok" if duplicate_count == 0 and not imbalance else "issues_found"
    }

async def monitored_chat(messages: list[dict], client) -> str:
    health = check_history_health(messages)
    if health["has_duplicates"]:
        print(f"History has {health['duplicate_count']} duplicates — deduplicating")
        messages = deduplicate_history(messages)
    if health["role_imbalance"]:
        print(f"Role imbalance detected: {health['role_counts']}")

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        messages=messages,
        max_tokens=1024
    )
    return response.content[0].text
```

### Option 6: Framework-level history management (avoid manual list mutation)

```python
class ConversationManager:
    """
    Manages conversation history with built-in duplicate prevention.
    Single point of history mutation — impossible to add duplicates accidentally.
    """

    def __init__(self, system: str = ""):
        self.system = system
        self._history: list[dict] = []
        self._turn_count = 0

    def add_user_message(self, content: str) -> None:
        """Add user message. Raises if last message was also from user."""
        if self._history and self._history[-1]["role"] == "user":
            raise RuntimeError(
                "Cannot add user message — last message was already from user. "
                "Call add_assistant_message() first."
            )
        self._history.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        """Add assistant message. Raises if last message was also from assistant."""
        if self._history and self._history[-1]["role"] == "assistant":
            raise RuntimeError(
                "Cannot add assistant message — last message was already from assistant. "
                "Call add_user_message() first."
            )
        self._history.append({"role": "assistant", "content": content})
        self._turn_count += 1

    async def send(self, user_message: str, client) -> str:
        """Send a message and get a response — manages history correctly"""
        self.add_user_message(user_message)

        response = await client.messages.create(
            model="claude-sonnet-4-6",
            system=self.system,
            messages=self._history,
            max_tokens=1024
        )

        response_text = response.content[0].text
        self.add_assistant_message(response_text)
        return response_text

    @property
    def history(self) -> list[dict]:
        return list(self._history)  # Return copy — prevent external mutation

conv = ConversationManager(system="You are a helpful assistant.")
response = await conv.send("Hello", client)
# Trying to add duplicate or out-of-order messages raises immediately
```

## Common Duplication Sources

| Source | Cause | Fix |
|---|---|---|
| Append before and after API call | Race in async code | Build new history atomically |
| Framework event fires twice | Double-registration of handler | Deduplicate in handler |
| History serialized + re-added | Reconstruction adds existing messages | Use explicit session ID + load-once |
| System in messages AND system param | Copy-paste mistake | Never put system in messages array |
| Tool result added twice | Tool execution + history update overlap | Single history update point |
| Retry appends to non-reset history | Retry logic doesn't reset history | Save history before retry, restore on fail |

## Expected Token Savings
10-turn conversation with all messages duplicated: 2× token cost
Deduplication: 50% token reduction, restores correct behavior

## Environment
- All conversational agents with maintained history; most common in async frameworks with event-driven history updates
- Source: direct experience; history duplication is the hardest to diagnose because responses look correct but cost 2× as much
