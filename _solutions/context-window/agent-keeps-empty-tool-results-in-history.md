---
layout: solution
title: "Agent keeps empty tool results in history"
category: context-window
description: "Agent retains tool_result blocks that returned empty strings, null values, or zero-row database results in the conversation history. These empty entries consume tokens every turn without providing any information the model can use."
tags: [context-window, tool-failure, history, cleanup, token-cost]
---

## Symptom

Inspecting the conversation history reveals tool_result entries like `""`, `"[]"`, `"null"`, `"No results found."`, or `"0 rows returned"`. On a long session with many such calls, these empty entries accumulate and collectively consume hundreds of tokens per API call — tokens that contribute nothing to the model's reasoning.

## Root Cause

The agent appends every tool result to history unconditionally. Empty results look like noise but take up the same structural tokens (role, type, tool_use_id fields) as informative results. Over a multi-turn session with 20 empty search results, the overhead can exceed 1,000 tokens per call even before counting the result content itself.

## Fix

Filter empty tool results before appending them to history. Replace them with a compact tombstone if the model needs to know the call was made, or drop them entirely if the failure is irrelevant to future reasoning.

---

### Option 1 — Drop empty results before appending to history

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

EMPTY_PATTERNS = {"", "null", "[]", "{}", "none", "no results", "0 rows", "0 results"}


def is_empty_result(content: str) -> bool:
    """Return True if a tool result carries no useful information."""
    if not content:
        return True
    stripped = content.strip().lower()
    if stripped in EMPTY_PATTERNS:
        return True
    # Empty JSON array or object
    try:
        parsed = json.loads(content)
        if parsed is None:
            return True
        if isinstance(parsed, (list, dict)) and len(parsed) == 0:
            return True
    except (json.JSONDecodeError, ValueError):
        pass
    return False


def append_tool_results(
    messages: list[dict],
    tool_results: list[dict],
) -> None:
    """Append only non-empty tool results to history."""
    meaningful = [r for r in tool_results if not is_empty_result(r.get("content", ""))]
    dropped = len(tool_results) - len(meaningful)

    if dropped:
        print(f"Dropped {dropped} empty tool result(s) from history")

    if meaningful:
        messages.append({"role": "user", "content": meaningful})


TOOLS = [
    {
        "name": "search_docs",
        "description": "Search the documentation.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


def fake_search(query: str) -> str:
    # Simulate empty results for most queries
    return "[]" if "obscure" in query.lower() else f"Found: docs matching '{query}'"


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for _ in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    content = fake_search(**block.input)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    })
            append_tool_results(messages, results)

    return ""
```

**Expected Token Savings:** Each dropped empty result saves ~50–80 tokens (structural overhead + content); across 20 empty results in a session, saves ~1,500 tokens per subsequent call.
**Environment:** Any agent that calls search or query tools that frequently return nothing; the filter adds negligible overhead.

---

### Option 2 — Replace empty results with compact tombstones

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def normalize_tool_result(tool_name: str, content: str) -> str | None:
    """
    Return a compact tombstone for empty results, None to drop entirely.
    For results that are informative about emptiness (e.g. "searched but found nothing"),
    a tombstone is better than a full drop so the model knows it tried.
    """
    stripped = content.strip()

    # Completely silent failures — drop entirely
    if stripped in ("", "null", "None"):
        return None

    # Empty collections — replace with compact tombstone
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list) and len(parsed) == 0:
            return f"[{tool_name}: 0 results]"
        if isinstance(parsed, dict) and len(parsed) == 0:
            return f"[{tool_name}: empty response]"
    except (json.JSONDecodeError, ValueError):
        pass

    # Short "no results" messages — compact them
    lowered = stripped.lower()
    no_result_phrases = [
        "no results found", "0 results", "not found", "no matches",
        "nothing found", "no data", "empty", "no records",
    ]
    if any(p in lowered for p in no_result_phrases) and len(stripped) < 100:
        return f"[{tool_name}: no results]"

    # Result has content — return as-is (possibly truncated elsewhere)
    return content


def process_tool_results(
    response_content: list,
    tool_dispatch: dict,
) -> list[dict] | None:
    """Build the tool_result list, dropping or compacting empty entries."""
    results = []
    for block in response_content:
        if block.type != "tool_use":
            continue
        raw = tool_dispatch.get(block.name, lambda **k: "")(** block.input)
        normalized = normalize_tool_result(block.name, raw)

        if normalized is None:
            continue   # fully drop this result

        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": normalized,
        })

    return results if results else None
```

