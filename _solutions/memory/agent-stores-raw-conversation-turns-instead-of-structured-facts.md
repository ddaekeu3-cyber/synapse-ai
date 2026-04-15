---
layout: solution
title: "Agent Stores Raw Conversation Turns Instead of Structured Facts"
category: memory
description: "Agent saves entire conversation blobs to memory — including filler words, clarifying questions, and redundant context — instead of extracting and storing structured facts, causing bloated memory that wastes tokens on retrieval."
tags: [memory, extraction, structured-data, token-cost, efficiency]
---

## Symptom

The memory store grows rapidly — a 10-turn conversation produces 10 large memory blobs. On the next session, the full conversation history is injected into context, costing thousands of tokens for information that could fit in 5 bullet points. Duplicate and contradictory facts are stored because each turn is saved verbatim. Searching memory returns entire conversation chunks when only a single fact was needed.

## Root Cause

The agent calls `memory.save(conversation_turn)` at the end of each turn, storing the full message pair. This feels safe — nothing is lost — but it conflates *storing everything* with *storing what matters*. A 500-word exchange might contain 3 actionable facts and 497 words of conversational scaffolding. Retrieving this memory later costs tokens proportional to the original conversation length, not the information density.

## Fix

### Option 1: Haiku fact extractor — convert turns to structured facts before storing

```python
import json
import anthropic

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()

# Structured memory store: {user_id: [fact_dict, ...]}
_memory: dict[str, list[dict]] = {}


def extract_facts_from_turn(user_message: str, assistant_response: str) -> list[dict]:
    """
    Use Haiku to extract structured facts from a conversation turn.
    Returns a list of fact objects rather than the raw text.
    """
    prompt = f"""Extract facts worth remembering from this conversation turn.
Return a JSON array of fact objects. Only include facts that are:
- User preferences, personal details, or stated goals
- Decisions made or conclusions reached
- Specific values, names, dates, or settings chosen
- Technical requirements or constraints stated

User: {user_message[:500]}
Assistant: {assistant_response[:500]}

Return JSON array: [{{"fact": "...", "category": "preference|decision|detail|constraint", "confidence": "high|medium"}}]
Return [] if nothing is worth remembering."""

    response = haiku.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    try:
        # Extract JSON array from response
        import re
        json_match = re.search(r"\[.*?\]", text, re.DOTALL)
        if json_match:
            facts = json.loads(json_match.group())
            return [f for f in facts if isinstance(f, dict) and "fact" in f]
    except (json.JSONDecodeError, AttributeError):
        pass
    return []


def save_facts(user_id: str, facts: list[dict]) -> None:
    if user_id not in _memory:
        _memory[user_id] = []
    _memory[user_id].extend(facts)
    # Deduplicate by fact text
    seen = set()
    deduped = []
    for f in _memory[user_id]:
        key = f["fact"].lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    _memory[user_id] = deduped


def build_memory_context(user_id: str) -> str:
    facts = _memory.get(user_id, [])
    if not facts:
        return ""
    lines = [f"- [{f.get('category', 'fact')}] {f['fact']}" for f in facts]
    return f"\n\n<user_memory>\n" + "\n".join(lines) + "\n</user_memory>"


def chat_with_memory(user_id: str, user_message: str) -> str:
    memory_ctx = build_memory_context(user_id)
    system = f"You are a helpful assistant.{memory_ctx}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    reply = response.content[0].text

    # Extract and store structured facts (not the raw turn)
    facts = extract_facts_from_turn(user_message, reply)
    if facts:
        save_facts(user_id, facts)
        print(f"[Memory] Stored {len(facts)} facts: {[f['fact'][:50] for f in facts]}")

    return reply


# Demo: multi-turn conversation
user_id = "alice"
chat_with_memory(user_id, "I prefer dark mode and my timezone is PST.")
chat_with_memory(user_id, "I'm building a Python API that needs to handle 10k requests per second.")
chat_with_memory(user_id, "I decided to use FastAPI with asyncpg for the database layer.")

print(f"\nStored memory for {user_id}:")
for fact in _memory.get(user_id, []):
    print(f"  [{fact.get('category')}] {fact['fact']}")

print(f"\nMemory context size: {len(build_memory_context(user_id))} chars")
```

