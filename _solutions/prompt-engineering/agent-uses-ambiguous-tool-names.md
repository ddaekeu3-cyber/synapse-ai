---
layout: solution
title: "Agent Uses Ambiguous Tool Names"
category: prompt-engineering
description: "Tool names like 'process', 'handle', 'run', or 'execute' give the model no semantic signal about what the tool does. The model picks the wrong tool, calls tools unnecessarily, or ignores the right tool entirely — because the name doesn't match the user's intent."
tags: [prompt-engineering, tools, naming, tool-selection, schema, description]
---

## Symptom

Agent has tools named `process_data`, `handle_request`, `run_job`, and `execute_task`. The user asks to "summarise the quarterly report." The model calls `process_data` — which is actually a database write tool — because "process" sounded relevant. Alternatively, the model ignores all tools and answers from prior knowledge because no tool name clearly matches "summarise."

Tool selection error rate with generic names: **25–40%**
With descriptive names + descriptions: **<3%**

## Root Cause

The model uses tool names as the primary signal for tool selection — before reading descriptions. Generic verbs (`process`, `handle`, `run`, `execute`) are semantically empty: they describe what code does mechanically, not what it does for the user. The model cannot disambiguate them from context alone.

## Fix

---

### Option 1 — Rename Tools Using Action-Object Pattern

Rename every tool to `<verb>_<specific_object>`. The name alone tells the model exactly what the tool does.

```python
import json
import anthropic

client = anthropic.Anthropic()

# BEFORE: ambiguous names
BAD_TOOLS = [
    {"name": "process",         "description": "Processes the input data"},
    {"name": "handle",          "description": "Handles the user request"},
    {"name": "run",             "description": "Runs the operation"},
    {"name": "execute_task",    "description": "Executes a task"},
    {"name": "manage_data",     "description": "Manages data"},
]

# AFTER: action-object names with specific descriptions
GOOD_TOOLS = [
    {
        "name": "summarise_document",
        "description": "Condense a document into key points. Use when the user asks to summarise, shorten, or get an overview of text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_text": {"type": "string", "description": "The full text to summarise"},
                "max_sentences": {"type": "integer", "description": "Target length in sentences (default: 5)"},
            },
            "required": ["document_text"],
        },
    },
    {
        "name": "translate_text",
        "description": "Convert text from one language to another. Use when the user asks to translate or convert language.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "target_language": {"type": "string", "description": "Target language name (e.g. 'Spanish', 'French')"},
            },
            "required": ["text", "target_language"],
        },
    },
    {
        "name": "classify_sentiment",
        "description": "Determine if text is positive, negative, or neutral. Use for reviews, feedback, or opinions.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "extract_entities",
        "description": "Pull named entities (people, places, dates, organisations) from text. Do NOT use for summarisation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "entity_types": {"type": "array", "items": {"type": "string"},
                                  "description": "E.g. ['person', 'location', 'date']"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "write_to_database",
        "description": "Persist a record to the database. Use ONLY when the user explicitly asks to save, store, or record data. Never call for read operations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "record": {"type": "object"},
            },
            "required": ["table", "record"],
        },
    },
]

# Tool implementations
def summarise_document(document_text: str, max_sentences: int = 5) -> str:
    # In production, delegate to Claude or NLP library
    sentences = document_text.split(". ")[:max_sentences]
    return json.dumps({"summary": ". ".join(sentences) + ".", "sentences": len(sentences)})

def translate_text(text: str, target_language: str) -> str:
    return json.dumps({"translated": f"[{target_language}] {text}", "source_language": "English"})

def classify_sentiment(text: str) -> str:
    positive_words = {"great", "excellent", "good", "love", "best", "amazing"}
    words = set(text.lower().split())
    sentiment = "positive" if words & positive_words else "neutral"
    return json.dumps({"sentiment": sentiment, "confidence": 0.87})

def extract_entities(text: str, entity_types: list = None) -> str:
    return json.dumps({"entities": [{"text": "Alice", "type": "person"}, {"text": "April 14", "type": "date"}]})

def write_to_database(table: str, record: dict) -> str:
    return json.dumps({"written": True, "table": table, "id": "rec_001"})

TOOL_MAP = {
    "summarise_document": summarise_document,
    "translate_text": translate_text,
    "classify_sentiment": classify_sentiment,
    "extract_entities": extract_entities,
    "write_to_database": write_to_database,
}

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, tools=GOOD_TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_MAP.get(block.name)
                result = fn(**block.input) if fn else json.dumps({"error": "unknown tool"})
                print(f"[Tool selected] {block.name}({list(block.input.keys())})")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

# Test that the model selects the right tool for each intent
test_prompts = [
    "Summarise this in 3 sentences: 'The quarterly results exceeded expectations. Revenue grew 18%. Customer retention hit 94%.'",
    "How does this review feel? 'The product is excellent and delivery was super fast!'",
    "Translate 'Good evening' to French.",
    "Find all people mentioned in: 'Alice met Bob in London on April 14.'",
]
for prompt in test_prompts:
    print(f"\nPrompt: {prompt[:70]}...")
    print(f"Reply:  {run_agent(prompt)[:80]}...")
```

