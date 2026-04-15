---
layout: solution
title: "Agent Includes Unnecessary Context in Every Request"
category: token-cost
description: "Agent sends full conversation history, verbose tool results, and irrelevant background on every call, inflating costs 3–10x."
tags: [token-cost, context-window, performance, prompt-engineering, cost-optimization]
---

## Symptom

Token costs grow quadratically with conversation length. A 20-turn conversation costs 10x more per turn than a 5-turn conversation, even though most of the history is no longer relevant. Tool results from 15 turns ago are still being sent verbatim. The system prompt contains a 3 000-word company wiki that's only relevant for one type of question.

## Root Cause

The default append-only history pattern sends every previous message on every call. Tool results, which can be large JSON payloads, accumulate in the history. System prompts often include background information for every possible question, rather than just what the current query needs. Without active context management, the token count of each request grows until it either hits the context limit or becomes prohibitively expensive.

## Fix

### Option 1 — Keep only the last N turns of history

```python
import anthropic

client = anthropic.Anthropic()

MAX_HISTORY_TURNS = 6   # keep 6 turns = 3 user + 3 assistant

def trim_history(history: list[dict], max_turns: int = MAX_HISTORY_TURNS) -> list[dict]:
    """Keep only the most recent turns, always keeping pairs intact."""
    if len(history) <= max_turns:
        return history
    # Ensure we start on a user message (maintain alternating pattern)
    trimmed = history[-max_turns:]
    if trimmed and trimmed[0]["role"] == "assistant":
        trimmed = trimmed[1:]
    removed = len(history) - len(trimmed)
    if removed:
        print(f"[context] trimmed {removed} old message(s) from history")
    return trimmed

def chat(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": user_message}]
    trimmed = trim_history(history)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=trimmed,
    )
    reply = response.content[0].text
    history = history + [{"role": "assistant", "content": reply}]
    return reply, history

history = []
for msg in [
    "My name is Alice.",
    "I'm building a FastAPI app.",
    "It uses PostgreSQL.",
    "Now explain dependency injection.",
    "Give me a code example.",
    "What about testing strategies?",
    "Can you summarise what we've covered?",
    "What was my name again?",  # tests if early context is gone
]:
    reply, history = chat(history, msg)
    sent_tokens = sum(len(m["content"]) // 4 for m in trim_history(history))
    print(f"User: {msg}")
    print(f"Agent: {reply[:80]} [~{sent_tokens} tokens sent]\n")
```

**Expected Token Savings:** Trimming to 6 turns from 20 reduces input tokens by 70% per call; savings compound as conversation grows.
**Environment:** All multi-turn agents; windowed history is the simplest cost-reduction step.

---

### Option 2 — Summarise and replace verbose tool results

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_TOOL_RESULT_CHARS = 500

def compress_tool_result(tool_name: str, result_json: str) -> str:
    """Replace large tool results with a compact summary."""
    if len(result_json) <= MAX_TOOL_RESULT_CHARS:
        return result_json

    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return result_json[:MAX_TOOL_RESULT_CHARS] + "…[truncated]"

    # Tool-specific compression rules
    if tool_name == "search_database" and "rows" in data:
        rows   = data["rows"]
        sample = rows[:3]
        return json.dumps({
            "total_rows": len(rows),
            "sample":     sample,
            "note":       f"Showing 3/{len(rows)} rows. Ask for more if needed.",
        }, ensure_ascii=False)

    if tool_name == "read_file" and "content" in data:
        content = data["content"]
        return json.dumps({
            "chars":   len(content),
            "preview": content[:300] + ("…" if len(content) > 300 else ""),
        })

    # Generic: summarise with Haiku
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": f"Summarise this {tool_name} result in 50 words:\n\n{result_json[:2000]}",
        }],
    )
    return json.dumps({"summary": response.content[0].text, "compressed": True})

