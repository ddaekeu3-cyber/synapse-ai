---
title: "Agent Doesn't Implement Incremental Context Building"
description: "Agents that rebuild the full context window from scratch on every turn pay O(n) token costs even when only a small delta has changed, making long conversations unnecessarily expensive and slow."
difficulty: intermediate
category: performance
tags: [context, incremental, tokens, conversation, kv-cache, efficiency, performance]
---

## Problem

On every turn, most agents serialize the entire conversation history, all tool results, and the full system prompt into a new request. For a 10-turn conversation with rich tool outputs, this means 90% of the tokens in turn 10 are identical to turn 9. This wastes money on redundant input tokens and wastes latency on re-processing content the model has effectively already seen.

```python
# Broken: full context rebuilt every turn — O(n²) total token cost
async def chat(history: list[dict], new_message: str) -> str:
    messages = history + [{"role": "user", "content": new_message}]
    # Every turn sends ALL prior turns again from scratch
    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,   # re-sent every time
        messages=messages       # grows linearly each turn
    )
    return response.content[0].text
```

---

## Solution 1: Stable System Prompt Caching (Anthropic Prompt Cache)

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Mark the static system prompt for caching — it's never reprocessed
CACHED_SYSTEM_PROMPT = [
    {
        "type": "text",
        "text": """You are a helpful AI assistant with access to the following tools:
[Long tool documentation, persona, rules — ~2000 tokens]
...
""",
        "cache_control": {"type": "ephemeral"}  # Anthropic prompt cache marker
    }
]

async def chat_with_cached_system(history: list[dict],
                                   new_message: str) -> str:
    """
    System prompt is processed once and cached.
    Subsequent turns only pay for the delta (new messages).
    Cache TTL: 5 minutes on Anthropic's infrastructure.
    """
    messages = history + [{"role": "user", "content": new_message}]
    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=CACHED_SYSTEM_PROMPT,
        messages=messages,
    )
    # Check cache utilization in usage
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_create = getattr(usage, "cache_creation_input_tokens", 0)
    print(f"[Cache] Read={cache_read} Create={cache_create} "
          f"Input={usage.input_tokens} Output={usage.output_tokens}")
    return response.content[0].text

# Mark large tool results for caching too (for repeated retrieval patterns)
def make_cached_tool_result(tool_use_id: str, content: str) -> dict:
    """Wrap a large tool result with cache_control so it's only processed once."""
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"}
                    }
                ]
            }
        ]
    }
```

---

## Solution 2: Rolling Summary — Replace Old Turns with Compressed Summary

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

SUMMARIZE_SYSTEM = """Summarize the conversation so far as a compact, information-dense
paragraph. Preserve: decisions made, facts established, tool outputs referenced,
current task state. Omit: pleasantries, restated questions, redundant phrasing."""

async def summarize_turns(turns: list[dict]) -> str:
    """Compress old turns into a summary paragraph."""
    history_text = "\n".join(
        f"{t['role'].upper()}: {_extract_text(t['content'])}"
        for t in turns
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",  # cheap for summarization
        max_tokens=400,
        system=SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": history_text}]
    )
    return response.content[0].text

def _extract_text(content) -> str:
    if isinstance(content, str):
        return content[:500]
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
        return " ".join(parts)[:500]
    return str(content)[:500]

class RollingContextManager:
    """
    Keeps the last `live_window` turns verbatim; compresses older turns
    into a running summary injected at the top of context.
    """

    def __init__(self, live_window: int = 6, max_summary_tokens: int = 500):
        self._summary: str = ""
        self._live_turns: list[dict] = []
        self._live_window = live_window
        self._max_summary_tokens = max_summary_tokens

    async def add_turn(self, role: str, content: str):
        self._live_turns.append({"role": role, "content": content})
        if len(self._live_turns) > self._live_window:
            # Compress the oldest turns into the summary
            to_compress = self._live_turns[:-self._live_window]
            self._live_turns = self._live_turns[-self._live_window:]
            new_summary = await summarize_turns(to_compress)
            if self._summary:
                # Merge old and new summary (cheap, short)
                self._summary = await self._merge_summaries(
                    self._summary, new_summary
                )
            else:
                self._summary = new_summary

    async def _merge_summaries(self, old: str, new: str) -> str:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=self._max_summary_tokens,
            messages=[{"role": "user",
                       "content": f"Merge these two summaries into one:\n\n"
                                  f"EARLIER:\n{old}\n\nRECENT:\n{new}"}]
        )
        return response.content[0].text

    def build_messages(self, new_user_message: str) -> list[dict]:
        """Construct the minimal message list for the next API call."""
        messages: list[dict] = []
        if self._summary:
            # Inject summary as a synthetic assistant turn
            messages.append({
                "role": "user",
                "content": "[Previous conversation summary follows]"
            })
            messages.append({
                "role": "assistant",
                "content": self._summary
            })
        messages.extend(self._live_turns)
        messages.append({"role": "user", "content": new_user_message})
        return messages

    def estimated_tokens(self) -> int:
        """Rough token estimate for current context."""
        summary_tokens = len(self._summary) // 4
        live_tokens = sum(len(_extract_text(t["content"])) // 4
                          for t in self._live_turns)
        return summary_tokens + live_tokens
```

