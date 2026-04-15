---
layout: solution
title: "Agent Doesn't Implement Query Result Deduplication"
category: performance
description: "Agents that issue multiple tool calls during a session often retrieve the same documents, facts, or records multiple times — wasting tokens, inflating context, and confusing the model with repeated information. Query result deduplication tracks what has already been retrieved and either skips redundant fetches or filters duplicates before they enter the context window."
tags: [deduplication, performance, token-efficiency, caching, context-window, tool-calls, retrieval]
---

# Agent Doesn't Implement Query Result Deduplication

## Problem

Multi-turn agents frequently retrieve the same information multiple times. A research agent might search "climate change" and "global warming" and get back 80% overlapping documents. A RAG agent might fetch the same chunk from different queries. Each duplicate adds tokens to the context window, degrades the signal-to-noise ratio for the LLM, and wastes API call budget.

**Symptoms:**
- Context window fills up faster than expected
- LLM gives redundant citations referencing the same source
- Tool calls return results the agent has already seen
- Token costs scale super-linearly with query count
- Agent loops when it keeps retrieving the same information

---

## Option 1: Content Hash Deduplication — Skip Seen Results

```python
import anthropic
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

@dataclass
class DeduplicatedToolResult:
    content: str
    source: str
    is_duplicate: bool
    original_hash: str

class ContentHashDeduplicator:
    """Track seen results by content hash; skip duplicates before adding to context."""

    def __init__(self, hash_prefix_len: int = 16):
        self._seen_hashes: set[str] = set()
        self._hash_len = hash_prefix_len
        self.stats = {"total": 0, "duplicates": 0, "tokens_saved": 0}

    def _hash(self, content: str) -> str:
        normalized = content.strip().lower()
        return hashlib.sha256(normalized.encode()).hexdigest()[:self._hash_len]

    def filter(self, results: list[dict], content_key: str = "content") -> list[dict]:
        """Return only results not seen before; update seen set."""
        unique = []
        for result in results:
            self.stats["total"] += 1
            content = str(result.get(content_key, result))
            h = self._hash(content)
            if h in self._seen_hashes:
                self.stats["duplicates"] += 1
                estimated_tokens = len(content.split()) // 4 * 3
                self.stats["tokens_saved"] += estimated_tokens
            else:
                self._seen_hashes.add(h)
                unique.append(result)
        return unique

    def report(self) -> str:
        total = self.stats["total"]
        dups = self.stats["duplicates"]
        rate = dups / total * 100 if total else 0
        return (f"Dedup: {dups}/{total} duplicates ({rate:.1f}%), "
                f"~{self.stats['tokens_saved']} tokens saved")

def simulate_retrieval(query: str) -> list[dict]:
    """Simulate a retrieval tool that sometimes returns overlapping results."""
    base_docs = [
        {"id": "doc1", "content": "Climate change refers to long-term shifts in global temperatures and weather patterns.", "source": "wikipedia"},
        {"id": "doc2", "content": "Global warming is the long-term heating of Earth's surface due to human activities.", "source": "noaa"},
        {"id": "doc3", "content": "The greenhouse effect traps heat in the atmosphere via CO2 and methane emissions.", "source": "nasa"},
        {"id": "doc4", "content": "Sea levels are rising at an accelerating rate due to melting ice sheets.", "source": "ipcc"},
    ]
    # Different queries return overlapping sets
    if "climate" in query.lower():
        return base_docs[:3]
    elif "warming" in query.lower() or "global" in query.lower():
        return base_docs[1:4]  # Overlaps with climate query
    else:
        return base_docs[2:]

def run_deduplicating_agent(queries: list[str]):
    client = anthropic.Anthropic()
    deduplicator = ContentHashDeduplicator()
    all_unique_results = []
    context_parts = []

    print(f"Running {len(queries)} queries with deduplication:\n")

    for query in queries:
        raw_results = simulate_retrieval(query)
        unique_results = deduplicator.filter(raw_results, content_key="content")

        print(f"Query: {query!r}")
        print(f"  Retrieved: {len(raw_results)}, Unique: {len(unique_results)}, "
              f"Duplicates filtered: {len(raw_results) - len(unique_results)}")

        all_unique_results.extend(unique_results)
        for r in unique_results:
            context_parts.append(f"[{r['source']}] {r['content']}")

    # Build context only from unique results
    context = "\n".join(context_parts)
    user_message = f"Based on this research:\n{context}\n\nSummarize the key facts."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}]
    )

    print(f"\n{deduplicator.report()}")
    print(f"Context length: {len(context.split())} words ({len(all_unique_results)} unique docs)")
    print(f"\nSummary:\n{response.content[0].text}")

run_deduplicating_agent([
    "climate change effects",
    "global warming causes",
    "climate science research"
])

# Expected Token Savings: ~30-60% for overlapping query sets — fewer duplicate docs in context
# Environment: Any retrieval pipeline; hash prefix length trades collision risk vs memory
```

