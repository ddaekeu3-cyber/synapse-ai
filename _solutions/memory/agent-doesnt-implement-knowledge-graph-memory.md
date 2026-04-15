---
layout: solution
title: "Agent Doesn't Implement Knowledge Graph Memory"
category: memory
description: "Flat key-value memory stores lose relationship context: knowing 'Alice works at Acme' and 'Acme uses Python' separately cannot answer 'What language does Alice use at work?' Knowledge graph memory stores entities and relations, enabling multi-hop reasoning across stored facts."
tags: [memory, knowledge-graph, entities, relations, sqlite, graph, multi-hop-reasoning, neo4j]
---

## Problem

Agents with flat memory (key-value or vector stores) cannot traverse relationships between stored facts. A customer support agent that knows "User prefers dark mode" and "Dark mode requires version 3.2+" cannot connect them to answer "Will this user need an upgrade?" Graph memory stores entities and typed relations, enabling the agent to follow edges to answer questions that span multiple stored facts — without re-injecting the entire knowledge base into context.

## Solutions

### Option 1: SQLite Entity-Relation Graph with Multi-Hop Query

```python
import anthropic
import sqlite3
import json
from pathlib import Path

DB = Path("/tmp/kg_memory.db")
client = anthropic.Anthropic()

def init_graph():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            type TEXT NOT NULL,
            props TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER REFERENCES entities(id),
            predicate TEXT NOT NULL,
            object_id INTEGER REFERENCES entities(id),
            UNIQUE(subject_id, predicate, object_id)
        );
        CREATE INDEX IF NOT EXISTS idx_rel_subj ON relations(subject_id);
        CREATE INDEX IF NOT EXISTS idx_rel_obj ON relations(object_id);
    """)
    con.commit()
    con.close()

def upsert_entity(name: str, entity_type: str, props: dict = None) -> int:
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR IGNORE INTO entities (name, type, props) VALUES (?, ?, ?)",
        (name, entity_type, json.dumps(props or {})),
    )
    row = con.execute("SELECT id FROM entities WHERE name=?", (name,)).fetchone()
    con.commit()
    con.close()
    return row[0]

def add_relation(subject: str, predicate: str, obj: str,
                 subject_type: str = "entity", obj_type: str = "entity"):
    sid = upsert_entity(subject, subject_type)
    oid = upsert_entity(obj, obj_type)
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR IGNORE INTO relations (subject_id, predicate, object_id) VALUES (?, ?, ?)",
        (sid, predicate, oid),
    )
    con.commit()
    con.close()

def one_hop(entity: str, predicate: str) -> list[str]:
    """Return all objects reachable from entity via predicate."""
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT e2.name FROM relations r
        JOIN entities e1 ON r.subject_id = e1.id
        JOIN entities e2 ON r.object_id = e2.id
        WHERE e1.name = ? AND r.predicate = ?
    """, (entity, predicate)).fetchall()
    con.close()
    return [r[0] for r in rows]

def two_hop(start: str, pred1: str, pred2: str) -> list[str]:
    """Follow two edges: start -pred1-> mid -pred2-> result."""
    mids = one_hop(start, pred1)
    results = []
    for mid in mids:
        results.extend(one_hop(mid, pred2))
    return results

def graph_context_for_query(query: str) -> str:
    """Extract relevant graph facts to inject as context."""
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT e1.name, r.predicate, e2.name
        FROM relations r
        JOIN entities e1 ON r.subject_id = e1.id
        JOIN entities e2 ON r.object_id = e2.id
    """).fetchall()
    con.close()
    facts = [f"{s} {p} {o}" for s, p, o in rows]
    return "Known facts:\n" + "\n".join(f"- {f}" for f in facts)

def answer_with_graph(question: str) -> str:
    context = graph_context_for_query(question)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are a reasoning agent. Use the provided facts to answer questions. Only use facts given.",
        messages=[{"role": "user", "content": f"{context}\n\nQuestion: {question}"}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    init_graph()

    # Build a knowledge graph
    add_relation("Alice", "works_at", "Acme Corp", "person", "company")
    add_relation("Alice", "uses", "Python", "person", "language")
    add_relation("Acme Corp", "requires", "Python", "company", "language")
    add_relation("Bob", "works_at", "Acme Corp", "person", "company")
    add_relation("Python", "version", "3.12", "language", "version")

    # Multi-hop: What language does Alice use at work?
    print("Alice's employer:", one_hop("Alice", "works_at"))
    print("Acme Corp requirements:", one_hop("Acme Corp", "requires"))
    print("Two-hop (Alice -> works_at -> requires):", two_hop("Alice", "works_at", "requires"))

    print("\n--- LLM answer with graph context ---")
    answer = answer_with_graph("What programming language does Alice's employer require?")
    print(answer)

# Expected Token Savings: inject only relevant graph edges (~100 tokens) vs full memory dump (~2000 tokens)
# Environment: any agent with entity-relationship knowledge; SQLite supports graphs up to millions of edges
```

