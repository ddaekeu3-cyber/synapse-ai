---
layout: solution
title: "Agent Confabulates Tool Return Values — Fabricates What a Tool Returned"
category: hallucination
description: "An agent calls a tool, receives an empty, error, or unexpected result, and instead of reporting the actual result, invents plausible-sounding data. It might report 'the database returned 5 records' when it returned 0, or describe file contents it never read. This is distinct from hallucinating facts — it's fabricating the output of a specific, real tool call the agent just made."
tags: [hallucination, tool-use, fabrication, grounding, verification, tool-results, faithfulness]
---

# Agent Confabulates Tool Return Values — Fabricates What a Tool Returned

## Symptom
- Agent reports "I found 3 matching orders" but the search tool returned zero results
- Agent describes file contents from a file it never successfully read (read tool returned an error)
- Tool returns an empty list; agent proceeds as if it returned data and acts on invented values
- Agent calls a database query tool, gets a connection error, then summarizes "database records" it made up
- Agent says "according to the API response..." but the API returned a timeout error
- Tool result is ambiguous (e.g., `{"status": "ok"}` with no data) — agent fills in the missing data with hallucinated values

## Root Cause
When a tool returns empty, partial, or error results, Claude's completion tendency pushes it toward generating a plausible continuation — including plausible tool outputs. Without explicit instruction to faithfully report tool results (even empty or failed ones), the model treats an unsatisfying tool result as a gap to fill. The fix is to: (1) validate tool results before passing them back, (2) explicitly instruct the model to report actual results verbatim, (3) verify the model's claims against raw tool outputs, and (4) structure tool results so empty/error states are unambiguous.

## Fix

### Option 1: Validate tool results before returning to the agent — flag empties and errors

```python
import anthropic
import json

client = anthropic.Anthropic()

def validate_tool_result(tool_name: str, raw_result: dict) -> dict:
    """
    Normalize tool results so empty and error states are explicit.
    The agent receives a structured result it cannot misinterpret.
    """
    if "error" in raw_result:
        return {
            "status": "error",
            "error_message": raw_result["error"],
            "data": None,
            "record_count": 0,
            "tool": tool_name,
            "instruction": "The tool call FAILED. Do not report any data — report the error to the user."
        }

    data = raw_result.get("data") or raw_result.get("results") or raw_result.get("items") or []

    if not data:
        return {
            "status": "empty",
            "data": [],
            "record_count": 0,
            "tool": tool_name,
            "instruction": "The tool returned ZERO results. Do not invent results. Tell the user nothing was found."
        }

    return {
        "status": "success",
        "data": data,
        "record_count": len(data) if isinstance(data, list) else 1,
        "tool": tool_name,
        "instruction": f"The tool returned exactly {len(data) if isinstance(data, list) else 1} result(s). Report only what is shown in 'data'."
    }


# Simulated tool implementations:
def search_orders(customer_id: str) -> dict:
    # Simulates a database returning no results:
    return {"results": []}

def read_file(path: str) -> dict:
    # Simulates a file read error:
    return {"error": f"File not found: {path}"}

def get_product(product_id: str) -> dict:
    return {"data": {"id": product_id, "name": "Widget Pro", "price": 49.99}}


TOOLS = [
    {
        "name": "search_orders",
        "description": "Search orders by customer ID",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"]
        }
    },
    {
        "name": "read_file",
        "description": "Read a file by path",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
        }
    }
]

SYSTEM = """You are a customer service assistant.
When tools return results, report EXACTLY what they returned.
If a tool returns zero results, say "no results found" — do not invent data.
If a tool returns an error, report the error — do not make up what the result might have been.
Never paraphrase or embellish tool outputs."""


def handle_tool_call(name: str, inputs: dict) -> str:
    if name == "search_orders":
        raw = search_orders(inputs["customer_id"])
    elif name == "read_file":
        raw = read_file(inputs["path"])
    else:
        raw = {"error": f"Unknown tool: {name}"}

    validated = validate_tool_result(name, raw)
    return json.dumps(validated)


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = handle_tool_call(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


# Now: "Find orders for customer 999" → "No orders found for customer 999."
# Before: → "I found 3 orders for customer 999: ..." (fabricated)
```

