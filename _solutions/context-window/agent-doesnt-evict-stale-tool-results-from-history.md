---
layout: solution
title: "Agent doesn't evict stale tool results from history"
category: context-window
description: "Agent accumulates every tool result in the conversation history, bloating the context window with data that is no longer relevant to the current task."
tags: [context-window, token-cost, tool-failure, memory, history]
---

## Symptom

After a dozen or more tool calls the context window meter climbs steadily. Old tool results — file contents read two hours ago, API responses from earlier subtasks, debug output that was already acted on — sit in the history consuming tokens on every subsequent turn. Eventually the context fills and either the model starts truncating earlier instructions or the API returns a 400 context-too-long error.

```
Turn 1:  read_file("/app/config.json")     → 8 KB result  [still in history]
Turn 4:  search_docs("deploy")             → 6 KB result  [still in history]
Turn 9:  list_files("/tmp")                → 4 KB result  [still in history]
Turn 14: NEW task begins — all old results still consuming 18 KB
```

## Root Cause

The agent appends every `tool_result` block to the messages list and never removes them. The model treats the full list as context on every API call. Tool results are often large (file contents, API payloads, search results) and have a short useful lifetime — once the reasoning step that required them is complete, they provide diminishing value but continue to consume tokens.

## Fix

Track which tool results have been "consumed" (the agent has moved past the subtask that required them) and evict or summarize them before they accumulate to the point of filling the window.

---

### Option 1 — Rolling window: keep only the N most recent tool results

```python
import anthropic
import json

client = anthropic.Anthropic()

MAX_TOOL_RESULT_TURNS = 3   # keep results from the last N tool-use rounds

def prune_old_tool_results(messages: list[dict], keep_rounds: int) -> list[dict]:
    """
    Remove tool_result blocks from older rounds, keeping the most recent N rounds.
    Non-tool messages (user text, assistant text) are always preserved.
    """
    # Find positions of all user turns that contain tool_results
    tool_result_turn_indices = [
        i for i, m in enumerate(messages)
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and any(b.get("type") == "tool_result" for b in m["content"] if isinstance(b, dict))
    ]

    # Determine which turns to evict (everything older than last N)
    evict_indices = set(tool_result_turn_indices[:-keep_rounds])

    pruned = []
    original_tokens = 0
    pruned_tokens = 0

    for i, msg in enumerate(messages):
        original_tokens += len(json.dumps(msg))
        if i not in evict_indices:
            pruned.append(msg)
            pruned_tokens += len(json.dumps(msg))
        else:
            # Replace the tool_result content with a compact placeholder
            new_content = []
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    new_content.append({
                        "type": "tool_result",
                        "tool_use_id": block["tool_use_id"],
                        "content": "[evicted — result consumed in earlier step]",
                    })
                else:
                    new_content.append(block)
            pruned.append({**msg, "content": new_content})
            pruned_tokens += len(json.dumps(pruned[-1]))

    saved_kb = (original_tokens - pruned_tokens) / 1024
    if saved_kb > 0.1:
        print(f"[PRUNE] Evicted {len(evict_indices)} old tool-result turns, saved {saved_kb:.1f} KB")

    return pruned

def fake_tool(name: str, inputs: dict) -> str:
    if name == "read_file":
        return "FILE CONTENT: " + ("line of code\n" * 50)   # ~800 bytes
    if name == "search_docs":
        return "RESULTS: " + ("doc entry\n" * 30)            # ~300 bytes
    if name == "list_files":
        return json.dumps([f"file_{i}.py" for i in range(40)])
    return "{}"

TOOLS = [
    {"name": "read_file",   "description": "Read a file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "search_docs", "description": "Search docs.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "list_files",  "description": "List files.",  "input_schema": {"type": "object", "properties": {"dir": {"type": "string"}}, "required": ["dir"]}},
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        # Prune before each API call
        messages_to_send = prune_old_tool_results(messages, keep_rounds=MAX_TOOL_RESULT_TURNS)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages_to_send,
        )

        # Append to the FULL history (not the pruned version)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": fake_tool(b.name, b.input)}
            for b in response.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": tool_results})

    return next(b.text for b in response.content if hasattr(b, "text"))

print(run_agent(
    "Read /app/config.json, search docs for 'deploy', list files in /tmp, "
    "then explain what you found and what the next steps should be."
))
```

**Expected Token Savings:** 40–70% reduction in history size for long agentic sessions; savings grow as the number of tool calls increases.

**Environment:** Any synchronous agent with multi-step tool use; adjust `MAX_TOOL_RESULT_TURNS` based on task complexity.

