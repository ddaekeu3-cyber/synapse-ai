---
layout: solution
title: "Agent increments counter but never checks termination condition"
category: loop-stuck
description: "Agent loop increments a step counter or result set but checks the wrong variable — or checks it at the wrong point — so the exit condition is never triggered and the loop runs until context window exhaustion."
tags: [loop-stuck, termination, exit-condition, counter, agentic-loop, off-by-one]
---

## Symptom

The agent produces N identical or nearly-identical results, fills the context window, then crashes with a context length error. The loop counter variable is incremented correctly but the `while` condition was written against a different variable, or the check comes after the state mutation that would have triggered it. Logging shows step count climbing to 50, 100, 200 before the process dies.

## Root Cause

The termination condition is logically correct in isolation but is placed or written incorrectly relative to state updates:
- `while results < MAX` but `results` is never mutated (a different variable `count` is)
- Exit check placed before the step that would satisfy it
- Off-by-one: `while i < N` with `i` starting at 1 instead of 0, or `<=` vs `<`
- Condition checks a stale copy of mutable state captured before the loop started

---

## Option 1 — Canonical agentic loop with explicit turn counter

**Make the termination condition the single source of truth: one counter, one check, one increment.**

```python
import anthropic

client = anthropic.Anthropic()

MAX_TURNS = 15   # absolute ceiling

SEARCH_TOOL = {
    "name": "search",
    "description": "Search for information.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


def search(query: str) -> str:
    return f"Search results for '{query}': [result A, result B, result C]"


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    turn = 0   # single counter, checked in one place

    while turn < MAX_TURNS:   # termination condition
        turn += 1              # increment at top of loop — always happens
        print(f"  Turn {turn}/{MAX_TURNS}")

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[SEARCH_TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            return f"Unexpected stop: {response.stop_reason}"

        tc = next(b for b in response.content if b.type == "tool_use")
        result = search(tc.input["query"])
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
        ]

    # Reached limit — force final answer
    messages.append({"role": "user", "content": "[System: turn limit reached — provide your best answer now.]"})
    response = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1024, messages=messages,
    )
    return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Research the history of the internet."))
```

**Expected Token Savings:** Hard turn cap prevents unbounded loops from consuming the entire 200k context window — caps worst-case spend at `MAX_TURNS × avg_turn_tokens`.

**Environment:** Any agentic loop; canonical pattern regardless of task type.

---

## Option 2 — Goal-completion detector as primary termination signal

**Primary exit: model signals task completion. Turn counter is a safety net, not the main exit.**

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_TURNS = 20

TOOLS = [
    {
        "name": "search",
        "description": "Search for information.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "task_complete",
        "description": "Call this when you have gathered all information needed to answer the user's question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "answer":      {"type": "string", "description": "Final answer to the user"},
                "confidence":  {"type": "string", "enum": ["high", "medium", "low"]},
                "sources_used": {"type": "integer"},
            },
            "required": ["answer", "confidence"],
        },
    },
]


def search(query: str) -> str:
    return f"Results for '{query}': [data point 1, data point 2]"


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for turn in range(1, MAX_TURNS + 1):
        print(f"  Turn {turn}")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tc = next(b for b in response.content if b.type == "tool_use")
        messages.append({"role": "assistant", "content": response.content})

        if tc.name == "task_complete":
            # Primary termination: model explicitly signals completion
            result = tc.input
            print(f"  Task complete (confidence={result.get('confidence')}, sources={result.get('sources_used', '?')})")
            return result["answer"]

        elif tc.name == "search":
            tool_result = search(tc.input["query"])
        else:
            tool_result = json.dumps({"error": f"Unknown tool: {tc.name}"})

        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": tool_result}],
        })

    return "Task did not complete within the allowed turns."


print(run_agent("Find three key facts about climate change."))
```

**Expected Token Savings:** `task_complete` tool gives the model an explicit "I'm done" signal — most tasks complete in 2–5 turns instead of running to `MAX_TURNS`, saving 60–80% of loop token overhead.

**Environment:** Research and multi-step information gathering agents; pairs well with a turn limit as a secondary safety net.

---

## Option 3 — Result accumulator with count-based termination

**Track collected results explicitly. Exit when the result count reaches the target — not when a counter reaches a threshold.**

```python
import json
import anthropic

client = anthropic.Anthropic()

TARGET_RESULTS = 5   # we want exactly 5 data points
MAX_TURNS      = 30


EXTRACT_TOOL = {
    "name": "extract_data_point",
    "description": "Extract one data point from the source. Call once per distinct finding.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fact":   {"type": "string", "description": "The data point"},
            "source": {"type": "string", "description": "Where this came from"},
        },
        "required": ["fact", "source"],
    },
}

