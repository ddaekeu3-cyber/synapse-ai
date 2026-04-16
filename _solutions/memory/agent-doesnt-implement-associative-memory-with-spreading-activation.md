---
title: "Agent Doesn't Implement Associative Memory with Spreading Activation"
description: "Build memory graphs where retrieving one concept activates related nodes, enabling human-like associative recall across agent sessions."
category: memory
difficulty: advanced
tags: [memory, knowledge-graph, associative, spreading-activation, recall, asyncio]
---

# Agent Doesn't Implement Associative Memory with Spreading Activation

## Problem

Flat vector stores retrieve isolated chunks with no concept of relatedness. Human memory works by association: recalling "Python" might activate "asyncio", "GIL", "Guido" — each at decreasing strength. Without spreading activation, agents miss context that a human expert would naturally surface.

---

## Option 1: Simple Weighted Graph with BFS Activation

```python
import asyncio
import anthropic
from collections import defaultdict, deque
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class MemoryNode:
    concept: str
    content: str
    activation: float = 0.0

@dataclass
class MemoryGraph:
    nodes: dict[str, MemoryNode] = field(default_factory=dict)
    edges: dict[str, list[tuple[str, float]]] = field(default_factory=lambda: defaultdict(list))
    # edges[src] = [(dst, weight), ...]

    def add(self, concept: str, content: str):
        self.nodes[concept] = MemoryNode(concept=concept, content=content)

    def link(self, src: str, dst: str, weight: float = 0.5):
        if src in self.nodes and dst in self.nodes:
            self.edges[src].append((dst, weight))
            self.edges[dst].append((src, weight))  # bidirectional

    def spread(self, seed: str, decay: float = 0.5, max_depth: int = 3) -> list[tuple[str, float]]:
        """BFS spreading activation from seed concept."""
        if seed not in self.nodes:
            return []

        # Reset activations
        for node in self.nodes.values():
            node.activation = 0.0

        queue: deque[tuple[str, float, int]] = deque([(seed, 1.0, 0)])
        visited: set[str] = {seed}
        self.nodes[seed].activation = 1.0

        while queue:
            concept, activation, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for neighbor, weight in self.edges.get(concept, []):
                new_act = activation * weight * decay
                if neighbor not in visited and new_act > 0.05:
                    visited.add(neighbor)
                    self.nodes[neighbor].activation += new_act
                    queue.append((neighbor, new_act, depth + 1))

        # Sort activated nodes by activation level (excluding seed)
        return sorted(
            [(c, n.activation) for c, n in self.nodes.items() if n.activation > 0 and c != seed],
            key=lambda x: -x[1]
        )

graph = MemoryGraph()

# Populate memory
graph.add("Python", "A high-level interpreted programming language.")
graph.add("asyncio", "Python's built-in async I/O framework.")
graph.add("GIL", "Global Interpreter Lock — limits CPU parallelism in CPython.")
graph.add("threading", "OS-level thread management in Python.")
graph.add("multiprocessing", "Process-based parallelism bypassing the GIL.")
graph.add("Guido", "Creator of Python.")
graph.add("asyncio.gather", "Run multiple coroutines concurrently.")

graph.link("Python", "asyncio", 0.8)
graph.link("Python", "GIL", 0.7)
graph.link("Python", "Guido", 0.6)
graph.link("asyncio", "asyncio.gather", 0.9)
graph.link("GIL", "threading", 0.8)
graph.link("GIL", "multiprocessing", 0.7)

async def recall(user_query: str) -> str:
    # Extract seed concept from query
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system=f"Given these concepts: {list(graph.nodes.keys())}. Return the single most relevant concept name. No other text.",
        messages=[{"role": "user", "content": user_query}]
    )
    seed = r.content[0].text.strip()
    activated = graph.spread(seed)

    context_parts = []
    if seed in graph.nodes:
        context_parts.append(f"[{seed}]: {graph.nodes[seed].content}")
    for concept, strength in activated[:4]:
        context_parts.append(f"[{concept} (act={strength:.2f})]: {graph.nodes[concept].content}")

    context = "\n".join(context_parts)
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"Use this associated memory context:\n{context}",
        messages=[{"role": "user", "content": user_query}]
    )
    return resp.content[0].text

result = asyncio.run(recall("How does Python handle parallel execution?"))
print(result)
```

