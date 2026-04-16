---
title: "Agent Doesn't Implement Inverted Index for Keyword Tool Routing"
description: "AI agents that route queries to tools by scanning all tool descriptions on every request have O(T) routing cost where T is the number of tools. An inverted index maps keywords to the tools that match them, reducing routing to an O(1) lookup. For agents with 50–200 tools, this cuts routing latency from 20–100 ms (LLM-based) or 5–20 ms (scan-based) to under 1 ms for exact and prefix matches."
date: 2025-02-15
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-inverted-index-for-keyword-tool-routing
tags:
  - inverted-index
  - tool-routing
  - keyword-lookup
  - performance
  - trie
  - routing
  - agent-tools
symptoms:
  - "Agent scans all 100 tool descriptions on every query to find the right tool"
  - "Tool routing LLM call adds 300 ms to every agent turn"
  - "Simple exact-match queries like 'get_user' still trigger a full tool selection pass"
  - "Adding more tools increases routing latency linearly"
  - "Tool descriptions are re-parsed on every request rather than indexed at startup"
---

## Problem

Most agent frameworks select tools by asking the LLM to choose from a formatted list of tool descriptions — a 200–500 ms operation for every query, even when the right tool is obvious from a single keyword. An inverted index pre-processes tool names, descriptions, and tags into a keyword→tool mapping at agent startup. At query time, the router tokenises the query, looks up matching tool sets, and returns intersected candidates in under 1 ms. The LLM is reserved for ambiguous cases where index lookup returns multiple candidates.

---

## Solution 1: KeywordInvertedIndex — Build and Query Tool Index

```python
import re
import string
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set


@dataclass
class ToolEntry:
    name: str
    description: str
    tags: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteResult:
    tools: List[ToolEntry]
    matched_keywords: List[str]
    confidence: float          # 0.0–1.0
    method: str                # "exact" | "prefix" | "fuzzy" | "fallback"


class KeywordInvertedIndex:
    """
    Inverted index over tool names, descriptions, and tags.
    Supports exact, prefix, and multi-keyword intersection routing.

    Usage:
        index = KeywordInvertedIndex()
        index.build(tools)

        result = index.route("get user profile")
        if result.confidence > 0.8:
            use_tool(result.tools[0])
        else:
            # Fall through to LLM-based selection
            use_llm_routing(query)
    """

    _STOP_WORDS = frozenset({
        "a", "an", "the", "for", "to", "of", "in", "on", "at",
        "is", "are", "was", "be", "by", "do", "get", "set", "run",
        "and", "or", "not", "with", "from", "that", "this",
    })

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        self._index: Dict[str, Set[str]] = defaultdict(set)  # keyword -> tool names
        self._prefix_index: Dict[str, Set[str]] = defaultdict(set)

    def _tokenise(self, text: str) -> List[str]:
        text = text.lower()
        text = re.sub(r"[_\-/]", " ", text)
        tokens = re.findall(r"[a-z0-9]+", text)
        return [t for t in tokens if t not in self._STOP_WORDS and len(t) > 1]

    def build(self, tools: List[ToolEntry]):
        for tool in tools:
            self._tools[tool.name] = tool
            sources = (
                [tool.name, tool.description]
                + tool.tags
                + tool.keywords
            )
            all_tokens: Set[str] = set()
            for source in sources:
                all_tokens.update(self._tokenise(source))

            for token in all_tokens:
                self._index[token].add(tool.name)
                # Prefix index: index every prefix of length ≥ 3
                for length in range(3, len(token) + 1):
                    self._prefix_index[token[:length]].add(tool.name)

    def route(self, query: str, top_k: int = 5) -> RouteResult:
        query_tokens = self._tokenise(query)
        if not query_tokens:
            return RouteResult([], [], 0.0, "fallback")

        # Exact match: intersect tool sets for all query tokens
        exact_sets = [self._index.get(t, set()) for t in query_tokens]
        if exact_sets:
            intersection = exact_sets[0].copy()
            for s in exact_sets[1:]:
                intersection &= s
            if intersection:
                matched = sorted(
                    intersection,
                    key=lambda n: self._score(n, query_tokens),
                    reverse=True,
                )[:top_k]
                confidence = min(1.0, len(intersection) / 1 * 0.9
                                  if len(intersection) == 1 else 0.7)
                return RouteResult(
                    tools=[self._tools[n] for n in matched],
                    matched_keywords=query_tokens,
                    confidence=confidence,
                    method="exact",
                )

        # Prefix match
        prefix_sets = [self._prefix_index.get(t, set()) for t in query_tokens]
        if prefix_sets:
            union: Set[str] = set()
            for s in prefix_sets:
                union |= s
            if union:
                scored = sorted(
                    union,
                    key=lambda n: self._score(n, query_tokens),
                    reverse=True,
                )[:top_k]
                return RouteResult(
                    tools=[self._tools[n] for n in scored],
                    matched_keywords=query_tokens,
                    confidence=0.5,
                    method="prefix",
                )

        return RouteResult([], query_tokens, 0.0, "fallback")

    def _score(self, tool_name: str, query_tokens: List[str]) -> float:
        tool = self._tools[tool_name]
        tool_tokens = set(self._tokenise(
            f"{tool.name} {' '.join(tool.tags)} {' '.join(tool.keywords)}"
        ))
        overlap = len(set(query_tokens) & tool_tokens)
        return overlap / max(len(query_tokens), 1)

    def stats(self) -> Dict[str, Any]:
        return {
            "tools": len(self._tools),
            "index_terms": len(self._index),
            "prefix_terms": len(self._prefix_index),
        }
```