**Expected Token Savings:** Structured facts use 50–200 tokens vs. 500–2,000 tokens for raw conversation blobs; 90%+ reduction in memory injection cost.
**Environment:** Python 3.9+; Haiku extraction costs ~100 tokens per turn; break-even after 2 sessions.

---

### Option 2: Schema-driven fact extraction with typed fields

```python
import json
import anthropic
from dataclasses import dataclass, field, asdict
from datetime import datetime

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()


@dataclass
class UserFact:
    """Typed structure for a stored fact."""
    fact_id: str
    category: str           # "preference", "goal", "constraint", "decision", "detail"
    key: str                # Short identifier: "language", "timezone", "model_choice"
    value: str              # The actual fact value
    confidence: str         # "high", "medium", "low"
    source_turn: int        # Which turn this came from
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    expires_after_turns: int | None = None  # None = permanent


class StructuredMemoryStore:
    def __init__(self):
        self._facts: dict[str, dict[str, UserFact]] = {}  # user_id → {key: fact}
        self._turn_count: dict[str, int] = {}

    def upsert(self, user_id: str, fact: UserFact) -> None:
        """Insert or update a fact by key — prevents duplicate facts for same key."""
        if user_id not in self._facts:
            self._facts[user_id] = {}
        self._facts[user_id][fact.key] = fact

    def get_all(self, user_id: str) -> list[UserFact]:
        """Return all facts for a user, filtering expired ones."""
        facts = list(self._facts.get(user_id, {}).values())
        current_turn = self._turn_count.get(user_id, 0)
        return [
            f for f in facts
            if f.expires_after_turns is None or current_turn <= f.source_turn + f.expires_after_turns
        ]

    def increment_turn(self, user_id: str) -> int:
        self._turn_count[user_id] = self._turn_count.get(user_id, 0) + 1
        return self._turn_count[user_id]

    def to_context_string(self, user_id: str) -> str:
        facts = self.get_all(user_id)
        if not facts:
            return ""
        by_category: dict[str, list[UserFact]] = {}
        for f in facts:
            by_category.setdefault(f.category, []).append(f)
        lines = []
        for cat, cat_facts in sorted(by_category.items()):
            lines.append(f"[{cat.upper()}]")
            for f in cat_facts:
                lines.append(f"  {f.key}: {f.value}")
        return "\n<user_profile>\n" + "\n".join(lines) + "\n</user_profile>"


store = StructuredMemoryStore()


def extract_typed_facts(user_message: str, assistant_reply: str, turn: int) -> list[UserFact]:
    prompt = f"""Extract typed facts from this conversation turn.
Return a JSON array. Only include genuinely new information worth remembering.

User: {user_message[:400]}
Assistant: {assistant_reply[:400]}

JSON format:
[{{"key": "short_identifier", "category": "preference|goal|constraint|decision|detail", "value": "the fact", "confidence": "high|medium"}}]

Examples of good facts:
  {{"key": "preferred_language", "category": "preference", "value": "Python", "confidence": "high"}}
  {{"key": "target_rps", "category": "constraint", "value": "10,000 requests per second", "confidence": "high"}}
  {{"key": "db_choice", "category": "decision", "value": "PostgreSQL with asyncpg", "confidence": "high"}}

Return [] if nothing worth storing."""

    response = haiku.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    import re, uuid
    text = response.content[0].text
    try:
        json_match = re.search(r"\[.*?\]", text, re.DOTALL)
        if json_match:
            raw_facts = json.loads(json_match.group())
            return [
                UserFact(
                    fact_id=str(uuid.uuid4())[:8],
                    category=f.get("category", "detail"),
                    key=f.get("key", f"fact_{i}"),
                    value=f.get("value", ""),
                    confidence=f.get("confidence", "medium"),
                    source_turn=turn,
                )
                for i, f in enumerate(raw_facts)
                if isinstance(f, dict) and f.get("value")
            ]
    except (json.JSONDecodeError, AttributeError):
        pass
    return []


def chat(user_id: str, message: str) -> str:
    turn = store.increment_turn(user_id)
    context = store.to_context_string(user_id)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"You are a helpful assistant.{context}",
        messages=[{"role": "user", "content": message}],
    )
    reply = response.content[0].text

    # Store typed facts, not raw turn
    facts = extract_typed_facts(message, reply, turn)
    for fact in facts:
        store.upsert(user_id, fact)
        print(f"  [Stored] {fact.key}: {fact.value} ({fact.category})")

    return reply


chat("user-1", "I prefer concise responses without bullet points.")
chat("user-1", "My project needs to support 50 concurrent users and uses MySQL.")
chat("user-1", "I chose React for the frontend after evaluating Vue and Svelte.")

print(store.to_context_string("user-1"))
```