---

## Option 2: LLM-Extracted Association Weights

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class AssociativeStore:
    memories: dict[str, str] = field(default_factory=dict)
    associations: dict[str, dict[str, float]] = field(default_factory=dict)

    async def add_memory(self, key: str, content: str):
        self.memories[key] = content
        # Use LLM to extract association weights to existing memories
        if self.memories:
            existing = [k for k in self.memories if k != key]
            if existing:
                r = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=300,
                    system='Return JSON: {"associations": {"existing_key": 0.0-1.0, ...}} strength of association between new concept and each existing concept. Keys must be from the provided list.',
                    messages=[{"role": "user", "content": f"New concept: '{key}': {content}\n\nExisting: {json.dumps({k: self.memories[k][:100] for k in existing})}"}]
                )
                try:
                    data = json.loads(r.content[0].text)
                    self.associations[key] = data.get("associations", {})
                    for existing_key, weight in self.associations[key].items():
                        if existing_key not in self.associations:
                            self.associations[existing_key] = {}
                        self.associations[existing_key][key] = weight
                except Exception:
                    pass

    def activate(self, seed_keys: list[str], decay: float = 0.6, depth: int = 3) -> dict[str, float]:
        activations: dict[str, float] = {}
        for seed in seed_keys:
            activations[seed] = 1.0

        for _ in range(depth):
            new_activations: dict[str, float] = {}
            for source, act in activations.items():
                for target, weight in self.associations.get(source, {}).items():
                    spread = act * weight * decay
                    if spread > 0.05:
                        new_activations[target] = max(new_activations.get(target, 0), spread)
            for k, v in new_activations.items():
                if k not in activations:
                    activations[k] = v
                else:
                    activations[k] = max(activations[k], v)

        # Remove seeds from result
        return {k: v for k, v in sorted(activations.items(), key=lambda x: -x[1]) if k not in seed_keys}

store = AssociativeStore()

async def build_store():
    await asyncio.gather(
        store.add_memory("retrieval-augmented generation", "RAG: grounding LLM responses with retrieved documents."),
        store.add_memory("vector embeddings", "Dense numerical representations of semantic meaning."),
    )
    await store.add_memory("cosine similarity", "Metric for comparing vector directions, used in semantic search.")
    await store.add_memory("FAISS", "Facebook's fast approximate nearest neighbor search library.")

async def answer_with_association(question: str) -> str:
    # Find seed memories
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=f"From: {list(store.memories.keys())}. Return JSON array of 1-2 most relevant keys.",
        messages=[{"role": "user", "content": question}]
    )
    try:
        seeds = json.loads(r.content[0].text)
    except Exception:
        seeds = list(store.memories.keys())[:1]

    activated = store.activate(seeds)
    context = "\n".join([f"[{k} (strength={v:.2f})]: {store.memories[k]}" for k, v in list(activated.items())[:5] if k in store.memories])

    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"Associated memory context:\n{context}",
        messages=[{"role": "user", "content": question}]
    )
    return resp.content[0].text

asyncio.run(build_store())
result = asyncio.run(answer_with_association("How do I do semantic search efficiently?"))
print(result)
```

---

## Option 3: Temporal Decay + Recency Boosting

```python
import asyncio
import anthropic
import time
import math
from dataclasses import dataclass, field
from collections import defaultdict

client = anthropic.AsyncAnthropic()

@dataclass
class TimedNode:
    concept: str
    content: str
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    base_strength: float = 1.0

    def current_strength(self, half_life_hours: float = 24.0) -> float:
        """Ebbinghaus forgetting curve with recency boost."""
        age_hours = (time.time() - self.last_accessed) / 3600
        decay = math.exp(-0.693 * age_hours / half_life_hours)
        recency_boost = 1.0 + math.log1p(self.access_count) * 0.2
        return self.base_strength * decay * recency_boost

    def access(self):
        self.last_accessed = time.time()
        self.access_count += 1

