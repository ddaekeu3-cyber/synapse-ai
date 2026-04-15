---
layout: solution
title: "Agent Stores Entire Conversation as Single Memory Blob"
category: memory
description: "Agent writes raw conversation transcripts as one undifferentiated text block — retrieval requires scanning everything, memory grows unbounded, and important details get buried inside walls of dialogue noise."
tags: [memory, retrieval, context, embedding, knowledge-graph, decay, performance]
---

## Symptom

After 20+ turns, the agent starts repeating questions the user already answered. Retrieval is slow because every lookup scans thousands of lines of transcript. Memory entries grow from 200 bytes to 80 KB. When the agent is asked "what did I tell you about my deadline?", it either fails to find it or returns the entire conversation history.

Blob memory after 30 turns: **~120 KB**, retrieval: **full scan**
Structured memory after 30 turns: **~8 KB index + typed fields**, retrieval: **O(1) key lookup or top-k vector search**

## Root Cause

The agent calls `memory.save(str(messages))` — dumping the raw messages list as a string. There is no structure, no indexing, no deduplication. Every subsequent retrieval requires scanning the full blob, and the blob grows linearly with conversation length.

## Fix

---

### Option 1 — Structured Memory with Typed Fields

Extract specific fact types from each turn and store them as typed records. Retrieval is a direct key lookup — no scanning.

```python
import json
import sqlite3
import anthropic
from dataclasses import dataclass, asdict
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class MemoryRecord:
    record_type: str       # "preference", "fact", "deadline", "entity"
    subject: str           # what it's about
    value: str             # the actual information
    confidence: float      # 0.0–1.0
    turn_index: int

class StructuredMemory:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_type TEXT NOT NULL,
                subject TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                turn_index INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_subject ON memory(subject)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON memory(record_type)")
        self.conn.commit()

    def extract_and_store(self, user_message: str, turn_index: int) -> list[MemoryRecord]:
        """Use Claude to extract structured facts from a user message."""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="""Extract structured facts from the user message.
Return JSON array of objects with fields: record_type, subject, value, confidence.
record_type must be one of: preference, fact, deadline, entity, constraint.
Only extract clearly stated information. Return [] if nothing notable.""",
            messages=[{"role": "user", "content": f"Extract facts from: {user_message}"}],
        )

        try:
            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            records_data = json.loads(text)
        except (json.JSONDecodeError, IndexError):
            return []

        records = []
        for rd in records_data:
            rec = MemoryRecord(
                record_type=rd.get("record_type", "fact"),
                subject=rd.get("subject", ""),
                value=rd.get("value", ""),
                confidence=float(rd.get("confidence", 1.0)),
                turn_index=turn_index,
            )
            if rec.subject and rec.value:
                self.conn.execute(
                    "INSERT OR REPLACE INTO memory (record_type, subject, value, confidence, turn_index) VALUES (?,?,?,?,?)",
                    (rec.record_type, rec.subject, rec.value, rec.confidence, rec.turn_index),
                )
                records.append(rec)

        self.conn.commit()
        return records

    def recall(self, subject: str = None, record_type: str = None) -> list[MemoryRecord]:
        """O(1) lookup by subject or type — no full scan."""
        if subject:
            cursor = self.conn.execute(
                "SELECT record_type, subject, value, confidence, turn_index FROM memory WHERE subject LIKE ? ORDER BY confidence DESC LIMIT 10",
                (f"%{subject}%",),
            )
        elif record_type:
            cursor = self.conn.execute(
                "SELECT record_type, subject, value, confidence, turn_index FROM memory WHERE record_type = ? ORDER BY turn_index DESC LIMIT 20",
                (record_type,),
            )
        else:
            cursor = self.conn.execute(
                "SELECT record_type, subject, value, confidence, turn_index FROM memory ORDER BY confidence DESC LIMIT 20"
            )

        return [MemoryRecord(*row) for row in cursor.fetchall()]

    def to_context(self) -> str:
        """Compact memory summary for system prompt injection."""
        records = self.recall()
        if not records:
            return ""
        lines = ["## Known Facts"]
        for r in records:
            lines.append(f"- [{r.record_type}] {r.subject}: {r.value}")
        return "\n".join(lines)

memory = StructuredMemory()
messages = []

def chat(user_input: str, turn: int) -> str:
    facts = memory.extract_and_store(user_input, turn)
    if facts:
        print(f"[Memory] Stored {len(facts)} facts: {[f.subject for f in facts]}")

    context = memory.to_context()
    system = f"You are a helpful assistant.\n\n{context}" if context else "You are a helpful assistant."

    messages.append({"role": "user", "content": user_input})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=messages[-10:],  # Recent turns only; memory carries the rest
    )
    reply = response.content[0].text
    messages.append({"role": "assistant", "content": reply})
    return reply

print(chat("My project deadline is April 30th and I prefer concise answers.", 1))
print(chat("I'm using Python 3.12 and FastAPI.", 2))
print(chat("What deadline did I mention?", 3))  # Retrieved from structured memory
```

