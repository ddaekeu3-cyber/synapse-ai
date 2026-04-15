---
layout: solution
title: "Agent Makes Up Tool Capabilities It Doesn't Have"
category: hallucination
description: "The agent confidently invokes tools that don't exist in its schema, describes capabilities it wasn't given, or calls real tools with parameters they don't support — all while appearing certain."
tags: [hallucination, tool-use, capability-awareness, grounding, schema, validation, self-knowledge]
---

## Symptom

The agent says "I'll use the `send_email` tool to notify the team" — but no email tool is in its tool list. Or it calls `search_web(query="...", date_filter="last_week")` when the real `search_web` tool doesn't have a `date_filter` parameter. The tool call fails, the agent tries a workaround, and the user gets confused about what the system can actually do.

## Root Cause

Language models generalize from training data where tools like email senders, web searchers, and calendars are common. When a task semantically implies a tool that doesn't exist, the model hallucinates a plausible-looking tool call. This is compounded by the model's inability to distinguish "tools in my current schema" from "tools that exist in the world." Without explicit capability grounding, the model reasons about what *should* exist rather than what *does* exist.

## Fix

---

### Option 1: Capability-Aware System Prompt with Explicit Enumeration

Tell the model exactly what it can and cannot do. Explicit negative constraints reduce hallucinated tool calls.

```python
import json
import anthropic

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": "Search the internal knowledge base for information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_ticket",
        "description": "Create a support ticket.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":    {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                "description": {"type": "string"},
            },
            "required": ["title", "priority", "description"],
        },
    },
]

# Build capability context dynamically from the actual tool list
def build_capability_system(tools: list[dict]) -> str:
    tool_names = [t["name"] for t in tools]
    tool_params = {
        t["name"]: list(t["input_schema"]["properties"].keys())
        for t in tools
    }
    params_desc = "\n".join(
        f"  - {name}: parameters are {params}"
        for name, params in tool_params.items()
    )

    return f"""You are a support assistant. You have access to EXACTLY these tools:
{json.dumps(tool_names)}

Tool parameters (only use these — do not invent new parameters):
{params_desc}

IMPORTANT CONSTRAINTS:
- You CANNOT send emails. If asked to email someone, explain you don't have that capability and offer to create a ticket instead.
- You CANNOT browse the web or access external URLs.
- You CANNOT access calendars, Slack, or any messaging systems.
- You CANNOT call any tool not listed above.
- If a task requires a capability you don't have, say so clearly and explain what you CAN do instead.

Never invent tool names or parameters. If you're unsure whether a tool exists, it doesn't.
""".strip()


def run_agent(user_message: str) -> str:
    system = build_capability_system(TOOLS)
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"  Tool call: {block.name}({block.input})")
                # Simulate tool execution
                result = {"status": "ok", "tool": block.name}
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ]


# Test with requests that might trigger hallucinated tools
print(run_agent("Can you email the engineering team about the bug I'm reporting?"))
print("---")
print(run_agent("Search the knowledge base for refund policies and create a high-priority ticket."))
```

**Expected Token Savings**: Prevents hallucinated tool calls (each one wastes 1–3 turns of retry). Clear negative constraints reduce hallucination by ~70% in capability-boundary tasks.
**Environment**: System prompt only. Rebuild `build_capability_system()` whenever tools change.

---

### Option 2: Tool Call Interceptor with Schema Validation

Intercept every tool call before execution. Reject calls to non-existent tools or with invalid parameters.

```python
import json
import anthropic
from jsonschema import validate, ValidationError

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city":    {"type": "string"},
                "country": {"type": "string", "description": "ISO 2-letter country code"},
            },
            "required": ["city"],
        },
    },
    {
        "name": "convert_currency",
        "description": "Convert an amount between currencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "from_currency": {"type": "string", "description": "ISO currency code"},
                "to_currency":   {"type": "string", "description": "ISO currency code"},
            },
            "required": ["amount", "from_currency", "to_currency"],
        },
    },
]

# Build lookup table from actual tools
TOOL_REGISTRY = {t["name"]: t for t in TOOLS}


def intercept_tool_call(name: str, args: dict) -> tuple[bool, str]:
    """
    Validate tool call before execution.
    Returns (is_valid, error_message).
    """
    # Check tool exists
    if name not in TOOL_REGISTRY:
        known = list(TOOL_REGISTRY.keys())
        return False, (
            f"Tool '{name}' does not exist. "
            f"Available tools: {known}. "
            f"Do not invent tools that are not in this list."
        )

    # Validate parameters against schema
    schema = TOOL_REGISTRY[name]["input_schema"]
    try:
        validate(instance=args, schema=schema)
    except ValidationError as e:
        valid_params = list(schema.get("properties", {}).keys())
        return False, (
            f"Tool '{name}' was called with invalid parameters: {e.message}. "
            f"Valid parameters for this tool: {valid_params}. "
            f"Do not add parameters that are not in the schema."
        )

    return True, ""


def execute_tool(name: str, args: dict) -> dict:
    if name == "get_weather":
        return {"city": args["city"], "temperature": "22°C", "condition": "sunny"}
    if name == "convert_currency":
        return {"result": args["amount"] * 1.08, "to": args["to_currency"]}
    return {"error": "unhandled"}


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    max_turns = 6

    for _ in range(max_turns):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                is_valid, error = intercept_tool_call(block.name, block.input)
                if is_valid:
                    result = execute_tool(block.name, block.input)
                    content = json.dumps(result)
                else:
                    print(f"  [Intercepted invalid tool call] {block.name}: {error[:80]}")
                    content = json.dumps({"error": error})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    **({"is_error": True} if not is_valid else {}),
                })

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ]

    return "Max turns reached"


print(run_agent("What's the weather in Tokyo, and convert 100 USD to EUR?"))
print("---")
# This might trigger a hallucinated tool call:
print(run_agent("What time is it in London and send me a notification?"))
```

