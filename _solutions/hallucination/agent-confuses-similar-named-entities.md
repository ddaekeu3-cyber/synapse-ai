---
layout: solution
title: "Agent Confuses Similar-Named Entities"
category: hallucination
description: "Agent conflates people, products, companies, or concepts that share similar names, producing confident but factually wrong responses."
tags: [hallucination, entities, disambiguation, grounding, retrieval]
---

## Symptom

A user asks about "Apple" and the agent responds with fruit nutrition facts instead of the technology company. A support agent confuses "John Smith (account #1042)" with "John Smith (account #8831)". A coding assistant describes `python-requests` when asked about `requests-mock`. The agent never hedges, never asks for clarification, and the user only discovers the error after acting on the wrong information.

## Root Cause

Large language models represent entities as high-dimensional vectors. Entities with similar names, similar contexts, or similar co-occurring vocabulary cluster together in embedding space. Without explicit disambiguation context, the model resolves ambiguity by picking the statistically most common referent — which may not be the one the user intended. Ambiguity compounds over a multi-turn conversation as each wrong assumption reinforces the next.

## Fix

### Option 1 — Explicit disambiguation prompt with candidate list

```python
import anthropic

client = anthropic.Anthropic()

DISAMBIGUATION_SYSTEM = """You are a precise assistant. When a query is ambiguous — especially names of
people, companies, products, or technical terms — you MUST ask which entity the user means before answering.

For every entity in the query, check: could this name refer to more than one thing?
If yes, list the candidates and ask. Only proceed once the user has confirmed which entity they mean."""

def ask(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=DISAMBIGUATION_SYSTEM,
        messages=history,
    )
    reply = response.content[0].text
    history = history + [{"role": "assistant", "content": reply}]
    return reply, history

# Simulate ambiguous queries
cases = [
    "Tell me about Mercury.",
    "What did Michael Jordan achieve in his career?",
    "How do I use Flask?",
    "What's the stock price of Amazon?",
]

for query in cases:
    reply, _ = ask([], query)
    print(f"Query: {query}")
    print(f"Agent: {reply[:200]}\n")
```

**Expected Token Savings:** One disambiguation turn costs ~50 tokens but prevents a wrong answer that generates a 2-4 turn correction sequence.
**Environment:** All general-purpose agents; explicit disambiguation instruction is the zero-infrastructure baseline.

---

### Option 2 — Entity resolver: classify and ground before answering

```python
import json
import anthropic

client = anthropic.Anthropic()

# Known entity registry — in production this would be a database
ENTITY_REGISTRY = {
    "apple": [
        {"id": "apple-inc",    "type": "company",  "description": "Technology company, maker of iPhone, Mac, iPad. HQ: Cupertino, CA."},
        {"id": "apple-fruit",  "type": "food",     "description": "Fruit of the apple tree (Malus domestica). Varieties: Gala, Fuji, Granny Smith."},
    ],
    "java": [
        {"id": "java-lang",    "type": "language", "description": "Object-oriented programming language by Oracle/Sun."},
        {"id": "java-island",  "type": "place",    "description": "Indonesian island; most populous island in the world."},
        {"id": "java-coffee",  "type": "food",     "description": "Slang for coffee, originating from Java island coffee trade."},
    ],
    "mercury": [
        {"id": "mercury-planet", "type": "planet",  "description": "Smallest planet in the solar system, closest to the Sun."},
        {"id": "mercury-element","type": "element", "description": "Chemical element Hg, atomic number 80. Liquid metal at room temperature."},
        {"id": "mercury-god",    "type": "mythology","description": "Roman god of commerce, communication, and travel."},
    ],
}

RESOLVER_SYSTEM = """Given a user query and a list of candidate entities, determine which entity the user most likely means.
Consider the full query context. Return JSON: {"entity_id": str, "confidence": 0.0-1.0, "reasoning": str}
If confidence < 0.8, set entity_id to "ambiguous"."""

def resolve_entity(query: str, candidates: list[dict]) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=RESOLVER_SYSTEM,
        messages=[{"role": "user", "content": f"Query: {query}\nCandidates: {json.dumps(candidates)}"}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"entity_id": "ambiguous", "confidence": 0.0, "reasoning": "parse error"}

def grounded_answer(query: str) -> str:
    # Find candidates for any ambiguous term in the query
    query_lower = query.lower()
    matched_candidates = []
    for term, candidates in ENTITY_REGISTRY.items():
        if term in query_lower:
            matched_candidates = candidates
            break

    if not matched_candidates:
        # No known ambiguous entity — answer directly
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": query}],
        )
        return response.content[0].text

    resolution = resolve_entity(query, matched_candidates)
    if resolution["entity_id"] == "ambiguous" or resolution["confidence"] < 0.8:
        options = "\n".join(f"  • {c['description']}" for c in matched_candidates)
        return f"That name could refer to several things:\n{options}\n\nWhich did you mean?"

    # Ground the answer with the resolved entity description
    entity = next((c for c in matched_candidates if c["id"] == resolution["entity_id"]), None)
    if not entity:
        return "I couldn't identify which entity you meant. Could you clarify?"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Context: {entity['description']}\n\nQuestion: {query}\n\nAnswer based on the context.",
        }],
    )
    return response.content[0].text

queries = [
    "Tell me about Apple's latest products.",
    "I want to learn Java for backend development.",
    "Mercury is closest to the Sun right?",
    "What is the boiling point of Mercury?",
]
for q in queries:
    print(f"Q: {q}")
    print(f"A: {grounded_answer(q)[:200]}\n")
```

