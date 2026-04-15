---
layout: solution
title: "Agent Ignores Tool Result and Uses Prior Knowledge Instead"
category: tool-failure
description: "Agent calls a tool, receives an accurate result, then answers using its training knowledge instead—leading to outdated or incorrect responses."
tags: [tool-failure, grounding, hallucination, tool-use, reliability]
---

## Symptom

The agent calls a tool and receives a fresh result, then ignores it and answers from training knowledge:

```
Tool call: get_current_price(symbol="AAPL")
Tool result: {"price": 189.42, "timestamp": "2026-04-15T14:32:00Z"}

Agent response: "Apple stock is currently trading around $175, based on recent market trends."
# ← Ignored the tool result entirely, used stale training data
```

Or more subtly, the agent partially uses the tool result but supplements with incorrect training knowledge:

```
Tool result: {"python_version": "3.13.2", "release_date": "2025-10-15"}
Agent: "Python 3.13 was released and includes several improvements. As of my knowledge,
        the main features include..." ← correct version number, wrong details (fabricated)
```

Root causes:
- System prompt doesn't explicitly require the agent to use tool results
- Tool result is in a format the agent doesn't parse correctly
- Agent's training knowledge about the topic is strong and overrides the tool
- Tool result is buried in a long conversation and the agent "forgets" it
- Agent is uncertain about the tool result and falls back to what it "knows"

---

## Root Cause

Language models have strong priors from training data. When a tool returns a value that conflicts with the model's training knowledge (e.g., a stock price different from what the model "remembers"), the model may default to its training distribution — especially if the system prompt doesn't explicitly require tool result primacy.

The fix is structural: make it impossible to answer without acknowledging the tool result, force the agent to quote the result verbatim, and validate that the answer is derived from the tool output.

---

## Fix

### Option 1 — Explicit Tool Primacy in System Prompt

Directly instruct the agent that tool results override all other knowledge.

```python
import anthropic
import json

client = anthropic.Anthropic()

TOOL_PRIMACY_SYSTEM = """You are a data retrieval assistant.

## Tool Result Authority (CRITICAL)

When you use a tool and receive a result:
1. The tool result is the GROUND TRUTH — it supersedes anything you know from training
2. You MUST base your answer on the tool result, not your prior knowledge
3. Quote specific values from the tool result in your response
4. If the tool result differs from what you expected, trust the tool result
5. Do NOT supplement tool results with your own knowledge unless explicitly asked

## Format requirement
When answering from a tool result, always say:
"According to the [tool_name] result: [quote key values]..."

If you did NOT call a tool, say:
"Based on my training knowledge (which may be outdated): ..."

This distinction is mandatory."""

tools = [
    {
        "name": "get_stock_price",
        "description": "Get current real-time stock price",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "get_software_version",
        "description": "Get the current version of a software package",
        "input_schema": {
            "type": "object",
            "properties": {
                "package": {"type": "string"}
            },
            "required": ["package"]
        }
    }
]

# Simulated tools returning specific values
def get_stock_price(symbol: str) -> dict:
    prices = {"AAPL": 189.42, "MSFT": 415.67, "NVDA": 875.23}
    price = prices.get(symbol.upper(), 100.00)
    return {"symbol": symbol.upper(), "price": price, "currency": "USD", "timestamp": "2026-04-15T14:32:00Z"}

def get_software_version(package: str) -> dict:
    versions = {"python": "3.13.2", "django": "5.1.1", "numpy": "2.1.0"}
    return {"package": package, "version": versions.get(package.lower(), "unknown"), "source": "PyPI"}

def run_agent_with_primacy(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=TOOL_PRIMACY_SYSTEM,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "get_stock_price":
                    result = get_stock_price(**block.input)
                elif block.name == "get_software_version":
                    result = get_software_version(**block.input)
                else:
                    result = {"error": "Unknown tool"}
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
        messages.append({"role": "user", "content": results})

# Test: agent must use tool result, not training knowledge
tests = [
    "What is Apple's current stock price?",
    "What version of Python is current?",
    "What's NVIDIA's stock price right now?",
]

for query in tests:
    print(f"\nQuery: {query}")
    answer = run_agent_with_primacy(query)
    print(f"Answer: {answer[:200]}")
```

**Expected Token Savings:** No direct savings — correctness fix. Prevents responses that mix stale training knowledge with fresh tool data.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 2 — Structured Tool Result Acknowledgment

Require the agent to explicitly acknowledge and quote the tool result before generating its response.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

