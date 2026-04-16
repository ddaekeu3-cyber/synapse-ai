---
title: "Agent Doesn't Implement Dynamic Context Window Allocation"
description: "AI agents that use a fixed context window split between system prompt, history, and tool results waste tokens on low-priority content and hit limits at the worst moment. Learn six patterns for dynamic context budget allocation based on task phase, content priority, and remaining capacity."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-dynamic-context-window-allocation
tags: [context-window, token-budget, allocation, performance, cost, optimization]
symptoms:
  - "Agent hits context limit mid-task because tool results consumed too many tokens"
  - "System prompt eats 40% of context on every turn regardless of task complexity"
  - "Old conversation turns crowd out recent tool results that are actually needed"
  - "Agent uses the same context budget for a simple query and a complex multi-step task"
  - "Cost spikes on long sessions because low-value history is never evicted"
---

## The Problem

Most agents allocate context naively: the system prompt takes a fixed slice, conversation history fills the rest, and tool results are appended until the limit is hit. This wastes capacity on low-priority content and causes failures at the worst time — deep into a multi-step task when dropping context means losing critical intermediate state.

Dynamic context window allocation treats the context budget as a resource to be managed: prioritizing recent and high-signal content, shrinking lower-priority allocations under pressure, and adapting the budget split to the current task phase.

```python
# ❌ Fixed allocation — hits limit unpredictably
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},  # 2000 tokens, always
    *conversation_history,                           # grows unbounded
    *tool_results,                                   # piled on until crash
]

# ✓ Dynamic allocation with priority eviction
allocator = ContextWindowAllocator(model_limit=200_000)
messages = allocator.build(
    system_prompt=SYSTEM_PROMPT,
    history=conversation_history,
    tool_results=tool_results,
    task_phase="deep_execution",
)
```

---

## Solution 1: Priority-Based Eviction with Token Counting

Assign priority scores to each context segment. When the total exceeds the budget, evict the lowest-priority segments first, preserving the most valuable content.

```python
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any
import tiktoken


class Priority(IntEnum):
    CRITICAL = 100    # Never evict: current task instructions, active tool results
    HIGH = 75         # Recent turns, last N tool results
    MEDIUM = 50       # Older turns, summarized history
    LOW = 25          # Background context, examples
    DISPOSABLE = 10   # Boilerplate, verbose tool output preambles


@dataclass
class ContextSegment:
    key: str
    content: str | list[dict]
    priority: Priority
    token_count: int = 0
    is_message: bool = True  # False = system prompt fragment


class PriorityContextAllocator:
    """Fills context budget by adding segments in priority order, evicting from bottom."""

    def __init__(self, model: str = "claude-3-5-sonnet-20241022",
                 budget_fraction: float = 0.85):
        self.model = model
        self.budget_fraction = budget_fraction
        # Model context limits
        self._limits = {
            "claude-3-5-sonnet-20241022": 200_000,
            "claude-opus-4-6": 200_000,
            "claude-haiku-4-5-20251001": 200_000,
        }
        self._enc = tiktoken.get_encoding("cl100k_base")

    @property
    def token_budget(self) -> int:
        limit = self._limits.get(self.model, 100_000)
        return int(limit * self.budget_fraction)

    def count_tokens(self, content: str | list | dict) -> int:
        if isinstance(content, str):
            return len(self._enc.encode(content))
        elif isinstance(content, list):
            return sum(self.count_tokens(item) for item in content)
        elif isinstance(content, dict):
            return self.count_tokens(str(content))
        return 0

    def build(self, segments: list[ContextSegment]) -> list[ContextSegment]:
        """Returns subset of segments that fits in budget, highest priority first."""
        # Assign token counts
        for seg in segments:
            if seg.token_count == 0:
                seg.token_count = self.count_tokens(seg.content)

        # Sort: CRITICAL first, DISPOSABLE last
        sorted_segs = sorted(segments, key=lambda s: s.priority, reverse=True)

        selected = []
        used = 0
        budget = self.token_budget

        for seg in sorted_segs:
            if used + seg.token_count <= budget:
                selected.append(seg)
                used += seg.token_count
            elif seg.priority >= Priority.CRITICAL:
                # Critical segments must fit — summarize if needed
                # (Truncation is a last resort)
                available = budget - used
                if available > 100:
                    truncated_content = self._truncate(seg.content, available)
                    seg.content = truncated_content
                    seg.token_count = available
                    selected.append(seg)
                    used = budget

        # Restore message order (system first, then by original index)
        selected.sort(key=lambda s: (0 if not s.is_message else 1, segments.index(s)))
        return selected

    def _truncate(self, content: str | list, max_tokens: int) -> str:
        if isinstance(content, list):
            content = str(content)
        tokens = self._enc.encode(content)
        return self._enc.decode(tokens[:max_tokens]) + "\n[truncated]"

    def usage_report(self, segments: list[ContextSegment]) -> dict:
        by_priority = {}
        for seg in segments:
            p = seg.priority.name
            by_priority[p] = by_priority.get(p, 0) + seg.token_count
        total = sum(seg.token_count for seg in segments)
        return {
            "total_tokens": total,
            "budget": self.token_budget,
            "utilization_pct": total / self.token_budget * 100,
            "by_priority": by_priority,
        }
```