**Expected Token Savings:** Entity registry lookup is O(1); resolver call is ~80 tokens; grounded context prevents multi-turn correction for wrong entity answers.
**Environment:** Domain-specific agents (product support, knowledge bases) where a closed set of entities is known in advance.

---

### Option 3 — Structured user context to anchor entity resolution

```python
import anthropic

client = anthropic.Anthropic()

def build_system_with_context(user_context: dict) -> str:
    """Inject user-specific entity context so ambiguous names resolve correctly."""
    lines = ["You are a helpful assistant."]
    if user_context.get("account_name"):
        lines.append(f"You are speaking with: {user_context['account_name']} (Account #{user_context.get('account_id', 'unknown')}).")
    if user_context.get("products"):
        lines.append(f"Their registered products: {', '.join(user_context['products'])}.")
    if user_context.get("active_project"):
        lines.append(f"Active project: {user_context['active_project']}.")
    lines.append("When entity names are ambiguous, resolve to the user's registered context before considering other interpretations.")
    return "\n".join(lines)

def ask(user_context: dict, question: str) -> str:
    system = build_system_with_context(user_context)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

# Two users asking about the same ambiguous name
user_a = {
    "account_name": "Sarah Chen",
    "account_id": "1042",
    "products": ["Falcon Pro", "Falcon Lite"],
    "active_project": "Falcon Pro upgrade to v3.2",
}

user_b = {
    "account_name": "Mark Torres",
    "account_id": "8831",
    "products": ["Eagle Enterprise"],
    "active_project": "Eagle Enterprise licence renewal",
}

question = "When does the renewal expire?"
print(f"User A ({user_a['account_name']}): {ask(user_a, question)[:150]}")
print(f"User B ({user_b['account_name']}): {ask(user_b, question)[:150]}")

# Same product name, different users
question2 = "How do I upgrade?"
print(f"\nUser A upgrade: {ask(user_a, question2)[:150]}")
print(f"User B upgrade: {ask(user_b, question2)[:150]}")
```

**Expected Token Savings:** Pre-loaded user context eliminates clarification turns; each session's system prompt anchors all entity resolution for the duration of the conversation.
**Environment:** Customer support and account management agents where every session has a known user identity.

---

### Option 4 — Retrieval-augmented entity grounding

```python
import anthropic

client = anthropic.Anthropic()

# Simulated vector search — in production: pgvector, Pinecone, Weaviate, etc.
KNOWLEDGE_CHUNKS = [
    {"id": "k1", "text": "Spark (Apache Spark) is a distributed data processing engine for big data analytics."},
    {"id": "k2", "text": "Spark (fitness app) tracks workouts, calories, and personal bests for gym users."},
    {"id": "k3", "text": "Kafka (Apache Kafka) is a distributed event-streaming platform for high-throughput messaging."},
    {"id": "k4", "text": "Kafka (Franz Kafka) was a 20th-century German-language novelist known for 'The Metamorphosis'."},
    {"id": "k5", "text": "Python (programming language) is a high-level interpreted language popular in data science and web development."},
    {"id": "k6", "text": "Python (snake) is a family of non-venomous constrictor snakes native to Asia, Africa, and Australia."},
]

def keyword_search(query: str, top_k: int = 3) -> list[dict]:
    """Simplified keyword retrieval — replace with embedding search in production."""
    query_lower = query.lower()
    scored = []
    for chunk in KNOWLEDGE_CHUNKS:
        score = sum(1 for word in query_lower.split() if word in chunk["text"].lower())
        scored.append((score, chunk))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k] if _ > 0]

def rag_answer(query: str) -> str:
    # Retrieve relevant chunks — may return multiple entities for ambiguous names
    chunks = keyword_search(query)

    if not chunks:
        context = "No specific context found."
    else:
        context = "\n".join(f"[{c['id']}] {c['text']}" for c in chunks)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Use ONLY the following context to answer. "
                f"If the context contains multiple entities with the same name, ask the user which one they mean.\n\n"
                f"Context:\n{context}\n\nQuestion: {query}"
            ),
        }],
    )
    return response.content[0].text

queries = [
    "How does Spark handle large datasets?",
    "What is Kafka used for?",
    "Tell me about Python.",
]
for q in queries:
    print(f"Q: {q}")
    print(f"A: {rag_answer(q)[:250]}\n")
```

**Expected Token Savings:** Retrieval limits context to relevant chunks (~200-500 tokens) rather than relying on parametric knowledge; grounded answers eliminate correction turns.
**Environment:** Knowledge-base agents where entity definitions are stored in a retrieval system.

