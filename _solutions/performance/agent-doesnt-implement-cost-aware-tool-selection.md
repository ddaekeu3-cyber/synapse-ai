---
title: "Agent Doesn't Implement Cost-Aware Tool Selection"
description: "When multiple tools can satisfy a request, agents that always call the most capable (and expensive) tool waste budget. Cost-aware tool selection picks the cheapest tool that meets the quality threshold for the task."
difficulty: intermediate
category: performance
tags: [cost, tool-selection, routing, budget, performance, optimization, llm]
---

## Problem

An agent has access to `web_search` (expensive, real-time), `cached_search` (cheap, possibly stale), and `local_lookup` (free, limited scope). For every query it always calls `web_search`, spending 10x more than necessary. The same applies to model routing: always using Claude Opus when Haiku would suffice for simple lookups.

```python
# Broken: always uses the most expensive tool regardless of query complexity
async def answer_question(question: str) -> str:
    result = await web_search(question)  # $0.01/call, always
    return result
# Could have used cached_search ($0.001) for 80% of queries
```

---

## Solution 1: Tool Cost Registry with Fallback Chain

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
import asyncio

@dataclass
class ToolSpec:
    name: str
    cost_usd: float          # estimated cost per call
    latency_ms: float        # estimated latency
    quality_score: float     # 0.0–1.0, how good results are
    freshness: str           # "realtime", "minutes", "hours", "days", "static"
    fn: Callable[..., Awaitable[Any]]

class CostAwareToolSelector:
    """
    Given a required quality threshold, select the cheapest tool that meets it.
    Falls back to more expensive tools only if cheaper ones fail or are insufficient.
    """

    def __init__(self, tools: list[ToolSpec],
                 quality_threshold: float = 0.8):
        # Sort by cost ascending — cheapest first
        self._tools = sorted(tools, key=lambda t: t.cost_usd)
        self._quality_threshold = quality_threshold
        self._call_counts: dict[str, int] = {t.name: 0 for t in tools}
        self._total_cost: float = 0.0

    async def call(self, *args, quality_override: float | None = None,
                   freshness_required: str | None = None, **kwargs) -> Any:
        """
        Try tools in cost order until one succeeds at the required quality.
        """
        threshold = quality_override or self._quality_threshold
        freshness_order = ["realtime", "minutes", "hours", "days", "static"]

        for tool in self._tools:
            # Skip tools that don't meet freshness requirement
            if freshness_required:
                tool_idx = freshness_order.index(tool.freshness) \
                    if tool.freshness in freshness_order else 99
                req_idx = freshness_order.index(freshness_required) \
                    if freshness_required in freshness_order else 0
                if tool_idx > req_idx:
                    continue

            if tool.quality_score < threshold:
                continue  # doesn't meet quality bar

            try:
                result = await tool.fn(*args, **kwargs)
                self._call_counts[tool.name] += 1
                self._total_cost += tool.cost_usd
                return result
            except Exception as e:
                print(f"[ToolSelector] {tool.name} failed: {e}, trying next")
                continue

        raise RuntimeError("No tool succeeded above quality threshold")

    def cost_summary(self) -> dict:
        return {
            "total_cost_usd": round(self._total_cost, 6),
            "calls_by_tool": dict(self._call_counts),
            "avg_cost_per_call": round(
                self._total_cost / max(1, sum(self._call_counts.values())), 6
            ),
        }

# Usage
async def demo():
    async def local_kb(q): return f"KB: {q}"
    async def cached_search(q): return f"Cached: {q}"
    async def web_search(q): return f"Web: {q}"

    selector = CostAwareToolSelector([
        ToolSpec("local_kb",    0.0,    2.0,  0.6, "static",   local_kb),
        ToolSpec("cached",      0.001,  50.0, 0.8, "hours",    cached_search),
        ToolSpec("web_search",  0.01,  800.0, 1.0, "realtime", web_search),
    ], quality_threshold=0.8)

    # Uses cached ($0.001) instead of web ($0.01) for threshold=0.8
    result = await selector.call("What is the capital of France?")
    print(selector.cost_summary())
```

---

## Solution 2: Query Complexity Classifier for Tool Routing

```python
import asyncio
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

class QueryComplexity(Enum):
    TRIVIAL   = 1   # factual lookup, math, well-known fact
    SIMPLE    = 2   # recent events, common knowledge
    MODERATE  = 3   # multi-step reasoning, comparison
    COMPLEX   = 4   # research, synthesis, original analysis

TRIVIAL_PATTERNS = [
    r"what is \d+\s*[+\-*/]\s*\d+",         # arithmetic
    r"how many (days|hours|minutes) in",      # unit conversion
    r"capital of [a-zA-Z]+",                  # geography facts
    r"who (invented|discovered|wrote)",       # history facts
    r"what (year|date) was .+ (born|founded|invented)",
]