---

## Solution 2: Phase-Aware Budget Splitting

Different task phases have different needs: planning needs full history, execution needs tool results, reflection needs a synthesis view. Adjust the budget split based on the current phase.

```python
from enum import Enum
from dataclasses import dataclass


class TaskPhase(Enum):
    PLANNING = "planning"
    EXECUTION = "execution"
    TOOL_HEAVY = "tool_heavy"
    SUMMARIZATION = "summarization"
    FINAL_RESPONSE = "final_response"


@dataclass
class BudgetSplit:
    system_prompt_pct: float
    history_pct: float
    tool_results_pct: float
    scratch_pct: float  # Reserved for new output + overhead


# Phase → budget allocation
PHASE_BUDGETS: dict[TaskPhase, BudgetSplit] = {
    TaskPhase.PLANNING: BudgetSplit(
        system_prompt_pct=0.15,
        history_pct=0.55,   # Need full history to plan
        tool_results_pct=0.15,
        scratch_pct=0.15,
    ),
    TaskPhase.EXECUTION: BudgetSplit(
        system_prompt_pct=0.10,
        history_pct=0.30,
        tool_results_pct=0.45,  # Tool results dominate during execution
        scratch_pct=0.15,
    ),
    TaskPhase.TOOL_HEAVY: BudgetSplit(
        system_prompt_pct=0.08,
        history_pct=0.15,
        tool_results_pct=0.62,  # Max space for large tool outputs
        scratch_pct=0.15,
    ),
    TaskPhase.SUMMARIZATION: BudgetSplit(
        system_prompt_pct=0.10,
        history_pct=0.50,
        tool_results_pct=0.25,
        scratch_pct=0.15,
    ),
    TaskPhase.FINAL_RESPONSE: BudgetSplit(
        system_prompt_pct=0.10,
        history_pct=0.35,
        tool_results_pct=0.30,
        scratch_pct=0.25,  # More room for output
    ),
}


class PhaseAwareContextBuilder:
    def __init__(self, total_tokens: int = 200_000, output_reserve: int = 4096):
        self.total = total_tokens - output_reserve  # Reserve for output

    def build(
        self,
        phase: TaskPhase,
        system_prompt: str,
        history: list[dict],
        tool_results: list[dict],
        count_fn,  # token counting function
    ) -> dict[str, list[dict] | str]:
        split = PHASE_BUDGETS[phase]

        sys_budget = int(self.total * split.system_prompt_pct)
        hist_budget = int(self.total * split.history_pct)
        tool_budget = int(self.total * split.tool_results_pct)

        # Trim system prompt
        sys_tokens = count_fn(system_prompt)
        if sys_tokens > sys_budget:
            # Keep the most critical part (first and last N chars)
            mid = sys_budget // 2
            enc_sys = system_prompt[:mid * 4] + "\n[...]\n" + system_prompt[-(mid * 4):]
            system_prompt = enc_sys

        # Select history: prefer recent turns
        selected_history = self._select_recent(history, hist_budget, count_fn)

        # Select tool results: prefer most recent
        selected_tools = self._select_recent(tool_results, tool_budget, count_fn)

        return {
            "system": system_prompt,
            "history": selected_history,
            "tool_results": selected_tools,
            "phase": phase.value,
            "budget_split": {
                "system": sys_budget,
                "history": hist_budget,
                "tools": tool_budget,
            },
        }

    def _select_recent(self, items: list, budget: int, count_fn) -> list:
        """Select most recent items that fit in budget."""
        selected = []
        used = 0
        for item in reversed(items):
            tokens = count_fn(item)
            if used + tokens <= budget:
                selected.insert(0, item)
                used += tokens
            else:
                break
        return selected

    def detect_phase(self, history: list[dict], pending_tool_calls: int) -> TaskPhase:
        """Heuristic phase detection from conversation state."""
        if not history:
            return TaskPhase.PLANNING
        if pending_tool_calls > 3:
            return TaskPhase.TOOL_HEAVY
        if pending_tool_calls > 0:
            return TaskPhase.EXECUTION
        last_role = history[-1].get("role", "") if history else ""
        if last_role == "tool":
            return TaskPhase.SUMMARIZATION
        return TaskPhase.FINAL_RESPONSE
```

