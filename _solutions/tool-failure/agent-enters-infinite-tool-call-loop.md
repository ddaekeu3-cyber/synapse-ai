---
layout: solution
title: "Agent Enters an Infinite Tool Call Loop — Never Returns a Final Answer"
category: tool-failure
description: "The agent calls a tool, gets a result, calls the same tool again with slightly different arguments, gets another result, and calls it again. It loops between two tools indefinitely. It never decides the information is sufficient to answer. The loop burns tokens, hits rate limits, and never produces a response. Without a turn limit, it runs until the context window fills or the budget exhausts."
tags: [tool-failure, infinite-loop, loop-detection, tool-use, agent-loop, stopping-condition, max-turns]
---

# Agent Enters an Infinite Tool Call Loop — Never Returns a Final Answer

## Symptom
- Agent calls the same tool 10+ times without converging
- Agent alternates between two tools in an infinite cycle
- `max_tokens` is exhausted on tool call results with no final text response
- Rate limit triggered by a single agent session making hundreds of calls
- Agent never produces a `stop_reason: "end_turn"` — always `"tool_use"`
- Logs show growing context with each iteration and no exit condition

## Root Cause
The model enters a loop when: (1) tool results don't contain the information it expected (so it tries again), (2) the task is under-specified and the model doesn't know when to stop, (3) two tools each suggest using the other, or (4) the stopping condition was never stated. The fix is to add a hard turn limit, detect repeated identical tool calls, add explicit stopping instructions to the system prompt, and break cycles by surfacing partial results after N iterations.

## Fix

### Option 1: Hard turn limit — unconditionally break after N iterations

```python
import anthropic
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def run_agent_with_turn_limit(
    user_message: str,
    tools: list[dict],
    tool_executor: Callable[[str, dict], Any],
    system: str = "",
    model: str = "claude-sonnet-4-6",
    max_turns: int = 10,          # Hard cap: never exceed this
    warn_at_turn: int = 7         # Warn the model it's running out of turns
) -> dict:
    """
    Run an agent loop with a hard turn limit.
    At warn_at_turn, inject a message telling the model to wrap up.
    At max_turns, force-stop and return partial results.
    """
    messages = [{"role": "user", "content": user_message}]
    turn_count = 0
    tool_calls_made = []

    while turn_count < max_turns:
        # Inject urgency warning as we approach the limit:
        system_with_urgency = system
        if turn_count >= warn_at_turn:
            remaining = max_turns - turn_count
            system_with_urgency = (
                system + f"\n\nIMPORTANT: You have {remaining} tool call(s) remaining. "
                "You must provide a final answer now, even if incomplete. Stop calling tools."
            )

        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_with_urgency,
            tools=tools,
            messages=messages
        )
        turn_count += 1

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b for b in response.content if b.type == "text"]

        # Check if the agent is done:
        if response.stop_reason == "end_turn" or not tool_use_blocks:
            final_text = text_blocks[0].text if text_blocks else "No response generated."
            logger.info(f"Agent completed in {turn_count} turns.")
            return {
                "response": final_text,
                "turns": turn_count,
                "tool_calls": tool_calls_made,
                "completed": True
            }

        # Execute tool calls:
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for tool_block in tool_use_blocks:
            tool_calls_made.append({"tool": tool_block.name, "input": tool_block.input})
            result = tool_executor(tool_block.name, tool_block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": str(result)
            })
        messages.append({"role": "user", "content": tool_results})

    # Forced stop — generate a partial response:
    logger.warning(f"Agent hit turn limit ({max_turns}). Forcing final response.")
    force_response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=messages + [{
            "role": "user",
            "content": "You have reached the maximum number of tool calls. Provide the best answer you can with the information gathered so far."
        }]
    )
    final_text = next((b.text for b in force_response.content if b.type == "text"), "Could not complete the task within the turn limit.")
    return {
        "response": final_text,
        "turns": turn_count,
        "tool_calls": tool_calls_made,
        "completed": False,
        "reason": "turn_limit_reached"
    }
```

### Option 2: Repeated call detection — identify and break cycles

