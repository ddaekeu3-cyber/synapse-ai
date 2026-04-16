---
title: "Agent Doesn't Implement Selective Context Eviction by Importance Score"
description: "Assign importance scores to conversation turns and context chunks, then evict the lowest-scoring content when approaching token limits—preserving what matters most while staying within budget."
difficulty: intermediate
category: context-window
tags: [context-window, eviction, importance-scoring, memory, token-management]
---

## Problem

When conversation history grows toward the token limit, agents either fail with a context-length error or blindly truncate the oldest messages—regardless of whether those messages contain critical instructions, established facts, or user preferences. Selective eviction with importance scoring keeps the most relevant context and discards what matters least, so the agent continues functioning effectively at any conversation length.

## Solutions

### Option 1: Recency + Keyword Importance Scoring

Score messages based on recency and the presence of high-value keywords, evict lowest-scoring messages.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

HIGH_IMPORTANCE_PATTERNS = [
    r"\b(remember|always|never|must|critical|important|key|note)\b",
    r"\b(my name is|i am|i work|my goal|i need|my preference)\b",
    r"\b(error|failed|fixed|solution|resolved)\b",
    r"\b(constraint|requirement|rule|policy)\b",
    r"```",  # Code blocks are usually important
    r"\b(deadline|due|by|until|before)\b",
]

LOW_IMPORTANCE_PATTERNS = [
    r"^(thanks?|ok|sure|great|got it|understood)[.!]?$",
    r"^(yes|no|yep|nope)[.!]?$",
    r"^(hello|hi|hey|goodbye|bye)[.!]?$",
]

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def importance_score(message: dict, position: int, total: int) -> float:
    content = message.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )

    score = 0.0

    # Recency: newer messages score higher (0-4 points)
    recency = position / max(total - 1, 1)
    score += recency * 4.0

    # High-importance keywords (0-3 points)
    text_lower = content.lower()
    for pattern in HIGH_IMPORTANCE_PATTERNS:
        if re.search(pattern, text_lower):
            score += 0.5

    # Low-importance patterns (-2 points)
    for pattern in LOW_IMPORTANCE_PATTERNS:
        if re.search(pattern, text_lower.strip()):
            score -= 2.0

    # Role weight: assistant messages slightly less important than user messages
    if message.get("role") == "assistant":
        score -= 0.3

    # Length: very short messages are likely less important
    if len(content.split()) < 5:
        score -= 0.5

    return max(0.0, score)

def evict_to_budget(messages: list[dict], max_tokens: int) -> list[dict]:
    """Remove lowest-scoring messages until under token budget."""
    scored = [
        (i, msg, importance_score(msg, i, len(messages)))
        for i, msg in enumerate(messages)
    ]

    total_tokens = sum(
        estimate_tokens(
            msg.get("content", "") if isinstance(msg.get("content"), str)
            else " ".join(b.get("text", "") for b in msg.get("content", []) if isinstance(b, dict))
        )
        for msg in messages
    )

    if total_tokens <= max_tokens:
        return messages

    # Sort by score, evict lowest first (but never evict the last 2 messages)
    evictable = sorted(scored[:-2], key=lambda x: x[2])
    evict_indices = set()

    for idx, msg, score in evictable:
        content = msg.get("content", "")
        tokens = estimate_tokens(content if isinstance(content, str) else str(content))
        evict_indices.add(idx)
        total_tokens -= tokens
        if total_tokens <= max_tokens:
            break

    retained = [msg for i, msg in enumerate(messages) if i not in evict_indices]
    print(f"[Eviction] Removed {len(evict_indices)} messages "
          f"({len(messages)} → {len(retained)})")
    return retained

class ImportanceScoredAgent:
    def __init__(self, max_context_tokens: int = 2000):
        self.max_tokens = max_context_tokens
        self.messages: list[dict] = []

    async def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        self.messages = evict_to_budget(self.messages, self.max_tokens)

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=self.messages,
        )
        text = response.content[0].text
        self.messages.append({"role": "assistant", "content": text})
        return text