---

## Solution 2: TrieToolRouter — Prefix Tree for Exact-Name Routing

```python
from typing import Dict, List, Optional, Set


class _TrieNode:
    __slots__ = ("children", "tool_names")

    def __init__(self):
        self.children: Dict[str, "_TrieNode"] = {}
        self.tool_names: Set[str] = set()


class TrieToolRouter:
    """
    Trie-based router for tool names and aliases.
    Fastest possible prefix matching: O(k) where k is query length.

    Usage:
        router = TrieToolRouter()
        router.insert("get_user_profile",   aliases=["user", "profile", "getuser"])
        router.insert("web_search",         aliases=["search", "google", "lookup"])
        router.insert("send_email",         aliases=["email", "mail", "send"])

        matches = router.search_prefix("user")
        # -> ["get_user_profile"]
    """

    def __init__(self):
        self._root = _TrieNode()

    def insert(self, tool_name: str, aliases: Optional[List[str]] = None):
        terms = [tool_name.lower().replace("_", "")] + [
            a.lower().replace("_", "") for a in (aliases or [])
        ]
        for term in terms:
            node = self._root
            for ch in term:
                if ch not in node.children:
                    node.children[ch] = _TrieNode()
                node = node.children[ch]
                node.tool_names.add(tool_name)

    def search_prefix(self, prefix: str) -> List[str]:
        prefix = prefix.lower().replace("_", "")
        node = self._root
        for ch in prefix:
            if ch not in node.children:
                return []
            node = node.children[ch]
        return list(node.tool_names)

    def exact_match(self, query: str) -> Optional[str]:
        matches = self.search_prefix(query)
        if len(matches) == 1:
            return matches[0]
        return None
```

---

## Solution 3: TagBasedToolSelector — Structured Tag Routing

```python
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set


class TagBasedToolSelector:
    """
    Routes queries to tools via a structured tag taxonomy.
    Tags like "database", "read", "write", "user", "auth" are
    pre-assigned to tools; queries carry a tag set extracted from
    intent classification (fast regex or rule-based).

    Usage:
        selector = TagBasedToolSelector()
        selector.register("db_get_user",   tags={"database", "read", "user"})
        selector.register("db_set_user",   tags={"database", "write", "user"})
        selector.register("send_email",    tags={"email", "write", "notification"})
        selector.register("web_search",    tags={"web", "read", "search"})

        matches = selector.select(required_tags={"database", "read"})
        # -> ["db_get_user"]
    """

    def __init__(self):
        self._tools: Dict[str, Set[str]] = {}          # tool -> tags
        self._tag_index: Dict[str, Set[str]] = defaultdict(set)  # tag -> tools

    def register(self, tool_name: str, tags: Set[str]):
        self._tools[tool_name] = tags
        for tag in tags:
            self._tag_index[tag.lower()].add(tool_name)

    def select(self, required_tags: Set[str],
                optional_tags: Optional[Set[str]] = None,
                top_k: int = 5) -> List[str]:
        required = {t.lower() for t in required_tags}
        optional = {t.lower() for t in (optional_tags or set())}

        # Intersection: tools that have ALL required tags
        if not required:
            return []
        sets = [self._tag_index.get(tag, set()) for tag in required]
        candidates = sets[0].copy()
        for s in sets[1:]:
            candidates &= s

        if not candidates:
            return []

        # Rank by optional tag overlap
        def score(tool: str) -> int:
            return len(self._tools.get(tool, set()) & optional)

        return sorted(candidates, key=score, reverse=True)[:top_k]

    def tags_for(self, tool_name: str) -> Set[str]:
        return self._tools.get(tool_name, set())

    def all_tags(self) -> Set[str]:
        return set(self._tag_index.keys())
```