---

## Solution 3: Delta-Only Context Update

```python
import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ContextBlock:
    """A reusable context block with content hash for change detection."""
    block_id: str
    content: str
    hash: str = ""
    version: int = 0

    def __post_init__(self):
        if not self.hash:
            self.hash = hashlib.sha256(self.content.encode()).hexdigest()[:16]

    def update(self, new_content: str) -> bool:
        """Returns True if content actually changed."""
        new_hash = hashlib.sha256(new_content.encode()).hexdigest()[:16]
        if new_hash == self.hash:
            return False
        self.content = new_content
        self.hash = new_hash
        self.version += 1
        return True

class IncrementalContextBuilder:
    """
    Tracks which context blocks have changed since the last API call.
    Provides the minimal message list needed for each turn.
    """

    def __init__(self):
        self._blocks: dict[str, ContextBlock] = {}
        self._message_history: list[dict] = []
        self._last_history_hash: str = ""

    def set_block(self, block_id: str, content: str) -> bool:
        """Update a named context block. Returns True if changed."""
        if block_id in self._blocks:
            return self._blocks[block_id].update(content)
        self._blocks[block_id] = ContextBlock(block_id=block_id, content=content)
        return True

    def build_system_prompt(self, block_ids: list[str]) -> str:
        """Assemble system prompt from named blocks in order."""
        return "\n\n".join(
            self._blocks[bid].content
            for bid in block_ids
            if bid in self._blocks
        )

    def add_message(self, role: str, content: str):
        self._message_history.append({"role": role, "content": content})

    def messages_since(self, last_count: int) -> list[dict]:
        """Return only messages added since the last N we included."""
        return self._message_history[-last_count:]

    def trim_history(self, max_messages: int, keep_last: int):
        """Keep only the last `keep_last` messages, drop middle."""
        if len(self._message_history) <= max_messages:
            return
        self._message_history = self._message_history[-keep_last:]

    def build_minimal_context(self,
                               include_blocks: list[str],
                               tail_messages: int = 10) -> tuple[str, list[dict]]:
        """
        Returns (system_prompt, messages) for the next API call.
        Only includes recent message history, not the full conversation.
        """
        system = self.build_system_prompt(include_blocks)
        messages = self._message_history[-tail_messages:]
        return system, messages

# Usage: agent that updates tool documentation incrementally
async def incremental_agent_demo():
    builder = IncrementalContextBuilder()

    # Set context blocks once
    builder.set_block("persona", "You are a helpful coding assistant.")
    builder.set_block("tools", "Available tools: web_search, code_exec, file_read")

    # Only update the block that changes
    builder.set_block("user_context", "User is working on a Python FastAPI project.")
    # Later: update only what changed
    changed = builder.set_block("user_context", "User switched to a Go project.")
    print(f"Context changed: {changed}")  # True

    builder.add_message("user", "How do I handle middleware in my framework?")
    system, messages = builder.build_minimal_context(
        include_blocks=["persona", "tools", "user_context"],
        tail_messages=6
    )
    return system, messages
```

---

## Solution 4: KV-Cache-Aware Prompt Structuring