async def demo_importance_scoring():
    agent = ImportanceScoredAgent(max_context_tokens=1000)

    conversation = [
        "Hi!",
        "My name is Alice and I'm building a trading system. Always prioritize safety.",
        "Ok",
        "Sure",
        "What Python libraries are good for financial data?",
        "Thanks",
        "Important constraint: we must never use external APIs in production.",
        "What's 2+2?",
        "Great",
        "Given my constraints, what's the best architecture for our trading system?",
    ]

    for msg in conversation:
        response = await agent.chat(msg)
        print(f"User: {msg}")
        print(f"Agent: {response.strip()[:80]}")
        print(f"  Context size: {len(agent.messages)} messages\n")

asyncio.run(demo_importance_scoring())
```

### Option 2: LLM-Based Importance Scoring

Use a lightweight model to assign importance scores to each message, enabling more nuanced judgments.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

SCORER_SYSTEM = """You assign importance scores to conversation messages for context management.

Score each message 1-10 based on:
- 10: Critical facts, constraints, user identity, established rules, code solutions
- 7-9: Useful context, preferences, partially relevant information
- 4-6: General conversation, questions now answered
- 1-3: Filler (thanks, ok, yes/no), repetitive content

Return JSON array: [{"index": 0, "score": N, "reason": "brief"}, ...]
Score in the order messages appear."""

async def score_messages_batch(messages: list[dict]) -> list[float]:
    """Score all messages in one LLM call."""
    formatted = [
        f"[{i}] {msg['role']}: {(msg.get('content') or '')[:100]}"
        for i, msg in enumerate(messages)
    ]
    prompt = "Score these conversation messages:\n" + "\n".join(formatted)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=SCORER_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        scored = json.loads(response.content[0].text)
        scores = [0.5] * len(messages)
        for item in scored:
            idx = item.get("index", -1)
            if 0 <= idx < len(messages):
                scores[idx] = item.get("score", 5) / 10.0
        return scores
    except (json.JSONDecodeError, KeyError):
        return [0.5] * len(messages)

def estimate_tokens(msg: dict) -> int:
    content = msg.get("content", "")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return max(1, len(str(content)) // 4)

async def evict_by_llm_score(
    messages: list[dict],
    max_tokens: int,
    always_keep_last: int = 2
) -> list[dict]:
    total = sum(estimate_tokens(m) for m in messages)
    if total <= max_tokens:
        return messages

    # Score all messages
    scores = await score_messages_batch(messages)

    # Pair messages with scores, protect last N messages
    protected_indices = set(range(len(messages) - always_keep_last, len(messages)))
    evictable = [
        (i, msg, scores[i])
        for i, msg in enumerate(messages)
        if i not in protected_indices
    ]
    evictable.sort(key=lambda x: x[2])  # Lowest score first

    evict_indices = set()
    for idx, msg, score in evictable:
        tokens = estimate_tokens(msg)
        evict_indices.add(idx)
        total -= tokens
        if total <= max_tokens:
            break

    retained = [msg for i, msg in enumerate(messages) if i not in evict_indices]
    print(f"[LLM Eviction] Kept {len(retained)}/{len(messages)} messages "
          f"(removed {len(evict_indices)} lowest-scored)")
    return retained

class LLMScoredContextManager:
    def __init__(self, max_tokens: int = 3000):
        self.max_tokens = max_tokens
        self.messages: list[dict] = []
        self._eviction_count = 0

    async def add_and_trim(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        total = sum(estimate_tokens(m) for m in self.messages)

        if total > self.max_tokens:
            self.messages = await evict_by_llm_score(
                self.messages, self.max_tokens
            )
            self._eviction_count += 1

    async def chat(self, user_message: str) -> str:
        await self.add_and_trim("user", user_message)

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=self.messages,
        )
        text = response.content[0].text
        await self.add_and_trim("assistant", text)
        return text

async def demo_llm_scoring():
    manager = LLMScoredContextManager(max_tokens=500)

    turns = [
        "My project requires PCI-DSS compliance. Never suggest storing raw card data.",
        "ok",
        "I need to build a payment flow.",
        "thanks",
        "What encryption should I use for cardholder data?",
        "got it",
        "What about tokenization?",
        "Given my compliance requirements, summarize the architecture.",
    ]

    for msg in turns:
        response = await manager.chat(msg)
        print(f"[{len(manager.messages)} msgs] User: {msg[:40]}")
        print(f"  Agent: {response.strip()[:80]}\n")

asyncio.run(demo_llm_scoring())
```