SEARCH_TOOL = {
    "name": "search",
    "description": "Search for more information.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


def run_collector(topic: str) -> list[dict]:
    messages  = [{"role": "user", "content": f"Collect {TARGET_RESULTS} distinct facts about: {topic}"}]
    collected: list[dict] = []   # termination tracks THIS, not a separate counter

    for turn in range(MAX_TURNS):
        # Termination condition checks the actual result collection
        if len(collected) >= TARGET_RESULTS:
            print(f"  Goal reached: {len(collected)} results collected.")
            break

        print(f"  Turn {turn+1} | collected={len(collected)}/{TARGET_RESULTS}")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[EXTRACT_TOOL, SEARCH_TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason != "tool_use":
            break

        tc = next(b for b in response.content if b.type == "tool_use")
        messages.append({"role": "assistant", "content": response.content})

        if tc.name == "extract_data_point":
            # The variable the exit condition checks is mutated here
            collected.append(tc.input)
            tool_result = json.dumps({"status": "recorded", "total": len(collected), "remaining": TARGET_RESULTS - len(collected)})
        elif tc.name == "search":
            tool_result = f"Search results for '{tc.input['query']}': [finding A, finding B, finding C]"
        else:
            tool_result = json.dumps({"error": "unknown tool"})

        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": tool_result}],
        })

    return collected


results = run_collector("renewable energy")
print(f"\nCollected {len(results)} facts:")
for i, r in enumerate(results, 1):
    print(f"  {i}. {r['fact'][:60]}")
```

**Expected Token Savings:** Result-count termination exits the moment the goal is achieved — no over-collection. For a 5-result target, prevents collecting 50+ results before a broken counter check would have fired.

**Environment:** Data collection, research, and enumeration agents where the task is "collect N items".

---

## Option 4 — Stateful loop inspector that detects missing progress

**Track state snapshots between turns. If state hasn't changed for K turns, the loop is stuck — exit with a diagnostic.**

```python
import hashlib
import json
import anthropic

client = anthropic.Anthropic()

MAX_TURNS    = 20
STALE_LIMIT  = 3   # exit if no new tool calls or new content for this many turns


def state_fingerprint(messages: list[dict]) -> str:
    """Fingerprint the current state — changes when new information is added."""
    relevant = [
        m for m in messages
        if m["role"] in ("assistant", "user") and isinstance(m.get("content"), (str, list))
    ]
    payload = json.dumps(relevant[-6:], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


SEARCH_TOOL = {
    "name": "search",
    "description": "Search for information.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}

_search_cache: dict[str, str] = {}


def search(query: str) -> str:
    # Deliberately return the same result for similar queries (simulates stale results)
    key = query.lower()[:20]
    if key in _search_cache:
        return _search_cache[key] + " [CACHED — same result]"
    result = f"Results for '{query}': [generic result A, generic result B]"
    _search_cache[key] = result
    return result


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    stale_turns = 0
    last_fingerprint = state_fingerprint(messages)

    for turn in range(1, MAX_TURNS + 1):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[SEARCH_TOOL],
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        new_fingerprint = state_fingerprint(messages)
        if new_fingerprint == last_fingerprint:
            stale_turns += 1
            print(f"  Turn {turn}: no state change ({stale_turns}/{STALE_LIMIT} stale turns)")
        else:
            stale_turns = 0
            last_fingerprint = new_fingerprint

        if stale_turns >= STALE_LIMIT:
            print(f"  Loop stuck — no progress for {STALE_LIMIT} turns. Forcing exit.")
            messages.append({
                "role": "user",
                "content": "You appear to be repeating the same actions. Provide your best answer with what you have.",
            })
            final = client.messages.create(model="claude-sonnet-4-6", max_tokens=512, messages=messages)
            return next(b.text for b in final.content if hasattr(b, "text"))

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            result = search(tc.input["query"])
            messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}],
            })

    return "Reached turn limit."