---

## Option 2: Query-Level Deduplication — Skip Redundant API Calls Entirely

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class CachedQuery:
    query_hash: str
    query: str
    results: list[dict]
    fetched_at: float
    hit_count: int = 0

class QueryDeduplicator:
    """Cache exact and near-duplicate query results; never fetch the same query twice."""

    def __init__(self, ttl_seconds: int = 300):
        self._cache: dict[str, CachedQuery] = {}
        self._ttl = ttl_seconds
        self._api_calls_made = 0
        self._api_calls_saved = 0

    def _normalize_query(self, query: str) -> str:
        """Normalize query for comparison: lowercase, strip filler words."""
        stop_words = {"the", "a", "an", "what", "is", "are", "tell", "me", "about"}
        words = query.lower().split()
        significant = [w for w in words if w not in stop_words]
        return " ".join(sorted(significant))  # sort for order-independence

    def _hash(self, normalized: str) -> str:
        return hashlib.md5(normalized.encode()).hexdigest()[:12]

    def get(self, query: str) -> Optional[list[dict]]:
        normalized = self._normalize_query(query)
        h = self._hash(normalized)
        cached = self._cache.get(h)
        if cached is None:
            return None
        if time.time() - cached.fetched_at > self._ttl:
            del self._cache[h]
            return None
        cached.hit_count += 1
        self._api_calls_saved += 1
        print(f"  [CACHE HIT] '{query}' -> reusing '{cached.query}' (hit #{cached.hit_count})")
        return cached.results

    def put(self, query: str, results: list[dict]):
        normalized = self._normalize_query(query)
        h = self._hash(normalized)
        self._cache[h] = CachedQuery(
            query_hash=h,
            query=query,
            results=results,
            fetched_at=time.time()
        )
        self._api_calls_made += 1

    def report(self) -> str:
        total = self._api_calls_made + self._api_calls_saved
        return (f"API calls: {self._api_calls_made} made, "
                f"{self._api_calls_saved} saved ({self._api_calls_saved/total*100:.0f}% dedup rate)")

def fetch_from_api(query: str) -> list[dict]:
    """Simulate expensive external API call."""
    time.sleep(0.05)  # Simulate latency
    return [
        {"id": f"r{hash(query) % 100}", "content": f"Result for: {query}", "relevance": 0.9},
        {"id": f"r{hash(query) % 100 + 1}", "content": f"Related context: {query}", "relevance": 0.7},
    ]

def tool_call_with_dedup(query: str, deduplicator: QueryDeduplicator) -> list[dict]:
    """Wrap any retrieval tool with query-level deduplication."""
    cached = deduplicator.get(query)
    if cached is not None:
        return cached
    results = fetch_from_api(query)
    deduplicator.put(query, results)
    print(f"  [FETCH] '{query}' -> {len(results)} results")
    return results

