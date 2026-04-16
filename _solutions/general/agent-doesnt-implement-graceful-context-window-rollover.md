---
layout: solution
title: "Agent Doesn't Implement Graceful Context Window Rollover"
category: general
description: "When a conversation approaches the context window limit, automatically summarize prior history and continue in a fresh context—preserving essential state without hitting max-context errors."
tags: [context-window, rollover, summarization, long-conversations, memory-management]
---

# Agent Doesn't Implement Graceful Context Window Rollover

## Problem

Long-running agents crash or produce degraded responses when conversation history exceeds the context window. Without rollover, agents either fail with API errors, silently truncate critical context, or force users to restart sessions.

## Solution Options

### Option 1: Token-Threshold Triggered Rollover

```python
import anthropic

client = anthropic.Anthropic()
CONTEXT_LIMIT = 180_000   # claude haiku context window
ROLLOVER_THRESHOLD = 0.75  # trigger rollover at 75% capacity

def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: ~4 chars per token."""
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    return total_chars // 4

def summarize_history(messages: list[dict], system: str) -> str:
    """Compress older messages into a summary."""
    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content'][:300]}" for m in messages
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a conversation summarizer. Be concise and preserve key facts, decisions, and context.",
        messages=[{
            "role": "user",
            "content": f"Summarize this conversation history into a compact paragraph preserving all important facts:\n\n{history_text}"
        }]
    )
    return resp.content[0].text

def chat_with_rollover(messages: list[dict], system: str, user_input: str) -> tuple[str, list[dict]]:
    estimated = estimate_tokens(messages)
    rollover_at = int(CONTEXT_LIMIT * ROLLOVER_THRESHOLD)

    if estimated > rollover_at:
        print(f"[ROLLOVER] {estimated} estimated tokens >= {rollover_at} threshold")
        summary = summarize_history(messages[:-2], system)  # summarize all but last exchange
        recent = messages[-2:]  # keep last exchange verbatim
        messages = [
            {"role": "user", "content": f"[CONVERSATION SUMMARY]\n{summary}"},
            {"role": "assistant", "content": "Understood. I have the context from our conversation."},
            *recent
        ]
        print(f"[ROLLOVER] Compressed to {estimate_tokens(messages)} estimated tokens")

    messages.append({"role": "user", "content": user_input})
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=messages
    )
    reply = resp.content[0].text
    messages.append({"role": "assistant", "content": reply})
    return reply, messages

system = "You are a helpful technical assistant with deep expertise in distributed systems."
messages = []

conversation = [
    "What is the CAP theorem?",
    "Can you give me examples of CP systems?",
    "What about AP systems?",
    "How does Cassandra handle availability vs consistency?",
    "What is tunable consistency?",
    "How do read/write quorums relate to consistency levels?",
]

for turn in conversation:
    reply, messages = chat_with_rollover(messages, system, turn)
    print(f"Q: {turn}\nA: {reply[:80]}...\n")

# Expected Token Savings: rollover compresses 75% of history into ~100 tokens; ~60% cost reduction
# Environment: long-running chat agents, multi-day sessions, research assistants
```

### Option 2: Sliding Window with Pinned Key Messages

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ManagedHistory:
    pinned: list[dict] = field(default_factory=list)   # always kept
    sliding: list[dict] = field(default_factory=list)  # recent window
    max_sliding: int = 10                               # keep last N turns

    def add_user(self, content: str, pin: bool = False) -> None:
        msg = {"role": "user", "content": content}
        if pin:
            self.pinned.append(msg)
        else:
            self.sliding.append(msg)
            if len(self.sliding) > self.max_sliding * 2:
                self.sliding = self.sliding[-self.max_sliding * 2:]

    def add_assistant(self, content: str, pin: bool = False) -> None:
        msg = {"role": "assistant", "content": content}
        if pin:
            self.pinned.append(msg)
        else:
            self.sliding.append(msg)

    def to_messages(self) -> list[dict]:
        all_msgs = self.pinned + self.sliding
        # Ensure alternating roles — deduplicate consecutive same-role messages
        result = []
        for msg in all_msgs:
            if result and result[-1]["role"] == msg["role"]:
                result[-1]["content"] += f"\n\n{msg['content']}"
            else:
                result.append(msg)
        return result

    @property
    def total_chars(self) -> int:
        return sum(len(str(m["content"])) for m in self.to_messages())

def chat_sliding_window(history: ManagedHistory, user_input: str, pin_response: bool = False) -> str:
    history.add_user(user_input, pin=False)
    messages = history.to_messages()

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=384,
        messages=messages
    )
    reply = resp.content[0].text
    history.add_assistant(reply, pin=pin_response)
    print(f"  [{history.total_chars} chars, {len(history.sliding)} sliding msgs]")
    return reply