def compress_history_tool_results(history: list[dict]) -> list[dict]:
    """Walk through history and compress old tool results."""
    compressed = []
    for msg in history:
        content = msg.get("content")
        if isinstance(content, list):
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    original = str(block.get("content", ""))
                    compacted = compress_tool_result("generic", original)
                    if compacted != original:
                        print(f"[context] tool result: {len(original)} → {len(compacted)} chars")
                    new_blocks.append({**block, "content": compacted})
                else:
                    new_blocks.append(block)
            compressed.append({**msg, "content": new_blocks})
        else:
            compressed.append(msg)
    return compressed

# Simulate a large tool result
large_result = json.dumps({
    "rows": [{"id": i, "name": f"User {i}", "email": f"u{i}@example.com"} for i in range(100)]
})
print(f"Original: {len(large_result)} chars")
compressed = compress_tool_result("search_database", large_result)
print(f"Compressed: {len(compressed)} chars")
print(compressed[:200])
```

**Expected Token Savings:** A 5 000-char DB result compressed to 200 chars saves ~1 200 tokens; savings multiply across every turn that follows.
**Environment:** Tool-using agents where results accumulate in history; compress after the turn that used the result.

---

### Option 3 — Dynamic system prompt: include only relevant sections

```python
import anthropic

client = anthropic.Anthropic()

# Section library — only include what's needed
SYSTEM_SECTIONS = {
    "core": "You are a helpful assistant for AcmeSoft customers.",

    "billing": """BILLING POLICY:
- Subscriptions renew on the 1st of each month.
- Refunds available within 30 days of purchase.
- Contact billing@acmesoft.com for invoice disputes.""",

    "technical": """TECHNICAL SUPPORT:
- Supported OS: Windows 10+, macOS 12+, Ubuntu 20.04+
- Minimum RAM: 8GB
- For API issues, check status.acmesoft.com""",

    "returns": """RETURN POLICY:
- Physical products: 30-day return window.
- Software licences: non-refundable after activation.
- Damaged goods: contact support within 7 days.""",

    "enterprise": """ENTERPRISE FEATURES:
- SSO via SAML 2.0 and OAuth 2.0
- Custom SLA available with 99.9% uptime guarantee
- Dedicated account manager for accounts > $10k/year""",
}

ROUTING_SYSTEM = """Classify this support query into ONE of: billing, technical, returns, enterprise, general.
Return exactly one word."""

def route_query(query: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        system=ROUTING_SYSTEM,
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text.strip().lower()

def build_system_prompt(query: str) -> str:
    route   = route_query(query)
    section = SYSTEM_SECTIONS.get(route, "")
    system  = SYSTEM_SECTIONS["core"]
    if section:
        system += f"\n\n{section}"
        print(f"[context] routing to section: {route!r} (+{len(section)} chars)")
    else:
        print(f"[context] general query — minimal system prompt")
    return system

def ask(query: str) -> str:
    system = build_system_prompt(query)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text

queries = [
    "When does my subscription renew?",
    "What OS does AcmeSoft support?",
    "Can I return a software licence?",
    "Do you support SSO?",
    "What are your business hours?",
]
for q in queries:
    print(f"\nQ: {q}")
    print(f"A: {ask(q)[:120]}")
```

**Expected Token Savings:** Routing to a 200-token section instead of sending a 3 000-token full manual saves ~2 800 tokens per request; 90%+ reduction on system prompt tokens.
**Environment:** Knowledge-base agents with large reference material; section routing is the highest-leverage system prompt optimisation.

---

### Option 4 — Prune irrelevant history with importance scoring

```python
import json
import anthropic

client = anthropic.Anthropic()

SCORE_SYSTEM = """Score each conversation message for relevance to the CURRENT QUERY on a scale 0-10.
10 = directly answers or provides context for the current query
5  = related background
0  = pleasantries, unrelated topics, already-resolved sub-tasks

Return JSON: {"scores": [int, ...]} — one score per message in order."""

def score_relevance(history: list[dict], current_query: str, max_to_score: int = 10) -> list[int]:
    to_score = history[-max_to_score:] if len(history) > max_to_score else history
    content  = json.dumps([{"role": m["role"], "content": str(m.get("content", ""))[:200]}
                           for m in to_score])
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=SCORE_SYSTEM,
        messages=[{"role": "user", "content": f"Current query: {current_query}\n\nMessages:\n{content}"}],
    )
    try:
        return json.loads(response.content[0].text.strip().lstrip("```json").rstrip("```").strip())["scores"]
    except Exception:
        return [5] * len(to_score)

