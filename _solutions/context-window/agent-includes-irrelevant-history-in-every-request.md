---
layout: solution
title: "Agent Includes Irrelevant History in Every Request"
category: context-window
description: "Agent sends the entire conversation history with every API call — including turns that are irrelevant to the current question — inflating input token costs and crowding out useful context."
tags: [context-window, token-cost, history-pruning, relevance, summarisation, retrieval]
---

## Symptom

A 50-turn conversation about 5 different topics sends all 50 turns to the API for every new question — even when only 2 turns are relevant. Token counters reveal:

```
Prompt tokens: 18,450   ← 16,000 are irrelevant history
Response tokens: 312
Cost per request: $0.18  ← should be $0.02
```

Context fills up with old turns, forcing the agent to drop the most recent turns or system prompt.

## Root Cause

The agent maintains a flat `messages` list and appends every turn without filtering. All turns are sent verbatim on every request. There is no selection step to identify which history is actually relevant to the current query.

## Fix

---

### Option 1 — Sliding Window: Keep Only the Last N Turns

The simplest fix: truncate history to the last N turns before sending. Works well when recent context is almost always the relevant context.

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = "You are a helpful assistant."
MAX_HISTORY_TURNS = 6   # Keep last 6 message pairs (12 messages)

class SlidingWindowAgent:
    def __init__(self, window_turns: int = MAX_HISTORY_TURNS):
        self._all_messages: list[dict] = []
        self._window = window_turns * 2  # turns → individual messages

    def _context_window(self) -> list[dict]:
        """Return only the last N messages for the API call."""
        return self._all_messages[-self._window:]

    def chat(self, user_message: str) -> str:
        self._all_messages.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=self._context_window(),
        )

        reply = response.content[0].text
        self._all_messages.append({"role": "assistant", "content": reply})

        total = len(self._all_messages)
        sent = len(self._context_window())
        print(f"[History: {total} total, {sent} sent to API]")
        return reply

agent = SlidingWindowAgent(window_turns=4)

# Simulate a multi-topic conversation
topics = [
    "What is Python?",
    "How do I install pip?",
    "What is machine learning?",
    "What is a neural network?",
    "Back to Python — how do I create a virtual environment?",
    "What is gradient descent?",
    "How do I activate a venv on Windows?",    # Relevant: Python venv
]

for q in topics:
    print(f"\nUser: {q}")
    print(f"Agent: {agent.chat(q)[:80]}...")
```

**Expected Token Savings:** ~60% reduction for 20+ turn conversations
**Environment:** `pip install anthropic`

---

### Option 2 — Topic-Scoped History Buckets

Group messages by topic. When the user asks a question, detect its topic and send only that topic's history — not unrelated threads.

```python
import anthropic
from collections import defaultdict

client = anthropic.Anthropic()

TOPICS = ["python", "machine_learning", "databases", "security", "general"]

def detect_topic(text: str) -> str:
    text_lower = text.lower()
    if any(w in text_lower for w in ["python", "pip", "venv", "django", "flask", "import"]):
        return "python"
    if any(w in text_lower for w in ["ml", "machine learning", "neural", "model", "training", "dataset"]):
        return "machine_learning"
    if any(w in text_lower for w in ["sql", "database", "query", "postgres", "mysql", "index"]):
        return "databases"
    if any(w in text_lower for w in ["auth", "password", "token", "encrypt", "security", "ssl"]):
        return "security"
    return "general"

class TopicScopedAgent:
    def __init__(self, max_topic_turns: int = 8):
        self._buckets: dict[str, list[dict]] = defaultdict(list)
        self._max_turns = max_topic_turns * 2

    def chat(self, user_message: str) -> str:
        topic = detect_topic(user_message)
        bucket = self._buckets[topic]

        # Use only this topic's history
        history = bucket[-self._max_turns:]
        history = history + [{"role": "user", "content": user_message}]

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=f"You are an expert assistant. Current topic: {topic.replace('_', ' ')}.",
            messages=history,
        )

        reply = response.content[0].text

        # Store in this topic's bucket
        self._buckets[topic].append({"role": "user", "content": user_message})
        self._buckets[topic].append({"role": "assistant", "content": reply})

        total_msgs = sum(len(v) for v in self._buckets.values())
        print(f"[Topic: {topic} | Sent: {len(history)} | Total stored: {total_msgs}]")
        return reply

