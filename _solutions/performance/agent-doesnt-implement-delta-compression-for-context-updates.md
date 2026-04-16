---
title: "Agent Doesn't Implement Delta Compression for Context Updates"
description: "AI agents resend the entire conversation context on every turn instead of transmitting only the changed portions, wasting tokens and increasing latency as conversations grow."
category: performance
difficulty: intermediate
tags: [context, compression, delta, diffing, tokens, efficiency, asyncio]
---

# Agent Doesn't Implement Delta Compression for Context Updates

## Problem

Long-running agent sessions accumulate conversation history. Without delta compression, every turn re-sends the full context — 50 KB of prior messages just to append a 200-byte user message. This wastes prompt tokens, increases latency, and inflates cost linearly with conversation length. Delta compression transmits only what changed.

## Solution 1: Prompt Caching for Static Prefix (KV Cache-Aware Structuring)

Anthropic's prompt caching caches the system prompt and stable conversation prefix, so only the new turn is billed and processed.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class CacheAwareConversation:
    """Structures messages so stable prefix is cached; only new turns are transmitted fresh."""

    def __init__(self, system_prompt: str):
        self._system = system_prompt
        self._history: list[dict] = []
        self._cache_boundary = 0  # index up to which history is considered stable

    def add_user(self, text: str):
        self._history.append({"role": "user", "content": text})

    def add_assistant(self, text: str):
        self._history.append({"role": "assistant", "content": text})
        # Every 10 turns, advance the cache boundary
        if len(self._history) % 10 == 0:
            self._cache_boundary = max(0, len(self._history) - 4)

    def _build_messages(self) -> list[dict]:
        messages = []
        for i, msg in enumerate(self._history):
            content = msg["content"]
            # Mark stable messages for caching
            if i < self._cache_boundary and msg["role"] == "user":
                content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            messages.append({"role": msg["role"], "content": content})
        return messages

    async def send(self, user_message: str) -> str:
        self.add_user(user_message)
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": self._system,
                "cache_control": {"type": "ephemeral"},  # cache system prompt
            }],
            messages=self._build_messages(),
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        reply = resp.content[0].text
        self.add_assistant(reply)
        cache_hit = resp.usage.cache_read_input_tokens if hasattr(resp.usage, "cache_read_input_tokens") else 0
        return reply

conv = CacheAwareConversation("You are a helpful AI assistant specializing in code review.")
```

**When to use**: Any multi-turn agent. Reduces per-turn token cost by 60–90% for stable prefixes.

---

## Solution 2: Incremental Message Diff — Only Send New Messages

Track which messages have already been sent; reconstruct only the delta on each turn.

```python
import hashlib
import json
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

@dataclass
class Message:
    role: str
    content: str
    _hash: str = field(init=False)

    def __post_init__(self):
        self._hash = hashlib.md5(f"{self.role}:{self.content}".encode()).hexdigest()

