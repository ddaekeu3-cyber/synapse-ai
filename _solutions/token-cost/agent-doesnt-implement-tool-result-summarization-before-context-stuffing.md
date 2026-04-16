---
title: "Agent Doesn't Implement Tool Result Summarization Before Context Stuffing"
description: "How to summarize large tool results before inserting them into the context window, preventing token bloat and reducing API costs."
categories: [token-cost]
difficulty: intermediate
---

Raw tool results—database rows, file contents, API payloads—can easily consume tens of thousands of tokens when pasted verbatim into the conversation. Summarizing them before context insertion keeps prompts lean, reduces cost, and prevents the model from losing focus in a sea of raw data.

## Solution 1: LLM-Summarized Tool Result with Size Gate

Only summarize when the result exceeds a token threshold; small results pass through unchanged.

```python
import anthropic

client = anthropic.AsyncAnthropic()

SUMMARY_THRESHOLD_TOKENS = 500
SUMMARY_MODEL = "claude-haiku-4-5-20251001"


async def count_tokens_approx(text: str) -> int:
    """Fast character-based approximation (1 token ≈ 4 chars)."""
    return len(text) // 4


async def summarize_tool_result(tool_name: str, raw_result: str) -> str:
    """Summarize a tool result using a cheap model."""
    response = await client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Summarize the following tool result from '{tool_name}' "
                    f"in ≤150 words, preserving all key facts and numbers:\n\n"
                    f"{raw_result}"
                ),
            }
        ],
    )
    return response.content[0].text


async def maybe_summarize(tool_name: str, raw_result: str) -> str:
    approx = await count_tokens_approx(raw_result)
    if approx <= SUMMARY_THRESHOLD_TOKENS:
        return raw_result
    summary = await summarize_tool_result(tool_name, raw_result)
    return f"[Summarized from {approx} tokens] {summary}"


async def agent_loop(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]
    tools = [
        {
            "name": "search_database",
            "description": "Search the product database",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                raw = f"[DB rows for '{block.input['query']}']: " + ", ".join(
                    f"product_{i}" for i in range(200)
                )
                condensed = await maybe_summarize(block.name, raw)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": condensed}
                )

        messages.append({"role": "user", "content": tool_results})
```

## Solution 2: Template-Based Structured Extraction

Extract only the fields the model actually needs rather than summarizing with a second LLM call.

```python
import json
import re
from dataclasses import dataclass
from typing import Any

import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class ExtractionTemplate:
    tool_name: str
    keep_fields: list[str]
    max_list_items: int = 5
    include_count: bool = True


TEMPLATES: dict[str, ExtractionTemplate] = {
    "search_database": ExtractionTemplate(
        tool_name="search_database",
        keep_fields=["id", "name", "price", "stock"],
        max_list_items=10,
    ),
    "fetch_logs": ExtractionTemplate(
        tool_name="fetch_logs",
        keep_fields=["timestamp", "level", "message"],
        max_list_items=20,
    ),
    "get_user_profile": ExtractionTemplate(
        tool_name="get_user_profile",
        keep_fields=["id", "email", "plan", "created_at"],
        max_list_items=1,
    ),
}


def extract_structured(tool_name: str, raw: Any) -> str:
    template = TEMPLATES.get(tool_name)
    if template is None:
        # No template — fall back to truncation
        text = json.dumps(raw) if not isinstance(raw, str) else raw
        return text[:2000] + ("…" if len(text) > 2000 else "")

    if isinstance(raw, list):
        total = len(raw)
        items = raw[: template.max_list_items]
        pruned = [
            {k: item[k] for k in template.keep_fields if k in item}
            for item in items
            if isinstance(item, dict)
        ]
        suffix = f" (showing {len(pruned)}/{total})" if template.include_count else ""
        return json.dumps(pruned) + suffix

    if isinstance(raw, dict):
        pruned = {k: raw[k] for k in template.keep_fields if k in raw}
        return json.dumps(pruned)

    return str(raw)[:2000]


async def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    tools = [
        {
            "name": "search_database",
            "description": "Search products",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    while True:
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                # Simulate a large raw result
                raw = [
                    {"id": i, "name": f"Product {i}", "price": i * 9.99,
                     "stock": i * 3, "description": "x" * 500, "tags": ["a", "b"]}
                    for i in range(500)
                ]
                condensed = extract_structured(block.name, raw)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": condensed}
                )

        messages.append({"role": "user", "content": results})
```

## Solution 3: Rolling Summary with Eviction

Maintain a rolling summary that grows only logarithmically: each new result is merged into a single running summary rather than appended verbatim.