def run_query_deduplicating_agent(questions: list[str]):
    client = anthropic.Anthropic()
    deduplicator = QueryDeduplicator(ttl_seconds=60)

    tools = [{
        "name": "search",
        "description": "Search for information",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query"}},
            "required": ["query"]
        }
    }]

    all_context = []

    for question in questions:
        print(f"\nQuestion: {question}")
        messages = [{"role": "user", "content": question}]

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages
        )

        # Handle tool calls with deduplication
        for block in response.content:
            if block.type == "tool_use":
                results = tool_call_with_dedup(block.input["query"], deduplicator)
                all_context.extend(results)

    print(f"\n{deduplicator.report()}")
    print(f"Total unique results accumulated: {len(set(r['id'] for r in all_context))}")

questions = [
    "What is machine learning?",
    "Tell me about ML",  # near-duplicate
    "Explain machine learning concepts",  # near-duplicate
    "What is deep learning?",  # genuinely different
]
run_query_deduplicating_agent(questions)

# Expected Token Savings: ~50% on tool call results for semantically similar queries
# Environment: Single-session; add Redis for cross-session query caching
```

---

## Option 3: Result-Set Diff — Only Inject New Information Into Context

```python
import anthropic
import hashlib
from dataclasses import dataclass, field

@dataclass
class RetrievalResult:
    result_id: str
    content: str
    metadata: dict

class IncrementalContextBuilder:
    """
    Track what's already in the context window.
    When new results arrive, inject ONLY the delta — new information not yet seen.
    """

    def __init__(self, max_context_items: int = 20):
        self._in_context: dict[str, RetrievalResult] = {}  # hash -> result
        self._insertion_order: list[str] = []
        self.max_items = max_context_items
        self.stats = {"injected": 0, "skipped": 0}

    def _content_hash(self, content: str) -> str:
        return hashlib.sha1(content.strip().lower().encode()).hexdigest()[:10]

    def add_results(self, results: list[RetrievalResult]) -> list[RetrievalResult]:
        """Returns only the results not already in context (the delta)."""
        new_results = []
        for result in results:
            h = self._content_hash(result.content)
            if h not in self._in_context:
                # Evict oldest if at capacity
                if len(self._in_context) >= self.max_items:
                    oldest_hash = self._insertion_order.pop(0)
                    del self._in_context[oldest_hash]
                self._in_context[h] = result
                self._insertion_order.append(h)
                new_results.append(result)
                self.stats["injected"] += 1
            else:
                self.stats["skipped"] += 1
        return new_results

    def build_context_block(self, new_results: list[RetrievalResult]) -> str:
        """Format only new results for injection into the next message."""
        if not new_results:
            return ""
        lines = ["[New information retrieved:]"]
        for r in new_results:
            lines.append(f"- [{r.result_id}] {r.content}")
        return "\n".join(lines)

    def report(self) -> str:
        total = self.stats["injected"] + self.stats["skipped"]
        rate = self.stats["skipped"] / total * 100 if total else 0
        return (f"Context dedup: {self.stats['injected']} injected, "
                f"{self.stats['skipped']} skipped ({rate:.0f}% duplicate rate)")

def simulate_search(topic: str, round_num: int) -> list[RetrievalResult]:
    """Simulate overlapping search results across multiple rounds."""
    shared_results = [
        RetrievalResult("r1", "Python is a high-level programming language.", {"source": "wiki"}),
        RetrievalResult("r2", "Python supports multiple programming paradigms.", {"source": "docs"}),
    ]
    round_specific = {
        1: [RetrievalResult("r3", f"Python was created by Guido van Rossum in 1991.", {"source": "history"})],
        2: [RetrievalResult("r4", f"Python 3.12 introduced significant performance improvements.", {"source": "release"})],
        3: [RetrievalResult("r5", f"Python is widely used in data science and AI.", {"source": "usage"})],
    }
    return shared_results + round_specific.get(round_num, [])