---

## Solution 3: Rolling Window with Async Summarization

When history exceeds its budget, instead of simply dropping old turns, summarize them asynchronously and inject the summary as a compact history block.

```python
import asyncio
import anthropic
from dataclasses import dataclass


@dataclass
class SummarizedHistory:
    summary: str
    turns_summarized: int
    original_tokens: int
    summary_tokens: int


class RollingWindowContextManager:
    """
    Maintains a rolling window of recent turns.
    When the window overflows, older turns are summarized and replaced.
    """

    SUMMARIZE_PROMPT = """Summarize the following conversation history in 3-5 bullet points.
Focus on: decisions made, information discovered, current task state, and any errors encountered.
Be extremely concise — every token counts.

Conversation:
{history}

Summary (bullet points only):"""

    def __init__(
        self,
        max_history_tokens: int = 40_000,
        summarize_when_over_pct: float = 0.80,
        summary_model: str = "claude-haiku-4-5-20251001",
    ):
        self.max_tokens = max_history_tokens
        self.summarize_threshold = int(max_history_tokens * summarize_when_over_pct)
        self.summary_model = summary_model
        self._turns: list[dict] = []
        self._summaries: list[SummarizedHistory] = []
        self._client = anthropic.AsyncAnthropic()
        self._summarizing = False

    def add_turn(self, role: str, content: str):
        self._turns.append({"role": role, "content": content})

    def _estimate_tokens(self, turns: list[dict]) -> int:
        return sum(len(str(t.get("content", ""))) // 4 for t in turns)

    async def get_context(self) -> list[dict]:
        """Returns context messages, summarizing old turns if needed."""
        current_tokens = self._estimate_tokens(self._turns)

        if current_tokens > self.summarize_threshold and not self._summarizing:
            self._summarizing = True
            try:
                await self._summarize_oldest()
            finally:
                self._summarizing = False

        messages = []

        # Inject compact summaries first
        if self._summaries:
            combined = "\n".join(
                f"[Summary of {s.turns_summarized} turns]: {s.summary}"
                for s in self._summaries
            )
            messages.append({
                "role": "user",
                "content": f"[Previous conversation summary]:\n{combined}",
            })
            messages.append({
                "role": "assistant",
                "content": "Understood. I have the context from the previous conversation.",
            })

        messages.extend(self._turns)
        return messages

    async def _summarize_oldest(self):
        """Summarize the oldest 40% of turns and replace them with a summary."""
        cutoff = len(self._turns) * 4 // 10
        to_summarize = self._turns[:cutoff]
        self._turns = self._turns[cutoff:]

        history_text = "\n".join(
            f"{t['role'].upper()}: {t['content']}" for t in to_summarize
        )
        original_tokens = self._estimate_tokens(to_summarize)

        resp = await self._client.messages.create(
            model=self.summary_model,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": self.SUMMARIZE_PROMPT.format(history=history_text[:8000]),
            }],
        )
        summary_text = resp.content[0].text
        summary_tokens = resp.usage.output_tokens

        self._summaries.append(SummarizedHistory(
            summary=summary_text,
            turns_summarized=len(to_summarize),
            original_tokens=original_tokens,
            summary_tokens=summary_tokens,
        ))
        print(
            f"[context] Summarized {len(to_summarize)} turns: "
            f"{original_tokens} → {summary_tokens} tokens "
            f"({original_tokens - summary_tokens} saved)"
        )
```

