---
title: "Agent Doesn't Implement Response Field Projection"
description: "Agents fetch full API responses and database records when they only need a few fields; the excess data inflates context window usage, increases token costs, and slows down serialization."
category: performance
difficulty: intermediate
tags: [projection, field-selection, context-efficiency, tokens, graphql, sql, performance]
---

# Agent Doesn't Implement Response Field Projection

## Problem

Agents that fetch complete records from databases or APIs — all 80 fields of a user object when only `name` and `email` are needed — waste tokens inserting irrelevant data into the LLM context window. Each unnecessary field increases input token cost (typically $3–15/MTok), inflates the prompt size, and can dilute the model's attention. Field projection (requesting only the fields you need) is the single cheapest optimization available: it requires no infrastructure change and directly reduces cost and latency.

## Solution 1: SQL Column Selection — Never SELECT *

Replace `SELECT *` with explicit column lists matched to what the agent actually needs for each query type.

```python
import asyncio
from typing import Any
import aiosqlite
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Define field sets for each query intent
FIELD_SETS = {
    "greeting":      ["name", "preferred_name"],
    "contact":       ["name", "email", "phone"],
    "billing":       ["name", "email", "billing_plan", "credits_remaining"],
    "full_profile":  ["name", "email", "phone", "address", "created_at", "last_login"],
}

async def fetch_user(db_path: str, user_id: str, intent: str) -> dict[str, Any]:
    """
    Fetch only the columns needed for the given intent.
    Avoids pulling password_hash, raw_events, preferences_blob, etc.
    """
    columns = FIELD_SETS.get(intent, FIELD_SETS["full_profile"])
    col_list = ", ".join(columns)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {col_list} FROM users WHERE id = ?",  # noqa: S608
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}

async def agent_with_projected_query(user_id: str, user_question: str, db_path: str) -> str:
    # Determine intent from question to pick field set
    intent_resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{
            "role": "user",
            "content": f"Classify this question into one of: greeting, contact, billing, full_profile.\nQuestion: {user_question}\nAnswer with one word:",
        }],
    )
    intent = intent_resp.content[0].text.strip().lower()
    if intent not in FIELD_SETS:
        intent = "full_profile"

    user_data = await fetch_user(db_path, user_id, intent)

    # Token count: "billing" fields ≈ 40 tokens vs full profile ≈ 200 tokens
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"User data ({intent} fields only): {user_data}\n\n{user_question}",
        }],
    )
    return resp.content[0].text

# Concrete token savings example:
# Full user record: 80 fields × ~5 tokens = 400 tokens → $0.0012 per call
# Projected record: 3 fields × ~5 tokens  = 15 tokens  → $0.00004 per call
# At 100K calls/day: $120/day vs $4/day
```

**When to use**: Any agent that queries a SQL database. This is the most impactful optimization for database-backed agents and requires no infrastructure changes.

---

## Solution 2: REST API Field Filtering — Use `fields` Query Parameters

Most REST APIs support a `fields` or `select` query parameter. Request only the fields you need instead of accepting the full response body.

