---
title: "Agent Doesn't Implement Conversation Pruning Based on Recency and Relevance"
description: "Intelligently remove low-value turns from conversation history to stay within context limits without losing important context."
category: token-cost
difficulty: intermediate
tags: [token-cost, context-window, conversation, pruning, recency, relevance]
---

# Agent Doesn't Implement Conversation Pruning Based on Recency and Relevance

## Problem

Agents that keep the full conversation history eventually hit context limits and start dropping the most recent (most important) turns from the beginning. Naive truncation destroys recent context. Smart pruning removes old, low-value turns while preserving recent activity, important decisions, and information the current query depends on.

---

## Option 1: Recency + Turn-Type Weighted Pruning

```python
import asyncio
import anthropic
import math
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class TurnType(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM_NOTE = "system_note"

TYPE_KEEP_WEIGHT = {
    TurnType.USER: 0.9,
    TurnType.ASSISTANT: 0.7,
    TurnType.TOOL_RESULT: 0.8,
    TurnType.TOOL_CALL: 0.5,
    TurnType.SYSTEM_NOTE: 0.95,
}

@dataclass
class Turn:
    role: str
    content: str
    turn_type: TurnType = TurnType.USER
    importance_override: float | None = None  # pin if set to 1.0

    def token_estimate(self) -> int:
        return max(1, int(len(self.content.split()) * 1.3))

def score_turn(turn: Turn, idx: int, total: int) -> float:
    if turn.importance_override is not None:
        return turn.importance_override

    # Recency: turns near the end get higher scores
    recency = (idx + 1) / total  # 0.0 (oldest) → 1.0 (newest)
    recency_weight = math.exp(-2.0 * (1.0 - recency))  # exponential decay

    type_weight = TYPE_KEEP_WEIGHT.get(turn.turn_type, 0.5)
    return 0.6 * recency_weight + 0.4 * type_weight

def prune_history(turns: list[Turn], token_budget: int) -> list[Turn]:
    """Always keep last N turns, prune oldest low-value turns first."""
    total_tokens = sum(t.token_estimate() for t in turns)
    if total_tokens <= token_budget:
        return turns

    # Always keep last 4 turns (most recent context)
    ALWAYS_KEEP_TAIL = 4
    tail = turns[-ALWAYS_KEEP_TAIL:]
    body = turns[:-ALWAYS_KEEP_TAIL]

    # Score body turns
    scored = [(score_turn(t, i, len(body)), i, t) for i, t in enumerate(body)]
    scored.sort(key=lambda x: x[0], reverse=True)  # highest score first

    tail_tokens = sum(t.token_estimate() for t in tail)
    remaining_budget = token_budget - tail_tokens
    kept_body: list[tuple[int, Turn]] = []
    used = 0

    for score, orig_idx, turn in scored:
        est = turn.token_estimate()
        if used + est <= remaining_budget:
            kept_body.append((orig_idx, turn))
            used += est

    # Re-sort kept body by original order
    kept_body.sort(key=lambda x: x[0])
    result = [t for _, t in kept_body] + tail
    print(f"[PRUNE] {len(turns)} → {len(result)} turns, ~{used + tail_tokens} tokens")
    return result

def turns_to_messages(turns: list[Turn]) -> list[dict]:
    return [{"role": t.role, "content": t.content} for t in turns]

async def chat(history: list[Turn], user_message: str, token_budget: int = 3000) -> tuple[str, list[Turn]]:
    history.append(Turn(role="user", content=user_message, turn_type=TurnType.USER))
    pruned = prune_history(history, token_budget)
    msgs = turns_to_messages(pruned)
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512, messages=msgs
    )
    reply = resp.content[0].text
    history.append(Turn(role="assistant", content=reply, turn_type=TurnType.ASSISTANT))
    return reply, history

async def main():
    history: list[Turn] = []
    questions = [
        "What is Python?",
        "How does asyncio work?",
        "Can you show me an example?",
        "What about error handling in async code?",
        "Going back to Python — what's the GIL?",
    ]
    for q in questions:
        reply, history = await chat(history, q, token_budget=1500)
        print(f"Q: {q}\nA: {reply[:80]}\nHistory turns: {len(history)}\n")

asyncio.run(main())
```