COMPLEX_INDICATORS = [
    "compare", "analyze", "evaluate", "synthesize", "research",
    "latest", "current", "recent", "today", "this week", "now",
    "predict", "forecast", "recommend", "which is better",
]

def classify_query(query: str) -> QueryComplexity:
    q = query.lower()
    for pattern in TRIVIAL_PATTERNS:
        if re.search(pattern, q, re.IGNORECASE):
            return QueryComplexity.TRIVIAL
    if any(indicator in q for indicator in COMPLEX_INDICATORS):
        return QueryComplexity.COMPLEX
    word_count = len(query.split())
    if word_count < 8:
        return QueryComplexity.SIMPLE
    if word_count < 20:
        return QueryComplexity.MODERATE
    return QueryComplexity.COMPLEX

@dataclass
class ToolRoute:
    complexity: QueryComplexity
    tool_name: str
    model: str
    max_cost_usd: float

ROUTING_TABLE: list[ToolRoute] = [
    ToolRoute(QueryComplexity.TRIVIAL,  "local_kb",   "claude-haiku-4-5-20251001", 0.001),
    ToolRoute(QueryComplexity.SIMPLE,   "cached_search", "claude-haiku-4-5-20251001", 0.005),
    ToolRoute(QueryComplexity.MODERATE, "web_search",    "claude-sonnet-4-6", 0.02),
    ToolRoute(QueryComplexity.COMPLEX,  "web_search",    "claude-opus-4-6", 0.10),
]

class ComplexityBasedRouter:
    def __init__(self, tool_registry: dict[str, Any],
                 budget_remaining: float = 10.0):
        self._tools = tool_registry
        self._budget = budget_remaining
        self._spent = 0.0

    async def route(self, query: str) -> tuple[str, str, float]:
        """Returns (tool_name, model_id, estimated_cost)."""
        complexity = classify_query(query)
        route = next(r for r in ROUTING_TABLE if r.complexity == complexity)

        if self._spent + route.max_cost_usd > self._budget:
            # Budget exhausted → fall back to free tier
            print(f"[CostRouter] Budget low (${self._budget - self._spent:.4f} left), "
                  f"using cheapest option")
            route = ROUTING_TABLE[0]

        self._spent += route.max_cost_usd
        return route.tool_name, route.model, route.max_cost_usd

    def budget_status(self) -> dict:
        return {
            "budget_usd": self._budget,
            "spent_usd": round(self._spent, 6),
            "remaining_usd": round(self._budget - self._spent, 6),
            "utilization_pct": round(self._spent / self._budget * 100, 1),
        }
```

---

## Solution 3: LLM-Scored Tool Fitness Before Calling

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Any

client = AsyncAnthropic()

@dataclass
class ToolCandidate:
    name: str
    description: str
    cost_usd: float
    fn: Any

SCORING_PROMPT = """Given the user query and tool description, score how well
this tool can answer the query on a scale 0-10 and estimate if it will
fully answer (yes/no). Respond with JSON only: {"score": N, "sufficient": true/false}"""

async def score_tool_fitness(query: str, tool: ToolCandidate) -> float:
    """Ask a cheap model to score how well this tool fits the query."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",  # cheap scoring model
        max_tokens=50,
        system=SCORING_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Query: {query}\n\nTool: {tool.name}\n{tool.description}"
        }]
    )
    try:
        result = json.loads(response.content[0].text)
        score = float(result.get("score", 0)) / 10.0
        sufficient = result.get("sufficient", False)
        return score if sufficient else score * 0.5
    except Exception:
        return 0.5  # default if scoring fails

async def cost_optimal_tool_selection(
    query: str,
    candidates: list[ToolCandidate],
    quality_min: float = 0.7,
    scoring_cost: float = 0.0001  # cost of Haiku scoring call
) -> ToolCandidate:
    """
    Score all tools with Haiku, then pick the cheapest tool
    that meets the quality threshold. Only run scoring if the
    total cost-with-scoring is less than using the default expensive tool.
    """
    # Sort cheapest first
    sorted_candidates = sorted(candidates, key=lambda t: t.cost_usd)

    # Quick check: if the cheapest tool is likely sufficient, skip scoring
    cheapest = sorted_candidates[0]
    most_expensive = sorted_candidates[-1]
    cost_savings = most_expensive.cost_usd - cheapest.cost_usd
    if cost_savings <= scoring_cost * len(candidates):
        # Scoring costs more than it saves — just use cheapest
        return cheapest

    # Score all candidates in parallel (cheap Haiku calls)
    scores = await asyncio.gather(*[
        score_tool_fitness(query, t) for t in sorted_candidates
    ])

    # Pick cheapest tool that meets quality threshold
    for tool, score in zip(sorted_candidates, scores):
        if score >= quality_min:
            print(f"[ToolFitness] Selected '{tool.name}' "
                  f"(score={score:.2f}, cost=${tool.cost_usd})")
            return tool

    # Fallback: best-scoring tool
    best_idx = max(range(len(scores)), key=lambda i: scores[i])
    return sorted_candidates[best_idx]
```