def run_incremental_context_agent():
    client = anthropic.Anthropic()
    ctx_builder = IncrementalContextBuilder(max_context_items=15)
    messages = [{"role": "user", "content": "Tell me about Python programming language."}]

    for search_round in range(1, 4):
        print(f"\n--- Search Round {search_round} ---")
        raw_results = simulate_search("python", search_round)

        # Get ONLY new results (delta)
        new_results = ctx_builder.add_results(raw_results)
        print(f"Retrieved: {len(raw_results)}, New: {len(new_results)}, "
              f"Skipped: {len(raw_results) - len(new_results)}")

        if new_results:
            # Only inject delta into the conversation
            context_block = ctx_builder.build_context_block(new_results)
            messages.append({"role": "user", "content": context_block})

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=messages
            )
            answer = response.content[0].text
            messages.append({"role": "assistant", "content": answer})
            print(f"Agent response: {answer[:100]}...")
        else:
            print("No new information — skipping LLM call")

    print(f"\n{ctx_builder.report()}")

run_incremental_context_agent()

# Expected Token Savings: ~40% — only new information is added to the context each round
# Environment: Multi-round retrieval agents; pairs well with prompt caching on static context
```

---

## Option 4: Embedding-Based Semantic Deduplication

```python
import anthropic
import json
import math
from dataclasses import dataclass, field

@dataclass
class SemanticDocument:
    doc_id: str
    content: str
    embedding: list[float] = field(default_factory=list)

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

def get_embedding(client: anthropic.Anthropic, text: str) -> list[float]:
    """
    Note: Anthropic doesn't provide a native embedding API.
    In production, use voyage-ai (voyageai.Client) or another embedding provider.
    This simulates embeddings using a hash-based deterministic vector for demonstration.
    """
    import hashlib
    h = hashlib.sha256(text.lower().encode()).digest()
    # Convert to 16-dim float vector (simulated; use real embeddings in production)
    vec = [(b / 255.0) * 2 - 1 for b in h[:16]]
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x / norm for x in vec]

class SemanticDeduplicator:
    """Remove semantically similar results using embedding cosine similarity."""

    def __init__(self, similarity_threshold: float = 0.92):
        self.threshold = similarity_threshold
        self._seen: list[SemanticDocument] = []
        self.stats = {"total": 0, "duplicates": 0}

    def is_duplicate(self, doc: SemanticDocument) -> bool:
        for seen_doc in self._seen:
            sim = cosine_similarity(doc.embedding, seen_doc.embedding)
            if sim >= self.threshold:
                print(f"  [SEM-DUP] sim={sim:.3f} >= {self.threshold}: "
                      f"'{doc.content[:40]}' ~ '{seen_doc.content[:40]}'")
                return True
        return False

    def add_if_unique(self, doc: SemanticDocument) -> bool:
        self.stats["total"] += 1
        if self.is_duplicate(doc):
            self.stats["duplicates"] += 1
            return False
        self._seen.append(doc)
        return True

    def filter_results(self, docs: list[SemanticDocument]) -> list[SemanticDocument]:
        return [doc for doc in docs if self.add_if_unique(doc)]

    def report(self) -> str:
        t = self.stats["total"]
        d = self.stats["duplicates"]
        return f"Semantic dedup: {d}/{t} near-duplicates removed ({d/t*100:.0f}%)" if t else "No results processed"

def embed_documents(client: anthropic.Anthropic, docs: list[dict]) -> list[SemanticDocument]:
    return [
        SemanticDocument(
            doc_id=d["id"],
            content=d["content"],
            embedding=get_embedding(client, d["content"])
        )
        for d in docs
    ]