**Expected Token Savings:** Keyed facts prevent duplicate storage (same `key` overwrites instead of appending); typed schema enables targeted retrieval; 80–95% size reduction vs. raw turns.
**Environment:** Python 3.10+; dataclass schema enforces fact structure; key-based upsert handles preference updates correctly.

---

### Option 3: Conversation summarizer that compresses multi-turn history

```python
import anthropic

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()


def summarize_conversation_to_facts(conversation: list[dict]) -> str:
    """
    Compress an entire conversation to a structured fact summary.
    Called when preparing memory for the next session — not after each turn.
    """
    # Format conversation for summarization
    formatted = []
    for msg in conversation:
        role = msg["role"].capitalize()
        content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        formatted.append(f"{role}: {content[:300]}")

    convo_text = "\n\n".join(formatted)

    response = haiku.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=(
            "You extract persistent facts from conversations for long-term memory storage. "
            "Ignore pleasantries, filler, and conversational scaffolding. "
            "Focus only on: user preferences, decisions made, stated goals, constraints, "
            "personal details, and technical requirements."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Extract all facts worth remembering from this conversation.\n\n"
                f"{convo_text}\n\n"
                "Format your output as:\n"
                "PREFERENCES: [list]\n"
                "DECISIONS: [list]\n"
                "GOALS: [list]\n"
                "CONSTRAINTS: [list]\n"
                "DETAILS: [list]\n"
                "Only include non-empty sections."
            ),
        }],
    )

    summary = response.content[0].text
    original_chars = sum(len(m.get("content", "")) for m in conversation if isinstance(m.get("content"), str))
    print(f"[Compression] {original_chars:,} chars → {len(summary):,} chars ({100*(1-len(summary)/max(original_chars,1)):.0f}% reduction)")
    return summary


class SessionManager:
    """Manages conversation history and end-of-session compression."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.conversation: list[dict] = []
        self.persistent_facts: str = ""  # Loaded from storage at session start

    def load_facts(self, facts: str) -> None:
        self.persistent_facts = facts

    def add_turn(self, user_msg: str, assistant_reply: str) -> None:
        self.conversation.append({"role": "user", "content": user_msg})
        self.conversation.append({"role": "assistant", "content": assistant_reply})

    def compress_and_save(self) -> str:
        """End-of-session: compress this session's conversation to facts."""
        if not self.conversation:
            return self.persistent_facts

        new_facts = summarize_conversation_to_facts(self.conversation)

        if self.persistent_facts:
            # Merge with existing facts (Haiku merges and deduplicates)
            merge_response = haiku.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Merge these two fact summaries, removing duplicates and keeping the most recent version of any updated facts:\n\n"
                        f"EXISTING FACTS:\n{self.persistent_facts}\n\n"
                        f"NEW FACTS:\n{new_facts}\n\n"
                        "Return the merged fact summary."
                    ),
                }],
            )
            return merge_response.content[0].text
        return new_facts

    def build_system_prompt(self) -> str:
        if self.persistent_facts:
            return f"You are a helpful assistant.\n\n<known_facts>\n{self.persistent_facts}\n</known_facts>"
        return "You are a helpful assistant."


def run_session(user_id: str, messages: list[str], existing_facts: str = "") -> str:
    session = SessionManager(user_id)
    session.load_facts(existing_facts)

    for user_msg in messages:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=session.build_system_prompt(),
            messages=[{"role": "user", "content": user_msg}],
        )
        reply = response.content[0].text
        session.add_turn(user_msg, reply)
        print(f"Turn: {user_msg[:60]} → {reply[:80]}")

    return session.compress_and_save()


# Session 1
facts_after_s1 = run_session("alice", [
    "My name is Alice and I work in Berlin.",
    "I prefer Python over JavaScript for backend work.",
    "I'm building a real-time dashboard that needs WebSocket support.",
])
print(f"\nFacts after session 1:\n{facts_after_s1}\n")

# Session 2 — starts with facts from session 1
facts_after_s2 = run_session("alice", [
    "I decided to use FastAPI with WebSockets.",
    "The dashboard needs to support 500 concurrent users.",
], existing_facts=facts_after_s1)
print(f"\nFacts after session 2:\n{facts_after_s2}")
```