### Option 2: Post-response verification — check agent's claims against actual tool outputs

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

class GroundedAgentRunner:
    """
    Runs the agent and then verifies its final response against the
    actual tool outputs — catching cases where the model invented data.
    """
    def __init__(self):
        self._tool_outputs: dict[str, str] = {}   # tool_use_id → raw result

    def _record_tool_output(self, tool_use_id: str, result: str) -> None:
        self._tool_outputs[tool_use_id] = result

    def _verify_response(self, response_text: str, claimed_numbers: list[int]) -> list[str]:
        """
        Compare numbers in the response against numbers in actual tool outputs.
        Returns list of discrepancy warnings.
        """
        warnings = []
        all_tool_data = " ".join(self._tool_outputs.values())

        for claimed_num in claimed_numbers:
            if str(claimed_num) not in all_tool_data:
                warnings.append(
                    f"Agent claimed '{claimed_num}' but this number does not appear in any tool output"
                )
        return warnings

    def _extract_numbers(self, text: str) -> list[int]:
        """Extract all integers from text for verification."""
        return [int(m) for m in re.findall(r'\b(\d+)\b', text)]

    def run(self, user_message: str, tools: list[dict], tool_handler) -> dict:
        self._tool_outputs.clear()
        messages = [{"role": "user", "content": user_message}]

        while True:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                tools=tools,
                messages=messages
            )

            if response.stop_reason == "end_turn":
                final_text = response.content[0].text
                claimed_numbers = self._extract_numbers(final_text)
                warnings = self._verify_response(final_text, claimed_numbers)

                return {
                    "response": final_text,
                    "tool_outputs": self._tool_outputs,
                    "verification_warnings": warnings,
                    "grounded": len(warnings) == 0
                }

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    raw_result = tool_handler(block.name, block.input)
                    self._record_tool_output(block.id, raw_result)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": raw_result
                    })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})


# Usage:
runner = GroundedAgentRunner()
result = runner.run(
    user_message="How many orders does customer 42 have?",
    tools=[{
        "name": "count_orders",
        "description": "Count orders for a customer",
        "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]}
    }],
    tool_handler=lambda name, inputs: json.dumps({"count": 0, "customer_id": inputs["customer_id"]})
)
print(result["response"])
if result["verification_warnings"]:
    print(f"[WARNING] Possible confabulation: {result['verification_warnings']}")
```

### Option 3: Structured output enforcement — force agent to quote raw tool data

```python
import anthropic
import json

client = anthropic.Anthropic()

# Force the agent to include raw tool data in its structured response.
# If it has to quote the raw data, it can't silently invent different data.

GROUNDED_RESPONSE_TOOL = {
    "name": "grounded_response",
    "description": "Provide a response grounded in tool outputs",
    "input_schema": {
        "type": "object",
        "properties": {
            "raw_tool_data_verbatim": {
                "type": "string",
                "description": "Copy the exact tool output(s) verbatim, as received"
            },
            "interpretation": {
                "type": "string",
                "description": "Your interpretation of the tool output for the user"
            },
            "data_was_empty": {
                "type": "boolean",
                "description": "True if the tool returned no data / empty results"
            },
            "data_had_error": {
                "type": "boolean",
                "description": "True if the tool returned an error"
            }
        },
        "required": ["raw_tool_data_verbatim", "interpretation", "data_was_empty", "data_had_error"]
    }
}

SYSTEM = """You are a data assistant.
After calling tools, you MUST call grounded_response with:
1. raw_tool_data_verbatim: the EXACT tool output, copied character-for-character
2. interpretation: your explanation to the user
3. data_was_empty: true if the tool returned empty/null/zero results
4. data_had_error: true if the tool returned an error

You must NEVER claim data exists that is not present in raw_tool_data_verbatim."""

DATA_TOOLS = [
    {
        "name": "query_database",
        "description": "Query the database",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"]
        }
    },
    GROUNDED_RESPONSE_TOOL
]