### Option 3: Semantic Clustering-Based Eviction

Group messages by topic and evict entire low-relevance clusters to reduce fragmentation.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class MessageCluster:
    topic: str
    messages: list[tuple[int, dict]]  # (original_index, message)
    relevance_to_current: float = 0.5
    importance: float = 0.5

async def cluster_and_score(
    messages: list[dict],
    current_query: str
) -> list[MessageCluster]:
    """Cluster messages by topic and score relevance to current query."""
    if not messages:
        return []

    formatted = [
        f"[{i}] {msg['role']}: {(msg.get('content') or '')[:80]}"
        for i, msg in enumerate(messages)
    ]

    clustering_prompt = f"""Group these conversation messages by topic and rate each group's relevance to: "{current_query[:100]}"

Messages:
{chr(10).join(formatted)}

Return JSON array of clusters:
[{{"topic": "...", "message_indices": [0, 1], "relevance": 0.0-1.0, "importance": 0.0-1.0}}]

Relevance: how related this cluster is to the current query.
Importance: general importance regardless of current query (instructions=high, greetings=low)."""

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": clustering_prompt}]
    )

    try:
        cluster_data = json.loads(response.content[0].text)
        clusters = []
        for cd in cluster_data:
            indices = cd.get("message_indices", [])
            cluster_messages = [
                (idx, messages[idx]) for idx in indices if 0 <= idx < len(messages)
            ]
            if cluster_messages:
                clusters.append(MessageCluster(
                    topic=cd.get("topic", "unknown"),
                    messages=cluster_messages,
                    relevance_to_current=cd.get("relevance", 0.5),
                    importance=cd.get("importance", 0.5),
                ))
        return clusters
    except (json.JSONDecodeError, KeyError):
        # Fallback: single cluster with all messages
        return [MessageCluster(
            topic="all",
            messages=[(i, m) for i, m in enumerate(messages)],
            relevance_to_current=0.5,
            importance=0.5,
        )]

def estimate_tokens(msg: dict) -> int:
    content = msg.get("content", "")
    return max(1, len(str(content)) // 4)

async def cluster_evict(
    messages: list[dict],
    current_query: str,
    max_tokens: int,
    always_keep_last: int = 4,
) -> list[dict]:
    total_tokens = sum(estimate_tokens(m) for m in messages)
    if total_tokens <= max_tokens:
        return messages

    protected = set(range(len(messages) - always_keep_last, len(messages)))
    evictable_messages = [m for i, m in enumerate(messages) if i not in protected]

    clusters = await cluster_and_score(evictable_messages, current_query)

    # Score clusters: lower relevance + lower importance = evict first
    clusters.sort(key=lambda c: c.relevance_to_current + c.importance)

    evict_indices: set[int] = set()
    for cluster in clusters:
        if total_tokens <= max_tokens:
            break
        for orig_idx, msg in cluster.messages:
            evict_indices.add(orig_idx)
            total_tokens -= estimate_tokens(msg)
            if total_tokens <= max_tokens:
                break

    retained = [msg for i, msg in enumerate(messages) if i not in evict_indices]
    print(f"[Cluster Eviction] {len(messages)} → {len(retained)} messages")
    return retained

async def demo_cluster_eviction():
    # Build a long conversation spanning multiple topics
    messages = [
        {"role": "user", "content": "Hi, I'm building a Python web app."},
        {"role": "assistant", "content": "Happy to help with your Python web app."},
        {"role": "user", "content": "My team uses FastAPI and PostgreSQL."},
        {"role": "assistant", "content": "Good choices for a web application."},
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "Sure."},
        {"role": "user", "content": "Critical: the database password is rotated monthly via Vault."},
        {"role": "assistant", "content": "Understood, monthly Vault rotation."},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "No problem."},
        {"role": "user", "content": "How do I implement connection pooling for my FastAPI app?"},
        {"role": "assistant", "content": "Use SQLAlchemy with connection pool settings."},
    ]

    current_query = "Given our setup, how should I handle database migrations?"
    trimmed = await cluster_evict(messages, current_query, max_tokens=400)
    print(f"\nRetained {len(trimmed)} messages:")
    for msg in trimmed:
        print(f"  {msg['role']}: {str(msg['content'])[:60]}")

