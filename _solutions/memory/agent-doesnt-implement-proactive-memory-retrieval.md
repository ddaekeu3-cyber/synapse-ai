---
layout: solution
title: "Agent Doesn't Implement Proactive Memory Retrieval"
category: memory
description: "Agent waits for explicit user references before retrieving past context, missing relevant memories that would improve response quality and personalization."
tags: [memory, retrieval, personalization, context, proactive]
---

# Agent Doesn't Implement Proactive Memory Retrieval

## Problem

Most agents only retrieve memories when a user explicitly references them ("remember when I told you..."). Without proactive retrieval—automatically surfacing relevant past context before generating a response—the agent provides generic answers when it could give highly personalized ones. Users repeat themselves across sessions, and the agent's knowledge degrades to the lowest common denominator rather than building understanding over time.

## Solution Options

### Option 1: Keyword-Triggered Proactive Retrieval

```python
import anthropic
import json
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class Memory:
    content: str
    keywords: list[str]
    importance: float  # 0-1

# Simulated memory store
MEMORY_STORE: list[Memory] = [
    Memory("User prefers Python over JavaScript", ["python", "javascript", "language", "prefer", "code"], 0.9),
    Memory("User is building a FastAPI backend for a fintech startup", ["fastapi", "backend", "api", "fintech", "startup"], 0.95),
    Memory("User dislikes verbose error messages, wants concise output", ["error", "verbose", "concise", "output", "message"], 0.8),
    Memory("User's team uses PostgreSQL as the primary database", ["postgresql", "postgres", "database", "db", "sql"], 0.85),
    Memory("User has 5 years of software engineering experience", ["experience", "years", "senior", "engineer", "background"], 0.7),
]

def extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from user message."""
    # Remove common stop words
    stop_words = {"the", "a", "an", "is", "in", "it", "of", "and", "or", "to", "for", "with", "how", "what", "can", "i", "my", "do"}
    words = re.findall(r'\b\w+\b', text.lower())
    return [w for w in words if w not in stop_words and len(w) > 2]

def retrieve_relevant_memories(user_message: str, top_k: int = 3) -> list[Memory]:
    """Proactively retrieve memories relevant to the user's message."""
    query_keywords = set(extract_keywords(user_message))
    scored = []

    for memory in MEMORY_STORE:
        memory_keywords = set(memory.keywords)
        overlap = len(query_keywords & memory_keywords)
        if overlap > 0:
            score = (overlap / len(query_keywords | memory_keywords)) * memory.importance
            scored.append((score, memory))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:top_k]]

def build_system_prompt_with_memories(base_prompt: str, memories: list[Memory]) -> str:
    if not memories:
        return base_prompt
    memory_section = "\n\n## Retrieved User Context\n"
    for m in memories:
        memory_section += f"- {m.content}\n"
    return base_prompt + memory_section

def chat_with_proactive_retrieval(user_message: str) -> str:
    memories = retrieve_relevant_memories(user_message)
    if memories:
        print(f"[MEMORY] Proactively retrieved {len(memories)} relevant memories:")
        for m in memories:
            print(f"  - {m.content}")

    system_prompt = build_system_prompt_with_memories(
        "You are a helpful programming assistant.",
        memories,
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

result = chat_with_proactive_retrieval("How should I structure my API error responses?")
print(f"\nAssistant: {result}")

# Expected Token Savings: Saves 1-3 turns of back-and-forth clarification per session
# Environment: Agents with persistent user profiles and structured keyword-indexed memory stores
```

### Option 2: LLM-Guided Memory Relevance Scoring

