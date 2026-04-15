---
layout: solution
title: "Agent Calls Wrong Tool for the Task"
category: tool-failure
description: "Agent selects an inappropriate tool — searching when it should retrieve, writing when it should read, or using a destructive tool when a safe one exists — causing incorrect results or unintended side effects."
tags: [tool-failure, tool-selection, tool-description, guardrails, validation, routing]
---

## Symptom

Agent calls `delete_file` instead of `read_file`, or calls `web_search` when the user asked about local data, or invokes a write tool when a read would suffice. In logs:

```
Tool called: delete_record(id="user-42")
Expected:    get_record(id="user-42")
```

Downstream effects range from wrong answers to data loss.

## Root Cause

Tool selection is driven by tool descriptions and the current conversation context. Ambiguous names, overlapping descriptions, or insufficient context about which tool is appropriate for which scenario cause the model to pick the wrong one. Destructive and safe tools sitting at the same priority level also contribute.

## Fix

---

### Option 1 — Precision Tool Descriptions with Anti-Confusion Notes

Rewrite every tool description to explicitly state what it does NOT do and when NOT to use it. This narrows the model's selection surface.

```python
import json
import anthropic

client = anthropic.Anthropic()

# Ambiguous (bad) tool definitions
AMBIGUOUS_TOOLS = [
    {
        "name": "file_operation",
        "description": "Perform a file operation",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "operation": {"type": "string"},
            },
            "required": ["path", "operation"],
        },
    }
]

# Precise (good) tool definitions
PRECISE_TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file. "
            "Use this when the user wants to view, inspect, or check file contents. "
            "Do NOT use this to modify, write, or delete files. "
            "Safe: this operation never changes data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write or overwrite a file with new content. "
            "Use ONLY when the user explicitly requests creating or updating a file. "
            "Do NOT use this to read or inspect files. "
            "WARNING: This permanently overwrites existing content. Confirm with user before using."
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
    {
        "name": "delete_file",
        "description": (
            "Permanently delete a file. "
            "Use ONLY when the user explicitly says 'delete' or 'remove'. "
            "Do NOT use to read or inspect files. "
            "DANGER: This is irreversible. Always confirm intent before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "confirmed": {
                    "type": "boolean",
                    "description": "Set true only after user explicitly confirmed deletion",
                },
            },
            "required": ["path", "confirmed"],
        },
    },
]

def simulate_tool(name: str, args: dict) -> str:
    print(f"[TOOL CALLED] {name}({json.dumps(args)})")
    if name == "read_file":
        return f"Contents of {args['path']}: hello world"
    if name == "write_file":
        return f"Wrote {len(args['content'])} bytes to {args['path']}"
    if name == "delete_file":
        if not args.get("confirmed"):
            return "Deletion refused: confirmed=false"
        return f"Deleted {args['path']}"
    return "Unknown tool"

def run_agent(task: str, tools: list) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = simulate_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})

print("=== With ambiguous tools ===")
run_agent("Show me the contents of /etc/hosts", AMBIGUOUS_TOOLS)

print("\n=== With precise tools ===")
run_agent("Show me the contents of /etc/hosts", PRECISE_TOOLS)
```

**Expected Token Savings:** Negligible; prevents costly wrong-tool side effects
**Environment:** `pip install anthropic`

---

### Option 2 — Pre-Call Tool Intent Validator

Before executing any tool call, run a lightweight validation check: confirm that the tool name matches the user's inferred intent. Block mismatches.

