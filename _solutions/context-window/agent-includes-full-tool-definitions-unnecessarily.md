---
layout: solution
title: "Agent Includes Full Tool Definitions Unnecessarily"
category: context-window
description: "Agent sends the complete tool schema for every available tool on every API call, even when only one or two tools are relevant to the current task, wasting hundreds of input tokens per call."
tags: [context-window, token-cost, tool-use, schema, optimization, efficiency]
---

## Symptom

An agent has 15 tools defined: file operations, web search, database queries, email sending, calendar access, and more. Every API call sends all 15 tool schemas regardless of what the user asked. Each schema averages 120 tokens. That's 1,800 tokens of tool definitions per call — more than the user's message and system prompt combined. A simple "what's the weather?" question pays for 14 irrelevant tool schemas before the model even processes the actual question.

## Root Cause

Developers define a single static `tools` list and pass it to every API call. This is simple to implement but ignores the fact that tools are tokens — the model must attend to every tool schema on every call. Most tasks only need a small subset of available tools. Sending all tools (1) inflates input token costs, (2) increases the chance the model selects the wrong tool (more options = more confusion), and (3) consumes context window space that could hold relevant conversation history.

## Fix

### Option 1 — Task-type router: select tool subset by query category

```python
import json
import anthropic

client = anthropic.Anthropic()

# Full tool library — all available tools
ALL_TOOLS = {
    "web_search": {
        "name": "web_search",
        "description": "Search the web for current information.",
        "input_schema": {"type": "object", "required": ["query"],
                         "properties": {"query": {"type": "string"}}},
    },
    "read_file": {
        "name": "read_file",
        "description": "Read a file from the filesystem.",
        "input_schema": {"type": "object", "required": ["path"],
                         "properties": {"path": {"type": "string"}}},
    },
    "write_file": {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {"type": "object", "required": ["path", "content"],
                         "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
    },
    "query_database": {
        "name": "query_database",
        "description": "Run a SQL query against the database.",
        "input_schema": {"type": "object", "required": ["sql"],
                         "properties": {"sql": {"type": "string"}}},
    },
    "send_email": {
        "name": "send_email",
        "description": "Send an email to a recipient.",
        "input_schema": {"type": "object", "required": ["to", "subject", "body"],
                         "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}},
    },
    "get_current_time": {
        "name": "get_current_time",
        "description": "Get the current date and time.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "calculate": {
        "name": "calculate",
        "description": "Evaluate a mathematical expression.",
        "input_schema": {"type": "object", "required": ["expression"],
                         "properties": {"expression": {"type": "string"}}},
    },
}

# Task category → relevant tool names
TOOL_SETS = {
    "search":    ["web_search", "get_current_time"],
    "file":      ["read_file", "write_file"],
    "database":  ["query_database"],
    "email":     ["send_email"],
    "math":      ["calculate"],
    "general":   ["web_search", "get_current_time", "calculate"],
}

ROUTER_SYSTEM = """Classify the user's request into one category:
search, file, database, email, math, general.
Return JSON: {"category": "..."}"""

def classify_task(question: str) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        system=ROUTER_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    raw = r.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw).get("category", "general")
    except json.JSONDecodeError:
        return "general"

def ask(question: str) -> str:
    category = classify_task(question)
    tool_names = TOOL_SETS.get(category, TOOL_SETS["general"])
    tools = [ALL_TOOLS[t] for t in tool_names if t in ALL_TOOLS]
    print(f"  [category={category}] tools={tool_names} ({len(tools)}/{len(ALL_TOOLS)} sent)")

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=tools,
        messages=[{"role": "user", "content": question}],
    )
    return next((b.text for b in r.content if b.type == "text"), "")

questions = [
    "Search for the latest Python 3.13 release notes.",
    "Read the file at /tmp/report.txt.",
    "What is 2847 divided by 43?",
    "Send a reminder email to alice@example.com.",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask(q)[:150]}\n")
```

**Expected Token Savings:** Sending 2 tools instead of 7 saves ~600 input tokens per call; for 10,000 daily calls with 7 tools at 120 tokens each, routing from all-7 to task-specific 2 saves ~6M input tokens/day.
**Environment:** General-purpose agents with diverse tool libraries; task routing is the highest-impact tool-schema optimisation and requires only a lightweight classifier call.