```python
import anthropic
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class MemoryEntry:
    id: str
    content: str
    created_at: str

ALL_MEMORIES: list[MemoryEntry] = [
    MemoryEntry("m1", "User is building a real-time chat application using WebSockets", "2026-01-10"),
    MemoryEntry("m2", "User mentioned their app needs to handle 10,000 concurrent users", "2026-01-12"),
    MemoryEntry("m3", "User prefers Redis for session storage and pub/sub messaging", "2026-01-14"),
    MemoryEntry("m4", "User's team deploys on AWS using ECS Fargate", "2026-01-15"),
    MemoryEntry("m5", "User is currently debugging a memory leak in their Node.js process", "2026-02-01"),
    MemoryEntry("m6", "User prefers TypeScript over plain JavaScript", "2026-01-08"),
]

def llm_score_memories(user_message: str, memories: list[MemoryEntry]) -> list[tuple[float, MemoryEntry]]:
    """Use Claude to score memory relevance."""
    memory_list = "\n".join([f"{m.id}: {m.content}" for m in memories])

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""Score each memory's relevance to the user's current message on a scale of 0.0 to 1.0.
Return only a JSON object mapping memory ID to score.

User's current message: "{user_message}"

Memories:
{memory_list}

Return format: {{"m1": 0.8, "m2": 0.3, ...}}"""
        }],
    )

    try:
        text = response.content[0].text
        # Extract JSON from response
        json_match = re.search(r'\{[^}]+\}', text)
        if json_match:
            scores = json.loads(json_match.group())
            return sorted(
                [(float(scores.get(m.id, 0)), m) for m in memories],
                key=lambda x: x[0],
                reverse=True,
            )
    except Exception:
        pass
    return [(0.0, m) for m in memories]

import re

def respond_with_llm_scored_memories(user_message: str, relevance_threshold: float = 0.5) -> str:
    scored = llm_score_memories(user_message, ALL_MEMORIES)
    relevant = [(score, m) for score, m in scored if score >= relevance_threshold]

    print(f"[MEMORY] LLM scored {len(ALL_MEMORIES)} memories; {len(relevant)} above threshold {relevance_threshold}:")
    for score, m in relevant:
        print(f"  [{score:.2f}] {m.content}")

    memory_context = ""
    if relevant:
        memory_context = "\n\nRelevant context about this user:\n" + "\n".join(
            f"- {m.content}" for _, m in relevant
        )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a helpful technical assistant." + memory_context,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

result = respond_with_llm_scored_memories(
    "What's the best way to scale my WebSocket connections?",
    relevance_threshold=0.6,
)
print(f"\nAssistant: {result}")

# Expected Token Savings: ~50% reduction in context setup tokens vs. injecting all memories
# Environment: Agents with 10-100 stored memories; LLM scoring adds ~100 tokens overhead
```

### Option 3: Embedding-Based Semantic Retrieval

```python
import anthropic
import json
import math
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class SemanticMemory:
    content: str
    embedding: list[float] = field(default_factory=list)

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x ** 2 for x in a))
    mag_b = math.sqrt(sum(x ** 2 for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)

def get_embedding(text: str) -> list[float]:
    """
    In production, use a real embedding model.
    Here we simulate with a simple character frequency vector.
    Replace with: openai.embeddings.create() or sentence-transformers.
    """
    vec = [0.0] * 52
    for ch in text.lower():
        if 'a' <= ch <= 'z':
            vec[ord(ch) - ord('a')] += 1.0
        elif '0' <= ch <= '9':
            vec[26 + int(ch)] += 1.0
    magnitude = math.sqrt(sum(x**2 for x in vec)) or 1.0
    return [x / magnitude for x in vec]

# Build memory store with embeddings
SEMANTIC_MEMORIES: list[SemanticMemory] = []
raw_memories = [
    "User is working on a machine learning pipeline for image classification",
    "User prefers PyTorch over TensorFlow for deep learning projects",
    "User's training dataset contains 50,000 labeled images of medical scans",
    "User uses Weights & Biases for experiment tracking",
    "User's model currently achieves 87% accuracy on the validation set",
    "User is considering deploying the model as a REST API using FastAPI",
]

for content in raw_memories:
    mem = SemanticMemory(content=content, embedding=get_embedding(content))
    SEMANTIC_MEMORIES.append(mem)

def retrieve_by_embedding(query: str, top_k: int = 3, min_similarity: float = 0.7) -> list[tuple[float, SemanticMemory]]:
    query_embedding = get_embedding(query)
    scored = [
        (cosine_similarity(query_embedding, m.embedding), m)
        for m in SEMANTIC_MEMORIES
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(s, m) for s, m in scored[:top_k] if s >= min_similarity]

def semantic_memory_agent(user_message: str) -> str:
    relevant = retrieve_by_embedding(user_message, top_k=3, min_similarity=0.6)

    print(f"[SEMANTIC RETRIEVAL] Found {len(relevant)} relevant memories:")
    for sim, mem in relevant:
        print(f"  [sim={sim:.3f}] {mem.content}")

    system = "You are a helpful ML engineering assistant."
    if relevant:
        context = "\n\nUser's background context:\n" + "\n".join(
            f"- {m.content}" for _, m in relevant
        )
        system += context

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

result = semantic_memory_agent("How can I improve my model's accuracy?")
print(f"\nAssistant: {result}")

# Expected Token Savings: Sub-linear context growth; inject only the 2-3 most relevant memories
# Environment: Agents with vector stores (Pinecone, pgvector, Chroma) for production-scale memory
```