**Expected Token Savings:** Tombstones (`[search_docs: 0 results]`) are ~10 tokens vs 30–80 tokens for a verbose "no results found" message; the model still knows the call was attempted.
**Environment:** Agents where the model needs to know a search was attempted (to avoid re-trying) but the empty result has no other value.

---

### Option 3 — History compactor: prune empty entries from existing history

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def is_empty_tool_result(content_item: dict) -> bool:
    if content_item.get("type") != "tool_result":
        return False
    content = content_item.get("content", "")
    if not content or content.strip() in ("", "null", "[]", "{}", "none"):
        return True
    try:
        parsed = json.loads(content)
        return isinstance(parsed, (list, dict)) and len(parsed) == 0
    except (json.JSONDecodeError, ValueError):
        return False


def compact_history(messages: list[dict]) -> tuple[list[dict], int]:
    """
    Remove empty tool_result entries from conversation history.
    Returns (compacted_messages, number_of_entries_removed).
    """
    removed = 0
    compacted: list[dict] = []

    for msg in messages:
        if msg["role"] != "user":
            compacted.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            compacted.append(msg)
            continue

        # Filter out empty tool results
        filtered = [c for c in content if not is_empty_tool_result(c)]
        removed += len(content) - len(filtered)

        if filtered:
            compacted.append({"role": "user", "content": filtered})
        # If all content was empty tool results, drop the entire message

    return compacted, removed


def run_agent_with_compaction(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]
    COMPACT_EVERY_N_TURNS = 5
    turn = 0

    for _ in range(20):
        turn += 1

        # Periodically compact history
        if turn % COMPACT_EVERY_N_TURNS == 0:
            messages, removed = compact_history(messages)
            if removed:
                print(f"Compacted history: removed {removed} empty tool result(s)")

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})

    return ""
```

**Expected Token Savings:** Retroactive compaction reclaims tokens even from early-session empty results; every 5 turns, the context shrinks by the number of empty entries accumulated.
**Environment:** Long-running agents where empty results accumulate over time; the periodic compaction keeps the context window lean without requiring changes to tool dispatch code.

---

### Option 4 — Async agent with empty-result counter and automatic summary

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class ToolResultStats:
    empty_count: int = 0
    nonempty_count: int = 0
    empty_by_tool: dict[str, int] = field(default_factory=dict)

    def record(self, tool_name: str, is_empty: bool) -> None:
        if is_empty:
            self.empty_count += 1
            self.empty_by_tool[tool_name] = self.empty_by_tool.get(tool_name, 0) + 1
        else:
            self.nonempty_count += 1

    def empty_summary(self) -> str | None:
        if not self.empty_by_tool:
            return None
        parts = [f"{n}×{tool}" for tool, n in sorted(self.empty_by_tool.items())]
        return f"[{self.empty_count} empty results: {', '.join(parts)}]"


async def dispatch_tool(tool_name: str, tool_input: dict) -> str:
    await asyncio.sleep(0.01)  # simulate async I/O
    if tool_name == "search_kb":
        return "[]"  # always empty for demo
    return f"Result: {tool_input}"


async def run_agent_async(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]
    stats = ToolResultStats()
    TOOLS = [
        {
            "name": "search_kb",
            "description": "Search the knowledge base.",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        }
    ]

    for turn in range(10):
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            summary = stats.empty_summary()
            if summary:
                print(f"Session stats: {summary}")
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            # Dispatch all tool calls concurrently
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            raw_results = await asyncio.gather(*[
                dispatch_tool(b.name, b.input) for b in tool_blocks
            ])

            meaningful_results = []
            accumulated_empty: list[str] = []

            for block, raw in zip(tool_blocks, raw_results):
                import json
                try:
                    parsed = json.loads(raw)
                    empty = isinstance(parsed, (list, dict)) and len(parsed) == 0
                except Exception:
                    empty = not raw.strip()

                stats.record(block.name, empty)

                if empty:
                    accumulated_empty.append(block.name)
                else:
                    meaningful_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": raw,
                    })

            # Inject a single compact summary for all empty results
            if accumulated_empty and turn == 0:
                meaningful_results.append({
                    "type": "text",
                    "text": f"Note: {len(accumulated_empty)} tool call(s) returned no results: {accumulated_empty}",
                })

            if meaningful_results:
                messages.append({"role": "user", "content": meaningful_results})

    return ""
```

**Expected Token Savings:** Multiple empty results per turn collapse into one short summary line; concurrent dispatch means no added latency.
**Environment:** Async agents with parallel tool calls where several tools may return empty simultaneously.

---