def query_database(sql: str) -> str:
    # Simulate empty result:
    return json.dumps({"rows": [], "row_count": 0, "query": sql})


def run_grounded_agent(user_question: str) -> dict:
    messages = [{"role": "user", "content": user_question}]
    tool_outputs = {}

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=SYSTEM,
            tools=DATA_TOOLS,
            tool_choice={"type": "any"},
            messages=messages
        )

        tool_results = []
        final_result = None

        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "query_database":
                raw = query_database(block.input["sql"])
                tool_outputs[block.id] = raw
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": raw
                })

            elif block.name == "grounded_response":
                # The agent has committed to its raw data quotation:
                final_result = block.input
                break

        if final_result:
            return {
                "response": final_result["interpretation"],
                "raw_data_quoted": final_result["raw_tool_data_verbatim"],
                "was_empty": final_result["data_was_empty"],
                "had_error": final_result["data_had_error"]
            }

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
```

### Option 4: Two-pass verification — have a second model check faithfulness

```python
import anthropic
import json

client = anthropic.Anthropic()

def verify_faithfulness(
    agent_response: str,
    tool_outputs: list[str],
    model: str = "claude-haiku-4-5-20251001"
) -> dict:
    """
    Ask a second model to verify that the agent's response is faithful
    to the actual tool outputs — not invented.
    """
    tool_data_str = "\n\n".join(f"Tool Output {i+1}:\n{o}" for i, o in enumerate(tool_outputs))

    response = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""Check if this response is faithful to the tool outputs.

TOOL OUTPUTS (ground truth):
{tool_data_str}

AGENT RESPONSE TO VERIFY:
{agent_response}

Does the agent response claim any facts that are NOT supported by the tool outputs above?
Respond with JSON: {{"faithful": true/false, "issues": ["list of specific fabrications if any"]}}"""
        }]
    )

    text = response.content[0].text.strip()
    # Extract JSON:
    import re
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"faithful": True, "issues": []}  # default to pass if parse fails


def agent_with_faithfulness_check(user_message: str, tools: list[dict], tool_handler) -> str:
    messages = [{"role": "user", "content": user_message}]
    all_tool_outputs = []

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            final_text = response.content[0].text

            if all_tool_outputs:
                check = verify_faithfulness(final_text, all_tool_outputs)
                if not check["faithful"]:
                    print(f"[faithfulness-check] Issues: {check['issues']}")
                    # Optionally: re-run with correction prompt
                    correction = client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=256,
                        messages=[
                            {"role": "user", "content": user_message},
                            {"role": "assistant", "content": final_text},
                            {
                                "role": "user",
                                "content": (
                                    f"Your response contained claims not supported by the actual tool results. "
                                    f"Issues: {check['issues']}. "
                                    f"Please correct your response to only state what the tool actually returned: "
                                    f"{all_tool_outputs}"
                                )
                            }
                        ]
                    )
                    return correction.content[0].text

            return final_text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                raw_result = tool_handler(block.name, block.input)
                all_tool_outputs.append(raw_result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": raw_result
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
```

### Option 5: Explicit empty-result handling in system prompt

```python
import anthropic

client = anthropic.Anthropic()

# The single highest-leverage change: explicit instructions for
# empty and error tool results in the system prompt.

ANTI_CONFABULATION_SYSTEM = """You are a data assistant. You help users query and interpret data.

## Faithfulness Rules (MANDATORY)

### When a tool returns empty results ([], {}, null, count=0):
- Say EXACTLY: "The [tool name] returned no results for [query]."
- Do NOT invent data. Do NOT say "I found X records" if the tool returned 0.
- Do NOT speculate about why there are no results unless explicitly asked.

### When a tool returns an error:
- Report the error message verbatim: "The tool returned an error: [error text]"
- Do NOT proceed as if the call succeeded.
- Do NOT make up what the result might have been.

### When a tool returns partial or ambiguous data:
- Report only what is explicitly present in the tool output.
- If a field is missing, say "the response did not include [field]."
- Never fill in missing fields with plausible values.