### Option 4: Multi-Signal Proactive Retrieval Pipeline

```python
import anthropic
import json
import re
import math
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class RetrievalSignal(Enum):
    KEYWORD = "keyword"
    RECENCY = "recency"
    IMPORTANCE = "importance"
    TOPIC_CONTINUITY = "topic_continuity"

@dataclass
class RichMemory:
    id: str
    content: str
    topics: list[str]
    importance: float       # 0-1, set at storage time
    days_ago: int           # age in days
    access_count: int = 0   # how often retrieved

def keyword_score(memory: RichMemory, query_words: set[str]) -> float:
    memory_words = set(re.findall(r'\b\w+\b', memory.content.lower()))
    overlap = query_words & memory_words
    return len(overlap) / max(len(query_words), 1)

def recency_score(memory: RichMemory) -> float:
    # Exponential decay: full score at 0 days, ~50% at 7 days, ~25% at 14 days
    return math.exp(-0.1 * memory.days_ago)

def importance_score(memory: RichMemory) -> float:
    return memory.importance

def topic_continuity_score(memory: RichMemory, recent_topics: list[str]) -> float:
    if not recent_topics:
        return 0.0
    matching = sum(1 for t in memory.topics if t in recent_topics)
    return matching / max(len(memory.topics), 1)

MEMORIES = [
    RichMemory("m1", "User is launching a SaaS product in Q2 2026", ["business", "saas", "launch"], 0.95, 3),
    RichMemory("m2", "User's tech stack: Next.js frontend, FastAPI backend, Supabase DB", ["tech", "stack", "nextjs", "fastapi"], 0.9, 5),
    RichMemory("m3", "User is worried about GDPR compliance for EU customers", ["compliance", "gdpr", "privacy", "eu"], 0.85, 10),
    RichMemory("m4", "User's co-founder handles design and marketing", ["team", "cofounder", "design"], 0.7, 15),
    RichMemory("m5", "User's pricing model: freemium with $29/mo pro plan", ["pricing", "freemium", "revenue"], 0.88, 2),
    RichMemory("m6", "User is stressed about runway — 8 months remaining", ["runway", "funding", "startup"], 0.92, 1),
]

def multi_signal_retrieve(
    user_message: str,
    recent_topics: list[str],
    top_k: int = 3,
    weights: dict[RetrievalSignal, float] | None = None,
) -> list[tuple[float, RichMemory]]:
    if weights is None:
        weights = {
            RetrievalSignal.KEYWORD: 0.35,
            RetrievalSignal.RECENCY: 0.25,
            RetrievalSignal.IMPORTANCE: 0.25,
            RetrievalSignal.TOPIC_CONTINUITY: 0.15,
        }

    query_words = set(re.findall(r'\b\w+\b', user_message.lower()))
    scored = []

    for memory in MEMORIES:
        score = (
            weights[RetrievalSignal.KEYWORD] * keyword_score(memory, query_words)
            + weights[RetrievalSignal.RECENCY] * recency_score(memory)
            + weights[RetrievalSignal.IMPORTANCE] * importance_score(memory)
            + weights[RetrievalSignal.TOPIC_CONTINUITY] * topic_continuity_score(memory, recent_topics)
        )
        scored.append((score, memory))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]

def multi_signal_agent(user_message: str, recent_topics: list[str] | None = None) -> str:
    if recent_topics is None:
        recent_topics = []

    retrieved = multi_signal_retrieve(user_message, recent_topics, top_k=3)

    print(f"[MULTI-SIGNAL RETRIEVAL] Retrieved memories:")
    for score, mem in retrieved:
        print(f"  [score={score:.3f}] {mem.content}")

    system = "You are a helpful startup advisor."
    if retrieved:
        context = "\n\nKey context about this user and their situation:\n" + "\n".join(
            f"- {m.content}" for _, m in retrieved
        )
        system += context

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

result = multi_signal_agent(
    "Should I spend time on GDPR compliance now or focus on getting more users first?",
    recent_topics=["compliance", "business", "saas"],
)
print(f"\nAssistant: {result}")

# Expected Token Savings: Balanced retrieval improves relevance quality; ~30% fewer clarification turns
# Environment: Startup/business advisors with rich user profile stores requiring multi-factor ranking
```

