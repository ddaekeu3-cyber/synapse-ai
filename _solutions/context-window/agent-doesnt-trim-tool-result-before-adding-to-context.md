---
layout: solution
title: "Agent doesn't trim tool result before adding to context"
category: context-window
description: "Agent appends raw tool results — full HTML pages, 10,000-line JSON blobs, entire database dumps — to the conversation context, exhausting the context window within a few tool calls."
tags: [context-window, tool-result, trimming, truncation, token-management, summarization]
---

## Symptom

After 3–5 tool calls, the agent hits a context length error or starts losing early conversation history. Inspection of the message list reveals tool results like:

```json
{"role": "user", "content": [{"type": "tool_result", "content": "<!DOCTYPE html><html>...[82,000 tokens of HTML]..."}]}
```

The agent was asked to "check the homepage" and faithfully stored the entire rendered HTML. The context window fills before the model can synthesise a useful answer.

## Root Cause

Tool handlers return raw API responses, file contents, or web page bodies without any size check. The Anthropic SDK adds these as-is to the `messages` array. There is no automatic truncation — each oversized tool result permanently occupies context space for the rest of the conversation. The problem compounds: once one large result is added, every subsequent call becomes more expensive and the window available for the model's reasoning shrinks.

---

## Option 1 — Hard truncation with byte limit

**Cap every tool result at a fixed character limit before inserting it into context.**

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_RESULT_CHARS = 4_000   # ~1,000 tokens


def truncate_result(content: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(content) <= limit:
        return content
    kept = content[:limit]
    trimmed = len(content) - limit
    return kept + f"\n\n[... {trimmed:,} characters truncated ...]"


FETCH_TOOL = {
    "name": "fetch_url",
    "description": "Fetch the text content of a URL.",
    "input_schema": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
}


def fetch_url_handler(url: str) -> str:
    # Simulate a large web page response
    raw = f"<html><body>" + "Lorem ipsum dolor sit amet. " * 3000 + "</body></html>"
    return truncate_result(raw)


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[FETCH_TOOL],
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            raw_result = fetch_url_handler(tc.input["url"])
            trimmed = truncate_result(raw_result)

            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": trimmed,
                }],
            })
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Summarise the content at https://example.com/docs"))
```

**Expected Token Savings:** A single uncapped HTML page can consume 80,000+ tokens. Capping at 4,000 chars saves ~95% of tool-result tokens for web-fetching agents — the most impactful single change for URL-fetching or file-reading agents.

**Environment:** Any agent using web-fetch or file-read tools; zero dependencies, immediate fix.

---

## Option 2 — Content-type aware extraction (HTML → text, JSON → key fields)

**Strip HTML tags and extract only relevant fields from JSON before inserting the result.**

```python
import json
import re
import anthropic

client = anthropic.Anthropic()


def strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_json_summary(data, max_depth: int = 2, max_items: int = 5) -> dict:
    """Recursively summarise a JSON object, keeping only top levels."""
    if isinstance(data, dict):
        result = {}
        for i, (k, v) in enumerate(data.items()):
            if i >= max_items:
                result["..."] = f"({len(data) - max_items} more keys)"
                break
            if max_depth > 0:
                result[k] = extract_json_summary(v, max_depth - 1, max_items)
            else:
                result[k] = f"<{type(v).__name__}>"
        return result
    elif isinstance(data, list):
        preview = [extract_json_summary(v, max_depth - 1, max_items) for v in data[:max_items]]
        if len(data) > max_items:
            preview.append(f"... ({len(data) - max_items} more items)")
        return preview
    return data


def smart_trim(content: str, content_type: str = "auto", max_chars: int = 3_000) -> str:
    if content_type == "html" or (content_type == "auto" and content.lstrip().startswith("<")):
        content = strip_html(content)

    if content_type == "json" or (content_type == "auto" and content.lstrip().startswith(("{", "["))):
        try:
            data = json.loads(content)
            content = json.dumps(extract_json_summary(data), indent=2)
        except json.JSONDecodeError:
            pass

    if len(content) > max_chars:
        content = content[:max_chars] + f"\n[truncated — {len(content)-max_chars:,} chars omitted]"

    return content


FETCH_TOOL = {
    "name": "fetch_url",
    "description": "Fetch a URL. Returns cleaned text (HTML stripped, JSON summarised).",
    "input_schema": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
}


def fetch_url_handler(url: str) -> str:
    # Simulate HTML response
    raw = "<html><body><h1>Docs</h1><p>" + "Content text. " * 2000 + "</p></body></html>"
    return smart_trim(raw, content_type="html")


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[FETCH_TOOL],
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            result = fetch_url_handler(tc.input["url"])
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("What does https://api.example.com/data return?"))
```

**Expected Token Savings:** HTML stripping reduces a typical web page from 80,000 tokens to ~2,000 tokens — 97% reduction. JSON field extraction reduces a large API response by 80–95% depending on nesting depth.

**Environment:** Web-browsing agents and API-calling agents; stdlib only (`re`, `json`).

---

## Option 3 — LLM-powered summarisation of large tool results

**When a result exceeds a threshold, ask a fast cheap model to summarise it before storing in context.**

```python
import anthropic