### Option 2: In-Memory Graph with Bidirectional Traversal

```python
import anthropic
from collections import defaultdict
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class Entity:
    name: str
    entity_type: str
    props: dict = field(default_factory=dict)

@dataclass
class Relation:
    subject: str
    predicate: str
    obj: str

class KnowledgeGraph:
    def __init__(self):
        self._entities: dict[str, Entity] = {}
        self._forward: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self._backward: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    def add_entity(self, name: str, entity_type: str, **props):
        self._entities[name] = Entity(name, entity_type, props)

    def add_relation(self, subject: str, predicate: str, obj: str):
        if subject not in self._entities:
            self._entities[subject] = Entity(subject, "unknown")
        if obj not in self._entities:
            self._entities[obj] = Entity(obj, "unknown")
        self._forward[subject][predicate].append(obj)
        self._backward[obj][predicate].append(subject)

    def follow(self, start: str, predicate: str, reverse: bool = False) -> list[str]:
        graph = self._backward if reverse else self._forward
        return list(graph.get(start, {}).get(predicate, []))

    def neighbors(self, entity: str) -> list[tuple[str, str, str]]:
        """All (subject, predicate, object) triples involving this entity."""
        triples = []
        for pred, objs in self._forward.get(entity, {}).items():
            for obj in objs:
                triples.append((entity, pred, obj))
        for pred, subjs in self._backward.get(entity, {}).items():
            for subj in subjs:
                triples.append((subj, pred, entity))
        return triples

    def subgraph_context(self, entities: list[str], depth: int = 1) -> str:
        """Extract triples within `depth` hops of given entities."""
        visited = set(entities)
        frontier = set(entities)
        triples = []
        for _ in range(depth):
            next_frontier = set()
            for e in frontier:
                for triple in self.neighbors(e):
                    triples.append(triple)
                    next_frontier.update([triple[0], triple[2]])
            frontier = next_frontier - visited
            visited |= frontier
        facts = list(set(triples))
        return "\n".join(f"- {s} --[{p}]--> {o}" for s, p, o in facts)

kg = KnowledgeGraph()

def answer_graph_question(question: str, focus_entities: list[str]) -> str:
    context = kg.subgraph_context(focus_entities, depth=2)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="Answer using ONLY the graph facts provided. If insufficient, say so.",
        messages=[{"role": "user", "content": f"Graph facts:\n{context}\n\nQuestion: {question}"}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    # Build graph
    kg.add_entity("Alice", "person", role="engineer")
    kg.add_entity("Bob", "person", role="manager")
    kg.add_entity("ProjectX", "project", status="active")
    kg.add_entity("Python", "language")
    kg.add_entity("FastAPI", "framework")

    kg.add_relation("Alice", "works_on", "ProjectX")
    kg.add_relation("Bob", "manages", "ProjectX")
    kg.add_relation("ProjectX", "uses", "Python")
    kg.add_relation("ProjectX", "uses", "FastAPI")
    kg.add_relation("FastAPI", "built_with", "Python")

    print("Who manages Alice's project?")
    managers = kg.follow("Alice", "works_on")
    for proj in managers:
        print(f"  {proj} is managed by:", kg.follow(proj, "manages", reverse=True))

    print("\nGraph context for 'Alice':")
    print(kg.subgraph_context(["Alice"], depth=2))

    print("\n--- LLM answer ---")
    ans = answer_graph_question("What languages does Alice's project use?", ["Alice"])
    print(ans)

# Expected Token Savings: subgraph_context returns only relevant edges; depth control limits context size
# Environment: in-memory agents; bidirectional traversal answers both "who manages X" and "what does X manage"
```