ACKNOWLEDGE_TOOL = {
    "name": "acknowledge_and_respond",
    "description": (
        "Use this tool to formulate EVERY response after a tool call. "
        "You must quote the tool result verbatim before adding any interpretation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tool_result_quoted": {
                "type": "string",
                "description": "Copy the exact key values from the tool result here"
            },
            "interpretation": {
                "type": "string",
                "description": "Your interpretation or additional context (keep brief)"
            },
            "final_response": {
                "type": "string",
                "description": "The complete response to show the user"
            }
        },
        "required": ["tool_result_quoted", "interpretation", "final_response"]
    }
}

DATA_TOOL = {
    "name": "lookup_metric",
    "description": "Look up a real-time business metric",
    "input_schema": {
        "type": "object",
        "properties": {
            "metric_name": {"type": "string"},
            "entity": {"type": "string"}
        },
        "required": ["metric_name", "entity"]
    }
}

def lookup_metric(metric_name: str, entity: str) -> dict:
    """Simulated real-time metric lookup."""
    data = {
        ("revenue", "acme_corp"): {"value": 4_820_000, "unit": "USD", "period": "Q1 2026"},
        ("users", "acme_corp"): {"value": 127_493, "unit": "active_users", "period": "2026-04-15"},
        ("latency", "api_v2"): {"value": 43.7, "unit": "ms_p99", "period": "last_5min"},
    }
    key = (metric_name.lower(), entity.lower())
    result = data.get(key, {"value": "not_found", "entity": entity, "metric": metric_name})
    return {"metric": metric_name, "entity": entity, **result}

def run_with_acknowledgment(query: str) -> str:
    """Agent must acknowledge tool results before responding."""
    tools = [DATA_TOOL, ACKNOWLEDGE_TOOL]
    messages = [{"role": "user", "content": query}]
    last_tool_result = None

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=(
                "After every tool call, use acknowledge_and_respond to formulate your response. "
                "Never answer directly after a lookup — always acknowledge first."
            ),
            tools=tools,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})
        results = []
        final_response = None

        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "lookup_metric":
                result = lookup_metric(**block.input)
                last_tool_result = result
                content = json.dumps(result)
                print(f"  Tool result: {content}")

            elif block.name == "acknowledge_and_respond":
                # Validate that the agent actually quoted from the tool result
                quoted = block.input.get("tool_result_quoted", "")
                if last_tool_result:
                    key_values = [str(v) for v in last_tool_result.values()]
                    quoted_something_real = any(str(kv) in quoted for kv in key_values)
                    if not quoted_something_real:
                        content = json.dumps({
                            "error": "You must quote actual values from the tool result. "
                                     f"The tool returned: {json.dumps(last_tool_result)}. "
                                     "Try again with real values in tool_result_quoted."
                        })
                    else:
                        final_response = block.input.get("final_response", "")
                        content = json.dumps({"acknowledged": True})
                else:
                    final_response = block.input.get("final_response", "")
                    content = json.dumps({"acknowledged": True})

            else:
                content = json.dumps({"error": "Unknown tool"})

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})

        if final_response:
            return final_response

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "No response"

        messages.append({"role": "user", "content": results})

queries = [
    "What is Acme Corp's Q1 2026 revenue?",
    "How many active users does Acme Corp have?",
    "What's the API v2 latency right now?",
]

for q in queries:
    print(f"\nQuery: {q}")
    answer = run_with_acknowledgment(q)
    print(f"Answer: {answer[:200]}")
```

**Expected Token Savings:** Acknowledgment requirement prevents hallucinated supplements to tool results, eliminating correction cycles.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 3 — Post-Response Grounding Validator

After the agent responds, verify that key values from the tool result appear in the answer.

```python
import anthropic
import json
import re
from typing import Any

client = anthropic.Anthropic()

def extract_key_values(data: Any, min_length: int = 3) -> list[str]:
    """Extract all leaf values from a nested dict/list that are worth checking."""
    values = []

    if isinstance(data, dict):
        for v in data.values():
            values.extend(extract_key_values(v, min_length))
    elif isinstance(data, list):
        for item in data:
            values.extend(extract_key_values(item, min_length))
    elif isinstance(data, (int, float)):
        values.append(str(data))
    elif isinstance(data, str) and len(data) >= min_length:
        values.append(data)

    return values

