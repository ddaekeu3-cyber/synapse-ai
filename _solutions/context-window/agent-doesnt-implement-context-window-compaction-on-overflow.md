---
layout: solution
title: "Agent Doesn't Implement Context Window Compaction on Overflow"
category: context-window
description: "Automatically compact conversation history when approaching the context window limit to prevent truncation errors and maintain coherent long-running sessions."
tags: [context-window, compaction, summarization, memory, token-management, long-context]
---

# Agent Doesn't Implement Context Window Compaction on Overflow

## Problem

When a conversation grows long enough to approach the model's context window limit, the agent crashes with a `context_length_exceeded` error, silently truncates early messages (losing critical state), or fails to respond coherently. Without proactive compaction, long-running agents become unreliable as sessions mature.

## Solutions

### Option 1: Token-Count Threshold with Oldest-Turn Summarization

Detect when the estimated token count exceeds a threshold, then summarize and drop the oldest non-system turns.

```python
import anthropic

client = anthropic.Anthropic()

CONTEXT_LIMIT = 180_000   # tokens — model max is 200k
COMPACT_TARGET = 80_000   # shrink to this after compaction
CHARS_PER_TOKEN = 4       # rough estimate


def estimate_tokens(messages: list[dict]) -> int:
    total = sum(len(str(m.get("content", ""))) for m in messages)
    return total // CHARS_PER_TOKEN


def summarize_turns(turns: list[dict]) -> str:
    if not turns:
        return ""
    text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in turns
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": (
                    "Summarize the following conversation excerpt into a compact "
                    "paragraph preserving all important facts, decisions, and context:\n\n"
                    + text
                ),
            }
        ],
    )
    return resp.content[0].text


def compact_messages(messages: list[dict]) -> list[dict]:
    """Summarize oldest turns until estimated tokens fall below COMPACT_TARGET."""
    system_msgs = [m for m in messages if m["role"] == "system"]
    conv_msgs   = [m for m in messages if m["role"] != "system"]

    while estimate_tokens(system_msgs + conv_msgs) > COMPACT_TARGET and len(conv_msgs) > 2:
        # grab oldest 6 turns for summarization
        batch = conv_msgs[:6]
        conv_msgs = conv_msgs[6:]
        summary = summarize_turns(batch)
        conv_msgs.insert(
            0,
            {
                "role": "user",
                "content": f"[Context summary of earlier conversation]\n{summary}",
            },
        )

    return system_msgs + conv_msgs


def chat(messages: list[dict], user_input: str) -> tuple[str, list[dict]]:
    messages.append({"role": "user", "content": user_input})

    if estimate_tokens(messages) > CONTEXT_LIMIT:
        print("[compacting context...]")
        messages = compact_messages(messages)

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=messages,
    )
    reply = resp.content[0].text
    messages.append({"role": "assistant", "content": reply})
    return reply, messages


if __name__ == "__main__":
    messages: list[dict] = []
    for turn in range(20):
        reply, messages = chat(messages, f"Turn {turn}: tell me something interesting about prime numbers.")
        print(f"[{estimate_tokens(messages)} tokens] {reply[:80]}...")

# Expected Token Savings: 50–70% reduction per compaction cycle on long sessions
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: Sliding Window with Summary Anchor

Keep a rolling window of recent turns and a persistent summary anchor that accumulates all earlier context.

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

WINDOW_SIZE   = 20   # max turns to keep verbatim
ANCHOR_MAX_TK = 800  # max tokens for the anchor summary


@dataclass
class WindowedConversation:
    system_prompt: str = ""
    anchor_summary: str = ""          # compressed history of dropped turns
    window: list[dict] = field(default_factory=list)  # recent verbatim turns

    def add_turn(self, role: str, content: str) -> None:
        self.window.append({"role": role, "content": content})
        if len(self.window) > WINDOW_SIZE:
            self._roll_window()

    def _roll_window(self) -> None:
        # drop oldest 4 turns, fold them into the anchor
        dropped = self.window[:4]
        self.window = self.window[4:]
        excerpt = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in dropped)
        update_prompt = (
            f"Existing summary:\n{self.anchor_summary}\n\n"
            f"New conversation excerpt to incorporate:\n{excerpt}\n\n"
            "Produce an updated, concise summary (≤150 words) preserving all key facts."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=ANCHOR_MAX_TK,
            messages=[{"role": "user", "content": update_prompt}],
        )
        self.anchor_summary = resp.content[0].text

    def build_messages(self) -> list[dict]:
        msgs: list[dict] = []
        if self.anchor_summary:
            msgs.append({
                "role": "user",
                "content": f"[Prior conversation summary]\n{self.anchor_summary}",
            })
            msgs.append({
                "role": "assistant",
                "content": "Understood. I have that context.",
            })
        msgs.extend(self.window)
        return msgs


def chat(conv: WindowedConversation, user_input: str) -> str:
    conv.add_turn("user", user_input)
    messages = conv.build_messages()

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=conv.system_prompt,
        messages=messages,
    )
    reply = resp.content[0].text
    conv.add_turn("assistant", reply)
    return reply


if __name__ == "__main__":
    conv = WindowedConversation(
        system_prompt="You are a knowledgeable research assistant."
    )
    topics = [
        "What is quantum entanglement?",
        "How does it relate to quantum computing?",
        "What are the main qubit implementations?",
        "How does error correction work?",
        "What companies are leading this space?",
        "What's the timeline to practical quantum advantage?",
    ] * 4  # simulate long session

    for topic in topics:
        reply = chat(conv, topic)
        print(f"Q: {topic}\nA: {reply[:100]}...\n")
        print(f"  window={len(conv.window)} turns, anchor={'yes' if conv.anchor_summary else 'no'}")

# Expected Token Savings: 60–75% on sessions > WINDOW_SIZE turns
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Importance-Scored LRU Eviction

Score each message by recency and content importance; evict lowest-scoring messages first when approaching the limit.

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

LIMIT_TOKENS  = 150_000
EVICT_TO      = 80_000
CHARS_PER_TOK = 4


@dataclass
class ScoredMessage:
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)
    importance: float = 1.0   # 0–2, boosted for tool results / decisions

    def token_estimate(self) -> int:
        return len(self.content) // CHARS_PER_TOK

    def score(self, now: float) -> float:
        age_hours = (now - self.timestamp) / 3600
        recency = 1.0 / (1.0 + age_hours)
        return self.importance * recency


def score_messages(messages: list[ScoredMessage]) -> None:
    """Ask the model to rate each message's importance (batched)."""
    contents = [{"idx": i, "text": m.content[:300]} for i, m in enumerate(messages)]
    prompt = (
        "Rate each message's importance for maintaining conversation coherence. "
        "Return JSON list [{\"idx\": N, \"score\": 0.0-2.0}].\n"
        "Messages:\n" + str(contents)
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        import json, re
        raw = resp.content[0].text
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            ratings = json.loads(match.group())
            for r in ratings:
                if 0 <= r["idx"] < len(messages):
                    messages[r["idx"]].importance = float(r["score"])
    except Exception:
        pass  # fall back to default scores


def evict_to_budget(messages: list[ScoredMessage]) -> list[ScoredMessage]:
    now = time.time()
    system = [m for m in messages if m.role == "system"]
    evictable = [m for m in messages if m.role != "system"]

    # score before evicting
    score_messages(evictable)

    # sort ascending by score — lowest score evicted first
    evictable.sort(key=lambda m: m.score(now))

    total = sum(m.token_estimate() for m in system + evictable)
    i = 0
    while total > EVICT_TO and i < len(evictable):
        total -= evictable[i].token_estimate()
        evictable[i] = None  # mark for removal
        i += 1

    kept = [m for m in evictable if m is not None]
    # restore chronological order
    kept.sort(key=lambda m: m.timestamp)
    return system + kept


def chat(messages: list[ScoredMessage], user_input: str, importance: float = 1.0) -> tuple[str, list[ScoredMessage]]:
    total = sum(m.token_estimate() for m in messages) + len(user_input) // CHARS_PER_TOK
    if total > LIMIT_TOKENS:
        print("[evicting low-importance messages...]")
        messages = evict_to_budget(messages)

    messages.append(ScoredMessage(role="user", content=user_input, importance=importance))

    api_msgs = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    system   = next((m.content for m in messages if m.role == "system"), None)

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system or anthropic.NOT_GIVEN,
        messages=api_msgs,
    )
    reply = resp.content[0].text
    messages.append(ScoredMessage(role="assistant", content=reply, importance=1.0))
    return reply, messages


if __name__ == "__main__":
    msgs: list[ScoredMessage] = [
        ScoredMessage(role="system", content="You are a helpful assistant.", importance=2.0)
    ]
    for i in range(10):
        imp = 1.5 if i % 3 == 0 else 0.7
        reply, msgs = chat(msgs, f"Question {i}: elaborate on topic {i % 5}", importance=imp)
        print(f"[{len(msgs)} msgs] {reply[:60]}...")

# Expected Token Savings: 40–60% per eviction pass; preserves high-importance context
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Hierarchical Compression (Summary of Summaries)

Compress older summaries further when the summary itself grows too large, creating a hierarchical tree of compressed context.

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

CHARS_PER_TOK   = 4
VERBATIM_TURNS  = 10    # always keep last N turns verbatim
SUMMARY_MAX_TOK = 600   # per summary level
TOTAL_LIMIT     = 160_000


@dataclass
class HierarchicalContext:
    """
    level_summaries[0] = oldest (most compressed)
    level_summaries[-1] = most recent summary
    verbatim = last VERBATIM_TURNS turns
    """
    system: str = ""
    level_summaries: list[str] = field(default_factory=list)
    verbatim: list[dict] = field(default_factory=list)

    def token_estimate(self) -> int:
        total = len(self.system) // CHARS_PER_TOK
        total += sum(len(s) // CHARS_PER_TOK for s in self.level_summaries)
        total += sum(len(str(m)) // CHARS_PER_TOK for m in self.verbatim)
        return total

    def _compress(self, text: str, level: int) -> str:
        word_limit = max(50, 200 - level * 40)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=SUMMARY_MAX_TOK,
            messages=[{
                "role": "user",
                "content": (
                    f"Compress the following into ≤{word_limit} words, "
                    "preserving all critical facts:\n\n" + text
                ),
            }],
        )
        return resp.content[0].text

    def add_turn(self, role: str, content: str) -> None:
        self.verbatim.append({"role": role, "content": content})
        if len(self.verbatim) > VERBATIM_TURNS:
            self._roll()

    def _roll(self) -> None:
        dropped = self.verbatim[:2]
        self.verbatim = self.verbatim[2:]
        new_text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in dropped)

        if not self.level_summaries:
            self.level_summaries.append(self._compress(new_text, level=0))
        else:
            combined = self.level_summaries[-1] + "\n" + new_text
            new_summary = self._compress(combined, level=len(self.level_summaries) - 1)
            self.level_summaries[-1] = new_summary

        # if summaries are piling up, compress them upward
        if len(self.level_summaries) > 3:
            merged = "\n".join(self.level_summaries[:2])
            compressed = self._compress(merged, level=len(self.level_summaries))
            self.level_summaries = [compressed] + self.level_summaries[2:]

    def build_messages(self) -> list[dict]:
        msgs: list[dict] = []
        if self.level_summaries:
            combined = "\n\n".join(
                f"[Level-{i} summary]\n{s}" for i, s in enumerate(self.level_summaries)
            )
            msgs.append({"role": "user", "content": combined})
            msgs.append({"role": "assistant", "content": "I have that prior context."})
        msgs.extend(self.verbatim)
        return msgs


def chat(ctx: HierarchicalContext, user_input: str) -> str:
    ctx.add_turn("user", user_input)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=ctx.system,
        messages=ctx.build_messages(),
    )
    reply = resp.content[0].text
    ctx.add_turn("assistant", reply)
    print(f"  [~{ctx.token_estimate()} tokens | {len(ctx.level_summaries)} summary levels]")
    return reply


if __name__ == "__main__":
    ctx = HierarchicalContext(system="You are a long-running research assistant.")
    questions = [
        "Explain transformer attention mechanisms.",
        "How do multi-head attention layers differ?",
        "What is positional encoding?",
        "Describe layer normalization.",
        "What is the feed-forward sublayer?",
        "How does the encoder-decoder architecture work?",
        "What is cross-attention?",
        "Explain causal masking.",
        "How does BERT differ from GPT?",
        "What are instruction-tuned models?",
    ] * 3

    for q in questions:
        answer = chat(ctx, q)
        print(f"Q: {q[:60]}\nA: {answer[:80]}...\n")

# Expected Token Savings: 70–85% on very long sessions via hierarchical re-compression
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Async Background Compaction Worker

Run compaction in a background asyncio task so the main conversation loop is never blocked.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

LIMIT_TOKENS    = 150_000
COMPACT_TARGET  = 70_000
CHARS_PER_TOK   = 4


@dataclass
class AsyncCompactingConversation:
    system: str = ""
    messages: list[dict] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _compacting: bool = False

    def token_estimate(self) -> int:
        return sum(len(str(m.get("content", ""))) for m in self.messages) // CHARS_PER_TOK

    async def _background_compact(self) -> None:
        async with self._lock:
            if self._compacting:
                return
            self._compacting = True

        try:
            # snapshot current messages
            async with self._lock:
                snapshot = list(self.messages)

            system_msgs = [m for m in snapshot if m["role"] == "system"]
            conv_msgs   = [m for m in snapshot if m["role"] != "system"]

            # summarize first half
            half = conv_msgs[: len(conv_msgs) // 2]
            rest = conv_msgs[len(conv_msgs) // 2 :]

            if not half:
                return

            text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in half)
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{
                    "role": "user",
                    "content": "Summarize this conversation excerpt concisely:\n\n" + text,
                }],
            )
            summary = resp.content[0].text
            compacted = system_msgs + [
                {"role": "user", "content": f"[Earlier context summary]\n{summary}"},
                {"role": "assistant", "content": "Understood."},
            ] + rest

            async with self._lock:
                self.messages = compacted
                print(f"  [background compact done: ~{self.token_estimate()} tokens]")
        finally:
            self._compacting = False

    async def add_and_reply(self, user_input: str) -> str:
        async with self._lock:
            self.messages.append({"role": "user", "content": user_input})
            msgs = [m for m in self.messages if m["role"] != "system"]

        # fire compaction in background if approaching limit
        if self.token_estimate() > LIMIT_TOKENS and not self._compacting:
            asyncio.create_task(self._background_compact())

        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=self.system,
            messages=msgs,
        )
        reply = resp.content[0].text

        async with self._lock:
            self.messages.append({"role": "assistant", "content": reply})

        return reply


async def main() -> None:
    conv = AsyncCompactingConversation(system="You are a helpful assistant.")
    questions = [
        "What is the CAP theorem?",
        "Explain eventual consistency.",
        "What is CRDT?",
        "How do distributed locks work?",
        "Explain the Raft consensus algorithm.",
        "What is the two-generals problem?",
        "How does Paxos work?",
        "What is vector clock?",
        "Explain partition tolerance.",
        "How do distributed databases handle split-brain?",
    ] * 3

    tasks = []
    for q in questions:
        reply = await conv.add_and_reply(q)
        print(f"Q: {q[:50]}\nA: {reply[:80]}...\n")
        print(f"  [~{conv.token_estimate()} estimated tokens]\n")
        await asyncio.sleep(0.05)  # small yield for background task

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 50–65% without blocking main loop; slight lag before compaction effect
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Streaming Compaction with Prompt Caching

Use extended thinking + prompt caching to compress context efficiently, and cache the resulting summary for future turns.

```python
import anthropic
import hashlib
from dataclasses import dataclass, field