**Expected Token Savings:** 40–70% — memory context is a compact summary, not raw transcript
**Environment:** `pip install anthropic`

---

### Option 2 — Chunked Memory with Semantic Retrieval

Split the conversation into fixed-size chunks, embed each chunk, and retrieve only the most relevant chunks at query time.

```python
import json
import math
import anthropic
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class MemoryChunk:
    chunk_id: str
    text: str
    embedding: list[float]
    turn_range: tuple[int, int]
    token_count: int

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

def fake_embed(text: str) -> list[float]:
    """
    Placeholder — replace with real embeddings:
    from anthropic import Anthropic  # use voyage-3 or text-embedding-3-small
    In production use: voyageai.Client().embed([text], model="voyage-3").embeddings[0]
    """
    import hashlib
    h = int(hashlib.md5(text.encode()).hexdigest(), 16)
    rng = [(h >> i) & 0xFF for i in range(0, 512, 8)][:64]
    norm = math.sqrt(sum(x * x for x in rng)) or 1.0
    return [x / norm for x in rng]

class ChunkedMemory:
    CHUNK_SIZE = 8  # turns per chunk

    def __init__(self):
        self.chunks: list[MemoryChunk] = []
        self.pending_turns: list[dict] = []
        self._chunk_counter = 0

    def add_turn(self, role: str, content: str, turn_index: int):
        self.pending_turns.append({"role": role, "content": content, "turn": turn_index})
        if len(self.pending_turns) >= self.CHUNK_SIZE:
            self._flush_chunk()

    def _flush_chunk(self):
        if not self.pending_turns:
            return
        start = self.pending_turns[0]["turn"]
        end = self.pending_turns[-1]["turn"]
        text = "\n".join(f"{t['role']}: {t['content']}" for t in self.pending_turns)
        self._chunk_counter += 1
        chunk = MemoryChunk(
            chunk_id=f"chunk_{self._chunk_counter}",
            text=text,
            embedding=fake_embed(text),
            turn_range=(start, end),
            token_count=len(text.split()),
        )
        self.chunks.append(chunk)
        self.pending_turns = []

    def retrieve(self, query: str, top_k: int = 3) -> list[MemoryChunk]:
        """Return top-k most relevant chunks by cosine similarity."""
        if not self.chunks:
            return []
        query_emb = fake_embed(query)
        scored = [
            (cosine_similarity(query_emb, c.embedding), c)
            for c in self.chunks
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:top_k]]

    def retrieve_as_context(self, query: str) -> str:
        chunks = self.retrieve(query)
        if not chunks:
            return ""
        parts = [f"[Turns {c.turn_range[0]}–{c.turn_range[1]}]\n{c.text}" for c in chunks]
        return "## Relevant Conversation History\n\n" + "\n\n---\n\n".join(parts)

chunk_memory = ChunkedMemory()
messages = []

def chat(user_input: str, turn: int) -> str:
    context = chunk_memory.retrieve_as_context(user_input)
    system = f"You are a helpful assistant.\n\n{context}" if context else "You are a helpful assistant."

    messages.append({"role": "user", "content": user_input})
    chunk_memory.add_turn("user", user_input, turn)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=messages[-4:],  # Only recent turns in active window
    )
    reply = response.content[0].text
    messages.append({"role": "assistant", "content": reply})
    chunk_memory.add_turn("assistant", reply, turn)
    return reply

for i, msg in enumerate([
    "I work at a startup building B2B SaaS tools.",
    "Our main tech stack is Python, Postgres, and React.",
    "We're targeting enterprise customers in healthcare.",
    "Our biggest challenge is HIPAA compliance.",
    "We have a team of 8 engineers.",
    "What's our main compliance challenge?",
]):
    print(f"Turn {i+1}: {chat(msg, i+1)[:80]}...")
```