class TemporalAssociativeMemory:
    def __init__(self, half_life_hours: float = 24.0):
        self.nodes: dict[str, TimedNode] = {}
        self.edges: dict[str, dict[str, float]] = defaultdict(dict)
        self.half_life = half_life_hours

    def store(self, concept: str, content: str, strength: float = 1.0):
        self.nodes[concept] = TimedNode(concept=concept, content=content, base_strength=strength)

    def associate(self, a: str, b: str, weight: float):
        self.edges[a][b] = weight
        self.edges[b][a] = weight

    def spread_activate(self, seed: str, top_k: int = 5) -> list[tuple[str, float, str]]:
        if seed not in self.nodes:
            return []
        self.nodes[seed].access()

        activations: dict[str, float] = {seed: self.nodes[seed].current_strength(self.half_life)}

        # Two hops
        for hop1, w1 in self.edges.get(seed, {}).items():
            if hop1 not in self.nodes:
                continue
            node1 = self.nodes[hop1]
            act1 = activations[seed] * w1 * node1.current_strength(self.half_life)
            activations[hop1] = max(activations.get(hop1, 0), act1)

            for hop2, w2 in self.edges.get(hop1, {}).items():
                if hop2 == seed or hop2 not in self.nodes:
                    continue
                node2 = self.nodes[hop2]
                act2 = act1 * w2 * 0.5 * node2.current_strength(self.half_life)
                activations[hop2] = max(activations.get(hop2, 0), act2)

        results = sorted(
            [(c, a, self.nodes[c].content) for c, a in activations.items() if c != seed and a > 0.01],
            key=lambda x: -x[1]
        )
        # Access top results
        for concept, _, _ in results[:top_k]:
            self.nodes[concept].access()

        return results[:top_k]

mem = TemporalAssociativeMemory(half_life_hours=1.0)  # short for demo

# Build memory graph
mem.store("transformers", "Attention-based neural architecture for sequence modeling.")
mem.store("BERT", "Bidirectional transformer pre-trained on masked language modeling.")
mem.store("GPT", "Autoregressive transformer for text generation.")
mem.store("attention", "Mechanism allowing tokens to attend to all other tokens.")
mem.store("fine-tuning", "Adapting pre-trained models to downstream tasks.")
mem.associate("transformers", "attention", 0.95)
mem.associate("transformers", "BERT", 0.85)
mem.associate("transformers", "GPT", 0.85)
mem.associate("BERT", "fine-tuning", 0.8)
mem.associate("GPT", "fine-tuning", 0.75)

async def query(question: str) -> str:
    activated = mem.spread_activate("transformers")
    context = "\n".join([f"[{c} strength={a:.3f}]: {content}" for c, a, content in activated])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"Memory context (temporally weighted):\n{context}",
        messages=[{"role": "user", "content": question}]
    )
    return resp.content[0].text

print(asyncio.run(query("What's the difference between BERT and GPT?")))
```

---

## Option 4: Async Concurrent Multi-Seed Activation

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ConceptGraph:
    nodes: dict[str, str] = field(default_factory=dict)        # concept -> content
    weights: dict[str, dict[str, float]] = field(default_factory=dict)  # src -> {dst: weight}

    def add(self, concept: str, content: str):
        self.nodes[concept] = content
        if concept not in self.weights:
            self.weights[concept] = {}

    def link(self, a: str, b: str, w: float):
        self.weights.setdefault(a, {})[b] = w
        self.weights.setdefault(b, {})[a] = w

    async def activate_from_seed(self, seed: str, decay: float) -> dict[str, float]:
        """Single-seed activation — runs independently per seed."""
        if seed not in self.nodes:
            return {}
        visited: dict[str, float] = {seed: 1.0}
        frontier = [(seed, 1.0)]
        for _ in range(3):  # 3 hops
            next_frontier = []
            for src, act in frontier:
                for dst, w in self.weights.get(src, {}).items():
                    new_act = act * w * decay
                    if new_act > 0.03 and (dst not in visited or visited[dst] < new_act):
                        visited[dst] = new_act
                        next_frontier.append((dst, new_act))
            frontier = next_frontier
        return {k: v for k, v in visited.items() if k != seed}

    async def multi_seed_activate(self, seeds: list[str], decay: float = 0.6) -> dict[str, float]:
        """Run activation from all seeds concurrently, merge by max."""
        results = await asyncio.gather(*[self.activate_from_seed(s, decay) for s in seeds])
        merged: dict[str, float] = {}
        for partial in results:
            for concept, strength in partial.items():
                merged[concept] = max(merged.get(concept, 0), strength)
        return dict(sorted(merged.items(), key=lambda x: -x[1]))

graph = ConceptGraph()
for concept, content in [
    ("neural networks", "Layered computation inspired by biological neurons."),
    ("backpropagation", "Algorithm to compute gradients through neural networks."),
    ("gradient descent", "Optimization by following the negative gradient."),
    ("learning rate", "Step size for gradient descent updates."),
    ("overfitting", "Model memorizes training data, generalizes poorly."),
    ("regularization", "Techniques to reduce overfitting: L1, L2, dropout."),
    ("dropout", "Randomly zero-ing activations to prevent co-adaptation."),
]:
    graph.add(concept, content)

graph.link("neural networks", "backpropagation", 0.9)
graph.link("backpropagation", "gradient descent", 0.95)
graph.link("gradient descent", "learning rate", 0.9)
graph.link("neural networks", "overfitting", 0.7)
graph.link("overfitting", "regularization", 0.95)
graph.link("regularization", "dropout", 0.8)

async def answer(question: str) -> str:
    # Identify multiple seed concepts
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        system=f"From {list(graph.nodes.keys())}, return JSON array of 1-3 relevant concept names.",
        messages=[{"role": "user", "content": question}]
    )
    try:
        seeds = json.loads(r.content[0].text)
    except Exception:
        seeds = ["neural networks"]

    activated = await graph.multi_seed_activate(seeds)
    ctx_parts = [f"[{c} act={v:.2f}]: {graph.nodes[c]}" for c, v in list(activated.items())[:6] if c in graph.nodes]
    context = "\n".join(ctx_parts)

    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"Associated memory:\n{context}",
        messages=[{"role": "user", "content": question}]
    )
    return resp.content[0].text

print(asyncio.run(answer("Why does my model overfit and how do I fix it?")))
```

