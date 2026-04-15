---
layout: solution
title: "Agent Doesn't Implement Sliding Window Context Management"
category: context-window
description: "Agent accumulates the full conversation history on every turn until hitting the context limit — causing token cost to grow quadratically and eventually crashing with a context overflow error mid-task."
tags: [context-window, sliding-window, context-management, token-cost, memory, summarization]
---

# Agent Doesn't Implement Sliding Window Context Management

## Problem

Every message added to a multi-turn agent conversation is resent on every subsequent API call. After 20 turns, the 20th request contains all 19 previous exchanges — and the 30th contains all 29. This causes:

- **Quadratic token growth**: N turns = N×(N+1)/2 total tokens sent across all turns
- **Context limit crashes**: tasks fail mid-execution with a "context length exceeded" error
- **Rising per-request cost**: each turn gets more expensive as history accumulates
- **Irrelevant noise**: early messages about a different sub-task clutter current reasoning

**Root cause:** The agent loop appends to `messages` unconditionally and never trims, summarizes, or windowed old turns.

---

## Option 1: Fixed-Size Sliding Window — Keep Last N Turns

Simplest fix: keep the system prompt and only the most recent N message pairs.

```python
import anthropic
from typing import Optional

client = anthropic.Anthropic()

def sliding_window(
    messages: list[dict],
    max_turns: int = 10,
    always_keep_first: bool = True
) -> list[dict]:
    """Keep the most recent `max_turns` user/assistant pairs.

    A "turn" = one user message + one assistant response = 2 messages.
    """
    max_messages = max_turns * 2

    if len(messages) <= max_messages:
        return messages

    if always_keep_first:
        # Always keep the first user message (often contains the original task)
        first = messages[0:1]
        rest = messages[1:]
        trimmed_rest = rest[-max_messages + 1:]
        result = first + trimmed_rest
    else:
        result = messages[-max_messages:]

    dropped = len(messages) - len(result)
    print(f"[window] Trimmed {dropped} messages ({len(result)} remaining)")
    return result

def count_approx_tokens(messages: list[dict]) -> int:
    """Rough token estimate: 1 token ≈ 4 chars."""
    total_chars = sum(
        len(str(m.get("content", ""))) for m in messages
    )
    return total_chars // 4

def run_sliding_window_agent(initial_query: str, follow_ups: list[str], window_turns: int = 5) -> list[str]:
    messages = [{"role": "user", "content": initial_query}]
    responses = []

    all_turns = [initial_query] + follow_ups

    for i, query in enumerate(all_turns):
        if i > 0:
            messages.append({"role": "user", "content": query})

        # Apply sliding window before each API call
        windowed = sliding_window(messages, max_turns=window_turns)
        tokens = count_approx_tokens(windowed)
        print(f"[window] Turn {i+1}: {len(windowed)} messages, ~{tokens} tokens")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=windowed
        )

        answer = response.content[0].text
        responses.append(answer)

        # Append full (unwindowed) response to real history
        messages.append({"role": "assistant", "content": answer})

    return responses

FOLLOW_UPS = [
    "Can you give a Python code example?",
    "What are the main drawbacks?",
    "How does this compare to Redis?",
    "What about horizontal scaling?",
    "When would you NOT use this approach?",
    "Summarize all your points in 3 bullets.",
]

answers = run_sliding_window_agent(
    "Explain SQLite as a cache backend for Python services",
    FOLLOW_UPS,
    window_turns=4
)
print(f"\nGenerated {len(answers)} responses")

# Expected Token Savings: ~60% on long conversations (window cap prevents quadratic growth)
# Environment: Any multi-turn chatbot, coding assistant, or interactive planning agent
```

---

## Option 2: Token-Budget Sliding Window — Trim to Stay Under a Token Limit

Trim old messages dynamically to stay within a token budget, not a fixed turn count.

