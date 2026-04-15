---
layout: solution
title: "Agent Calls Wrong Tool When Tool Names or Descriptions Are Similar"
category: tool-failure
description: "The agent has get_user and get_user_profile — it calls the wrong one. It has search_documents and search_web and picks the wrong context. It invokes send_email when send_notification was intended. Similar tool names cause the model to guess wrong. Bad tool descriptions don't distinguish when each should be used. The wrong tool runs with real side effects."
tags: [tool-failure, tool-selection, tool-naming, tool-description, disambiguation, system-prompt, agent-design]
---

# Agent Calls Wrong Tool When Tool Names or Descriptions Are Similar

## Symptom
- Agent calls `search_documents` when it should call `search_web` (or vice versa)
- `send_message` called instead of `send_draft` — message sent prematurely
- `delete_file` called instead of `archive_file` — destructive wrong action
- Agent uses a read tool when a write tool was needed
- Two similar tools exist and the agent alternates between them inconsistently
- Tool selection is correct 80% of the time but fails on the ambiguous 20%

## Root Cause
Tool selection depends on the model matching the user's intent to a tool's name and description. When two tools have similar names, overlapping descriptions, or unclear "when to use" guidance, the model's choice becomes probabilistic. The fix is to: (1) use distinct, action-verb-first names, (2) write descriptions that explicitly state when NOT to use the tool, (3) use a system prompt to define tool selection rules, and (4) add a tool-routing layer for high-stakes selections.

## Fix

### Option 1: Rename tools to be unambiguous — action-verb + clear object

```python
import anthropic

client = anthropic.Anthropic()

# WRONG — similar names, vague descriptions:
BAD_TOOLS = [
    {
        "name": "search",
        "description": "Search for information",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    },
    {
        "name": "find",
        "description": "Find relevant results",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    },
    {
        "name": "lookup",
        "description": "Look up information",
        "input_schema": {"type": "object", "properties": {"term": {"type": "string"}}, "required": ["term"]}
    }
]

# RIGHT — distinct names, explicit when-to-use guidance:
GOOD_TOOLS = [
    {
        "name": "search_internal_knowledge_base",
        "description": (
            "Search the company's internal documentation, policies, and knowledge base. "
            "Use this when the user asks about internal processes, company policies, or product documentation. "
            "Do NOT use this for current events, public information, or questions the knowledge base wouldn't cover."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "doc_type": {
                    "type": "string",
                    "enum": ["policy", "product", "process", "all"],
                    "description": "Type of document to search"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_public_web",
        "description": (
            "Search the public internet for current information. "
            "Use this for: current events, public company information, general knowledge not in the internal KB, "
            "and any question that requires up-to-date information. "
            "Do NOT use this for internal company data — use search_internal_knowledge_base instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The web search query"},
                "recency": {
                    "type": "string",
                    "enum": ["any", "past_week", "past_month", "past_year"],
                    "default": "any"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "lookup_user_by_id",
        "description": (
            "Retrieve a specific user record by their exact user ID. "
            "Use this when you have the user's ID and need their profile data. "
            "Do NOT use this to search for users by name or email — use search_users_by_name for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "The exact user ID (format: usr_XXXXXXXX)"}
            },
            "required": ["user_id"]
        }
    }
]

def call_agent_with_clear_tools(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=GOOD_TOOLS,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text
```

### Option 2: Tool selection rules in system prompt — explicit disambiguation guide

```python
import anthropic

client = anthropic.Anthropic()

TOOL_SELECTION_SYSTEM = """## Tool Selection Rules

Follow these rules when choosing which tool to call:

### Search tools
- `search_internal_knowledge_base`: Use ONLY for internal company data, policies, procedures
- `search_public_web`: Use for anything external, current events, public information
- When in doubt about the source: prefer search_internal_knowledge_base for work-related questions

### User data tools
- `get_user_profile`: Returns the CURRENT user's own profile (no arguments needed)
- `lookup_user_by_id`: Returns ANY user's data by their user_id (requires a known user_id)
- `search_users_by_name`: Finds users whose name matches a search string (for finding someone by name)
- NEVER use lookup_user_by_id to find the current user — use get_user_profile

### Message tools
- `save_draft_message`: Creates a draft — does NOT send. Use when unsure if user wants to send.
- `send_message`: Immediately sends — cannot be undone. Only call after user explicitly confirms sending.
- `schedule_message`: Sends at a future time. Requires a send_at timestamp.
- ALWAYS use save_draft_message first unless the user explicitly said "send" or "send now"

### File tools
- `read_file`: Non-destructive read. Use freely.
- `write_file`: Overwrites contents. Confirm before calling if user hasn't explicitly said to write.
- `archive_file`: Moves to archive (recoverable). Prefer over delete_file.
- `delete_file`: Permanent deletion. Ask for confirmation before calling. Never call unless user explicitly says "delete".

### When two tools seem equally applicable
State which tool you're going to use and why, then call it. Example:
"I'll use search_internal_knowledge_base since this is a question about internal policy."
"""

TOOLS_WITH_OVERLAP = [
    {
        "name": "get_user_profile",
        "description": "Get the current authenticated user's profile and preferences. No arguments needed.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "lookup_user_by_id",
        "description": "Get any user's public profile by their user_id. Use when you know the specific user_id.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"]
        }
    },
    {
        "name": "save_draft_message",
        "description": "Save a message as a draft. Does not send. Safe to call without confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}
            },
            "required": ["to", "body"]
        }
    },
    {
        "name": "send_message",
        "description": "Send a message immediately. IRREVERSIBLE. Only call after explicit user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}
            },
            "required": ["to", "body"]
        }
    }
]

def chat_with_tool_rules(message: str, history: list[dict] | None = None) -> str:
    messages = (history or []) + [{"role": "user", "content": message}]
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=TOOL_SELECTION_SYSTEM,
        tools=TOOLS_WITH_OVERLAP,
        messages=messages
    )
    return response.content[0].text
```