```python
import json
import anthropic

client = anthropic.Anthropic()

# Intent patterns: user intends X → only these tools are acceptable
INTENT_RULES = {
    "read": {"allowed": {"read_file", "get_record", "list_files", "search"}, "blocked": {"write_file", "delete_file", "update_record"}},
    "write": {"allowed": {"write_file", "update_record", "create_record"}, "blocked": {"delete_file", "delete_record"}},
    "delete": {"allowed": {"delete_file", "delete_record"}, "blocked": set()},
    "search": {"allowed": {"search", "web_search", "find_files"}, "blocked": {"write_file", "delete_file"}},
}

DESTRUCTIVE_TOOLS = {"delete_file", "delete_record", "drop_table", "send_email"}

def infer_intent(user_message: str) -> str:
    msg = user_message.lower()
    if any(w in msg for w in ["delete", "remove", "destroy", "drop"]):
        return "delete"
    if any(w in msg for w in ["write", "create", "update", "save", "modify"]):
        return "write"
    if any(w in msg for w in ["find", "search", "look up", "query"]):
        return "search"
    return "read"

def validate_tool_call(tool_name: str, user_intent: str) -> tuple[bool, str]:
    rules = INTENT_RULES.get(user_intent, {})

    # Block destructive tools unless intent is explicitly delete
    if tool_name in DESTRUCTIVE_TOOLS and user_intent != "delete":
        return False, f"Tool '{tool_name}' is destructive but user intent is '{user_intent}'. Blocked."

    blocked = rules.get("blocked", set())
    if tool_name in blocked:
        return False, f"Tool '{tool_name}' is not allowed for intent '{user_intent}'."

    return True, "OK"

def safe_tool_call(name: str, args: dict, user_intent: str) -> str:
    allowed, reason = validate_tool_call(name, user_intent)
    if not allowed:
        print(f"[BLOCKED] {reason}")
        return f"Error: {reason}"

    print(f"[ALLOWED] {name}({json.dumps(args)})")
    # Simulate tool execution
    if name == "read_file":
        return f"File contents of {args.get('path', '?')}"
    if name == "delete_file":
        return f"Deleted {args.get('path', '?')}"
    return f"Executed {name}"

def run_validated_agent(user_message: str, tools: list) -> str:
    intent = infer_intent(user_message)
    print(f"[Intent detected: {intent}]")

    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = safe_tool_call(block.name, block.input, intent)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})

TOOLS = [
    {
        "name": "read_file",
        "description": "Read contents of a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "delete_file",
        "description": "Delete a file permanently",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
]

run_validated_agent("Show me what's in /var/log/app.log", TOOLS)
run_validated_agent("Delete /var/log/app.log", TOOLS)
```

**Expected Token Savings:** Prevents wasted tool execution cost on wrong calls
**Environment:** `pip install anthropic`

---

### Option 3 — Tool Router: Separate Read-Only and Write Tool Sets

Expose only read-only tools by default. Only inject write/delete tools when the conversation explicitly signals a mutating intent. This eliminates accidental wrong-tool selection.

```python
import anthropic

client = anthropic.Anthropic()

READ_ONLY_TOOLS = [
    {
        "name": "get_user",
        "description": "Retrieve a user record by ID. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "list_orders",
        "description": "List orders for a user. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "search_products",
        "description": "Search the product catalogue. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

WRITE_TOOLS = [
    {
        "name": "update_user",
        "description": "Update a user's profile. Use only when user explicitly asks to change their data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "fields": {"type": "object"},
            },
            "required": ["user_id", "fields"],
        },
    },
    {
        "name": "cancel_order",
        "description": "Cancel an order. Use only when user explicitly requests cancellation.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
]

MUTATING_KEYWORDS = {
    "update", "change", "modify", "edit", "cancel", "delete",
    "remove", "create", "add", "set", "fix", "correct",
}

def needs_write_tools(message: str) -> bool:
    words = set(message.lower().split())
    return bool(words & MUTATING_KEYWORDS)

def simulate_tool(name: str, args: dict) -> str:
    print(f"[TOOL] {name}({args})")
    if name == "get_user":
        return f'{{"id": "{args["user_id"]}", "name": "Alice", "email": "alice@example.com"}}'
    if name == "list_orders":
        return '[{"order_id": "ORD-1", "status": "shipped"}]'
    if name == "search_products":
        return '[{"name": "Widget", "price": 9.99}]'
    if name == "update_user":
        return f"Updated user {args['user_id']} with {args['fields']}"
    if name == "cancel_order":
        return f"Cancelled order {args['order_id']}"
    return "Unknown"

def routed_agent(user_message: str) -> str:
    write_mode = needs_write_tools(user_message)
    tools = READ_ONLY_TOOLS + (WRITE_TOOLS if write_mode else [])
    mode = "READ+WRITE" if write_mode else "READ-ONLY"
    print(f"[Tool set: {mode} — {len(tools)} tools exposed]")

    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = simulate_tool(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print(routed_agent("What orders does user-42 have?"))
print(routed_agent("Cancel order ORD-1 for user-42."))
```

**Expected Token Savings:** ~15% fewer tool description tokens in read-only mode
**Environment:** `pip install anthropic`

---

### Option 4 — Tool Selection Audit with Haiku Pre-Check

Before the main agent runs, use Haiku to audit which tool is most appropriate for the task. If the audit disagrees with the agent's choice, override it.