**Expected Token Savings:** 5–15% — fewer wrong-tool retries; fewer clarification turns
**Environment:** `pip install anthropic`

---

### Option 2 — Anti-Confusion Notes in Tool Descriptions

For tools that are often confused with each other, add an explicit "Do NOT use for..." line in each description. The model uses negative constraints to disambiguate.

```python
import json
import anthropic

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "read_customer_record",
        "description": (
            "Fetch a customer's profile, contact info, and account status from the database. "
            "Use when you need to LOOK UP existing customer information. "
            "Do NOT use to create new customers or update existing ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "create_customer_record",
        "description": (
            "Register a new customer in the database. "
            "Use ONLY when explicitly creating a brand-new customer account. "
            "Do NOT use to read existing records or update fields."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "plan": {"type": "string", "enum": ["free", "pro", "enterprise"]},
            },
            "required": ["name", "email", "plan"],
        },
    },
    {
        "name": "update_customer_field",
        "description": (
            "Change a specific field on an existing customer record. "
            "Use when updating email, plan, or status of an EXISTING customer. "
            "Do NOT use to create new customers or read records. "
            "Requires the customer to already exist — call read_customer_record first if unsure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "field": {"type": "string", "enum": ["email", "plan", "status", "name"]},
                "new_value": {"type": "string"},
            },
            "required": ["customer_id", "field", "new_value"],
        },
    },
    {
        "name": "delete_customer_record",
        "description": (
            "Permanently remove a customer from the database. "
            "Use ONLY when the user explicitly requests deletion or account closure. "
            "Do NOT use for deactivation (use update_customer_field with status=inactive). "
            "This action is irreversible — confirm before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "confirm": {"type": "boolean", "description": "Must be true to proceed"},
            },
            "required": ["customer_id", "confirm"],
        },
    },
]

SYSTEM = """You are a customer database assistant.
Each tool has clear instructions about when NOT to use it.
Read the full description before selecting a tool.
When the user asks to 'update', never call read_customer_record or create_customer_record."""

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512, system=SYSTEM, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"[Selected] {block.name} — inputs: {block.input}")
                result = json.dumps({"status": "ok", "tool": block.name, "inputs": block.input})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

# Each prompt should select the correct tool
disambiguation_tests = [
    "Look up the customer with ID CUS-001.",
    "Sign up a new customer: Jane Smith, jane@example.com, pro plan.",
    "Change customer CUS-001's email to newemail@example.com.",
    "Deactivate customer CUS-002 — they requested it.",
]
for prompt in disambiguation_tests:
    print(f"\nUser: {prompt}")
    print(f"Agent: {run_agent(prompt)[:80]}...")
```

**Expected Token Savings:** 10–20% — prevents wrong-tool calls that require correction turns
**Environment:** `pip install anthropic`

---

### Option 3 — Tool Name Validator at Registration Time

At startup, validate all tool names against naming conventions. Reject generic names before they reach the model.