---

### Option 5 — Confidence check: refuse to answer low-confidence entity claims

```python
import json
import anthropic

client = anthropic.Anthropic()

CONFIDENCE_SYSTEM = """Before answering, assess your confidence that you have correctly identified the specific entity the user is asking about.

Steps:
1. Identify all entities in the question.
2. For each entity, consider: are there multiple well-known things with this name?
3. Score your entity-resolution confidence (0.0 - 1.0).
4. If confidence < 0.75 for any key entity, ask for clarification instead of answering.

Return JSON first, then the answer or clarification question:
{"entities": [{"name": str, "resolved_as": str, "confidence": float}], "overall_confidence": float, "action": "answer" | "clarify"}

Then on a new line after the JSON: your response."""

def careful_answer(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=384,
        system=CONFIDENCE_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    text = response.content[0].text.strip()

    # Split JSON header from response body
    try:
        json_end = text.index("\n", text.index("}"))
        meta_raw = text[:json_end].strip().lstrip("```json").rstrip("```").strip()
        body     = text[json_end:].strip()
        meta     = json.loads(meta_raw)
    except (ValueError, json.JSONDecodeError):
        return text   # fallback: return full text

    entities_info = ", ".join(
        f"{e['name']} → {e['resolved_as']} ({e['confidence']:.0%})"
        for e in meta.get("entities", [])
    )
    print(f"  [meta] {entities_info} | action={meta.get('action')} | overall={meta.get('overall_confidence', 0):.0%}")
    return body

questions = [
    "What programming language was created by Guido van Rossum?",   # unambiguous
    "Tell me about the Titan.",                                       # ambiguous
    "How does Delta work?",                                           # very ambiguous
    "What are the specs of the M2?",                                  # ambiguous
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {careful_answer(q)[:200]}\n")
```

**Expected Token Savings:** Confidence check adds ~60 tokens per call but prevents wrong-entity answers that cost 3-5 correction turns at 200-400 tokens each.
**Environment:** High-stakes agents (medical, legal, financial) where a wrong entity identification causes significant harm.

---

### Option 6 — Conversation-scoped entity memory

```python
import anthropic

client = anthropic.Anthropic()

class EntityMemorySession:
    """
    Tracks entities confirmed during a conversation.
    Subsequent messages automatically disambiguate using established context.
    """

    def __init__(self):
        self.resolved_entities: dict[str, str] = {}   # name → resolved description
        self.history: list[dict] = []

    def build_system(self) -> str:
        base = "You are a precise assistant that carefully tracks which specific entities have been established in the conversation."
        if self.resolved_entities:
            entity_lines = "\n".join(
                f"  • '{name}' refers to: {desc}"
                for name, desc in self.resolved_entities.items()
            )
            base += f"\n\nEstablished entities in this session:\n{entity_lines}\n\nAlways use these established definitions. Do not re-interpret them."
        else:
            base += "\n\nNo entities have been confirmed yet. If a name is ambiguous, ask for clarification."
        return base

    def register_entity(self, name: str, description: str) -> None:
        """Call this when the user confirms which entity they mean."""
        self.resolved_entities[name.lower()] = description
        print(f"  [memory] registered: '{name}' = {description[:60]}")

    def chat(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=self.build_system(),
            messages=self.history,
        )
        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply

# Simulate a session where the user establishes entities
session = EntityMemorySession()

# Turn 1: Ambiguous — agent should ask
r1 = session.chat("Tell me about Falcon.")
print(f"User: Tell me about Falcon.\nAgent: {r1[:200]}\n")

# User clarifies — register the entity
session.register_entity("Falcon", "Falcon 9: SpaceX orbital rocket, first stage is reusable, ~70m tall.")

# Turn 2: Now unambiguous for rest of session
r2 = session.chat("How many engines does Falcon have?")
print(f"User: How many engines does Falcon have?\nAgent: {r2[:200]}\n")

r3 = session.chat("When was the first Falcon launch?")
print(f"User: When was the first Falcon launch?\nAgent: {r3[:200]}\n")
```

**Expected Token Savings:** Entity memory eliminates repeated disambiguation for the same term across multiple turns; each registered entity saves one clarification round per subsequent mention.
**Environment:** Multi-turn agents in specialised domains (aerospace, medical, legal) where the same ambiguous term recurs throughout a session.

---

## Comparison

| Option | Disambiguation Trigger | Memory Across Turns | Infrastructure Required | Best For |
|---|---|---|---|---|
| 1. Disambiguation prompt | Prompt instruction | No | None | Baseline for all agents |
| 2. Entity registry + resolver | Known entity set | No | Entity database | Domain-specific agents with closed entity sets |
| 3. User context injection | Session setup | Implicit via system | User profile store | Customer support with account data |
| 4. RAG grounding | Retrieval results | No | Vector store | Knowledge-base agents |
| 5. Confidence check | Per-response self-scoring | No | None | High-stakes decisions |
| 6. Session entity memory | User confirmation | Yes | None (in-memory) | Long sessions with recurring ambiguous terms |
