---
layout: solution
title: "Agent Loses Tool Results After Context Prune — Re-runs Already-Completed Steps"
category: memory
description: "When context is pruned to manage length, agent loses memory of completed tool calls. It re-runs web searches, file reads, or API calls that were already done earlier in the session."
tags: [context-prune, tool-results, memory, duplicate-calls, efficiency]
---

# Agent Loses Tool Results After Context Prune — Re-runs Already-Completed Steps

## Symptom
- Agent searches for something, finds it, continues work
- Later in a long session, asks for the same search again
- Re-runs tool calls that were already completed
- User sees duplicate API calls or tool executions
- Token cost doubles for already-completed work
- May produce different results if tool output changed between calls

## Root Cause
When context pruning removes old messages (including old tool call results), the agent has no persistent memory of what it already retrieved. From its perspective, it has never run those tool calls — so it runs them again.

## Fix

### Option 1: Persist key tool results to a summary file

```python
import json

TOOL_MEMORY_FILE = "/workspace/.agent_tool_memory.json"

def save_tool_result(tool_name, args, result, summary=None):
    """Save tool result to persistent file that survives context pruning"""
    try:
        memory = json.loads(open(TOOL_MEMORY_FILE).read()) if os.path.exists(TOOL_MEMORY_FILE) else {}
    except Exception:
        memory = {}

    key = f"{tool_name}:{hash(str(args))}"
    memory[key] = {
        "tool": tool_name,
        "args": str(args)[:200],
        "summary": summary or str(result)[:500],
        "timestamp": time.time()
    }

    with open(TOOL_MEMORY_FILE, 'w') as f:
        json.dump(memory, f, indent=2)

def get_cached_result(tool_name, args, max_age_seconds=3600):
    """Check if we already ran this tool call"""
    if not os.path.exists(TOOL_MEMORY_FILE):
        return None
    memory = json.loads(open(TOOL_MEMORY_FILE).read())
    key = f"{tool_name}:{hash(str(args))}"
    if key in memory:
        age = time.time() - memory[key]['timestamp']
        if age < max_age_seconds:
            return memory[key]['summary']
    return None
```

### Option 2: Include tool memory summary in system prompt refresh

```python
def build_system_with_tool_memory():
    """Load persisted tool results and include in system prompt"""
    if not os.path.exists(TOOL_MEMORY_FILE):
        return BASE_SYSTEM_PROMPT

    memory = json.loads(open(TOOL_MEMORY_FILE).read())
    if not memory:
        return BASE_SYSTEM_PROMPT

    summary_lines = [
        f"- {v['tool']}({v['args'][:50]}): {v['summary'][:100]}"
        for v in list(memory.values())[-10:]  # Last 10 tool results
    ]

    return BASE_SYSTEM_PROMPT + "\n\nAlready retrieved this session:\n" + "\n".join(summary_lines)
```

### Option 3: Add tool call deduplication at the framework level

```python
class DeduplicatingToolRunner:
    def __init__(self, session_id):
        self.session_id = session_id
        self.call_cache = {}  # In-memory for this session

    async def run_tool(self, tool_name, args):
        cache_key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"

        if cache_key in self.call_cache:
            cached = self.call_cache[cache_key]
            print(f"Tool cache hit: {tool_name} (original result from {cached['when']})")
            return cached['result']

        result = await actual_tool_runner(tool_name, args)
        self.call_cache[cache_key] = {
            'result': result,
            'when': time.strftime('%H:%M:%S')
        }
        return result
```

### Option 4: Session handoff file for context resets

When intentionally resetting context, write a handoff:

```python
HANDOFF_FILE = "/workspace/.session_handoff.md"

def write_session_handoff(completed_steps, key_findings, current_goal):
    """Write handoff before context reset"""
    content = f"""# Session Handoff — {time.strftime('%Y-%m-%d %H:%M')}

## Completed steps (do NOT redo these)
{chr(10).join(f'- {step}' for step in completed_steps)}

## Key findings from tool calls
{chr(10).join(f'- {k}: {v}' for k, v in key_findings.items())}

## Current goal
{current_goal}

## Next step
{get_next_step()}
"""
    with open(HANDOFF_FILE, 'w') as f:
        f.write(content)
```

## Expected Token Savings
Re-running 5 tool calls that were already done: ~10,000 tokens
Tool result persistence: prevents all duplicate calls

## Environment
- Long agent sessions with context pruning enabled
- Agents with expensive or slow tool calls (web search, API calls)
- Source: direct experience with multi-hour agent sessions