```python
import json
import anthropic

client = anthropic.Anthropic()

AVAILABLE_TOOLS_SUMMARY = """
- read_file: Read file contents. Safe. Use for viewing/inspecting files.
- write_file: Create or overwrite a file. Destructive. Use only when creating/updating.
- search_web: Search the internet for current information. Use for external facts.
- query_db: Query the local database. Use for internal structured data.
- send_email: Send an email to a user. Irreversible. Use only when explicitly requested.
"""

def audit_tool_selection(task: str, proposed_tool: str) -> tuple[bool, str]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=(
            "You are a tool selection auditor. "
            "Given a task and a proposed tool, decide if the tool is correct.\n"
            f"Available tools:\n{AVAILABLE_TOOLS_SUMMARY}\n"
            'Respond ONLY with JSON: {"correct": true/false, "reason": "...", "better_tool": "..." or null}'
        ),
        messages=[{
            "role": "user",
            "content": f"Task: {task}\nProposed tool: {proposed_tool}",
        }],
    )

    try:
        result = json.loads(response.content[0].text.strip())
        return result.get("correct", True), result.get("reason", "")
    except json.JSONDecodeError:
        return True, "Audit parse failed — allowing"

def execute_with_audit(task: str, proposed_tool: str, args: dict) -> str:
    is_correct, reason = audit_tool_selection(task, proposed_tool)

    if not is_correct:
        print(f"[AUDIT BLOCKED] Tool '{proposed_tool}' rejected: {reason}")
        return f"Tool '{proposed_tool}' was blocked by audit: {reason}"

    print(f"[AUDIT PASSED] {proposed_tool} — {reason or 'OK'}")
    # Simulate execution
    return f"Executed {proposed_tool}({json.dumps(args)})"

# Simulate agent deciding to call a tool
print(execute_with_audit(
    task="Show me the contents of config.yaml",
    proposed_tool="read_file",
    args={"path": "config.yaml"},
))

print(execute_with_audit(
    task="Show me the contents of config.yaml",
    proposed_tool="write_file",    # Wrong tool!
    args={"path": "config.yaml", "content": ""},
))

print(execute_with_audit(
    task="What's the weather in Tokyo today?",
    proposed_tool="query_db",      # Wrong tool!
    args={"query": "SELECT weather FROM cities WHERE name='Tokyo'"},
))
```

**Expected Token Savings:** Haiku audit costs ~50 tokens; saves costly wrong-tool side effects
**Environment:** `pip install anthropic`

---

### Option 5 — Confirmation Gate for Destructive Tools

Wrap all destructive tools with a mandatory confirmation step. The model must call `confirm_action` first; the destructive tool is blocked until confirmation is received.

```python
import json
import anthropic

client = anthropic.Anthropic()

DESTRUCTIVE_TOOLS = {"delete_record", "drop_table", "send_bulk_email", "revoke_access"}

PENDING_CONFIRMATIONS: dict[str, dict] = {}

TOOLS = [
    {
        "name": "get_record",
        "description": "Read a database record. Safe. No confirmation needed.",
        "input_schema": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
    {
        "name": "confirm_action",
        "description": (
            "Request user confirmation before performing a destructive action. "
            "ALWAYS call this before calling delete_record or any destructive tool. "
            "Pass the action description and let the user confirm."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_description": {"type": "string"},
                "tool_to_confirm": {"type": "string"},
                "tool_args": {"type": "object"},
            },
            "required": ["action_description", "tool_to_confirm", "tool_args"],
        },
    },
    {
        "name": "delete_record",
        "description": (
            "Permanently delete a database record. DESTRUCTIVE. "
            "You MUST call confirm_action first. If not confirmed, this will fail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
]

def handle_tool(name: str, args: dict, confirmed_actions: set) -> str:
    if name == "get_record":
        return json.dumps({"id": args["record_id"], "data": "some data"})

    if name == "confirm_action":
        tool = args["tool_to_confirm"]
        key = f"{tool}:{json.dumps(args['tool_args'], sort_keys=True)}"
        confirmed_actions.add(key)
        print(f"[CONFIRM] User confirmed: {args['action_description']}")
        return f"Confirmed. You may now proceed with {tool}."

    if name in DESTRUCTIVE_TOOLS:
        key = f"{name}:{json.dumps(args, sort_keys=True)}"
        if key not in confirmed_actions:
            print(f"[BLOCKED] {name} called without prior confirmation")
            return (
                f"Error: {name} requires prior confirmation via confirm_action. "
                "Please call confirm_action first."
            )
        print(f"[EXECUTED] {name}({args}) — confirmation verified")
        return f"Deleted record {args.get('record_id', '?')}"

    return f"Unknown tool: {name}"

def run_agent_with_gate(task: str) -> str:
    messages = [{"role": "user", "content": task}]
    confirmed_actions: set = set()

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=(
                "Always call confirm_action before any destructive operation. "
                "Never skip the confirmation step."
            ),
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = handle_tool(block.name, block.input, confirmed_actions)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print(run_agent_with_gate("Delete record user-99."))
```