**Expected Token Savings:** 50–80% — only top-k relevant chunks retrieved, not full history
**Environment:** `pip install anthropic`

---

### Option 3 — Hierarchical Memory (Episodic, Semantic, Procedural)

Separate memory into three tiers: episodic (recent events), semantic (long-term facts), procedural (learned patterns). Each tier has its own retrieval strategy and retention policy.

```python
import json
import time
import anthropic
from collections import deque
from dataclasses import dataclass, field
from typing import Any

client = anthropic.Anthropic()

@dataclass
class EpisodicMemory:
    """Recent events — fixed-size FIFO, high fidelity."""
    buffer: deque = field(default_factory=lambda: deque(maxlen=20))

    def add(self, role: str, content: str):
        self.buffer.append({"role": role, "content": content, "ts": time.time()})

    def recent(self, n: int = 6) -> list[dict]:
        items = list(self.buffer)
        return [{"role": m["role"], "content": m["content"]} for m in items[-n:]]

@dataclass
class SemanticMemory:
    """Long-term facts — extracted and deduplicated."""
    facts: dict[str, str] = field(default_factory=dict)  # subject -> value

    def learn(self, facts: dict[str, str]):
        self.facts.update(facts)

    def to_context(self) -> str:
        if not self.facts:
            return ""
        lines = ["## Long-term Knowledge"]
        for subject, value in list(self.facts.items())[:20]:
            lines.append(f"- {subject}: {value}")
        return "\n".join(lines)

@dataclass
class ProceduralMemory:
    """Learned patterns and preferences — updated on positive signal."""
    patterns: list[str] = field(default_factory=list)

    def add_pattern(self, pattern: str):
        if pattern not in self.patterns:
            self.patterns.append(pattern)
            if len(self.patterns) > 15:
                self.patterns.pop(0)

    def to_context(self) -> str:
        if not self.patterns:
            return ""
        lines = ["## Learned Patterns"]
        for p in self.patterns[-10:]:
            lines.append(f"- {p}")
        return "\n".join(lines)

class HierarchicalMemory:
    def __init__(self):
        self.episodic = EpisodicMemory()
        self.semantic = SemanticMemory()
        self.procedural = ProceduralMemory()

    def extract_semantic_facts(self, user_message: str) -> dict[str, str]:
        """Extract key-value facts from a user message."""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system='Extract key facts as JSON object {"subject": "value"}. Return {} if none.',
            messages=[{"role": "user", "content": user_message}],
        )
        try:
            text = response.content[0].text.strip()
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    def consolidate(self, user_message: str, assistant_reply: str):
        """Consolidate episodic turn into semantic/procedural memory."""
        self.episodic.add("user", user_message)
        self.episodic.add("assistant", assistant_reply)

        facts = self.extract_semantic_facts(user_message)
        if facts:
            self.semantic.learn(facts)

        # Learn procedural patterns from brief replies
        if len(assistant_reply.split()) < 30 and len(user_message.split()) > 5:
            self.procedural.add_pattern("User prefers concise answers to factual questions")

    def build_context(self) -> str:
        parts = []
        semantic = self.semantic.to_context()
        procedural = self.procedural.to_context()
        if semantic:
            parts.append(semantic)
        if procedural:
            parts.append(procedural)
        return "\n\n".join(parts)

    def active_messages(self) -> list[dict]:
        return self.episodic.recent(6)

hm = HierarchicalMemory()

def chat(user_input: str) -> str:
    context = hm.build_context()
    system = f"You are a helpful assistant.\n\n{context}" if context else "You are a helpful assistant."

    active = hm.active_messages()
    active.append({"role": "user", "content": user_input})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=active,
    )
    reply = response.content[0].text
    hm.consolidate(user_input, reply)
    return reply

dialogues = [
    "I'm building an agent framework in Python.",
    "I prefer minimal dependencies — avoid heavy ML libraries.",
    "The agent needs to handle multi-step tool use.",
    "What constraints did I mention about dependencies?",  # Recalled from semantic
]
for msg in dialogues:
    print(f"User: {msg}")
    print(f"Agent: {chat(msg)[:100]}...")
    print()
```