**Expected Token Savings:** End-of-session compression converts 2,000–10,000 chars of conversation to 200–500 chars of facts; multi-session agents stay lean indefinitely.
**Environment:** Python 3.9+; Haiku compression costs ~200 tokens per session; merge cost is ~100 tokens.

---

### Option 4: Incremental fact store with recency-weighted retrieval

```python
import time
import math
import anthropic

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()


class WeightedFactStore:
    """
    Stores structured facts with timestamps.
    Retrieval weights recent facts higher than old ones.
    """

    def __init__(self, max_facts: int = 100, decay_half_life_hours: float = 24.0):
        self._facts: list[dict] = []
        self.max_facts = max_facts
        self.decay_lambda = math.log(2) / decay_half_life_hours

    def add(self, fact: str, category: str, confidence: str = "high") -> None:
        self._facts.append({
            "fact": fact,
            "category": category,
            "confidence": confidence,
            "timestamp": time.time(),
        })
        # Evict oldest low-confidence facts if over limit
        if len(self._facts) > self.max_facts:
            self._facts.sort(key=lambda f: (f["confidence"] == "high", f["timestamp"]))
            self._facts = self._facts[-self.max_facts:]

    def get_relevant(self, query: str, top_k: int = 10) -> list[dict]:
        """Return top_k facts, weighted by recency and confidence."""
        now = time.time()
        scored = []
        query_words = set(query.lower().split())

        for fact in self._facts:
            # Recency score: exponential decay
            age_hours = (now - fact["timestamp"]) / 3600
            recency = math.exp(-self.decay_lambda * age_hours)

            # Relevance score: keyword overlap
            fact_words = set(fact["fact"].lower().split())
            relevance = len(query_words & fact_words) / max(len(query_words), 1)

            # Confidence multiplier
            conf_mult = 1.0 if fact["confidence"] == "high" else 0.6

            score = (0.4 * recency + 0.6 * relevance) * conf_mult
            scored.append((score, fact))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored[:top_k]]

    def to_context(self, query: str) -> str:
        relevant = self.get_relevant(query)
        if not relevant:
            return ""
        lines = [f"- [{f['category']}] {f['fact']}" for f in relevant]
        return "<relevant_facts>\n" + "\n".join(lines) + "\n</relevant_facts>"


store = WeightedFactStore(max_facts=50, decay_half_life_hours=48)


def extract_and_store_facts(user_msg: str, assistant_reply: str) -> None:
    response = haiku.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"Extract 0–3 facts worth long-term storage from:\n"
                f"User: {user_msg[:300]}\nAssistant: {assistant_reply[:300]}\n\n"
                "Return one fact per line: CATEGORY|CONFIDENCE|fact text\n"
                "Categories: preference, goal, decision, constraint, detail\n"
                "Return NONE if nothing worth storing."
            ),
        }],
    )

    for line in response.content[0].text.strip().split("\n"):
        if "|" in line and line.upper() != "NONE":
            parts = line.split("|", 2)
            if len(parts) == 3:
                cat, conf, fact_text = parts
                store.add(fact_text.strip(), cat.strip().lower(), conf.strip().lower())
                print(f"  [+fact] {cat.strip()}: {fact_text.strip()[:60]}")


def chat(user_message: str) -> str:
    context = store.to_context(user_message)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=f"You are a helpful assistant.{chr(10) + context if context else ''}",
        messages=[{"role": "user", "content": user_message}],
    )
    reply = response.content[0].text
    extract_and_store_facts(user_message, reply)
    return reply


chat("I'm Alice, a backend engineer who prefers Go for high-performance services.")
chat("I'm evaluating gRPC vs REST for my internal microservices.")
chat("My team decided to use gRPC with Protocol Buffers.")
chat("The services need to handle 50k QPS with p99 latency under 10ms.")

print(f"\n{len(store._facts)} facts stored. Sample context for 'performance':")
print(store.to_context("high performance latency"))
```