def check_grounding(response_text: str, tool_result: dict) -> dict:
    """
    Check that the agent's response references actual values from the tool result.
    Returns grounding score and details.
    """
    key_values = extract_key_values(tool_result)
    found = []
    missing = []

    for value in key_values:
        # Normalize for comparison
        value_str = str(value).lower().replace(",", "").replace("$", "")
        response_clean = response_text.lower().replace(",", "").replace("$", "")

        if value_str in response_clean:
            found.append(value)
        else:
            missing.append(value)

    score = len(found) / max(len(key_values), 1)
    return {
        "grounding_score": score,
        "found_in_response": found,
        "missing_from_response": missing,
        "is_grounded": score >= 0.5,  # at least 50% of key values appear
    }

def regenerate_grounded(
    messages: list[dict],
    tool_result: dict,
    system: str,
    tools: list,
) -> str:
    """Regenerate with an explicit grounding instruction appended."""
    values_str = json.dumps(tool_result, indent=2)
    grounding_instruction = (
        f"\n\n[IMPORTANT: Your previous response did not reference the tool result. "
        f"You MUST base your answer on these exact values:\n{values_str}\n"
        f"Include the specific numbers/values from this result in your response.]"
    )

    # Add instruction to the last user message
    augmented_messages = messages.copy()
    if augmented_messages and augmented_messages[-1]["role"] == "user":
        last = augmented_messages[-1]
        if isinstance(last["content"], str):
            augmented_messages[-1] = {
                **last,
                "content": last["content"] + grounding_instruction
            }

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        tools=tools,
        messages=augmented_messages,
    )
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return ""

tools = [{
    "name": "get_analytics",
    "description": "Get website analytics for a given period",
    "input_schema": {
        "type": "object",
        "properties": {
            "site": {"type": "string"},
            "period": {"type": "string"}
        },
        "required": ["site", "period"]
    }
}]

def get_analytics(site: str, period: str) -> dict:
    return {
        "site": site,
        "period": period,
        "page_views": 248_731,
        "unique_visitors": 89_422,
        "bounce_rate": 0.34,
        "avg_session_minutes": 4.7,
        "top_page": "/blog/ai-agents",
    }

SYSTEM = "You are an analytics assistant. Base all answers on tool results."
messages = [{"role": "user", "content": "Give me a summary of example.com analytics for last month"}]
last_tool_result = None

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        tools=tools,
        messages=messages,
    )

    if response.stop_reason == "end_turn":
        final_text = response.content[0].text if response.content else ""

        if last_tool_result:
            grounding = check_grounding(final_text, last_tool_result)
            print(f"Grounding score: {grounding['grounding_score']:.0%}")

            if not grounding["is_grounded"]:
                print(f"  Response NOT grounded. Missing values: {grounding['missing_from_response']}")
                print("  Regenerating with explicit grounding instruction...")
                final_text = regenerate_grounded(messages, last_tool_result, SYSTEM, tools)

        print(f"\nFinal response:\n{final_text}")
        break

    messages.append({"role": "assistant", "content": response.content})
    results = []
    for block in response.content:
        if block.type == "tool_use" and block.name == "get_analytics":
            result = get_analytics(**block.input)
            last_tool_result = result
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Grounding check catches ungrounded responses before they reach the user; regeneration costs ~500 tokens vs. a full user correction cycle (~1,500 tokens).

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 4 — Tool Result Injection into Assistant Turn

Inject the tool result directly into the assistant's reasoning trace using prefill.