---

## Solution 4: HybridToolRouter — Index-First, LLM-Fallback

```python
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolRoutingDecision:
    tool_name: str
    confidence: float
    latency_ms: float
    method: str


class HybridToolRouter:
    """
    Routes queries using the inverted index first (sub-millisecond).
    Falls back to LLM-based selection only when the index returns
    multiple high-confidence candidates or no result.

    Usage:
        router = HybridToolRouter(
            index=inverted_index,
            llm_router_fn=select_tool_with_llm,
            confidence_threshold=0.8,
        )
        decision = await router.route("get user profile for id u-123")
        tool_fn = registry[decision.tool_name]
    """

    def __init__(self, index: KeywordInvertedIndex,
                 llm_router_fn: Callable,
                 confidence_threshold: float = 0.8,
                 index_hits: int = 0,
                 llm_hits: int = 0):
        self._index = index
        self._llm = llm_router_fn
        self._threshold = confidence_threshold
        self._index_hits = 0
        self._llm_hits = 0

    async def route(self, query: str) -> ToolRoutingDecision:
        t0 = time.monotonic()
        result = self._index.route(query, top_k=3)
        index_ms = (time.monotonic() - t0) * 1000

        if result.tools and result.confidence >= self._threshold:
            self._index_hits += 1
            logger.debug(
                "tool_route_index query=%r tool=%s conf=%.2f ms=%.1f",
                query[:40], result.tools[0].name, result.confidence, index_ms,
            )
            return ToolRoutingDecision(
                tool_name=result.tools[0].name,
                confidence=result.confidence,
                latency_ms=index_ms,
                method="index",
            )

        # LLM fallback
        self._llm_hits += 1
        t1 = time.monotonic()
        candidates = [t.name for t in result.tools] if result.tools else None
        tool_name = await self._llm(query, candidates=candidates)
        llm_ms = (time.monotonic() - t1) * 1000
        total_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "tool_route_llm query=%r tool=%s ms=%.1f",
            query[:40], tool_name, llm_ms,
        )
        return ToolRoutingDecision(
            tool_name=tool_name,
            confidence=0.95,
            latency_ms=total_ms,
            method="llm",
        )

    def routing_stats(self) -> Dict[str, Any]:
        total = self._index_hits + self._llm_hits
        return {
            "total_routes": total,
            "index_hits": self._index_hits,
            "llm_hits": self._llm_hits,
            "index_hit_rate": round(self._index_hits / max(total, 1), 3),
        }
```

---

## Solution 5: RoutingCacheLayer — Memoize Routed Queries

```python
import hashlib
import time
from collections import OrderedDict
from typing import Any, Dict, Optional


class RoutingCacheLayer:
    """
    LRU cache for tool routing decisions. Identical or near-identical
    queries (same tokenised form) return the same routing decision without
    re-hitting the index or LLM.

    Usage:
        cache = RoutingCacheLayer(capacity=2000, ttl_s=3600)
        router = CachedHybridRouter(hybrid_router, cache)
        decision = await router.route("get user profile u-123")
    """

    def __init__(self, capacity: int = 2000, ttl_s: float = 3600.0):
        self._capacity = capacity
        self._ttl = ttl_s
        self._cache: OrderedDict[str, tuple] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def _key(self, query: str) -> str:
        # Normalise: lowercase, collapse spaces
        normalised = " ".join(query.lower().split())
        return hashlib.sha256(normalised.encode()).hexdigest()[:20]

    def get(self, query: str) -> Optional[ToolRoutingDecision]:
        key = self._key(query)
        entry = self._cache.get(key)
        if not entry:
            self._misses += 1
            return None
        decision, ts = entry
        if time.monotonic() - ts > self._ttl:
            del self._cache[key]
            self._misses += 1
            return None
        self._cache.move_to_end(key)
        self._hits += 1
        return decision

    def put(self, query: str, decision: ToolRoutingDecision):
        key = self._key(query)
        self._cache[key] = (decision, time.monotonic())
        self._cache.move_to_end(key)
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)

    def stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "cached": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 3),
        }
```