client = anthropic.Anthropic()

SUMMARISE_THRESHOLD = 2_000    # chars before we summarise
SUMMARISE_TARGET    = 500      # chars in the summary


def summarise_result(tool_name: str, raw_result: str, task_context: str) -> str:
    """Use haiku to summarise a large tool result."""
    if len(raw_result) <= SUMMARISE_THRESHOLD:
        return raw_result

    prompt = (
        f"The tool '{tool_name}' returned the following output. "
        f"The agent's task is: {task_context}\n\n"
        f"Summarise the output in ≤ {SUMMARISE_TARGET} characters, "
        f"keeping only information relevant to the task.\n\n"
        f"OUTPUT:\n{raw_result[:20_000]}"   # cap input to summariser
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    summary = resp.content[0].text
    return f"[Summarised from {len(raw_result):,} chars]\n{summary}"


SEARCH_TOOL = {
    "name": "web_search",
    "description": "Search the web and return results.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


def web_search_handler(query: str) -> str:
    # Simulate large search results
    return "\n".join([
        f"Result {i}: " + "Detailed information about the query result. " * 50
        for i in range(10)
    ])


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[SEARCH_TOOL],
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            raw = web_search_handler(tc.input["query"])
            result = summarise_result("web_search", raw, user_message)

            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Find the latest Python 3.13 release notes."))
```

**Expected Token Savings:** Summarisation reduces a 10,000-token search result to ~130 tokens in context. The haiku summarisation call costs ~500 input + ~130 output tokens — net saving of ~9,370 tokens per tool call.

**Environment:** Agents with multi-step search or document retrieval workflows; most effective when tool results are verbose but only a small fraction is relevant.

---

## Option 4 — Sliding window that drops old tool results

**Keep only the N most recent tool results in context; replace older ones with a one-line summary.**

```python
import json
from typing import Any
import anthropic

client = anthropic.Anthropic()

MAX_TOOL_RESULTS_IN_CONTEXT = 3


def compact_tool_result(content: list[dict] | str) -> str:
    """Reduce a tool result to a one-line summary for the archive."""
    if isinstance(content, list):
        text = " ".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    else:
        text = str(content)
    preview = text[:120].replace("\n", " ")
    return f"[archived tool result: {preview}…]"


def trim_tool_results(messages: list[dict]) -> list[dict]:
    """Replace all but the last MAX_TOOL_RESULTS_IN_CONTEXT tool results with summaries."""
    tool_result_indices = []
    for i, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for item in msg["content"]:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_result_indices.append(i)
                    break

    # Archive older results
    to_compact = tool_result_indices[:-MAX_TOOL_RESULTS_IN_CONTEXT]
    result = []
    for i, msg in enumerate(messages):
        if i in to_compact:
            new_content = []
            for item in msg["content"]:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    new_content.append({
                        "type": "tool_result",
                        "tool_use_id": item["tool_use_id"],
                        "content": compact_tool_result(item.get("content", "")),
                    })
                else:
                    new_content.append(item)
            result.append({**msg, "content": new_content})
        else:
            result.append(msg)
    return result


CALC_TOOL = {
    "name": "calculate",
    "description": "Run a calculation and return detailed intermediate steps.",
    "input_schema": {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    },
}


def calculate_handler(expression: str) -> str:
    # Simulate verbose output
    return "\n".join([f"Step {i}: intermediate result = {i * 42}" for i in range(100)])


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        messages = trim_tool_results(messages)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[CALC_TOOL],
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            result = calculate_handler(tc.input["expression"])
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Calculate compound interest for 5 different scenarios."))
```

**Expected Token Savings:** Retaining only 3 recent tool results instead of all of them reduces context growth from linear to constant — for a 20-tool-call session, saves ~17 full tool results worth of tokens.

**Environment:** Long multi-step agent sessions where early tool results are no longer needed for later reasoning.

---

## Option 5 — Structured extraction: return only requested fields

**Instead of returning the full API response, parse it and return only the fields the model asked for.**

```python
import json
import anthropic

client = anthropic.Anthropic()

# Simulated large API response
def call_weather_api(city: str) -> dict:
    return {
        "city": city,
        "temperature": {"celsius": 22, "fahrenheit": 71.6},
        "humidity": 65,
        "wind": {"speed_kmh": 15, "direction": "NE", "gusts_kmh": 25},
        "forecast": [{"day": f"Day {i}", "high": 20 + i, "low": 15 + i, "desc": "Partly cloudy"} for i in range(7)],
        "air_quality": {"aqi": 42, "pm25": 8.2, "pm10": 12.1, "o3": 38, "no2": 12},
        "uv_index": 6,
        "sunrise": "06:32",
        "sunset": "19:48",
        "moon_phase": "waxing crescent",
        "pressure_hpa": 1015,
        "visibility_km": 20,
        "dew_point": 12,
        "raw_station_data": "A" * 50_000,  # 50k chars of raw telemetry
    }


WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get weather for a city. Specify which fields you need.",
    "input_schema": {
        "type": "object",
        "properties": {
            "city":   {"type": "string"},
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Fields to return: temperature, humidity, wind, forecast, air_quality, uv_index",
            },
        },
        "required": ["city"],
    },
}