---

### Option 2 — LLM-generated summary replaces raw tool result after consumption

```python
import anthropic
import json

client = anthropic.Anthropic()

def summarize_tool_result(tool_name: str, result: str, max_summary_tokens: int = 40) -> str:
    """Ask the model to condense a tool result into a short summary."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_summary_tokens,
        messages=[{
            "role": "user",
            "content": (
                f"Summarize this {tool_name} result in ≤15 words. "
                f"Keep only facts needed for later reasoning:\n\n{result[:2000]}"
            ),
        }],
    )
    return resp.content[0].text.strip()

def compress_consumed_results(
    messages: list[dict],
    consumed_tool_ids: set[str],
) -> list[dict]:
    """
    Replace consumed tool_result blocks with LLM-generated summaries.
    consumed_tool_ids: set of tool_use_id values already acted on.
    """
    compressed = []
    for msg in messages:
        if msg["role"] != "user" or not isinstance(msg.get("content"), list):
            compressed.append(msg)
            continue

        new_content = []
        for block in msg["content"]:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") in consumed_tool_ids
                and not block.get("content", "").startswith("[summary]")
            ):
                summary = summarize_tool_result("tool", block["content"])
                new_content.append({**block, "content": f"[summary] {summary}"})
                print(f"  [COMPRESSED] {block['tool_use_id'][:8]}… → {summary[:60]}")
            else:
                new_content.append(block)
        compressed.append({**msg, "content": new_content})

    return compressed

def fake_tool(name: str) -> str:
    return "LARGE RESULT: " + ("data payload entry\n" * 40)

TOOLS = [
    {"name": "fetch_config", "description": "Fetch config.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "fetch_schema", "description": "Fetch schema.", "input_schema": {"type": "object", "properties": {}, "required": []}},
]

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    consumed: set[str] = set()
    turn = 0

    while True:
        # After turn 1, compress results from previous turns
        if turn > 0:
            messages = compress_consumed_results(messages, consumed)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        turn += 1

        if response.stop_reason != "tool_use":
            break

        tool_use_ids = [b.id for b in response.content if b.type == "tool_use"]
        tool_results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": fake_tool(b.name)}
            for b in response.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": tool_results})

        # Mark these IDs as consumed on the next turn
        consumed.update(tool_use_ids)

    return next(b.text for b in response.content if hasattr(b, "text"))

print(run("Fetch the config and schema, then describe the system architecture based on them."))
```

**Expected Token Savings:** 60–80% reduction in tool-result token size; summaries typically 10–20 tokens vs. hundreds for raw results; small summarization cost is recouped within 2–3 subsequent turns.

**Environment:** Long-running agents where tool results are read-once; not suitable for results that may need exact reproduction later.

---

### Option 3 — Age-based TTL eviction with configurable staleness threshold

```python
import anthropic
import json
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class TimestampedMessage:
    message: dict
    created_at: float = field(default_factory=time.monotonic)

TTL_SECONDS = 120.0   # tool results older than this are evicted

class TTLMessageHistory:
    def __init__(self, ttl: float = TTL_SECONDS) -> None:
        self._entries: list[TimestampedMessage] = []
        self._ttl = ttl

    def append(self, message: dict) -> None:
        self._entries.append(TimestampedMessage(message))

    def messages_for_api(self) -> list[dict]:
        """Return messages with stale tool_results evicted."""
        now = time.monotonic()
        result = []
        evicted = 0

        for entry in self._entries:
            age = now - entry.created_at
            msg = entry.message

            if (
                msg["role"] == "user"
                and isinstance(msg.get("content"), list)
                and age > self._ttl
            ):
                new_content = []
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        new_content.append({
                            "type": "tool_result",
                            "tool_use_id": block["tool_use_id"],
                            "content": f"[expired after {age:.0f}s — use current data if needed]",
                        })
                        evicted += 1
                    else:
                        new_content.append(block)
                result.append({**msg, "content": new_content})
            else:
                result.append(msg)

        if evicted:
            print(f"[TTL] Evicted {evicted} stale tool_result blocks (TTL={self._ttl}s)")
        return result

def fake_tool(name: str) -> str:
    return f"[{name} RESULT] " + ("payload line\n" * 20)

TOOLS = [
    {"name": "check_status",  "description": "Check system status.",  "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_metrics",   "description": "Get current metrics.",  "input_schema": {"type": "object", "properties": {}, "required": []}},
]

def run(user_message: str) -> str:
    history = TTLMessageHistory(ttl=TTL_SECONDS)
    history.append({"role": "user", "content": user_message})

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=history.messages_for_api(),
        )
        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        history.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": fake_tool(b.name)}
                for b in response.content if b.type == "tool_use"
            ],
        })

    return next(b.text for b in response.content if hasattr(b, "text"))

print(run("Check system status and get metrics, then summarize the health of the system."))
```