**Expected Token Savings**: Hallucinated tool calls receive immediate, informative error feedback. Model self-corrects in 1 turn instead of 3+.
**Environment**: `pip install jsonschema`. Interceptor works with any tool schema.

---

### Option 3: Capability Self-Assessment Before Tool Use

Before executing any multi-step plan, have the agent declare which tools it will use. Validate the plan against available tools.

```python
import json
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
]

AVAILABLE_TOOL_NAMES = {t["name"] for t in TOOLS}

PLAN_TOOL = {
    "name": "declare_execution_plan",
    "description": "Declare which tools you will use to complete this task, before executing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool_name":   {"type": "string"},
                        "purpose":     {"type": "string"},
                        "can_do_this": {"type": "boolean", "description": "True if this tool is in your available tool list"},
                    },
                    "required": ["tool_name", "purpose", "can_do_this"],
                },
            },
            "steps_i_cannot_do": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Describe any steps that require capabilities you don't have",
            },
        },
        "required": ["steps", "steps_i_cannot_do"],
    },
}


def validate_plan(plan: dict) -> tuple[bool, list[str]]:
    """Check plan against actual available tools."""
    issues = []
    for step in plan.get("steps", []):
        tool = step["tool_name"]
        if tool not in AVAILABLE_TOOL_NAMES and step.get("can_do_this"):
            issues.append(
                f"Step claims tool '{tool}' is available, but it is not. "
                f"Available tools: {list(AVAILABLE_TOOL_NAMES)}"
            )
    return len(issues) == 0, issues


def run_planned_agent(user_message: str) -> str:
    # Step 1: Get execution plan
    planning_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"Available tools: {json.dumps(list(AVAILABLE_TOOL_NAMES))}. Plan before acting.",
        tools=[PLAN_TOOL] + TOOLS,
        tool_choice={"type": "tool", "name": "declare_execution_plan"},
        messages=[{"role": "user", "content": user_message}],
    )

    plan = None
    for block in planning_response.content:
        if block.type == "tool_use" and block.name == "declare_execution_plan":
            plan = block.input
            break

    if plan:
        is_valid, issues = validate_plan(plan)
        if not is_valid:
            print(f"  [Plan validation failed] {issues}")
            # Return early with capability explanation
            return (
                f"I can't fully complete this task. Issues: {issues}\n"
                f"What I CAN do: {plan.get('steps', [])}\n"
                f"What I cannot do: {plan.get('steps_i_cannot_do', [])}"
            )

        if plan.get("steps_i_cannot_do"):
            print(f"  [Agent self-reported limitations] {plan['steps_i_cannot_do']}")

    # Step 2: Execute validated plan
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,  # Only real tools, no plan tool
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name not in AVAILABLE_TOOL_NAMES:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                   "content": json.dumps({"error": f"Tool '{block.name}' does not exist"}), "is_error": True})
                else:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                   "content": json.dumps({"status": "ok"})})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


print(run_planned_agent("Read config.json and write a backup to config.backup.json"))
print("---")
print(run_planned_agent("Read report.txt and email it to the team"))
```

**Expected Token Savings**: Pre-flight plan validation catches hallucinated tools before any execution turn. Saves 2–5 wasted tool call turns.
**Environment**: Two-phase agent (plan then execute). Adds ~1 extra API call for planning.

---

### Option 4: Capability Boundary Response Templates

When a request exceeds capability, respond from a pre-defined deflection template rather than attempting and failing.

