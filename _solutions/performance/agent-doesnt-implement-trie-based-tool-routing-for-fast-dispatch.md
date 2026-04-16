---
title: "Agent Doesn't Implement Trie-Based Tool Routing for Fast Dispatch"
description: "Agents with large tool registries perform linear search or repeated LLM calls to match user intent to the right tool. Implement trie-based keyword routing and intent-prefix matching to dispatch tool calls in O(k) time where k is the query length, eliminating expensive re-routing LLM calls."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-trie-based-tool-routing-for-fast-dispatch
tags: [trie, tool-routing, performance, dispatch, intent-matching, keyword-indexing]
symptoms:
  - "Agent with 200 tools makes a secondary LLM call to decide which tool to use"
  - "Tool selection latency grows linearly as tool registry expands"
  - "Simple keyword-matching tool calls still go through full LLM inference"
  - "Tool routing is a sequential loop over all registered tools"
  - "Repeated identical routing decisions are not cached"
---

## Why This Happens

As agent tool registries grow, routing decisions become expensive. A naive approach calls the LLM with all tool descriptions to select one — this takes 200–500ms and costs tokens. Many intents are deterministic: "search for X" always routes to the search tool, "get weather in Y" always routes to the weather tool. A trie (prefix tree) built over tool trigger keywords enables O(k) lookup instead of O(n) scan, and eliminates the secondary LLM call for high-confidence matches.

## Solution 1: Keyword Trie for Intent Prefix Matching

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

@dataclass
class TrieNode:
    children: Dict[str, "TrieNode"] = field(default_factory=dict)
    tools: Set[str] = field(default_factory=set)  # tool names that match at this node
    is_terminal: bool = False

class ToolRoutingTrie:
    """
    Trie built over keyword triggers for each tool.
    Given a user query, finds all tools whose trigger keywords
    appear as prefixes in the normalized query.
    """

    def __init__(self):
        self._root = TrieNode()
        self._tool_triggers: Dict[str, List[str]] = {}

    def register(self, tool_name: str, triggers: List[str]) -> None:
        """Register a tool with a list of trigger keyword phrases."""
        self._tool_triggers[tool_name] = triggers
        for trigger in triggers:
            words = self._normalize(trigger).split()
            self._insert(words, tool_name)

    def _normalize(self, text: str) -> str:
        import re
        return re.sub(r'[^\w\s]', '', text.lower()).strip()

    def _insert(self, words: List[str], tool_name: str) -> None:
        node = self._root
        for word in words:
            if word not in node.children:
                node.children[word] = TrieNode()
            node = node.children[word]
        node.tools.add(tool_name)
        node.is_terminal = True

    def match(self, query: str) -> Dict[str, float]:
        """
        Returns {tool_name: confidence} for tools whose triggers
        appear in the query. Longer matches get higher confidence.
        """
        words = self._normalize(query).split()
        matches: Dict[str, float] = {}

        for start in range(len(words)):
            node = self._root
            for depth, word in enumerate(words[start:], start=1):
                if word not in node.children:
                    break
                node = node.children[word]
                if node.is_terminal:
                    for tool in node.tools:
                        # Confidence: longer phrase = higher score
                        confidence = min(depth / max(len(words), 1), 1.0)
                        matches[tool] = max(matches.get(tool, 0.0), confidence)

        return matches

    def top_match(self, query: str, threshold: float = 0.3) -> Optional[str]:
        """Returns the best-matching tool name above threshold, or None."""
        matches = self.match(query)
        if not matches:
            return None
        best = max(matches, key=matches.get)
        return best if matches[best] >= threshold else None
```

## Solution 2: Intent Classifier with Trie Fast-Path

```python
import asyncio
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

@dataclass
class RoutingDecision:
    tool_name: str
    confidence: float
    method: str      # "trie" | "llm" | "cache"
    latency_ms: float