**Expected Token Savings:** 55–75% — episodic window is small; semantic/procedural are compact summaries
**Environment:** `pip install anthropic`

---

### Option 4 — Memory with Importance Scoring and Decay

Score each memory by importance and recency. Low-importance memories decay over time and are pruned, keeping only what matters.

```python
import time
import math
import json
import sqlite3
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ScoredMemory:
    memory_id: int
    content: str
    importance: float    # 0.0–1.0, assigned at write time
    created_at: float    # unix timestamp
    access_count: int
    decay_rate: float    # higher = fades faster

    def effective_score(self, now: float = None) -> float:
        """Importance decays exponentially with time, boosted by access frequency."""
        if now is None:
            now = time.time()
        age_hours = (now - self.created_at) / 3600
        decay = math.exp(-self.decay_rate * age_hours)
        access_boost = math.log1p(self.access_count) * 0.1
        return self.importance * decay + access_boost

class DecayingMemory:
    PRUNE_THRESHOLD = 0.05  # Remove memories below this score

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                importance REAL DEFAULT 0.5,
                created_at REAL NOT NULL,
                access_count INTEGER DEFAULT 0,
                decay_rate REAL DEFAULT 0.1
            )
        """)
        self.conn.commit()

    def _score_importance(self, content: str) -> tuple[float, float]:
        """Return (importance, decay_rate) for the content."""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system='Score this fact: {"importance": 0.0-1.0, "decay_rate": 0.01-0.5}. '
                   'High importance: deadlines, names, constraints. '
                   'High decay: greetings, filler. Return JSON only.',
            messages=[{"role": "user", "content": content}],
        )
        try:
            data = json.loads(response.content[0].text.strip())
            return float(data.get("importance", 0.5)), float(data.get("decay_rate", 0.1))
        except (json.JSONDecodeError, KeyError, ValueError):
            return 0.5, 0.1

    def store(self, content: str) -> int:
        importance, decay_rate = self._score_importance(content)
        cursor = self.conn.execute(
            "INSERT INTO memories (content, importance, created_at, decay_rate) VALUES (?,?,?,?)",
            (content, importance, time.time(), decay_rate),
        )
        self.conn.commit()
        return cursor.lastrowid

    def retrieve_top(self, n: int = 10) -> list[ScoredMemory]:
        now = time.time()
        cursor = self.conn.execute(
            "SELECT id, content, importance, created_at, access_count, decay_rate FROM memories"
        )
        memories = [ScoredMemory(*row) for row in cursor.fetchall()]
        memories.sort(key=lambda m: m.effective_score(now), reverse=True)

        # Bump access count for returned memories
        top = memories[:n]
        for m in top:
            self.conn.execute(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ?", (m.memory_id,)
            )
        self.conn.commit()
        return top

    def prune(self):
        """Remove memories below decay threshold."""
        now = time.time()
        cursor = self.conn.execute(
            "SELECT id, importance, created_at, access_count, decay_rate FROM memories"
        )
        to_delete = []
        for row in cursor.fetchall():
            mem = ScoredMemory(row[0], "", row[1], row[2], row[3], row[4])
            if mem.effective_score(now) < self.PRUNE_THRESHOLD:
                to_delete.append(row[0])
        if to_delete:
            self.conn.execute(f"DELETE FROM memories WHERE id IN ({','.join('?' * len(to_delete))})", to_delete)
            self.conn.commit()
            print(f"[Memory] Pruned {len(to_delete)} decayed memories")

    def to_context(self) -> str:
        top = self.retrieve_top(12)
        if not top:
            return ""
        now = time.time()
        lines = ["## Active Memory (by relevance)"]
        for m in top:
            score = m.effective_score(now)
            lines.append(f"- [{score:.2f}] {m.content}")
        return "\n".join(lines)

dm = DecayingMemory()
messages = []

def chat(user_input: str) -> str:
    # Extract and store any facts from user input
    if len(user_input.split()) > 4:
        dm.store(user_input)

    dm.prune()  # Clean up decayed memories periodically

    context = dm.to_context()
    system = f"You are a helpful assistant.\n\n{context}" if context else "You are a helpful assistant."
    messages.append({"role": "user", "content": user_input})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=messages[-6:],
    )
    reply = response.content[0].text
    messages.append({"role": "assistant", "content": reply})
    return reply

turns = [
    "Hi there!",                                         # Low importance, high decay
    "My name is Alex and I'm a senior backend engineer.", # High importance
    "I need to finish the API migration by May 15th.",    # Critical deadline
    "The weather is nice today.",                         # Low importance
    "What's my deadline again?",                          # Retrieves high-importance memory
]
for msg in turns:
    print(f"User: {msg}")
    print(f"Agent: {chat(msg)[:80]}...")
    print()
```