### Option 3: Graph Memory with Claude-Powered Extraction

```python
import anthropic
import json
import sqlite3
from pathlib import Path

DB = Path("/tmp/kg_extract.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS triples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            source TEXT,
            UNIQUE(subject, predicate, object)
        );
    """)
    con.commit()
    con.close()

def extract_triples(text: str) -> list[dict]:
    """Use Haiku to extract (subject, predicate, object) triples from text."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=(
            "Extract factual triples from the text. "
            "Return JSON array: [{\"s\": subject, \"p\": predicate, \"o\": object}]. "
            "Use simple, consistent predicates (is_a, works_at, uses, has, owns, located_in). "
            "Return ONLY the JSON array."
        ),
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip()
    raw = raw.removeprefix("```json").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []

def store_triples(triples: list[dict], source: str = ""):
    con = sqlite3.connect(DB)
    for t in triples:
        s, p, o = t.get("s", ""), t.get("p", ""), t.get("o", "")
        if s and p and o:
            con.execute(
                "INSERT OR IGNORE INTO triples (subject, predicate, object, source) VALUES (?,?,?,?)",
                (s.strip(), p.strip(), o.strip(), source),
            )
    con.commit()
    con.close()

def learn_from_text(text: str, source: str = ""):
    triples = extract_triples(text)
    store_triples(triples, source)
    print(f"Learned {len(triples)} triples from: {source or 'input'}")
    for t in triples:
        print(f"  ({t.get('s')}) --[{t.get('p')}]--> ({t.get('o')})")
    return triples

def recall(subject: str) -> str:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT predicate, object FROM triples WHERE subject=?", (subject,)
    ).fetchall()
    con.close()
    if not rows:
        return f"No facts known about '{subject}'"
    return "\n".join(f"- {subject} {p} {o}" for p, o in rows)