agent = TopicScopedAgent()

conversations = [
    ("What is a Python list?", "python"),
    ("What is a neural network?", "machine_learning"),
    ("How do SQL indexes work?", "databases"),
    ("How do I slice a Python list?", "python"),   # Should reuse python history
    ("What is gradient descent?", "machine_learning"),  # Should reuse ML history
]

for q, _ in conversations:
    print(f"\nUser: {q}")
    print(f"Agent: {agent.chat(q)[:80]}...")
```

**Expected Token Savings:** ~70% for multi-topic conversations; each request only sees one topic's history
**Environment:** `pip install anthropic`

---

### Option 3 — Embedding-Based Relevance Retrieval

Compute embeddings for all historical turns. At query time, retrieve only the top-K most semantically similar turns. Irrelevant history is excluded regardless of recency.

```python
import math
import anthropic

client = anthropic.Anthropic()

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

def embed(text: str) -> list[float]:
    """Get text embedding using Voyage AI via Anthropic SDK."""
    # In production: use a real embedding API (voyage-3-lite, text-embedding-3-small, etc.)
    # This is a placeholder that simulates embeddings
    words = set(text.lower().split())
    vocab = ["python", "machine", "learning", "sql", "database", "security", "neural", "model"]
    return [1.0 if w in words else 0.0 for w in vocab]

class RetrievalAugmentedHistory:
    def __init__(self, top_k: int = 4, always_include_last_n: int = 2):
        self._turns: list[dict] = []        # {role, content, embedding}
        self._top_k = top_k
        self._always_last = always_include_last_n * 2

    def _add_turn(self, role: str, content: str):
        embedding = embed(content)
        self._turns.append({"role": role, "content": content, "embedding": embedding})

    def _retrieve_relevant(self, query: str) -> list[dict]:
        if not self._turns:
            return []

        query_emb = embed(query)

        # Always include the most recent turns
        always_include = set(range(max(0, len(self._turns) - self._always_last), len(self._turns)))

        # Score remaining turns
        scored = []
        for i, turn in enumerate(self._turns):
            if i in always_include:
                continue
            sim = cosine_similarity(query_emb, turn["embedding"])
            scored.append((sim, i))

        scored.sort(reverse=True)
        top_indices = {i for _, i in scored[:self._top_k]}
        selected = top_indices | always_include

        # Return in original order, without embedding field
        return [
            {"role": t["role"], "content": t["content"]}
            for i, t in enumerate(self._turns)
            if i in selected
        ]

    def chat(self, user_message: str) -> str:
        relevant_history = self._retrieve_relevant(user_message)

        messages = relevant_history + [{"role": "user", "content": user_message}]

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="You are a helpful assistant.",
            messages=messages,
        )

        reply = response.content[0].text

        self._add_turn("user", user_message)
        self._add_turn("assistant", reply)

        total = len(self._turns)
        sent = len(messages)
        print(f"[History: {total} turns stored, {sent} sent (relevant + recent)]")
        return reply

agent = RetrievalAugmentedHistory(top_k=3, always_include_last_n=1)

history_data = [
    "What is Python?",
    "How does machine learning work?",
    "What is a database index?",
    "Explain neural networks.",
    "How do I write a Python class?",
    "What is SQL?",
]

for q in history_data:
    reply = agent.chat(q)

# Test retrieval — should pull Python-relevant turns, not ML or DB turns
print("\nRelevance test:")
result = agent.chat("How do I define methods in Python?")
print(result[:120])
```

**Expected Token Savings:** ~75% reduction when conversation spans many unrelated topics
**Environment:** `pip install anthropic`

---

### Option 4 — Periodic History Summarisation

Every N turns, compress older history into a single summary message. The summary represents all prior context in a fraction of the tokens.

```python
import anthropic

client = anthropic.Anthropic()

SUMMARISE_EVERY_N_TURNS = 6
SUMMARY_MAX_TOKENS = 256