```python
import anthropic
import hashlib
import json
import logging
from collections import Counter
from typing import Any, Callable

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def fingerprint_tool_call(tool_name: str, tool_input: dict) -> str:
    """Create a stable hash of a tool call for deduplication."""
    normalized = json.dumps({"name": tool_name, "input": tool_input}, sort_keys=True)
    return hashlib.md5(normalized.encode()).hexdigest()[:12]

def run_agent_with_cycle_detection(
    user_message: str,
    tools: list[dict],
    tool_executor: Callable[[str, dict], Any],
    system: str = "",
    model: str = "claude-sonnet-4-6",
    max_turns: int = 15,
    max_identical_calls: int = 2    # Allow at most 2 identical calls
) -> dict:
    """
    Detect and break loops where the agent calls the same tool with the same input repeatedly.
    """
    messages = [{"role": "user", "content": user_message}]
    call_history: Counter = Counter()
    turn_count = 0

    while turn_count < max_turns:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            tools=tools,
            messages=messages
        )
        turn_count += 1

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks or response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if b.type == "text"), "")
            return {"response": text, "turns": turn_count, "completed": True}

        # Check for repeated calls:
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        cycle_detected = False

        for tool_block in tool_use_blocks:
            fp = fingerprint_tool_call(tool_block.name, tool_block.input)
            call_history[fp] += 1

            if call_history[fp] > max_identical_calls:
                logger.warning(
                    f"Cycle detected: tool '{tool_block.name}' called {call_history[fp]} times "
                    f"with identical arguments. Breaking loop."
                )
                # Tell the model about the cycle:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": (
                        f"[LOOP DETECTED] You have called {tool_block.name} with these same arguments "
                        f"{call_history[fp]} times. The result will not change. "
                        f"Stop calling this tool and provide your best answer with the information you have."
                    ),
                    "is_error": True
                })
                cycle_detected = True
            else:
                result = tool_executor(tool_block.name, tool_block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": str(result)
                })

        messages.append({"role": "user", "content": tool_results})

        if cycle_detected:
            # One final chance to respond:
            final_response = client.messages.create(
                model=model,
                max_tokens=1024,
                system=system,
                tools=tools,
                tool_choice={"type": "none"},  # Force text response
                messages=messages
            )
            text = next((b.text for b in final_response.content if b.type == "text"), "Could not complete.")
            return {"response": text, "turns": turn_count, "completed": False, "reason": "cycle_detected"}

    return {"response": "Turn limit reached.", "turns": turn_count, "completed": False}
```

### Option 3: Stopping condition in system prompt — tell the model when to stop

```python
import anthropic

client = anthropic.Anthropic()

# Bad system prompt — no stopping condition:
BAD_SYSTEM = "You are a research assistant. Use tools to find information."

# Good system prompt — explicit stopping conditions:
GOOD_SYSTEM = """You are a research assistant.

## Tool Use Rules

**When to stop calling tools:**
- You have found the specific information requested
- You have called 3+ tools and have enough to give a useful answer (even if incomplete)
- A tool returns an error or empty result twice — report what you found and move on
- You're searching for something that clearly doesn't exist — say so and stop

**When NOT to keep searching:**
- Don't call the same tool with slightly rephrased queries hoping for a different result
- Don't cross-reference every piece of information with multiple tools
- Don't verify information that is already clear from previous tool results

**After getting tool results:**
Ask yourself: "Do I have enough to answer the user's question?" If yes, answer now.
If no, call one more tool. After 5 tools total, answer with what you have.

**Output format:**
- Start your final response immediately after deciding you have enough information
- Don't announce "I'm now going to call tool X" — just call it
"""

def research_agent(question: str) -> str:
    """Research agent with explicit stopping conditions."""
    response_accumulator = []
    messages = [{"role": "user", "content": question}]
    tools = [/* your tools here */]

    for _ in range(10):  # Hard cap
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=GOOD_SYSTEM,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        # ... handle tool calls ...
    return "Research limit reached."
```

### Option 4: Token budget enforcement — stop when budget is low