```python
import anthropic
import json

client = anthropic.Anthropic()

def answer_with_result_prefill(
    query: str,
    tool_name: str,
    tool_result: dict,
) -> str:
    """
    Generate a response with the tool result prefilled into the assistant turn.
    The model must continue from a sentence that already references the tool output.
    """
    result_str = json.dumps(tool_result, indent=2)
    key_fact = _extract_primary_value(tool_result)

    # Prefill the assistant turn to anchor on the actual result
    prefill = f"Based on the {tool_name} result, the value is {key_fact}. "

    messages = [
        {"role": "user", "content": query},
        # Simulated tool use turn
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": f"I'll look that up now."
                }
            ]
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "fake_id",
                "content": result_str,
            }]
        },
        # Prefill the response to force grounding
        {
            "role": "assistant",
            "content": prefill,
        }
    ]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system="Answer based on tool results. Quote specific values.",
        messages=messages,
    )

    return prefill + response.content[0].text

def _extract_primary_value(result: dict) -> str:
    """Extract the most prominent value from a tool result for prefill."""
    # Try common key names first
    for key in ["price", "value", "version", "count", "total", "result", "data"]:
        if key in result:
            return str(result[key])
    # Fall back to first non-dict, non-list value
    for v in result.values():
        if isinstance(v, (int, float, str)) and not isinstance(v, bool):
            return str(v)
    return json.dumps(result)

# Test: tool result is fixed; verify the response uses it
tool_results = [
    ("get_user_count", {"total_users": 48_293, "active_today": 12_847, "as_of": "2026-04-15"}),
    ("get_temp", {"location": "Seoul", "temp_c": 18.3, "humidity_pct": 62, "condition": "partly cloudy"}),
    ("get_version", {"package": "anthropic", "version": "0.51.0", "pypi_url": "https://pypi.org/project/anthropic/"}),
]

queries = [
    "How many users do we have?",
    "What's the weather in Seoul?",
    "What version of the anthropic package is current?",
]

for (tool_name, result), query in zip(tool_results, queries):
    print(f"\nQuery: {query}")
    print(f"Tool result: {result}")
    answer = answer_with_result_prefill(query, tool_name, result)
    print(f"Answer: {answer[:200]}")
```

**Expected Token Savings:** Prefill anchoring prevents full regeneration cycles; the correct value is locked in from the first token of the response.

**Environment:** Python 3.9+, `anthropic>=0.40.0`. Uses assistant prefill — a standard Anthropic API feature.

---

### Option 5 — Tool Result Formatting for Maximum Salience

Structure tool results to maximize the model's attention to key values.

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

def format_tool_result_for_salience(
    tool_name: str,
    result: dict,
    primary_keys: list[str] = None,
) -> str:
    """
    Format a tool result to maximize salience — the model is more likely
    to use values that are prominent and clearly labeled.
    """
    if primary_keys:
        primary = {k: result[k] for k in primary_keys if k in result}
        secondary = {k: v for k, v in result.items() if k not in primary_keys}
    else:
        primary = result
        secondary = {}

    lines = [
        f"=== {tool_name.upper()} RESULT ===",
        "",
        "PRIMARY VALUES (USE THESE IN YOUR RESPONSE):",
    ]

    for key, value in primary.items():
        lines.append(f"  • {key}: {value}")

    if secondary:
        lines.append("")
        lines.append("Additional context:")
        for key, value in secondary.items():
            lines.append(f"  {key}: {value}")

    lines.extend([
        "",
        "INSTRUCTION: Your response MUST reference the PRIMARY VALUES above.",
        "Do not use your training knowledge for these values.",
        "=== END RESULT ===",
    ])

    return "\n".join(lines)

tools = [
    {
        "name": "get_deployment_status",
        "description": "Get current deployment status",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"]
        }
    }
]

def get_deployment_status(service: str) -> dict:
    return {
        "service": service,
        "status": "degraded",
        "version": "v2.4.1",
        "uptime_pct": 97.3,
        "last_deploy": "2026-04-15T08:15:00Z",
        "error_rate_pct": 3.7,
        "p99_latency_ms": 892,
        "region": "us-west-2",
        "incident_id": "INC-4821",
    }

def run_with_salient_results(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system="You are an ops assistant. Always answer from tool results, not memory.",
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        results = []

        for block in response.content:
            if block.type == "tool_use" and block.name == "get_deployment_status":
                raw_result = get_deployment_status(**block.input)

                # Format for maximum salience — key metrics prominently labeled
                formatted = format_tool_result_for_salience(
                    tool_name="get_deployment_status",
                    result=raw_result,
                    primary_keys=["status", "error_rate_pct", "p99_latency_ms", "incident_id"],
                )

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": formatted,
                })

        messages.append({"role": "user", "content": results})

queries = [
    "What's the current status of the payment service?",
    "Is the auth service healthy? What are the key metrics?",
]

for q in queries:
    print(f"\nQuery: {q}")
    print(f"Answer: {run_with_salient_results(q)[:300]}")