def summarise_history(messages: list[dict]) -> str:
    if not messages:
        return ""

    conversation = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=SUMMARY_MAX_TOKENS,
        system=(
            "Compress this conversation into a concise summary. "
            "Preserve: key facts, decisions, user preferences, and open questions. "
            "Drop: pleasantries, filler, and redundant restatements. "
            "Output in bullet-point format."
        ),
        messages=[{"role": "user", "content": conversation}],
    )
    return response.content[0].text

class SummarisedHistoryAgent:
    def __init__(self):
        self._summary: str = ""
        self._recent: list[dict] = []  # Unsummarised recent turns
        self._turn_count: int = 0

    def _maybe_summarise(self):
        if len(self._recent) >= SUMMARISE_EVERY_N_TURNS * 2:
            print(f"[Summarising {len(self._recent)} messages → ~{SUMMARY_MAX_TOKENS} tokens]")
            new_summary = summarise_history(self._recent)
            if self._summary:
                self._summary = self._summary + "\n\n" + new_summary
            else:
                self._summary = new_summary
            self._recent = []

    def _build_messages(self, user_message: str) -> list[dict]:
        messages = []
        if self._summary:
            messages.append({
                "role": "user",
                "content": f"[Conversation summary so far]\n{self._summary}",
            })
            messages.append({
                "role": "assistant",
                "content": "Understood — I have the conversation context.",
            })
        messages.extend(self._recent)
        messages.append({"role": "user", "content": user_message})
        return messages

    def chat(self, user_message: str) -> str:
        self._maybe_summarise()

        messages = self._build_messages(user_message)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="You are a helpful assistant.",
            messages=messages,
        )

        reply = response.content[0].text
        self._recent.append({"role": "user", "content": user_message})
        self._recent.append({"role": "assistant", "content": reply})
        self._turn_count += 1

        total_tokens_est = (
            len((self._summary or "").split()) * 1.3
            + sum(len(m["content"].split()) * 1.3 for m in self._recent)
        )
        print(f"[Turn {self._turn_count} | Est. prompt tokens: {total_tokens_est:.0f}]")
        return reply

agent = SummarisedHistoryAgent()
questions = [f"Question {i}: Tell me about topic {i % 3}" for i in range(15)]

for q in questions:
    agent.chat(q)
```

**Expected Token Savings:** ~65% after first summarisation cycle; compounds as conversation grows
**Environment:** `pip install anthropic`

---

### Option 5 — Explicit Turn Tagging with Selective Recall

Tag each turn with a relevance label when storing. At recall time, only include turns tagged as relevant to the current question's category.

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class TaggedTurn:
    role: str
    content: str
    tags: set[str]
    is_permanent: bool = False  # Always include (e.g., user preferences)

KEYWORD_TAGS = {
    "python": {"python", "pip", "venv", "django", "flask"},
    "databases": {"sql", "postgres", "mysql", "mongodb", "index", "query"},
    "ml": {"machine learning", "neural", "model", "training", "gradient"},
    "security": {"auth", "token", "password", "oauth", "jwt", "ssl"},
    "user_pref": {"prefer", "always", "never", "remember", "my setup"},
}

def tag_message(content: str) -> set[str]:
    content_lower = content.lower()
    tags: set[str] = set()
    for tag, keywords in KEYWORD_TAGS.items():
        if any(kw in content_lower for kw in keywords):
            tags.add(tag)
    return tags or {"general"}

class TaggedHistoryAgent:
    def __init__(self, max_tagged_turns: int = 6):
        self._turns: list[TaggedTurn] = []
        self._max_tagged = max_tagged_turns * 2

    def _select_messages(self, query: str) -> list[dict]:
        query_tags = tag_message(query)
        selected: list[tuple[int, TaggedTurn]] = []

        for i, turn in enumerate(self._turns):
            if turn.is_permanent:
                selected.append((i, turn))
                continue
            if turn.tags & query_tags:
                selected.append((i, turn))

        # Limit to max_tagged, always keeping most recent
        if len(selected) > self._max_tagged:
            selected = selected[-self._max_tagged:]

        return [{"role": t.role, "content": t.content} for _, t in selected]

    def chat(self, user_message: str) -> str:
        query_tags = tag_message(user_message)
        selected = self._select_messages(user_message)

        messages = selected + [{"role": "user", "content": user_message}]

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="You are a helpful assistant.",
            messages=messages,
        )

        reply = response.content[0].text

        # Check if user is setting a preference
        is_perm = "user_pref" in query_tags

        self._turns.append(TaggedTurn("user", user_message, query_tags, is_perm))
        self._turns.append(TaggedTurn("assistant", reply, query_tags, is_perm))

        print(f"[Tags: {query_tags} | History: {len(self._turns)} | Sent: {len(messages)}]")
        return reply

agent = TaggedHistoryAgent()

agent.chat("I prefer verbose explanations, always give examples.")   # Tagged permanent
agent.chat("What is Python?")
agent.chat("How does SQL indexing work?")
agent.chat("What is machine learning?")
agent.chat("How do I write a Python decorator?")  # Should recall Python turns + preference
```