### Option 3: Tool routing layer — classify intent before selecting tool

```python
import anthropic
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

TOOL_ROUTING_MAP = {
    "internal_search": {
        "tool": "search_internal_knowledge_base",
        "triggers": ["policy", "procedure", "internal", "company", "handbook", "docs"]
    },
    "web_search": {
        "tool": "search_public_web",
        "triggers": ["news", "latest", "current", "public", "external"]
    },
    "own_profile": {
        "tool": "get_user_profile",
        "triggers": ["my account", "my profile", "my settings", "about me"]
    },
    "other_user": {
        "tool": "lookup_user_by_id",
        "triggers": ["user id", "usr_", "another user", "their profile"]
    }
}

def route_tool_call(user_message: str, available_tools: list[str]) -> str | None:
    """
    Use a fast model to pre-classify which tool should be used.
    Returns the tool name or None (let the main model decide).
    """
    tool_list = "\n".join(f"- {t}" for t in available_tools)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # Fast, cheap routing model
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": (
                f"Which single tool should be used for this request?\n\n"
                f"Request: {user_message!r}\n\n"
                f"Available tools:\n{tool_list}\n\n"
                "Reply with only the tool name, or 'unclear' if multiple tools could apply."
            )
        }]
    )

    tool_name = response.content[0].text.strip().lower()
    if tool_name in available_tools:
        return tool_name
    return None  # Let the main model decide

def call_with_routing(user_message: str, tools: list[dict]) -> str:
    """
    Pre-route tool selection, then confirm with main model.
    """
    available_tool_names = [t["name"] for t in tools]
    routed_tool = route_tool_call(user_message, available_tool_names)

    # If routing is confident, hint the main model:
    system = ""
    if routed_tool:
        system = (
            f"Based on the user's request, the most appropriate tool is likely '{routed_tool}'. "
            f"Use this tool unless there's a clear reason to use a different one."
        )
        logger.info(f"Tool router suggests: {routed_tool}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        tools=tools,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text
```

### Option 4: Destructive tool confirmation guard — intercept dangerous tool calls

```python
import anthropic
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

# Tools that have irreversible side effects — require confirmation:
DESTRUCTIVE_TOOLS = {
    "send_message": "This will send the message immediately and cannot be undone.",
    "delete_file": "This will permanently delete the file.",
    "delete_user": "This will permanently delete the user account.",
    "execute_payment": "This will charge the payment method.",
    "deploy_to_production": "This will deploy code to the production environment.",
}

def run_with_confirmation_guard(
    user_message: str,
    tools: list[dict],
    confirm_fn: Callable[[str, str, dict], bool],
    history: list[dict] | None = None
) -> str:
    """
    Run an agent turn. If it calls a destructive tool, ask for confirmation
    before executing. If confirmation is denied, return an explanation.

    confirm_fn: (tool_name, warning_message, tool_input) -> bool
    """
    messages = (history or []) + [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages
        )

        # Check if the model wants to call a destructive tool:
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            return response.content[0].text

        # Process each tool call:
        tool_results = []
        for tool_block in tool_use_blocks:
            tool_name = tool_block.name
            tool_input = tool_block.input

            if tool_name in DESTRUCTIVE_TOOLS:
                warning = DESTRUCTIVE_TOOLS[tool_name]
                confirmed = confirm_fn(tool_name, warning, tool_input)
                if not confirmed:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": f"User declined to execute {tool_name}. Action was not performed.",
                        "is_error": False
                    })
                    logger.info(f"Destructive tool {tool_name} blocked — user declined")
                    continue

            # Execute the tool (your actual tool implementation):
            result = execute_tool(tool_name, tool_input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": str(result)
            })

        # Continue the conversation with tool results:
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

def execute_tool(name: str, input_data: dict) -> Any:
    """Placeholder — replace with actual tool implementations."""
    return {"status": "executed", "tool": name, "input": input_data}

# Interactive confirmation:
def interactive_confirm(tool_name: str, warning: str, tool_input: dict) -> bool:
    print(f"\n⚠️  About to call: {tool_name}")
    print(f"Warning: {warning}")
    print(f"Parameters: {tool_input}")
    answer = input("Proceed? (yes/no): ").strip().lower()
    return answer in ("yes", "y")
```