```python
import anthropic
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def run_agent_with_token_budget(
    user_message: str,
    tools: list[dict],
    tool_executor: Callable[[str, dict], Any],
    system: str = "",
    model: str = "claude-sonnet-4-6",
    total_token_budget: int = 50_000,  # Stop before spending more than this
    reserve_tokens: int = 4096         # Reserve for final response
) -> dict:
    """
    Track cumulative token usage. Stop calling tools when budget is nearly exhausted.
    Prevents runaway loops from draining the API budget.
    """
    messages = [{"role": "user", "content": user_message}]
    tokens_used = 0
    turn_count = 0

    while True:
        remaining_budget = total_token_budget - tokens_used - reserve_tokens
        if remaining_budget < 2000:
            logger.warning(f"Token budget nearly exhausted ({tokens_used}/{total_token_budget}). Forcing stop.")
            break

        response = client.messages.create(
            model=model,
            max_tokens=min(4096, remaining_budget),
            system=system,
            tools=tools,
            messages=messages
        )

        tokens_used += response.usage.input_tokens + response.usage.output_tokens
        turn_count += 1

        logger.debug(f"Turn {turn_count}: used {tokens_used}/{total_token_budget} tokens")

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if b.type == "text"), "")
            return {
                "response": text,
                "tokens_used": tokens_used,
                "turns": turn_count,
                "completed": True
            }

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            text = next((b.text for b in response.content if b.type == "text"), "")
            return {"response": text, "tokens_used": tokens_used, "turns": turn_count, "completed": True}

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for tool_block in tool_use_blocks:
            result = tool_executor(tool_block.name, tool_block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": str(result)[:2000]  # Truncate large results
            })
        messages.append({"role": "user", "content": tool_results})

    # Force final response with remaining budget:
    final = client.messages.create(
        model=model,
        max_tokens=reserve_tokens,
        system=system,
        messages=messages + [{"role": "user", "content": "Provide your best answer based on what you've found so far."}]
    )
    text = next((b.text for b in final.content if b.type == "text"), "Budget exhausted.")
    return {"response": text, "tokens_used": tokens_used, "turns": turn_count, "completed": False}
```

### Option 5: Tool call graph analysis — detect multi-tool cycles

```python
import anthropic
import logging
from collections import deque
from typing import Any, Callable

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

class ToolCallGraph:
    """
    Track sequences of tool calls to detect cycles.
    A cycle is detected when the same sequence of 2+ tools repeats.
    """

    def __init__(self, window: int = 6, max_cycle_repeat: int = 2):
        self._calls: deque[str] = deque(maxlen=window * max_cycle_repeat)
        self._window = window
        self._max_repeat = max_cycle_repeat

    def record(self, tool_name: str):
        self._calls.append(tool_name)

    def detect_cycle(self) -> tuple[bool, list[str]]:
        """
        Detect if the last N calls form a repeating pattern.
        Returns (cycle_detected, cycle_pattern).
        """
        calls = list(self._calls)
        if len(calls) < 4:
            return False, []

        # Check for cycles of length 2, 3, 4:
        for cycle_len in range(2, min(5, len(calls) // 2 + 1)):
            last_n = calls[-cycle_len * self._max_repeat:]
            if len(last_n) < cycle_len * 2:
                continue

            pattern = last_n[:cycle_len]
            # Check if pattern repeats:
            all_match = True
            for rep in range(1, self._max_repeat):
                segment = last_n[rep * cycle_len:(rep + 1) * cycle_len]
                if segment != pattern:
                    all_match = False
                    break

            if all_match:
                return True, pattern

        return False, []

def run_agent_with_cycle_graph(
    user_message: str,
    tools: list[dict],
    tool_executor: Callable[[str, dict], Any],
    system: str = "",
    model: str = "claude-sonnet-4-6",
    max_turns: int = 20
) -> dict:
    messages = [{"role": "user", "content": user_message}]
    call_graph = ToolCallGraph(window=6, max_cycle_repeat=2)
    turn_count = 0

    while turn_count < max_turns:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            tools=tools,
            messages=messages
        )
        turn_count += 1

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks or response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if b.type == "text"), "")
            return {"response": text, "turns": turn_count, "completed": True}

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for tool_block in tool_use_blocks:
            call_graph.record(tool_block.name)

        cycle_found, cycle_pattern = call_graph.detect_cycle()

        if cycle_found:
            logger.warning(f"Tool cycle detected: {cycle_pattern}. Breaking loop.")
            for tool_block in tool_use_blocks:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": (
                        f"[CYCLE DETECTED] You are repeating the pattern: {' → '.join(cycle_pattern)}. "
                        "Stop calling tools and provide your final answer now."
                    ),
                    "is_error": True
                })
        else:
            for tool_block in tool_use_blocks:
                result = tool_executor(tool_block.name, tool_block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": str(result)
                })

        messages.append({"role": "user", "content": tool_results})

        if cycle_found:
            final = client.messages.create(
                model=model, max_tokens=1024, system=system,
                tools=tools, tool_choice={"type": "none"}, messages=messages
            )
            text = next((b.text for b in final.content if b.type == "text"), "")
            return {"response": text, "turns": turn_count, "completed": False, "reason": "cycle"}

    return {"response": "Turn limit reached.", "turns": turn_count, "completed": False}
```

