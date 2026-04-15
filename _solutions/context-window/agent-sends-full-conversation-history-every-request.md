---
layout: solution
title: "Agent Sends Full Conversation History Every Request"
category: context-window
description: "Every API call includes the entire conversation from message 1. Costs compound quadratically, latency grows linearly, and the context window eventually overflows."
tags: [context-window, token-cost, conversation-history, summarization, trimming, sliding-window]
---

## Symptom

A 50-turn conversation sends 50 × (average turn size) tokens on every single request. Turn 1 costs 200 tokens. Turn 50 costs 10,000 tokens for the same one-line user message. Anthropic's context limit hits unexpectedly, the agent crashes mid-session, and your bill scales with conversation length squared rather than linearly.

## Root Cause

The naive pattern appends every message to a list and passes the entire list to `messages=`. There is no trimming, summarization, or window management. Every turn re-sends all prior context even when it is no longer relevant to the current question.

```python
# Anti-pattern — unbounded history
history = []

def chat(user_message: str) -> str:
    history.append({"role": "user", "content": user_message})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=history,  # grows forever
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply
```

## Fix

---

### Option 1: Fixed Sliding Window (Last N Turns)

Keep only the most recent K message pairs. Simple, zero latency overhead.

```python
from collections import deque
import anthropic

client = anthropic.Anthropic()

class SlidingWindowHistory:
    """
    Keeps the last `max_pairs` user/assistant pairs.
    Always retains the system prompt separately.
    """
    def __init__(self, max_pairs: int = 10):
        self.max_pairs = max_pairs
        self._pairs: deque[tuple[dict, dict]] = deque(maxlen=max_pairs)
        self.system: str = ""

    def add(self, user_msg: str, assistant_msg: str):
        self._pairs.append((
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": assistant_msg},
        ))

    def to_messages(self) -> list[dict]:
        messages = []
        for user, asst in self._pairs:
            messages.append(user)
            messages.append(asst)
        return messages

    def token_estimate(self) -> int:
        """Rough estimate: 4 chars ≈ 1 token."""
        total_chars = sum(
            len(u["content"]) + len(a["content"])
            for u, a in self._pairs
        )
        return total_chars // 4


history = SlidingWindowHistory(max_pairs=10)
history.system = "You are a helpful assistant."


def chat(user_message: str) -> str:
    messages = history.to_messages()
    messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=history.system,
        messages=messages,
    )
    reply = response.content[0].text
    history.add(user_message, reply)

    est = history.token_estimate()
    print(f"  Context estimate: ~{est} tokens ({len(history._pairs)} pairs)")
    return reply


# Usage
turns = [
    "What is the capital of France?",
    "And of Germany?",
    "What language do they speak there?",
    "Translate 'hello' into that language.",
]
for turn in turns:
    print(f"User: {turn}")
    print(f"Agent: {chat(turn)}\n")
```

**Expected Token Savings**: 10-turn window caps context at ~2,000 tokens regardless of session length. 50-turn session saves ~80% vs unbounded.
**Environment**: In-memory, no dependencies. Stateless between restarts.

---

### Option 2: Token-Budget Trimmer with Recency Bias

Count actual tokens using the API's usage field. Drop oldest pairs when budget is exceeded.

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class MessagePair:
    user: str
    assistant: str
    token_count: int = 0  # set after first use


class TokenBudgetHistory:
    """
    Keeps as many recent pairs as fit within `max_context_tokens`.
    Oldest pairs are evicted first.
    """
    def __init__(self, max_context_tokens: int = 40_000):
        self.max_context_tokens = max_context_tokens
        self._pairs: list[MessagePair] = []
        self.system = ""

    def add(self, user: str, assistant: str, tokens_used: int):
        pair = MessagePair(user=user, assistant=assistant, token_count=tokens_used)
        self._pairs.append(pair)
        self._evict_if_needed()

    def _evict_if_needed(self):
        while self._total_tokens() > self.max_context_tokens and len(self._pairs) > 1:
            evicted = self._pairs.pop(0)
            print(f"  [evicted oldest pair, freed ~{evicted.token_count} tokens]")

    def _total_tokens(self) -> int:
        return sum(p.token_count for p in self._pairs)

    def to_messages(self) -> list[dict]:
        msgs = []
        for p in self._pairs:
            msgs.append({"role": "user",      "content": p.user})
            msgs.append({"role": "assistant", "content": p.assistant})
        return msgs


history = TokenBudgetHistory(max_context_tokens=8_000)
history.system = "You are a concise assistant."


def chat(user_message: str) -> str:
    messages = history.to_messages()
    messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=history.system,
        messages=messages,
    )

    reply = response.content[0].text
    tokens = response.usage.input_tokens + response.usage.output_tokens
    history.add(user_message, reply, tokens_used=tokens)

    print(f"  Total context tokens: {history._total_tokens()} / {history.max_context_tokens}")
    return reply