---

## Option 5: Persistent SQLite-Backed Association Graph

```python
import asyncio
import anthropic
import aiosqlite
import json
import time

client = anthropic.AsyncAnthropic()
DB_PATH = "associative_memory.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                concept TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                created_at REAL,
                access_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS edges (
                src TEXT,
                dst TEXT,
                weight REAL,
                PRIMARY KEY (src, dst)
            );
        """)
        await db.commit()

async def add_node(concept: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, 0)",
            (concept, content, time.time())
        )
        await db.commit()

async def add_edge(src: str, dst: str, weight: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO edges VALUES (?, ?, ?)", (src, dst, weight))
        await db.execute("INSERT OR REPLACE INTO edges VALUES (?, ?, ?)", (dst, src, weight))
        await db.commit()

async def spread_activate(seed: str, max_results: int = 8) -> list[tuple[str, float, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        # Use recursive CTE for spreading activation
        query = """
        WITH RECURSIVE activation(concept, strength, depth) AS (
            SELECT ?, 1.0, 0
            UNION ALL
            SELECT e.dst, a.strength * e.weight * 0.6, a.depth + 1
            FROM activation a
            JOIN edges e ON e.src = a.concept
            WHERE a.depth < 3 AND a.strength * e.weight * 0.6 > 0.03
        ),
        best AS (
            SELECT concept, MAX(strength) AS strength FROM activation
            WHERE concept != ?
            GROUP BY concept
        )
        SELECT b.concept, b.strength, n.content
        FROM best b JOIN nodes n ON n.concept = b.concept
        ORDER BY b.strength DESC LIMIT ?
        """
        async with db.execute(query, (seed, seed, max_results)) as cursor:
            rows = await cursor.fetchall()
        # Update access counts
        for row in rows:
            await db.execute("UPDATE nodes SET access_count = access_count + 1 WHERE concept = ?", (row[0],))
        await db.commit()
        return [(r[0], r[1], r[2]) for r in rows]

async def query_with_memory(question: str) -> str:
    await init_db()
    # Identify seed
    all_concepts = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT concept FROM nodes") as cursor:
            all_concepts = [row[0] async for row in cursor]

    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        system=f"From {all_concepts}, return the single most relevant concept name. No other text.",
        messages=[{"role": "user", "content": question}]
    )
    seed = r.content[0].text.strip()
    activated = await spread_activate(seed)
    context = "\n".join([f"[{c} strength={s:.3f}]: {content}" for c, s, content in activated])

    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"Memory graph context:\n{context}" if context else "No relevant memories found.",
        messages=[{"role": "user", "content": question}]
    )
    return resp.content[0].text
```