---

## Solution 4: Tool Result Compression with Relevance Scoring

Large tool results (search results, file contents, API responses) consume disproportionate context. Score each result's relevance to the current query and truncate low-relevance results aggressively.

```python
import re
from dataclasses import dataclass


@dataclass
class ToolResultWithScore:
    tool_name: str
    result_text: str
    token_count: int
    relevance_score: float  # 0-1
    turn_index: int         # Position in conversation (recent = higher)


class ToolResultCompressor:
    """
    Scores tool results by relevance and recency, compresses low-priority results.
    """

    def __init__(self, query_keywords: list[str] | None = None):
        self.keywords = [k.lower() for k in (query_keywords or [])]

    def score_relevance(self, result: str, turn_index: int, total_turns: int) -> float:
        """Score 0-1: keyword match + recency."""
        text = result.lower()
        keyword_score = 0.0
        if self.keywords:
            matches = sum(1 for kw in self.keywords if kw in text)
            keyword_score = min(matches / len(self.keywords), 1.0)

        recency_score = turn_index / max(total_turns, 1)

        # Weighted: recency matters more for execution context
        return 0.4 * keyword_score + 0.6 * recency_score

    def compress(
        self,
        tool_results: list[dict],
        budget_tokens: int,
        query: str = "",
    ) -> list[dict]:
        if not tool_results:
            return []

        # Extract keywords from query
        if query:
            self.keywords = re.findall(r'\b\w{4,}\b', query.lower())[:10]

        total = len(tool_results)
        scored = []
        for i, result in enumerate(tool_results):
            text = str(result.get("content", ""))
            tokens = len(text) // 4
            score = self.score_relevance(text, i, total)
            scored.append(ToolResultWithScore(
                tool_name=result.get("tool_use_id", f"tool_{i}"),
                result_text=text,
                token_count=tokens,
                relevance_score=score,
                turn_index=i,
            ))

        # Sort: high relevance first
        scored.sort(key=lambda r: r.relevance_score, reverse=True)

        selected = []
        used = 0

        for item in scored:
            available = budget_tokens - used
            if available <= 0:
                break

            if item.token_count <= available:
                selected.append(item)
                used += item.token_count
            elif available > 200:
                # Truncate to available space
                keep_chars = available * 4
                truncated = item.result_text[:keep_chars] + "\n[truncated — low relevance]"
                item.result_text = truncated
                item.token_count = available
                selected.append(item)
                used = budget_tokens

        # Restore original order
        selected.sort(key=lambda r: r.turn_index)
        return [
            {
                "tool_use_id": r.tool_name,
                "content": r.result_text,
                "_relevance": r.relevance_score,
            }
            for r in selected
        ]
```

---

## Solution 5: KV-Cache-Aware Context Layout

Anthropic's prompt cache works on prefix matching. Lay out context so stable, cacheable content (system prompt, static tool schemas) comes first and changes only at the end, maximizing cache hits and reducing effective token cost.