```python
import anthropic
import tiktoken  # pip install tiktoken (or use char-based estimate)

client = anthropic.Anthropic()

def estimate_tokens(messages: list[dict]) -> int:
    """Character-based token estimate (4 chars ≈ 1 token)."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(str(block.get("text", "") or block.get("content", ""))) // 4
    return total + 50  # Overhead per message

def token_budget_window(
    messages: list[dict],
    max_tokens: int = 8000,
    system_tokens: int = 500
) -> list[dict]:
    """Trim oldest messages (except the first) until total tokens fit within budget."""
    available = max_tokens - system_tokens

    if estimate_tokens(messages) <= available:
        return messages

    # Always keep the first message
    first = messages[0:1]
    rest = messages[1:]

    # Drop from the front until we fit
    while rest and estimate_tokens(first + rest) > available:
        dropped = rest.pop(0)
        print(f"[budget] Dropped message: {str(dropped.get('content', ''))[:50]}...")

    result = first + rest
    print(f"[budget] Window: {len(result)} messages, ~{estimate_tokens(result)} tokens (budget: {available})")
    return result

def run_budget_windowed_agent(
    task: str,
    turns: list[str],
    token_budget: int = 6000
) -> str:
    messages = [{"role": "user", "content": task}]
    last_response = ""

    for i, follow_up in enumerate([None] + turns):
        if follow_up is not None:
            messages.append({"role": "user", "content": follow_up})

        windowed = token_budget_window(messages, max_tokens=token_budget)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=windowed
        )

        last_response = response.content[0].text
        messages.append({"role": "assistant", "content": last_response})
        print(f"[budget] Turn {i+1} done | history: {estimate_tokens(messages)} tokens total")

    return last_response

result = run_budget_windowed_agent(
    "Let's design a microservices architecture for an e-commerce platform.",
    [
        "Start with the authentication service design.",
        "Now describe the product catalog service.",
        "How should the order service communicate with inventory?",
        "What database would you use for each service?",
        "How do we handle distributed transactions?",
        "Summarize the entire architecture we've designed.",
    ],
    token_budget=5000
)
print(f"\nFinal response: {result[:200]}...")

# Expected Token Savings: ~65% (token-aware trimming keeps every request under a hard budget)
# Environment: Agents with large tool outputs or verbose responses that grow context rapidly
```

---

## Option 3: Summarizing Window — Compress Old Turns Into a Running Summary

Instead of dropping old turns, compress them into a rolling summary that replaces the oldest messages.

```python
import anthropic

client = anthropic.Anthropic()

def summarize_messages(messages: list[dict], topic_hint: str = "") -> str:
    """Use a cheap model to compress a slice of conversation history."""
    if not messages:
        return ""

    formatted = []
    for m in messages:
        role = m["role"].upper()
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(b) for b in content)
        formatted.append(f"{role}: {content[:300]}")

    history_text = "\n".join(formatted)
    hint = f"Topic context: {topic_hint}\n" if topic_hint else ""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"{hint}Summarize this conversation history in 3-5 bullet points. "
                       f"Preserve key decisions, facts, and conclusions.\n\n{history_text}"
        }]
    )
    return response.content[0].text

class SummarizingWindow:
    def __init__(self, max_recent_turns: int = 6, compress_after_turns: int = 10):
        self.max_recent = max_recent_turns
        self.compress_after = compress_after_turns
        self.summary: str = ""
        self.recent_messages: list[dict] = []
        self.full_turn_count: int = 0

    def add_turn(self, user_msg: str, assistant_msg: str):
        self.recent_messages.append({"role": "user", "content": user_msg})
        self.recent_messages.append({"role": "assistant", "content": assistant_msg})
        self.full_turn_count += 1

        # Compress when we exceed the threshold
        if len(self.recent_messages) // 2 > self.compress_after:
            compress_count = len(self.recent_messages) - self.max_recent * 2
            to_compress = self.recent_messages[:compress_count]
            new_summary = summarize_messages(to_compress)

            # Merge with existing summary
            if self.summary:
                merge_response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    messages=[{
                        "role": "user",
                        "content": f"Merge these two summaries into one concise summary:\n\n"
                                   f"Earlier: {self.summary}\n\nRecent: {new_summary}"
                    }]
                )
                self.summary = merge_response.content[0].text
            else:
                self.summary = new_summary

            self.recent_messages = self.recent_messages[compress_count:]
            print(f"[summary-window] Compressed {compress_count} messages into summary. "
                  f"Recent: {len(self.recent_messages)} messages.")

    def build_messages(self, current_query: str) -> list[dict]:
        """Build the message list for the next API call."""
        messages = []
        if self.summary:
            # Inject summary as a system-like context message
            messages.append({
                "role": "user",
                "content": f"[Previous conversation summary]\n{self.summary}"
            })
            messages.append({
                "role": "assistant",
                "content": "Understood. I have the context from our earlier discussion."
            })
        messages.extend(self.recent_messages)
        messages.append({"role": "user", "content": current_query})
        return messages

def run_summarizing_window_agent(task: str, turns: list[str]) -> str:
    window = SummarizingWindow(max_recent_turns=4, compress_after_turns=6)
    last_response = ""

    # First turn
    messages = [{"role": "user", "content": task}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=messages
    )
    last_response = response.content[0].text
    window.add_turn(task, last_response)

    for follow_up in turns:
        messages = window.build_messages(follow_up)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages
        )
        last_response = response.content[0].text
        window.add_turn(follow_up, last_response)
        print(f"[summary-window] Turn {window.full_turn_count} | Summary exists: {bool(window.summary)}")

    return last_response

result = run_summarizing_window_agent(
    "We're building a real-time chat application. Let's start with the architecture.",
    [
        "What WebSocket library should we use?",
        "How do we handle message persistence?",
        "What about user presence (online/offline)?",
        "How do we scale to 100k concurrent users?",
        "What's the database schema for messages?",
        "How do we implement read receipts?",
        "What caching strategy do you recommend?",
    ]
)
print(f"\nFinal: {result[:200]}...")

# Expected Token Savings: ~70% (compressed history is 90% smaller than raw turns; critical facts preserved)
# Environment: Long design sessions, code review conversations, multi-step planning with back-references
```