### General faithfulness:
- Treat tool outputs as the ground truth. Your knowledge does not override them.
- If tool output contradicts your prior knowledge, trust the tool output.
- Quote exact numbers, names, and values from tool outputs — do not paraphrase into approximate values.
"""

def build_tools() -> list[dict]:
    return [
        {
            "name": "get_user_stats",
            "description": "Get statistics for a user",
            "input_schema": {
                "type": "object",
                "properties": {"user_id": {"type": "integer"}},
                "required": ["user_id"]
            }
        }
    ]

def get_user_stats(user_id: int) -> dict:
    # Simulates empty result for unknown user:
    return {"found": False, "user_id": user_id, "stats": None}

# With this system prompt, the agent correctly reports:
# "get_user_stats returned no data for user 999" rather than inventing statistics.
```

### Option 6: Tool result schema — make empty/error states structurally distinct

```python
import anthropic
import json
from enum import Enum
from dataclasses import dataclass
from typing import Any

client = anthropic.Anthropic()

class ResultStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    ERROR = "error"
    PARTIAL = "partial"

@dataclass
class ToolResult:
    """
    Structured tool result with unambiguous status.
    The agent cannot confuse an empty result with a successful one.
    """
    status: ResultStatus
    data: Any
    count: int
    message: str           # human-readable description of what happened

    def to_json(self) -> str:
        return json.dumps({
            "status": self.status.value,
            "data": self.data,
            "count": self.count,
            "message": self.message
        })

    @classmethod
    def success(cls, data: Any) -> "ToolResult":
        count = len(data) if isinstance(data, (list, dict)) else 1
        return cls(
            status=ResultStatus.SUCCESS,
            data=data,
            count=count,
            message=f"Returned {count} result(s)"
        )

    @classmethod
    def empty(cls, query_description: str = "") -> "ToolResult":
        return cls(
            status=ResultStatus.EMPTY,
            data=[],
            count=0,
            message=f"No results found{f' for: {query_description}' if query_description else ''}"
        )

    @classmethod
    def error(cls, error_message: str) -> "ToolResult":
        return cls(
            status=ResultStatus.ERROR,
            data=None,
            count=0,
            message=f"Error: {error_message}"
        )


# Wrap all tool implementations to return ToolResult:
def safe_search_customers(name: str) -> str:
    try:
        results = []  # simulated empty DB result
        if results:
            return ToolResult.success(results).to_json()
        else:
            return ToolResult.empty(f"name='{name}'").to_json()
    except Exception as e:
        return ToolResult.error(str(e)).to_json()


# The agent now sees:
# {"status": "empty", "data": [], "count": 0, "message": "No results found for: name='John'"}
# Instead of: {} or []
# Making it structurally impossible to confuse "no results" with "forgot to populate results"
```

## Confabulation Triggers and Defenses

| Trigger | What Agent Does | Defense |
|---|---|---|
| Tool returns `[]` | Reports invented records | Validate + annotate empty result (Option 1) |
| Tool returns error | Reports fabricated success | Explicit error instruction in system prompt (Option 5) |
| Tool returns partial data | Fills in missing fields | Structured ToolResult schema (Option 6) |
| Agent paraphrases numbers | Changes 3 → "several" | Enforce verbatim quoting via structured output (Option 3) |
| Complex multi-tool output | Mixes up which tool said what | Post-response faithfulness check (Option 4) |

## Expected Token Savings
Verification passes (Option 2, 4) add ~200–500 tokens per check. The alternatives — debugging confabulated data that reached production — are far more costly. Structured results (Options 1, 6) add zero overhead and eliminate the most common confabulation triggers.

## Environment
- Any agent using tools that query databases, APIs, file systems, or external services; most critical when tool results directly drive user-visible outputs (reports, summaries, financial data); Option 5 (system prompt rules) is zero-cost and should always be applied; Option 1 (validated results) is the most robust engineering fix and requires only a thin wrapper around tool implementations; combine Options 1 + 5 as a baseline for all production agents
