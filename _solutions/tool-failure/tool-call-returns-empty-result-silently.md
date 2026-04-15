---
layout: solution
title: "Tool Call Returns Empty Result — Agent Proceeds as if Successful"
category: tool-failure
description: "Tool call returns 200 OK with empty body, null result, or empty array. Agent interprets this as success and continues with wrong assumptions, causing downstream errors."
tags: [mcp, tool-result, empty-result, validation]
---

# Tool Call Returns Empty Result — Agent Proceeds as if Successful

## Symptom
- Tool call returns 200 with `{}`, `null`, `[]`, or `{"result": null}`
- Agent doesn't report an error — it continues working
- Subsequent steps fail or produce wrong output because they built on an empty result
- Problem: agent says "I searched for X" but acted on no results

## Root Cause
Tools return empty results for two distinct reasons:
1. **Legitimate empty**: The query found nothing (correct behavior)
2. **Silent failure**: The tool hit an internal error but returned empty instead of an error code

Without explicit validation, the agent can't distinguish these cases and treats both as "no results found" — which is only correct for case 1.

## Fix

### Option 1: Add result validation in the tool call wrapper

```python
async def call_tool(tool_name, params, require_results=True):
    result = await mcp_client.call_tool(tool_name, params)

    if result is None or result == {} or result == []:
        if require_results:
            raise ToolEmptyResultError(
                f"Tool '{tool_name}' returned empty result for params: {params}. "
                f"This may indicate a tool failure, not a legitimate empty response."
            )
        else:
            # Log but don't fail — caller expects empty is possible
            logger.warning(f"Tool '{tool_name}' returned empty result")

    return result
```

### Option 2: Configure empty result handling per tool

```yaml
tools:
  web_search:
    on_empty_result:
      action: warn_and_continue   # Empty is legitimate — no results found
  database_query:
    on_empty_result:
      action: raise_error         # DB query should never return nothing
  file_read:
    on_empty_result:
      action: raise_error         # Empty file read = likely path error
```

### Option 3: Add result shape validation

```python
TOOL_RESULT_VALIDATORS = {
    'web_search': lambda r: isinstance(r, list),  # Must be a list (can be empty)
    'read_file': lambda r: isinstance(r, str) and len(r) > 0,  # Must have content
    'db_query': lambda r: 'rows' in r,  # Must have rows key
}

def validate_tool_result(tool_name, result):
    validator = TOOL_RESULT_VALIDATORS.get(tool_name)
    if validator and not validator(result):
        raise ValueError(f"Tool '{tool_name}' result failed validation: {result}")
    return result
```

### Option 4: Prompt the agent to verify before proceeding

```
System prompt addition:
"After each tool call, explicitly state what you received.
If a tool returned empty or null, do NOT proceed to the next step —
instead report: 'Tool X returned no results. This may indicate an error.
Shall I retry or proceed differently?'"
```

## Expected Token Savings
Agent proceeding on empty results and failing 3 steps later: ~12,000 tokens
This fix: ~200 tokens

## Environment
- MCP tools with any backend
- Most common with: web search, database query, file read tools
- Source: direct experience, common in agent pipelines