def run_semantic_dedup_agent(query: str):
    client = anthropic.Anthropic()
    deduplicator = SemanticDeduplicator(similarity_threshold=0.90)

    # Simulate multiple searches returning semantically overlapping results
    search_rounds = [
        [
            {"id": "a1", "content": "Neural networks are computing systems inspired by biological brains."},
            {"id": "a2", "content": "Deep learning uses multiple layers of neural networks to learn representations."},
        ],
        [
            {"id": "b1", "content": "Artificial neural networks are modeled after the human brain's structure."},  # near-dup of a1
            {"id": "b2", "content": "Convolutional neural networks excel at image recognition tasks."},
        ],
        [
            {"id": "c1", "content": "Neural network architectures are inspired by how biological neurons work."},  # near-dup of a1
            {"id": "c2", "content": "Transformer models use attention mechanisms instead of recurrence."},
        ],
    ]

    all_unique_docs = []
    for round_num, raw_docs in enumerate(search_rounds, 1):
        print(f"\nRound {round_num}: {len(raw_docs)} results")
        embedded = embed_documents(client, raw_docs)
        unique = deduplicator.filter_results(embedded)
        all_unique_docs.extend(unique)
        print(f"  Unique added: {len(unique)}")

    print(f"\n{deduplicator.report()}")

    # Build context from unique docs only
    context = "\n".join(f"- {d.content}" for d in all_unique_docs)
    messages = [{"role": "user", "content": f"Based on these facts:\n{context}\n\nAnswer: {query}"}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=messages
    )
    print(f"\nQuery: {query}")
    print(f"Answer: {response.content[0].text}")
    print(f"Context docs used: {len(all_unique_docs)} (vs {sum(len(r) for r in search_rounds)} total retrieved)")

run_semantic_dedup_agent("How do neural networks learn?")

# Expected Token Savings: ~35-50% — removes paraphrased duplicates that exact hash would miss
# Environment: Replace simulated embeddings with voyage-ai or text-embedding-3-small for production
```

---

## Option 5: Tool Result Deduplication with LRU Cache

```python
import anthropic
import functools
import json
import time
from collections import OrderedDict
from dataclasses import dataclass

@dataclass
class CacheEntry:
    results: list[dict]
    cached_at: float
    access_count: int = 0

class LRUToolCache:
    """LRU cache for tool results — evicts least-recently-used on capacity overflow."""

    def __init__(self, max_size: int = 50, ttl_seconds: int = 600):
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl_seconds
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def _make_key(self, tool_name: str, tool_input: dict) -> str:
        canonical = json.dumps(tool_input, sort_keys=True)
        return f"{tool_name}:{canonical}"

    def get(self, tool_name: str, tool_input: dict) -> list[dict] | None:
        key = self._make_key(tool_name, tool_input)
        entry = self._cache.get(key)
        if entry is None:
            self.stats["misses"] += 1
            return None
        if time.time() - entry.cached_at > self.ttl:
            del self._cache[key]
            self.stats["misses"] += 1
            return None
        # Move to end (most recently used)
        self._cache.move_to_end(key)
        entry.access_count += 1
        self.stats["hits"] += 1
        return entry.results

    def put(self, tool_name: str, tool_input: dict, results: list[dict]):
        key = self._make_key(tool_name, tool_input)
        if len(self._cache) >= self.max_size:
            # Evict LRU (first item)
            self._cache.popitem(last=False)
            self.stats["evictions"] += 1
        self._cache[key] = CacheEntry(results=results, cached_at=time.time())
        self._cache.move_to_end(key)

    def report(self) -> str:
        total = self.stats["hits"] + self.stats["misses"]
        hit_rate = self.stats["hits"] / total * 100 if total else 0
        return (f"LRU cache: {self.stats['hits']} hits, {self.stats['misses']} misses "
                f"({hit_rate:.0f}% hit rate), {self.stats['evictions']} evictions, "
                f"{len(self._cache)}/{self.max_size} slots used")

def execute_tool(tool_name: str, tool_input: dict, cache: LRUToolCache) -> str:
    """Execute a tool call with LRU caching."""
    cached = cache.get(tool_name, tool_input)
    if cached is not None:
        print(f"  [CACHE HIT] {tool_name}({tool_input})")
        return json.dumps(cached)

    # Simulate actual tool execution
    time.sleep(0.02)
    if tool_name == "search":
        results = [{"doc": f"Result for '{tool_input.get('query', '')}'", "score": 0.95}]
    elif tool_name == "lookup":
        results = [{"value": f"Lookup result for '{tool_input.get('key', '')}'"}]
    else:
        results = [{"output": f"Tool {tool_name} executed"}]

    cache.put(tool_name, tool_input, results)
    print(f"  [FETCH] {tool_name}({tool_input})")
    return json.dumps(results)