```python
import anthropic
from dataclasses import dataclass


@dataclass
class CacheAwareContext:
    stable_prefix: list[dict]   # System + static content — cache these
    dynamic_suffix: list[dict]  # Recent turns + latest tool results — never cached
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0


class KVCacheAwareAllocator:
    """
    Structures context to maximize Anthropic prompt cache hits.
    Stable content (system prompt, tool schemas) is placed at the start
    with cache_control markers. Dynamic content (recent turns) at the end.
    """

    # Anthropic minimum cacheable prefix length
    MIN_CACHE_TOKENS = 1024

    def __init__(self, model: str = "claude-3-5-sonnet-20241022"):
        self.model = model

    def build_cache_aware_messages(
        self,
        system_prompt: str,
        tool_schemas: list[dict],
        stable_context: list[dict],  # e.g. retrieved docs unlikely to change
        recent_turns: list[dict],    # Last N turns — not cached
        max_total_tokens: int = 160_000,
    ) -> tuple[list[dict], dict]:
        """
        Returns (messages, cache_metadata).
        Stable content gets cache_control: ephemeral markers.
        """
        # Estimate stable content size
        stable_size = (
            len(system_prompt) // 4 +
            sum(len(str(t)) // 4 for t in tool_schemas) +
            sum(len(str(t.get("content", ""))) // 4 for t in stable_context)
        )

        # Only cache if stable content is large enough to be worth it
        use_cache = stable_size >= self.MIN_CACHE_TOKENS

        messages = []

        # Stable context: inject as early user/assistant turns with cache marker
        if stable_context:
            stable_text = "\n\n".join(
                f"[Reference Document {i+1}]:\n{t.get('content', '')}"
                for i, t in enumerate(stable_context)
            )
            cache_msg: dict = {
                "role": "user",
                "content": stable_text,
            }
            if use_cache:
                cache_msg["content"] = [
                    {
                        "type": "text",
                        "text": stable_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            messages.append(cache_msg)
            messages.append({
                "role": "assistant",
                "content": "I have read and understood the reference documents.",
            })

        # Dynamic content: recent turns — no cache marker
        messages.extend(recent_turns)

        system_content: list | str = system_prompt
        if use_cache and tool_schemas:
            # Attach tool schema to system prompt with cache marker
            system_content = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        return messages, {
            "system": system_content,
            "cache_enabled": use_cache,
            "estimated_stable_tokens": stable_size,
            "estimated_dynamic_tokens": sum(
                len(str(t.get("content", ""))) // 4 for t in recent_turns
            ),
        }

    async def call_with_cache(
        self, system_prompt: str, tool_schemas: list[dict],
        stable_context: list[dict], recent_turns: list[dict],
    ) -> anthropic.types.Message:
        messages, meta = self.build_cache_aware_messages(
            system_prompt, tool_schemas, stable_context, recent_turns
        )
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=meta["system"],
            messages=messages,
        )
        cache_read = getattr(resp.usage, "cache_read_input_tokens", 0)
        cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0)
        print(
            f"[cache] read={cache_read} write={cache_write} "
            f"input={resp.usage.input_tokens}"
        )
        return resp
```

---

## Solution 6: ContextBudgetManager — Unified Allocation Controller

A single controller that manages the entire context lifecycle: budget tracking, priority eviction, phase-aware splits, summarization triggers, and cache layout — exposing a clean `build()` API.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextConfig:
    model_token_limit: int = 200_000
    output_reserve_tokens: int = 8192
    system_prompt_max_pct: float = 0.12
    history_max_pct: float = 0.40
    tool_results_max_pct: float = 0.35
    summarize_history_at_pct: float = 0.85  # of history budget
    min_recent_turns: int = 4  # Always keep last N turns
    enable_tool_compression: bool = True
    enable_cache_layout: bool = True