```python
import asyncio
import httpx
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Map agent task types to required API fields
GITHUB_FIELD_MAP = {
    "repo_summary":    ["name", "description", "stargazers_count", "language", "updated_at"],
    "repo_contact":    ["name", "owner.login", "owner.html_url", "homepage"],
    "repo_ci":         ["name", "default_branch", "clone_url", "topics"],
    "repo_full":       None,  # None = no projection, get everything
}

async def fetch_github_repo(owner: str, repo: str, task: str) -> dict:
    """
    Fetch GitHub repo data with field projection using the GitHub API's
    response filtering (via GraphQL) or manual post-filtering for REST.
    """
    fields = GITHUB_FIELD_MAP.get(task)

    async with httpx.AsyncClient() as http:
        resp = await http.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=10.0,
        )
        resp.raise_for_status()
        full_data = resp.json()

    if fields is None:
        return full_data

    # Project: keep only needed top-level fields
    projected = {}
    for field_path in fields:
        parts = field_path.split(".")
        src = full_data
        dst = projected
        for i, part in enumerate(parts):
            if part not in src:
                break
            if i == len(parts) - 1:
                dst[part] = src[part]
            else:
                dst.setdefault(part, {})
                src = src[part]
                dst = dst[part]
    return projected

def project_dict(data: dict, fields: list[str]) -> dict:
    """Generic field projector for any dict response."""
    if not fields:
        return data
    result = {}
    for field_path in fields:
        parts = field_path.split(".")
        src = data
        dst = result
        for i, part in enumerate(parts):
            if not isinstance(src, dict) or part not in src:
                break
            if i == len(parts) - 1:
                dst[part] = src[part]
            else:
                dst.setdefault(part, {})
                src = src[part]
                dst = dst[part]
    return result

async def agent_repo_analysis(owner: str, repo: str, question: str) -> str:
    # Fetch with projection — only what we need for the summary task
    repo_data = await fetch_github_repo(owner, repo, task="repo_summary")

    # repo_summary: ~80 tokens vs full response: ~800 tokens (10× reduction)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Repository data: {repo_data}\n\n{question}",
        }],
    )
    return resp.content[0].text
```

**When to use**: Agents that call external REST APIs. Even when APIs don't support server-side field filtering, post-filter client-side before injecting into the LLM context.

---

## Solution 3: GraphQL Field Selection — Request Exactly What You Need

For GraphQL APIs, construct queries that select only the fields the agent needs for the current task.

```python
import asyncio
import httpx
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# GraphQL queries by agent task — each requests exactly the fields needed
GRAPHQL_QUERIES = {
    "user_greeting": """
query UserGreeting($id: ID!) {
  user(id: $id) {
    displayName
    firstName
  }
}""",
    "user_billing": """
query UserBilling($id: ID!) {
  user(id: $id) {
    displayName
    email
    subscription {
      plan
      creditsRemaining
      renewalDate
    }
  }
}""",
    "user_activity": """
query UserActivity($id: ID!) {
  user(id: $id) {
    displayName
    lastLoginAt
    recentActions(limit: 5) {
      type
      timestamp
    }
  }
}""",
}

async def graphql_fetch(
    endpoint: str,
    query: str,
    variables: dict,
    auth_token: str,
) -> dict:
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            endpoint,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        result = resp.json()
        if "errors" in result:
            raise ValueError(f"GraphQL errors: {result['errors']}")
        return result.get("data", {})

async def agent_with_graphql_projection(
    user_id: str,
    task: str,
    question: str,
    graphql_endpoint: str,
    auth_token: str,
) -> str:
    query = GRAPHQL_QUERIES.get(task, GRAPHQL_QUERIES["user_greeting"])
    user_data = await graphql_fetch(
        graphql_endpoint,
        query,
        variables={"id": user_id},
        auth_token=auth_token,
    )

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"User data: {user_data}\n\n{question}",
        }],
    )
    return resp.content[0].text

# Token comparison for a typical user object:
# Full user (all fields):          ~600 tokens
# user_greeting query:             ~15 tokens  (97% reduction)
# user_billing query:              ~60 tokens  (90% reduction)
# user_activity query (5 events):  ~120 tokens (80% reduction)
```

**When to use**: Agents backed by GraphQL APIs. GraphQL field selection is the most precise form of projection — the server never serializes fields you don't request.

---

## Solution 4: Context Window Budget — Trim Tool Results to Fit

Enforce a per-tool token budget. Tool results that exceed the budget are summarized or truncated before injection into the LLM context.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)