### Option 5: Anticipatory Retrieval Based on Conversation Flow

```python
import anthropic
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class FlowMemory:
    content: str
    trigger_patterns: list[str]  # Conversation patterns that predict this memory is needed
    next_likely_needs: list[str]  # IDs of memories likely needed after this one

FLOW_MEMORIES: dict[str, FlowMemory] = {
    "auth_setup": FlowMemory(
        "User configured OAuth2 with Google and GitHub providers",
        ["auth", "login", "oauth", "signin"],
        ["session_mgmt", "user_roles"],
    ),
    "session_mgmt": FlowMemory(
        "User stores sessions in Redis with 24h TTL",
        ["session", "redis", "persist"],
        ["user_roles"],
    ),
    "user_roles": FlowMemory(
        "User's role system: admin, editor, viewer — stored in PostgreSQL",
        ["role", "permission", "admin", "access"],
        ["api_design"],
    ),
    "api_design": FlowMemory(
        "User's API follows REST conventions with versioning at /api/v1/",
        ["api", "endpoint", "rest", "route"],
        ["deployment"],
    ),
    "deployment": FlowMemory(
        "User deploys via GitHub Actions to Render.com on every push to main",
        ["deploy", "ci", "github", "render", "production"],
        [],
    ),
    "performance": FlowMemory(
        "User's app response time target is p99 < 200ms",
        ["performance", "latency", "speed", "slow"],
        ["deployment"],
    ),
}

def anticipatory_retrieve(
    current_message: str,
    conversation_history: list[str],
    previously_shown: set[str],
) -> list[FlowMemory]:
    """Retrieve both directly relevant AND anticipated-next memories."""
    import re
    all_text = " ".join(conversation_history + [current_message]).lower()
    words = set(re.findall(r'\b\w+\b', all_text))

    direct_matches: list[tuple[int, str, FlowMemory]] = []
    for mem_id, memory in FLOW_MEMORIES.items():
        if mem_id in previously_shown:
            continue
        hits = sum(1 for p in memory.trigger_patterns if p in words)
        if hits > 0:
            direct_matches.append((hits, mem_id, memory))

    direct_matches.sort(key=lambda x: x[0], reverse=True)
    selected = direct_matches[:2]

    # Also include anticipated next memories
    anticipated_ids = set()
    for _, mem_id, _ in selected:
        for next_id in FLOW_MEMORIES[mem_id].next_likely_needs:
            if next_id not in previously_shown:
                anticipated_ids.add(next_id)

    result = [m for _, _, m in selected]
    for ant_id in list(anticipated_ids)[:1]:  # Add 1 anticipated memory
        result.append(FLOW_MEMORIES[ant_id])
        print(f"[ANTICIPATORY] Pre-loading likely-needed memory: {FLOW_MEMORIES[ant_id].content[:50]}...")

    return result

def run_anticipatory_agent():
    conversation_history: list[str] = []
    previously_shown: set[str] = set()

    print("Chat with the agent (type 'quit' to exit):\n")
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() == "quit":
            break

        retrieved = anticipatory_retrieve(user_input, conversation_history, previously_shown)

        for mem in retrieved:
            previously_shown.add(next(k for k, v in FLOW_MEMORIES.items() if v == mem))

        system = "You are a helpful web development assistant."
        if retrieved:
            context = "\n\nUser's system context:\n" + "\n".join(f"- {m.content}" for m in retrieved)
            system += context
            print(f"[RETRIEVAL] Injecting {len(retrieved)} memories into context")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user_input}],
        )

        answer = response.content[0].text
        print(f"Assistant: {answer}\n")
        conversation_history.append(user_input)

# run_anticipatory_agent()  # Interactive — uncomment to run
print("Anticipatory retrieval agent ready. Call run_anticipatory_agent() to start.")

# Expected Token Savings: Pre-loads context before user asks follow-up, saving 1 full turn
# Environment: Complex multi-topic conversations where memory needs flow in predictable sequences
```

### Option 6: Async Parallel Memory Retrieval with Confidence Gating