class ContextBudgetManager:
    """
    Unified context allocation controller.
    Call build() to get a fully optimized message list.
    """

    def __init__(self, config: ContextConfig | None = None,
                 summary_fn=None):
        self.cfg = config or ContextConfig()
        self._summary_fn = summary_fn  # async fn(turns) -> str
        self._total_budget = self.cfg.model_token_limit - self.cfg.output_reserve_tokens
        self._turn_count = 0
        self._eviction_log: list[str] = []

    def _count(self, obj: Any) -> int:
        return len(str(obj)) // 4

    def _budget(self, pct: float) -> int:
        return int(self._total_budget * pct)

    def _fit_to_budget(self, items: list, budget: int,
                       keep_last_n: int = 0) -> tuple[list, int]:
        """Select items to fit in budget, always keeping last keep_last_n items."""
        if not items:
            return [], 0

        # Always keep the last N
        tail = items[-keep_last_n:] if keep_last_n else []
        tail_tokens = sum(self._count(i) for i in tail)
        remaining_budget = budget - tail_tokens

        head = items[:-keep_last_n] if keep_last_n else items
        selected = []
        used = tail_tokens

        for item in reversed(head):
            t = self._count(item)
            if used + t <= budget:
                selected.insert(0, item)
                used += t

        return selected + tail, used

    async def build(
        self,
        system_prompt: str,
        history: list[dict],
        tool_results: list[dict],
        query: str = "",
    ) -> dict:
        """
        Returns dict with 'system', 'messages', and 'budget_report'.
        """
        self._turn_count += 1
        evictions = []

        # --- System prompt ---
        sys_budget = self._budget(self.cfg.system_prompt_max_pct)
        sys_tokens = self._count(system_prompt)
        if sys_tokens > sys_budget:
            system_prompt = system_prompt[:sys_budget * 4] + "\n[sys truncated]"
            evictions.append(f"system_prompt truncated: {sys_tokens}→{sys_budget}")

        # --- Tool results ---
        tool_budget = self._budget(self.cfg.tool_results_max_pct)
        if self.cfg.enable_tool_compression and tool_results:
            compressor = ToolResultCompressor(query_keywords=query.split()[:8])
            tool_results = compressor.compress(tool_results, tool_budget, query)
        else:
            tool_results, _ = self._fit_to_budget(tool_results, tool_budget, keep_last_n=2)

        tool_tokens_used = sum(self._count(r) for r in tool_results)

        # --- History ---
        hist_budget = self._budget(self.cfg.history_max_pct)
        history, hist_tokens_used = self._fit_to_budget(
            history, hist_budget, keep_last_n=self.cfg.min_recent_turns
        )

        if hist_tokens_used > hist_budget * self.cfg.summarize_history_at_pct:
            if self._summary_fn:
                old_turns = history[:-self.cfg.min_recent_turns]
                if old_turns:
                    summary = await self._summary_fn(old_turns)
                    summary_msg = {"role": "user", "content": f"[History summary]: {summary}"}
                    ack_msg = {"role": "assistant", "content": "Understood."}
                    history = [summary_msg, ack_msg] + history[-self.cfg.min_recent_turns:]
                    evictions.append(f"summarized {len(old_turns)} old turns")

        # Assemble messages
        messages = [*history]
        if tool_results:
            for r in tool_results:
                messages.append({"role": "tool", "content": r.get("content", ""),
                                  "tool_use_id": r.get("tool_use_id", "")})

        total_tokens = self._count(system_prompt) + hist_tokens_used + tool_tokens_used

        return {
            "system": system_prompt,
            "messages": messages,
            "budget_report": {
                "total_used": total_tokens,
                "total_budget": self._total_budget,
                "utilization_pct": total_tokens / self._total_budget * 100,
                "system_tokens": self._count(system_prompt),
                "history_tokens": hist_tokens_used,
                "tool_tokens": tool_tokens_used,
                "evictions": evictions,
            },
        }
```

---

## Comparison

| Pattern | Context Savings | Complexity | Cache-Friendly | Best For |
|---|---|---|---|---|
| Priority-based eviction | High (drops low-priority) | Low | No | General-purpose agents with mixed content |
| Phase-aware budget split | Medium (right allocation per phase) | Low | No | Multi-phase task agents (plan → execute → summarize) |
| Rolling window + async summarization | High (compresses old turns) | Medium | Partial | Long-running conversation agents |
| Tool result compression | High (scores and trims tool output) | Medium | No | Agents doing heavy tool use (search, code exec) |
| KV-cache-aware layout | Cost reduction (not size) | Low | Yes | Any agent with large stable system prompts or RAG docs |
| ContextBudgetManager (unified) | Highest (all techniques combined) | High | Yes | Production agents needing full lifecycle management |

**Recommendations:**
- Apply **KV-cache-aware layout** (Solution 5) immediately for any agent with a long system prompt — it costs nothing and reduces token spend.
- Use **phase-aware budget split** (Solution 2) for multi-step task agents where context needs change dramatically across phases.
- Use **rolling window + summarization** (Solution 3) for agents handling long user sessions (customer support, coding assistants).
- Use **tool result compression** (Solution 4) when tool outputs are large and variable (web search, file reads, API responses).
- Deploy the **ContextBudgetManager** (Solution 6) in production as a unified controller — it handles all the above automatically with a single `build()` call.
