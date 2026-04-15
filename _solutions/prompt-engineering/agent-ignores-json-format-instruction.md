---
layout: solution
title: "Agent Ignores JSON Output Format Instruction — Returns Prose Instead of JSON"
category: prompt-engineering
description: "Agent is instructed to respond in JSON format but returns prose text, markdown, or JSON wrapped in code fences. Downstream parsing fails with JSONDecodeError."
tags: [json, output-format, parsing, prompt-design]
---

# Agent Ignores JSON Output Format Instruction — Returns Prose Instead of JSON

## Symptom
- Prompt says "respond in JSON" but agent returns natural language
- Sometimes returns JSON inside markdown code fences (````json ... ````), causing `json.loads()` to fail
- Occasionally returns a mix: explanation text followed by JSON
- Behavior is inconsistent — sometimes works, sometimes doesn't

## Root Cause
Vague format instructions lose to the model's default preference for natural language. Without an explicit structure + example + enforcement, the model defaults to whatever feels most natural for the content type. Code fences appear because the model was trained to present code/JSON in fenced blocks.

## Fix

### Step 1: Add explicit constraint + schema + example

```
# WEAK (often ignored)
"Respond in JSON."

# STRONG
"Your response MUST be a valid JSON object with NO other text.
Schema:
{
  "status": "success" | "error",
  "result": <string>,
  "confidence": <number 0.0-1.0>
}

Example response:
{"status": "success", "result": "Fixed the auth error", "confidence": 0.9}

Do NOT include markdown code fences. Do NOT add explanation text before or after the JSON."
```

### Step 2: Parse defensively with fence stripping

```python
import json, re

def parse_agent_json(response: str) -> dict:
    # Strip markdown fences if present
    response = response.strip()
    match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
    if match:
        response = match.group(1).strip()

    # Strip any leading/trailing prose
    json_start = response.find('{')
    json_end = response.rfind('}') + 1
    if json_start >= 0 and json_end > json_start:
        response = response[json_start:json_end]

    return json.loads(response)
```

### Step 3: Add a retry with explicit correction

```python
async def get_json_response(prompt, max_retries=2):
    for attempt in range(max_retries + 1):
        response = await agent.complete(prompt)
        try:
            return parse_agent_json(response)
        except (json.JSONDecodeError, ValueError) as e:
            if attempt == max_retries:
                raise
            prompt += f"\n\nYour previous response was not valid JSON: {response[:200]}\nRespond with ONLY a valid JSON object, nothing else."
```

### Step 4: Use tool_use / structured outputs when available

For Anthropic API, use tool_use to enforce output structure:
```python
tools = [{
    "name": "return_result",
    "description": "Return the structured result",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["success", "error"]},
            "result": {"type": "string"},
            "confidence": {"type": "number"}
        },
        "required": ["status", "result", "confidence"]
    }
}]
# Model is forced to call the tool with valid JSON — no format failures
```

## Expected Token Savings
Debugging JSON format failures across multiple turns: ~6,000 tokens
This fix: ~400 tokens to implement

## Environment
- Anthropic Claude / any LLM with function calling
- Source: direct experience with production agent outputs