### Option 5: Tool description template — standardize descriptions for clarity

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class ToolDescription:
    """
    Structured tool description that generates consistent, unambiguous descriptions.
    Forces you to specify: what it does, when to use it, when NOT to use it.
    """
    action: str          # What the tool does (verb phrase)
    use_when: list[str]  # Bullet points: when to use this tool
    not_when: list[str]  # Bullet points: when NOT to use this tool
    side_effects: Optional[str] = None  # Side effects, if any
    returns: Optional[str] = None       # What it returns

    def build(self) -> str:
        parts = [self.action]
        parts.append("Use this tool when:")
        for condition in self.use_when:
            parts.append(f"- {condition}")
        parts.append("Do NOT use this tool when:")
        for condition in self.not_when:
            parts.append(f"- {condition}")
        if self.side_effects:
            parts.append(f"Side effects: {self.side_effects}")
        if self.returns:
            parts.append(f"Returns: {self.returns}")
        return "\n".join(parts)

# Well-described tools using the template:
WELL_DESCRIBED_TOOLS = [
    {
        "name": "get_product_from_database",
        "description": ToolDescription(
            action="Retrieve a product record from the internal product database by exact product ID.",
            use_when=[
                "You have an exact product ID (format: prod_XXXXXXXX)",
                "The user asked about a specific known product",
                "You need to verify current price, inventory, or specs"
            ],
            not_when=[
                "You want to search for products by name or category — use search_products instead",
                "The product ID is approximate or uncertain",
                "You need competitor product data — this is internal only"
            ],
            side_effects=None,
            returns="Product record with id, name, price, inventory, description, category"
        ).build(),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "pattern": "^prod_[A-Za-z0-9]{8}$"}
            },
            "required": ["product_id"]
        }
    },
    {
        "name": "search_products",
        "description": ToolDescription(
            action="Search for products by name, category, or description using full-text search.",
            use_when=[
                "You know the product name but not its exact ID",
                "The user wants to find products matching a description",
                "You need to list products in a category"
            ],
            not_when=[
                "You have an exact product ID — use get_product_from_database instead (faster)",
                "Searching for competitor products — this searches internal catalog only"
            ],
            returns="List of up to 10 matching products with id, name, price, and short description"
        ).build(),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string", "enum": ["all", "electronics", "clothing", "food"]}
            },
            "required": ["query"]
        }
    }
]
```

### Option 6: Few-shot tool selection examples — show correct vs incorrect choices

```python
import anthropic

client = anthropic.Anthropic()

TOOL_SELECTION_EXAMPLES = """## Tool Selection Examples

### Example 1: Searching
User: "What's our refund policy?"
Correct: search_internal_knowledge_base(query="refund policy")
Wrong: search_public_web — this is internal company information

### Example 2: User lookup
User: "Show me user ID usr_abc123's account"
Correct: lookup_user_by_id(user_id="usr_abc123")
Wrong: get_user_profile — that's for the current user only

### Example 3: Drafting vs sending
User: "Write an email to john@example.com about the meeting"
Correct: save_draft_message(to="john@example.com", body="...")
Wrong: send_message — user didn't say to send, only to write

### Example 4: Destructive actions
User: "I don't need the old report anymore"
Correct: archive_file(path="old_report.pdf")
Wrong: delete_file — user said "don't need", not "delete"; prefer reversible action

### Example 5: Reading vs writing
User: "What does config.yaml contain?"
Correct: read_file(path="config.yaml")
Wrong: write_file — user asked to read, not modify
"""

def chat_with_examples(user_message: str, tools: list[dict], history: list[dict] | None = None) -> str:
    system = TOOL_SELECTION_EXAMPLES
    messages = (history or []) + [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        tools=tools,
        messages=messages
    )
    return response.content[0].text
```

## Tool Naming Rules

| Pattern | WRONG | RIGHT |
|---|---|---|
| Action-first naming | `user_data` | `get_user_by_id` |
| Scope in name | `search` | `search_internal_kb` vs `search_web` |
| Destructive marker | `remove` | `delete_permanently` |
| Reversible marker | `delete` | `archive_file` |
| Target object | `send` | `send_email` vs `send_notification` |
| Read vs write | `user_profile` | `get_user_profile` vs `update_user_profile` |

## Description Quality Checklist

✅ Starts with what it does (verb phrase)
✅ States explicitly when to use it (2–3 bullet points)
✅ States explicitly when NOT to use it (1–2 bullet points)
✅ Mentions side effects if any (irreversible actions)
✅ Describes what it returns
✅ No overlap with similar tool's description

## Expected Token Savings
Wrong tool called → tool returns error or wrong data → agent retries or asks user → correction loop: ~2,000–5,000 tokens overhead
Correct tool on first call: 0 overhead tokens

## Environment
- Any agent with 5+ tools, especially when tools share a domain (multiple search tools, multiple user tools, multiple file tools); tool confusion is worst when tools have semantic similarity but operational differences; invest in description quality before adding more tools — a well-described set of 10 tools outperforms a poorly-described set of 20
- Source: direct experience; tool selection errors are responsible for ~25% of "agent did the wrong thing" reports in production, and 90% of those cases have descriptions that don't explain when NOT to use the tool