def relevance_trim(history: list[dict], current_query: str,
                   keep_recent: int = 2, threshold: int = 4) -> list[dict]:
    if len(history) <= keep_recent:
        return history
    recent = history[-keep_recent:]
    older  = history[:-keep_recent]
    if not older:
        return recent

    scores = score_relevance(older, current_query)
    kept   = [m for m, s in zip(older, scores) if s >= threshold]
    print(f"[context] kept {len(kept)}/{len(older)} older messages (threshold={threshold})")
    return kept + recent

def chat(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    trimmed = relevance_trim(history, user_message)
    trimmed.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=trimmed,
    )
    reply = response.content[0].text
    history = history + [
        {"role": "user",      "content": user_message},
        {"role": "assistant", "content": reply},
    ]
    return reply, history

# Build a history with diverse topics
history = [
    {"role": "user",      "content": "What is your name?"},
    {"role": "assistant", "content": "I'm an AI assistant."},
    {"role": "user",      "content": "I'm building a REST API in Python."},
    {"role": "assistant", "content": "Great! I can help with that."},
    {"role": "user",      "content": "What's the weather like?"},
    {"role": "assistant", "content": "I don't have access to weather data."},
]
reply, history = chat(history, "How do I add JWT authentication to my REST API?")
print(f"Answer: {reply[:200]}")
```

**Expected Token Savings:** Relevance scoring drops 60–80% of irrelevant history; the scoring call costs ~80 tokens and saves hundreds per turn.
**Environment:** Long conversations that switch topics frequently; relevance pruning keeps context focused.

---

### Option 5 — Request-scoped context: build fresh context per query

```python
import anthropic

client = anthropic.Anthropic()

# Context blocks with token estimates
CONTEXT_LIBRARY = {
    "user_profile": {
        "tokens": 150,
        "content": "User: Alice, plan: Pro, join_date: 2023-01, timezone: EST",
    },
    "product_catalogue": {
        "tokens": 800,
        "content": "Products: AcmeSoft Basic $29/mo, Pro $79/mo, Enterprise $299/mo...",
    },
    "api_docs": {
        "tokens": 2000,
        "content": "API Reference: POST /v1/messages, GET /v1/users/{id}...",
    },
    "billing_faq": {
        "tokens": 400,
        "content": "Billing FAQ: Renewal on 1st, refunds within 30 days...",
    },
    "policies": {
        "tokens": 300,
        "content": "Policies: GDPR compliant, data retention 90 days...",
    },
}

TOKEN_BUDGET = 1000  # max tokens for context

def select_context(query: str) -> str:
    """Select relevant context blocks within token budget."""
    import re
    keywords = {
        "api":      ["api_docs"],
        "billing":  ["billing_faq"],
        "price":    ["product_catalogue"],
        "policy":   ["policies"],
        "account":  ["user_profile"],
    }
    # Determine which blocks are relevant
    needed: list[str] = ["user_profile"]   # always include
    for kw, blocks in keywords.items():
        if kw in query.lower():
            needed.extend(blocks)

    # Fill within budget
    selected = []
    used_tokens = 0
    for block_name in dict.fromkeys(needed):   # deduplicate
        block = CONTEXT_LIBRARY.get(block_name)
        if block and used_tokens + block["tokens"] <= TOKEN_BUDGET:
            selected.append(block["content"])
            used_tokens += block["tokens"]

    print(f"[context] selected {len(selected)} blocks (~{used_tokens} tokens)")
    return "\n\n".join(selected) if selected else ""