---

## Option 2: Relevance-Scored Pruning Using Current Query

```python
import asyncio
import anthropic
import hashlib
import math
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

def embed(text: str, dim: int = 64) -> list[float]:
    vec = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x/norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

@dataclass
class HistoryTurn:
    role: str
    content: str
    emb: list[float] = field(default_factory=list)

    def __post_init__(self):
        if not self.emb:
            self.emb = embed(self.content)

    def token_est(self) -> int:
        return max(1, int(len(self.content.split()) * 1.3))

def query_relevance_prune(
    history: list[HistoryTurn],
    current_query: str,
    token_budget: int,
    recency_weight: float = 0.4,
    relevance_weight: float = 0.6,
) -> list[HistoryTurn]:
    if not history:
        return history

    query_emb = embed(current_query)
    total = len(history)

    scored: list[tuple[float, int, HistoryTurn]] = []
    for i, turn in enumerate(history):
        rel = cosine(query_emb, turn.emb)
        rec = (i + 1) / total
        score = relevance_weight * rel + recency_weight * rec
        scored.append((score, i, turn))

    # Always keep last 3 turns regardless of score
    tail_indices = set(range(total - 3, total))
    tail_turns = history[-3:]
    tail_tokens = sum(t.token_est() for t in tail_turns)
    remaining = token_budget - tail_tokens

    # Sort non-tail turns by score, take highest
    body_scored = [(s, i, t) for s, i, t in scored if i not in tail_indices]
    body_scored.sort(key=lambda x: -x[0])

    kept_body: list[tuple[int, HistoryTurn]] = []
    used = 0
    for _, orig_idx, turn in body_scored:
        est = turn.token_est()
        if used + est <= remaining:
            kept_body.append((orig_idx, turn))
            used += est

    kept_body.sort(key=lambda x: x[0])
    result = [t for _, t in kept_body] + tail_turns
    dropped = len(history) - len(result)
    if dropped > 0:
        print(f"[RELEVANCE PRUNE] Dropped {dropped}/{len(history)} turns for query: {current_query[:40]}")
    return result

class RelevancePrunedAgent:
    def __init__(self, token_budget: int = 4000):
        self.history: list[HistoryTurn] = []
        self.budget = token_budget

    async def respond(self, user_message: str) -> str:
        # Prune based on current query relevance
        pruned = query_relevance_prune(self.history, user_message, self.budget)

        msgs = [{"role": t.role, "content": t.content} for t in pruned]
        msgs.append({"role": "user", "content": user_message})

        resp = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512, messages=msgs
        )
        reply = resp.content[0].text

        self.history.append(HistoryTurn(role="user", content=user_message))
        self.history.append(HistoryTurn(role="assistant", content=reply))
        return reply

agent = RelevancePrunedAgent(token_budget=2000)

async def main():
    for msg in [
        "Explain Python classes.",
        "Now tell me about asyncio.",
        "Back to classes — what is inheritance?",
        "What is multiple inheritance?",
        "And for asyncio — how do I use gather()?",
    ]:
        reply = await agent.respond(msg)
        print(f"Q: {msg}\nA: {reply[:80]}\n")

asyncio.run(main())
```

---

## Option 3: Summarization-Based Compaction

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ConversationMemory:
    recent_turns: list[dict] = field(default_factory=list)
    compressed_summary: str = ""
    total_turns_seen: int = 0
    max_recent: int = 8          # keep this many recent turns verbatim
    compress_threshold: int = 15  # compress when we exceed this many turns

    def add(self, role: str, content: str):
        self.recent_turns.append({"role": role, "content": content})
        self.total_turns_seen += 1

    async def maybe_compress(self):
        if len(self.recent_turns) < self.compress_threshold:
            return

        # Compress everything except the most recent turns
        to_compress = self.recent_turns[:-self.max_recent]
        keep = self.recent_turns[-self.max_recent:]

        if not to_compress:
            return

        convo_text = "\n".join([f"{t['role'].upper()}: {t['content'][:200]}" for t in to_compress])
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system="Summarize this conversation segment into a concise paragraph capturing the key topics, decisions, and facts discussed. This will be used as context for future turns.",
            messages=[{"role": "user", "content": convo_text}]
        )
        new_summary = resp.content[0].text
        if self.compressed_summary:
            self.compressed_summary = f"{self.compressed_summary}\n\n[Later:] {new_summary}"
        else:
            self.compressed_summary = new_summary

        self.recent_turns = keep
        print(f"[COMPRESS] Compressed {len(to_compress)} turns → summary ({len(new_summary)} chars). Kept {len(keep)} recent turns.")

    def build_messages(self, user_message: str) -> list[dict]:
        msgs: list[dict] = []
        if self.compressed_summary:
            msgs.append({"role": "user", "content": f"[Conversation history summary: {self.compressed_summary}]"})
            msgs.append({"role": "assistant", "content": "Understood, I'll keep this context in mind."})
        msgs.extend(self.recent_turns)
        msgs.append({"role": "user", "content": user_message})
        return msgs

