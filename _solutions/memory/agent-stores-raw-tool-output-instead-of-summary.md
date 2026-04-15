---
layout: solution
title: "Agent stores raw tool output instead of summary"
category: memory
description: "Agent persists the full API response or file contents as a memory entry rather than extracting and storing only the key facts. Memory balloons with verbose noise, retrieval quality drops, and future context injections waste tokens."
tags: [memory, tool-failure, token-cost, summarization, retrieval]
---

## Symptom

The agent's memory store grows rapidly. Inspecting it reveals entries like a full 50-line JSON API response, a complete file listing, or a verbatim database row dump — stored exactly as the tool returned them. When these memories are retrieved and injected into future prompts, they consume hundreds of tokens to deliver information that could be expressed in two sentences.

## Root Cause

The tool result is passed directly to the memory write call without a summarization step. The agent conflates "I got a result" with "I should remember all of it". The relevant signal — "the user's email is alice@example.com" — is buried in a 2 KB JSON blob that also contains timestamps, metadata, and irrelevant fields.

## Fix

Extract a compact summary from every tool result before storing it. The summary should answer: "what fact does future-me need from this result?" — not reproduce the raw data.

---

### Option 1 — LLM-based summarizer before memory write

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

# In-memory store (replace with a real vector DB in production)
MEMORY: list[dict] = []