for msg in ["Tell me about Python.", "What are its main uses?", "Compare it to Go."]:
    print(f"User: {msg}")
    print(f"Agent: {chat(msg)}\n")
```

**Expected Token Savings**: Hard cap at 40K tokens regardless of session length. Each turn pays only for what fits, not for the full history.
**Environment**: In-memory. Token counts are tracked from actual API responses.

---

### Option 3: Rolling Summary Compression

When history exceeds a threshold, summarize old turns with a cheap Haiku call, replace them with the summary.

```python
import anthropic

client = anthropic.Anthropic()

SUMMARY_THRESHOLD_PAIRS = 15   # compress when history exceeds this
KEEP_RECENT_PAIRS = 5          # keep this many recent pairs verbatim


class SummarizingHistory:
    def __init__(self):
        self._pairs: list[tuple[str, str]] = []
        self._summary: str = ""  # compressed representation of old turns
        self.system = ""

    def add(self, user: str, assistant: str):
        self._pairs.append((user, assistant))
        if len(self._pairs) > SUMMARY_THRESHOLD_PAIRS:
            self._compress()

    def _compress(self):
        """Summarize all but the most recent pairs."""
        to_compress = self._pairs[:-KEEP_RECENT_PAIRS]
        self._pairs = self._pairs[-KEEP_RECENT_PAIRS:]

        # Build text to summarize
        history_text = "\n".join(
            f"User: {u}\nAssistant: {a}"
            for u, a in to_compress
        )
        if self._summary:
            history_text = f"[Previous summary]\n{self._summary}\n\n[New turns]\n{history_text}"

        summary_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    "Summarize this conversation history in 3-5 bullet points, "
                    "preserving key facts, decisions, and context:\n\n"
                    + history_text
                ),
            }],
        )
        self._summary = summary_response.content[0].text
        print(f"  [Compressed {len(to_compress)} pairs into summary]")

    def to_messages(self) -> list[dict]:
        msgs = []
        # Inject summary as a system-level context note
        if self._summary:
            msgs.append({
                "role": "user",
                "content": f"[Conversation summary so far]\n{self._summary}",
            })
            msgs.append({
                "role": "assistant",
                "content": "Understood. I'll use this context for our continued conversation.",
            })
        for user, asst in self._pairs:
            msgs.append({"role": "user",      "content": user})
            msgs.append({"role": "assistant", "content": asst})
        return msgs


history = SummarizingHistory()
history.system = "You are a helpful assistant."


def chat(user_message: str) -> str:
    messages = history.to_messages()
    messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=history.system,
        messages=messages,
    )
    reply = response.content[0].text
    history.add(user_message, reply)
    return reply


# Simulate a long conversation
topics = [
    "What is machine learning?", "Explain supervised learning.",
    "What is gradient descent?", "What is overfitting?",
    "How does regularization help?", "What is a neural network?",
    "Explain backpropagation.", "What are CNNs?",
    "What are transformers?", "What is attention mechanism?",
    "How does BERT work?", "What is GPT?",
    "How are LLMs trained?", "What is RLHF?",
    "Summarize everything we discussed.",
]
for topic in topics:
    print(f"User: {topic}")
    reply = chat(topic)
    print(f"Agent: {reply[:80]}...\n")
```

**Expected Token Savings**: 15-turn sessions compress to ~512-token summaries. A 100-turn conversation costs ~60% less than sending raw history.
**Environment**: Two-model setup: Haiku for summarization, Sonnet for main chat.

---

### Option 4: Selective History Retrieval (Embedding-Based)

Store all messages but retrieve only the K most relevant to the current query using cosine similarity on embeddings.

```python
import asyncio
import numpy as np
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class StoredMessage:
    role: str
    content: str
    embedding: list[float] = field(default_factory=list)
    pair_id: int = 0  # links user/assistant pairs


async def embed(text: str) -> list[float]:
    """Use a lightweight embedding model via Voyage or local model."""
    # Fallback: use simple TF-IDF-like bag-of-words for demo
    # In production: replace with voyage-3-lite or local sentence-transformers
    words = set(text.lower().split())
    # Create a sparse 512-dim vector based on hash of each word
    vec = np.zeros(512)
    for word in words:
        idx = hash(word) % 512
        vec[idx] += 1.0
    norm = np.linalg.norm(vec)
    return (vec / norm if norm > 0 else vec).tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.array(a), np.array(b)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / denom) if denom > 0 else 0.0