ALLOWED_FIELDS = {"temperature", "humidity", "wind", "forecast", "air_quality", "uv_index", "sunrise", "sunset"}


def get_weather_handler(city: str, fields: list[str] | None = None) -> str:
    raw = call_weather_api(city)

    if fields:
        wanted = set(fields) & ALLOWED_FIELDS
        result = {k: raw[k] for k in wanted if k in raw}
    else:
        # Default: return safe subset, never raw_station_data
        result = {k: raw[k] for k in ALLOWED_FIELDS if k in raw}

    # Limit forecast to 3 days
    if "forecast" in result:
        result["forecast"] = result["forecast"][:3]

    return json.dumps(result)


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=[WEATHER_TOOL],
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            result = get_weather_handler(
                tc.input["city"],
                tc.input.get("fields"),
            )
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("What's the temperature and UV index in Tokyo?"))
```

**Expected Token Savings:** Field filtering reduces a 50,000-token raw API response to ~200 tokens of relevant fields — 99.6% reduction. The model learns to request only what it needs via the `fields` parameter.

**Environment:** Agents calling data-rich APIs (weather, finance, analytics); especially effective when the model's question targets a small subset of available fields.

---

## Option 6 — Token-counted trimming with `tiktoken` estimates

**Measure tool result size in tokens before inserting; trim to a token budget rather than a character limit.**

```python
import re
import anthropic

client = anthropic.Anthropic()


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


def trim_to_token_budget(content: str, max_tokens: int = 1_000) -> str:
    """Trim content to fit within max_tokens (estimated)."""
    estimated = estimate_tokens(content)
    if estimated <= max_tokens:
        return content

    # Binary search for the right character count
    target_chars = max_tokens * 4
    trimmed = content[:target_chars]
    removed_tokens = estimated - estimate_tokens(trimmed)
    return trimmed + f"\n\n[... ~{removed_tokens:,} tokens omitted ...]"


def build_trimmed_tool_result(tool_use_id: str, raw_content: str, budget: int = 1_000) -> dict:
    trimmed = trim_to_token_budget(raw_content, budget)
    actual = estimate_tokens(trimmed)
    print(f"  Tool result: {estimate_tokens(raw_content):,} → {actual:,} estimated tokens")
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": trimmed,
    }


FILE_TOOL = {
    "name": "read_file",
    "description": "Read a file's contents.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}


def read_file_handler(path: str) -> str:
    # Simulate a large log file
    return "\n".join([f"2024-01-{i:02d} ERROR: Something went wrong in module {i}" for i in range(1, 1001)])


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[FILE_TOOL],
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            raw = read_file_handler(tc.input["path"])
            tool_result = build_trimmed_tool_result(tc.id, raw, budget=800)

            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [tool_result]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Find errors in /var/log/app.log"))
```

**Expected Token Savings:** Token-budget trimming ensures every tool result costs a predictable amount — prevents any single result from consuming more than ~2% of a 100k-token context window. For log-reading or file-reading agents, typical saving is 90–98% of tool-result tokens.

**Environment:** Agents reading log files, source code, or any large text files; no extra dependencies beyond the stdlib.

---

## Comparison

| Option | Method | Preserves Structure | Needs Extra LLM Call | Complexity |
|--------|--------|--------------------|--------------------|------------|
| 1. Hard truncation | Character limit | No | No | Very Low |
| 2. Content-type extraction | HTML strip + JSON prune | Partial | No | Low |
| 3. LLM summarisation | Task-aware summary | No | Yes (haiku) | Medium |
| 4. Sliding window | Drop oldest results | No | No | Medium |
| 5. Field selection | API-level filtering | Yes | No | Low |
| 6. Token-budget trimming | Estimated token cap | No | No | Low |

**Recommended path:** Apply Option 1 (hard truncation) immediately as a safety net — prevents any runaway result. Then add Option 2 (content-type extraction) for HTML and JSON tools. Use Option 3 (LLM summarisation) only for results where the content is semantically complex and a raw truncation would lose critical information.
