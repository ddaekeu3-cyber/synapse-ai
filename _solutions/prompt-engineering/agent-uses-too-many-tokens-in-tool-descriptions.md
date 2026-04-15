---
layout: solution
title: "Agent uses too many tokens in tool descriptions"
category: prompt-engineering
description: "Tool definitions with 300–700 word descriptions silently consume 3,000–8,000 tokens on every API call, inflating cost by 40–200% before a single word of the user's request is processed."
tags: [token-cost, tool-use, prompt-engineering, tool-descriptions, optimization]
---

## Symptom

Token usage per request is much higher than expected even for short conversations. Inspection of the raw API payload reveals tool definitions that look like this:

```json
{
  "name": "search_documents",
  "description": "This tool allows the agent to search through a large collection of documents stored in the vector database. It accepts a natural language query string and returns the most semantically relevant documents based on cosine similarity scoring. The tool supports optional filtering by date range, document type, author, and department. Results are returned as a ranked list with relevance scores. Use this tool whenever the user asks about company policies, procedures, historical records, or any information that may be stored in the document repository. This tool should be preferred over web search for internal information. The query should be a clear, concise natural language description of what the user is looking for..."
}
```

This description alone is ~130 tokens. With 15 such tools, the overhead is ~2,000 tokens per call — billed on every turn even when none of those tools are used.

## Root Cause

Tool descriptions were written once for human readability and never audited for token efficiency. The model only needs enough description to decide *when* and *how* to call the tool — it does not need a tutorial embedded in every API call. Long descriptions survive because their cost is invisible: they appear in input tokens which are cheaper but still significant, and they never show up in response latency.

---

## Option 1 — Concise description style guide (rewrite existing tools)

**Rewrite descriptions to ≤ 20 words. Move usage guidance to the system prompt once instead of repeating it per tool.**

```python
import anthropic

client = anthropic.Anthropic()

# BEFORE — 97 tokens just for description
VERBOSE_TOOLS = [
    {
        "name": "search_documents",
        "description": (
            "This tool allows the agent to search through a large collection of documents "
            "stored in the vector database. It accepts a natural language query string and "
            "returns the most semantically relevant documents based on cosine similarity "
            "scoring. The tool supports optional filtering by date range, document type, "
            "author, and department. Results are returned as a ranked list with relevance "
            "scores. Use this tool whenever the user asks about internal company information."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 5},
            },
            "required": ["query"],
        },
    }
]

# AFTER — 8 tokens for description; guidance moved to system prompt
CONCISE_TOOLS = [
    {
        "name": "search_documents",
        "description": "Search internal document store by semantic query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    }
]

SYSTEM_PROMPT = """You have access to these tools:
- search_documents: use for any question about internal policies, procedures, or records.
- send_email: use when the user explicitly requests sending a message.
- create_ticket: use when the user reports a bug or requests a new feature.
Always prefer search_documents before answering from memory."""


def ask(user_message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        tools=CONCISE_TOOLS,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text if response.content[0].type == "text" else str(response.content[0])


print(ask("What is our remote work policy?"))
```

**Expected Token Savings:** Moving verbose guidance to the system prompt (cached after first call) and shortening descriptions saves 60–80% of tool-definition tokens — typically 1,500–5,000 tokens per request for a 10–15 tool agent.

**Environment:** Any agent with static tool sets; immediate benefit with zero architecture changes.

---

## Option 2 — Lazy tool loading: only include relevant tools per request

**Classify the user's request first, then include only the subset of tools likely needed for that request type.**