memory = ConversationMemory(max_recent=6, compress_threshold=12)

async def chat(user_msg: str) -> str:
    await memory.maybe_compress()
    msgs = memory.build_messages(user_msg)
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512, messages=msgs
    )
    reply = resp.content[0].text
    memory.add("user", user_msg)
    memory.add("assistant", reply)
    return reply

async def main():
    topics = [
        "What is dependency injection?",
        "How does it work in Python?",
        "Can you show me a container example?",
        "What about testing with DI?",
        "How is DI different from service locator?",
        "Now let's talk about SOLID principles.",
        "What is the single responsibility principle?",
        "Give me an example of SRP violation.",
        "How do I fix that violation?",
        "What about the open/closed principle?",
    ]
    for msg in topics:
        reply = await chat(msg)
        print(f"Q: {msg}\nA: {reply[:70]}\nRecent turns: {len(memory.recent_turns)}\n")

asyncio.run(main())
```

---

## Option 4: Sliding Window with Pinned Anchors

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class WindowTurn:
    role: str
    content: str
    pinned: bool = False      # always kept
    is_decision: bool = False  # important decision/fact

    def token_est(self) -> int:
        return max(1, int(len(self.content.split()) * 1.3))

class SlidingWindowHistory:
    def __init__(self, token_budget: int = 4000):
        self.turns: list[WindowTurn] = []
        self.budget = token_budget

    def add(self, role: str, content: str, pinned: bool = False):
        self.turns.append(WindowTurn(role=role, content=content, pinned=pinned))

    def _mark_decisions(self, content: str) -> bool:
        """Heuristic: detect if assistant turn contains a decision or key fact."""
        markers = ["we decided", "the answer is", "remember", "important:", "note:", "key point", "in conclusion"]
        return any(m in content.lower() for m in markers)

    def get_window(self) -> list[dict]:
        # Separate pinned and unpinned
        pinned = [t for t in self.turns if t.pinned]
        unpinned = [t for t in self.turns if not t.pinned]

        pinned_tokens = sum(t.token_est() for t in pinned)
        remaining = self.budget - pinned_tokens

        # Fill remaining budget from most recent unpinned turns
        window: list[WindowTurn] = []
        used = 0
        for turn in reversed(unpinned):
            est = turn.token_est()
            if used + est <= remaining:
                window.insert(0, turn)
                used += est
            else:
                break

        dropped = len(unpinned) - len(window)
        if dropped > 0:
            print(f"[WINDOW] Dropped {dropped} old turns, keeping {len(window)} recent + {len(pinned)} pinned")

        # Merge: pinned at front (as context), then window
        all_turns = pinned + window
        return [{"role": t.role, "content": t.content} for t in all_turns]

window = SlidingWindowHistory(token_budget=2500)

async def chat(user_msg: str, pin_response: bool = False) -> str:
    msgs = window.get_window()
    msgs.append({"role": "user", "content": user_msg})
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=400, messages=msgs
    )
    reply = resp.content[0].text
    is_decision = window._mark_decisions(reply)
    window.add("user", user_msg)
    window.add("assistant", reply, pinned=pin_response or is_decision)
    return reply

async def main():
    # Pin the initial system context
    window.turns.append(WindowTurn(
        role="user", content="You are helping me build a Flask API.", pinned=True
    ))
    window.turns.append(WindowTurn(
        role="assistant", content="Understood! I'll help you build the Flask API.", pinned=True
    ))

    for msg in [
        "What routes should I create?",
        "Let's use REST. What about authentication?",
        "We'll use JWT. What about the database?",
        "PostgreSQL with SQLAlchemy. Now show me a basic route.",
        "Add error handling to that route.",
        "What about rate limiting?",
    ]:
        reply = await chat(msg)
        print(f"Q: {msg}\nA: {reply[:80]}\nTotal turns: {len(window.turns)}\n")

asyncio.run(main())
```