---

## Solution 4: Budget-Tracked Tool Execution with Billing

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class ToolCall:
    tool_name: str
    cost_usd: float
    latency_ms: float
    success: bool
    timestamp: float = field(default_factory=time.time)

class BudgetTracker:
    def __init__(self, session_budget_usd: float = 1.0,
                 per_call_limit_usd: float = 0.10):
        self._session_budget = session_budget_usd
        self._per_call_limit = per_call_limit_usd
        self._calls: list[ToolCall] = []
        self._total_spent: float = 0.0

    def can_afford(self, cost_usd: float) -> bool:
        return (cost_usd <= self._per_call_limit and
                self._total_spent + cost_usd <= self._session_budget)

    def record(self, call: ToolCall):
        self._calls.append(call)
        if call.success:
            self._total_spent += call.cost_usd

    @property
    def remaining(self) -> float:
        return max(0.0, self._session_budget - self._total_spent)

    def summary(self) -> dict:
        successful = [c for c in self._calls if c.success]
        return {
            "total_calls": len(self._calls),
            "successful_calls": len(successful),
            "total_spent_usd": round(self._total_spent, 6),
            "remaining_usd": round(self.remaining, 6),
            "by_tool": {
                name: {
                    "calls": sum(1 for c in successful if c.tool_name == name),
                    "cost": round(sum(c.cost_usd for c in successful
                                      if c.tool_name == name), 6)
                }
                for name in {c.tool_name for c in successful}
            }
        }

class BudgetConstrainedAgent:
    """Agent that respects a per-session budget and picks tools accordingly."""

    def __init__(self, tracker: BudgetTracker,
                 selector: "CostAwareToolSelector"):
        self._tracker = tracker
        self._selector = selector

    async def call_tool(self, tool_fn: Callable,
                         estimated_cost: float,
                         tool_name: str,
                         *args, **kwargs) -> Any | None:
        if not self._tracker.can_afford(estimated_cost):
            print(f"[Budget] Cannot afford {tool_name} "
                  f"(${estimated_cost:.4f}, remaining=${self._tracker.remaining:.4f})")
            return None

        start = time.monotonic()
        success = False
        try:
            result = await tool_fn(*args, **kwargs)
            success = True
            return result
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            self._tracker.record(ToolCall(
                tool_name=tool_name,
                cost_usd=estimated_cost if success else 0.0,
                latency_ms=latency_ms,
                success=success,
            ))
```

---

## Solution 5: Tiered Model Selection for Tool Result Processing

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Any

client = AsyncAnthropic()

@dataclass
class ProcessingTier:
    model: str
    max_tokens: int
    cost_per_mtok_input: float
    cost_per_mtok_output: float
    use_when: str  # description for logging

TIERS = [
    ProcessingTier(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        cost_per_mtok_input=0.80,
        cost_per_mtok_output=4.00,
        use_when="simple extraction, classification, yes/no"
    ),
    ProcessingTier(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        cost_per_mtok_input=3.00,
        cost_per_mtok_output=15.00,
        use_when="multi-step reasoning, moderate complexity"
    ),
    ProcessingTier(
        model="claude-opus-4-6",
        max_tokens=4096,
        cost_per_mtok_input=15.00,
        cost_per_mtok_output=75.00,
        use_when="complex research, synthesis, creative work"
    ),
]

def estimate_output_tokens(task: str) -> int:
    """Rough estimate of required output tokens based on task description."""
    task_lower = task.lower()
    if any(w in task_lower for w in ["yes/no", "classify", "extract", "is it"]):
        return 50
    if any(w in task_lower for w in ["summarize", "list", "enumerate"]):
        return 300
    if any(w in task_lower for w in ["explain", "describe", "analyze"]):
        return 800
    if any(w in task_lower for w in ["research", "compare", "evaluate", "write"]):
        return 2000
    return 500

def select_tier(task: str, context_tokens: int,
                budget_remaining_usd: float) -> ProcessingTier:
    """Pick the cheapest tier that can handle the task within budget."""
    output_tokens = estimate_output_tokens(task)

    for tier in TIERS:
        input_cost = context_tokens / 1_000_000 * tier.cost_per_mtok_input
        output_cost = output_tokens / 1_000_000 * tier.cost_per_mtok_output
        total_cost = input_cost + output_cost

        if total_cost > budget_remaining_usd:
            continue  # too expensive
        if output_tokens > tier.max_tokens:
            continue  # won't fit

        return tier

    return TIERS[-1]  # fallback: most capable

async def cost_aware_process(task: str, context: str,
                              budget_remaining: float = 0.10) -> str:
    """Process with the cheapest suitable model tier."""
    context_tokens = len(context) // 4
    tier = select_tier(task, context_tokens, budget_remaining)
    print(f"[TierSelect] Using {tier.model} ({tier.use_when})")

    response = await client.messages.create(
        model=tier.model,
        max_tokens=tier.max_tokens,
        messages=[{
            "role": "user",
            "content": f"Context:\n{context}\n\nTask: {task}"
        }]
    )
    return response.content[0].text
```

