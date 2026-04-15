---
layout: solution
title: "Agent Gets Stuck When Tool Returns Empty Results"
category: loop-stuck
description: "Agent calls a search or lookup tool that returns zero results, then enters a loop — rephrasing the same query, retrying with slight variations, or asking for clarification indefinitely instead of gracefully acknowledging no data was found."
tags: [loop-stuck, tool-failure, empty-results, graceful-degradation, search, fallback]
---

## Symptom

Agent calls `search_database(query="premium users in Chicago")` and gets `[]`. Instead of stopping, it:

```
Turn 1: search_database("premium users in Chicago") → []
Turn 2: search_database("premium Chicago users") → []
Turn 3: search_database("Chicago premium accounts") → []
Turn 4: search_database("users Chicago premium tier") → []
...
```

The loop continues until context fills up or the user gives up.

## Root Cause

The agent has no explicit policy for handling empty results. Without a "no results = stop and report" rule, the model infers that rephrasing might succeed — a reasonable heuristic in search UX, but pathological when applied indefinitely. The stop condition is missing.

## Fix

---

### Option 1 — Explicit Empty-Result Handling in System Prompt

Add a clear policy: if a search returns no results after one retry with a broader query, report "nothing found" and stop. Never loop more than twice.

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a data assistant with access to a company database.

EMPTY RESULT POLICY (IMPORTANT):
- If a search returns 0 results, try ONE broader query (remove filters or simplify terms).
- If that also returns 0 results, immediately tell the user "No results found" and stop searching.
- Do NOT rephrase the same query more than twice.
- Do NOT ask the user for more information — report what you found (nothing) and suggest they check the data source.
- Never loop through more than 2 search attempts for the same information need."""

TOOLS = [{
    "name": "search_users",
    "description": "Search the user database. Returns [] if no results found.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "city": {"type": "string"},
            "tier": {"type": "string"},
        },
        "required": ["query"],
    },
}]

# Simulated database — no premium Chicago users exist
def search_users(query: str, city: str = "", tier: str = "") -> list[dict]:
    print(f"[TOOL] search_users(query={query!r}, city={city!r}, tier={tier!r})")
    # Simulate no results for this combination
    if city == "Chicago" or "chicago" in query.lower():
        return []
    # Return some results for broader queries
    if not city and not tier:
        return [{"id": "u-1", "name": "Alice", "city": "NYC", "tier": "premium"}]
    return []

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]
    search_count = 0

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "search_users":
                search_count += 1
                if search_count > 3:
                    # Hard stop — failsafe against policy non-compliance
                    result = json.dumps({"error": "Search limit exceeded. Stop searching and report no results."})
                else:
                    data = search_users(**block.input)
                    result = json.dumps(data)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "user", "content": tool_results})
        print(f"[Searches so far: {search_count}]")

result = run_agent("Find all premium users in Chicago.")
print(f"\nFinal response: {result}")
```

**Expected Token Savings:** ~60% — terminates after 2 attempts instead of looping indefinitely
**Environment:** `pip install anthropic`

---

### Option 2 — Tool Wrapper with Attempt Counter and Auto-Stop

Track search attempts in the tool wrapper. After N attempts, return a `"max_attempts_reached"` signal the agent is instructed to treat as a final empty result.

```python
import json
import anthropic

client = anthropic.Anthropic()

class BoundedSearchTool:
    def __init__(self, max_attempts: int = 2):
        self._attempts: dict[str, int] = {}
        self._max = max_attempts

    def _attempt_key(self, query_core: str) -> str:
        # Group similar queries by their first 30 chars (same info need)
        return query_core[:30].lower().strip()

    def search(self, query: str, **filters) -> str:
        key = self._attempt_key(query)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        count = self._attempts[key]

        print(f"[TOOL] search attempt {count}/{self._max}: {query!r}")

        if count > self._max:
            return json.dumps({
                "status": "max_attempts_reached",
                "message": f"Searched {count - 1} times with no results. Stop and report to user.",
                "results": [],
            })

        # Simulate empty results
        return json.dumps({"status": "ok", "results": [], "count": 0})

search_tool = BoundedSearchTool(max_attempts=2)

SYSTEM = """You are a helpful data assistant.