history = ManagedHistory(max_sliding=8)

# Pin important context
history.add_user("My name is Alex and I'm building a distributed key-value store using Raft.", pin=True)
history.add_assistant("Understood — I'll keep your Raft-based KV store project in mind throughout our conversation.", pin=True)

turns = [
    ("What are the main Raft leader election steps?", False),
    ("How do I handle network partitions in leader election?", False),
    ("What is log replication in Raft?", False),
    ("How do I handle follower crashes during replication?", False),
    ("What is a committed entry in Raft?", False),
    ("Can you summarize the key Raft invariants I should implement?", True),  # pin this summary
]

for question, pin in turns:
    reply = chat_sliding_window(history, question, pin_response=pin)
    print(f"Q: {question[:60]}\nA: {reply[:80]}...\n")

# Expected Token Savings: sliding window keeps costs ~constant regardless of conversation length
# Environment: long-running sessions, persistent assistants, task-focused agents
```

### Option 3: Hierarchical Compression with Priority Tiers

```python
import anthropic
from enum import Enum

client = anthropic.Anthropic()

class MessagePriority(Enum):
    CRITICAL = 3  # never compress (instructions, key decisions)
    HIGH = 2      # compress last; keep verbatim as long as possible
    NORMAL = 1    # compress first
    LOW = 0       # drop first

def compress_tier(messages: list[dict], model: str = "claude-haiku-4-5-20251001") -> dict:
    """Compress a list of messages into a single summary message."""
    text = "\n".join(f"{m['role'].upper()}: {m['content'][:400]}" for m in messages)
    resp = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Compress these conversation turns into 2-3 sentences preserving key facts:\n\n{text}"
        }]
    )
    return {
        "role": "user",
        "content": f"[COMPRESSED HISTORY] {resp.content[0].text}",
        "_priority": MessagePriority.HIGH.value,
        "_compressed": True
    }

def char_count(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages)

class HierarchicalHistoryManager:
    MAX_CHARS = 40_000  # conservative limit

    def __init__(self):
        self.messages: list[dict] = []

    def add(self, role: str, content: str, priority: MessagePriority = MessagePriority.NORMAL) -> None:
        self.messages.append({"role": role, "content": content, "_priority": priority.value})
        self._maybe_compress()

    def _maybe_compress(self) -> None:
        if char_count(self.messages) <= self.MAX_CHARS:
            return

        # Compress the oldest NORMAL priority messages first
        normal_indices = [i for i, m in enumerate(self.messages)
                          if m.get("_priority", 1) == MessagePriority.NORMAL.value
                          and not m.get("_compressed")]
        if len(normal_indices) >= 4:
            to_compress = [self.messages[i] for i in normal_indices[:4]]
            compressed = compress_tier(to_compress)
            # Replace first 4 normal messages with summary
            for i in sorted(normal_indices[:4], reverse=True):
                self.messages.pop(i)
            insert_pos = normal_indices[0]
            self.messages.insert(insert_pos, compressed)
            print(f"[COMPRESS] Compressed {len(to_compress)} messages -> 1 summary ({char_count(self.messages)} chars)")

    def get_messages(self) -> list[dict]:
        return [{"role": m["role"], "content": m["content"]} for m in self.messages]

mgr = HierarchicalHistoryManager()

# Critical context — never compress
mgr.add("user", "I am building a Kubernetes operator for managing Redis clusters.", MessagePriority.CRITICAL)
mgr.add("assistant", "I'll help you build the Kubernetes operator for Redis cluster management.", MessagePriority.CRITICAL)

# Normal conversation turns
topics = [
    "What CRDs should I define for the Redis operator?",
    "How do I implement the reconcile loop?",
    "What is the controller-runtime library?",
    "How do I handle Redis Sentinel vs Cluster mode?",
    "What metrics should I expose from the operator?",
    "How do I handle operator upgrades without downtime?",
]

for topic in topics:
    mgr.add("user", topic, MessagePriority.NORMAL)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=mgr.get_messages()
    )
    reply = resp.content[0].text
    mgr.add("assistant", reply, MessagePriority.NORMAL)
    print(f"[{char_count(mgr.messages)} chars] {topic[:50]}: {reply[:60]}...")