class EmbeddingHistory:
    def __init__(self, top_k: int = 6, always_keep_recent: int = 2):
        self._messages: list[StoredMessage] = []
        self._pair_counter = 0
        self.top_k = top_k
        self.always_keep_recent = always_keep_recent

    async def add(self, user: str, assistant: str):
        pid = self._pair_counter
        self._pair_counter += 1
        ue = await embed(user)
        ae = await embed(assistant)
        self._messages.append(StoredMessage("user",      user,      ue, pid))
        self._messages.append(StoredMessage("assistant", assistant, ae, pid))

    async def retrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        k = top_k or self.top_k
        qe = await embed(query)

        # Always include the most recent `always_keep_recent` pairs
        recent_pair_ids = set()
        seen_pids = []
        for msg in reversed(self._messages):
            if msg.pair_id not in seen_pids:
                seen_pids.append(msg.pair_id)
            if len(seen_pids) >= self.always_keep_recent:
                break
        recent_pair_ids = set(seen_pids)

        # Score remaining messages by similarity
        scored = []
        for msg in self._messages:
            if msg.pair_id in recent_pair_ids:
                continue
            sim = cosine_similarity(qe, msg.embedding)
            scored.append((sim, msg))

        scored.sort(key=lambda x: -x[0])
        selected_pair_ids = set(recent_pair_ids)
        for sim, msg in scored:
            if len(selected_pair_ids) >= k:
                break
            selected_pair_ids.add(msg.pair_id)

        # Reconstruct ordered messages for selected pairs
        result = [
            {"role": m.role, "content": m.content}
            for m in self._messages
            if m.pair_id in selected_pair_ids
        ]
        print(f"  Retrieved {len(result)} messages (from {len(self._messages)} stored)")
        return result


emb_history = EmbeddingHistory(top_k=6, always_keep_recent=2)


async def chat(user_message: str) -> str:
    relevant = await emb_history.retrieve(user_message)
    relevant.append({"role": "user", "content": user_message})

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=relevant,
    )
    reply = response.content[0].text
    await emb_history.add(user_message, reply)
    return reply


async def main():
    turns = [
        "I love hiking in the mountains.",
        "My favorite programming language is Python.",
        "What are good hiking trails near Seattle?",
        "Tell me about Python list comprehensions.",
        "Any mountain gear recommendations?",
    ]
    for turn in turns:
        print(f"User: {turn}")
        print(f"Agent: {await chat(turn)}\n")

asyncio.run(main())
```

**Expected Token Savings**: Only K=6 relevant messages sent per turn instead of N. Particularly effective for long factual sessions where early context is rarely re-used.
**Environment**: Async Python. Replace hash-based embeddings with voyage-3-lite for real semantic retrieval.

---

### Option 5: Tiered Context Strategy (Recent + Summary + Anchors)

Three-layer context: immutable anchors (goals/constraints), rolling summary of old turns, verbatim recent turns.

```python
import anthropic

client = anthropic.Anthropic()


class TieredContextHistory:
    """
    Three tiers:
    1. Anchors: permanent facts/goals (never compressed)
    2. Summary: compressed representation of older turns
    3. Recent: last N turns verbatim
    """
    def __init__(self, recent_turns: int = 6):
        self.anchors: list[str] = []       # permanent
        self._recent: list[tuple[str, str]] = []
        self._old: list[tuple[str, str]] = []
        self._summary: str = ""
        self.recent_turns = recent_turns

    def add_anchor(self, fact: str):
        self.anchors.append(fact)

    def add(self, user: str, assistant: str):
        self._recent.append((user, assistant))
        if len(self._recent) > self.recent_turns:
            overflow = self._recent.pop(0)
            self._old.append(overflow)
            if len(self._old) >= 5:
                self._summarize_old()

    def _summarize_old(self):
        text = "\n".join(f"User: {u}\nA: {a}" for u, a in self._old)
        if self._summary:
            text = f"[Prior summary]\n{self._summary}\n\n[New]\n{text}"

        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content":
                "Compress this conversation into 3-4 key points:\n\n" + text}],
        )
        self._summary = r.content[0].text
        self._old = []
        print(f"  [Summarized {5} old pairs]")

    def build_messages(self, current_user: str) -> list[dict]:
        msgs = []

        # Tier 1: Anchors as a context note
        if self.anchors:
            anchor_text = "\n".join(f"• {a}" for a in self.anchors)
            msgs += [
                {"role": "user",      "content": f"[Session context]\n{anchor_text}"},
                {"role": "assistant", "content": "Understood. I'll keep these in mind."},
            ]

        # Tier 2: Summary of older turns
        if self._summary:
            msgs += [
                {"role": "user",      "content": f"[Earlier discussion summary]\n{self._summary}"},
                {"role": "assistant", "content": "Got it."},
            ]

        # Tier 3: Recent verbatim turns
        for u, a in self._recent:
            msgs.append({"role": "user",      "content": u})
            msgs.append({"role": "assistant", "content": a})

        msgs.append({"role": "user", "content": current_user})
        return msgs