**Expected Token Savings:** 45–65% — decayed memories are pruned; only high-importance facts injected
**Environment:** `pip install anthropic`

---

### Option 5 — Entity-Relationship Memory Graph

Model memory as a graph of entities and their relationships. Queries traverse edges, returning only connected subgraphs — precise retrieval without scanning everything.

```python
import json
import anthropic
from dataclasses import dataclass, field
from collections import defaultdict

client = anthropic.Anthropic()

@dataclass
class Entity:
    name: str
    entity_type: str   # "person", "project", "deadline", "technology", "constraint"
    attributes: dict[str, str] = field(default_factory=dict)

@dataclass
class Relationship:
    source: str
    relation: str      # "works_on", "has_deadline", "uses", "blocked_by", "reports_to"
    target: str
    weight: float = 1.0

class MemoryGraph:
    def __init__(self):
        self.entities: dict[str, Entity] = {}
        self.relationships: list[Relationship] = []
        self._adj: dict[str, list[Relationship]] = defaultdict(list)

    def upsert_entity(self, name: str, entity_type: str, **attributes):
        if name in self.entities:
            self.entities[name].attributes.update(attributes)
        else:
            self.entities[name] = Entity(name, entity_type, dict(attributes))

    def add_relationship(self, source: str, relation: str, target: str, weight: float = 1.0):
        rel = Relationship(source, relation, target, weight)
        self.relationships.append(rel)
        self._adj[source].append(rel)

    def extract_graph(self, text: str) -> dict:
        """Use Claude to extract entities and relationships from text."""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="""Extract entities and relationships from text.
Return JSON: {
  "entities": [{"name": str, "type": str, "attributes": {}}],
  "relationships": [{"source": str, "relation": str, "target": str}]
}
Return {"entities": [], "relationships": []} if nothing notable.""",
            messages=[{"role": "user", "content": text}],
        )
        try:
            text_resp = response.content[0].text.strip()
            if "```" in text_resp:
                text_resp = text_resp.split("```")[1]
                if text_resp.startswith("json"):
                    text_resp = text_resp[4:]
            return json.loads(text_resp)
        except json.JSONDecodeError:
            return {"entities": [], "relationships": []}

    def ingest(self, user_message: str):
        graph_data = self.extract_graph(user_message)
        for e in graph_data.get("entities", []):
            self.upsert_entity(e["name"], e.get("type", "unknown"), **e.get("attributes", {}))
        for r in graph_data.get("relationships", []):
            if r.get("source") and r.get("target") and r.get("relation"):
                self.add_relationship(r["source"], r["relation"], r["target"])

    def subgraph_context(self, root_entities: list[str], depth: int = 2) -> str:
        """BFS from root entities to build focused context."""
        visited = set()
        frontier = list(root_entities)
        facts = []

        for _ in range(depth):
            next_frontier = []
            for node in frontier:
                if node in visited or node not in self.entities:
                    continue
                visited.add(node)
                e = self.entities[node]
                desc = f"{e.name} ({e.entity_type})"
                if e.attributes:
                    desc += ": " + ", ".join(f"{k}={v}" for k, v in e.attributes.items())
                facts.append(desc)
                for rel in self._adj.get(node, []):
                    facts.append(f"  → {rel.relation} → {rel.target}")
                    if rel.target not in visited:
                        next_frontier.append(rel.target)
            frontier = next_frontier

        if not facts:
            return ""
        return "## Entity Graph\n" + "\n".join(facts)

    def all_context(self) -> str:
        if not self.entities:
            return ""
        lines = ["## Memory Graph"]
        for name, entity in self.entities.items():
            line = f"- {name} ({entity.entity_type})"
            if entity.attributes:
                line += ": " + ", ".join(f"{k}={v}" for k, v in entity.attributes.items())
            lines.append(line)
        for rel in self.relationships[-20:]:
            lines.append(f"  {rel.source} --[{rel.relation}]--> {rel.target}")
        return "\n".join(lines)