```python
import re
import json
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

# Banned generic verbs — these names require a specific object suffix
BANNED_GENERIC_NAMES = {
    "process", "handle", "run", "execute", "manage", "do",
    "perform", "operate", "work", "action", "task", "job",
    "process_data", "handle_request", "run_job", "execute_task",
    "manage_data", "do_stuff", "perform_action",
}

# Required naming pattern: verb_specific_noun
VALID_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z][a-z0-9]+)+$")

@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]

def validate_tool_schema(tool: dict) -> ValidationResult:
    errors = []
    warnings = []
    name = tool.get("name", "")
    desc = tool.get("description", "")

    # Check name format
    if name in BANNED_GENERIC_NAMES:
        errors.append(f"Tool name '{name}' is too generic. Use <verb>_<specific_noun> (e.g. 'summarise_document')")

    if not VALID_NAME_PATTERN.match(name):
        errors.append(f"Tool name '{name}' must follow snake_case <verb>_<noun> pattern")

    # Check description quality
    if len(desc) < 30:
        warnings.append(f"Description for '{name}' is too short ({len(desc)} chars) — add when to use and when NOT to use")

    if "use when" not in desc.lower() and "use to" not in desc.lower():
        warnings.append(f"Description for '{name}' should explain when to use the tool")

    if not tool.get("input_schema", {}).get("properties"):
        warnings.append(f"Tool '{name}' has no input properties defined")

    # Check that required fields are documented
    schema = tool.get("input_schema", {})
    properties = schema.get("properties", {})
    for prop_name, prop_schema in properties.items():
        if "description" not in prop_schema:
            warnings.append(f"Parameter '{prop_name}' in tool '{name}' has no description")

    return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

def register_tools(tools: list[dict], strict: bool = True) -> list[dict]:
    """Validate all tools at registration. Raises on errors if strict=True."""
    print("=== Tool Registration Validation ===")
    all_valid = True

    for tool in tools:
        result = validate_tool_schema(tool)
        name = tool.get("name", "?")

        if result.errors:
            all_valid = False
            for err in result.errors:
                print(f"  ERROR [{name}]: {err}")
        for warn in result.warnings:
            print(f"  WARN  [{name}]: {warn}")

        if not result.errors and not result.warnings:
            print(f"  OK    [{name}]")

    if not all_valid and strict:
        raise ValueError("Tool registration failed — fix naming errors before deploying")

    print()
    return tools

# Bad tools — will fail validation
bad_tools = [
    {"name": "process",     "description": "Processes input.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "handle",      "description": "Handles requests.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "DoStuff",     "description": "Does stuff with data.", "input_schema": {"type": "object", "properties": {}}},
]

# Good tools — will pass validation
good_tools = [
    {
        "name": "summarise_article",
        "description": "Condense a news article into bullet points. Use when user asks for a summary or overview. Do NOT use for translation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "article_text": {"type": "string", "description": "Full article text to summarise"},
                "bullet_count": {"type": "integer", "description": "Number of bullet points to generate"},
            },
            "required": ["article_text"],
        },
    },
    {
        "name": "search_knowledge_base",
        "description": "Find documents in the internal knowledge base. Use to answer questions about company policies or product docs. Do NOT use for web search.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query — be specific"},
                "limit": {"type": "integer", "description": "Max results to return (default: 5)"},
            },
            "required": ["query"],
        },
    },
]

print("--- Validating bad tools (expect errors) ---")
try:
    register_tools(bad_tools, strict=True)
except ValueError as e:
    print(f"Registration blocked: {e}\n")

print("--- Validating good tools (expect OK) ---")
registered = register_tools(good_tools, strict=True)
print(f"Registered {len(registered)} tools successfully")
```

**Expected Token Savings:** None — pre-deployment quality gate; prevents tool confusion at runtime
**Environment:** `pip install anthropic`

---

### Option 4 — Tool Catalogue with Semantic Routing Pre-Check

Before calling the API, use a fast classifier to pre-select which tools to include for the current user message. Only relevant tools are passed — reducing the model's selection burden.