**Expected Token Savings:** Recency weighting ensures only relevant facts are injected; max_facts cap prevents unbounded growth; selective retrieval serves 5–10 facts vs. full history.
**Environment:** Python 3.9+; pure Python math; replace keyword relevance with embedding similarity for production semantic search.

---

### Option 5: Diff-based fact update — only store what changed

```python
import json
import anthropic

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()


def compute_fact_diff(existing_facts: dict[str, str], new_turn_summary: str) -> dict:
    """
    Use Haiku to identify what new information to add, update, or remove
    based on a conversation turn summary. Returns a diff, not a full snapshot.
    """
    existing_json = json.dumps(existing_facts, indent=2) if existing_facts else "{}"

    response = haiku.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Given these existing user facts:\n{existing_json}\n\n"
                f"And this new information from the latest conversation turn:\n{new_turn_summary}\n\n"
                "Return a JSON diff object with ONLY the changes:\n"
                "{\"add\": {\"key\": \"value\"}, \"update\": {\"key\": \"new_value\"}, \"remove\": [\"key\"]}\n"
                "Use empty objects/arrays if no changes in that category.\n"
                "Only include facts that actually changed — don't re-state existing unchanged facts."
            ),
        }],
    )

    import re
    text = response.content[0].text
    try:
        json_match = re.search(r"\{.*?\}", text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"add": {}, "update": {}, "remove": []}


def apply_diff(facts: dict[str, str], diff: dict) -> dict[str, str]:
    """Apply a fact diff to the current fact store."""
    updated = dict(facts)
    for k, v in diff.get("add", {}).items():
        updated[k] = v
    for k, v in diff.get("update", {}).items():
        if k in updated:
            updated[k] = v
    for k in diff.get("remove", []):
        updated.pop(k, None)
    return updated


class DiffMemoryStore:
    def __init__(self):
        self._facts: dict[str, dict[str, str]] = {}  # user_id → {key: value}

    def get(self, user_id: str) -> dict[str, str]:
        return self._facts.get(user_id, {})

    def update(self, user_id: str, turn_summary: str) -> dict:
        existing = self.get(user_id)
        diff = compute_fact_diff(existing, turn_summary)

        changes = sum([len(diff.get("add", {})), len(diff.get("update", {})), len(diff.get("remove", []))])
        if changes > 0:
            self._facts[user_id] = apply_diff(existing, diff)
            print(f"  [Diff] +{len(diff.get('add', {}))} update={len(diff.get('update', {}))} -{len(diff.get('remove', []))}")

        return diff

    def to_context(self, user_id: str) -> str:
        facts = self.get(user_id)
        if not facts:
            return ""
        lines = [f"  {k}: {v}" for k, v in facts.items()]
        return "\n<user_facts>\n" + "\n".join(lines) + "\n</user_facts>"


store = DiffMemoryStore()


def get_turn_summary(user_msg: str, assistant_reply: str) -> str:
    """Extract a brief turn summary for diff computation."""
    response = haiku.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"Summarize any NEW facts stated by the user in 1–3 short sentences:\nUser: {user_msg[:300]}\nAssistant: {assistant_reply[:300]}",
        }],
    )
    return response.content[0].text.strip()


def chat(user_id: str, user_message: str) -> str:
    context = store.to_context(user_id)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=f"You are a helpful assistant.{context}",
        messages=[{"role": "user", "content": user_message}],
    )
    reply = response.content[0].text

    turn_summary = get_turn_summary(user_message, reply)
    store.update(user_id, turn_summary)
    return reply


chat("alice", "I prefer Python and my timezone is EST.")
chat("alice", "Actually I moved to Berlin so my timezone is now CET.")  # Should update timezone
chat("alice", "I want to build a FastAPI backend.")
print(f"\nFinal facts:\n{json.dumps(store.get('alice'), indent=2)}")
```

**Expected Token Savings:** Diff-based updates prevent duplicate storage of unchanged facts; only deltas are processed; fact store stays compact regardless of conversation length.
**Environment:** Python 3.10+; diff pattern handles preference updates correctly (e.g., timezone change overwrites instead of appending).

---

### Option 6: Tiered memory — working memory vs. long-term structured store