---

### Option 2 — Semantic tool selector: embed query, retrieve relevant tools

```python
import math
import anthropic

client = anthropic.Anthropic()

# Tool registry with semantic descriptions for embedding-based retrieval
TOOL_REGISTRY = [
    {"name": "web_search",      "description": "Search the internet for current information, news, facts"},
    {"name": "read_file",       "description": "Read file contents from the local filesystem"},
    {"name": "write_file",      "description": "Write or create files on the local filesystem"},
    {"name": "query_database",  "description": "Execute SQL queries against relational databases"},
    {"name": "send_email",      "description": "Send email messages to recipients"},
    {"name": "get_current_time","description": "Get current date, time, and timezone information"},
    {"name": "calculate",       "description": "Evaluate mathematical expressions and arithmetic"},
    {"name": "list_directory",  "description": "List files and folders in a directory"},
    {"name": "run_shell",       "description": "Execute shell commands and scripts"},
    {"name": "fetch_url",       "description": "Fetch content from a specific URL or web page"},
]

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x**2 for x in a))
    mag_b = math.sqrt(sum(x**2 for x in b))
    return dot / (mag_a * mag_b + 1e-9)

def embed(text: str) -> list[float]:
    """In production: use a real embedding API or local model."""
    # Simplified keyword-based pseudo-embedding for demonstration
    keywords = {
        "search": [1,0,0,0,0,0,0,0,0,0],
        "file":   [0,1,1,0,0,0,0,1,0,0],
        "sql":    [0,0,0,1,0,0,0,0,0,0],
        "email":  [0,0,0,0,1,0,0,0,0,0],
        "time":   [0,0,0,0,0,1,0,0,0,0],
        "math":   [0,0,0,0,0,0,1,0,0,0],
        "shell":  [0,0,0,0,0,0,0,0,1,0],
        "url":    [1,0,0,0,0,0,0,0,0,1],
    }
    vec = [0.0] * 10
    text_lower = text.lower()
    for kw, weights in keywords.items():
        if kw in text_lower:
            vec = [v + w for v, w in zip(vec, weights)]
    norm = math.sqrt(sum(v**2 for v in vec)) or 1.0
    return [v / norm for v in vec]

# Pre-compute tool embeddings
for tool in TOOL_REGISTRY:
    tool["embedding"] = embed(tool["description"])

def select_tools(query: str, top_k: int = 3) -> list[str]:
    q_embed = embed(query)
    scored  = [
        (tool["name"], cosine_similarity(q_embed, tool["embedding"]))
        for tool in TOOL_REGISTRY
    ]
    scored.sort(key=lambda x: -x[1])
    return [name for name, score in scored[:top_k] if score > 0.1]

TOOL_SCHEMAS = {
    t["name"]: {
        "name": t["name"],
        "description": t["description"],
        "input_schema": {"type": "object", "properties": {"input": {"type": "string"}}},
    }
    for t in TOOL_REGISTRY
}

def ask(question: str) -> str:
    selected = select_tools(question, top_k=3)
    tools    = [TOOL_SCHEMAS[n] for n in selected if n in TOOL_SCHEMAS]
    print(f"  [semantic] selected={selected}")

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=tools or list(TOOL_SCHEMAS.values())[:1],
        messages=[{"role": "user", "content": question}],
    )
    return next((b.text for b in r.content if b.type == "text"), r.content[0].type)

questions = [
    "Search for today's top tech news.",
    "Read and summarise the file at /tmp/data.csv.",
    "What time is it in Tokyo right now?",
    "Compute the square root of 144.",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask(q)[:100]}\n")
```

**Expected Token Savings:** Semantic selection retrieves the 3 most relevant tools from a library of 10, saving 7 × 120 = 840 tokens per call; for complex tool libraries (20+ tools), semantic retrieval scales better than manual category mapping.
**Environment:** Agents with large, heterogeneous tool libraries; semantic retrieval generalises to any query without requiring manual category rules.

---