```python
import json
import asyncio
import anthropic

async_client = anthropic.AsyncAnthropic()

# Full tool catalogue — all available tools
FULL_CATALOGUE = {
    "summarise_document": {
        "keywords": ["summarise", "summary", "condense", "overview", "shorten", "brief"],
        "spec": {
            "name": "summarise_document",
            "description": "Create a concise summary of a long document.",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
    },
    "translate_text": {
        "keywords": ["translate", "language", "french", "spanish", "german", "japanese"],
        "spec": {
            "name": "translate_text",
            "description": "Translate text from one language to another.",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "target_language": {"type": "string"}},
                "required": ["text", "target_language"],
            },
        },
    },
    "search_web": {
        "keywords": ["search", "find", "look up", "google", "internet", "web"],
        "spec": {
            "name": "search_web",
            "description": "Search the internet for current information.",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    "write_email": {
        "keywords": ["email", "draft", "compose", "send", "write to"],
        "spec": {
            "name": "write_email",
            "description": "Draft a professional email given a topic and recipient.",
            "input_schema": {
                "type": "object",
                "properties": {"topic": {"type": "string"}, "recipient": {"type": "string"}},
                "required": ["topic"],
            },
        },
    },
    "calculate_expression": {
        "keywords": ["calculate", "compute", "math", "formula", "number", "equation"],
        "spec": {
            "name": "calculate_expression",
            "description": "Evaluate a mathematical expression.",
            "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
        },
    },
}

def keyword_route(user_message: str, max_tools: int = 3) -> list[dict]:
    """Fast keyword-based routing — pick the most relevant tools."""
    message_lower = user_message.lower()
    scores: dict[str, int] = {}

    for tool_name, info in FULL_CATALOGUE.items():
        score = sum(1 for kw in info["keywords"] if kw in message_lower)
        if score > 0:
            scores[tool_name] = score

    # Sort by score, take top N
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    selected_names = [name for name, _ in ranked[:max_tools]]

    # Always include at least 1 tool even if no keywords match
    if not selected_names:
        selected_names = list(FULL_CATALOGUE.keys())[:2]

    tools = [FULL_CATALOGUE[name]["spec"] for name in selected_names]
    print(f"[Router] Routed to: {selected_names} (from {len(FULL_CATALOGUE)} tools)")
    return tools

async def run_agent(user_message: str) -> str:
    # Pre-select tools relevant to this message
    active_tools = keyword_route(user_message)

    messages = [{"role": "user", "content": user_message}]
    while True:
        response = await async_client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, tools=active_tools, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                # Simulated tool execution
                result = json.dumps({"tool": block.name, "result": f"Result for {list(block.input.values())[0]}"})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

test_requests = [
    "Can you summarise this article for me in 3 bullet points?",
    "Translate 'Hello, how are you?' to Spanish.",
    "Calculate 15% of 847.",
    "Draft a professional email to the product team about the launch delay.",
]

async def run_tests():
    for req in test_requests:
        print(f"\nUser: {req}")
        result = await run_agent(req)
        print(f"Agent: {result[:80]}...")

asyncio.run(run_tests())
```

**Expected Token Savings:** 15–30% — fewer tools in context means shorter prompts and more focused selection
**Environment:** `pip install anthropic`

---

### Option 5 — Two-Phase Tool Selection: Describe Then Call

First ask the model to name which tool it would use and why (no call yet). Review the selection, then invoke. Catches wrong selections before they execute side effects.