---

## Option 4: Hierarchical Window — Recency-Weighted Message Scoring

Score each message by recency, relevance to current query, and information density; drop lowest scorers.

```python
import anthropic
import math

client = anthropic.Anthropic()

def score_message(
    message: dict,
    position: int,
    total: int,
    current_query: str
) -> float:
    """Score a message for relevance. Higher = more important to keep."""
    content = str(message.get("content", "")).lower()
    query_lower = current_query.lower()

    # Recency score: exponential decay, recent messages score higher
    recency = math.exp((position - total) / 5)

    # Keyword overlap with current query
    query_words = set(query_lower.split())
    content_words = set(content.split())
    overlap = len(query_words & content_words)
    relevance = overlap / max(len(query_words), 1)

    # Information density: longer messages have more info
    density = min(1.0, len(content) / 500)

    # Role weight: assistant messages often have higher info density
    role_weight = 1.1 if message.get("role") == "assistant" else 1.0

    return (recency * 0.5 + relevance * 0.3 + density * 0.2) * role_weight

def hierarchical_window(
    messages: list[dict],
    current_query: str,
    max_messages: int = 12,
    always_keep_first: int = 2
) -> list[dict]:
    """Keep top-scored messages by recency + relevance."""
    if len(messages) <= max_messages:
        return messages

    # Always keep the first N messages (original task context)
    pinned = messages[:always_keep_first]
    candidates = messages[always_keep_first:]

    # Score and sort
    scored = [
        (score_message(m, i + always_keep_first, len(messages), current_query), i, m)
        for i, m in enumerate(candidates)
    ]
    scored.sort(reverse=True)

    # Take top (max_messages - pinned) messages, re-sort by original order
    keep_count = max_messages - len(pinned)
    kept_with_index = [(idx, m) for _, idx, m in scored[:keep_count]]
    kept_with_index.sort(key=lambda x: x[0])  # Restore chronological order

    result = pinned + [m for _, m in kept_with_index]
    print(f"[hierarchical] Kept {len(result)}/{len(messages)} messages "
          f"(dropped {len(messages) - len(result)} low-relevance messages)")
    return result

def run_hierarchical_window_agent(task: str, turns: list[str]) -> list[str]:
    messages = [{"role": "user", "content": task}]
    responses = []

    all_queries = [task] + turns

    for i, query in enumerate(all_queries):
        if i > 0:
            messages.append({"role": "user", "content": query})

        windowed = hierarchical_window(messages, current_query=query, max_messages=10)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=windowed
        )

        answer = response.content[0].text
        responses.append(answer)
        messages.append({"role": "assistant", "content": answer})

    return responses

TURNS = [
    "What Python libraries should I use for async HTTP?",
    "Show me a basic FastAPI setup.",
    "How do I add JWT authentication?",
    "What about database connections with SQLAlchemy?",
    "Going back to the async HTTP question — what about httpx vs aiohttp?",
    "Summarize the tech stack we've discussed.",
]

answers = run_hierarchical_window_agent(
    "Help me build a production-ready Python API service.",
    TURNS
)
print(f"Generated {len(answers)} answers")
for q, a in zip(["initial"] + TURNS, answers):
    print(f"  Q: {q[:40]}... → A: {a[:60]}...")

# Expected Token Savings: ~55% (relevance scoring keeps only messages that matter to current query)
# Environment: Long technical conversations where users backtrack to earlier topics
```