asyncio.run(demo_cluster_eviction())
```

### Option 4: Importance-Weighted Summarization

Instead of discarding low-importance messages, compress them into a summary to preserve information at lower token cost.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

SUMMARIZER_SYSTEM = (
    "You compress conversation history into a concise summary. "
    "Preserve ALL facts, constraints, user preferences, and key decisions. "
    "Discard pleasantries, acknowledgments, and repetition. "
    "Start with: 'Context summary: ...'"
)

async def summarize_segment(messages: list[dict]) -> str:
    formatted = "\n".join(
        f"{m['role'].upper()}: {m.get('content', '')[:200]}"
        for m in messages
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=SUMMARIZER_SYSTEM,
        messages=[{"role": "user", "content": f"Summarize:\n{formatted}"}]
    )
    return response.content[0].text

def importance_score_simple(msg: dict) -> float:
    import re
    content = str(msg.get("content", "")).lower()
    score = 5.0

    high_patterns = [r"\b(must|never|always|critical|important|constraint|rule|remember)\b"]
    low_patterns = [r"^(ok|thanks|sure|got it|yes|no|great)[.!]?$"]

    for p in high_patterns:
        if re.search(p, content):
            score += 2.0

    for p in low_patterns:
        if re.search(p, content.strip()):
            score -= 3.0

    if "```" in content:
        score += 2.0

    return max(0.0, score)

class SummarizingContextManager:
    def __init__(self, max_tokens: int = 2000, summarize_threshold: float = 0.85):
        self.max_tokens = max_tokens
        self.summarize_threshold = summarize_threshold
        self.messages: list[dict] = []
        self._summaries_created = 0

    def _token_count(self) -> int:
        return sum(estimate_tokens(str(m.get("content", ""))) for m in self.messages)

    async def _compress_if_needed(self):
        if self._token_count() < self.max_tokens * self.summarize_threshold:
            return

        # Score all messages
        scored = [(i, m, importance_score_simple(m)) for i, m in enumerate(self.messages)]

        # Sort by score; compress the bottom half
        scored.sort(key=lambda x: x[2])
        n_compress = max(2, len(scored) // 3)
        to_compress_indices = {idx for idx, _, _ in scored[:n_compress]}

        # Don't compress the last 4 messages
        protected = set(range(len(self.messages) - 4, len(self.messages)))
        to_compress_indices -= protected

        if not to_compress_indices:
            return

        to_compress = [m for i, m in enumerate(self.messages) if i in to_compress_indices]
        summary_text = await summarize_segment(to_compress)

        # Replace compressed messages with summary
        summary_msg = {"role": "user", "content": f"[Summary of earlier context]: {summary_text}"}
        self.messages = [
            m for i, m in enumerate(self.messages) if i not in to_compress_indices
        ]
        self.messages.insert(0, summary_msg)
        self._summaries_created += 1
        print(f"[Compress] Created summary #{self._summaries_created}. "
              f"Context: {self._token_count()} tokens")

    async def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        await self._compress_if_needed()

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=self.messages,
        )
        text = response.content[0].text
        self.messages.append({"role": "assistant", "content": text})
        return text

async def demo_summarizing_context():
    manager = SummarizingContextManager(max_tokens=500, summarize_threshold=0.8)

    turns = [
        "Hi there!",
        "I need help with a Rust project. Important: we target WebAssembly.",
        "ok",
        "We use wasm-bindgen for JS interop.",
        "sure",
        "What are the memory constraints in WASM?",
        "thanks for that",
        "Remember: our WASM module must stay under 2MB.",
        "cool",
        "Given our constraints, how should we handle large data structures?",
    ]

    for msg in turns:
        response = await manager.chat(msg)
        tokens = manager._token_count()
        print(f"[{tokens}tok] User: {msg[:40]}")
        print(f"  Agent: {response.strip()[:80]}\n")