### Option 3 — Progressive tool disclosure: start minimal, add tools on demand

```python
import json
import anthropic

client = anthropic.Anthropic()

# Minimal bootstrap tool — only tool sent on the first call
REQUEST_TOOLS_TOOL = {
    "name": "request_additional_tools",
    "description": "Request access to additional tools needed for this task.",
    "input_schema": {
        "type": "object",
        "required": ["tools_needed", "reason"],
        "properties": {
            "tools_needed": {
                "type": "array",
                "items": {"type": "string",
                          "enum": ["web_search", "read_file", "write_file", "send_email", "calculate", "query_database"]},
                "description": "List of tool names needed",
            },
            "reason": {"type": "string", "description": "Why these tools are needed"},
        },
    },
}

# Full tool library (only sent when requested)
FULL_TOOLS = {
    "web_search":    {"name": "web_search",    "description": "Search the web.",              "input_schema": {"type": "object", "required": ["query"],      "properties": {"query":      {"type": "string"}}}},
    "read_file":     {"name": "read_file",     "description": "Read a file.",                 "input_schema": {"type": "object", "required": ["path"],       "properties": {"path":       {"type": "string"}}}},
    "write_file":    {"name": "write_file",    "description": "Write to a file.",             "input_schema": {"type": "object", "required": ["path","content"],"properties": {"path": {"type": "string"}, "content": {"type": "string"}}}},
    "send_email":    {"name": "send_email",    "description": "Send an email.",               "input_schema": {"type": "object", "required": ["to","subject","body"],"properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}}},
    "calculate":     {"name": "calculate",     "description": "Evaluate a math expression.",  "input_schema": {"type": "object", "required": ["expression"], "properties": {"expression": {"type": "string"}}}},
    "query_database":{"name": "query_database","description": "Run a SQL query.",             "input_schema": {"type": "object", "required": ["sql"],        "properties": {"sql":        {"type": "string"}}}},
}

def run_agent(question: str) -> str:
    messages       = [{"role": "user", "content": question}]
    active_tools   = [REQUEST_TOOLS_TOOL]   # start with only the request tool

    for step in range(8):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=active_tools,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type != "tool_use":
                continue
            if b.name == "request_additional_tools":
                requested = b.input.get("tools_needed", [])
                print(f"  [step {step}] agent requested: {requested} — reason: {b.input.get('reason','')[:60]}")
                # Grant requested tools
                for tool_name in requested:
                    if tool_name in FULL_TOOLS:
                        tool_def = FULL_TOOLS[tool_name]
                        if not any(t["name"] == tool_name for t in active_tools):
                            active_tools.append(tool_def)
                result = json.dumps({"granted": requested, "message": "Tools are now available."})
            else:
                # Simulate tool execution
                result = json.dumps({"result": f"[simulated result for {b.name}]"})
                print(f"  [step {step}] {b.name}({b.input}) → simulated")
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

questions = [
    "Search the web for Python 3.13 release notes.",
    "What is 15% of 240?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {run_agent(q)[:200]}\n")
```

**Expected Token Savings:** Progressive disclosure starts with 1 tool schema (~50 tokens) instead of all tools; only tools the agent actually needs are ever sent — tasks that need no tools pay 0 tool overhead after the initial bootstrap.
**Environment:** Agents where task type is unknown upfront; progressive disclosure is ideal when the agent should determine its own tool needs rather than having them pre-classified.

---

### Option 4 — Compact tool schemas: strip verbose descriptions in production