def trim_to_budget(data: dict | list | str, budget_tokens: int) -> str:
    """
    Serialize data and trim to fit within budget_tokens.
    For dicts/lists, progressively removes items from the end.
    """
    serialized = json.dumps(data, default=str)
    if estimate_tokens(serialized) <= budget_tokens:
        return serialized

    budget_chars = budget_tokens * 4

    if isinstance(data, dict):
        # Trim fields from end until within budget
        keys = list(data.keys())
        while keys and len(json.dumps({k: data[k] for k in keys}, default=str)) > budget_chars:
            keys.pop()
        trimmed = {k: data[k] for k in keys}
        note = f"... [{len(data) - len(keys)} fields omitted, budget={budget_tokens}t]"
        return json.dumps(trimmed, default=str) + note

    if isinstance(data, list):
        # Trim items from end
        items = list(data)
        while items and len(json.dumps(items, default=str)) > budget_chars:
            items.pop()
        note = f"... [{len(data) - len(items)} items omitted, budget={budget_tokens}t]"
        return json.dumps(items, default=str) + note

    # String: truncate
    return serialized[:budget_chars] + f"... [truncated, budget={budget_tokens}t]"

# Per-tool token budgets (tune based on importance)
TOOL_BUDGETS = {
    "database_query":   500,   # DB results: detailed but bounded
    "web_search":       300,   # Search snippets: summary only
    "file_read":        800,   # Files: allow more tokens
    "api_response":     400,   # External APIs: moderate
    "user_profile":     100,   # Profile: tiny — just key fields
}

async def agent_with_budgeted_tools(question: str) -> str:
    # Simulate tool calls with large responses
    async def fake_db_query():
        return [{"id": i, "name": f"Record {i}", "data": "x" * 100} for i in range(50)]

    async def fake_web_search():
        return [{"title": f"Result {i}", "snippet": "s" * 200, "url": f"https://ex.com/{i}"} for i in range(10)]

    db_result, search_result = await asyncio.gather(fake_db_query(), fake_web_search())

    # Trim each tool result to its budget before injection
    db_text = trim_to_budget(db_result, TOOL_BUDGETS["database_query"])
    search_text = trim_to_budget(search_result, TOOL_BUDGETS["web_search"])

    context = (
        f"Database results ({estimate_tokens(db_text)} tokens):\n{db_text}\n\n"
        f"Web search results ({estimate_tokens(search_text)} tokens):\n{search_text}"
    )

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{context}\n\n{question}"}],
    )
    return resp.content[0].text
```

**When to use**: Agents where tool result sizes are variable and unpredictable. Budget enforcement prevents a single large tool result from consuming the entire context window.

---

## Solution 5: LLM-Summarized Tool Results — Compress Before Injection

When trimming loses too much information, use a cheap fast model to summarize tool results before injecting them into the main model's context.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def summarize_tool_result(
    tool_name: str,
    raw_result: str,
    question_context: str,
    max_tokens: int = 150,
) -> str:
    """
    Use a cheap/fast model to summarize a tool result before injection.
    The summary is focused on what's relevant to the question.
    """
    prompt = f"""Summarize this {tool_name} result in ≤{max_tokens // 4} words.
Focus only on information relevant to: "{question_context}"
Omit IDs, timestamps, and metadata unless directly relevant.

Raw result:
{raw_result[:4000]}

Summary:"""

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",  # cheap + fast
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()

async def agent_with_summarized_context(question: str) -> str:
    # Simulate a large tool result (e.g., full document or large API response)
    large_result = "\n".join([
        f"Record {i}: customer_id=C{i:04d}, purchase_date=2024-0{(i%9)+1}-15, "
        f"product=Widget-{i%5}, amount={50+i*3}.00, status=completed, "
        f"shipping_address=123 Main St City State ZIP, notes=standard_order"
        for i in range(100)
    ])

    # Summarize before injection — reduces 2000 tokens to ~150
    summary = await summarize_tool_result(
        tool_name="purchase_history",
        raw_result=large_result,
        question_context=question,
        max_tokens=200,
    )

    # Main model now sees a focused summary instead of raw data
    resp = await client.messages.create(
        model="claude-sonnet-4-6",  # premium model for final answer
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Purchase history summary:\n{summary}\n\n{question}",
        }],
    )

    # Cost analysis:
    # Without summarization: 2000 tokens × $3/MTok (Sonnet) = $0.006 per call
    # With summarization:    150 tokens  × $3/MTok (Sonnet) + 2000 × $0.25/MTok (Haiku) = $0.0009 per call
    # Savings: ~85% per call

    return resp.content[0].text

```