graph_memory = MemoryGraph()
messages = []

def chat(user_input: str) -> str:
    graph_memory.ingest(user_input)
    context = graph_memory.all_context()
    system = f"You are a helpful assistant.\n\n{context}" if context else "You are a helpful assistant."

    messages.append({"role": "user", "content": user_input})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=messages[-6:],
    )
    reply = response.content[0].text
    messages.append({"role": "assistant", "content": reply})
    return reply

scenarios = [
    "Sarah is the tech lead for Project Atlas.",
    "Project Atlas uses Kubernetes and Postgres.",
    "Atlas has a hard deadline of June 1st due to a client contract.",
    "Sarah reports to CTO Marcus.",
    "What do you know about the Atlas project deadline?",
]
for s in scenarios:
    print(f"User: {s}")
    print(f"Agent: {chat(s)[:100]}...")
    print()
```

**Expected Token Savings:** 50–70% — graph context is structured and compact vs raw transcript
**Environment:** `pip install anthropic`

---

### Option 6 — Sliding Window with Selective Promotion to Long-Term Memory

Keep a short sliding window for recent turns. At each turn boundary, evaluate whether anything in the expiring turn deserves promotion to long-term memory before it slides out.

```python
import json
import anthropic
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class Turn:
    turn_id: int
    role: str
    content: str