```python
import anthropic
from enum import Enum

client = anthropic.Anthropic()


class TaskType(str, Enum):
    SEARCH = "search"
    EMAIL = "email"
    CODE = "code"
    CALENDAR = "calendar"
    UNKNOWN = "unknown"


# Full tool registry — never sent all at once
ALL_TOOLS: dict[str, dict] = {
    "search_documents": {
        "name": "search_documents",
        "description": "Search internal document store.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    "send_email": {
        "name": "send_email",
        "description": "Send an email to one or more recipients.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    "run_code": {
        "name": "run_code",
        "description": "Execute Python code and return stdout.",
        "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    },
    "create_calendar_event": {
        "name": "create_calendar_event",
        "description": "Create a calendar event.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "time": {"type": "string"}},
            "required": ["title", "time"],
        },
    },
}

TASK_TOOL_MAP: dict[TaskType, list[str]] = {
    TaskType.SEARCH: ["search_documents"],
    TaskType.EMAIL: ["search_documents", "send_email"],
    TaskType.CODE: ["run_code"],
    TaskType.CALENDAR: ["create_calendar_event", "search_documents"],
    TaskType.UNKNOWN: ["search_documents"],
}


def classify_request(user_message: str) -> TaskType:
    """Lightweight classification — haiku, no tools, single token output."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=(
            "Classify the request. Reply with exactly one word: "
            "search, email, code, calendar, or unknown."
        ),
        messages=[{"role": "user", "content": user_message}],
    )
    label = response.content[0].text.strip().lower()
    return TaskType(label) if label in TaskType._value2member_map_ else TaskType.UNKNOWN


def ask(user_message: str) -> str:
    task_type = classify_request(user_message)
    tool_names = TASK_TOOL_MAP[task_type]
    tools = [ALL_TOOLS[name] for name in tool_names]

    print(f"Task type: {task_type.value} — loading {len(tools)}/{len(ALL_TOOLS)} tools")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text if hasattr(response.content[0], "text") else ""


print(ask("Can you find our vacation policy?"))
print(ask("Schedule a meeting for tomorrow at 2pm"))
```

**Expected Token Savings:** Loading 2–3 tools instead of 15 saves ~80% of tool-definition tokens per request. Classification costs ~50 tokens (haiku) — net saving of 1,200–4,000 tokens for a 15-tool agent.

**Environment:** Agents with 8+ tools covering distinct domains; slightly higher latency from the classification call.

---

## Option 3 — LLM-powered description compressor

**Pass existing verbose descriptions through a compression prompt to produce ≤ 15-word equivalents automatically.**

```python
import json
import anthropic

client = anthropic.Anthropic()


def compress_tool_descriptions(tools: list[dict]) -> list[dict]:
    """Rewrite tool descriptions to ≤ 15 words using Claude."""
    descriptions = {t["name"]: t["description"] for t in tools}

    prompt = (
        "Rewrite each tool description to ≤ 15 words. "
        "Keep all critical information about what the tool does and when to use it. "
        "Return a JSON object mapping tool name to compressed description.\n\n"
        f"Input:\n{json.dumps(descriptions, indent=2)}"
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text
    # Extract JSON from response
    start = raw.find("{")
    end = raw.rfind("}") + 1
    compressed: dict[str, str] = json.loads(raw[start:end])

    result = []
    for tool in tools:
        new_tool = dict(tool)
        if tool["name"] in compressed:
            original_len = len(tool["description"].split())
            new_len = len(compressed[tool["name"]].split())
            print(f"  {tool['name']}: {original_len} → {new_len} words")
            new_tool["description"] = compressed[tool["name"]]
        result.append(new_tool)
    return result


# Example verbose tools
verbose_tools = [
    {
        "name": "search_web",
        "description": (
            "This tool performs a web search using the DuckDuckGo search engine. "
            "It accepts a search query string and returns the top results including "
            "title, URL, and snippet. Use this tool when the user asks about current "
            "events, recent news, or any information that might not be in the internal "
            "knowledge base. The tool works best with specific, focused queries."
        ),
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "write_file",
        "description": (
            "This tool writes content to a file on the local filesystem. It accepts "
            "a file path and content string and creates or overwrites the file at the "
            "specified location. Use this tool when the user asks you to save output, "
            "create configuration files, write code to disk, or persist any data "
            "between sessions. Always confirm the file path with the user before writing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
]

compressed_tools = compress_tool_descriptions(verbose_tools)
print(json.dumps([{"name": t["name"], "description": t["description"]} for t in compressed_tools], indent=2))
```

**Expected Token Savings:** One-time compression cost (~300 tokens) pays back within the first 3–5 API calls. Compressed descriptions typically reduce tool-definition overhead by 70–85%.

**Environment:** Migration tool for existing agents; run once offline, commit compressed definitions to source.

---

## Option 4 — Token budget enforcer at tool registration

**Wrap tool registration with a budget check that raises at startup if any description exceeds a word limit.**