**When to use**: Agents that process large documents or API responses. LLM-based summarization is more semantically accurate than simple truncation and often cheaper when the main model is expensive.

---

## Solution 6: Schema-Driven Field Registry — Centralize Projection Definitions

Maintain a central registry of field sets per entity type and task, so projection logic doesn't scatter across the codebase.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class ProjectionSchema:
    entity: str
    task: str
    fields: list[str]
    estimated_tokens: int

class FieldRegistry:
    """Central registry of entity projection schemas."""

    def __init__(self):
        self._schemas: dict[tuple[str, str], ProjectionSchema] = {}

    def register(self, schema: ProjectionSchema) -> None:
        self._schemas[(schema.entity, schema.task)] = schema

    def get_fields(self, entity: str, task: str) -> list[str] | None:
        schema = self._schemas.get((entity, task))
        return schema.fields if schema else None

    def project(self, entity: str, task: str, data: dict) -> dict:
        """Apply projection to a data dict."""
        fields = self.get_fields(entity, task)
        if fields is None:
            return data
        return {k: v for k, v in data.items() if k in fields}

    def estimated_tokens(self, entity: str, task: str) -> int:
        schema = self._schemas.get((entity, task))
        return schema.estimated_tokens if schema else 999

    def all_schemas(self) -> list[ProjectionSchema]:
        return list(self._schemas.values())

# Global registry — define once, use everywhere
registry = FieldRegistry()

registry.register(ProjectionSchema("user", "greeting",    ["name", "preferred_name"],                                  10))
registry.register(ProjectionSchema("user", "billing",     ["name", "email", "plan", "credits"],                        40))
registry.register(ProjectionSchema("user", "support",     ["name", "email", "plan", "ticket_count", "last_contact"],   60))
registry.register(ProjectionSchema("user", "full",        ["name", "email", "phone", "address", "plan", "created_at"], 120))
registry.register(ProjectionSchema("order", "summary",    ["order_id", "status", "total", "item_count"],               30))
registry.register(ProjectionSchema("order", "detail",     ["order_id", "status", "total", "items", "shipping"],        120))
registry.register(ProjectionSchema("product", "listing",  ["name", "price", "in_stock", "rating"],                     25))
registry.register(ProjectionSchema("product", "detail",   ["name", "price", "description", "specs", "rating"],         80))

async def agent_with_registry(
    entity: str,
    entity_data: dict,
    task: str,
    question: str,
) -> dict:
    projected = registry.project(entity, task, entity_data)
    token_budget = registry.estimated_tokens(entity, task)

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"{entity} data ({task}, ~{token_budget} tokens):\n{projected}\n\n{question}",
        }],
    )

    return {
        "response": resp.content[0].text,
        "fields_used": list(projected.keys()),
        "estimated_tokens": token_budget,
    }

# Usage:
# user_data = fetch_full_user(user_id)  # fetch once from DB
# result = await agent_with_registry("user", user_data, "billing", "How many credits do I have?")
# The projection reduces 120-token full record to 40-token billing view
```

**When to use**: Codebases where multiple agent workflows query the same entities. The registry makes field selection auditable ("why are we sending the password field to the LLM?") and easy to update.

---

## Comparison

| Solution | Server-Side | Client-Side | Semantically Accurate | Setup Cost | Token Reduction | Best For |
|---|---|---|---|---|---|---|
| SQL column selection | Yes | No | Yes | Low | 70–90% | SQL database backends |
| REST field filtering | Partial | Yes | Yes | Low | 60–85% | REST API backends |
| GraphQL field selection | Yes | No | Yes | Medium | 80–97% | GraphQL API backends |
| Context window budget | No | Yes | Partial | Low | 50–80% | Variable-size tool results |
| LLM summarization | No | Yes | Yes | Medium | 80–95% | Large documents/responses |
| Schema-driven registry | No | Yes | Yes | Medium | 70–90% | Multi-entity codebases |

**Rule of thumb**: Never inject a full database row or API response into an LLM context without explicit field selection. The cheapest optimization is to never fetch the fields you don't need. After that, trim or summarize before injecting anything over ~200 tokens.
