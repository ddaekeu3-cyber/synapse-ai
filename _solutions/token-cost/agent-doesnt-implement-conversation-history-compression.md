---
layout: solution
title: "Agent Doesn't Implement Conversation History Compression"
category: token-cost
description: "Agents that append every turn to conversation history cause token counts to grow unboundedly. Long sessions hit context limits, incur escalating per-call costs, and slow down due to larger payloads — all avoidable with history compression strategies."
tags: [token-cost, conversation-history, compression, summarization, context-window, cost-optimization]
---

## Problem

Every time an agent appends a full turn to history and re-sends the entire transcript, the input token count grows linearly. A 50-turn conversation that starts at 200 tokens can balloon to 10,000+ tokens per call. This triples costs, approaches context limits, and degrades quality when old irrelevant turns dilute current context. Agents need history compression: rolling summaries, sliding windows, or selective pruning that keep the useful signal while discarding verbosity.

## Solutions

### Option 1: Rolling Summary Window — Summarize Oldest N Turns

```python
import anthropic

client = anthropic.Anthropic()

def summarize_turns(turns: list[dict]) -> str:
    """Compress a list of turns into a single summary string via Haiku."""
    transcript = "\n".join(
        f"{t['role'].upper()}: {t['content']}" for t in turns
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="Summarize the following conversation excerpt in 2-3 sentences. Preserve key facts, decisions, and named entities.",
        messages=[{"role": "user", "content": transcript}],
    )
    return resp.content[0].text

def compress_history(
    history: list[dict],
    keep_recent: int = 6,
    compress_threshold: int = 10,
) -> list[dict]:
    """
    Once history exceeds compress_threshold turns, summarize the oldest
    (len - keep_recent) turns into a single synthetic 'system' message.
    """
    if len(history) <= compress_threshold:
        return history

    old_turns = history[: len(history) - keep_recent]
    recent_turns = history[len(history) - keep_recent :]

    summary = summarize_turns(old_turns)
    summary_msg = {
        "role": "user",
        "content": f"[Conversation summary up to this point: {summary}]",
    }
    # Paired with a brief assistant ack so the history stays valid
    ack_msg = {"role": "assistant", "content": "Understood. Continuing from that context."}
    return [summary_msg, ack_msg] + recent_turns

def chat(history: list[dict], user_input: str, system: str = "") -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": user_input}]
    history = compress_history(history)
    kwargs = dict(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=history,
    )
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    reply = resp.content[0].text
    history = history + [{"role": "assistant", "content": reply}]
    return reply, history

if __name__ == "__main__":
    hist: list[dict] = []
    for i, q in enumerate([
        "What is machine learning?",
        "Give an example of supervised learning.",
        "What is overfitting?",
        "How do we prevent overfitting?",
        "What is cross-validation?",
        "What is a confusion matrix?",
        "Explain precision vs recall.",
        "What is the F1 score?",
        "When do I use ROC-AUC vs F1?",
        "Summarize everything we discussed.",
    ]):
        reply, hist = chat(hist, q)
        print(f"Turn {i+1} | history_len={len(hist)} | Q: {q[:40]}")
    print(f"\nFinal reply: {reply[:100]}...")

# Expected Token Savings: 40-70% reduction on long sessions; oldest turns collapsed to ~250 tokens
# Environment: multi-turn chatbots; works with any Claude model; Haiku summarizer adds ~250 tokens once
```

### Option 2: Sliding Window — Keep Only Last N Turns

