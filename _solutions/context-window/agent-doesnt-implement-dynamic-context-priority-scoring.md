---
layout: solution
title: "Agent Doesn't Implement Dynamic Context Priority Scoring"
category: context-window
description: "Score each piece of context by recency, relevance, and importance before injection, evicting low-priority content when the context window budget is tight."
tags: [context-window, priority, scoring, eviction, token-budget, context-management]
---

# Agent Doesn't Implement Dynamic Context Priority Scoring

Agents near their context limit either truncate blindly (losing important early instructions) or fail entirely. Dynamic priority scoring assigns each context element a composite score based on recency, task relevance, and explicit importance, then fills the context window greedily with the highest-scoring items — ensuring the most valuable content always makes it in.

## Option 1: Recency + Importance Score with Hard Budget

```python
import time
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()
MAX_CONTEXT_TOKENS = 4000  # conservative budget for demonstration


@dataclass
class ContextItem:
    content: str
    importance: float       # 0.0–1.0 set at write time
    created_at: float = field(default_factory=time.time)
    item_type: str = "fact"  # "instruction" | "fact" | "tool_result" | "history"

    @property
    def approx_tokens(self) -> int:
        return max(1, len(self.content) // 4)


def priority_score(item: ContextItem, now: float, max_age: float) -> float:
    recency = 1.0 - min((now - item.created_at) / max(max_age, 1.0), 1.0)
    type_boost = {"instruction": 1.5, "tool_result": 1.2, "fact": 1.0, "history": 0.7}.get(item.item_type, 1.0)
    return item.importance * type_boost + 0.3 * recency


def select_context(items: list[ContextItem], token_budget: int) -> list[ContextItem]:
    now = time.time()
    max_age = max((now - i.created_at for i in items), default=1.0)
    ranked = sorted(items, key=lambda i: priority_score(i, now, max_age), reverse=True)

    selected = []
    used = 0
    for item in ranked:
        if used + item.approx_tokens > token_budget:
            continue
        selected.append(item)
        used += item.approx_tokens

    return selected


def run_agent(user_query: str, context_items: list[ContextItem]) -> str:
    selected = select_context(context_items, MAX_CONTEXT_TOKENS)
    print(f"[CONTEXT] {len(selected)}/{len(context_items)} items fit ({sum(i.approx_tokens for i in selected)} tokens)")

    context_block = "\n".join(f"[{i.item_type.upper()}] {i.content}" for i in selected)
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nContext:\n{context_block}",
        messages=[{"role": "user", "content": user_query}],
    )
    return r.content[0].text


if __name__ == "__main__":
    items = [
        ContextItem("Always respond in English.", importance=1.0, item_type="instruction"),
        ContextItem("User's name is Alex.", importance=0.8, item_type="fact"),
        ContextItem("User's project uses Python 3.11.", importance=0.7, item_type="fact"),
        ContextItem("Earlier tool result: file list = [main.py, utils.py]", importance=0.5, item_type="tool_result", created_at=time.time() - 300),
        ContextItem("User mentioned async patterns 2 hours ago.", importance=0.3, item_type="history", created_at=time.time() - 7200),
        ContextItem("Current task: review the agent's memory module.", importance=0.9, item_type="instruction"),
    ]
    print(run_agent("What should I focus on in the code review?", items))

# Expected Token Savings: Priority eviction saves 20-50% context tokens on tight budgets
# Environment: Python 3.9+; tune type_boost weights for your agent's content mix
```

## Option 2: Relevance-Weighted Scoring with Query Matching