---

## Option 5: Tool-Result-Aware Window — Aggressively Trim Verbose Tool Outputs

Keep conversational turns but aggressively truncate tool results, which are often the biggest context consumers.

```python
import anthropic
import json

client = anthropic.Anthropic()

def truncate_tool_result(content: str | list, max_chars: int = 500) -> str:
    """Truncate tool result content aggressively."""
    if isinstance(content, list):
        text = " ".join(str(b.get("text", b.get("content", ""))) for b in content)
    else:
        text = str(content)

    if len(text) <= max_chars:
        return text

    half = max_chars // 2
    truncated = f"{text[:half]}\n...[{len(text) - max_chars} chars omitted]...\n{text[-half:]}"
    return truncated

def tool_result_aware_window(
    messages: list[dict],
    max_turns: int = 8,
    max_tool_result_chars: int = 400
) -> list[dict]:
    """Sliding window + aggressive tool result truncation."""
    max_messages = max_turns * 2

    # First, truncate all tool results in the full history
    trimmed = []
    for m in messages:
        if m["role"] == "user" and isinstance(m.get("content"), list):
            # Check if this is a tool_results message
            new_content = []
            for block in m["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    trimmed_content = truncate_tool_result(
                        block.get("content", ""), max_tool_result_chars
                    )
                    new_content.append({**block, "content": trimmed_content})
                else:
                    new_content.append(block)
            trimmed.append({**m, "content": new_content})
        else:
            trimmed.append(m)

    # Then apply sliding window
    if len(trimmed) > max_messages:
        first = trimmed[0:1]
        rest = trimmed[1:]
        trimmed = first + rest[-max_messages + 1:]

    return trimmed

tools = [
    {
        "name": "search_docs",
        "description": "Search documentation (returns verbose results)",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    }
]

def mock_search(query: str) -> str:
    # Simulate a verbose tool result (2000 chars)
    return f"# Search Results for: {query}\n\n" + "\n\n".join([
        f"## Result {i+1}\n{'Lorem ipsum dolor sit amet, ' * 20}\nRelevance: {0.9 - i*0.1:.1f}"
        for i in range(5)
    ])

def run_tool_aware_window_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        windowed = tool_result_aware_window(messages, max_turns=6, max_tool_result_chars=300)
        char_count = sum(len(str(m.get("content", ""))) for m in windowed)
        print(f"[tool-window] {len(windowed)} messages, ~{char_count // 4} tokens after truncation")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=windowed
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result_text = mock_search(block.input.get("query", ""))
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

result = run_tool_aware_window_agent(
    "Search for Python async patterns, then search for FastAPI best practices, then summarize both."
)
print(f"\nResult: {result[:200]}...")

# Expected Token Savings: ~75% (tool results are often 80% of context; truncating them is highest leverage)
# Environment: RAG agents, search-heavy agents, any agent using verbose external API responses
```

---

## Option 6: Persistent Window with SQLite Offload

Store the full conversation in SQLite; load only the recent window for each API call, preserving complete history for later retrieval.