def ask(query: str) -> str:
    context = select_context(query)
    system  = f"You are AcmeSoft support.\n\n{context}" if context else "You are AcmeSoft support."
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text

queries = [
    "What's the price of the Pro plan?",
    "How do I call the messages API?",
    "When does my subscription renew?",
    "What's the weather today?",
]
for q in queries:
    print(f"\nQ: {q}")
    print(f"A: {ask(q)[:100]}")
```

**Expected Token Savings:** Context budget of 1 000 tokens vs. 3 750 (all blocks) = 73% system prompt reduction; budget enforced on every request.
**Environment:** Agents with large multi-domain knowledge bases; budget-aware context selection scales linearly.

---

### Option 6 — Message compactor: compress the full conversation periodically

```python
import anthropic

client = anthropic.Anthropic()

COMPACT_THRESHOLD_CHARS = 8000
COMPACT_SYSTEM = """Compress this conversation into a compact summary that preserves:
- All decisions made
- All technical details established (names, configs, preferences)
- All unresolved questions or open tasks
- The last 2 turns verbatim

Format:
[SUMMARY]
<bullet points>
[LAST 2 TURNS]
<verbatim>"""

def estimate_chars(history: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) for m in history)

def compact(history: list[dict]) -> list[dict]:
    if estimate_chars(history) < COMPACT_THRESHOLD_CHARS:
        return history

    transcript = "\n".join(
        f"{m['role'].upper()}: {str(m.get('content', ''))[:500]}"
        for m in history
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=COMPACT_SYSTEM,
        messages=[{"role": "user", "content": transcript}],
    )
    compacted_content = response.content[0].text
    compacted_history = [
        {"role": "user",      "content": f"[Compacted conversation]\n{compacted_content}"},
        {"role": "assistant", "content": "I have the compacted context from our conversation."},
    ]
    before = estimate_chars(history)
    after  = estimate_chars(compacted_history)
    print(f"[compact] {before:,} → {after:,} chars ({(1 - after/before)*100:.0f}% reduction)")
    return compacted_history

def chat(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    history = compact(history)
    history.append({"role": "user", "content": user_message})
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply, history

# Simulate a long conversation
history = []
topics = [
    "I'm building a microservices architecture.",
    "We're using Kubernetes for orchestration.",
    "The main service is written in Go.",
    "We need a service mesh — considering Istio.",
    "Auth will use JWT with RS256.",
    "Database: PostgreSQL with pgBouncer.",
    "Now, how should I design the API gateway?",
    "What about observability?",
    "How do I handle distributed tracing?",
]
for msg in topics:
    reply, history = chat(history, msg)
    print(f"User: {msg[:50]}")
    print(f"Agent: {reply[:60]} | history: {estimate_chars(history):,} chars\n")
```

**Expected Token Savings:** Compaction at 8 000 chars reduces to ~2 000 chars — 75% reduction; cost savings scale with conversation length.
**Environment:** Long coding sessions, extended planning conversations; compaction keeps costs bounded regardless of session length.

---

## Comparison

| Option | Mechanism | Information Lost? | Latency Added | Best For |
|---|---|---|---|---|
| 1. Window trim | Drop oldest turns | Some early context | None | Simple cost reduction |
| 2. Tool result compression | Summarise large results | Minimal | +1 call on compression | Tool-heavy agents |
| 3. Dynamic system prompt | Route to relevant section | No | +1 routing call | Large knowledge bases |
| 4. Relevance scoring | Score and filter history | Low-relevance | +1 scoring call | Topic-switching conversations |
| 5. Request-scoped context | Budget-aware selection | Possible | None | Multi-domain knowledge agents |
| 6. Conversation compactor | Periodic full compression | Low-signal detail | +1 compact call | Very long sessions |