**Expected Token Savings:** None — safety feature; prevents destructive wrong-tool calls
**Environment:** `pip install anthropic`

---

### Option 6 — Tool Call Logging and Mismatch Alerting

Log every tool call with the originating user intent. Run an async mismatch detector that flags anomalous tool selections for ops review without blocking the user.

```python
import json
import time
import threading
import anthropic
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class ToolCallLog:
    timestamp: str
    session_id: str
    user_intent: str
    tool_called: str
    tool_args: dict
    flagged: bool = False
    flag_reason: str = ""

TOOL_INTENT_MATRIX = {
    "read_file":   ["read", "view", "inspect", "show", "display"],
    "write_file":  ["write", "create", "save", "update", "modify"],
    "delete_file": ["delete", "remove", "destroy"],
    "search":      ["find", "search", "look", "query"],
    "send_email":  ["email", "send", "notify", "message"],
}

def check_mismatch(tool_name: str, user_intent: str) -> tuple[bool, str]:
    expected_intents = TOOL_INTENT_MATRIX.get(tool_name, [])
    if not expected_intents:
        return False, ""

    intent_words = user_intent.lower().split()
    if not any(w in " ".join(intent_words) for w in expected_intents):
        return True, (
            f"Tool '{tool_name}' expects intents {expected_intents} "
            f"but user intent was: '{user_intent}'"
        )
    return False, ""

alert_queue: list[ToolCallLog] = []
alert_lock = threading.Lock()

def async_alert_processor():
    while True:
        time.sleep(5)
        with alert_lock:
            flagged = [log for log in alert_queue if log.flagged]
            if flagged:
                print(f"\n[ALERT] {len(flagged)} suspicious tool calls detected:")
                for log in flagged:
                    print(f"  {log.timestamp} | {log.tool_called} | {log.flag_reason}")
                alert_queue.clear()

alert_thread = threading.Thread(target=async_alert_processor, daemon=True)
alert_thread.start()

def logged_tool_call(
    session_id: str,
    tool_name: str,
    tool_args: dict,
    user_intent: str,
    executor,
) -> str:
    flagged, reason = check_mismatch(tool_name, user_intent)

    log = ToolCallLog(
        timestamp=datetime.utcnow().isoformat(),
        session_id=session_id,
        user_intent=user_intent,
        tool_called=tool_name,
        tool_args=tool_args,
        flagged=flagged,
        flag_reason=reason,
    )

    with alert_lock:
        alert_queue.append(log)

    if flagged:
        print(f"[MISMATCH WARNING] {reason}")

    return executor(tool_name, tool_args)

def dummy_executor(name: str, args: dict) -> str:
    return f"Executed {name}({json.dumps(args)})"

# Simulate normal and anomalous tool calls
logged_tool_call("sess-1", "read_file", {"path": "/etc/hosts"}, "show me the hosts file", dummy_executor)
logged_tool_call("sess-1", "delete_file", {"path": "/etc/hosts"}, "show me the hosts file", dummy_executor)  # Flagged!
logged_tool_call("sess-2", "send_email", {"to": "admin"}, "notify admin about the error", dummy_executor)

time.sleep(6)  # Let alert processor fire
```

**Expected Token Savings:** None — observability feature; catches wrong-tool patterns before they become incidents
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Prevention Type | Blocking | Overhead | Best For |
|--------|----------------|----------|----------|----------|
| Precise Descriptions | Proactive | No | None | All agents (always apply) |
| Intent Validator | Proactive | Yes | Low | Safety-critical agents |
| Tool Router | Proactive | Yes | Low | CRUD applications |
| Haiku Audit | Proactive | Yes | ~50 tokens | High-stakes operations |
| Confirmation Gate | Reactive | Yes | One extra turn | Destructive operations |
| Mismatch Logging | Detective | No | None | Production monitoring |

**Recommended starting point:** Option 1 (Precise Descriptions) for all agents, Option 3 (Tool Router) for any agent with both read and write tools.