def summarize_for_memory(tool_name: str, tool_result: str) -> str:
    """Ask a cheap model to extract the key facts from a tool result."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=(
            "You extract key facts from tool results for long-term memory storage. "
            "Respond with 1–3 concise sentences containing only the facts that would "
            "be useful to recall in a future conversation. "
            "Omit metadata, timestamps, and formatting noise."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Tool: {tool_name}\n"
                    f"Result:\n{tool_result[:3000]}"  # cap input to summarizer
                ),
            }
        ],
    )
    return response.content[0].text.strip()


def store_memory(tool_name: str, tool_result: str, user_id: str) -> None:
    summary = summarize_for_memory(tool_name, tool_result)
    entry = {
        "user_id": user_id,
        "tool_name": tool_name,
        "summary": summary,
        "raw_size_chars": len(tool_result),
        "summary_size_chars": len(summary),
    }
    MEMORY.append(entry)
    compression = round(len(summary) / len(tool_result) * 100, 1) if tool_result else 0
    print(f"Stored memory: {len(summary)} chars (was {len(tool_result)} — {compression}% of original)")


# Example usage
raw_api_response = json.dumps({
    "user": {
        "id": "usr_12345",
        "email": "alice@example.com",
        "name": "Alice Johnson",
        "created_at": "2024-01-15T08:23:11Z",
        "last_login": "2026-04-14T19:42:00Z",
        "preferences": {"theme": "dark", "language": "en-US", "notifications": True},
        "subscription": {"plan": "pro", "expires": "2027-01-15"},
        "metadata": {"source": "oauth_google", "verified": True},
    }
}, indent=2)

store_memory("get_user_profile", raw_api_response, user_id="session_001")
```

**Expected Token Savings:** A 500-token raw API response becomes a 30-token summary; the summarizer costs ~150 Haiku tokens but saves that on every future retrieval.
**Environment:** Agents that persist tool results for cross-session recall; the Haiku summarizer pays for itself after 1–2 retrievals.

---

### Option 2 — Field-extraction filter: keep only declared important fields

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

# Declare which fields matter for each tool's output
IMPORTANT_FIELDS: dict[str, list[str]] = {
    "get_user_profile": ["email", "name", "subscription.plan"],
    "search_web": ["title", "url", "snippet"],
    "query_database": ["id", "status", "created_at"],
    "get_weather": ["temperature", "condition", "humidity"],
    "list_files": ["name", "size", "modified"],
}


def extract_fields(data: Any, field_path: str) -> Any:
    """Traverse dot-notation field paths (e.g. 'subscription.plan')."""
    parts = field_path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def compact_result(tool_name: str, raw_result: str) -> str:
    """Extract only the declared important fields from a JSON tool result."""
    fields = IMPORTANT_FIELDS.get(tool_name)
    if not fields:
        # No filter defined — apply a hard character cap
        return raw_result[:400] + ("..." if len(raw_result) > 400 else "")

    try:
        data = json.loads(raw_result)
        # Handle both direct objects and {"data": {...}} wrappers
        if "data" in data and isinstance(data["data"], dict):
            data = data["data"]
        if "user" in data and isinstance(data["user"], dict):
            data = data["user"]

        extracted = {field: extract_fields(data, field) for field in fields}
        extracted = {k: v for k, v in extracted.items() if v is not None}
        return json.dumps(extracted, separators=(",", ":"))

    except (json.JSONDecodeError, AttributeError):
        # Not JSON — truncate to first meaningful line
        return raw_result.splitlines()[0][:300]


MEMORY: list[dict] = []


def store_compact_memory(tool_name: str, raw_result: str, context: str = "") -> None:
    compact = compact_result(tool_name, raw_result)
    MEMORY.append({
        "tool": tool_name,
        "memory": compact,
        "context": context,
    })
    print(f"[{tool_name}] {len(raw_result)} → {len(compact)} chars")
```

**Expected Token Savings:** 70–95 % depending on response verbosity; zero extra LLM calls since extraction is deterministic.
**Environment:** Agents with predictable, schema-stable API responses; define the field list once and it applies to every call of that tool.

---

### Option 3 — Two-phase: store compact now, retrieve-and-expand on demand

```python
import anthropic
import json
import hashlib

client = anthropic.Anthropic(api_key="sk-live-...")

# Two-tier storage: compact summaries in fast memory, full results in cold storage
COMPACT_MEMORY: dict[str, str] = {}     # key → compact summary
COLD_STORAGE: dict[str, str] = {}       # key → full raw result


def store_with_tiering(tool_name: str, raw_result: str, key: str | None = None) -> str:
    """
    Store compact summary in fast memory; full result in cold storage.
    Returns the storage key.
    """
    storage_key = key or hashlib.sha256(raw_result.encode()).hexdigest()[:12]

    # Cold storage: always store full result
    COLD_STORAGE[storage_key] = raw_result

    # Fast memory: extract compact representation
    compact = _make_compact(tool_name, raw_result)
    COMPACT_MEMORY[storage_key] = compact

    print(f"Stored: compact={len(compact)}chars, full={len(raw_result)}chars, key={storage_key}")
    return storage_key


def _make_compact(tool_name: str, raw: str) -> str:
    """Rule-based compact extraction — no LLM required."""
    try:
        data = json.loads(raw)
        # Keep only scalar values up to depth 2
        flat: dict = {}
        for k, v in data.items():
            if isinstance(v, (str, int, float, bool)):
                flat[k] = v
            elif isinstance(v, dict):
                for kk, vv in v.items():
                    if isinstance(vv, (str, int, float, bool)):
                        flat[f"{k}.{kk}"] = vv
        compact = json.dumps(flat, separators=(",", ":"))
        return compact[:500]
    except Exception:
        return raw[:300]


def retrieve(key: str, expand: bool = False) -> str:
    """Return compact summary by default; full result if expand=True."""
    if expand:
        return COLD_STORAGE.get(key, "Not found in cold storage")
    return COMPACT_MEMORY.get(key, "Not found in memory")


def run_agent(user_message: str) -> str:
    # Simulate a large API result
    big_result = json.dumps({"user": {"id": "u1", "email": "bob@example.com", "name": "Bob", "score": 98}, "meta": {"page": 1, "total": 1}})
    key = store_with_tiering("get_user", big_result)

    # Inject compact version into context
    memory_context = f"Memory [{key}]: {retrieve(key)}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"Relevant memory:\n{memory_context}",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Fast-path retrievals use compact summaries (~50 tokens); the model can request `expand=True` via a tool only when it genuinely needs the full data.
**Environment:** Agents with mixed retrieval patterns — most lookups need a summary, occasional deep analysis needs the full result.

---

### Option 4 — Template-based summary formatter per tool type

```python
import anthropic
import json
from string import Template
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

MEMORY: list[str] = []

# Memory templates: extract values and format as a short human-readable sentence
MEMORY_TEMPLATES: dict[str, tuple[list[str], str]] = {
    # (required_fields, template_string)
    "get_user_profile": (
        ["name", "email"],
        "User: $name ($email), plan: $plan",
    ),
    "search_web": (
        ["title", "url"],
        "Found: '$title' at $url",
    ),
    "get_weather": (
        ["location", "temperature", "condition"],
        "Weather in $location: $temperature°C, $condition",
    ),
    "query_database": (
        ["id", "status"],
        "Record $id has status '$status'",
    ),
}


def _safe_get(data: dict, key: str, default: str = "unknown") -> str:
    """Nested get with dot notation."""
    parts = key.split(".")
    cur: Any = data
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p, default)
        else:
            return default
    return str(cur) if cur is not None else default


def format_memory(tool_name: str, raw_result: str) -> str:
    spec = MEMORY_TEMPLATES.get(tool_name)
    if spec is None:
        return raw_result[:200]

    required_fields, template_str = spec

    try:
        data = json.loads(raw_result)
        # Flatten common wrappers
        for wrapper in ("user", "data", "result"):
            if wrapper in data and isinstance(data[wrapper], dict):
                data = {**data, **data[wrapper]}

        values = {f.replace(".", "_"): _safe_get(data, f) for f in required_fields}
        # Also add optional fields used in template
        values.setdefault("plan", _safe_get(data, "subscription.plan"))
        t = Template(template_str.replace(".", "_"))
        return t.safe_substitute(values)

    except Exception:
        return raw_result[:200]


def store_memory(tool_name: str, raw_result: str) -> None:
    formatted = format_memory(tool_name, raw_result)
    MEMORY.append(formatted)
    print(f"Stored: '{formatted}' (was {len(raw_result)} chars)")
```

**Expected Token Savings:** Template-formatted memories are 20–60 characters; zero LLM cost since formatting is deterministic.
**Environment:** Agents with a fixed set of tools whose output schemas are stable; templates are authored once at development time.

---

### Option 5 — Async batch summarizer for high-throughput pipelines

```python
import anthropic
import asyncio
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

MEMORY: list[dict] = []
_summarize_semaphore = asyncio.Semaphore(5)   # max 5 concurrent summarizations


@dataclass
class MemoryEntry:
    tool_name: str
    summary: str
    original_chars: int


async def summarize_async(tool_name: str, raw_result: str) -> str:
    async with _summarize_semaphore:
        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=(
                "Extract the key facts from this tool result as 1–2 sentences. "
                "Include only actionable information. Omit metadata and formatting."
            ),
            messages=[{"role": "user", "content": f"{tool_name}: {raw_result[:2000]}"}],
        )
        return response.content[0].text.strip()


async def store_memories_batch(
    tool_results: list[tuple[str, str]],  # (tool_name, raw_result)
) -> list[MemoryEntry]:
    """Summarize and store a batch of tool results concurrently."""
    summaries = await asyncio.gather(*[
        summarize_async(name, result)
        for name, result in tool_results
    ])
    entries: list[MemoryEntry] = []
    for (name, raw), summary in zip(tool_results, summaries):
        entry = MemoryEntry(tool_name=name, summary=summary, original_chars=len(raw))
        MEMORY.append({"tool": entry.tool_name, "summary": entry.summary})
        entries.append(entry)
    return entries


async def main() -> None:
    # Simulate a parallel multi-tool turn
    tool_results = [
        ("get_user_profile", '{"id":"u1","email":"carol@example.com","name":"Carol","subscription":{"plan":"enterprise"}}'),
        ("query_database", "[" + ",".join(f'{{"id":{i},"status":"active","score":{i*10}}}' for i in range(50)) + "]"),
        ("search_web", '{"results":[{"title":"Q3 Report","url":"https://example.com/q3","snippet":"Revenue up 23% in Q3 2026."}]}'),
    ]
    entries = await store_memories_batch(tool_results)
    for e in entries:
        print(f"{e.tool_name}: {e.original_chars}→{len(e.summary)} chars: {e.summary}")


asyncio.run(main())
```

**Expected Token Savings:** Parallel summarization means a 5-tool turn adds ~750 Haiku tokens but converts 10K characters of noise into 300 characters of signal.
**Environment:** Multi-tool async agents processing several results per turn; the semaphore prevents summarization from overwhelming the API.

---

### Option 6 — Memory schema enforcer: reject oversized entries at write time

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_MEMORY_CHARS = 300
MEMORY: list[dict] = []


class MemoryWriteError(ValueError):
    pass


def validated_memory_write(
    content: str,
    tool_name: str,
    auto_truncate: bool = False,
) -> dict:
    """
    Write a memory entry, enforcing a size limit.
    If auto_truncate=True, silently truncate. Otherwise raise.
    """
    if len(content) > MAX_MEMORY_CHARS:
        if auto_truncate:
            content = content[:MAX_MEMORY_CHARS] + "…"
        else:
            raise MemoryWriteError(
                f"Memory entry for tool '{tool_name}' is {len(content)} chars "
                f"(max {MAX_MEMORY_CHARS}). Summarize before storing."
            )
    entry = {"tool": tool_name, "content": content, "chars": len(content)}
    MEMORY.append(entry)
    return entry


def process_tool_result(tool_name: str, raw_result: str) -> str:
    """
    Try to store result directly. If too large, ask the model to summarize.
    """
    try:
        entry = validated_memory_write(raw_result, tool_name, auto_truncate=False)
        return entry["content"]
    except MemoryWriteError:
        # Result is too large — summarize with a cheap model
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"Summarize this {tool_name} result in under 200 characters, "
                    f"keeping only the most important fact:\n\n{raw_result[:2000]}"
                ),
            }],
        )
        summary = response.content[0].text.strip()[:MAX_MEMORY_CHARS]
        entry = validated_memory_write(summary, tool_name, auto_truncate=True)
        return entry["content"]


# Comparison table
# | Option | Summarization | LLM Cost | Best For |
# |--------|--------------|----------|---------|
# | 1 LLM summarizer | Haiku per result | ~150 tok/result | Variable schemas |
# | 2 Field extractor | None | 0 | Stable JSON APIs |
# | 3 Two-tier storage | None (compact inline) | 0 | Mixed access patterns |
# | 4 Template formatter | None | 0 | Fixed output schemas |
# | 5 Async batch | Haiku per result | ~150 tok/result | Multi-tool turns |
# | 6 Schema enforcer | Haiku on violation | 0 or ~150 tok | Catch-all gate |
```

**Expected Token Savings:** The 300-char limit means no memory entry ever costs more than ~75 tokens to inject; the enforcement gate ensures the policy is never accidentally bypassed.
**Environment:** Any agent with a memory store; the enforcer acts as a last-resort gate that catches oversized entries regardless of which code path created them.