### Option 6: Result convergence check — stop when new calls add no new info

```python
import anthropic
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def information_gain(new_result: str, accumulated_results: list[str]) -> float:
    """
    Estimate how much new information a tool result adds.
    Returns 0.0 (no new info) to 1.0 (completely new info).
    Simple word overlap heuristic — replace with embedding similarity for production.
    """
    if not accumulated_results:
        return 1.0

    new_words = set(new_result.lower().split())
    all_previous_words = set(" ".join(accumulated_results).lower().split())

    if not new_words:
        return 0.0

    overlap = new_words & all_previous_words
    gain = 1.0 - len(overlap) / len(new_words)
    return gain

def run_agent_convergence_check(
    user_message: str,
    tools: list[dict],
    tool_executor: Callable[[str, dict], Any],
    system: str = "",
    model: str = "claude-sonnet-4-6",
    max_turns: int = 15,
    min_information_gain: float = 0.1  # Stop if <10% new info per call
) -> dict:
    messages = [{"role": "user", "content": user_message}]
    accumulated_results: list[str] = []
    turn_count = 0

    while turn_count < max_turns:
        response = client.messages.create(
            model=model, max_tokens=4096, system=system, tools=tools, messages=messages
        )
        turn_count += 1

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks or response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if b.type == "text"), "")
            return {"response": text, "turns": turn_count, "completed": True}

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        low_gain_count = 0

        for tool_block in tool_use_blocks:
            result = str(tool_executor(tool_block.name, tool_block.input))
            gain = information_gain(result, accumulated_results)

            if gain < min_information_gain:
                low_gain_count += 1
                logger.info(f"Low info gain ({gain:.2f}) from {tool_block.name} — may be looping")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": result + "\n[Note: This result is very similar to previous results. Consider stopping.]"
                })
            else:
                tool_results.append({"type": "tool_result", "tool_use_id": tool_block.id, "content": result})
                accumulated_results.append(result)

        messages.append({"role": "user", "content": tool_results})

        if low_gain_count == len(tool_use_blocks) and turn_count >= 3:
            logger.warning("All tool calls returning low-information results. Stopping loop.")
            final = client.messages.create(
                model=model, max_tokens=1024, system=system,
                tools=tools, tool_choice={"type": "none"}, messages=messages
            )
            text = next((b.text for b in final.content if b.type == "text"), "")
            return {"response": text, "turns": turn_count, "completed": False, "reason": "convergence"}

    return {"response": "Turn limit reached.", "turns": turn_count, "completed": False}
```

## Loop Detection Strategy Summary

| Detection Method | Best For | Overhead |
|---|---|---|
| Hard turn limit (Option 1) | All agents — baseline protection | None |
| Identical call detection (Option 2) | Single-tool loops | Low |
| System prompt stopping conditions (Option 3) | Preventive (preferred) | None |
| Token budget enforcement (Option 4) | Cost-sensitive agents | Low |
| Multi-tool cycle detection (Option 5) | A↔B↔C loops | Low |
| Convergence / info gain check (Option 6) | Research agents | Medium |

## Recommended Configuration
```python
# Combine: hard limit + cycle detection + stopping instructions
result = run_agent_with_turn_limit(
    user_message=question,
    tools=my_tools,
    tool_executor=execute_tool,
    system=GOOD_SYSTEM,  # Explicit stopping conditions
    max_turns=10,        # Hard cap
    warn_at_turn=7       # Urgency warning
)
```

## Expected Token Savings
Undetected 20-turn loop: 20 × average_turn_cost (can be 100,000+ tokens for data-heavy tools)
With 10-turn hard limit: at most 10 turns before forced stop
Emergency stop saves 50%+ of loop cost; proper stopping conditions in system prompt prevent loops entirely

## Environment
- Any agent with 3+ tools where the task completion criteria are not always clear; loops are most common in: research agents (keep searching for better data), multi-step planning agents (keep refining the plan), and agents using search + lookup tool pairs; add a hard turn limit to every agent as a minimum baseline — it's one line of code and prevents the worst outcomes
- Source: direct experience; infinite tool loops are responsible for ~15% of agent cost overruns and are the most common cause of "the agent is stuck" support tickets for autonomous agents
