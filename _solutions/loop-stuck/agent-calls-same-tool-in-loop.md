---
layout: solution
title: "Agent Calls the Same Tool Repeatedly in a Loop Without Progress"
category: loop-stuck
description: "Agent calls the same tool 5, 10, 20+ times with minor variations, making no progress. Each call fails or returns the same result. No circuit breaker stops the loop."
tags: [loop, tool-failure, circuit-breaker, retry-storm]
---

# Agent Calls the Same Tool Repeatedly in a Loop Without Progress

## Symptom
- Agent calls `web_search`, `read_file`, or another tool 10+ times in one session
- Each call has slight variations but same fundamental query
- Tool keeps returning the same result or failing with the same error
- Agent generates explanations for each failure and then tries again
- Token usage spikes — each failed attempt adds to context
- Eventually hits context limit or token budget

## Root Cause
No circuit breaker on tool calls. The agent is trained to be persistent but has no mechanism to recognize "I've already tried this approach multiple times and it's not working." Without a maximum-attempt policy, the agent's helpfulness heuristic drives it to keep trying.

## Fix

### Option 1: Configure tool-level circuit breaker

```yaml
# openclaw.config.yaml
agent:
  tool_circuit_breaker:
    enabled: true
    per_tool:
      web_search:
        failure_threshold: 3      # Stop after 3 failures
        same_query_threshold: 2   # Stop if same query seen twice
      read_file:
        failure_threshold: 2
      database_query:
        failure_threshold: 3
    on_open:
      action: skip_tool_and_report
      message: "Tool {tool_name} circuit open after {attempts} attempts. Reporting failure."
    reset_timeout_ms: 60000
```

### Option 2: Track tool call history in agent loop

```python
from collections import Counter

class ToolCallTracker:
    def __init__(self, max_same_tool=5, max_same_args=2):
        self.call_counts = Counter()
        self.call_args = {}
        self.max_same_tool = max_same_tool
        self.max_same_args = max_same_args

    def should_allow(self, tool_name, args):
        key = f"{tool_name}:{hash(str(args))}"

        self.call_counts[tool_name] += 1
        self.call_args[key] = self.call_args.get(key, 0) + 1

        if self.call_counts[tool_name] > self.max_same_tool:
            raise CircuitOpenError(
                f"{tool_name} called {self.call_counts[tool_name]} times. "
                f"Stopping — likely stuck in loop."
            )
        if self.call_args[key] > self.max_same_args:
            raise CircuitOpenError(
                f"{tool_name} called with same args {self.call_args[key]} times. "
                f"Stopping — query is not producing new results."
            )

tracker = ToolCallTracker()
```

### Option 3: Add loop-detection system prompt instruction

```
System prompt addition:
"Tool call discipline:
- If a tool call fails, try a DIFFERENT approach — not the same call again.
- If you've made the same tool call more than 2 times without progress, STOP and report failure.
- Never retry the exact same arguments that already failed.
- If stuck, say: 'I've tried [approach] X times without success. I cannot complete this task with available tools.'"
```

### Option 4: Token budget circuit breaker

```python
MAX_TOKENS_PER_TASK = 50000

async def run_task_with_budget(task, agent):
    tokens_used = 0
    while True:
        response = await agent.step(task)
        tokens_used += response.usage.total_tokens

        if tokens_used > MAX_TOKENS_PER_TASK:
            return {
                "status": "budget_exceeded",
                "tokens_used": tokens_used,
                "last_response": response,
                "message": "Task stopped — token budget exceeded. Manual review needed."
            }

        if response.is_complete:
            return response
```

## Detection Pattern

```python
LOOP_SIGNALS = [
    "I'll try a different",
    "Let me try again",
    "I'll attempt this another way",
    "Let me search for",
    "I'll look for this differently",
]

def detect_loop_behavior(agent_message):
    return sum(1 for s in LOOP_SIGNALS if s.lower() in agent_message.lower()) >= 3
```

## Expected Token Savings
Unchecked tool retry loop (50 iterations): 80,000–200,000 tokens wasted
This fix (circuit breaker at 3 attempts): saves 90% of that waste

## Environment
- Any agent with tool-use capability
- Highest risk with: web search tools, external APIs that may be down
- Source: direct experience, very common in agentic task pipelines