```python
import copy
import anthropic

client = anthropic.Anthropic()

# VERBOSE schemas — full descriptions for development/documentation
VERBOSE_TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the internet for current information, news, recent events, and facts. "
            "Use this tool when the user asks about something that may have changed recently, "
            "when you need to verify a claim, or when you need up-to-date data. "
            "Provide a specific, well-formed search query for best results."
        ),
        "input_schema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to submit. Be specific and include relevant keywords.",
                    "minLength": 3,
                    "maxLength": 200,
                },
            },
        },
    },
    {
        "name": "calculate",
        "description": (
            "Evaluate a mathematical expression. Supports basic arithmetic (+, -, *, /), "
            "exponentiation (**), modulo (%), and common functions (sqrt, abs, round). "
            "Use this for any numeric calculation to avoid arithmetic errors."
        ),
        "input_schema": {
            "type": "object",
            "required": ["expression"],
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "A valid Python mathematical expression, e.g. '2 ** 10' or 'round(3.14159, 2)'",
                    "examples": ["2 + 2", "100 * 0.15", "2 ** 32"],
                },
            },
        },
    },
]

def compact_tools(tools: list[dict], max_desc_chars: int = 80) -> list[dict]:
    """Strip verbose descriptions to the first sentence and remove examples/constraints."""
    compacted = []
    for tool in tools:
        t = copy.deepcopy(tool)
        # Truncate tool description
        desc = t.get("description", "")
        if len(desc) > max_desc_chars:
            t["description"] = desc[:max_desc_chars].rsplit(" ", 1)[0] + "."
        # Strip property descriptions and constraints
        for prop in t.get("input_schema", {}).get("properties", {}).values():
            if "description" in prop and len(prop["description"]) > 60:
                prop["description"] = prop["description"][:60].rsplit(" ", 1)[0] + "."
            prop.pop("examples", None)
            prop.pop("minLength", None)
            prop.pop("maxLength", None)
        compacted.append(t)
    return compacted

import json as _json

verbose_tok  = len(_json.dumps(VERBOSE_TOOLS))
compact_tok  = len(_json.dumps(compact_tools(VERBOSE_TOOLS)))
print(f"Verbose schema:  ~{verbose_tok // 4} tokens")
print(f"Compact schema:  ~{compact_tok // 4} tokens")
print(f"Savings:         ~{(verbose_tok - compact_tok) // 4} tokens per call")

# Use compact tools in production calls
tools_to_send = compact_tools(VERBOSE_TOOLS)
r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    tools=tools_to_send,
    messages=[{"role": "user", "content": "What is 144 divided by 12?"}],
)
print(f"\nA: {next((b.text for b in r.content if b.type == 'text'), r.content[0].name if r.content else '')}")
```

**Expected Token Savings:** Compacting tool descriptions from 150 to 50 characters saves ~25 tokens per tool; for a 10-tool agent, compacting saves ~250 tokens per call — 2.5M tokens/day at 10,000 calls/day.
**Environment:** Production agents where tool schemas were originally written for human readability; schema compaction is a zero-logic-change optimisation that reduces token cost without affecting tool selection accuracy.

---

### Option 5 — Tool schema caching with prompt cache headers

```python
import anthropic

client = anthropic.Anthropic()

# Large tool library — expensive to send on every call
TOOLS = [
    {
        "name": "web_search",
        "description": "Search the internet for current information.",
        "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
        "cache_control": {"type": "ephemeral"},   # cache this tool definition
    },
    {
        "name": "read_file",
        "description": "Read a file from the filesystem.",
        "input_schema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
        "cache_control": {"type": "ephemeral"},
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {"type": "object", "required": ["path", "content"], "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
        "cache_control": {"type": "ephemeral"},
    },
    {
        "name": "calculate",
        "description": "Evaluate a mathematical expression.",
        "input_schema": {"type": "object", "required": ["expression"], "properties": {"expression": {"type": "string"}}},
    },
]

SYSTEM = "You are a helpful assistant with access to tools."

questions = [
    "Search for Python 3.13 features.",
    "What is 2 to the power of 16?",
    "Search for the latest news about AI agents.",
]

for i, q in enumerate(questions):
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=SYSTEM,
        tools=TOOLS,
        messages=[{"role": "user", "content": q}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    usage = r.usage
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    cache_read  = getattr(usage, "cache_read_input_tokens", 0)
    print(f"Q{i+1}: {q[:50]}")
    print(f"  tokens: input={usage.input_tokens} cache_write={cache_write} cache_read={cache_read}")
    print(f"  {'[CACHE HIT]' if cache_read > 0 else '[CACHE MISS]'}\n")
```