def answer_with_memory(question: str) -> str:
    # Get all facts as context
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT subject, predicate, object FROM triples").fetchall()
    con.close()
    facts = "\n".join(f"- {s} {p} {o}" for s, p, o in rows)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="Answer using the provided knowledge graph facts only.",
        messages=[{"role": "user", "content": f"Facts:\n{facts}\n\nQuestion: {question}"}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    init_db()

    learn_from_text(
        "Sarah is a data scientist who works at DeepMind. She uses PyTorch and Python daily.",
        source="intro"
    )
    learn_from_text(
        "DeepMind is owned by Alphabet. Alphabet is headquartered in Mountain View.",
        source="company_info"
    )

    print("\n--- Recall: Sarah ---")
    print(recall("Sarah"))

    print("\n--- Multi-hop question ---")
    ans = answer_with_memory("What company owns Sarah's employer, and where is it headquartered?")
    print(ans)

# Expected Token Savings: Haiku extracts triples (~200 tokens) once; recall queries return 10-50 tokens
# Environment: agents that learn from user messages; auto-extraction builds graph without manual input
```

### Option 4: Typed Relation Schema with Confidence Scores

```python
import anthropic
import sqlite3
import time
from pathlib import Path

DB = Path("/tmp/kg_typed.db")
client = anthropic.Anthropic()

ALLOWED_PREDICATES = {
    "is_a", "works_at", "knows", "uses", "has_skill", "located_in",
    "manages", "reports_to", "owns", "part_of", "prefers", "version_of",
}

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS typed_triples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            subject_type TEXT DEFAULT 'unknown',
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            object_type TEXT DEFAULT 'unknown',
            confidence REAL DEFAULT 1.0,
            added_at REAL NOT NULL,
            UNIQUE(subject, predicate, object)
        );
    """)
    con.commit()
    con.close()

def add_triple(subject: str, predicate: str, obj: str,
               subject_type: str = "unknown", obj_type: str = "unknown",
               confidence: float = 1.0):
    if predicate not in ALLOWED_PREDICATES:
        print(f"  [SKIP] Unknown predicate: {predicate}")
        return False
    con = sqlite3.connect(DB)
    con.execute("""
        INSERT OR REPLACE INTO typed_triples
        (subject, subject_type, predicate, object, object_type, confidence, added_at)
        VALUES (?,?,?,?,?,?,?)
    """, (subject, subject_type, predicate, obj, obj_type, confidence, time.time()))
    con.commit()
    con.close()
    return True

def query_graph(subject: str | None = None, predicate: str | None = None,
                min_confidence: float = 0.5) -> list[tuple]:
    con = sqlite3.connect(DB)
    where_parts = ["confidence >= ?"]
    params: list = [min_confidence]
    if subject:
        where_parts.append("subject = ?")
        params.append(subject)
    if predicate:
        where_parts.append("predicate = ?")
        params.append(predicate)
    where = " AND ".join(where_parts)
    rows = con.execute(
        f"SELECT subject, predicate, object, confidence FROM typed_triples WHERE {where}",
        params,
    ).fetchall()
    con.close()
    return rows

def graph_summary(min_confidence: float = 0.7) -> str:
    rows = query_graph(min_confidence=min_confidence)
    if not rows:
        return "Knowledge graph is empty."
    return "\n".join(f"- {s} --[{p}]--> {o} (conf={c:.0%})" for s, p, o, c in rows)

def answer_question(question: str) -> str:
    context = graph_summary(min_confidence=0.6)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="Use ONLY the provided knowledge graph to answer. Only use facts with high confidence.",
        messages=[{"role": "user", "content": f"Knowledge graph:\n{context}\n\nQuestion: {question}"}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    init_db()

    add_triple("Alice", "works_at", "TechCorp", "person", "company", confidence=1.0)
    add_triple("Alice", "has_skill", "Python", "person", "skill", confidence=1.0)
    add_triple("Alice", "has_skill", "Rust", "person", "skill", confidence=0.7)
    add_triple("TechCorp", "located_in", "Berlin", "company", "city", confidence=1.0)
    add_triple("TechCorp", "uses", "Kubernetes", "company", "technology", confidence=0.9)
    add_triple("Alice", "knows", "Bob", "person", "person", confidence=0.8)

    print("High-confidence facts:")
    print(graph_summary(min_confidence=0.8))

    print("\n--- Question ---")
    ans = answer_question("Where does Alice work, and what city is that in?")
    print(ans)

# Expected Token Savings: confidence filter reduces context to only reliable facts; avoids injecting uncertain data
# Environment: agents learning from uncertain sources (user claims, web scraping); confidence prevents hallucination
```

### Option 5: Graph-Augmented RAG — Combine Vector + Graph

```python
import anthropic
import hashlib
import json
import sqlite3
from pathlib import Path

DB = Path("/tmp/kg_rag.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS kg_nodes (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            node_type TEXT,
            summary TEXT
        );
        CREATE TABLE IF NOT EXISTS kg_edges (
            from_id INTEGER REFERENCES kg_nodes(id),
            relation TEXT NOT NULL,
            to_id INTEGER REFERENCES kg_nodes(id),
            PRIMARY KEY (from_id, relation, to_id)
        );
        CREATE TABLE IF NOT EXISTS embeddings (
            node_id INTEGER PRIMARY KEY REFERENCES kg_nodes(id),
            text_hash TEXT NOT NULL
        );
    """)
    con.commit()
    con.close()