```python
import anthropic

client = anthropic.AsyncAnthropic()

MERGE_MODEL = "claude-haiku-4-5-20251001"
MAX_SUMMARY_TOKENS = 400


async def merge_into_running_summary(
    existing_summary: str, tool_name: str, new_result: str
) -> str:
    """Merge new tool result into an existing running summary."""
    if not existing_summary:
        prompt = (
            f"Summarize this tool result from '{tool_name}' in ≤200 words:\n{new_result}"
        )
    else:
        prompt = (
            f"You have an existing summary:\n{existing_summary}\n\n"
            f"New tool result from '{tool_name}':\n{new_result}\n\n"
            f"Merge both into an updated summary of ≤200 words, preserving all key facts."
        )

    resp = await client.messages.create(
        model=MERGE_MODEL,
        max_tokens=MAX_SUMMARY_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


async def agent_with_rolling_summary(user_query: str) -> str:
    running_summary = ""
    messages = [{"role": "user", "content": user_query}]
    tools = [
        {
            "name": "query_analytics",
            "description": "Query analytics data",
            "input_schema": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "days": {"type": "integer"},
                },
                "required": ["metric", "days"],
            },
        }
    ]

    iteration = 0
    while iteration < 10:
        iteration += 1

        # Inject the current running summary as context
        context_messages = list(messages)
        if running_summary:
            context_messages.insert(
                1,
                {
                    "role": "user",
                    "content": f"[Accumulated tool context]\n{running_summary}",
                },
            )
            context_messages.insert(
                2, {"role": "assistant", "content": "Understood, I have that context."}
            )

        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=context_messages,
        )

        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})

        for block in resp.content:
            if block.type == "tool_use":
                raw_result = f"[Analytics data: {block.input}] " + ", ".join(
                    f"day_{d}: {d * 1234}" for d in range(90)
                )
                running_summary = await merge_into_running_summary(
                    running_summary, block.name, raw_result
                )
                # Inject a concise placeholder into message history
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": "[Result merged into running context summary]",
                            }
                        ],
                    }
                )

    return "Max iterations reached."
```

## Solution 4: Semantic Chunking with Relevance Ranking

Split the result into semantic chunks, embed them, and inject only the top-k most relevant chunks.

```python
import hashlib
import math
from collections import defaultdict
from typing import Any

import anthropic

client = anthropic.AsyncAnthropic()


def simple_tfidf_score(chunk: str, query: str) -> float:
    """Approximate relevance with TF-IDF on word overlap (no external deps)."""
    query_words = set(query.lower().split())
    chunk_words = chunk.lower().split()
    if not chunk_words:
        return 0.0
    tf = sum(1 for w in chunk_words if w in query_words) / len(chunk_words)
    idf = math.log(1 + len(query_words))
    return tf * idf


def chunk_text(text: str, chunk_size: int = 300) -> list[str]:
    """Split text into overlapping character chunks."""
    chunks = []
    step = chunk_size - 50  # 50-char overlap
    for i in range(0, len(text), step):
        chunk = text[i : i + chunk_size]
        if chunk.strip():
            chunks.append(chunk)
    return chunks


def select_top_chunks(chunks: list[str], query: str, top_k: int = 5) -> list[str]:
    scored = [(chunk, simple_tfidf_score(chunk, query)) for chunk in chunks]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in scored[:top_k]]


async def agent_with_chunk_ranking(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]
    tools = [
        {
            "name": "fetch_documentation",
            "description": "Fetch API documentation",
            "input_schema": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        }
    ]

    while True:
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                # Simulate a large documentation page
                raw = " ".join(
                    f"Section {i}: This covers {block.input['topic']} aspect {i}. "
                    + "Details follow with code examples and explanations. " * 10
                    for i in range(50)
                )
                chunks = chunk_text(raw)
                top = select_top_chunks(chunks, user_query, top_k=5)
                condensed = "\n---\n".join(top)
                condensed = f"[Top {len(top)}/{len(chunks)} chunks by relevance]\n{condensed}"
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": condensed}
                )

        messages.append({"role": "user", "content": results})
```

## Solution 5: Type-Aware Compressor Pipeline

Apply different compression strategies based on the detected result type (JSON list, log lines, HTML, plain text).