**Expected Token Savings:** Tool schema caching charges the full tool token cost once, then 90% less on cache hits; for 3 cached tools at 200 tokens each, subsequent calls save 540 tokens × 0.9 = 486 tokens at a 90% discount — the breakeven is 1 subsequent call.
**Environment:** Agents with stable tool schemas that don't change between calls; tool caching is most effective when combined with a stable system prompt cache — both are cached together in the same cache block.

---

### Option 6 — Dynamic tool injection: inject only tools needed for this turn

```python
import json
import anthropic

client = anthropic.Anthropic()

# Tool library with metadata for dynamic selection
TOOL_LIBRARY = {
    "web_search":    {"schema": {"name": "web_search",    "description": "Search the web.",  "input_schema": {"type": "object", "required": ["query"],      "properties": {"query":      {"type": "string"}}}}, "keywords": ["search","find","look up","latest","current","news"]},
    "calculate":     {"schema": {"name": "calculate",     "description": "Do math.",         "input_schema": {"type": "object", "required": ["expression"], "properties": {"expression": {"type": "string"}}}}, "keywords": ["calculate","compute","math","multiply","divide","percentage","+","-","*","/"]},
    "read_file":     {"schema": {"name": "read_file",     "description": "Read a file.",     "input_schema": {"type": "object", "required": ["path"],       "properties": {"path":       {"type": "string"}}}}, "keywords": ["read","file","open","contents","show"]},
    "send_email":    {"schema": {"name": "send_email",    "description": "Send an email.",   "input_schema": {"type": "object", "required": ["to","subject","body"],"properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}}}, "keywords": ["email","send","message","notify","alert"]},
    "get_time":      {"schema": {"name": "get_time",      "description": "Get current time.","input_schema": {"type": "object", "properties": {}}},             "keywords": ["time","date","today","now","when","timezone"]},
}

def select_tools_by_keywords(query: str, max_tools: int = 3) -> list[dict]:
    query_lower = query.lower()
    scores = {}
    for name, meta in TOOL_LIBRARY.items():
        score = sum(1 for kw in meta["keywords"] if kw in query_lower)
        if score > 0:
            scores[name] = score
    # Return top-scoring tools, plus always include a fallback if nothing matched
    if not scores:
        return [TOOL_LIBRARY["web_search"]["schema"]]   # default fallback
    top = sorted(scores.items(), key=lambda x: -x[1])[:max_tools]
    tools = [TOOL_LIBRARY[name]["schema"] for name, _ in top]
    print(f"  [keyword match] selected={[t['name'] for t in tools]}")
    return tools

def ask(question: str) -> str:
    tools = select_tools_by_keywords(question)
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=tools,
        messages=[{"role": "user", "content": question}],
    )
    return next((b.text for b in r.content if b.type == "text"), r.content[0].type if r.content else "")

questions = [
    "Search for the latest news about quantum computing.",
    "Calculate 15% tip on a $84.50 bill.",
    "What time is it right now in New York?",
    "Read the file at /tmp/notes.txt and summarise it.",
    "Send an email to bob@example.com about the meeting tomorrow.",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask(q)[:100]}\n")
```

**Expected Token Savings:** Keyword matching selects 1-3 tools from a 5-tool library, saving 2-4 × ~100 tokens per call; keyword matching adds zero API calls and runs in microseconds — it is the lightest-weight tool selection method available.
**Environment:** Agents with moderate-sized tool libraries (5-15 tools) where keyword signals reliably indicate tool relevance; keyword matching is the best choice when the overhead of a classifier call is not justified.

---

## Comparison

| Option | Selection Method | Extra API Calls | Scales to Large Libraries | Best For |
|---|---|---|---|---|
| 1. Task-type router | Classifier (LLM) | 1 router call | Yes (via categories) | General-purpose agents |
| 2. Semantic selector | Embedding similarity | 0 (pre-computed) | Yes | Large heterogeneous tool sets |
| 3. Progressive disclosure | Agent self-requests | 0 extra | Yes | Unknown task types upfront |
| 4. Compact schemas | Preprocessing | 0 | Yes (multiplicative) | All agents — always apply |
| 5. Prompt cache | Cache headers | 0 | No (all still sent) | Stable schemas, many calls |
| 6. Keyword matching | String matching | 0 | Moderate | Small libraries, low overhead |