---

## Option 5: Async LLM-Scored Turn Importance

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ScoredTurn:
    role: str
    content: str
    importance: float | None = None  # set by LLM scorer

    def token_est(self) -> int:
        return max(1, int(len(self.content.split()) * 1.3))

async def score_turns_batch(turns: list[ScoredTurn], current_query: str) -> list[ScoredTurn]:
    """Use Haiku to score all turns' relevance to the current query."""
    if not turns:
        return turns
    turns_text = "\n".join([f'[{i}] {t.role}: {t.content[:100]}' for i, t in enumerate(turns)])
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=f'Rate each conversation turn 0.0-1.0 for relevance to this query: "{current_query}". Return JSON: {{"scores": [0.0, ...]}} with one score per turn in order.',
        messages=[{"role": "user", "content": turns_text}]
    )
    try:
        data = json.loads(resp.content[0].text)
        scores = data.get("scores", [])
        for i, score in enumerate(scores):
            if i < len(turns):
                turns[i].importance = float(score)
    except Exception:
        for t in turns:
            if t.importance is None:
                t.importance = 0.5
    return turns

async def llm_scored_prune(history: list[ScoredTurn], current_query: str, token_budget: int) -> list[ScoredTurn]:
    if not history:
        return history

    # Always keep last 3 turns
    tail = history[-3:]
    body = history[:-3]

    if body:
        body = await score_turns_batch(body, current_query)

    tail_tokens = sum(t.token_est() for t in tail)
    remaining = token_budget - tail_tokens

    # Sort body by importance, keep highest
    scored_body = sorted(body, key=lambda t: t.importance or 0.0, reverse=True)
    kept: list[ScoredTurn] = []
    used = 0
    for turn in scored_body:
        est = turn.token_est()
        if used + est <= remaining:
            kept.append(turn)
            used += est

    # Re-sort by original order
    kept_indices = {id(t): i for i, t in enumerate(body)}
    kept.sort(key=lambda t: kept_indices.get(id(t), 999))

    dropped = len(body) - len(kept)
    if dropped > 0:
        print(f"[LLM PRUNE] Dropped {dropped} turns (lowest importance for: {current_query[:40]})")

    return kept + tail

class LLMPrunedAgent:
    def __init__(self, token_budget: int = 3000):
        self.history: list[ScoredTurn] = []
        self.budget = token_budget

    async def respond(self, user_msg: str) -> str:
        pruned = await llm_scored_prune(self.history, user_msg, self.budget)
        msgs = [{"role": t.role, "content": t.content} for t in pruned]
        msgs.append({"role": "user", "content": user_msg})
        resp = await client.messages.create(model="claude-sonnet-4-6", max_tokens=400, messages=msgs)
        reply = resp.content[0].text
        self.history.append(ScoredTurn(role="user", content=user_msg))
        self.history.append(ScoredTurn(role="assistant", content=reply))
        return reply

agent = LLMPrunedAgent(token_budget=2000)

async def main():
    for msg in [
        "Tell me about Python decorators.",
        "How do class decorators work?",
        "Now let's talk about generators.",
        "What is yield from?",
        "Back to decorators — can they take arguments?",
    ]:
        reply = await agent.respond(msg)
        print(f"Q: {msg}\nA: {reply[:80]}\n")