def run_lru_cached_agent(user_message: str, turns: int = 3):
    client = anthropic.Anthropic()
    cache = LRUToolCache(max_size=20, ttl_seconds=120)

    tools = [
        {
            "name": "search",
            "description": "Search for documents",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        },
        {
            "name": "lookup",
            "description": "Look up a specific value by key",
            "input_schema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"]
            }
        }
    ]

    messages = [{"role": "user", "content": user_message}]

    for turn in range(turns):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result_str = execute_tool(block.name, block.input, cache)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str
                })

        if response.stop_reason == "end_turn":
            break

        messages.append({"role": "assistant", "content": response.content})
        if tool_results:
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    print(f"\n{cache.report()}")

run_lru_cached_agent(
    "Search for Python tutorials, then look up the Python version, "
    "then search for Python tutorials again to verify.",
    turns=4
)

# Expected Token Savings: ~45% on tool results for agents that revisit the same queries
# Environment: In-process LRU; use Redis with TTL for multi-agent or cross-session dedup
```

---

## Option 6: Deduplication Pipeline with Tiered Filters

```python
import anthropic
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class Document:
    doc_id: str
    content: str
    source: str
    metadata: dict = field(default_factory=dict)

@dataclass
class DeduplicationReport:
    input_count: int
    output_count: int
    removed_exact: int
    removed_near: int
    removed_short: int
    processing_ms: float

    @property
    def reduction_pct(self) -> float:
        return (1 - self.output_count / self.input_count) * 100 if self.input_count else 0.0

class DeduplicationPipeline:
    """
    Tiered deduplication: fast filters first, expensive filters last.
    Tier 1: Exact content hash (O(1))
    Tier 2: Normalized hash — collapse whitespace/case (O(n))
    Tier 3: Short content filter — too short to be useful (O(1))
    Tier 4: Substring containment — one doc fully contains another (O(n²))
    """

    def __init__(
        self,
        min_content_length: int = 50,
        substring_threshold: float = 0.85
    ):
        self._exact_hashes: set[str] = set()
        self._norm_hashes: set[str] = set()
        self.min_length = min_content_length
        self.substring_threshold = substring_threshold

    def _exact_hash(self, content: str) -> str:
        return hashlib.md5(content.encode()).hexdigest()

    def _norm_hash(self, content: str) -> str:
        normalized = re.sub(r"\s+", " ", content.lower().strip())
        normalized = re.sub(r"[^\w\s]", "", normalized)
        return hashlib.md5(normalized.encode()).hexdigest()

    def _is_substring_duplicate(self, content: str, accepted: list[Document]) -> bool:
        """Check if content is mostly contained in an already-accepted document."""
        content_words = set(content.lower().split())
        if len(content_words) < 5:
            return False
        for doc in accepted:
            doc_words = set(doc.content.lower().split())
            overlap = len(content_words & doc_words) / len(content_words)
            if overlap >= self.substring_threshold:
                return True
        return False

    def run(self, documents: list[Document]) -> tuple[list[Document], DeduplicationReport]:
        start = time.time()
        removed_exact = removed_near = removed_short = 0
        accepted: list[Document] = []

        for doc in documents:
            # Tier 1: Exact hash
            exact_h = self._exact_hash(doc.content)
            if exact_h in self._exact_hashes:
                removed_exact += 1
                continue
            self._exact_hashes.add(exact_h)

            # Tier 2: Normalized hash
            norm_h = self._norm_hash(doc.content)
            if norm_h in self._norm_hashes:
                removed_near += 1
                continue
            self._norm_hashes.add(norm_h)

            # Tier 3: Too short
            if len(doc.content.strip()) < self.min_length:
                removed_short += 1
                continue

            # Tier 4: Substring containment (most expensive, run last)
            if self._is_substring_duplicate(doc.content, accepted):
                removed_near += 1
                continue

            accepted.append(doc)

        elapsed_ms = (time.time() - start) * 1000
        report = DeduplicationReport(
            input_count=len(documents),
            output_count=len(accepted),
            removed_exact=removed_exact,
            removed_near=removed_near,
            removed_short=removed_short,
            processing_ms=elapsed_ms
        )
        return accepted, report