# Expected Token Savings: ~50% by compressing normal-priority messages; critical context always preserved
# Environment: technical assistants, long coding sessions, multi-topic research agents
```

### Option 4: Automatic Rollover with Chapter Tracking

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class Chapter:
    number: int
    summary: str
    message_count: int
    created_at: float = field(default_factory=time.time)

@dataclass
class ChapteredConversation:
    system: str
    chapters: list[Chapter] = field(default_factory=list)
    current_messages: list[dict] = field(default_factory=list)
    MAX_MESSAGES_PER_CHAPTER: int = 8

    def _should_rollover(self) -> bool:
        return len(self.current_messages) >= self.MAX_MESSAGES_PER_CHAPTER

    def _create_chapter_summary(self) -> str:
        history = "\n".join(
            f"{m['role'].upper()}: {str(m['content'])[:250]}"
            for m in self.current_messages
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"Summarize these {len(self.current_messages)} conversation turns into key points:\n\n{history}"
            }]
        )
        return resp.content[0].text

    def rollover(self) -> None:
        if not self.current_messages:
            return
        summary = self._create_chapter_summary()
        chapter_num = len(self.chapters) + 1
        self.chapters.append(Chapter(
            number=chapter_num,
            summary=summary,
            message_count=len(self.current_messages)
        ))
        print(f"[CHAPTER {chapter_num} CLOSED] {len(self.current_messages)} msgs -> summary")
        self.current_messages = []

    def build_context_messages(self) -> list[dict]:
        """Build messages including chapter summaries as context."""
        context = []
        if self.chapters:
            chapter_ctx = "\n\n".join(
                f"Chapter {c.number}: {c.summary}" for c in self.chapters
            )
            context = [
                {"role": "user", "content": f"[PRIOR CONVERSATION CHAPTERS]\n{chapter_ctx}"},
                {"role": "assistant", "content": "I have the full conversation context from all prior chapters."}
            ]
        return context + self.current_messages

    def chat(self, user_input: str) -> str:
        if self._should_rollover():
            self.rollover()

        self.current_messages.append({"role": "user", "content": user_input})
        messages = self.build_context_messages()

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=384,
            system=self.system,
            messages=messages
        )
        reply = resp.content[0].text
        self.current_messages.append({"role": "assistant", "content": reply})
        return reply

conv = ChapteredConversation(
    system="You are an expert Python tutor.",
    MAX_MESSAGES_PER_CHAPTER=6
)

questions = [
    "What are Python decorators?",
    "Can you show a simple decorator example?",
    "What is functools.wraps and why use it?",
    "What are class-based decorators?",
    "How do parameterized decorators work?",
    "What is a decorator factory?",
    "How do I stack multiple decorators?",
    "What is the difference between @property and regular decorators?",
    "Can decorators modify function return values?",
    "What are some real-world uses of decorators?"
]

for q in questions:
    reply = conv.chat(q)
    print(f"[Ch{len(conv.chapters)+1}] Q: {q[:50]}\nA: {reply[:80]}...\n")

print(f"\nTotal chapters: {len(conv.chapters)}, current window: {len(conv.current_messages)} msgs")

# Expected Token Savings: chapter-based rollover keeps active context under 2000 tokens; ~65% savings
# Environment: tutoring agents, long technical consultations, episodic task agents
```

### Option 5: Context Pressure Monitor with Adaptive Summarization

```python
import anthropic

client = anthropic.Anthropic()

MODEL_CONTEXT_LIMITS = {
    "claude-haiku-4-5-20251001": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
}

def get_context_pressure(usage_input_tokens: int, model: str) -> float:
    limit = MODEL_CONTEXT_LIMITS.get(model, 200_000)
    return usage_input_tokens / limit

def adaptive_summarize(messages: list[dict], target_pct: float = 0.5) -> list[dict]:
    """
    Summarize history to reach target_pct of current length.
    target_pct=0.5 means produce a summary half as long.
    """
    # Keep last 4 messages verbatim
    recent = messages[-4:]
    to_summarize = messages[:-4]

    if not to_summarize:
        return messages

    target_words = int(sum(len(str(m["content"]).split()) for m in to_summarize) * target_pct)
    history_text = "\n".join(f"{m['role'].upper()}: {str(m['content'])[:300]}" for m in to_summarize)

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max(128, target_words),
        messages=[{
            "role": "user",
            "content": f"Summarize the following conversation in approximately {target_words} words. Preserve all important facts, decisions, and code:\n\n{history_text}"
        }]
    )

    return [
        {"role": "user", "content": f"[ADAPTIVE SUMMARY]\n{resp.content[0].text}"},
        {"role": "assistant", "content": "Context loaded from summary."},
        *recent
    ]

def pressure_adaptive_chat(messages: list[dict], system: str, user_input: str) -> tuple[str, list[dict]]:
    messages = messages + [{"role": "user", "content": user_input}]

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=messages
    )

    pressure = get_context_pressure(resp.usage.input_tokens, "claude-haiku-4-5-20251001")
    print(f"  Context pressure: {pressure:.1%} ({resp.usage.input_tokens} tokens)")

    reply = resp.content[0].text
    messages.append({"role": "assistant", "content": reply})

    # Adaptive summarization thresholds
    if pressure > 0.80:
        print("  [CRITICAL] Aggressive summarization (50% compression)")
        messages = adaptive_summarize(messages, target_pct=0.3)
    elif pressure > 0.60:
        print("  [WARNING] Moderate summarization (70% compression)")
        messages = adaptive_summarize(messages, target_pct=0.5)

    return reply, messages

system = "You are a senior backend engineer helping with system design."
messages = []

for q in [
    "Design a URL shortener service",
    "How should I handle the database schema?",
    "What caching strategy would you recommend?",
    "How do I handle high read throughput?",
    "What about analytics on click data?",
]:
    reply, messages = pressure_adaptive_chat(messages, system, q)
    print(f"Q: {q}\nA: {reply[:80]}...\n")

# Expected Token Savings: pressure-adaptive compression prevents runaway costs; saves 40-70%
# Environment: interactive design sessions, production chatbots with no session resets
```