**Expected Token Savings:** ~70% for topic-diverse conversations
**Environment:** `pip install anthropic`

---

### Option 6 — Prompt-Cached System Summary with Minimal Live History

Store a growing conversation summary in the system prompt with `cache_control`. Send only the last 2 live turns as messages. The model "knows" earlier context through the cached summary — at negligible cost after the first call.

```python
import anthropic

client = anthropic.Anthropic()

class CachedSummaryAgent:
    def __init__(self, update_summary_every_n: int = 4):
        self._live_turns: list[dict] = []
        self._summary: str = "No prior conversation."
        self._turn_count: int = 0
        self._update_every = update_summary_every_n
        self._max_live = 4  # Messages (2 turns)

    def _update_summary(self):
        if not self._live_turns:
            return

        conversation = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in self._live_turns
        )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="Maintain a growing conversation summary. Merge new content into the existing summary. Keep it under 300 words.",
            messages=[{
                "role": "user",
                "content": f"Existing summary:\n{self._summary}\n\nNew turns to add:\n{conversation}",
            }],
        )
        self._summary = response.content[0].text
        self._live_turns = []
        print(f"[Summary updated: {len(self._summary.split())} words]")

    def chat(self, user_message: str) -> str:
        self._turn_count += 1

        # Periodically refresh summary
        if self._turn_count % self._update_every == 0:
            self._update_summary()

        # Build request: cached summary as system + last N live messages
        live = self._live_turns[-self._max_live:]
        messages = live + [{"role": "user", "content": user_message}]

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": (
                        "You are a helpful assistant.\n\n"
                        f"CONVERSATION HISTORY SUMMARY:\n{self._summary}"
                    ),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        reply = response.content[0].text
        self._live_turns.append({"role": "user", "content": user_message})
        self._live_turns.append({"role": "assistant", "content": reply})

        live_count = len(messages)
        print(f"[Turn {self._turn_count} | Live: {live_count} msgs | Summary cached]")
        return reply

agent = CachedSummaryAgent(update_summary_every_n=3)

for i in range(12):
    q = f"Question {i + 1} about topic {'ABC'[i % 3]}"
    reply = agent.chat(q)
    print(f"Q{i+1}: {reply[:60]}...")
```

**Expected Token Savings:** ~80% after first summary; cached system prompt costs 0 tokens on repeat calls
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Strategy | Token Savings | Preserves Old Context | Best For |
|--------|----------|---------------|----------------------|----------|
| Sliding Window | Recency cutoff | ~60% | No | Single-topic chats |
| Topic Buckets | Topic isolation | ~70% | Partial (by topic) | Multi-topic assistants |
| Embedding Retrieval | Semantic similarity | ~75% | Yes | Diverse long conversations |
| Periodic Summarisation | Lossy compression | ~65% | Partial (summary) | Long-running agents |
| Turn Tagging | Keyword relevance | ~70% | Yes | Structured domain agents |
| Cached Summary | Summary + cache | ~80% | Partial (summary) | High-throughput production |

**Recommended starting point:** Option 1 (Sliding Window) as an immediate fix; Option 4 (Summarisation) or Option 6 (Cached Summary) for production systems with long conversations.