```python
import re
import math
import time
import anthropic
from collections import Counter
from dataclasses import dataclass, field

client = anthropic.Anthropic()


@dataclass
class ContextChunk:
    text: str
    importance: float = 0.5
    created_at: float = field(default_factory=time.time)
    pinned: bool = False  # pinned items always included


def tokenize(text: str) -> Counter:
    return Counter(re.findall(r'\w+', text.lower()))


def relevance(query: str, chunk: ContextChunk) -> float:
    q_tokens = tokenize(query)
    c_tokens = tokenize(chunk.text)
    common = set(q_tokens) & set(c_tokens)
    if not common:
        return 0.0
    dot = sum(q_tokens[t] * c_tokens[t] for t in common)
    return dot / (math.sqrt(sum(v**2 for v in q_tokens.values())) *
                  math.sqrt(sum(v**2 for v in c_tokens.values())) + 1e-9)


def score_chunk(query: str, chunk: ContextChunk, now: float, max_age: float) -> float:
    if chunk.pinned:
        return float('inf')
    rel = relevance(query, chunk)
    recency = 1.0 - min((now - chunk.created_at) / max(max_age, 1.0), 1.0)
    return 0.5 * rel + 0.3 * chunk.importance + 0.2 * recency


def fill_context(query: str, chunks: list[ContextChunk], token_budget: int) -> list[ContextChunk]:
    now = time.time()
    max_age = max((now - c.created_at for c in chunks), default=1.0)
    ranked = sorted(chunks, key=lambda c: score_chunk(query, c, now, max_age), reverse=True)

    selected = []
    used = 0
    for chunk in ranked:
        cost = max(1, len(chunk.text) // 4)
        if used + cost <= token_budget:
            selected.append(chunk)
            used += cost
    return selected


CHUNKS = [
    ContextChunk("System: You are a Python code assistant.", importance=1.0, pinned=True),
    ContextChunk("User's IDE is VS Code with the Pylance extension.", importance=0.4),
    ContextChunk("The agent uses asyncio.TaskGroup for structured concurrency.", importance=0.7),
    ContextChunk("Error log: TimeoutError in fetch_data() on line 42.", importance=0.9, created_at=time.time() - 30),
    ContextChunk("Documentation: asyncio.TaskGroup was added in Python 3.11.", importance=0.6),
    ContextChunk("Previous turn: User asked about error handling.", importance=0.5, created_at=time.time() - 60),
    ContextChunk("User prefers type annotations.", importance=0.3),
]


def run_agent(query: str, budget: int = 800) -> str:
    selected = fill_context(query, CHUNKS, budget)
    print(f"[CONTEXT] {len(selected)}/{len(CHUNKS)} chunks selected")

    ctx = "\n".join(c.text for c in selected)
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=ctx,
        messages=[{"role": "user", "content": query}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print(run_agent("How should I fix the TimeoutError in the asyncio code?"))

# Expected Token Savings: Relevance scoring keeps task-specific chunks; pinned items always fit
# Environment: Python 3.9+; replace tokenize() with vector embeddings for semantic relevance
```

## Option 3: SQLite-Tracked Context Budget with Tier Allocation