```python
import anthropic
from dataclasses import dataclass, field
from collections import deque

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()


@dataclass
class TieredMemory:
    """
    Two-tier memory:
    - Working memory: last N raw turns (fast, no extraction)
    - Long-term: structured facts extracted at tier promotion
    """
    working_window: int = 5  # Keep last 5 turns raw
    long_term: dict[str, str] = field(default_factory=dict)
    _working: deque = field(default_factory=lambda: deque(maxlen=5))
    _promotion_count: int = 0

    def add_turn(self, user: str, assistant: str) -> None:
        """Add to working memory. Promote oldest turn to long-term if window is full."""
        if len(self._working) == self._working.maxlen:
            oldest_user, oldest_asst = self._working[0]
            self._promote_to_long_term(oldest_user, oldest_asst)
        self._working.append((user, assistant))

    def _promote_to_long_term(self, user_msg: str, assistant_reply: str) -> None:
        """Extract facts from an evicted working memory turn and store in long-term."""
        response = haiku.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    f"Extract key-value facts from this conversation turn. "
                    f"Return as: key: value (one per line). Max 3 facts.\n"
                    f"User: {user_msg[:200]}\nAssistant: {assistant_reply[:200]}\n"
                    "Return NONE if nothing worth storing long-term."
                ),
            }],
        )
        self._promotion_count += 1
        text = response.content[0].text.strip()
        if text.upper() == "NONE":
            return

        for line in text.split("\n"):
            if ": " in line:
                key, _, value = line.partition(": ")
                self.long_term[key.strip().lower().replace(" ", "_")] = value.strip()
                print(f"  [Promote] {key.strip()}: {value.strip()[:50]}")

    def build_system_context(self) -> str:
        """Build context from long-term facts + recent working memory turns."""
        parts = []

        if self.long_term:
            facts = "\n".join(f"  {k}: {v}" for k, v in self.long_term.items())
            parts.append(f"<long_term_memory>\n{facts}\n</long_term_memory>")

        if self._working:
            recent = "\n".join(
                f"User: {u[:100]}\nAssistant: {a[:100]}"
                for u, a in list(self._working)[-2:]  # Only last 2 turns
            )
            parts.append(f"<recent_context>\n{recent}\n</recent_context>")

        return "\n\n".join(parts) if parts else ""

    def stats(self) -> str:
        return (
            f"Working: {len(self._working)}/{self.working_window} turns, "
            f"Long-term: {len(self.long_term)} facts, "
            f"Promotions: {self._promotion_count}"
        )


mem = TieredMemory(working_window=3)


def chat(user_message: str) -> str:
    ctx = mem.build_system_context()
    system = f"You are a helpful assistant.{chr(10) + ctx if ctx else ''}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    reply = response.content[0].text
    mem.add_turn(user_message, reply)
    return reply


# 8 turns — working memory fills and promotes
turns = [
    "Hi, I'm building a Python CLI tool.",
    "It needs to parse YAML configuration files.",
    "I prefer click over argparse for CLI frameworks.",
    "The tool should support Linux and macOS.",
    "I want to distribute it via PyPI.",
    "It needs to work with Python 3.10 and above.",
    "I'll use pytest for testing.",
    "Should I use poetry or setuptools for packaging?",
]

for msg in turns:
    reply = chat(msg)
    print(f"Turn: {msg[:60]}")

print(f"\n{mem.stats()}")
print(f"Long-term facts: {mem.long_term}")
```

**Expected Token Savings:** Tiered approach injects structured long-term facts (~200 tokens) + only last 2 raw turns (~400 tokens) instead of full 8-turn history (~4,000 tokens); 80% reduction.
**Environment:** Python 3.10+; `deque(maxlen=N)` auto-evicts oldest entries; promotion triggers exactly when working memory fills.

---

| Option | Approach | Storage Unit | Best For |
|--------|----------|-------------|----------|
| 1 | Haiku turn extractor | Fact list per turn | Simple single-user agents |
| 2 | Typed schema with upsert | Keyed UserFact objects | Multi-field user profiles |
| 3 | End-of-session compressor | Session summary | Long-running multi-session agents |
| 4 | Recency-weighted retrieval | Scored fact list | High-volume fact accumulation |
| 5 | Diff-based updates | Key-value delta | Preference tracking with updates |
| 6 | Tiered working + long-term | Promoted structured facts | Agentic assistants with long context |