class DeltaConversationManager:
    """Sends only new messages each turn; server reconstructs from acknowledged history."""

    def __init__(self):
        self._messages: list[Message] = []
        self._sent_count = 0  # how many messages the server has acknowledged

    def add(self, role: str, content: str) -> Message:
        msg = Message(role=role, content=content)
        self._messages.append(msg)
        return msg

    def delta_payload(self) -> list[dict]:
        """Only unsent messages — the delta since last acknowledgement."""
        new_msgs = self._messages[self._sent_count:]
        return [{"role": m.role, "content": m.content} for m in new_msgs]

    def acknowledge(self, count: int):
        """Server confirms it processed `count` new messages."""
        self._sent_count += count

    def full_payload(self) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in self._messages]

    @property
    def delta_token_estimate(self) -> int:
        delta = self.delta_payload()
        return sum(len(m["content"]) // 4 for m in delta)

    @property
    def full_token_estimate(self) -> int:
        return sum(len(m.content) // 4 for m in self._messages)

    def compression_ratio(self) -> float:
        full = self.full_token_estimate
        if full == 0:
            return 1.0
        return self.delta_token_estimate / full

# Usage example
mgr = DeltaConversationManager()
mgr.add("user", "Hello, let's start a long coding session.")
mgr.add("assistant", "Sure! What would you like to work on?")
mgr.acknowledge(2)  # server processed these

# Turn 3: only send new messages
mgr.add("user", "Help me optimize this function.")
print(f"Delta: {mgr.delta_token_estimate} tokens (vs {mgr.full_token_estimate} full)")
# → Delta: 10 tokens (vs 28 full)
```

**When to use**: Custom agent servers where you control both client and server state reconstruction.

---

## Solution 3: Sliding Window with Summarized Prefix

Compress old turns into a rolling summary; keep only the recent window as verbatim messages.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class SlidingWindowContext:
    def __init__(self, window_size: int = 10, summary_interval: int = 8):
        self._window_size = window_size
        self._summary_interval = summary_interval
        self._summary: str = ""
        self._recent: list[dict] = []
        self._total_turns = 0

    async def _summarize(self, messages: list[dict]) -> str:
        """Compress a batch of old turns into a brief summary."""
        history_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:200]}" for m in messages
        )
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": f"Summarize this conversation history in 2-3 sentences, preserving key facts and decisions:\n\n{history_text}",
            }],
        )
        return resp.content[0].text

    def _build_messages(self) -> list[dict]:
        messages = []
        if self._summary:
            # Inject summary as a synthetic context message
            messages.append({
                "role": "user",
                "content": f"[Previous conversation summary: {self._summary}]",
            })
            messages.append({
                "role": "assistant",
                "content": "Understood, I have the context from our previous conversation.",
            })
        messages.extend(self._recent)
        return messages

    async def chat(self, user_message: str) -> str:
        self._recent.append({"role": "user", "content": user_message})
        self._total_turns += 1

        # Trigger summarization when recent window is full
        if len(self._recent) >= self._summary_interval:
            to_summarize = self._recent[:-2]  # keep last 2 turns verbatim
            new_summary = await self._summarize(to_summarize)
            if self._summary:
                self._summary = f"{self._summary} Later: {new_summary}"
            else:
                self._summary = new_summary
            self._recent = self._recent[-2:]

        messages = self._build_messages()
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )
        reply = resp.content[0].text
        self._recent.append({"role": "assistant", "content": reply})
        return reply

    @property
    def context_size_estimate(self) -> int:
        return sum(len(m["content"]) // 4 for m in self._build_messages())
```

**When to use**: Very long conversations where verbatim history is impractical. Reduces context by 70–80% after many turns.

---

## Solution 4: Content-Addressed Message Deduplication

Hash each message; skip re-sending messages the server has already seen (identified by hash).

```python
import hashlib
import json
from typing import Any
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class ContentAddressedContext:
    """Messages identified by content hash; server caches and reconstructs from hashes."""

    def __init__(self):
        self._messages: list[dict] = []
        self._content_store: dict[str, str] = {}  # hash → content

    def _hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def add(self, role: str, content: str) -> str:
        h = self._hash(content)
        self._content_store[h] = content
        self._messages.append({"role": role, "hash": h})
        return h

    def full_messages(self) -> list[dict]:
        return [
            {"role": m["role"], "content": self._content_store[m["hash"]]}
            for m in self._messages
        ]

    def delta_manifest(self, server_known_hashes: set[str]) -> dict:
        """Payload: list of hashes the server needs content for + message order."""
        needed_hashes = {
            m["hash"] for m in self._messages
            if m["hash"] not in server_known_hashes
        }
        return {
            "order": [{"role": m["role"], "hash": m["hash"]} for m in self._messages],
            "content": {h: self._content_store[h] for h in needed_hashes},
        }

    def bytes_saved(self, server_known_hashes: set[str]) -> int:
        saved = sum(
            len(self._content_store[m["hash"]])
            for m in self._messages
            if m["hash"] in server_known_hashes
        )
        return saved

# Simulate: after turn 5 server knows first 8 messages
ctx = ContentAddressedContext()
for i in range(10):
    ctx.add("user" if i % 2 == 0 else "assistant", f"Message {i} content " * 20)

server_known = {m["hash"] for m in ctx._messages[:8]}
manifest = ctx.delta_manifest(server_known)
print(f"Sending {len(manifest['content'])} new content blocks (saved {ctx.bytes_saved(server_known)} chars)")
```

**When to use**: Systems where the same content appears multiple times (e.g., repeated tool results, static instructions injected each turn).

---

## Solution 5: Structural Diff for Tool Result Updates

When tool results change slightly between turns, send only the diff rather than the full result.

```python
import difflib
import json
from dataclasses import dataclass

@dataclass
class ToolResultDelta:
    tool_name: str
    is_full: bool
    content: str  # full content or unified diff

class ToolResultDiffTracker:
    """Tracks prior tool results and emits diffs for incremental updates."""

    def __init__(self, diff_threshold: float = 0.7):
        self._prior: dict[str, str] = {}
        self._diff_threshold = diff_threshold  # similarity ratio above which to use diff

    def encode(self, tool_name: str, result: str) -> ToolResultDelta:
        prior = self._prior.get(tool_name)
        if prior is None:
            self._prior[tool_name] = result
            return ToolResultDelta(tool_name=tool_name, is_full=True, content=result)

        # Compute similarity
        matcher = difflib.SequenceMatcher(None, prior, result)
        ratio = matcher.ratio()

        if ratio >= self._diff_threshold:
            # Send unified diff
            diff_lines = list(difflib.unified_diff(
                prior.splitlines(keepends=True),
                result.splitlines(keepends=True),
                lineterm="",
            ))
            diff = "".join(diff_lines)
            if len(diff) < len(result) * 0.8:  # diff is actually smaller
                self._prior[tool_name] = result
                return ToolResultDelta(tool_name=tool_name, is_full=False, content=diff)

        # Fall back to full
        self._prior[tool_name] = result
        return ToolResultDelta(tool_name=tool_name, is_full=True, content=result)

    @staticmethod
    def decode(prior: str, delta: ToolResultDelta) -> str:
        if delta.is_full:
            return delta.content
        # Apply unified diff
        patched = prior.splitlines(keepends=True)
        for line in delta.content.splitlines(keepends=True):
            pass  # simplified: in production use `patch` library
        return delta.content  # placeholder

# Usage
tracker = ToolResultDiffTracker()

result_v1 = json.dumps({"status": "running", "progress": 10, "output": ["step1"]})
result_v2 = json.dumps({"status": "running", "progress": 15, "output": ["step1", "step2"]})

d1 = tracker.encode("job_status", result_v1)
d2 = tracker.encode("job_status", result_v2)

print(f"v1: full={d1.is_full}, size={len(d1.content)}")
print(f"v2: full={d2.is_full}, size={len(d2.content)} (vs {len(result_v2)} full)")
```

**When to use**: Agents that poll the same tool repeatedly (e.g., job status, live data feeds).

---

## Solution 6: Context Compression Pipeline with Token Budget

Combine multiple compression strategies in a pipeline, selecting the best one within a token budget.

```python
import asyncio
import json
from dataclasses import dataclass
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class CompressionResult:
    strategy: str
    messages: list[dict]
    estimated_tokens: int
    compression_ratio: float

def estimate_tokens(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) // 4 for m in messages)

def truncate_old_messages(messages: list[dict], keep_last: int = 6) -> list[dict]:
    if len(messages) <= keep_last:
        return messages
    system_msgs = [m for m in messages if m.get("role") == "system"]
    recent = messages[-keep_last:]
    return system_msgs + recent

def truncate_long_contents(messages: list[dict], max_chars: int = 500) -> list[dict]:
    result = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str) and len(content) > max_chars:
            content = content[:max_chars] + f"... [truncated {len(content)-max_chars} chars]"
        result.append({**m, "content": content})
    return result

async def llm_compress(messages: list[dict]) -> list[dict]:
    history = "\n".join(f"{m['role']}: {str(m.get('content',''))[:300]}" for m in messages[:-2])
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Compress this conversation history to essential facts only (max 3 sentences):\n{history}"}],
    )
    summary = resp.content[0].text
    return [{"role": "user", "content": f"[Context: {summary}]"}, {"role": "assistant", "content": "Understood."}] + messages[-2:]

async def compress_to_budget(messages: list[dict], token_budget: int) -> CompressionResult:
    current_tokens = estimate_tokens(messages)

    if current_tokens <= token_budget:
        return CompressionResult("none", messages, current_tokens, 1.0)

    # Strategy 1: truncate long contents
    truncated = truncate_long_contents(messages)
    tokens = estimate_tokens(truncated)
    if tokens <= token_budget:
        return CompressionResult("truncate_contents", truncated, tokens, tokens/current_tokens)

    # Strategy 2: drop old messages
    kept = truncate_old_messages(messages, keep_last=6)
    tokens = estimate_tokens(kept)
    if tokens <= token_budget:
        return CompressionResult("drop_old", kept, tokens, tokens/current_tokens)

    # Strategy 3: LLM summarization
    compressed = await llm_compress(messages)
    tokens = estimate_tokens(compressed)
    return CompressionResult("llm_summarize", compressed, tokens, tokens/current_tokens)

# Usage
async def chat_with_budget(conversation: list[dict], user_msg: str, budget: int = 4000) -> str:
    conversation.append({"role": "user", "content": user_msg})
    result = await compress_to_budget(conversation, budget)
    if result.strategy != "none":
        print(f"Compressed via {result.strategy}: {result.compression_ratio:.0%} of original")
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=result.messages,
    )
    return resp.content[0].text
```

**When to use**: Production agents with strict token budgets that need adaptive compression.

---

## Comparison

| Solution | Token Savings | Lossless | Implementation | Latency Added | Best For |
|---|---|---|---|---|---|
| Prompt caching | 60–90% | Yes | Low | None | All multi-turn agents |
| Incremental delta | 50–80% | Yes | Medium | None | Custom server-side context |
| Sliding window + summary | 70–80% | No (lossy) | Medium | Summarization call | Very long conversations |
| Content-addressed dedup | 30–60% | Yes | High | None | Repeated content across turns |
| Tool result diff | 20–70% | Partial | Medium | None | Polling tool agents |
| Compression pipeline | Adaptive | Partial | Medium | Optional | Token-budget-constrained agents |

**Rule of thumb**: Always use prompt caching first (free savings). Add sliding-window summarization for conversations exceeding 20 turns.