```python
import sqlite3
import time
import anthropic
from dataclasses import dataclass

DB_PATH = "context_budget.db"
client = anthropic.Anthropic()

# Token budget tiers (total = TOTAL_BUDGET)
TOTAL_BUDGET = 6000
TIER_BUDGETS = {
    "system":       1000,   # system prompt, persona
    "instruction":  800,    # task instructions
    "tool_result":  2000,   # recent tool outputs
    "memory":       1500,   # relevant memories
    "history":      700,    # conversation history
}


@dataclass
class ContextEntry:
    tier: str
    content: str
    importance: float
    created_at: float


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS context_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tier TEXT, content TEXT, importance REAL, created_at REAL
        )
    """)
    conn.commit()
    return conn


def add_entry(conn: sqlite3.Connection, tier: str, content: str, importance: float = 0.5) -> None:
    conn.execute("INSERT INTO context_entries VALUES (NULL,?,?,?,?)", (tier, content, importance, time.time()))
    conn.commit()


def select_within_budget(conn: sqlite3.Connection, tier: str, budget: int) -> list[str]:
    rows = conn.execute(
        "SELECT content, importance, created_at FROM context_entries WHERE tier=? ORDER BY importance DESC, created_at DESC",
        (tier,),
    ).fetchall()
    selected = []
    used = 0
    for content, imp, _ in rows:
        cost = max(1, len(content) // 4)
        if used + cost <= budget:
            selected.append(content)
            used += cost
    return selected


def build_context(conn: sqlite3.Connection) -> str:
    sections = []
    for tier, budget in TIER_BUDGETS.items():
        items = select_within_budget(conn, tier, budget)
        if items:
            sections.append(f"=== {tier.upper()} ===\n" + "\n".join(items))
    used = sum(len(s) // 4 for s in sections)
    print(f"[CONTEXT] {used}/{TOTAL_BUDGET} tokens used across {len(sections)} tiers")
    return "\n\n".join(sections)


def run_agent(query: str) -> str:
    conn = init_db()
    # Seed on first run
    if conn.execute("SELECT COUNT(*) FROM context_entries").fetchone()[0] == 0:
        seeds = [
            ("system", "You are a helpful Python assistant.", 1.0),
            ("instruction", "Current task: debug the async fetch pipeline.", 0.9),
            ("tool_result", "fetch_data() returned: {'status': 'timeout', 'elapsed': 31.2}", 0.8),
            ("memory", "User prefers asyncio over threading.", 0.7),
            ("memory", "Project uses Python 3.11 with type annotations.", 0.6),
            ("history", "Previous turn: user asked about connection pooling.", 0.4),
        ]
        for tier, content, importance in seeds:
            add_entry(conn, tier, content, importance)

    context = build_context(conn)
    conn.close()

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=context,
        messages=[{"role": "user", "content": query}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print(run_agent("How should I fix the timeout issue in the async pipeline?"))

# Expected Token Savings: Tier budgets guarantee headroom for each content type; prevents single tier flooding
# Environment: Python 3.9+, SQLite3; adjust TIER_BUDGETS based on your agent's content distribution
```

## Option 4: Sliding Window with Importance-Based Eviction

```python
import time
import anthropic
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

MAX_WINDOW_TOKENS = 3000


@dataclass
class WindowItem:
    role: str       # "system" | "user" | "assistant" | "tool"
    content: str
    importance: float = 0.5
    created_at: float = field(default_factory=time.time)
    protected: bool = False  # never evict

    @property
    def tokens(self) -> int:
        return max(1, len(self.content) // 4)


class PriorityWindow:
    def __init__(self, max_tokens: int) -> None:
        self._items: deque[WindowItem] = deque()
        self._max_tokens = max_tokens

    @property
    def total_tokens(self) -> int:
        return sum(i.tokens for i in self._items)

    def add(self, item: WindowItem) -> None:
        self._items.append(item)
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        while self.total_tokens > self._max_tokens:
            # Find lowest-priority non-protected item
            evict_candidates = [
                (i, idx) for idx, i in enumerate(self._items) if not i.protected
            ]
            if not evict_candidates:
                break
            # Score: lower importance + older = evict first
            now = time.time()
            worst = min(evict_candidates, key=lambda x: x[0].importance - 0.1 * (now - x[0].created_at) / 3600)
            worst_item, worst_idx = worst
            print(f"[EVICT] Removing {worst_item.role}: '{worst_item.content[:40]}...' (importance={worst_item.importance:.1f})")
            items_list = list(self._items)
            items_list.pop(worst_idx)
            self._items = deque(items_list)

    def to_messages(self) -> list[dict]:
        return [{"role": i.role if i.role in ("user", "assistant") else "user", "content": i.content}
                for i in self._items if i.role not in ("system",)]

    def system_text(self) -> str:
        system_items = [i.content for i in self._items if i.role == "system"]
        return "\n".join(system_items)


WINDOW = PriorityWindow(max_tokens=MAX_WINDOW_TOKENS)


def add_to_window(role: str, content: str, importance: float = 0.5, protected: bool = False) -> None:
    WINDOW.add(WindowItem(role=role, content=content, importance=importance, protected=protected))


def run_turn(user_message: str) -> str:
    add_to_window("user", user_message, importance=0.8)
    print(f"[WINDOW] {WINDOW.total_tokens}/{MAX_WINDOW_TOKENS} tokens")

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=WINDOW.system_text() or "You are a helpful assistant.",
        messages=WINDOW.to_messages() or [{"role": "user", "content": user_message}],
    )
    reply = r.content[0].text
    add_to_window("assistant", reply, importance=0.6)
    return reply


if __name__ == "__main__":
    # Seed window
    add_to_window("system", "You are a Python expert assistant.", importance=1.0, protected=True)
    add_to_window("user", "I'm working on an async data pipeline.", importance=0.7)
    add_to_window("assistant", "Great! What kind of data are you processing?", importance=0.5)

    print(run_turn("I'm getting TimeoutError in my fetch function."))
    print(run_turn("Should I use asyncio.wait_for or a custom timeout?"))

# Expected Token Savings: Sliding eviction prevents context overflow; protected items always survive
# Environment: Python 3.9+; tune importance weights based on how critical each content type is
```