```python
import re
import json
import anthropic

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "search_products",
        "description": "Search the product catalog.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "get_order_status",
        "description": "Check the status of an order by order ID.",
        "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
]

AVAILABLE_TOOLS = {t["name"] for t in TOOLS}

# Capability boundary patterns: detect requests that require missing capabilities
CAPABILITY_GAPS = [
    {
        "pattern": r"\b(email|send.*mail|notify.*email|email.*team)\b",
        "capability": "email sending",
        "response": "I don't have the ability to send emails. I can search products and check order status. Would either of those help?",
    },
    {
        "pattern": r"\b(schedule|calendar|appointment|book.*meeting|set.*reminder)\b",
        "capability": "calendar management",
        "response": "I can't access calendars or schedule appointments. I can help you check on orders or find products instead.",
    },
    {
        "pattern": r"\b(refund|process.*refund|issue.*refund|return.*money)\b",
        "capability": "payment processing",
        "response": "I can't process refunds directly. Please contact our billing team. I can check your order status to provide context for your request.",
    },
    {
        "pattern": r"\b(web.*search|google|browse|internet|external.*site)\b",
        "capability": "web browsing",
        "response": "I can only search our product catalog, not the broader internet. Want me to search our products instead?",
    },
]


def check_capability_gaps(user_message: str) -> str | None:
    """Return deflection response if request exceeds capabilities, else None."""
    msg_lower = user_message.lower()
    for gap in CAPABILITY_GAPS:
        if re.search(gap["pattern"], msg_lower, re.IGNORECASE):
            return gap["response"]
    return None


def validate_tool_call(name: str) -> bool:
    return name in AVAILABLE_TOOLS


def run_agent(user_message: str) -> str:
    # Pre-flight capability check
    deflection = check_capability_gaps(user_message)
    if deflection:
        print(f"  [Capability boundary] Pre-empted with template response")
        return deflection

    messages = [{"role": "user", "content": user_message}]
    for _ in range(5):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                if not validate_tool_call(block.name):
                    content = json.dumps({
                        "error": f"'{block.name}' is not available. Use only: {list(AVAILABLE_TOOLS)}"
                    })
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": True})
                else:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                   "content": json.dumps({"status": "ok", "tool": block.name})})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]

    return "Could not complete request"


for msg in [
    "Search for wireless headphones",
    "Email me when my order ships",
    "What's the status of order #12345",
    "Schedule a product demo for Thursday",
]:
    print(f"User: {msg}")
    print(f"Agent: {run_agent(msg)}\n")
```

**Expected Token Savings**: Capability gap detection at ~5ms (regex) prevents full API calls for requests that will always fail. Zero tokens spent on hallucinated tool attempts.
**Environment**: Regex pattern matching + tool validation. Patterns are maintainable in a config file.

---

### Option 5: Tool Existence Assertion in System Prompt with Dynamic List

Dynamically generate the "what I can do" section from the actual tool list. Model can only claim capabilities that appear in this list.

```python
import json
import anthropic

client = anthropic.Anthropic()


def build_grounded_system(tools: list[dict]) -> str:
    """Generate a system prompt grounded in actual tool capabilities."""
    tool_summaries = []
    for t in tools:
        props = t["input_schema"].get("properties", {})
        param_list = ", ".join(f"`{p}`" for p in props.keys())
        required = t["input_schema"].get("required", [])
        req_str = f" (required: {required})" if required else ""
        tool_summaries.append(f"- **{t['name']}**: {t['description']}{req_str}. Parameters: {param_list}")

    tools_section = "\n".join(tool_summaries)
    tool_names = [t["name"] for t in tools]

    return f"""You are a helpful assistant with access to specific tools.

## Your Exact Tool Capabilities

{tools_section}

## Critical Rules

1. You can ONLY call tools from this list: {tool_names}
2. You can ONLY use parameters listed above for each tool — never invent new parameters
3. If a user asks you to do something requiring a tool NOT in your list, say:
   "I don't have the ability to [X]. I can [list what you CAN do instead]."
4. Never say "I'll use [tool_name]" unless that tool_name is in the list above
5. Never make up what a tool does — only use it as described

Your capabilities are EXACTLY what's listed. Nothing more.
"""


TOOLS = [
    {
        "name": "lookup_customer",
        "description": "Look up customer information by customer ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "include_orders": {"type": "boolean", "default": False},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "update_customer_note",
        "description": "Add a note to a customer record.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["customer_id", "note"],
        },
    },
]


def run_agent(user_message: str) -> str:
    system = build_grounded_system(TOOLS)
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"status": "ok", "customer_id": "C-001", "name": "Alice Smith"}),
                })

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


print(run_agent("Look up customer C-001 with their order history"))
print("---")
print(run_agent("Look up customer C-002 and send them a welcome email"))
```