```python
import anthropic

client = anthropic.Anthropic()

MAX_DESCRIPTION_WORDS = 20
MAX_PARAM_DESCRIPTION_WORDS = 10


class ToolRegistry:
    """Tool registry that enforces description token budgets."""

    def __init__(
        self,
        max_desc_words: int = MAX_DESCRIPTION_WORDS,
        max_param_words: int = MAX_PARAM_DESCRIPTION_WORDS,
    ) -> None:
        self._tools: list[dict] = []
        self.max_desc = max_desc_words
        self.max_param = max_param_words

    def register(self, tool: dict) -> "ToolRegistry":
        name = tool["name"]
        desc = tool.get("description", "")
        word_count = len(desc.split())

        if word_count > self.max_desc:
            raise ValueError(
                f"Tool '{name}' description is {word_count} words "
                f"(limit: {self.max_desc}). Shorten it.\n"
                f"  Current: {desc!r}"
            )

        # Check parameter descriptions
        props = tool.get("input_schema", {}).get("properties", {})
        for param, schema in props.items():
            pdesc = schema.get("description", "")
            pwords = len(pdesc.split())
            if pwords > self.max_param:
                raise ValueError(
                    f"Tool '{name}' param '{param}' description is {pwords} words "
                    f"(limit: {self.max_param})."
                )

        self._tools.append(tool)
        return self

    @property
    def tools(self) -> list[dict]:
        return self._tools

    def summary(self) -> None:
        total_words = sum(len(t.get("description", "").split()) for t in self._tools)
        print(f"Registered {len(self._tools)} tools, ~{total_words} description words total.")


registry = ToolRegistry(max_desc_words=20, max_param_words=10)

# This will pass
registry.register({
    "name": "search_documents",
    "description": "Search internal document store by semantic query.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query"}},
        "required": ["query"],
    },
})

# This will raise ValueError at startup — caught early, not at call time
try:
    registry.register({
        "name": "send_email",
        "description": (
            "Send an email to the specified recipients. This tool handles both internal "
            "and external email delivery and supports CC and BCC fields. Always confirm "
            "before sending to avoid accidental delivery to wrong addresses."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    })
except ValueError as e:
    print(f"Registration failed: {e}")

registry.summary()

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    tools=registry.tools,
    messages=[{"role": "user", "content": "Find our refund policy."}],
)
print(response.content[0].text if hasattr(response.content[0], "text") else response.stop_reason)
```

**Expected Token Savings:** Prevents description bloat from creeping back in — keeps tool overhead at ≤ 400 tokens for a 15-tool agent vs. 3,000–8,000 without enforcement. Saves 70–95% of tool-definition tokens long-term.

**Environment:** Any agent codebase; add to CI to block PRs that introduce verbose descriptions.

---

## Option 5 — Hierarchical tool registry with on-demand expansion

**Register tools with a one-line summary. Expand to full description only when the model explicitly requests details.**

```python
import json
import anthropic

client = anthropic.Anthropic()


class HierarchicalToolRegistry:
    """Two-level tool descriptions: summary (always sent) + detail (on demand)."""

    def __init__(self) -> None:
        self._tools: dict[str, dict] = {}

    def register(self, name: str, summary: str, detail: str, schema: dict) -> None:
        assert len(summary.split()) <= 15, f"Summary for '{name}' must be ≤ 15 words"
        self._tools[name] = {"summary": summary, "detail": detail, "schema": schema}

    def slim_tools(self) -> list[dict]:
        """Return tools with one-line descriptions — for normal requests."""
        result = []
        for name, meta in self._tools.items():
            result.append({
                "name": name,
                "description": meta["summary"],
                "input_schema": meta["schema"],
            })
        return result

    def detail_for(self, name: str) -> str:
        return self._tools[name]["detail"]


registry = HierarchicalToolRegistry()
registry.register(
    name="generate_report",
    summary="Generate a formatted PDF or CSV report from query results.",
    detail=(
        "Accepts a SQL SELECT query and output format ('pdf' or 'csv'). "
        "Runs the query against the analytics database, formats the results, "
        "and returns a download URL valid for 24 hours. "
        "For PDF: includes header, footer, and page numbers. "
        "For CSV: includes header row with column names. "
        "Max 100,000 rows. Use for scheduled reports or data exports."
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "format": {"type": "string", "enum": ["pdf", "csv"]},
        },
        "required": ["query", "format"],
    },
)

# Normal call — slim descriptions only
DETAIL_TOOL = {
    "name": "get_tool_detail",
    "description": "Get full documentation for a specific tool by name.",
    "input_schema": {
        "type": "object",
        "properties": {"tool_name": {"type": "string", "description": "Tool name"}},
        "required": ["tool_name"],
    },
}


def ask(user_message: str) -> str:
    tools = registry.slim_tools() + [DETAIL_TOOL]
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            tool_call = next(b for b in response.content if b.type == "tool_use")
            if tool_call.name == "get_tool_detail":
                result = registry.detail_for(tool_call.input["tool_name"])
            else:
                result = f"[Tool {tool_call.name} executed successfully]"

            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": result}],
            })
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(ask("Generate a CSV report of sales from last quarter."))
```