## Option 5: LLM-Judged Relevance Filtering Before Context Assembly

```python
import json
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

RELEVANCE_JUDGE_PROMPT = """Given a user's question and a list of context candidates, score each from 0-10 for relevance.
0 = completely irrelevant, 10 = directly answers or strongly informs the question.

Question: {question}

Context candidates (by index):
{candidates}

Return JSON array of objects: [{{"index": N, "score": <int 0-10>}}]"""


async def score_candidates(question: str, candidates: list[str]) -> list[tuple[int, float]]:
    candidate_text = "\n".join(f"{i}: {c[:100]}" for i, c in enumerate(candidates))
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": RELEVANCE_JUDGE_PROMPT.format(
            question=question, candidates=candidate_text
        )}],
    )
    try:
        scores = json.loads(r.content[0].text)
        return [(s["index"], float(s["score"])) for s in scores]
    except (json.JSONDecodeError, KeyError):
        return [(i, 5.0) for i in range(len(candidates))]


async def priority_context_assembly(
    question: str,
    candidates: list[str],
    token_budget: int = 2000,
    min_score: float = 4.0,
) -> list[str]:
    scores = await score_candidates(question, candidates)
    print(f"[JUDGE] Scores: {[(candidates[i][:30], s) for i, s in scores]}")

    # Filter by minimum relevance score
    qualified = [(i, s) for i, s in scores if s >= min_score]
    ranked = sorted(qualified, key=lambda x: x[1], reverse=True)

    selected = []
    used = 0
    for idx, score in ranked:
        cost = max(1, len(candidates[idx]) // 4)
        if used + cost <= token_budget:
            selected.append(candidates[idx])
            used += cost

    return selected


async def run_agent(question: str, context_pool: list[str]) -> str:
    selected = await priority_context_assembly(question, context_pool)
    ctx = "\n".join(f"- {c}" for c in selected)

    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nRelevant context:\n{ctx}",
        messages=[{"role": "user", "content": question}],
    )
    return r.content[0].text


async def main() -> None:
    pool = [
        "The agent uses asyncio for concurrent tool execution.",
        "The agent's database backend is PostgreSQL.",
        "TimeoutError was raised in fetch_data() at 31 seconds.",
        "User's favorite color is blue.",
        "asyncio.wait_for() wraps a coroutine with a timeout.",
        "The agent runs on Python 3.11.",
        "The team uses Slack for communication.",
        "asyncio.TimeoutError is a subclass of concurrent.futures.TimeoutError.",
    ]
    result = await run_agent("How do I fix a TimeoutError in my asyncio fetch function?", pool)
    print(result)


asyncio.run(main())

# Expected Token Savings: LLM judge filters irrelevant context; adds ~80 tokens, saves 200-1000
# Environment: Python 3.11+; cache judge scores for repeated queries to avoid overhead
```

## Option 6: Multi-Signal Composite Score with Decay Curves