```python
import anthropic
import asyncio
import json
import re
import math
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class AsyncMemory:
    id: str
    content: str
    category: str  # e.g., "preference", "project", "constraint", "goal"

ALL_ASYNC_MEMORIES = [
    AsyncMemory("p1", "User prefers detailed explanations over short summaries", "preference"),
    AsyncMemory("p2", "User dislikes solutions that require Docker", "preference"),
    AsyncMemory("pj1", "User is building a CLI tool for managing Kubernetes namespaces", "project"),
    AsyncMemory("pj2", "User's CLI is written in Go using Cobra framework", "project"),
    AsyncMemory("c1", "User's company requires all tools to be approved by the security team", "constraint"),
    AsyncMemory("c2", "User cannot use any cloud services due to air-gapped environment", "constraint"),
    AsyncMemory("g1", "User wants to open-source the CLI once stable", "goal"),
]

async def retrieve_category(category: str, query: str, memories: list[AsyncMemory]) -> list[tuple[float, AsyncMemory]]:
    """Retrieve memories from a specific category asynchronously."""
    await asyncio.sleep(0)  # Yield to event loop
    query_words = set(re.findall(r'\b\w+\b', query.lower()))
    cat_memories = [m for m in memories if m.category == category]
    scored = []
    for m in cat_memories:
        mem_words = set(re.findall(r'\b\w+\b', m.content.lower()))
        overlap = len(query_words & mem_words)
        score = overlap / max(len(query_words), 1)
        # Boost preferences and constraints always — they're always relevant
        if m.category in ("preference", "constraint"):
            score = max(score, 0.4)
        scored.append((score, m))
    return scored

async def parallel_memory_retrieve(user_message: str, min_confidence: float = 0.3) -> list[AsyncMemory]:
    """Retrieve from all memory categories in parallel."""
    categories = list(set(m.category for m in ALL_ASYNC_MEMORIES))

    tasks = [retrieve_category(cat, user_message, ALL_ASYNC_MEMORIES) for cat in categories]
    results = await asyncio.gather(*tasks)

    # Merge and filter
    all_scored: list[tuple[float, AsyncMemory]] = []
    for scored_list in results:
        all_scored.extend(scored_list)

    all_scored.sort(key=lambda x: x[0], reverse=True)
    return [m for score, m in all_scored if score >= min_confidence]

async def async_memory_agent(user_message: str) -> str:
    # Run memory retrieval in parallel with any other prep work
    retrieved_task = asyncio.create_task(parallel_memory_retrieve(user_message))

    # Could do other async prep here in parallel
    retrieved = await retrieved_task

    print(f"[ASYNC RETRIEVAL] Retrieved {len(retrieved)} memories:")
    for m in retrieved:
        print(f"  [{m.category}] {m.content}")

    system = "You are a helpful software engineering assistant."
    if retrieved:
        grouped: dict[str, list[str]] = {}
        for m in retrieved:
            grouped.setdefault(m.category, []).append(m.content)

        context_parts = []
        for cat, items in grouped.items():
            context_parts.append(f"\n{cat.title()}s:")
            context_parts.extend(f"  - {item}" for item in items)

        system += "\n\nUser Context:" + "\n".join(context_parts)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

async def main():
    result = await async_memory_agent(
        "What's the best way to package and distribute my CLI tool?"
    )
    print(f"\nAssistant: {result}")

asyncio.run(main())

# Expected Token Savings: Parallel retrieval adds zero latency; structured grouping reduces context tokens 20%
# Environment: High-throughput async agents with categorized memory stores and strict latency budgets
```

## Comparison

| Option | Retrieval Method | Latency Impact | Memory Scale | Anticipatory | Best For |
|--------|-----------------|----------------|--------------|--------------|---------|
| 1. Keyword Trigger | Keyword overlap | None | <50 memories | No | Simple rule-based agents |
| 2. LLM Scoring | Claude relevance call | +1 LLM call | <100 memories | No | High-precision retrieval |
| 3. Embedding Semantic | Vector similarity | Minimal | 1M+ memories | No | Large-scale memory stores |
| 4. Multi-Signal | 4-factor weighted | None | <200 memories | No | Rich user profile systems |
| 5. Anticipatory Flow | Pattern + prediction | None | <50 memories | Yes | Sequential workflow agents |
| 6. Async Parallel | Category-parallel async | Zero added | <1000 memories | No | High-throughput async services |