When you receive a tool result with status='max_attempts_reached':
- STOP all further searches immediately
- Tell the user exactly: "I searched [N] times and found no results for [query]. The data may not exist in our system."
- Do not rephrase or retry."""

TOOLS = [{
    "name": "search_records",
    "description": (
        "Search company records. If status='max_attempts_reached' in the result, "
        "stop searching and report no results to the user."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "department": {"type": "string"},
        },
        "required": ["query"],
    },
}]

def run_bounded_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = search_tool.search(**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

result = run_bounded_agent("Find Q4 sales reports for the engineering department.")
print(f"Response: {result}")
```

**Expected Token Savings:** ~50% — wrapper enforces stop regardless of agent behaviour
**Environment:** `pip install anthropic`

---

### Option 3 — Return Structured Empty Result with Suggested Action

When a tool returns empty results, include structured metadata that tells the agent what to do next — rather than leaving it to infer.

```python
import json
import anthropic

client = anthropic.Anthropic()

def search_inventory(product_name: str, warehouse: str = "", in_stock_only: bool = False) -> str:
    print(f"[TOOL] search_inventory({product_name!r}, warehouse={warehouse!r})")

    # Simulate: no results for this specific query
    has_results = not warehouse and not in_stock_only

    if has_results:
        return json.dumps({
            "status": "ok",
            "results": [{"sku": "WDG-001", "name": product_name, "qty": 0, "warehouse": "WH-1"}],
            "count": 1,
        })

    # Return structured empty result with guidance
    return json.dumps({
        "status": "no_results",
        "results": [],
        "count": 0,
        "agent_instruction": (
            "No results found for this query. "
            "Do NOT retry with different phrasing. "
            "Tell the user the item was not found and suggest they contact the warehouse team directly."
        ),
        "suggested_response": (
            f"No inventory records found for '{product_name}'"
            + (f" in warehouse '{warehouse}'" if warehouse else "")
            + ". Please contact the warehouse team for manual verification."
        ),
    })

SYSTEM = """You are an inventory assistant.

When a tool returns status='no_results', follow the agent_instruction field exactly.
Use the suggested_response as a template for your reply.
Do not perform additional searches after receiving status='no_results'."""

TOOLS = [{
    "name": "search_inventory",
    "description": "Search warehouse inventory. Returns structured result with instructions when empty.",
    "input_schema": {
        "type": "object",
        "properties": {
            "product_name": {"type": "string"},
            "warehouse": {"type": "string"},
            "in_stock_only": {"type": "boolean"},
        },
        "required": ["product_name"],
    },
}]

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = search_inventory(**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

result = run_agent("Find widgets in warehouse WH-3, in stock only.")
print(f"Response: {result}")
```

**Expected Token Savings:** ~40% — structured instruction eliminates retry loops
**Environment:** `pip install anthropic`

---

### Option 4 — Fallback Chain: Try Broad, Then Report

Define a fallback chain for searches: try specific → try broad → report. The agent follows the chain and stops after exhausting it, never looping.

```python
import json
import anthropic

client = anthropic.Anthropic()

def search_api(query: str, filters: dict | None = None) -> list[dict]:
    """Simulated search — returns empty for specific queries, some for broad."""
    print(f"[API] search(query={query!r}, filters={filters})")
    if filters:
        return []  # Specific filtered search: no results
    if "widget" in query.lower():
        return [{"id": "p-1", "name": "Generic Widget", "category": "hardware"}]
    return []

SYSTEM = """You are a product search assistant.

SEARCH STRATEGY — follow this sequence exactly:
1. First: search with the user's exact terms and any specified filters
2. If empty: search again with broader terms (remove filters, simplify query)
3. If still empty: tell the user "No products found for [original query]" and STOP

Never perform more than 2 searches per user request.
After 2 empty results, report immediately — do not ask for more info or retry."""

TOOLS = [{
    "name": "search_products",
    "description": (
        "Search product catalogue. "
        "IMPORTANT: Only call this tool at most twice per user request. "
        "After 2 empty results, stop and report."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "category": {"type": "string", "description": "Optional category filter"},
            "min_price": {"type": "number"},
            "max_price": {"type": "number"},
        },
        "required": ["query"],
    },
}]

def run_fallback_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]
    search_count = 0

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                search_count += 1
                if search_count > 2:
                    # Failsafe — force stop
                    result = json.dumps({
                        "results": [],
                        "error": "Search limit exceeded. Report no results now.",
                    })
                else:
                    filters = {k: v for k, v in block.input.items() if k != "query"}
                    results = search_api(block.input["query"], filters or None)
                    result = json.dumps({"results": results, "count": len(results)})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

# Test: specific query → broad fallback → report
print("=== Filtered search (no results expected) ===")
result = run_fallback_agent("Find red widgets under $10 in the electronics category.")
print(f"Response: {result}\n")

print("=== Broad query that finds results ===")
result = run_fallback_agent("Find widgets.")
print(f"Response: {result}")
```

**Expected Token Savings:** ~55% — predictable 1–2 search calls, never more
**Environment:** `pip install anthropic`

---

### Option 5 — Empty Result Detection with Turn Budget

Track the turn count and inject a "stop now" message when the agent has used too many turns on the same task without finding results.

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a research assistant with database access.
Search for information using the available tools.
If your searches return no results, report this clearly and stop."""