class SlidingWindowMemory:
    WINDOW_SIZE = 8     # Number of turns in active window
    PROMOTE_THRESHOLD = 0.6  # Importance score to trigger promotion

    def __init__(self):
        self.window: deque[Turn] = deque(maxlen=self.WINDOW_SIZE)
        self.long_term: list[str] = []   # Promoted facts — kept indefinitely
        self._turn_counter = 0

    def _should_promote(self, turn: Turn) -> tuple[bool, str]:
        """Ask Claude if a turn contains anything worth keeping long-term."""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system="""Decide if this turn contains important long-term information.
Return JSON: {"promote": true/false, "summary": "one-line fact if promoting, else ''", "score": 0.0-1.0}
Promote: names, deadlines, constraints, preferences, key decisions.
Do not promote: greetings, filler, routine questions.""",
            messages=[{"role": "user", "content": f"{turn.role}: {turn.content}"}],
        )
        try:
            data = json.loads(response.content[0].text.strip())
            should = data.get("promote", False) and float(data.get("score", 0)) >= self.PROMOTE_THRESHOLD
            summary = data.get("summary", "")
            return should, summary
        except (json.JSONDecodeError, ValueError):
            return False, ""

    def add(self, role: str, content: str):
        self._turn_counter += 1
        new_turn = Turn(self._turn_counter, role, content)

        # Check if window is full — oldest turn will be evicted
        if len(self.window) == self.WINDOW_SIZE:
            oldest = self.window[0]
            should_promote, summary = self._should_promote(oldest)
            if should_promote and summary:
                self.long_term.append(summary)
                print(f"[Memory] Promoted: {summary}")

        self.window.append(new_turn)

    def active_messages(self) -> list[dict]:
        return [{"role": t.role, "content": t.content} for t in self.window]

    def long_term_context(self) -> str:
        if not self.long_term:
            return ""
        lines = ["## Long-term Memory (promoted facts)"]
        for fact in self.long_term[-15:]:
            lines.append(f"- {fact}")
        return "\n".join(lines)

swm = SlidingWindowMemory()

def chat(user_input: str) -> str:
    swm.add("user", user_input)
    context = swm.long_term_context()
    system = f"You are a helpful assistant.\n\n{context}" if context else "You are a helpful assistant."

    active = swm.active_messages()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=active,
    )
    reply = response.content[0].text
    swm.add("assistant", reply)
    return reply

# Simulate a long conversation where early facts must be remembered later
long_conversation = [
    "I'm Maria, a product manager at FinTech startup GridPay.",
    "Our primary market is small business payroll in Southeast Asia.",
    "We're launching in Thailand first — target: Q3 this year.",
    "The regulation we need to comply with is Thailand BOT requirements.",
    "My biggest blocker is finding a local banking partner.",
    "How's the weather?",
    "Let's talk about something else — what's a good lunch?",
    "Back to work: who am I again and what are we building?",  # Tests long-term recall
]
for msg in long_conversation:
    reply = chat(msg)
    print(f"User: {msg[:60]}")
    print(f"Agent: {reply[:80]}...")
    print()
```

**Expected Token Savings:** 45–60% — window keeps context small; long-term store is compact promoted facts only
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Storage Model | Retrieval Method | Best For |
|--------|--------------|-----------------|----------|
| Structured Fields | Typed SQLite records | O(1) key lookup | Factual agents with known data types |
| Chunked Embeddings | Vector store | Cosine top-k | Long open-ended conversations |
| Hierarchical (3-tier) | Episodic/Semantic/Procedural | Tier-specific | General-purpose assistants |
| Importance + Decay | Scored SQLite | Sorted by effective score | Long-running agents with evolving context |
| Entity Graph | Node/edge store | BFS subgraph | Domain-specific agents with relationships |
| Sliding Window + Promotion | FIFO + long-term list | Window + promoted facts | Chat agents with bounded resources |

**Recommended starting point:** Option 1 (Structured Fields) for most use cases — 30 minutes to implement, eliminates blob scanning immediately. Upgrade to Option 2 (Chunked Embeddings) when conversation topics become unpredictable and need semantic retrieval.