**Expected Token Savings:** 50–75% for long-running sessions (hours); TTL eviction is automatic and requires no manual tracking of which results have been consumed.

**Environment:** Long-lived agents (daemon processes, scheduled runners); set TTL based on how quickly the underlying data changes.

---

### Option 4 — Selective keep-list: mark results as important before eviction

```python
import anthropic
import json

client = anthropic.Anthropic()

IMPORTANT_MARKER = "[KEEP]"   # prefix this to any result that must survive eviction

def mark_important(result: str) -> str:
    return f"{IMPORTANT_MARKER} {result}"

def evict_unimportant_results(
    messages: list[dict],
    max_kept_unimportant: int = 2,
) -> list[dict]:
    """
    Keep all results marked [KEEP].
    For unmarked results, keep only the most recent N.
    """
    # Collect positions of unmarked tool_result turns
    unmarked_positions = []
    for i, msg in enumerate(messages):
        if msg["role"] != "user" or not isinstance(msg.get("content"), list):
            continue
        if any(
            isinstance(b, dict)
            and b.get("type") == "tool_result"
            and not b.get("content", "").startswith(IMPORTANT_MARKER)
            for b in msg["content"]
        ):
            unmarked_positions.append(i)

    evict_set = set(unmarked_positions[:-max_kept_unimportant])
    evicted = 0

    result_msgs = []
    for i, msg in enumerate(messages):
        if i not in evict_set:
            result_msgs.append(msg)
            continue
        new_content = []
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if not block.get("content", "").startswith(IMPORTANT_MARKER):
                    new_content.append({**block, "content": "[evicted]"})
                    evicted += 1
                else:
                    new_content.append(block)
            else:
                new_content.append(block)
        result_msgs.append({**msg, "content": new_content})

    if evicted:
        print(f"[SELECTIVE] Evicted {evicted} unmarked tool_results")
    return result_msgs

def fake_tool(name: str) -> str:
    return f"{name} returned: " + ("data\n" * 30)

def fake_tool_important(name: str) -> str:
    # Some results are marked as important and survive eviction
    return mark_important(f"{name} returned CRITICAL: " + ("important config\n" * 5))

TOOLS = [
    {"name": "get_config",    "description": "Get critical config (important).", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_logs",      "description": "Get recent logs.",                 "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_temp_data", "description": "Get temporary working data.",      "input_schema": {"type": "object", "properties": {}, "required": []}},
]

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        messages_to_send = evict_unimportant_results(messages, max_kept_unimportant=2)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages_to_send,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        results = []
        for b in response.content:
            if b.type != "tool_use":
                continue
            # Mark important results — others are evictable
            if b.name == "get_config":
                content = fake_tool_important(b.name)
            else:
                content = fake_tool(b.name)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})

        messages.append({"role": "user", "content": results})

    return next(b.text for b in response.content if hasattr(b, "text"))

print(run("Get config, recent logs, and temp data. Summarize the system state."))
```

**Expected Token Savings:** 50–70% for sessions with mixed important/transient tool results; important results are always available while transient bulk data is shed.

**Environment:** Agents with heterogeneous tool result importance (e.g., config is permanent, search results are transient); no changes needed to tool implementations.

---

### Option 5 — Context budget enforcer: evict until under token threshold