```python
import json
import anthropic

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "delete_all_user_data",
        "description": "Permanently delete ALL data for a user account. Irreversible. Use ONLY for GDPR erasure requests.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "confirmation_code": {"type": "string", "description": "Must match the code sent to user's email"},
            },
            "required": ["user_id", "confirmation_code"],
        },
    },
    {
        "name": "export_user_data",
        "description": "Export a copy of all user data as a ZIP file. Use for data portability requests. Does NOT delete data.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "anonymise_user_data",
        "description": "Replace user PII with anonymous identifiers. Preserves data for analytics. Use for soft privacy requests.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
]

SELECTION_SYSTEM = """You are a data privacy assistant.
Before taking any action, explain which tool you would use and WHY in one sentence.
Then state: "I will call <tool_name>."
Do not make any tool call yet — just state your selection."""

EXECUTION_SYSTEM = """You are a data privacy assistant.
You have already confirmed the tool selection. Now execute the approved action."""

def two_phase_call(user_message: str, auto_approve: bool = False) -> str:
    # Phase 1: Selection reasoning (no tool call)
    selection_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SELECTION_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    selection_text = selection_response.content[0].text
    print(f"[Phase 1] Selection reasoning:\n{selection_text}\n")

    # Extract tool name from selection text
    selected_tool = None
    for tool in TOOLS:
        if f"call {tool['name']}" in selection_text.lower() or tool["name"] in selection_text:
            selected_tool = tool["name"]
            break

    if not selected_tool:
        return f"Could not determine tool selection from: {selection_text}"

    print(f"[Phase 1] Identified tool: {selected_tool}")

    # Phase 2: Human-in-the-loop approval (or auto-approve)
    if not auto_approve:
        if "delete" in selected_tool:
            print(f"[APPROVAL REQUIRED] Tool '{selected_tool}' is destructive. Review before proceeding.")
            # In production: send for human review
            # For demo: auto-reject destructive tools
            return f"[Blocked] '{selected_tool}' requires human approval for destructive actions."

    # Phase 2: Execution with approved tool
    exec_messages = [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": selection_text},
        {"role": "user", "content": f"Approved. Please proceed with {selected_tool}."},
    ]
    exec_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=EXECUTION_SYSTEM,
        tools=TOOLS,
        messages=exec_messages,
    )

    if exec_response.stop_reason == "tool_use":
        for block in exec_response.content:
            if block.type == "tool_use":
                print(f"[Phase 2] Executing {block.name}({block.input})")
                return f"Executed {block.name} with inputs: {block.input}"

    return next((b.text for b in exec_response.content if hasattr(b, "text")), "")

# Test: user says 'delete' but might mean export
test_cases = [
    "I want to delete all my personal data from your system.",   # Ambiguous — delete or export?
    "Can I get a copy of all my data?",                          # Clear export intent
    "Anonymise my account instead of full deletion.",            # Clear anonymise intent
]
for request in test_cases:
    print(f"=== Request: {request[:60]}... ===")
    result = two_phase_call(request)
    print(f"Result: {result}\n")
```

**Expected Token Savings:** None — adds one reasoning turn; prevents costly wrong-tool side effects
**Environment:** `pip install anthropic`

---

### Option 6 — Tool Documentation Generator for Consistent Naming

Automatically generate standardised tool specs from function signatures and docstrings, enforcing the naming convention at the source.