---

## Option 6: Self-Organizing Memory with Automatic Association Discovery

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class SelfOrganizingMemory:
    nodes: dict[str, str] = field(default_factory=dict)
    edges: dict[str, dict[str, float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def store(self, concept: str, content: str):
        async with self._lock:
            self.nodes[concept] = content
            self.edges.setdefault(concept, {})

        if len(self.nodes) > 1:
            # Automatically discover associations with existing nodes
            asyncio.create_task(self._discover_associations(concept, content))

    async def _discover_associations(self, new_concept: str, new_content: str):
        existing = {k: v for k, v in self.nodes.items() if k != new_concept}
        if not existing:
            return

        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system='Return JSON object where keys are concept names from the provided list, values are association strengths (0.0-1.0). Only include concepts with strength > 0.2.',
            messages=[{
                "role": "user",
                "content": f"New: '{new_concept}': {new_content}\n\nExisting:\n{json.dumps({k: v[:80] for k, v in list(existing.items())[:10]})}"
            }]
        )
        try:
            associations = json.loads(r.content[0].text)
            async with self._lock:
                for target, weight in associations.items():
                    if target in self.nodes and isinstance(weight, (int, float)):
                        self.edges[new_concept][target] = float(weight)
                        self.edges.setdefault(target, {})[new_concept] = float(weight)
        except Exception:
            pass

    def activate(self, seed: str, decay: float = 0.55, hops: int = 3) -> list[tuple[str, float, str]]:
        if seed not in self.nodes:
            return []
        active = {seed: 1.0}
        frontier = {seed: 1.0}

        for _ in range(hops):
            next_frontier: dict[str, float] = {}
            for src, act in frontier.items():
                for dst, weight in self.edges.get(src, {}).items():
                    new_act = act * weight * decay
                    if new_act > 0.04 and new_act > active.get(dst, 0):
                        active[dst] = new_act
                        next_frontier[dst] = new_act
            frontier = next_frontier

        return sorted(
            [(c, s, self.nodes[c]) for c, s in active.items() if c != seed and c in self.nodes],
            key=lambda x: -x[1]
        )

    async def query(self, question: str) -> str:
        # Find seed
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            system=f"From {list(self.nodes.keys())}, one most relevant key. No other text.",
            messages=[{"role": "user", "content": question}]
        )
        seed = r.content[0].text.strip()
        activated = self.activate(seed)

        ctx = "\n".join([f"[{c} act={s:.2f}]: {content}" for c, s, content in activated[:6]])
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            system=f"Associative memory context:\n{ctx}",
            messages=[{"role": "user", "content": question}]
        )
        return resp.content[0].text

async def main():
    mem = SelfOrganizingMemory()
    # Store memories — associations discovered automatically
    await asyncio.gather(
        mem.store("reinforcement learning", "Learning by reward signals from an environment."),
        mem.store("Q-learning", "Model-free RL algorithm using action-value functions."),
        mem.store("policy gradient", "RL methods that directly optimize the policy."),
        mem.store("exploration vs exploitation", "Trade-off between trying new actions and using known good ones."),
        mem.store("reward shaping", "Designing reward functions to guide agent learning."),
    )
    await asyncio.sleep(2)  # let background association discovery complete
    answer = await mem.query("How do I train a reinforcement learning agent to explore effectively?")
    print(answer)

asyncio.run(main())
```

---

## Comparison

| Option | Association Source | Persistence | Decay Model | Best For |
|--------|------------------|-------------|-------------|----------|
| 1 – BFS Graph | Manual links | In-memory | None | Curated knowledge bases |
| 2 – LLM Weights | LLM-extracted | In-memory | None | Auto-discovery from text |
| 3 – Temporal Decay | Manual + time | In-memory | Ebbinghaus | Long-running agents |
| 4 – Multi-Seed Async | Manual links | In-memory | None | Multi-topic queries |
| 5 – SQLite CTE | Manual links | SQLite file | None | Production persistence |
| 6 – Self-Organizing | LLM auto-discovery | In-memory | None | Fully automatic knowledge graphs |

**Recommendation:** Use Option 5 for production (persistent, SQL-native spreading activation). Combine with Option 6's automatic association discovery to build the graph without manual curation. Add Option 3's temporal decay if the domain knowledge changes over time.