```python
import anthropic
from collections import deque

client = anthropic.Anthropic()

class SlidingWindowHistory:
    """
    Maintains a fixed-size window of conversation turns.
    Oldest turns are discarded when window fills.
    """
    def __init__(self, max_turns: int = 8, system: str = ""):
        self._window: deque[dict] = deque(maxlen=max_turns)
        self._system = system
        self._total_turns = 0

    def add(self, role: str, content: str):
        self._window.append({"role": role, "content": content})
        self._total_turns += 1

    def messages(self) -> list[dict]:
        return list(self._window)

    def reply(self, user_input: str) -> str:
        self.add("user", user_input)
        kwargs = dict(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=self.messages(),
        )
        if self._system:
            kwargs["system"] = self._system
        resp = client.messages.create(**kwargs)
        text = resp.content[0].text
        self.add("assistant", text)
        return text

    @property
    def window_size(self) -> int:
        return len(self._window)

    @property
    def tokens_avoided(self) -> int:
        """Rough estimate: each dropped turn saves ~150 tokens average."""
        dropped = max(0, self._total_turns - self._window.maxlen)
        return dropped * 150

if __name__ == "__main__":
    agent = SlidingWindowHistory(max_turns=6, system="You are a helpful assistant.")
    questions = [
        "Hello, my name is Alex.",
        "I'm interested in Python async programming.",
        "What is asyncio?",
        "What are coroutines?",
        "How do I run multiple coroutines at once?",
        "What is asyncio.gather?",
        "What is asyncio.wait?",
        "Can I cancel a running coroutine?",
    ]
    for q in questions:
        r = agent.reply(q)
        print(f"window={agent.window_size} | Q: {q[:40]}")
    print(f"\nEstimated tokens avoided: {agent.tokens_avoided}")

# Expected Token Savings: proportional to session length; 8-turn window on a 20-turn session saves ~1800 tokens
# Environment: support bots, coding assistants; acceptable when old context is not needed for current answer
```

### Option 3: Importance-Weighted Pruning — Keep High-Signal Turns

```python
import anthropic
import json

client = anthropic.Anthropic()

def score_turn_importance(turn: dict) -> float:
    """
    Ask Haiku to rate the importance of a turn (0.0-1.0).
    High-importance turns contain decisions, facts, or constraints.
    """
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        system='Rate the importance of this conversation turn for future context (0.0-1.0). Reply with JSON: {"score": 0.7}',
        messages=[{"role": "user", "content": f"{turn['role']}: {turn['content'][:300]}"}],
    )
    try:
        data = json.loads(resp.content[0].text.strip())
        return float(data.get("score", 0.5))
    except Exception:
        return 0.5

def prune_history(
    history: list[dict],
    max_turns: int = 8,
    always_keep_last: int = 4,
) -> list[dict]:
    """
    If history exceeds max_turns, score all eligible turns and
    keep the top-scoring ones plus the always_keep_last recent turns.
    """
    if len(history) <= max_turns:
        return history

    recent = history[-always_keep_last:]
    candidates = history[:-always_keep_last]

    scored = [(score_turn_importance(t), t) for t in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)

    keep_count = max_turns - always_keep_last
    kept = [t for _, t in scored[:keep_count]]

    # Restore original order for kept candidates
    orig_idx = {id(t): i for i, t in enumerate(candidates)}
    kept.sort(key=lambda t: orig_idx.get(id(t), 0))

    return kept + recent

def chat(history: list[dict], user_input: str) -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": user_input}]
    history = prune_history(history, max_turns=8, always_keep_last=4)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=history,
    )
    reply = resp.content[0].text
    return reply, history + [{"role": "assistant", "content": reply}]

if __name__ == "__main__":
    history: list[dict] = []
    turns = [
        "My API key is sk-test-1234.",
        "I need to integrate with the payments API.",
        "The payment endpoint is POST /v1/charge.",
        "What's the weather today?",
        "By the way, my budget is $500/month.",
        "Can you remind me of the payment endpoint?",
    ]
    for q in turns:
        reply, history = chat(history, q)
        print(f"history_len={len(history)} | Q: {q[:50]}")
    print(f"\nFinal: {reply[:100]}")

# Expected Token Savings: 30-60%; semantic scoring keeps facts/constraints, drops chitchat
# Environment: task-oriented agents where some turns carry critical constraints
```

### Option 4: Token-Budget-Aware Truncation