asyncio.run(demo_summarizing_context())
```

### Option 5: Pinned Message Priority System

Allow certain messages to be explicitly pinned as always-retained, with the rest subject to eviction.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class PinnedMessage:
    role: str
    content: str
    pinned: bool = False
    importance: float = 0.5
    token_count: int = field(init=False)

    def __post_init__(self):
        self.token_count = max(1, len(self.content) // 4)

    def to_api_format(self) -> dict:
        return {"role": self.role, "content": self.content}

class PinningContextManager:
    def __init__(self, max_tokens: int = 2000):
        self.max_tokens = max_tokens
        self._messages: list[PinnedMessage] = []

    def add(self, role: str, content: str, pinned: bool = False, importance: float = 0.5):
        self._messages.append(PinnedMessage(
            role=role, content=content, pinned=pinned, importance=importance
        ))
        self._evict_if_needed()

    def pin(self, index: int):
        """Pin a specific message to prevent eviction."""
        if 0 <= index < len(self._messages):
            self._messages[index].pinned = True

    def _total_tokens(self) -> int:
        return sum(m.token_count for m in self._messages)

    def _evict_if_needed(self):
        while self._total_tokens() > self.max_tokens:
            # Find lowest-importance non-pinned message (protect last 2)
            candidates = [
                (i, m) for i, m in enumerate(self._messages[:-2])
                if not m.pinned
            ]
            if not candidates:
                break

            # Evict lowest importance
            evict_idx, _ = min(candidates, key=lambda x: x[1].importance)
            evicted = self._messages.pop(evict_idx)
            print(f"[Pin] Evicted: {evicted.role}: {evicted.content[:40]}")

    def get_messages(self) -> list[dict]:
        return [m.to_api_format() for m in self._messages]

    def stats(self) -> dict:
        pinned_count = sum(1 for m in self._messages if m.pinned)
        return {
            "total_messages": len(self._messages),
            "pinned": pinned_count,
            "evictable": len(self._messages) - pinned_count,
            "tokens": self._total_tokens(),
        }

class PinningAgent:
    def __init__(self):
        self.context = PinningContextManager(max_tokens=600)

    def set_system_constraint(self, constraint: str):
        """Add a pinned system-level constraint that will never be evicted."""
        self.context.add("user", f"[SYSTEM CONSTRAINT]: {constraint}", pinned=True, importance=10.0)
        self.context.add("assistant", f"Understood. I will always follow: {constraint}", pinned=True, importance=10.0)

    async def chat(self, user_message: str, importance: float = 0.5) -> str:
        self.context.add("user", user_message, importance=importance)

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=self.context.get_messages(),
        )
        text = response.content[0].text
        self.context.add("assistant", text, importance=importance * 0.9)
        return text

async def demo_pinning():
    agent = PinningAgent()
    agent.set_system_constraint("Always respond in formal English. Never use contractions.")
    agent.set_system_constraint("The user is a senior engineer. Skip basic explanations.")

    turns = [
        ("What is Python?", 0.3),
        ("ok", 0.1),
        ("How do decorators work?", 0.5),
        ("thanks", 0.1),
        ("great", 0.1),
        ("What's a context manager?", 0.5),
        ("ok got it", 0.1),
        ("How do I write a thread-safe singleton?", 0.7),
    ]

    for msg, importance in turns:
        response = await agent.chat(msg, importance)
        stats = agent.context.stats()
        print(f"[{stats['tokens']}tok | {stats['pinned']}pinned] User: {msg[:40]}")
        print(f"  Agent: {response.strip()[:80]}\n")

asyncio.run(demo_pinning())
```

### Option 6: Dynamic Importance Recalculation on Query Change

Recalculate all message importance scores when the user's current focus shifts, re-evicting as needed.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

RELEVANCE_SCORER = """Given a current user query, score each past conversation message's relevance.

Return JSON array: [{"index": N, "relevance": 0.0-1.0}]
- 1.0: Directly relevant to current query
- 0.7: Background context useful for current query
- 0.3: Tangentially related
- 0.0: Completely unrelated to current query

Consider only relevance to the CURRENT query, not general importance."""