---

## Solution 6: IndexedToolRegistry — Unified Registration and Routing

```python
from typing import Any, Callable, Dict, List, Optional


class IndexedToolRegistry:
    """
    Combines tool registration, inverted index building, and trie routing
    into a single interface. Register tools once at startup; route queries
    in microseconds.

    Usage:
        registry = IndexedToolRegistry()

        registry.register(
            name="get_user",
            fn=get_user_fn,
            description="Fetch user profile by ID",
            tags=["user", "read", "database"],
            aliases=["user_profile", "fetch_user"],
        )
        registry.build_index()

        result = registry.route("get user profile")
        if result:
            tool_fn, decision = result
            output = await tool_fn(user_id=uid)
    """

    def __init__(self):
        self._fns: Dict[str, Callable] = {}
        self._entries: List[ToolEntry] = []
        self._index: Optional[KeywordInvertedIndex] = None
        self._trie: Optional[TrieToolRouter] = None
        self._cache = RoutingCacheLayer()

    def register(self, name: str, fn: Callable,
                  description: str = "",
                  tags: Optional[List[str]] = None,
                  aliases: Optional[List[str]] = None,
                  keywords: Optional[List[str]] = None):
        self._fns[name] = fn
        self._entries.append(ToolEntry(
            name=name,
            description=description,
            tags=tags or [],
            keywords=(keywords or []) + (aliases or []),
        ))

    def build_index(self):
        self._index = KeywordInvertedIndex()
        self._index.build(self._entries)
        self._trie = TrieToolRouter()
        for entry in self._entries:
            self._trie.insert(
                entry.name,
                aliases=entry.keywords + entry.tags,
            )

    def route(self, query: str) -> Optional[tuple]:
        cached = self._cache.get(query)
        if cached:
            fn = self._fns.get(cached.tool_name)
            return (fn, cached) if fn else None

        # Trie exact match first
        if self._trie:
            exact = self._trie.exact_match(query.replace(" ", ""))
            if exact and exact in self._fns:
                decision = ToolRoutingDecision(
                    tool_name=exact, confidence=1.0,
                    latency_ms=0.0, method="trie_exact",
                )
                self._cache.put(query, decision)
                return (self._fns[exact], decision)

        # Inverted index
        if self._index:
            result = self._index.route(query)
            if result.tools:
                decision = ToolRoutingDecision(
                    tool_name=result.tools[0].name,
                    confidence=result.confidence,
                    latency_ms=0.0,
                    method=result.method,
                )
                self._cache.put(query, decision)
                fn = self._fns.get(result.tools[0].name)
                return (fn, decision) if fn else None

        return None

    def all_tools(self) -> List[str]:
        return list(self._fns.keys())

    def cache_stats(self) -> Dict[str, Any]:
        return self._cache.stats()
```

---

## Comparison

| Approach | Exact Match | Prefix Match | Tag-Based | LLM Fallback | Cache |
|---|---|---|---|---|---|
| **KeywordInvertedIndex** | Yes | Yes | No | No | No |
| **TrieToolRouter** | Yes | Yes | No | No | No |
| **TagBasedToolSelector** | No | No | Yes | No | No |
| **HybridToolRouter** | Via index | Via index | No | Yes | No |
| **RoutingCacheLayer** | Via cache | Via cache | No | No | Yes |
| **IndexedToolRegistry** | Yes | Yes | No | No | Yes |

**Key insight**: build the inverted index and trie at agent startup, not at query time. A 100-tool corpus indexes in under 50 ms and consumes under 1 MB of memory. At query time, exact-match routes complete in under 0.1 ms; even prefix-scan over 10k index terms takes under 1 ms. Reserve LLM-based routing for genuinely ambiguous queries — in typical workloads 60–80% of queries route deterministically via the index, reducing per-turn LLM calls and total latency significantly.