```python
import anthropic

client = anthropic.Anthropic()
TOKENIZER_APPROX = 4  # chars per token (rough estimate)

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // TOKENIZER_APPROX)

def estimate_history_tokens(history: list[dict]) -> int:
    return sum(estimate_tokens(t["content"]) for t in history)

def fit_history_to_budget(
    history: list[dict],
    token_budget: int = 4000,
    always_keep_last: int = 2,
) -> list[dict]:
    """
    Drop oldest turns until total estimated tokens fit within budget.
    Always preserve the last `always_keep_last` turns.
    """
    if estimate_history_tokens(history) <= token_budget:
        return history

    protected = history[-always_keep_last:]
    candidates = list(history[:-always_keep_last])

    while candidates and estimate_history_tokens(candidates + protected) > token_budget:
        candidates.pop(0)

    return candidates + protected

def chat_with_budget(
    history: list[dict],
    user_input: str,
    token_budget: int = 4000,
    system: str = "",
) -> tuple[str, list[dict], int]:
    history = history + [{"role": "user", "content": user_input}]
    history = fit_history_to_budget(history, token_budget=token_budget)

    estimated = estimate_history_tokens(history)
    kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=min(1024, token_budget - estimated),
        messages=history,
    )
    if system:
        kwargs["system"] = system

    resp = client.messages.create(**kwargs)
    reply = resp.content[0].text
    actual_tokens = resp.usage.input_tokens + resp.usage.output_tokens
    history = history + [{"role": "assistant", "content": reply}]
    return reply, history, actual_tokens

if __name__ == "__main__":
    history: list[dict] = []
    total_tokens = 0
    for q in [
        "Tell me about the history of computing.",
        "Elaborate on vacuum tubes.",
        "What came after vacuum tubes?",
        "Explain transistors in detail.",
        "What is Moore's Law?",
        "What are modern CPUs made of?",
    ]:
        reply, history, tokens = chat_with_budget(history, q, token_budget=1500)
        total_tokens += tokens
        est = estimate_history_tokens(history)
        print(f"hist={len(history)} | est_tokens={est} | actual={tokens} | Q: {q[:40]}")
    print(f"\nTotal tokens: {total_tokens}")

# Expected Token Savings: hard cap ensures no single call exceeds budget; prevents surprise overruns
# Environment: cost-controlled deployments; useful when different users have different budgets
```

### Option 5: Hierarchical Compression — Compress in Tiers

```python
import anthropic

client = anthropic.Anthropic()

def compress_tier(turns: list[dict], target_sentences: int = 2) -> dict:
    """Reduce a tier of turns to a single summary message pair."""
    transcript = "\n".join(f"{t['role'].upper()}: {t['content']}" for t in turns)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=f"Summarize in exactly {target_sentences} sentences. Preserve names, numbers, and decisions.",
        messages=[{"role": "user", "content": transcript}],
    )
    summary = resp.content[0].text
    return {"role": "user", "content": f"[Prior context: {summary}]"}

class HierarchicalHistory:
    """
    Three tiers:
      - Tier 3 (oldest): compressed to 1 sentence
      - Tier 2: compressed to 2 sentences
      - Tier 1 (recent): raw turns
    """
    def __init__(self, tier1_size: int = 4, tier2_size: int = 6):
        self.tier1: list[dict] = []       # raw recent turns
        self.tier2_summary: dict | None = None  # compressed mid turns
        self.tier3_summary: dict | None = None  # compressed oldest turns
        self._tier1_size = tier1_size
        self._tier2_size = tier2_size
        self._tier2_buffer: list[dict] = []

    def _promote(self):
        """Move overflow from tier1 into tier2, tier2 into tier3."""
        if len(self.tier1) > self._tier1_size:
            overflow = self.tier1[: len(self.tier1) - self._tier1_size]
            self.tier1 = self.tier1[len(self.tier1) - self._tier1_size :]
            self._tier2_buffer.extend(overflow)

        if len(self._tier2_buffer) > self._tier2_size:
            promote_to_3 = self._tier2_buffer[: len(self._tier2_buffer) - self._tier2_size]
            self._tier2_buffer = self._tier2_buffer[len(self._tier2_buffer) - self._tier2_size :]
            if self.tier3_summary:
                combined = [self.tier3_summary] + promote_to_3
                self.tier3_summary = compress_tier(combined, target_sentences=1)
            else:
                self.tier3_summary = compress_tier(promote_to_3, target_sentences=1)

        if self._tier2_buffer:
            self.tier2_summary = compress_tier(self._tier2_buffer, target_sentences=2)

    def add(self, role: str, content: str):
        self.tier1.append({"role": role, "content": content})
        self._promote()

    def messages(self) -> list[dict]:
        msgs = []
        if self.tier3_summary:
            msgs.append(self.tier3_summary)
            msgs.append({"role": "assistant", "content": "Noted."})
        if self.tier2_summary:
            msgs.append(self.tier2_summary)
            msgs.append({"role": "assistant", "content": "Understood."})
        msgs.extend(self.tier1)
        return msgs

    def reply(self, user_input: str) -> str:
        self.add("user", user_input)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=self.messages(),
        )
        text = resp.content[0].text
        self.add("assistant", text)
        return text

if __name__ == "__main__":
    agent = HierarchicalHistory(tier1_size=4, tier2_size=6)
    for i, q in enumerate([
        "I'm building a REST API with FastAPI.",
        "I need JWT authentication.",
        "I want rate limiting per user.",
        "Should I use Redis for rate limiting?",
        "How do I set up Redis with Docker?",
        "What's the FastAPI dependency injection pattern?",
        "How do I write middleware?",
        "How do I add CORS?",
        "What about request logging?",
        "Summarize the architecture we decided on.",
    ]):
        r = agent.reply(q)
        msgs = agent.messages()
        print(f"Turn {i+1:2d} | msgs={len(msgs)} | Q: {q[:45]}")
    print(f"\nFinal: {r[:100]}")

# Expected Token Savings: 60-80% on 20+ turn sessions; tiered compression avoids single large summary call
# Environment: long-running assistant sessions; tier1 stays fresh, older context progressively compressed
```