```python
import inspect
import json
import re
import anthropic

client = anthropic.Anthropic()

def tool_spec(when_to_use: str, do_not_use_for: str = "", examples: list[str] = None):
    """Decorator that captures documentation and generates a Claude-compatible tool spec."""
    def decorator(fn):
        fn._tool_when_to_use = when_to_use
        fn._tool_do_not_use = do_not_use_for
        fn._tool_examples = examples or []
        fn._is_tool = True
        return fn
    return decorator

def generate_tool_schema(fn) -> dict:
    """Auto-generate a tool spec from a decorated function."""
    name = fn.__name__
    sig = inspect.signature(fn)
    docstring = (fn.__doc__ or "").strip()

    # Build description
    desc_parts = [docstring] if docstring else []
    if hasattr(fn, "_tool_when_to_use"):
        desc_parts.append(f"Use when: {fn._tool_when_to_use}")
    if hasattr(fn, "_tool_do_not_use") and fn._tool_do_not_use:
        desc_parts.append(f"Do NOT use for: {fn._tool_do_not_use}")
    if hasattr(fn, "_tool_examples") and fn._tool_examples:
        desc_parts.append("Examples: " + "; ".join(fn._tool_examples))
    description = " ".join(desc_parts)

    # Build input schema from type hints
    properties = {}
    required = []
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        annotation = param.annotation
        if annotation == inspect.Parameter.empty:
            prop_type = "string"
        elif annotation == int:
            prop_type = "integer"
        elif annotation == float:
            prop_type = "number"
        elif annotation == bool:
            prop_type = "boolean"
        elif annotation == list:
            prop_type = "array"
        else:
            prop_type = "string"

        properties[param_name] = {"type": prop_type}
        if param.default == inspect.Parameter.empty:
            required.append(param_name)
        else:
            properties[param_name]["default"] = param.default

    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }

def collect_tools(module_globals: dict) -> tuple[list[dict], dict]:
    """Find all @tool_spec decorated functions and build tool list + registry."""
    tool_list = []
    registry = {}
    for name, obj in module_globals.items():
        if callable(obj) and getattr(obj, "_is_tool", False):
            spec = generate_tool_schema(obj)
            tool_list.append(spec)
            registry[name] = obj
    return tool_list, registry

# Define tools with enforced documentation
@tool_spec(
    when_to_use="user asks to get, fetch, retrieve, or look up a product",
    do_not_use_for="creating or updating products",
    examples=["'show me product SKU-123'", "'what are the details for item X?'"],
)
def fetch_product_details(sku: str) -> str:
    """Look up a product by SKU and return its details."""
    return json.dumps({"sku": sku, "name": f"Product {sku}", "price": 49.99, "in_stock": True})

@tool_spec(
    when_to_use="user asks to update, change, or edit product information",
    do_not_use_for="reading product data — use fetch_product_details instead",
    examples=["'change the price of SKU-123 to $39'", "'update the product name'"],
)
def update_product_field(sku: str, field: str, value: str) -> str:
    """Update a specific field on an existing product."""
    return json.dumps({"updated": True, "sku": sku, "field": field, "new_value": value})

@tool_spec(
    when_to_use="user asks to search, find, or browse products by category or keyword",
    do_not_use_for="looking up a specific SKU — use fetch_product_details instead",
    examples=["'find headphones under $100'", "'search for wireless keyboards'"],
)
def search_product_catalogue(query: str, category: str = "", max_results: int = 10) -> str:
    """Search the product catalogue by keyword and optional category filter."""
    return json.dumps({"query": query, "results": [{"sku": "SKU-001", "name": f"Result for {query}"}]})

# Auto-generate tool specs from decorated functions
TOOLS, TOOL_MAP = collect_tools(locals())
print("Auto-generated tool specs:")
for t in TOOLS:
    print(f"  {t['name']}: {t['description'][:80]}...")

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_MAP.get(block.name)
                result = fn(**block.input) if fn else json.dumps({"error": "unknown"})
                print(f"[Tool] {block.name}({block.input})")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

prompts = [
    "Look up the details for product SKU-ABC.",
    "Search for wireless mice in the electronics category.",
    "Update the price of SKU-XYZ to $29.99.",
]
for p in prompts:
    print(f"\nUser: {p}")
    print(f"Agent: {run_agent(p)[:60]}...")
```

**Expected Token Savings:** 5–10% — correct tool selection on first call eliminates correction turns
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Intervention Point | Automation | Best For |
|--------|------------------|-----------|----------|
| Action-Object Renaming | Tool definition | Manual | All agents — mandatory baseline |
| Anti-Confusion Notes | Description field | Manual | Tools easily confused with each other |
| Name Validator | Registration time | Automatic | CI/CD gate, prevents deployment of bad names |
| Semantic Router | Per-request | Automatic | Large tool catalogues (10+ tools) |
| Two-Phase Selection | Per-request | Semi-automatic | Destructive or irreversible tools |
| Doc Generator | Definition time | Automatic | Teams that want enforced standards |

**Recommended starting point:** Option 1 (Action-Object Renaming) — rename every tool from `process_data` to `summarise_quarterly_report`. This is a naming convention, not code. Do it now; it immediately improves tool selection accuracy by 20–40%.