```python
import anthropic
import json

client = anthropic.Anthropic()

CONTEXT_BUDGET_CHARS = 40_000   # rough character budget (≈ 10K tokens)

def estimate_chars(messages: list[dict]) -> int:
    return sum(len(json.dumps(m)) for m in messages)

def enforce_budget(messages: list[dict], budget: int) -> list[dict]:
    """
    Evict oldest non-system tool_result blocks until total size is within budget.
    Preserves the first user message and all assistant text.
    """
    if estimate_chars(messages) <= budget:
        return messages

    # Find evictable positions (user turns with tool_results, oldest first)
    evictable = [
        i for i, m in enumerate(messages)
        if m["role"] == "user"
        and isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"] if isinstance(b, dict))
        and i > 0  # never evict the first user message
    ]

    msgs = [m for m in messages]   # shallow copy
    evicted = 0

    for idx in evictable:
        if estimate_chars(msgs) <= budget:
            break
        new_content = []
        for block in msgs[idx].get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                new_content.append({**block, "content": "[evicted: budget exceeded]"})
                evicted += 1
            else:
                new_content.append(block)
        msgs[idx] = {**msgs[idx], "content": new_content}

    final_size = estimate_chars(msgs)
    print(f"[BUDGET] Evicted {evicted} results | size {estimate_chars(messages):,} → {final_size:,} chars")
    return msgs

def fake_tool(name: str) -> str:
    return f"[{name}] " + ("content block\n" * 60)   # ~800 chars each

TOOLS = [
    {"name": f"tool_{i}", "description": f"Tool {i}.", "input_schema": {"type": "object", "properties": {}, "required": []}}
    for i in range(5)
]

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        messages_to_send = enforce_budget(messages, CONTEXT_BUDGET_CHARS)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages_to_send,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": fake_tool(b.name)}
                for b in response.content if b.type == "tool_use"
            ],
        })

    return next(b.text for b in response.content if hasattr(b, "text"))

print(run("Use tool_0, tool_1, tool_2, tool_3, and tool_4 in sequence, then summarize all results."))
```

**Expected Token Savings:** Guarantees the context never exceeds budget; particularly useful when exact token counting is not available and a character proxy is sufficient.

**Environment:** Any agent; set `CONTEXT_BUDGET_CHARS` to approximately 80% of the model's context limit in characters.

---

### Option 6 — Async history compactor running in background between turns

```python
import anthropic
import asyncio
import json

async_client = anthropic.AsyncAnthropic()

async def compact_history_async(
    messages: list[dict],
    max_result_chars: int = 500,
) -> list[dict]:
    """
    Summarize every tool_result that exceeds max_result_chars.
    Runs concurrently for all eligible results.
    """

    async def summarize(block: dict) -> dict:
        content = block.get("content", "")
        if not isinstance(content, str) or len(content) <= max_result_chars:
            return block
        resp = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=30,
            messages=[{"role": "user", "content": f"Summarize in ≤10 words:\n{content[:1000]}"}],
        )
        summary = resp.content[0].text.strip()
        print(f"  [COMPACT] {len(content)} chars → '{summary}'")
        return {**block, "content": f"[compact] {summary}"}

    tasks = []
    for msg in messages:
        if msg["role"] != "user" or not isinstance(msg.get("content"), list):
            tasks.append(asyncio.coroutine(lambda m=msg: m)())
            continue

        async def process_msg(m: dict) -> dict:
            new_blocks = await asyncio.gather(*[
                summarize(b) if isinstance(b, dict) and b.get("type") == "tool_result" else asyncio.coroutine(lambda x=b: x)()
                for b in m["content"]
            ])
            return {**m, "content": list(new_blocks)}

        tasks.append(process_msg(msg))

    return list(await asyncio.gather(*tasks))

def fake_tool(name: str) -> str:
    return f"[{name}] " + ("long result payload\n" * 40)

TOOLS = [
    {"name": "analyze_repo",  "description": "Analyze repository.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "run_tests",     "description": "Run test suite.",     "input_schema": {"type": "object", "properties": {}, "required": []}},
]

async def run_async(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    turn = 0

    while True:
        # Compact history from previous turns before sending
        if turn > 0:
            messages = await compact_history_async(messages, max_result_chars=200)

        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        turn += 1

        if response.stop_reason != "tool_use":
            break

        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": fake_tool(b.name)}
                for b in response.content if b.type == "tool_use"
            ],
        })

    return next(b.text for b in response.content if hasattr(b, "text"))

print(asyncio.run(run_async("Analyze the repo and run the tests, then summarize findings.")))
```

**Expected Token Savings:** 65–85% reduction in history size; async fan-out means multiple tool results are compacted in parallel, adding minimal latency.

**Environment:** Async agents; summarization API calls run concurrently so the compaction overhead scales with the number of results, not their total size.

---

## Comparison

| Option | Eviction Trigger | Preserves Content | LLM Cost | Implementation |
|--------|-----------------|------------------|---------|---------------|
| 1 — Rolling window | Turn count | Placeholder text | None | Simple |
| 2 — LLM summary | Consumed flag | Summary | Small | Moderate |
| 3 — TTL expiry | Age in seconds | Placeholder text | None | Simple |
| 4 — Keep-list | Importance marker | Marked results | None | Moderate |
| 5 — Budget enforcer | Character count | Placeholder text | None | Simple |
| 6 — Async compactor | Size threshold | Async summary | Small | Moderate |

**Recommended default:** Option 1 (rolling window) for quick setup; Option 5 (budget enforcer) for production agents where you need a hard guarantee against context overflow.