def add_node(name: str, node_type: str, summary: str = "") -> int:
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR IGNORE INTO kg_nodes (name, node_type, summary) VALUES (?,?,?)",
        (name, node_type, summary),
    )
    row = con.execute("SELECT id FROM kg_nodes WHERE name=?", (name,)).fetchone()
    con.commit()
    con.close()
    return row[0]

def add_edge(from_name: str, relation: str, to_name: str):
    con = sqlite3.connect(DB)
    from_row = con.execute("SELECT id FROM kg_nodes WHERE name=?", (from_name,)).fetchone()
    to_row = con.execute("SELECT id FROM kg_nodes WHERE name=?", (to_name,)).fetchone()
    if from_row and to_row:
        con.execute(
            "INSERT OR IGNORE INTO kg_edges VALUES (?,?,?)",
            (from_row[0], relation, to_row[0]),
        )
        con.commit()
    con.close()

def simple_keyword_search(query: str, top_k: int = 3) -> list[str]:
    """Keyword-based node retrieval (substitute for vector search in this example)."""
    query_lower = query.lower()
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT name, summary FROM kg_nodes").fetchall()
    con.close()
    scored = []
    for name, summary in rows:
        score = sum(1 for w in query_lower.split() if w in (name + " " + summary).lower())
        if score > 0:
            scored.append((score, name))
    scored.sort(reverse=True)
    return [name for _, name in scored[:top_k]]

def expand_with_graph(seed_nodes: list[str], hops: int = 1) -> str:
    """Take seed nodes from retrieval and expand with graph neighbors."""
    all_triples = []
    visited = set(seed_nodes)

    def get_neighbors(node: str) -> list[tuple]:
        con = sqlite3.connect(DB)
        rows = con.execute("""
            SELECT n1.name, e.relation, n2.name
            FROM kg_edges e
            JOIN kg_nodes n1 ON e.from_id = n1.id
            JOIN kg_nodes n2 ON e.to_id = n2.id
            WHERE n1.name = ? OR n2.name = ?
        """, (node, node)).fetchall()
        con.close()
        return rows

    current = set(seed_nodes)
    for _ in range(hops):
        next_level = set()
        for node in current:
            triples = get_neighbors(node)
            all_triples.extend(triples)
            for s, r, o in triples:
                next_level.update([s, o])
        current = next_level - visited
        visited |= current

    seen = set()
    unique = []
    for t in all_triples:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return "\n".join(f"- {s} --[{r}]--> {o}" for s, r, o in unique)