**Expected Token Savings:** Normal requests use slim descriptions (~15 tokens/tool). Detail expansion adds ~100 tokens only when needed — typically <5% of requests. Net saving: 70–90% of tool-definition tokens.

**Environment:** Agents with complex tools where full documentation occasionally helps; adds one extra round-trip for detail requests.

---

## Option 6 — Auto-minifier with semantic similarity check

**Compress descriptions automatically and verify the compressed version preserves meaning via embedding similarity.**

```python
import json
import anthropic

client = anthropic.Anthropic()


def embed(text: str) -> list[float]:
    """Placeholder — replace with real embedding call (e.g., voyage-3)."""
    # In production use: anthropic voyage API or openai embeddings
    return [hash(text + str(i)) % 1000 / 1000.0 for i in range(128)]


def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def minify_description(name: str, description: str, target_words: int = 15) -> str:
    """Compress description; verify semantic similarity ≥ 0.85."""
    if len(description.split()) <= target_words:
        return description

    prompt = (
        f"Compress this tool description to ≤ {target_words} words. "
        f"Preserve: what it does, when to use it, key constraints.\n\n"
        f"Original: {description}"
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    compressed = response.content[0].text.strip()

    # Verify semantic similarity (use real embeddings in production)
    sim = cosine_sim(embed(description), embed(compressed))
    print(f"  '{name}': {len(description.split())} → {len(compressed.split())} words | sim={sim:.2f}")

    if sim < 0.7:
        print(f"  WARNING: low similarity for '{name}' — keeping original")
        return description

    return compressed


def minify_tools(tools: list[dict]) -> list[dict]:
    result = []
    for tool in tools:
        new_tool = dict(tool)
        if "description" in tool:
            new_tool["description"] = minify_description(
                tool["name"], tool["description"]
            )
        result.append(new_tool)
    return result


# Example usage
verbose_tools = [
    {
        "name": "query_database",
        "description": (
            "Execute a read-only SQL query against the production analytics database. "
            "The tool accepts a valid PostgreSQL SELECT statement and returns results "
            "as a list of row dictionaries. All queries are automatically limited to "
            "1,000 rows. Use this tool when the user asks for data, metrics, statistics, "
            "counts, or any information that requires querying structured data."
        ),
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    }
]

minified = minify_tools(verbose_tools)
print(json.dumps([{"name": t["name"], "description": t["description"]} for t in minified], indent=2))
```

**Expected Token Savings:** Automated minification across a 15-tool agent typically saves 2,000–6,000 tokens per call. One-time compression cost (~500 tokens) is recovered within the first 1–2 API calls.

**Environment:** Agents with programmatically generated or third-party tool definitions; run as a build step before deployment.

---

## Comparison

| Option | Approach | Token Saving | One-time Cost | Ongoing Cost |
|--------|---------|-------------|--------------|-------------|
| 1. Style guide rewrite | Manual — shorten descriptions | 60–80% | Hours of editing | Zero |
| 2. Lazy tool loading | Classify then subset tools | 75–85% | Classification call | ~50 tokens/req |
| 3. LLM compressor | Auto-rewrite offline | 70–85% | ~300 tokens | Zero after migration |
| 4. Budget enforcer | Fail-fast at registration | 70–95% | Zero | Zero |
| 5. Hierarchical registry | Slim + on-demand detail | 70–90% | Refactor effort | ~100 tokens (rare) |
| 6. Auto-minifier + sim check | Compress + verify | 65–80% | ~500 tokens | Zero after migration |

**Recommended path:** Start with Option 4 (budget enforcer) to stop the bleed immediately — add it to the tool registration path and CI so bloated descriptions never ship. Then apply Option 1 (style guide rewrite) to fix existing tools. For large or dynamic tool sets, add Option 2 (lazy loading) for the largest per-request savings.