@dataclass
class DynamicContextManager:
    max_tokens: int = 2000
    always_keep_last: int = 3
    messages: list[dict] = field(default_factory=list)
    _relevance_scores: list[float] = field(default_factory=list)

    def estimate_tokens(self, msg: dict) -> int:
        return max(1, len(str(msg.get("content", ""))) // 4)

    async def recalculate_relevance(self, current_query: str):
        if len(self.messages) < 4:
            self._relevance_scores = [0.5] * len(self.messages)
            return

        formatted = [
            f"[{i}] {m['role']}: {str(m.get('content', ''))[:80]}"
            for i, m in enumerate(self.messages[:-self.always_keep_last])
        ]

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=RELEVANCE_SCORER,
            messages=[{"role": "user", "content":
                f"Current query: {current_query}\n\nMessages:\n" + "\n".join(formatted)}]
        )

        try:
            scored = json.loads(response.content[0].text)
            scores = [0.5] * len(self.messages)
            for item in scored:
                idx = item.get("index", -1)
                if 0 <= idx < len(scores):
                    scores[idx] = item.get("relevance", 0.5)
            # Always give max score to protected messages
            for i in range(len(self.messages) - self.always_keep_last, len(self.messages)):
                scores[i] = 1.0
            self._relevance_scores = scores
        except (json.JSONDecodeError, KeyError):
            self._relevance_scores = [0.5] * len(self.messages)

    async def add_and_trim(self, role: str, content: str, current_query: str = ""):
        self.messages.append({"role": role, "content": content})

        total = sum(self.estimate_tokens(m) for m in self.messages)
        if total > self.max_tokens and len(self.messages) > self.always_keep_last + 2:
            await self.recalculate_relevance(current_query or content)

            protected = set(range(len(self.messages) - self.always_keep_last, len(self.messages)))
            evictable = [
                (i, m, self._relevance_scores[i] if i < len(self._relevance_scores) else 0.5)
                for i, m in enumerate(self.messages)
                if i not in protected
            ]
            evictable.sort(key=lambda x: x[2])

            evict_indices = set()
            for idx, msg, score in evictable:
                if total <= self.max_tokens:
                    break
                evict_indices.add(idx)
                total -= self.estimate_tokens(msg)

            if evict_indices:
                self.messages = [m for i, m in enumerate(self.messages) if i not in evict_indices]
                print(f"[Dynamic] Evicted {len(evict_indices)} low-relevance messages "
                      f"for query: '{current_query[:40]}'")

async def demo_dynamic_relevance():
    manager = DynamicContextManager(max_tokens=400)

    turns = [
        ("user", "My project uses FastAPI and PostgreSQL for a fintech app."),
        ("assistant", "Got it, FastAPI + PostgreSQL for fintech."),
        ("user", "We use Docker for deployment."),
        ("assistant", "Docker noted."),
        ("user", "The app must be PCI-DSS compliant."),
        ("assistant", "PCI-DSS compliance understood."),
        ("user", "ok thanks"),
        ("assistant", "Sure."),
        # Topic shifts — old Docker/fintech context less relevant
        ("user", "How do I implement connection pooling?"),
    ]

    for role, content in turns:
        await manager.add_and_trim(role, content, current_query=content if role == "user" else "")
        print(f"[{sum(manager.estimate_tokens(m) for m in manager.messages)}tok] "
              f"{role}: {content[:50]}")

asyncio.run(demo_dynamic_relevance())
```

## Comparison

| Approach | Eviction Intelligence | Token Savings | Latency Added | Best For |
|---|---|---|---|---|
| Keyword Importance Scoring | Heuristic | High | None | Fast, low-overhead agents |
| LLM-Based Scoring | Nuanced | High | 1 LLM call per batch | Accuracy-critical conversations |
| Semantic Cluster Eviction | Topic-aware | Very High | 1 LLM call | Topic-switching conversations |
| Importance-Weighted Summarization | Preserves info | Very High | 1 LLM call | Long research conversations |
| Pinned Priority System | Explicit control | Medium | None | Rule-constrained agents |
| Dynamic Relevance Recalculation | Query-adaptive | Highest | 1 LLM call | Goal-shifting conversations |

**Choose Keyword Importance Scoring** as a zero-latency baseline—it prevents eviction of critical messages with no overhead. **Choose Importance-Weighted Summarization** when eviction would lose information that might be needed later—compressing is always safer than discarding. **Choose Dynamic Relevance Recalculation** for agents that help users work through multi-phase problems where context relevance changes dramatically between phases.