```python
"""
Anthropic's KV cache stores the processed representation of a prefix.
To maximize cache hits, the STABLE parts of the prompt must come FIRST
and the CHANGING parts must come LAST.

Cache-friendly structure:
  [System: static persona + tool docs]  ← cached after first call
  [User: background context]            ← cached if unchanged
  [User: tool results from this session] ← partially cached
  [User: latest message]                ← always new
"""

from dataclasses import dataclass, field
from typing import Any

@dataclass
class CacheAwareMessage:
    role: str
    content_blocks: list[dict]

    @classmethod
    def static(cls, role: str, text: str) -> "CacheAwareMessage":
        """Mark content as static — eligible for caching."""
        return cls(role=role, content_blocks=[{
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"}
        }])

    @classmethod
    def dynamic(cls, role: str, text: str) -> "CacheAwareMessage":
        """Dynamic content — not cached."""
        return cls(role=role, content_blocks=[{"type": "text", "text": text}])

    def to_api_dict(self) -> dict:
        return {"role": self.role, "content": self.content_blocks}

class KVCacheAwareContext:
    """
    Builds messages in cache-optimal order:
    1. Static system content (persona, tool docs)
    2. Per-session static content (background docs, user profile)
    3. Dynamic tool results from this turn
    4. New user message
    """

    def __init__(self, static_system: str):
        self._static_system = static_system
        self._session_statics: list[CacheAwareMessage] = []
        self._dynamic_messages: list[CacheAwareMessage] = []

    def add_session_static(self, role: str, text: str):
        """Add content that's stable for the session (e.g., retrieved documents)."""
        self._session_statics.append(CacheAwareMessage.static(role, text))

    def add_dynamic(self, role: str, text: str):
        """Add new dynamic content (latest user message, current tool result)."""
        self._dynamic_messages.append(CacheAwareMessage.dynamic(role, text))

    def build(self) -> tuple[list[dict], list[dict]]:
        """
        Returns (system_blocks, messages_list).
        System is marked for caching; messages maintain cache-friendly ordering.
        """
        system = [{
            "type": "text",
            "text": self._static_system,
            "cache_control": {"type": "ephemeral"}
        }]
        messages = (
            [m.to_api_dict() for m in self._session_statics] +
            [m.to_api_dict() for m in self._dynamic_messages]
        )
        return system, messages

    def rotate_turn(self):
        """Move dynamic messages to end of session statics after each turn."""
        # The most recent turn's content is now "history" — could be cached
        for msg in self._dynamic_messages:
            self._session_statics.append(
                CacheAwareMessage.static(msg.role,
                    msg.content_blocks[0].get("text", ""))
            )
        self._dynamic_messages.clear()

    def estimated_cache_savings(self,
                                 input_cost_per_mtok: float = 3.0,
                                 cache_read_cost_per_mtok: float = 0.30) -> dict:
        """Estimate savings from caching vs re-processing."""
        static_tokens = sum(
            len(m.content_blocks[0].get("text", "")) // 4
            for m in self._session_statics
        )
        full_cost = static_tokens / 1_000_000 * input_cost_per_mtok
        cached_cost = static_tokens / 1_000_000 * cache_read_cost_per_mtok
        return {
            "static_tokens": static_tokens,
            "full_cost_usd": round(full_cost, 6),
            "cached_cost_usd": round(cached_cost, 6),
            "savings_usd": round(full_cost - cached_cost, 6),
            "savings_pct": round((1 - cache_read_cost_per_mtok / input_cost_per_mtok) * 100, 1),
        }
```

---

## Solution 5: Lazy Tool Result Injection

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class LazyToolResult:
    """A tool result that's only injected into context when referenced."""
    tool_use_id: str
    tool_name: str
    raw_result: Any
    _summarized: str | None = None

    async def get_for_context(self,
                               summarize_fn: Callable[[str, Any], Awaitable[str]],
                               token_budget: int = 500) -> str:
        """
        Return a context-appropriate representation.
        Summarize large results to fit within token budget.
        """
        raw_str = str(self.raw_result)
        approx_tokens = len(raw_str) // 4
        if approx_tokens <= token_budget:
            return raw_str
        if self._summarized is None:
            self._summarized = await summarize_fn(self.tool_name, self.raw_result)
        return self._summarized

class LazyContextAssembler:
    """
    Only injects tool results into context when they're needed.
    Old tool results from many turns ago are summarized or dropped.
    """

    def __init__(self, max_tool_turns_in_context: int = 3):
        self._recent_tools: list[LazyToolResult] = []
        self._max_tool_turns = max_tool_turns_in_context

    def record_tool_result(self, tool_use_id: str,
                            tool_name: str, result: Any):
        self._recent_tools.append(
            LazyToolResult(tool_use_id=tool_use_id,
                           tool_name=tool_name, raw_result=result)
        )
        # Keep only the last N tool results in context
        if len(self._recent_tools) > self._max_tool_turns:
            self._recent_tools.pop(0)

    async def build_tool_context_block(
        self,
        summarize_fn: Callable[[str, Any], Awaitable[str]],
        per_result_budget: int = 300
    ) -> str:
        """Build a compact tool-results block for injection into context."""
        parts = []
        for r in self._recent_tools:
            summary = await r.get_for_context(summarize_fn, per_result_budget)
            parts.append(f"[{r.tool_name}]: {summary}")
        return "\n\n".join(parts)

    def estimate_token_savings(self, avg_result_size_tokens: int = 1000) -> dict:
        full = len(self._recent_tools) * avg_result_size_tokens
        lazy = len(self._recent_tools) * 300  # post-summarization
        return {
            "full_tokens": full,
            "lazy_tokens": lazy,
            "savings_pct": round((1 - lazy / max(full, 1)) * 100, 1),
        }