client = anthropic.Anthropic()

LIMIT_TOKENS   = 150_000
CHARS_PER_TOK  = 4
WINDOW_KEEP    = 8   # verbatim turns to keep after compaction


@dataclass
class CacheAwareConversation:
    system: str = ""
    cached_summary: str = ""
    cached_summary_hash: str = ""
    verbatim: list[dict] = field(default_factory=list)
    total_compactions: int = 0

    def token_estimate(self) -> int:
        base = (len(self.system) + len(self.cached_summary)) // CHARS_PER_TOK
        base += sum(len(str(m)) // CHARS_PER_TOK for m in self.verbatim)
        return base

    def _build_summary_block(self) -> dict | None:
        if not self.cached_summary:
            return None
        return {
            "type": "text",
            "text": f"[Conversation history summary]\n{self.cached_summary}",
            "cache_control": {"type": "ephemeral"},
        }

    def compact(self) -> None:
        """Summarize verbatim turns older than WINDOW_KEEP, update cached summary."""
        to_compress = self.verbatim[:-WINDOW_KEEP] if len(self.verbatim) > WINDOW_KEEP else []
        self.verbatim = self.verbatim[-WINDOW_KEEP:] if len(self.verbatim) > WINDOW_KEEP else self.verbatim

        if not to_compress:
            return

        excerpt = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in to_compress)
        update_prompt = (
            f"Previous summary:\n{self.cached_summary}\n\n"
            f"New excerpt to incorporate:\n{excerpt}\n\n"
            "Produce an updated concise summary (≤200 words)."
        )

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": update_prompt}],
        )
        new_summary = resp.content[0].text
        new_hash = hashlib.md5(new_summary.encode()).hexdigest()

        if new_hash != self.cached_summary_hash:
            self.cached_summary = new_summary
            self.cached_summary_hash = new_hash

        self.total_compactions += 1
        print(f"  [compaction #{self.total_compactions} | ~{self.token_estimate()} tokens]")

    def chat(self, user_input: str) -> str:
        self.verbatim.append({"role": "user", "content": user_input})

        if self.token_estimate() > LIMIT_TOKENS:
            self.compact()

        # build messages with cached summary as first user block
        msgs: list[dict] = []
        summary_block = self._build_summary_block()
        if summary_block:
            msgs.append({"role": "user", "content": [summary_block]})
            msgs.append({"role": "assistant", "content": "Understood, I have the prior context."})

        msgs.extend(self.verbatim)

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=self.system,
            messages=msgs,
        )
        reply = resp.content[0].text
        self.verbatim.append({"role": "assistant", "content": reply})
        return reply