asyncio.run(main())
```

---

## Option 6: Hybrid Pruning — Summarize Old, Window Recent, Pin Important

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class HybridTurn:
    role: str
    content: str
    pinned: bool = False
    turn_index: int = 0

    def token_est(self) -> int:
        return max(1, int(len(self.content.split()) * 1.3))

class HybridHistory:
    def __init__(self, window_turns: int = 10, summary_budget_tokens: int = 400, max_budget: int = 5000):
        self.turns: list[HybridTurn] = []
        self.summary: str = ""
        self.summary_tokens: int = 0
        self.window_turns = window_turns
        self.summary_budget = summary_budget_tokens
        self.max_budget = max_budget
        self._turn_idx = 0

    def add(self, role: str, content: str, pinned: bool = False):
        self._turn_idx += 1
        self.turns.append(HybridTurn(role=role, content=content, pinned=pinned, turn_index=self._turn_idx))

    async def consolidate(self):
        """Summarize old non-pinned turns beyond the window."""
        pinned = [t for t in self.turns if t.pinned]
        unpinned = [t for t in self.turns if not t.pinned]

        if len(unpinned) <= self.window_turns:
            return  # nothing to compress

        to_compress = unpinned[:-self.window_turns]
        keep_window = unpinned[-self.window_turns:]

        if not to_compress:
            return

        convo = "\n".join([f"{t.role}: {t.content[:150]}" for t in to_compress])
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=self.summary_budget,
            system="Summarize in 2-3 sentences the key topics, facts, and decisions from this conversation segment.",
            messages=[{"role": "user", "content": convo}]
        )
        new_summary = resp.content[0].text
        self.summary = (self.summary + " " + new_summary).strip() if self.summary else new_summary
        self.turns = pinned + keep_window
        print(f"[HYBRID] Summarized {len(to_compress)} turns. Summary: {len(self.summary)} chars. Active: {len(self.turns)}")

    def build_context(self, user_msg: str) -> list[dict]:
        msgs: list[dict] = []
        if self.summary:
            msgs += [
                {"role": "user", "content": f"[Prior conversation summary: {self.summary}]"},
                {"role": "assistant", "content": "Noted, I'll consider this context."}
            ]
        msgs += [{"role": t.role, "content": t.content} for t in self.turns]
        msgs.append({"role": "user", "content": user_msg})
        return msgs

hist = HybridHistory(window_turns=6, summary_budget_tokens=250, max_budget=3000)

async def chat(user_msg: str, pin: bool = False) -> str:
    await hist.consolidate()
    msgs = hist.build_context(user_msg)
    resp = await client.messages.create(model="claude-sonnet-4-6", max_tokens=400, messages=msgs)
    reply = resp.content[0].text
    hist.add("user", user_msg)
    hist.add("assistant", reply, pinned=pin)
    return reply

async def main():
    questions = [f"Topic {i}: {q}" for i, q in enumerate([
        "What is Python?", "What are lists?", "What are dicts?",
        "Tell me about sets.", "What are tuples?", "How about generators?",
        "What is asyncio?", "How do coroutines work?", "What is the GIL?",
        "Now, back to the first topic — tell me more about Python."
    ])]
    for q in questions:
        reply = await chat(q)
        print(f"Q: {q}\nA: {reply[:70]}\nActive turns: {len(hist.turns)}\n")

asyncio.run(main())
```

---

## Comparison

| Option | Strategy | Extra API Calls | Memory Preserved | Best For |
|--------|---------|----------------|-----------------|----------|
| 1 – Recency + Type | Score-based eviction | None | Recency + type priority | General chat agents |
| 2 – Query Relevance | Cosine similarity | None | Relevant to current query | Topic-switching conversations |
| 3 – Summarization | Compress old turns | Yes (Haiku) | Summary + recent | Long multi-topic sessions |
| 4 – Sliding Window + Pins | Window + pinned anchors | None | Pinned + recent N | Goal-oriented agents |
| 5 – LLM-Scored | Haiku importance scorer | Yes (Haiku) | Query-relevant turns | High-accuracy requirement |
| 6 – Hybrid | Summary + window + pins | Yes (Haiku) | Summary + recent + pins | Production long-lived agents |

**Recommendation:** Use Option 4 (sliding window with pins) for most agents — zero API overhead, predictable behavior, and explicit control over what's always kept. Add Option 3's summarization when conversations span many topics and you need semantic continuity beyond what the window holds. Use Option 6 in production where you need all three: summaries for old context, a recent window, and pinned decisions.