```

---

## Solution 6: Context Budget Manager with Eviction Policy

```python
import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

class Priority(IntEnum):
    CRITICAL = 4   # system prompt, current task — never evicted
    HIGH     = 3   # recent turns, active tool results
    MEDIUM   = 2   # background context, session info
    LOW      = 1   # old turns, cached search results

@dataclass
class ContextItem:
    content: str
    priority: Priority
    tokens: int = 0
    turn_added: int = 0

    def __post_init__(self):
        if not self.tokens:
            self.tokens = max(1, len(self.content) // 4)

class ContextBudgetManager:
    """
    Maintains a fixed token budget for the context window.
    When budget is exceeded, evicts lowest-priority items first.
    """

    def __init__(self, max_tokens: int = 100_000,
                 reserve_for_output: int = 4096):
        self._max_tokens = max_tokens - reserve_for_output
        self._items: list[ContextItem] = []
        self._current_tokens = 0
        self._turn = 0

    def add(self, content: str, priority: Priority) -> bool:
        """Add item to context. Returns False if it couldn't fit even after eviction."""
        item = ContextItem(content=content, priority=priority, turn_added=self._turn)
        needed = item.tokens

        if needed > self._max_tokens:
            return False  # item is too large even alone

        # Evict until we have space
        while self._current_tokens + needed > self._max_tokens:
            if not self._evict_lowest():
                return False  # nothing left to evict

        self._items.append(item)
        self._current_tokens += item.tokens
        return True

    def _evict_lowest(self) -> bool:
        """Evict the lowest-priority, oldest item."""
        if not self._items:
            return False
        # Sort by (priority ASC, turn_added ASC) — evict oldest low-priority first
        candidate = min(self._items,
                        key=lambda x: (x.priority, x.turn_added))
        self._items.remove(candidate)
        self._current_tokens -= candidate.tokens
        return True

    def advance_turn(self):
        self._turn += 1
        # Demote OLD high-priority items to medium after 5 turns
        for item in self._items:
            age = self._turn - item.turn_added
            if item.priority == Priority.HIGH and age > 5:
                freed = item.tokens
                self._current_tokens -= freed
                item.priority = Priority.MEDIUM
                self._current_tokens += item.tokens  # tokens unchanged but priority drops

    def build_context(self) -> str:
        """Assemble context items sorted by priority (highest first)."""
        sorted_items = sorted(self._items,
                               key=lambda x: (-x.priority, x.turn_added))
        return "\n\n".join(item.content for item in sorted_items)

    def utilization(self) -> dict:
        return {
            "used_tokens": self._current_tokens,
            "max_tokens": self._max_tokens,
            "utilization_pct": round(self._current_tokens / self._max_tokens * 100, 1),
            "item_count": len(self._items),
            "by_priority": {
                p.name: sum(1 for i in self._items if i.priority == p)
                for p in Priority
            }
        }
```

---

## Comparison

| Solution | Token Savings | Latency Impact | Implementation Cost | Stale Risk | Best For |
|---|---|---|---|---|---|
| 1. Prompt cache markers | 50–90% on system prompt | None | Low | None (server-side cache) | Static system prompts |
| 2. Rolling summary | 30–70% on history | +1 summarization call | Med | Low (Haiku summarizes) | Long conversations |
| 3. Delta tracking | 10–40% on context blocks | None | Low | Low (hash-gated) | Dynamic configuration blocks |
| 4. KV-cache-aware ordering | 50–90% on static prefix | None | Low | None | Any conversation |
| 5. Lazy tool results | 40–70% on tool outputs | +1 summarization call | Low | None | Rich tool-heavy agents |
| 6. Budget manager + eviction | Up to 80% | None | Med | Low (explicit priority) | Context-window-constrained agents |

**Key principle**: combine solutions 4 (KV-cache ordering) and 1 (cache markers) as a free baseline — they require no summarization and no latency overhead. Add rolling summary (solution 2) for long conversations. The combined effect is typically 60–80% reduction in input tokens over a 20-turn conversation compared to naive full-context replay.