```python
import math
import time
import anthropic
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()


class ContentType(Enum):
    SYSTEM_INSTRUCTION = "system_instruction"
    CURRENT_TASK        = "current_task"
    TOOL_RESULT         = "tool_result"
    USER_PREFERENCE     = "user_preference"
    CONVERSATION        = "conversation"
    BACKGROUND_INFO     = "background_info"


# Base importance weights per content type
TYPE_BASE: dict[ContentType, float] = {
    ContentType.SYSTEM_INSTRUCTION: 1.0,
    ContentType.CURRENT_TASK:        0.95,
    ContentType.TOOL_RESULT:         0.85,
    ContentType.USER_PREFERENCE:     0.70,
    ContentType.CONVERSATION:        0.50,
    ContentType.BACKGROUND_INFO:     0.40,
}

# Decay half-lives in seconds per content type
HALF_LIFE: dict[ContentType, float] = {
    ContentType.SYSTEM_INSTRUCTION: float('inf'),  # never decays
    ContentType.CURRENT_TASK:        float('inf'),
    ContentType.TOOL_RESULT:         300.0,         # 5 min
    ContentType.USER_PREFERENCE:     86400.0,       # 1 day
    ContentType.CONVERSATION:        600.0,         # 10 min
    ContentType.BACKGROUND_INFO:     3600.0,        # 1 hour
}


@dataclass
class ContextElement:
    content: str
    ctype: ContentType
    created_at: float = field(default_factory=time.time)
    access_count: int = 0

    def score(self) -> float:
        base = TYPE_BASE[self.ctype]
        age = time.time() - self.created_at
        hl = HALF_LIFE[self.ctype]
        decay = 1.0 if math.isinf(hl) else math.exp(-math.log(2) * age / hl)
        # Access boost: frequently accessed content stays relevant
        access_boost = math.log1p(self.access_count) * 0.1
        return base * decay + access_boost

    @property
    def tokens(self) -> int:
        return max(1, len(self.content) // 4)


def assemble_context(elements: list[ContextElement], budget: int) -> list[ContextElement]:
    ranked = sorted(elements, key=lambda e: e.score(), reverse=True)
    selected = []
    used = 0
    for e in ranked:
        if used + e.tokens <= budget:
            selected.append(e)
            e.access_count += 1
            used += e.tokens
    print(f"[SCORE] Selected {len(selected)}/{len(elements)} elements ({used} tokens)")
    return selected


ELEMENTS: list[ContextElement] = [
    ContextElement("You are an expert Python developer.", ContentType.SYSTEM_INSTRUCTION),
    ContextElement("Current task: optimize the async data ingestion pipeline.", ContentType.CURRENT_TASK),
    ContextElement("Tool result: profiler shows 80% time in I/O wait.", ContentType.TOOL_RESULT, created_at=time.time() - 30),
    ContextElement("User prefers async/await over threading.", ContentType.USER_PREFERENCE),
    ContextElement("Older tool result: memory usage is 2GB.", ContentType.TOOL_RESULT, created_at=time.time() - 600),
    ContextElement("Background: service handles 10k records/hour.", ContentType.BACKGROUND_INFO),
    ContextElement("Earlier conversation: user asked about batch sizes.", ContentType.CONVERSATION, created_at=time.time() - 900),
]


def run_agent(query: str, budget: int = 1200) -> str:
    selected = assemble_context(ELEMENTS, budget)
    ctx = "\n".join(f"[{e.ctype.value}] {e.content}" for e in selected)

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nContext:\n{ctx}",
        messages=[{"role": "user", "content": query}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print(run_agent("How can I reduce the I/O wait time in the pipeline?"))

# Expected Token Savings: Exponential decay evicts stale content; access boost keeps referenced items
# Environment: Python 3.9+; tune HALF_LIFE values based on how quickly content becomes stale in your domain
```

## Comparison

| Option | Scoring Signals | Eviction | Tier Allocation | Dynamic | Best For |
|--------|----------------|---------|----------------|---------|----------|
| 1. Recency + Importance | Recency + importance | Budget fill | No | No | Simple agents |
| 2. Relevance-Weighted | Relevance + recency + importance | Budget fill | No | Per-query | RAG pipelines |
| 3. SQLite Tier Budget | Importance + recency | Per-tier budget | Yes | No | Multi-content-type agents |
| 4. Sliding Eviction | Importance + age | Lowest-priority | No | Rolling | Multi-turn conversations |
| 5. LLM Judge | LLM relevance score | Min-score filter | No | Per-query | Diverse, mixed-quality context |
| 6. Decay Curves | Type-base × decay × access | Budget fill | No | Time-based | Long-running agents |