---

## Solution 6: Cost-Aware Caching — Skip Expensive Calls for Known Answers

```python
import asyncio
import hashlib
import json
import time
from typing import Any, Callable, Awaitable

class CostAwareCache:
    """
    Cache tool results keyed by (tool_name, args_hash).
    Before calling an expensive tool, check if the cache has a fresh answer.
    Track how much cost was avoided via cache hits.
    """

    def __init__(self, default_ttl: float = 3600.0):
        self._store: dict[str, tuple[Any, float, float]] = {}  # key → (value, expires_at, cost_avoided)
        self._default_ttl = default_ttl
        self._hits: int = 0
        self._misses: int = 0
        self._cost_avoided_usd: float = 0.0

    def _key(self, tool_name: str, args: tuple, kwargs: dict) -> str:
        canonical = json.dumps({"tool": tool_name, "args": args,
                                 "kwargs": kwargs}, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def get_or_call(self,
                           tool_name: str,
                           tool_fn: Callable[..., Awaitable[Any]],
                           tool_cost_usd: float,
                           *args,
                           ttl: float | None = None,
                           **kwargs) -> tuple[Any, bool]:
        """
        Returns (result, was_cached).
        Calls tool_fn on cache miss; returns cached value on hit.
        """
        key = self._key(tool_name, args, kwargs)
        entry = self._store.get(key)
        now = time.monotonic()

        if entry and entry[1] > now:
            self._hits += 1
            self._cost_avoided_usd += tool_cost_usd
            return entry[0], True

        # Cache miss — call the real tool
        result = await tool_fn(*args, **kwargs)
        expires_at = now + (ttl or self._default_ttl)
        self._store[key] = (result, expires_at, tool_cost_usd)
        self._misses += 1
        return result, False

    def evict_expired(self):
        now = time.monotonic()
        stale = [k for k, (_, exp, _) in self._store.items() if exp <= now]
        for k in stale:
            del self._store[k]

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(1, total), 3),
            "cost_avoided_usd": round(self._cost_avoided_usd, 6),
            "cached_entries": len(self._store),
        }

# Usage
async def demo():
    cache = CostAwareCache(default_ttl=300.0)

    async def web_search(query: str) -> str:
        return f"Search results for: {query}"

    # First call: cache miss → actual call ($0.01)
    result1, cached1 = await cache.get_or_call(
        "web_search", web_search, 0.01, "capital of France"
    )
    # Second call: cache hit → $0.01 avoided
    result2, cached2 = await cache.get_or_call(
        "web_search", web_search, 0.01, "capital of France"
    )
    print(f"Cached: {cached1}, {cached2}")
    print(cache.stats())  # cost_avoided_usd: 0.01
```

---

## Comparison

| Solution | Routing Logic | Budget Tracking | Caching | LLM Scoring | Best For |
|---|---|---|---|---|---|
| 1. Cost registry + fallback | Quality threshold | No | No | No | Simple tiered tool sets |
| 2. Query complexity classifier | Regex + heuristic | Yes | No | No | Well-defined query types |
| 3. LLM-scored fitness | Haiku scoring | No | No | Yes | Ambiguous query routing |
| 4. Budget tracker | Per-session budget | Yes | No | No | Hard budget constraints |
| 5. Model tier selection | Output size estimate | Partial | No | No | LLM processing step selection |
| 6. Cost-aware cache | N/A (cache-first) | Yes (avoided cost) | Yes | No | Repeated queries, deterministic tools |

**Key principle**: combine a cache (solution 6) with a complexity classifier (solution 2) as a free baseline — cache avoids tool calls entirely; classifier routes uncached queries to the cheapest sufficient tool. Add budget tracking (solution 4) to prevent runaway costs. Reserve LLM-based fitness scoring (solution 3) for cases where the routing decision is genuinely ambiguous and the scoring cost is justified by the savings.