def run_pipeline_agent(user_query: str):
    client = anthropic.Anthropic()
    pipeline = DeduplicationPipeline(min_content_length=40, substring_threshold=0.80)

    # Simulate multiple retrieval rounds with duplicates
    all_raw_docs = [
        Document("d1", "Machine learning is a subset of artificial intelligence that enables systems to learn from data.", "source_a"),
        Document("d2", "Machine learning is a subset of artificial intelligence that enables systems to learn from data.", "source_b"),  # exact dup
        Document("d3", "Machine Learning is a Subset of Artificial Intelligence that enables systems to learn from data!!!", "source_c"),  # near dup
        Document("d4", "Deep learning is a type of machine learning that uses neural networks with multiple layers.", "source_a"),
        Document("d5", "Deep learning uses neural networks.", "source_d"),  # too short + substring
        Document("d6", "Reinforcement learning trains agents to make decisions by rewarding desired behaviors.", "source_b"),
        Document("d7", "ML systems learn from data.", "source_e"),  # too short
        Document("d8", "Supervised learning requires labeled training data to learn the mapping from inputs to outputs.", "source_c"),
    ]

    print(f"Input: {len(all_raw_docs)} documents")
    unique_docs, report = pipeline.run(all_raw_docs)

    print(f"\nDeduplication Report:")
    print(f"  Input:          {report.input_count}")
    print(f"  Output:         {report.output_count}")
    print(f"  Exact dups:     {report.removed_exact}")
    print(f"  Near dups:      {report.removed_near}")
    print(f"  Too short:      {report.removed_short}")
    print(f"  Reduction:      {report.reduction_pct:.0f}%")
    print(f"  Processing:     {report.processing_ms:.1f}ms")

    context = "\n".join(f"- {d.content}" for d in unique_docs)
    messages = [{"role": "user", "content": f"Based on:\n{context}\n\nAnswer: {user_query}"}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=messages
    )
    print(f"\nAnswer: {response.content[0].text}")

run_pipeline_agent("What are the main types of machine learning?")

# Expected Token Savings: ~40-55% via tiered filtering — exact hash is free, substring is expensive but catches paraphrases
# Environment: Production RAG pipelines; run Tier 4 only on < 200 docs due to O(n²) complexity
```

---

## Comparison

| Option | Detection Method | Speed | Cross-Session | Catches Paraphrases | Best For |
|--------|----------------|-------|--------------|--------------------|----|
| Content Hash | SHA-256 exact match | O(1) | With persistence | No | Exact duplicate tool results |
| Query Dedup | Normalized query hash | O(1) | With Redis | Partial | Redundant API calls |
| Incremental Delta | Content hash, per-round | O(n) | No | No | Multi-round retrieval agents |
| Semantic Embedding | Cosine similarity | O(n·d) | With vector DB | Yes | Near-duplicate documents |
| LRU Tool Cache | Tool name + input JSON | O(1) | With Redis | No | Repeated tool calls |
| Tiered Pipeline | Hash + length + substring | O(n²) | Configurable | Yes | Production RAG pipelines |

**Recommendation:** Start with **Option 1** (content hash) for immediate wins at zero overhead. Add **Option 2** (query dedup) to eliminate redundant API calls entirely. Use **Option 6** (tiered pipeline) for production RAG where quality matters — it catches paraphrased duplicates that exact hashing misses while keeping expensive checks last.