```python
import json
import re
from typing import Callable

import anthropic

client = anthropic.AsyncAnthropic()

CompressorFn = Callable[[str], str]


def compress_json_list(raw: str, max_items: int = 10) -> str:
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            total = len(data)
            sample = data[:max_items]
            return f"[JSON list: {total} items, showing {max_items}]\n{json.dumps(sample, indent=2)}"
    except json.JSONDecodeError:
        pass
    return raw


def compress_log_lines(raw: str, max_lines: int = 30) -> str:
    lines = raw.strip().splitlines()
    total = len(lines)
    if total <= max_lines:
        return raw
    # Keep first 10, last 10, and sample from middle
    head = lines[:10]
    tail = lines[-10:]
    step = max(1, (total - 20) // 10)
    middle = lines[10:-10:step][:10]
    kept = head + [f"... ({total - len(head) - len(tail)} lines omitted) ..."] + middle + tail
    return "\n".join(kept)


def compress_html(raw: str) -> str:
    # Strip tags, collapse whitespace
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:3000] + ("…" if len(text) > 3000 else "")


def compress_plain(raw: str, max_chars: int = 2000) -> str:
    if len(raw) <= max_chars:
        return raw
    return raw[:max_chars] + f"… [truncated {len(raw) - max_chars} chars]"


def detect_and_compress(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        result = compress_json_list(raw)
        if result != raw:
            return result
    if re.search(r"\d{4}-\d{2}-\d{2}.*\[(ERROR|WARN|INFO)\]", raw):
        return compress_log_lines(raw)
    if "<html" in raw.lower() or stripped.startswith("<"):
        return compress_html(raw)
    return compress_plain(raw)


async def typed_compressor_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    tools = [
        {
            "name": "fetch_data",
            "description": "Fetch arbitrary data",
            "input_schema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "format": {"type": "string", "enum": ["json", "logs", "html", "text"]},
                },
                "required": ["source", "format"],
            },
        }
    ]

    while True:
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                fmt = block.input.get("format", "text")
                if fmt == "json":
                    raw = json.dumps([{"id": i, "v": i * 3.14, "x": "y" * 200} for i in range(500)])
                elif fmt == "logs":
                    raw = "\n".join(
                        f"2024-01-{i % 28 + 1:02d} 12:00:00 [{'ERROR' if i % 7 == 0 else 'INFO'}] event_{i}"
                        for i in range(1000)
                    )
                elif fmt == "html":
                    raw = "<html>" + "<p>content " * 5000 + "</p></html>"
                else:
                    raw = "text data " * 3000

                condensed = detect_and_compress(raw)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": condensed}
                )

        messages.append({"role": "user", "content": results})
```

## Solution 6: Budget-Gated Summarizer with Inline Token Accounting

Track token budget across the conversation; increase summarization aggressiveness as the budget fills up.

```python
import anthropic

client = anthropic.AsyncAnthropic()

MODEL = "claude-sonnet-4-6"
SUMMARIZER_MODEL = "claude-haiku-4-5-20251001"
CONTEXT_LIMIT = 180_000  # tokens
BUDGET_TIERS = [
    (0.50, 0),       # < 50% full: pass raw
    (0.70, 500),     # 50-70%: summarize to 500 tokens
    (0.85, 200),     # 70-85%: summarize to 200 tokens
    (1.00, 100),     # > 85%: summarize to 100 tokens
]


def budget_tier(used: int) -> int:
    ratio = used / CONTEXT_LIMIT
    for threshold, max_tokens in BUDGET_TIERS:
        if ratio < threshold:
            return max_tokens  # 0 means pass through
    return 100


async def budget_aware_summarize(
    tool_name: str, raw: str, used_tokens: int
) -> tuple[str, int]:
    """Returns (condensed_text, approx_tokens_used_by_condensed)."""
    max_tok = budget_tier(used_tokens)
    approx_raw = len(raw) // 4

    if max_tok == 0 or approx_raw <= max_tok:
        return raw, approx_raw

    resp = await client.messages.create(
        model=SUMMARIZER_MODEL,
        max_tokens=max_tok,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Summarize the tool result from '{tool_name}' in ≤{max_tok // 2} words. "
                    f"Only keep essential facts.\n\n{raw[:8000]}"
                ),
            }
        ],
    )
    summary = resp.content[0].text
    return f"[Summarized to {max_tok} tok budget] {summary}", len(summary) // 4


async def budget_agent(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]
    used_tokens = len(user_query) // 4
    tools = [
        {
            "name": "query_warehouse",
            "description": "Query the data warehouse",
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string"},
                },
                "required": ["sql"],
            },
        }
    ]

    for _ in range(15):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )
        used_tokens += resp.usage.input_tokens + resp.usage.output_tokens

        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                raw = "row_id,value,description\n" + "\n".join(
                    f"{i},{i * 1.5},{'description text ' * 20}" for i in range(1000)
                )
                condensed, condensed_tokens = await budget_aware_summarize(
                    block.name, raw, used_tokens
                )
                used_tokens += condensed_tokens
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": condensed}
                )

        messages.append({"role": "user", "content": results})

    return "Budget exhausted."
```

## Comparison

| Solution | Summarization trigger | Cost | Fidelity | Best for |
|---|---|---|---|---|
| **LLM size gate** | Token threshold | Low (Haiku) | High | General purpose |
| **Template extraction** | Always | Zero (no LLM) | Medium | Structured data |
| **Rolling summary** | Always | Low (Haiku) | Medium | Long multi-step tasks |
| **Chunk ranking** | Always | Zero | Medium | Large text/docs |
| **Type-aware compressor** | Always | Zero | Medium | Mixed result types |
| **Budget-gated** | Context fill ratio | Adaptive | Adaptive | Long conversations |

Start with **template extraction** (Solution 2) for structured tool results — zero extra API cost. Add **LLM size gate** (Solution 1) as a fallback for unstructured payloads. Escalate to **budget-gated** (Solution 6) when conversations run long and you need automatic aggressiveness scaling.