class HybridToolRouter:
    """
    Fast-path: trie lookup for high-confidence matches (no LLM call).
    Slow-path: LLM call for ambiguous queries.
    Cache layer: avoid repeated LLM routing calls for identical queries.
    """

    def __init__(
        self,
        trie: ToolRoutingTrie,
        llm_router: Callable[[str, List[str]], asyncio.Coroutine],
        trie_threshold: float = 0.6,
        cache_size: int = 1000,
    ):
        self._trie = trie
        self._llm_router = llm_router
        self._threshold = trie_threshold
        self._cache: Dict[str, RoutingDecision] = {}
        self._cache_size = cache_size

    async def route(self, query: str, available_tools: List[str]) -> RoutingDecision:
        import time, hashlib
        cache_key = hashlib.sha256(query.lower().encode()).hexdigest()[:16]

        # Cache hit
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return RoutingDecision(
                tool_name=cached.tool_name,
                confidence=cached.confidence,
                method="cache",
                latency_ms=0.0,
            )

        t0 = time.monotonic()

        # Trie fast-path
        matches = self._trie.match(query)
        best_tool = max(matches, key=matches.get) if matches else None
        best_conf = matches.get(best_tool, 0.0) if best_tool else 0.0

        if best_tool and best_conf >= self._threshold and best_tool in available_tools:
            decision = RoutingDecision(
                tool_name=best_tool,
                confidence=best_conf,
                method="trie",
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        else:
            # LLM slow-path
            tool_name = await self._llm_router(query, available_tools)
            decision = RoutingDecision(
                tool_name=tool_name,
                confidence=0.85,
                method="llm",
                latency_ms=(time.monotonic() - t0) * 1000,
            )

        # Cache the decision
        if len(self._cache) >= self._cache_size:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
        self._cache[cache_key] = decision
        return decision
```

## Solution 3: Inverted Index for Multi-Keyword Tool Matching

```python
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

@dataclass
class ScoredTool:
    tool_name: str
    score: float
    matched_keywords: List[str]

class InvertedIndexToolRouter:
    """
    Inverted index over tool keywords + TF-IDF-style scoring.
    Returns ranked list of tools matching the query.
    Used when multiple tools might apply and ranking matters.
    """

    def __init__(self):
        # keyword -> {tool_name: weight}
        self._index: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._tool_keyword_counts: Dict[str, int] = {}
        self._num_tools = 0

    def register(self, tool_name: str, keywords: List[str], weights: Optional[List[float]] = None) -> None:
        if weights is None:
            weights = [1.0] * len(keywords)
        self._tool_keyword_counts[tool_name] = len(keywords)
        self._num_tools += 1
        for kw, w in zip(keywords, weights):
            self._index[kw.lower()][tool_name] = w

    def search(self, query: str, top_k: int = 5) -> List[ScoredTool]:
        import math, re
        query_words = set(re.sub(r'[^\w\s]', '', query.lower()).split())
        scores: Dict[str, float] = defaultdict(float)
        matched_kws: Dict[str, List[str]] = defaultdict(list)

        for word in query_words:
            if word in self._index:
                # IDF: log(num_tools / tools_containing_this_keyword)
                idf = math.log(self._num_tools / max(len(self._index[word]), 1))
                for tool_name, tf_weight in self._index[word].items():
                    scores[tool_name] += tf_weight * idf
                    matched_kws[tool_name].append(word)

        results = [
            ScoredTool(
                tool_name=t,
                score=round(s, 4),
                matched_keywords=matched_kws[t],
            )
            for t, s in sorted(scores.items(), key=lambda x: x[1], reverse=True)
        ]
        return results[:top_k]

    def top_tool(self, query: str, min_score: float = 0.1) -> Optional[str]:
        results = self.search(query, top_k=1)
        if results and results[0].score >= min_score:
            return results[0].tool_name
        return None
```

## Solution 4: Tool Registry Builder with Auto-Extracted Triggers

```python
import re
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class ToolDefinition:
    name: str
    description: str
    explicit_triggers: List[str] = field(default_factory=list)
    auto_extracted_triggers: List[str] = field(default_factory=list)

class ToolTriggerExtractor:
    """
    Automatically extracts trigger keywords from tool descriptions
    so developers don't need to manually enumerate all triggers.
    """

    STOP_WORDS = {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "to", "of", "in", "on", "at", "for",
        "with", "by", "from", "about", "into", "through", "during", "this",
        "that", "these", "those", "it", "its", "and", "or", "but", "not",
    }

    VERB_PATTERNS = [
        r'\b(search|find|look\s+up|query|get|fetch|retrieve|read)\b',
        r'\b(create|add|insert|write|save|store|upload|post)\b',
        r'\b(update|modify|edit|change|patch|set)\b',
        r'\b(delete|remove|cancel|clear|reset)\b',
        r'\b(send|email|notify|alert|message)\b',
        r'\b(calculate|compute|analyze|summarize|translate)\b',
    ]

    def extract(self, tool: ToolDefinition) -> List[str]:
        text = tool.description.lower()
        triggers = list(tool.explicit_triggers)

        # Extract verb phrases
        for pattern in self.VERB_PATTERNS:
            matches = re.findall(pattern, text, re.IGNORECASE)
            triggers.extend(matches)

        # Extract noun phrases (2–3 word sequences, no stop words)
        words = re.sub(r'[^\w\s]', '', text).split()
        for i in range(len(words) - 1):
            if words[i] not in self.STOP_WORDS and words[i + 1] not in self.STOP_WORDS:
                triggers.append(f"{words[i]} {words[i+1]}")

        # Deduplicate and filter
        seen = set()
        unique = []
        for t in triggers:
            t = t.strip()
            if t and t not in seen and len(t) >= 3:
                seen.add(t)
                unique.append(t)

        tool.auto_extracted_triggers = unique
        return unique

class ToolRegistry:
    def __init__(self):
        self._tools: dict = {}
        self._trie = ToolRoutingTrie()
        self._inverted = InvertedIndexToolRouter()
        self._extractor = ToolTriggerExtractor()

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool
        triggers = self._extractor.extract(tool)
        all_triggers = list(set(tool.explicit_triggers + triggers))
        self._trie.register(tool.name, all_triggers)
        self._inverted.register(tool.name, all_triggers)

    def route(self, query: str) -> Optional[str]:
        # Trie first (fast)
        match = self._trie.top_match(query, threshold=0.5)
        if match:
            return match
        # Inverted index fallback
        return self._inverted.top_tool(query)
```

## Solution 5: Routing Metrics Tracker

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field

@dataclass
class RoutingStats:
    trie_hits: int = 0
    llm_fallbacks: int = 0
    cache_hits: int = 0
    total_queries: int = 0
    total_trie_ms: float = 0.0
    total_llm_ms: float = 0.0

class RoutingMetricsTracker:
    def __init__(self, router: HybridToolRouter):
        self._router = router
        self._stats = RoutingStats()
        self._tool_frequency: dict = defaultdict(int)

    async def route(self, query: str, available_tools: list) -> RoutingDecision:
        decision = await self._router.route(query, available_tools)
        self._stats.total_queries += 1
        self._tool_frequency[decision.tool_name] += 1

        if decision.method == "trie":
            self._stats.trie_hits += 1
            self._stats.total_trie_ms += decision.latency_ms
        elif decision.method == "llm":
            self._stats.llm_fallbacks += 1
            self._stats.total_llm_ms += decision.latency_ms
        elif decision.method == "cache":
            self._stats.cache_hits += 1

        return decision

    def summary(self) -> dict:
        s = self._stats
        total = max(s.total_queries, 1)
        return {
            "total_queries": total,
            "trie_hit_rate": round(s.trie_hits / total, 3),
            "llm_fallback_rate": round(s.llm_fallbacks / total, 3),
            "cache_hit_rate": round(s.cache_hits / total, 3),
            "avg_trie_latency_ms": round(s.total_trie_ms / max(s.trie_hits, 1), 2),
            "avg_llm_latency_ms": round(s.total_llm_ms / max(s.llm_fallbacks, 1), 2),
            "top_tools": sorted(self._tool_frequency.items(), key=lambda x: x[1], reverse=True)[:10],
        }
```

## Solution 6: Incremental Trie Updater (Hot-Reload Tool Definitions)

```python
import asyncio
from typing import List

class HotReloadableToolRegistry(ToolRegistry):
    """
    Supports adding and removing tools at runtime without restart.
    Rebuilds trie and inverted index incrementally.
    """

    async def add_tool(self, tool: ToolDefinition) -> None:
        self.register(tool)
        print(f"[tool_registry] registered '{tool.name}' with "
              f"{len(tool.auto_extracted_triggers)} auto-triggers")

    async def remove_tool(self, tool_name: str) -> None:
        if tool_name in self._tools:
            del self._tools[tool_name]
            # Rebuild trie and index without the removed tool
            self._trie = ToolRoutingTrie()
            self._inverted = InvertedIndexToolRouter()
            for tool in self._tools.values():
                all_triggers = list(set(tool.explicit_triggers + tool.auto_extracted_triggers))
                self._trie.register(tool.name, all_triggers)
                self._inverted.register(tool.name, all_triggers)
            print(f"[tool_registry] removed '{tool_name}', rebuilt index")

    async def watch_definitions(self, definitions_path: str, interval: float = 30.0) -> None:
        """Poll a definitions file and hot-reload changed tools."""
        import json, os
        last_mtime = 0.0
        while True:
            await asyncio.sleep(interval)
            try:
                mtime = os.path.getmtime(definitions_path)
                if mtime > last_mtime:
                    with open(definitions_path) as f:
                        defs = json.load(f)
                    for d in defs:
                        tool = ToolDefinition(
                            name=d["name"],
                            description=d["description"],
                            explicit_triggers=d.get("triggers", []),
                        )
                        await self.add_tool(tool)
                    last_mtime = mtime
            except Exception as exc:
                print(f"[tool_registry] watch error: {exc}")
```

## Comparison

| Approach | Lookup Complexity | Ranking | Auto-Extraction | LLM-Free |
|---|---|---|---|---|
| ToolRoutingTrie | O(k) prefix match | No (first match) | No | Yes |
| HybridToolRouter | O(k) + LLM fallback | No | No | Partial |
| InvertedIndexToolRouter | O(q × match) TF-IDF | Yes | No | Yes |
| ToolTriggerExtractor | N/A (build-time) | N/A | Yes | N/A |
| RoutingMetricsTracker | Wrapper overhead | N/A | N/A | N/A |
| HotReloadableToolRegistry | O(rebuild) on change | N/A | Yes | Yes |

**Best for production**: Use `ToolRegistry` with `ToolTriggerExtractor` to auto-index all tools at startup. Route with `HybridToolRouter`: trie fast-path for high-confidence keyword matches (saves 200–500ms + token cost per call), LLM fallback for ambiguous intents. Wrap with `RoutingMetricsTracker` to monitor trie hit rate and target >80% trie-resolved routing for common intents.