### Option 5 — Budget-aware history trimmer targeting empty results first

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

CONTEXT_BUDGET_CHARS = 50_000   # approximate; tune per model


def estimate_chars(messages: list[dict]) -> int:
    return sum(len(str(m)) for m in messages)


def _is_empty_tool_msg(msg: dict) -> bool:
    """True if this user message contains ONLY empty tool results."""
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    tool_results = [c for c in content if c.get("type") == "tool_result"]
    if not tool_results or len(tool_results) != len(content):
        return False
    for tr in tool_results:
        raw = tr.get("content", "")
        if raw.strip() not in ("", "null", "[]", "{}"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, (list, dict)) and len(parsed) > 0:
                    return False
            except Exception:
                return False
    return True


def trim_to_budget(
    messages: list[dict],
    budget_chars: int = CONTEXT_BUDGET_CHARS,
) -> list[dict]:
    """
    If history exceeds budget, remove empty-result messages first (cheapest to drop),
    then fall back to removing oldest non-system messages.
    """
    if estimate_chars(messages) <= budget_chars:
        return messages

    # Pass 1: drop empty-result user messages (keep system + first user)
    trimmed = []
    for i, msg in enumerate(messages):
        if i > 0 and msg.get("role") == "user" and _is_empty_tool_msg(msg):
            continue  # drop
        trimmed.append(msg)

    if estimate_chars(trimmed) <= budget_chars:
        return trimmed

    # Pass 2: drop oldest messages (keep first and last N)
    keep_first = 1
    keep_last = max(4, len(trimmed) // 2)
    return trimmed[:keep_first] + trimmed[-keep_last:]


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for _ in range(20):
        messages = trim_to_budget(messages)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})

    return ""
```

**Expected Token Savings:** Empty-result messages are the first casualty of trimming, preserving informative history while staying under the budget; no summarization LLM calls required.
**Environment:** Long-running agents with a context budget; the two-pass strategy removes the lowest-value content first.

---

### Option 6 — Tool result cache: avoid re-calling tools whose results were empty

```python
import anthropic
import json
import hashlib

client = anthropic.Anthropic(api_key="sk-live-...")

# Cache: tool_name + input_hash → result
_result_cache: dict[str, str | None] = {}  # None means "known empty"


def _cache_key(tool_name: str, tool_input: dict) -> str:
    canonical = json.dumps(tool_input, sort_keys=True)
    return f"{tool_name}:{hashlib.sha256(canonical.encode()).hexdigest()[:12]}"


def cached_dispatch(tool_name: str, tool_input: dict, fn) -> str | None:
    """
    Dispatch a tool call. If the result was previously empty, return None immediately.
    Cache non-empty results to avoid redundant calls.
    """
    key = _cache_key(tool_name, tool_input)

    if key in _result_cache:
        cached = _result_cache[key]
        if cached is None:
            print(f"[cache] Skipping {tool_name} — known empty for this input")
        else:
            print(f"[cache] Returning cached result for {tool_name}")
        return cached

    raw = fn(tool_input)

    # Determine if empty
    is_empty = False
    stripped = raw.strip() if raw else ""
    if not stripped or stripped in ("null", "[]", "{}"):
        is_empty = True
    else:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, (list, dict)) and len(parsed) == 0:
                is_empty = True
        except Exception:
            pass

    _result_cache[key] = None if is_empty else raw
    return None if is_empty else raw


def fake_search(tool_input: dict) -> str:
    return "[]"   # always empty for demo


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]
    TOOLS = [
        {
            "name": "search_docs",
            "description": "Search documentation.",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        }
    ]

    for _ in range(8):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = cached_dispatch(block.name, block.input, fake_search)
                if result is not None:
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            if results:
                messages.append({"role": "user", "content": results})

    return ""


# Comparison table
# | Option | When It Acts | Model Notified? | Extra Cost |
# |--------|-------------|-----------------|------------|
# | 1 Drop on append | Write time | No | None |
# | 2 Tombstone | Write time | Yes (compact) | None |
# | 3 Retroactive compactor | Periodic | No | None |
# | 4 Async counter + summary | Per turn | Yes (batch) | None |
# | 5 Budget-first trimmer | Budget exceeded | No | None |
# | 6 Cache + skip | Call time | No | None |
```

**Expected Token Savings:** Caching prevents re-calling known-empty tools (0 tokens for the skipped call + 0 tokens for the empty result in history); particularly effective when the model retries the same empty search multiple times.
**Environment:** Agents that retry searches with the same parameters hoping for different results; the cache surfaces this anti-pattern immediately.