```python
import anthropic
import json
import sqlite3
from pathlib import Path

client = anthropic.Anthropic()
HISTORY_DB = Path("/tmp/agent_conversation_history.db")

def init_history_db() -> sqlite3.Connection:
    conn = sqlite3.connect(HISTORY_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            token_estimate INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON messages(session_id)")
    conn.commit()
    return conn

def save_message(conn: sqlite3.Connection, session_id: str, role: str, content):
    content_str = json.dumps(content) if not isinstance(content, str) else content
    token_est = len(content_str) // 4
    conn.execute(
        "INSERT INTO messages (session_id, role, content, token_estimate) VALUES (?, ?, ?, ?)",
        (session_id, role, content_str, token_est)
    )
    conn.commit()

def load_window(
    conn: sqlite3.Connection,
    session_id: str,
    max_tokens: int = 4000,
    always_include_first: bool = True
) -> list[dict]:
    """Load messages from SQLite, respecting token budget."""
    rows = conn.execute(
        "SELECT id, role, content, token_estimate FROM messages WHERE session_id=? ORDER BY id",
        (session_id,)
    ).fetchall()

    if not rows:
        return []

    first_row = rows[0] if always_include_first else None
    remaining_rows = rows[1:] if always_include_first else rows

    # Greedily include from the most recent backwards
    selected = []
    budget = max_tokens
    if first_row:
        budget -= (first_row[3] or 0)

    for row in reversed(remaining_rows):
        cost = row[3] or len(row[2]) // 4
        if budget - cost < 0:
            break
        selected.insert(0, row)
        budget -= cost

    all_selected = ([first_row] if first_row else []) + selected

    def parse_content(content_str: str):
        try:
            return json.loads(content_str)
        except (json.JSONDecodeError, TypeError):
            return content_str

    result = [
        {"role": row[1], "content": parse_content(row[2])}
        for row in all_selected
    ]

    print(f"[sqlite-window] Loaded {len(result)}/{len(rows)} messages "
          f"(~{max_tokens - budget} tokens, {len(rows) - len(result)} offloaded to DB)")
    return result

conn = init_history_db()

def run_sqlite_windowed_agent(session_id: str, query: str, follow_ups: list[str]) -> list[str]:
    # Save initial query
    save_message(conn, session_id, "user", query)
    responses = []

    all_turns = [query] + follow_ups

    for i, current_query in enumerate(all_turns):
        if i > 0:
            save_message(conn, session_id, "user", current_query)

        # Load only what fits in the context window
        messages = load_window(conn, session_id, max_tokens=3000)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages
        )

        answer = response.content[0].text
        responses.append(answer)

        # Save to persistent DB (full history preserved)
        save_message(conn, session_id, "assistant", answer)
        print(f"[sqlite-window] Turn {i+1}: full history={i*2+2} messages, loaded={len(messages)}")

    return responses

answers = run_sqlite_windowed_agent(
    session_id="session-xyz-001",
    query="Let's design a distributed job queue system from scratch.",
    follow_ups=[
        "What persistence layer should we use?",
        "How do we handle worker failures?",
        "What's the job priority mechanism?",
        "How do we implement dead letter queues?",
        "How do we monitor queue depth?",
        "Can we go back to worker failures — what about partial completion?",
    ]
)
print(f"\nGenerated {len(answers)} answers. Full history preserved in SQLite.")

# Expected Token Savings: ~70% (SQLite offload keeps full history without paying for it on every turn)
# Environment: Long-running agent sessions spanning hours or days; support bots with persistent customer history
```

---

## Comparison

| Option | Trim Strategy | History Preserved | Relevant Content Kept | Best For |
|--------|--------------|-------------------|----------------------|----------|
| 1. Fixed Turn Window | Drop oldest turns | No | Recent only | Simple chatbots, short tasks |
| 2. Token Budget Window | Drop oldest by token count | No | Recent within budget | Agents with variable-length responses |
| 3. Summarizing Window | Compress → summary | Yes (summarized) | Key decisions preserved | Long design sessions |
| 4. Hierarchical Scoring | Score + keep top-N | No | Recency + relevance | Back-reference heavy conversations |
| 5. Tool-Result Truncation | Truncate tool outputs | Yes (truncated) | Full dialogue, brief tool results | RAG and search-heavy agents |
| 6. SQLite Offload | DB-backed window | Yes (full) | Token-budget window | Long-lived persistent sessions |