def rag_answer(question: str) -> str:
    seeds = simple_keyword_search(question, top_k=3)
    graph_context = expand_with_graph(seeds, hops=2)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="Use the provided knowledge graph facts to answer precisely.",
        messages=[{"role": "user", "content": f"Graph context:\n{graph_context}\n\nQuestion: {question}"}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    init_db()

    for name, ntype, summary in [
        ("Alice", "person", "Alice is a senior engineer"),
        ("ProjectX", "project", "ProjectX is a machine learning platform"),
        ("Python", "language", "Python programming language"),
        ("TensorFlow", "library", "TensorFlow deep learning library"),
    ]:
        add_node(name, ntype, summary)

    add_edge("Alice", "leads", "ProjectX")
    add_edge("ProjectX", "uses", "Python")
    add_edge("ProjectX", "uses", "TensorFlow")
    add_edge("TensorFlow", "written_in", "Python")

    print("Seeds for 'What does Alice lead?':", simple_keyword_search("What does Alice lead?"))
    print("\nAnswer:", rag_answer("What technologies does Alice's project use?"))

# Expected Token Savings: keyword retrieval + 2-hop expansion returns ~20 triples vs full graph dump
# Environment: large knowledge bases; graph expansion bridges retrieval gap for multi-hop questions
```

### Option 6: Temporal Knowledge Graph with Fact Expiry

```python
import anthropic
import sqlite3
import time
from pathlib import Path

DB = Path("/tmp/kg_temporal.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS temporal_triples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            valid_from REAL NOT NULL,
            valid_until REAL,  -- NULL means currently valid
            superseded INTEGER DEFAULT 0
        );
    """)
    con.commit()
    con.close()

def assert_fact(subject: str, predicate: str, obj: str, ttl_seconds: float | None = None):
    """Add a fact, superseding any existing fact with same (subject, predicate)."""
    now = time.time()
    valid_until = now + ttl_seconds if ttl_seconds else None
    con = sqlite3.connect(DB)
    # Supersede old facts
    con.execute("""
        UPDATE temporal_triples SET superseded=1, valid_until=?
        WHERE subject=? AND predicate=? AND superseded=0
    """, (now, subject, predicate))
    con.execute("""
        INSERT INTO temporal_triples (subject, predicate, object, valid_from, valid_until)
        VALUES (?,?,?,?,?)
    """, (subject, predicate, obj, now, valid_until))
    con.commit()
    con.close()

def current_facts(as_of: float | None = None) -> list[tuple]:
    """Return all non-superseded, non-expired facts."""
    now = as_of or time.time()
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT subject, predicate, object FROM temporal_triples
        WHERE superseded = 0
          AND valid_from <= ?
          AND (valid_until IS NULL OR valid_until > ?)
    """, (now, now)).fetchall()
    con.close()
    return rows

def history(subject: str, predicate: str) -> list[tuple]:
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT object, valid_from, valid_until, superseded
        FROM temporal_triples
        WHERE subject=? AND predicate=?
        ORDER BY valid_from ASC
    """, (subject, predicate)).fetchall()
    con.close()
    return rows

def answer_with_current_facts(question: str) -> str:
    facts = current_facts()
    context = "\n".join(f"- {s} {p} {o}" for s, p, o in facts)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="Answer using only currently valid facts. Do not speculate.",
        messages=[{"role": "user", "content": f"Current facts:\n{context}\n\nQuestion: {question}"}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    init_db()

    # Initial state
    assert_fact("Alice", "works_at", "StartupA")
    assert_fact("Alice", "role", "Engineer")
    assert_fact("Alice", "location", "Remote", ttl_seconds=300)
    time.sleep(0.1)

    # Alice changes jobs
    assert_fact("Alice", "works_at", "BigCorp")
    assert_fact("Alice", "role", "Senior Engineer")

    print("Current facts:", current_facts())
    print("\nHistory of Alice.works_at:")
    for obj, vf, vu, sup in history("Alice", "works_at"):
        status = "SUPERSEDED" if sup else "CURRENT"
        print(f"  [{status}] {obj} (from={vf:.0f}, until={vu})")

    print("\n--- Current answer ---")
    print(answer_with_current_facts("Where does Alice work now?"))

# Expected Token Savings: only current facts injected; TTL auto-expires stale facts without manual cleanup
# Environment: agents tracking evolving state (job changes, project status, user preferences over time)
```

## Comparison

| Option | Storage | Traversal | Auto-Extraction | Temporal | Best For |
|--------|---------|----------|----------------|---------|---------|
| 1 — SQLite + multi-hop | SQLite | 1-2 hop SQL | No | No | Structured entity graphs |
| 2 — In-memory bidirectional | RAM | N-hop BFS | No | No | Fast traversal, no persistence |
| 3 — Claude extraction | SQLite | SQL | Yes (Haiku) | No | Learning from free text |
| 4 — Typed + confidence | SQLite | SQL | No | No | Uncertain sources (web scraping) |
| 5 — Graph + RAG | SQLite | Keyword + graph expand | No | No | Large graphs with retrieval |
| 6 — Temporal | SQLite | SQL | No | Yes | Evolving facts (job changes, status) |