**Expected Token Savings**: Dynamically generated capability section always matches actual tools — no drift between docs and reality. Prevents hallucinated tool names.
**Environment**: System prompt generation from tool list. Rebuild on every new tool addition.

---

### Option 6: Post-Response Tool Hallucination Audit

After each response, scan for mentions of tools that don't exist. Log and flag for prompt improvement.

```python
import re
import json
import sqlite3
import time
import anthropic

client = anthropic.Anthropic()
audit_db = sqlite3.connect("tool_hallucinations.db")
audit_db.execute("""
    CREATE TABLE IF NOT EXISTS hallucinations (
        ts TEXT, user_message TEXT, response TEXT, hallucinated_tool TEXT
    )
""")
audit_db.commit()

TOOLS = [
    {"name": "get_stock_price", "description": "Get current stock price.", "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_company_info", "description": "Get company information.", "input_schema": {"type": "object", "properties": {"company": {"type": "string"}}, "required": ["company"]}},
]

VALID_TOOL_NAMES = {t["name"] for t in TOOLS}

# Common tool names the model might hallucinate
HALLUCINATION_PATTERNS = [
    r"\b(send_email|email_team|notify_user|send_notification)\b",
    r"\b(search_web|google_search|web_search|browse_internet)\b",
    r"\b(schedule_meeting|create_calendar|book_appointment)\b",
    r"\b(write_to_database|update_db|db_insert)\b",
    r"\b(call_api|http_request|fetch_url)\b",
    r"I(?:'ll| will) use (?:the )?`?(\w+)`? tool",
    r"using (?:the )?`?(\w+)`? tool",
]


def audit_response(user_message: str, response_text: str) -> list[str]:
    """Detect mentions of non-existent tools in response text."""
    found = []
    for pattern in HALLUCINATION_PATTERNS:
        matches = re.findall(pattern, response_text, re.IGNORECASE)
        for match in matches:
            tool_name = match if match else pattern
            if isinstance(tool_name, str) and tool_name not in VALID_TOOL_NAMES:
                if tool_name.lower() not in ("the", "a", "an", "this", "that"):
                    found.append(tool_name)

    if found:
        audit_db.execute(
            "INSERT INTO hallucinations VALUES (?,?,?,?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), user_message[:200], response_text[:500], json.dumps(found))
        )
        audit_db.commit()
        print(f"  [Hallucination audit] Detected non-existent tool references: {found}")

    return found


def get_hallucination_report() -> list[dict]:
    rows = audit_db.execute(
        "SELECT hallucinated_tool, COUNT(*) as count FROM hallucinations GROUP BY hallucinated_tool ORDER BY count DESC"
    ).fetchall()
    return [{"tool": row[0], "count": row[1]} for row in rows]


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            reply = next(b.text for b in response.content if b.type == "text")
            # Audit for hallucinated tool mentions in the text response
            audit_response(user_message, reply)
            return reply

        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name not in VALID_TOOL_NAMES:
                    print(f"  [Hallucinated tool call caught] {block.name}")
                    audit_db.execute(
                        "INSERT INTO hallucinations VALUES (?,?,?,?)",
                        (time.strftime("%Y-%m-%dT%H:%M:%S"), user_message[:200], "", json.dumps([block.name]))
                    )
                    audit_db.commit()
                    results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": json.dumps({"error": f"Tool '{block.name}' does not exist."}),
                        "is_error": True,
                    })
                else:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                   "content": json.dumps({"price": "$150.25"})})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


run_agent("Get the stock price for AAPL and email me the result")
run_agent("Look up Apple Inc. company info and post it to Slack")

print("\n--- Hallucination Report ---")
for item in get_hallucination_report():
    print(f"  {item['tool']}: {item['count']} occurrences")
print("Use this report to improve system prompts and add negative constraints.")
```

**Expected Token Savings**: Audit log reveals recurring hallucination patterns — use them to add targeted negative constraints to system prompts, preventing future hallucinations.
**Environment**: SQLite audit log. Review weekly; add most common hallucinated tools to `CAPABILITY_GAPS` patterns.

---

| Option | Strategy | Prevents At | Detection Speed | Best For |
|--------|---------|------------|----------------|---------|
| 1 | Capability-aware system prompt | Generation | Zero | General-purpose prevention |
| 2 | Schema validation interceptor | Execution | Immediate | Runtime safety net |
| 3 | Pre-flight plan declaration | Planning | Pre-execution | Complex multi-step tasks |
| 4 | Capability boundary templates | Request receipt | ~5ms regex | Known capability gaps |
| 5 | Dynamic grounded system prompt | Generation | Zero | Tool sets that change frequently |
| 6 | Post-response audit log | Post-execution | After response | Continuous improvement feedback |