history = TieredContextHistory(recent_turns=6)
history.add_anchor("User is building a Python REST API with FastAPI.")
history.add_anchor("They prefer concise answers with code examples.")


def chat(user_message: str) -> str:
    messages = history.build_messages(user_message)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=messages,
    )
    reply = response.content[0].text
    history.add(user_message, reply)
    return reply


for msg in [
    "How do I add authentication to FastAPI?",
    "What's the best way to handle database connections?",
    "Show me how to add rate limiting.",
    "How do I write tests for FastAPI endpoints?",
    "What about async endpoints?",
    "How do I deploy this to AWS Lambda?",
    "Any tips for reducing cold start times?",
]:
    print(f"User: {msg}")
    print(f"Agent: {chat(msg)[:100]}\n")
```

**Expected Token Savings**: Anchors use ~50 tokens instead of repeating full initial instructions each turn. Combined with summary tier, 50-turn sessions stay under 4K tokens per request.
**Environment**: Single process. Summary compression adds 1 Haiku call per 5 overflow pairs (~$0.0001 each).

---

### Option 6: Prompt Caching with Stable Prefix

Structure conversation so the stable system/persona prefix qualifies for Anthropic's prompt caching. Pay for dynamic suffix only.

```python
import anthropic

client = anthropic.Anthropic()

# This large, stable block is cached after first use.
# Subsequent calls don't pay full input token price for it.
CACHED_SYSTEM = """
You are an expert Python and FastAPI developer with 10 years of experience.
You always provide working code examples.
You follow PEP 8 and modern Python 3.12+ conventions.
You know these frameworks deeply: FastAPI, SQLAlchemy, Pydantic, asyncio, pytest.

[Company knowledge base — stable, never changes]
- Internal API: https://internal-api.company.com/v2
- Auth pattern: Bearer tokens via Authorization header
- Database: PostgreSQL 15 with asyncpg driver
- Deployment: AWS ECS Fargate with ALB
- CI/CD: GitHub Actions → ECR → ECS rolling deploy
- Monitoring: CloudWatch + Datadog APM

[Code style guide — 847 tokens of stable context]
Always use type hints. Prefer async def for I/O operations.
Use dependency injection via FastAPI's Depends().
Handle errors with HTTPException and custom exception handlers.
Write docstrings for all public functions.
...
""".strip()

# Sliding window for the dynamic (non-cached) portion
_recent: list[dict] = []
MAX_DYNAMIC_MESSAGES = 8


def chat(user_message: str) -> str:
    global _recent

    # Keep only recent dynamic messages
    if len(_recent) >= MAX_DYNAMIC_MESSAGES * 2:
        _recent = _recent[-(MAX_DYNAMIC_MESSAGES * 2):]

    messages = _recent + [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": CACHED_SYSTEM,
                "cache_control": {"type": "ephemeral"},  # Enable prompt caching
            }
        ],
        messages=messages,
    )

    reply = response.content[0].text
    _recent.append({"role": "user",      "content": user_message})
    _recent.append({"role": "assistant", "content": reply})

    # Log cache performance
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_created = getattr(usage, "cache_creation_input_tokens", 0)
    print(
        f"  Tokens — input: {usage.input_tokens}, "
        f"cache_read: {cache_read}, cache_created: {cache_created}, "
        f"output: {usage.output_tokens}"
    )
    return reply


# First call: cache miss (pays full price for system prompt)
# Subsequent calls: cache hit (pays ~10% for system prefix)
for msg in [
    "How do I add JWT auth to my FastAPI app?",
    "Show me the database connection setup.",
    "How do I write a test for the auth endpoint?",
]:
    print(f"User: {msg}")
    print(f"Agent: {chat(msg)[:100]}\n")
```

**Expected Token Savings**: Prompt cache hits charge only 10% of normal input token price for the cached prefix. For a 2,000-token system prompt with 100 requests/day, saves ~$0.50/day on Sonnet.
**Environment**: Requires `claude-sonnet-4-6` or `claude-opus-4-6` (caching not available on Haiku). Cache TTL is 5 minutes by default.

---

| Option | Mechanism | Token Overhead | Semantic Quality | Best For |
|--------|-----------|---------------|-----------------|----------|
| 1 | Sliding window (last N) | Zero | Loses old context | Short, task-focused sessions |
| 2 | Token-budget trimmer | Zero | Loses old context | Cost-capped sessions |
| 3 | Rolling summary | ~512 tokens/compress | Good compression | Long multi-topic sessions |
| 4 | Embedding retrieval | ~50 tokens overhead | Best relevance | Reference-heavy sessions |
| 5 | Tiered (anchors+summary+recent) | ~100 tokens overhead | Excellent | Complex goal-driven sessions |
| 6 | Prompt caching | None (pays 10%) | Full fidelity | Stable system prompts, high volume |
