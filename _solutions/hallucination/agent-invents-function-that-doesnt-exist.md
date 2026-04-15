---
layout: solution
title: "Agent Invents a Function or Method That Doesn't Exist in the Codebase"
category: hallucination
description: "Agent confidently calls a function, method, or module that does not exist. Code fails at runtime with NameError or ImportError. Agent may not notice and continue building on the fabricated API."
tags: [hallucination, code-generation, verification, grounding]
---

# Agent Invents a Function or Method That Doesn't Exist in the Codebase

## Symptom
- Agent writes code calling `some_module.process_data()` — function doesn't exist
- Runtime error: `AttributeError`, `NameError`, or `ImportError`
- Agent may acknowledge the error but invent a similar non-existent alternative
- Multiple rounds of "fix the error" produce different non-existent functions
- Agent may cite documentation for the invented API

## Root Cause
LLMs interpolate from training patterns. If the model has seen similar libraries, it generates plausible-looking APIs that may not exist in the specific version or library being used. The model has no real-time access to check whether a function exists — it generates the most statistically likely API shape.

## Fix

### Option 1: Require citation before code use

```
System prompt addition:
"Before calling any function or method, verify it exists by searching the codebase
or checking the library documentation. If you cannot verify a function exists,
state: 'I believe [function] exists but could not verify — please confirm.'
Never call a function you invented without verification."
```

### Option 2: Search codebase before generating code

Prompt the agent to verify before writing:

```
User: "Before writing code that uses [library], search for actual usage examples
in the codebase first. Only use patterns that appear in existing code."
```

Or as a tool call:
```python
# Agent should call this before using any function
async def verify_function_exists(module_path, function_name):
    import importlib, inspect
    try:
        mod = importlib.import_module(module_path)
        return hasattr(mod, function_name), dir(mod)
    except ImportError:
        return False, []
```

### Option 3: Run generated code in sandbox before accepting

```yaml
agent:
  code_execution:
    sandbox: true
    pre_accept_test: true
    on_error:
      action: report_and_retry
      provide_traceback: true
      max_retries: 2
```

When `AttributeError` or `ImportError` appears in the sandbox output, feed it back to the agent with the real available API:

```python
async def execute_with_feedback(code, agent):
    try:
        result = sandbox.run(code)
        return result
    except (AttributeError, ImportError) as e:
        available = get_available_api(e.module)
        return await agent.retry(
            f"Error: {e}\nAvailable in {e.module}: {available[:20]}"
        )
```

### Option 4: Provide actual API surface in context

```python
import inspect, mymodule

# Get real API surface and include in system prompt
api_summary = {
    name: inspect.signature(func)
    for name, func in inspect.getmembers(mymodule, inspect.isfunction)
}

SYSTEM_PROMPT = f"""
Available functions in mymodule:
{api_summary}

Only use functions listed above. Do not invent new ones.
"""
```

## Recovery
When you discover fabricated functions:
1. Stop the agent
2. Show it the actual error message + available API
3. Restart with "the function X does not exist, available functions are: [list]"
4. Add the fabricated function name to your prompt as a known-bad example

## Expected Token Savings
Multiple rounds of debugging fabricated APIs: ~15,000 tokens
This fix: ~500 tokens with upfront verification

## Environment
- Any code-generating agent, especially for less common libraries
- Higher risk with: niche Python packages, internal company APIs, newer SDKs
- Source: direct experience, extremely common pattern