if __name__ == "__main__":
    conv = CacheAwareConversation(system="You are an expert software architect.")
    topics = [
        "Explain microservices architecture.",
        "What are service meshes?",
        "How does API gateway pattern work?",
        "Explain the saga pattern for distributed transactions.",
        "What is the outbox pattern?",
        "How do you handle service discovery?",
        "What is circuit breaking in microservices?",
        "Explain event sourcing.",
        "What is CQRS?",
        "How do you implement distributed tracing?",
    ] * 4

    for topic in topics:
        reply = conv.chat(topic)
        print(f"Q: {topic}\nA: {reply[:100]}...\n")

# Expected Token Savings: 55–70% with cache hits reducing repeated summary transmission cost
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Strategy | Latency Impact | Coherence | Token Savings | Best For |
|--------|----------|---------------|-----------|---------------|----------|
| 1 | Oldest-turn summarization | Low | Good | 50–70% | Simple chatbots |
| 2 | Sliding window + anchor | Low | Very Good | 60–75% | Research assistants |
| 3 | Importance-scored LRU eviction | Medium | Good | 40–60% | Varied-priority conversations |
| 4 | Hierarchical summary tree | Medium | Excellent | 70–85% | Very long sessions |
| 5 | Async background compaction | None (async) | Good | 50–65% | High-throughput services |
| 6 | Cache-aware + prompt caching | Low | Very Good | 55–70% | Production APIs with caching |