print(run_agent("Find information about quantum computing."))
```

**Expected Token Savings:** Stale-state detection exits a stuck loop after 3 turns of no progress instead of running to MAX_TURNS — saves up to 85% of loop tokens for the common "same-search-different-wording" stuck pattern.

**Environment:** Agents with search or retrieval tools where looping without progress is a known failure mode.

---

## Option 5 — Pytest: verify loop termination on all exit paths

**Write tests that assert every loop exit path is reachable and the counter variable is the one being checked.**

```python
"""
tests/test_loop_termination.py
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


# Simulated agent loop (the function under test)
def run_agent_loop(responses: list[str], max_turns: int = 10) -> tuple[str, int]:
    """Returns (final_answer, turns_taken)."""
    turn = 0
    while turn < max_turns:
        turn += 1
        response = responses[turn - 1] if turn <= len(responses) else "end_turn"
        if response == "end_turn":
            return ("Final answer", turn)
        # Process tool call...
    return ("Timed out", turn)


def test_exits_on_end_turn():
    responses = ["tool_call", "tool_call", "end_turn"]
    answer, turns = run_agent_loop(responses, max_turns=10)
    assert answer == "Final answer"
    assert turns == 3, f"Expected 3 turns, got {turns}"


def test_exits_at_max_turns():
    responses = ["tool_call"] * 20   # never ends naturally
    answer, turns = run_agent_loop(responses, max_turns=10)
    assert answer == "Timed out"
    assert turns == 10, f"Expected exactly 10 turns, got {turns}"


def test_exits_on_first_turn():
    responses = ["end_turn"]
    answer, turns = run_agent_loop(responses, max_turns=10)
    assert turns == 1


def test_zero_turns_not_possible():
    """Loop must execute at least once before checking exit."""
    responses = []
    answer, turns = run_agent_loop(responses, max_turns=5)
    assert turns >= 1, "Loop should always execute at least once"


def test_turn_counter_matches_response_count():
    """Counter must increment for every response processed."""
    responses = ["tool_call"] * 4 + ["end_turn"]
    answer, turns = run_agent_loop(responses, max_turns=10)
    assert turns == 5, f"5 responses processed, expected 5 turns, got {turns}"
```

**Run in CI:**
```bash
pytest tests/test_loop_termination.py -v
```

**Expected Token Savings:** Tests catch off-by-one and wrong-variable bugs before deployment — prevents production incidents where a stuck loop burns thousands of tokens. Zero runtime token cost for the fix.

**Environment:** Any Python agent codebase with pytest; critical to have before shipping any agentic loop to production.

---

## Option 6 — Loop budget with exponential backoff on repeated tool calls

**Track consecutive identical tool calls. Back off exponentially and reduce the remaining budget with each repeat — loops become self-limiting.**

```python
import asyncio
import hashlib
import json
import anthropic

client = anthropic.AsyncAnthropic()

MAX_TURNS      = 20
REPEAT_PENALTY = 2   # each repeated call costs this many extra turns from budget


def call_hash(tool_name: str, tool_input: dict) -> str:
    return hashlib.sha256(
        json.dumps({"name": tool_name, "input": tool_input}, sort_keys=True).encode()
    ).hexdigest()[:10]


SEARCH_TOOL = {
    "name": "search",
    "description": "Search for information.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


async def run_agent(user_message: str) -> str:
    messages     = [{"role": "user", "content": user_message}]
    budget       = MAX_TURNS
    call_history: dict[str, int] = {}   # hash → count

    while budget > 0:
        print(f"  Budget remaining: {budget}/{MAX_TURNS}")
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[SEARCH_TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tc = next(b for b in response.content if b.type == "tool_use")
        messages.append({"role": "assistant", "content": response.content})

        sig = call_hash(tc.name, tc.input)
        repeat_count = call_history.get(sig, 0)
        call_history[sig] = repeat_count + 1

        if repeat_count > 0:
            # Penalise repeated calls by draining more budget
            penalty = REPEAT_PENALTY * repeat_count
            budget -= penalty + 1
            backoff = min(2.0 ** repeat_count, 8.0)
            print(f"  Repeated call detected ({repeat_count}×) — penalty={penalty}, backoff={backoff:.1f}s")
            await asyncio.sleep(backoff)
            tool_result = json.dumps({
                "result": f"[same result as before — {repeat_count} repeats]",
                "warning": f"This exact query was run {repeat_count} time(s) before. Try a different approach.",
            })
        else:
            budget -= 1
            tool_result = f"Search results for '{tc.input['query']}': [finding 1, finding 2]"

        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": tool_result}],
        })

    # Budget exhausted — get final answer
    messages.append({"role": "user", "content": "[Budget exhausted. Give your best answer now.]"})
    final = await client.messages.create(model="claude-sonnet-4-6", max_tokens=512, messages=messages)
    return next(b.text for b in final.content if hasattr(b, "text"))


print(asyncio.run(run_agent("Research solar energy storage solutions.")))
```

**Expected Token Savings:** Exponential penalty makes repeat-loops self-limiting — a loop that would run 20 identical calls is stopped after 4–5 due to budget depletion, saving ~75% of repeat-call tokens.

**Environment:** asyncio agents where loops degenerate into repeated searches; Python 3.10+.

---

## Comparison

| Option | Exit Trigger | Detects Stale Loops | Extra Complexity | Best For |
|--------|-------------|--------------------|-----------------|----|
| 1. Turn counter | Count ceiling | No | Very Low | All agents |
| 2. `task_complete` tool | Model signals done | No | Low | Research agents |
| 3. Result accumulator | Target count reached | No | Low | Collection tasks |
| 4. State fingerprint | No state change | Yes | Medium | Search/retrieval |
| 5. Pytest termination | CI gate | N/A | Low | Any codebase |
| 6. Budget + penalties | Adaptive depletion | Yes (implicit) | Medium | Repeat-prone loops |

**Recommended path:** Apply Option 1 (turn counter) universally as the baseline safety net. Add Option 2 (`task_complete` tool) for research agents to enable early exit. Use Option 4 (state fingerprint) or Option 6 (budget penalties) when the agent is known to generate stuck search loops.