### Option 6: Semantic Deduplication of Redundant Turns

```python
import anthropic
import hashlib

client = anthropic.Anthropic()

def semantic_hash(text: str) -> str:
    """Coarse hash: lowercase, strip whitespace, take first 200 chars."""
    normalized = " ".join(text.lower().split())[:200]
    return hashlib.md5(normalized.encode()).hexdigest()

def word_overlap(a: str, b: str) -> float:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

def deduplicate_history(
    history: list[dict],
    similarity_threshold: float = 0.85,
) -> list[dict]:
    """
    Remove turns that are highly similar to a later turn in the same role.
    Preserves conversation validity (alternating user/assistant structure).
    """
    if len(history) <= 2:
        return history

    keep_flags = [True] * len(history)
    for i in range(len(history) - 1):
        if not keep_flags[i]:
            continue
        for j in range(i + 1, len(history)):
            if history[i]["role"] != history[j]["role"]:
                continue
            sim = word_overlap(history[i]["content"], history[j]["content"])
            if sim >= similarity_threshold:
                keep_flags[i] = False  # keep the later (more recent) occurrence
                break

    # Ensure alternating roles after dedup
    result = [t for t, keep in zip(history, keep_flags) if keep]
    cleaned = []
    last_role = None
    for turn in result:
        if turn["role"] == last_role:
            # merge with previous rather than drop
            cleaned[-1]["content"] += " " + turn["content"]
        else:
            cleaned.append(dict(turn))
            last_role = turn["role"]
    return cleaned

def chat(history: list[dict], user_input: str) -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": user_input}]
    before = len(history)
    history = deduplicate_history(history)
    after = len(history)
    if before != after:
        print(f"  [dedup: {before} → {after} turns]")

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=history,
    )
    reply = resp.content[0].text
    return reply, history + [{"role": "assistant", "content": reply}]

if __name__ == "__main__":
    history: list[dict] = []
    turns = [
        "How do I sort a list in Python?",
        "Can you show me how to sort a list in Python?",  # near-duplicate
        "What about sorting by a key?",
        "How do I sort a list in Python with a custom key?",  # near-duplicate
        "What is a lambda function?",
        "How do I use a lambda to sort?",
    ]
    for q in turns:
        reply, history = chat(history, q)
        print(f"hist={len(history):2d} | Q: {q[:50]}")

# Expected Token Savings: 20-40% on repetitive sessions (user rephrases same question); zero API cost
# Environment: user-facing chat where users rephrase; prevents paying for duplicate context
```

## Comparison

| Option | Strategy | Token Reduction | Accuracy Risk | API Cost |
|--------|----------|----------------|---------------|----------|
| 1 — Rolling summary | Summarize oldest N turns | 40-70% | Low (summary is accurate) | 1 Haiku call per compress |
| 2 — Sliding window | Drop oldest turns entirely | Proportional to session length | Medium (old context lost) | Zero extra |
| 3 — Importance pruning | Score + keep high-signal turns | 30-60% | Low (key facts kept) | 1 Haiku call per turn |
| 4 — Token budget | Hard token cap via truncation | Configurable hard cap | Medium (oldest dropped) | Zero extra |
| 5 — Hierarchical tiers | Compress in 3 tiers | 60-80% | Low (tiered granularity) | 1 Haiku call per tier promotion |
| 6 — Semantic dedup | Remove near-duplicate turns | 20-40% | Very low (exact dedup) | Zero extra |