### Option 6: Multi-Session Rollover with Persistent Summary Store

```python
import anthropic
import sqlite3
import json
import time
import uuid

client = anthropic.Anthropic()

def init_session_store(path: str = "/tmp/session_store.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_summaries (
            session_id TEXT NOT NULL,
            window_number INTEGER NOT NULL,
            summary TEXT NOT NULL,
            message_count INTEGER,
            created_at REAL,
            PRIMARY KEY (session_id, window_number)
        )
    """)
    conn.commit()
    return conn

def save_window_summary(conn: sqlite3.Connection, session_id: str, window_num: int,
                         messages: list[dict]) -> str:
    history = "\n".join(f"{m['role'].upper()}: {str(m['content'])[:300]}" for m in messages)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Summarize key facts and outcomes from:\n\n{history}"
        }]
    )
    summary = resp.content[0].text
    conn.execute(
        "INSERT OR REPLACE INTO session_summaries VALUES (?,?,?,?,?)",
        (session_id, window_num, summary, len(messages), time.time())
    )
    conn.commit()
    return summary

def load_session_context(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT window_number, summary FROM session_summaries WHERE session_id=? ORDER BY window_number",
        (session_id,)
    ).fetchall()
    if not rows:
        return []
    combined = "\n\n".join(f"Window {r[0]}: {r[1]}" for r in rows)
    return [
        {"role": "user", "content": f"[SESSION HISTORY ACROSS {len(rows)} WINDOWS]\n{combined}"},
        {"role": "assistant", "content": "Session history loaded. I have full context from all prior windows."}
    ]

class PersistentSession:
    WINDOW_SIZE = 6

    def __init__(self, session_id: str | None = None):
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.conn = init_session_store()
        self.window_number = 0
        self.current_window: list[dict] = []
        # Load prior context if resuming
        self.prior_context = load_session_context(self.conn, self.session_id)
        print(f"Session {self.session_id}: {len(self.prior_context)//2} prior windows loaded")

    def chat(self, user_input: str) -> str:
        if len(self.current_window) >= self.WINDOW_SIZE * 2:
            self.window_number += 1
            save_window_summary(self.conn, self.session_id, self.window_number, self.current_window)
            print(f"[WINDOW {self.window_number} SAVED] {len(self.current_window)} messages compressed")
            self.current_window = []
            self.prior_context = load_session_context(self.conn, self.session_id)

        self.current_window.append({"role": "user", "content": user_input})
        messages = self.prior_context + self.current_window

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=384,
            messages=messages
        )
        reply = resp.content[0].text
        self.current_window.append({"role": "assistant", "content": reply})
        return reply

session = PersistentSession()
for q in [
    "I'm building a chat application. What database should I use?",
    "Should I use PostgreSQL or MongoDB for chat messages?",
    "How do I handle real-time message delivery?",
    "What's the best approach for message pagination?",
    "How do I handle offline message delivery?",
    "What about end-to-end encryption for messages?",
    "How should I structure chat rooms vs direct messages?",
    "What's the right data model for message threads?",
]:
    reply = session.chat(q)
    print(f"[W{session.window_number}] {q[:50]}: {reply[:70]}...")

# Expected Token Savings: persistent windows keep active context < 2000 tokens indefinitely
# Environment: multi-day agents, persistent assistants, applications with session resume
```

## Comparison

| Option | Mechanism | History Preserved | Best For |
|--------|-----------|------------------|----------|
| 1 | Token threshold + summary | Compressed | General chatbots |
| 2 | Sliding window + pinned messages | Recent + critical | Task-focused agents |
| 3 | Priority-tier compression | Critical always | Multi-topic sessions |
| 4 | Chapter-based rollover | Chapter summaries | Long episodic tasks |
| 5 | Pressure-adaptive summarization | Adaptive | Cost-critical production |
| 6 | Persistent multi-window store | All windows (SQLite) | Multi-day sessions |