```

**Expected Token Savings:** Better-formatted tool results require no extra LLM calls — they reduce the probability of ignored results at zero additional cost.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 6 — Knowledge Source Tagging with Audit

Require the agent to tag every factual claim with its source: tool result, training knowledge, or inference.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

TAGGED_RESPONSE_TOOL = {
    "name": "write_tagged_response",
    "description": "Write a response where every factual claim is tagged with its source.",
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string", "description": "The factual statement"},
                        "source": {
                            "type": "string",
                            "enum": ["tool_result", "training_knowledge", "inference"],
                            "description": "Where this fact comes from"
                        },
                        "evidence": {"type": "string", "description": "Specific value or reasoning"}
                    },
                    "required": ["claim", "source", "evidence"]
                }
            },
            "response_text": {"type": "string", "description": "The complete response for the user"}
        },
        "required": ["claims", "response_text"]
    }
}

def audit_claims(claims: list[dict], tool_results: list[dict]) -> dict:
    """Verify that claims tagged as 'tool_result' actually appear in tool output."""
    tool_values = set()
    for result in tool_results:
        values = [str(v).lower() for v in result.values() if isinstance(v, (int, float, str))]
        tool_values.update(values)

    audit = []
    for claim in claims:
        if claim["source"] == "tool_result":
            evidence = claim.get("evidence", "").lower()
            grounded = any(tv in evidence for tv in tool_values if len(tv) > 2)
            audit.append({
                "claim": claim["claim"],
                "source": claim["source"],
                "grounded": grounded,
                "issue": None if grounded else "Claims tool_result source but evidence not found in tool output",
            })
        else:
            audit.append({
                "claim": claim["claim"],
                "source": claim["source"],
                "grounded": True,
                "issue": None,
            })

    issues = [a for a in audit if not a["grounded"]]
    return {"issues": issues, "total_claims": len(claims), "ungrounded": len(issues)}

data_tool = {
    "name": "get_server_metrics",
    "description": "Get current server metrics",
    "input_schema": {
        "type": "object",
        "properties": {"server_id": {"type": "string"}},
        "required": ["server_id"]
    }
}

def get_server_metrics(server_id: str) -> dict:
    return {
        "server_id": server_id,
        "cpu_pct": 78.4,
        "memory_gb_used": 12.7,
        "memory_gb_total": 16.0,
        "disk_free_gb": 234.1,
        "network_mbps": 847.3,
        "status": "warning",
    }

tools = [data_tool, TAGGED_RESPONSE_TOOL]
collected_tool_results = []

messages = [{"role": "user", "content": "How is server prod-01 doing?"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "After every tool call, use write_tagged_response to answer. "
            "Tag each factual claim as tool_result, training_knowledge, or inference."
        ),
        tools=tools,
        messages=messages,
    )

    messages.append({"role": "assistant", "content": response.content})
    results = []
    final_answer = None

    for block in response.content:
        if block.type != "tool_use":
            continue

        if block.name == "get_server_metrics":
            result = get_server_metrics(**block.input)
            collected_tool_results.append(result)
            print(f"Tool result: {result}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        elif block.name == "write_tagged_response":
            claims = block.input.get("claims", [])
            audit = audit_claims(claims, collected_tool_results)

            if audit["ungrounded"] > 0:
                print(f"  Audit FAILED: {audit['ungrounded']} claims not grounded in tool results")
                for issue in audit["issues"]:
                    print(f"    ✗ {issue['claim'][:60]}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({
                        "error": "Grounding audit failed",
                        "issues": audit["issues"],
                        "instruction": "Claims tagged as tool_result must reference actual tool output values."
                    })
                })
            else:
                final_answer = block.input.get("response_text", "")
                print(f"  Audit PASSED: all {audit['total_claims']} claims grounded")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"ok": True})})

    if final_answer:
        print(f"\nFinal answer:\n{final_answer}")
        break

    if response.stop_reason == "end_turn":
        print(response.content[0].text if response.content else "No response")
        break

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Source tagging + audit catches knowledge-substitution before it reaches the user; saves 2-4 correction turns downstream.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

## Comparison

| Option | Prevention | Detects Post-Fact | Adds Latency | Complexity |
|--------|-----------|-------------------|-------------|------------|
| 1 — Tool Primacy Prompt | Instructional | No | No | Low |
| 2 — Acknowledgment Tool | Structural | Yes (blocks) | No | Medium |
| 3 — Grounding Validator | Post-generation | Yes | +1 call | Medium |
| 4 — Prefill Anchoring | Structural | No | No | Low |
| 5 — Salient Formatting | Attention-based | No | No | Low |
| 6 — Source Tagging + Audit | Self-report + audit | Yes | No | High |

**Start with Options 1 + 5** (primacy prompt + salient formatting) — zero extra cost, immediately effective. Add **Option 3** (grounding validator) for high-stakes data where errors are costly. Use **Option 6** (source tagging) for compliance or auditable systems.