TOOLS = [{
    "name": "search_database",
    "description": "Search the company database.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}]

def simulate_empty_search(query: str) -> str:
    print(f"[DB] search({query!r}) → []")
    return json.dumps({"results": [], "total": 0})

def run_with_turn_budget(task: str, max_tool_turns: int = 2) -> str:
    messages = [{"role": "user", "content": task}]
    tool_turn_count = 0
    consecutive_empty = 0

    while True:
        # Inject stop instruction when budget is nearly exhausted
        system = SYSTEM
        if tool_turn_count >= max_tool_turns:
            system = (
                SYSTEM + "\n\n"
                "STOP INSTRUCTION: You have reached the search limit. "
                "Do NOT call any more tools. "
                "Report to the user that no results were found and stop."
            )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=TOOLS if tool_turn_count < max_tool_turns else [],  # Remove tools after budget
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_turn_count += 1
                result_str = simulate_empty_search(block.input["query"])
                result_data = json.loads(result_str)

                if result_data["total"] == 0:
                    consecutive_empty += 1
                else:
                    consecutive_empty = 0

                # Append feedback about consecutive empty results
                if consecutive_empty >= 2:
                    result_str = json.dumps({
                        **result_data,
                        "note": f"This is the {consecutive_empty}nd consecutive empty result. Please stop searching and report.",
                    })

                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_str})

        messages.append({"role": "user", "content": tool_results})
        print(f"[Turn budget: {tool_turn_count}/{max_tool_turns}]")

result = run_with_turn_budget("Find all sales data for project Alpha from Q3 2020.")
print(f"Response: {result}")
```

**Expected Token Savings:** ~50% — dynamic system prompt + tool removal enforces hard stop
**Environment:** `pip install anthropic`

---

### Option 6 — Graceful Degradation with Alternative Suggestions

When results are empty, instead of looping, the agent provides alternatives: suggests related queries the user could try, or escalates to a human.

```python
import json
import anthropic

client = anthropic.Anthropic()

def search_knowledge_base(query: str, category: str = "") -> str:
    print(f"[KB] search({query!r})")
    # Simulate: no results + alternative suggestions
    return json.dumps({
        "results": [],
        "count": 0,
        "alternatives": [
            f"Try searching for '{query.split()[0]}' without additional terms",
            "Browse the full catalogue at /catalogue",
            "Contact support at support@acme.com",
        ],
        "escalation_available": True,
    })

def escalate_to_human(reason: str) -> str:
    print(f"[ESCALATE] Reason: {reason}")
    return json.dumps({
        "ticket_id": "TKT-9421",
        "eta": "2-4 hours",
        "message": "A support agent will follow up via email.",
    })

SYSTEM = """You are a knowledge base assistant.

When search returns 0 results:
1. Tell the user clearly that no results were found
2. Offer the 'alternatives' suggestions from the search result
3. Ask if they want to escalate to a human agent (use escalate_to_human tool)
4. Do NOT retry the same or similar searches

This is the correct flow — follow it exactly."""

TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": "Search the knowledge base. On empty results, provides alternative suggestions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Create a support ticket for human agent follow-up.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]

def run_graceful_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "search_knowledge_base":
                    result = search_knowledge_base(**block.input)
                elif block.name == "escalate_to_human":
                    result = escalate_to_human(**block.input)
                else:
                    result = json.dumps({"error": "Unknown tool"})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print("=== Empty result with graceful degradation ===")
result = run_graceful_agent("How do I configure SAML SSO for enterprise accounts?")
print(f"Response:\n{result}")
```

**Expected Token Savings:** ~45% — single search then graceful stop; no retry loop
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Stop Mechanism | Agent Compliance | User Feedback | Best For |
|--------|---------------|-----------------|---------------|----------|
| System Prompt Policy | Instruction | Medium | Via agent | Quick fix for any agent |
| Bounded Tool Wrapper | Hard limit | High | Via signal | Tooling-layer enforcement |
| Structured Empty Result | Schema guidance | High | Via template | APIs you control |
| Fallback Chain | Sequence contract | High | Via agent | Search-heavy agents |
| Turn Budget | Dynamic prompt | Very High | Via agent | Complex multi-turn agents |
| Graceful Degradation | Policy + escalation | High | Rich alternatives | Customer-facing agents |

**Recommended starting point:** Option 2 (Bounded Tool Wrapper) for immediate protection. Add Option 3 (Structured Empty Result) if you control the tool implementation.
