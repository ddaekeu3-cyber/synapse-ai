---
layout: solution
title: "Agent doesn't use extended thinking for complex reasoning"
category: context-window
description: "Agent uses standard claude-sonnet for multi-step math, code planning, and logical inference tasks where extended thinking would produce significantly more accurate results at lower total cost than multiple clarification rounds."
tags: [extended-thinking, reasoning, context-window, claude-sonnet, accuracy, complex-tasks]
---

## Symptom

The agent produces subtly wrong answers on multi-step problems — correct intermediate steps but wrong final results, or correct conclusions reached via flawed logic. Users send follow-up corrections, triggering 3–5 additional turns to arrive at the right answer. The agent was using `max_tokens=1024` with no thinking budget, forcing the model to compress its reasoning into the visible response.

## Root Cause

Standard Claude models do their reasoning in the same token stream as the response. For simple questions this is fine; for complex multi-step problems (constraint satisfaction, code architecture, mathematical proof), the model cannot explore alternative approaches before committing to one. Extended thinking gives the model a private scratchpad — invisible to the user but billable as input tokens — where it can work through the problem before producing the final answer. Without it, complex tasks produce first-draft reasoning that lacks backtracking and verification.

---

## Option 1 — Enable extended thinking for identified complex task types

**Classify the request complexity and enable thinking only when the task warrants it.**

```python
import anthropic

client = anthropic.Anthropic()

# Tasks that benefit from extended thinking
COMPLEX_PATTERNS = [
    "step by step", "prove", "derive", "algorithm", "architecture",
    "optimize", "debug", "compare", "tradeoffs", "design pattern",
    "mathematical", "logic puzzle", "constraint", "systematic",
]


def is_complex(prompt: str) -> bool:
    lower = prompt.lower()
    return any(p in lower for p in COMPLEX_PATTERNS) or len(prompt.split()) > 100


def ask(prompt: str) -> str:
    use_thinking = is_complex(prompt)
    print(f"  Extended thinking: {'ON' if use_thinking else 'OFF'}")

    if use_thinking:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16_000,
            thinking={
                "type":         "enabled",
                "budget_tokens": 10_000,   # scratchpad budget
            },
            messages=[{"role": "user", "content": prompt}],
        )
    else:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

    # Extract only text blocks (not thinking blocks) for the user
    return "\n".join(
        b.text for b in response.content
        if hasattr(b, "text") and getattr(b, "type", None) == "text"
    )


# Simple task — no thinking needed
simple = ask("What is the capital of Japan?")
print(f"Simple: {simple}\n")

# Complex task — thinking enabled
complex_q = ask(
    "Design a Python class hierarchy for a plugin system where plugins can "
    "depend on each other, and implement topological sort for load order. "
    "Show the algorithm step by step and handle circular dependency detection."
)
print(f"Complex (first 200 chars): {complex_q[:200]}\n")
```

**Expected Token Savings:** Extended thinking resolves complex problems in one pass instead of 3–5 clarification rounds. Each clarification round costs ~500–2,000 tokens; thinking budget of 10,000 tokens is cheaper than 3 retry rounds at ~1,500 tokens each.

**Environment:** Claude Sonnet 4.6 with extended thinking support; `max_tokens` must be ≥ `budget_tokens + expected_response_tokens`.

---

## Option 2 — Streaming extended thinking with visible progress

**Stream the thinking process for long-running tasks so the user sees progress instead of waiting in silence.**

```python
import anthropic

client = anthropic.Anthropic()


def ask_with_streaming_thinking(prompt: str) -> str:
    """Stream extended thinking; show thinking progress, return only final answer."""
    thinking_chars = 0
    final_text     = []

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=8_000,
        thinking={
            "type":          "enabled",
            "budget_tokens": 5_000,
        },
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        current_block_type = None

        for event in stream:
            event_type = type(event).__name__

            if event_type == "RawContentBlockStartEvent":
                block = event.content_block
                current_block_type = getattr(block, "type", None)
                if current_block_type == "thinking":
                    print("  [Thinking", end="", flush=True)

            elif event_type == "RawContentBlockDeltaEvent":
                delta = event.delta
                if getattr(delta, "type", None) == "thinking_delta":
                    thinking_chars += len(getattr(delta, "thinking", ""))
                    print(".", end="", flush=True)   # progress dots
                elif getattr(delta, "type", None) == "text_delta":
                    final_text.append(getattr(delta, "text", ""))

            elif event_type == "RawContentBlockStopEvent":
                if current_block_type == "thinking":
                    print(f" {thinking_chars} chars]")

    return "".join(final_text)


result = ask_with_streaming_thinking(
    "Solve this step by step: A train leaves Station A at 60 mph. "
    "Another leaves Station B (200 miles away) at 80 mph toward Station A. "
    "A bird flies back and forth between them at 100 mph until they meet. "
    "How far does the bird fly total?"
)
print(f"\nAnswer: {result}")
```

**Expected Token Savings:** Streaming shows real-time progress — reduces user impatience that leads to premature cancellation and re-submission. Fewer duplicate requests means lower total API spend.

**Environment:** Interactive agents; streaming requires `anthropic>=0.25`; thinking events are yielded alongside text events.

---

## Option 3 — Thinking budget calibration based on task complexity score

**Assign a thinking budget proportional to estimated task complexity rather than a fixed value.**

```python
import anthropic

client = anthropic.Anthropic()


def estimate_complexity(prompt: str) -> int:
    """Return estimated thinking budget in tokens (1,024–20,000)."""
    words  = len(prompt.split())
    score  = 0

    # Word count contribution
    score += min(words // 20, 10)

    # Keyword contributions
    keywords = {
        "prove":        10, "derive":    10, "algorithm":  8,
        "architecture": 8,  "optimize":  7,  "debug":      6,
        "tradeoffs":    6,  "compare":   4,  "explain":    2,
        "list":         1,  "what is":   0,  "define":     1,
    }
    lower = prompt.lower()
    for kw, weight in keywords.items():
        if kw in lower:
            score += weight

    # Multi-part questions
    score += lower.count("?") * 2
    score += lower.count(" and ") * 1

    # Map score to budget (1,024 minimum required by API)
    if score <= 5:
        return 1_024    # minimal thinking
    elif score <= 15:
        return 4_000    # standard reasoning
    elif score <= 25:
        return 8_000    # complex reasoning
    else:
        return 16_000   # deep analysis


def ask_calibrated(prompt: str) -> str:
    budget = estimate_complexity(prompt)
    print(f"  Thinking budget: {budget:,} tokens (score={estimate_complexity(prompt)})")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=budget + 2_000,   # thinking + response headroom
        thinking={"type": "enabled", "budget_tokens": budget},
        messages=[{"role": "user", "content": prompt}],
    )

    return "\n".join(
        b.text for b in response.content
        if hasattr(b, "text") and getattr(b, "type", None) == "text"
    )


tasks = [
    "What is Python?",
    "Explain the difference between TCP and UDP.",
    "Design and implement a thread-safe LRU cache in Python with O(1) get/put. "
    "Prove the time complexity of each operation and handle edge cases.",
]

for task in tasks:
    print(f"Q: {task[:60]}")
    result = ask_calibrated(task)
    print(f"A: {result[:100]}\n")
```

**Expected Token Savings:** Budget calibration avoids paying 16,000 thinking tokens for a simple question while ensuring complex questions get enough scratchpad space to avoid retry rounds. Saves 50–80% of thinking token spend vs. a fixed large budget.

**Environment:** Mixed-complexity agent workloads; calibration function should be tuned empirically for your specific task distribution.

---

## Option 4 — Thinking for tool-use planning (decide which tools to call)

**Use extended thinking to let the model plan its tool-calling strategy before making any tool calls.**

```python
import json
import anthropic

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "search_web",
        "description": "Search the web for current information.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "query_database",
        "description": "Query the internal database for historical records.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    },
    {
        "name": "run_calculation",
        "description": "Execute a mathematical calculation.",
        "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
    },
]


def execute_tool(name: str, args: dict) -> str:
    if name == "search_web":
        return json.dumps({"results": [f"Web result for '{args['query']}'"]})
    if name == "query_database":
        return json.dumps({"rows": [{"id": 1, "value": 42}]})
    if name == "run_calculation":
        try:
            return json.dumps({"result": eval(args["expression"], {"__builtins__": {}})})
        except Exception as e:
            return json.dumps({"error": str(e)})
    return json.dumps({"error": "unknown tool"})


def run_thinking_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8_000,
            thinking={"type": "enabled", "budget_tokens": 4_000},
            tools=TOOLS,
            messages=messages,
        )

        # Collect tool calls from response
        tool_calls = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b.text for b in response.content if hasattr(b, "text") and b.type == "text"]

        if response.stop_reason == "end_turn":
            return "\n".join(text_blocks)

        if not tool_calls:
            return "\n".join(text_blocks)

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tc in tool_calls:
            result = execute_tool(tc.name, tc.input)
            print(f"  Tool: {tc.name}({tc.input}) → {result[:60]}")
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": tc.id,
                "content":     result,
            })

        messages.append({"role": "user", "content": tool_results})


result = run_thinking_agent(
    "I need to know: 1) today's price of Bitcoin, 2) our total revenue from last month in the database, "
    "and 3) what percentage of last month's revenue could buy a whole Bitcoin at today's price. "
    "Plan which tools to use and in what order, then execute."
)
print(f"\nFinal answer: {result[:300]}")
```

**Expected Token Savings:** Thinking-informed tool planning executes the optimal tool sequence on the first attempt — avoids 2–3 extra turns of suboptimal tool ordering that each require another LLM call (~800 tokens each).

**Environment:** Multi-tool agents with complex task decomposition requirements; extended thinking is most valuable when the optimal tool sequence is non-obvious.

---

## Option 5 — Compare answers with and without thinking for accuracy benchmarking

**Run the same complex questions with and without thinking to measure accuracy improvement for your specific use case.**

```python
import json
import anthropic

client = anthropic.Anthropic()

TEST_CASES = [
    {
        "question": "If a snail moves at 0.03 mph and needs to cross a 10-foot garden, "
                    "how many minutes will it take? Show your work.",
        "expected_contains": ["189", "190"],   # ~189.4 minutes
    },
    {
        "question": "What is wrong with this Python code? "
                    "`lst = [1,2,3]; for i in lst: lst.remove(i)` "
                    "What will `lst` contain after execution?",
        "expected_contains": ["[2]", "2"],   # [2] due to mutation during iteration
    },
]


def call(prompt: str, thinking: bool, budget: int = 5_000) -> str:
    if thinking:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=budget + 2_000,
            thinking={"type": "enabled", "budget_tokens": budget},
            messages=[{"role": "user", "content": prompt}],
        )
    else:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1_024,
            messages=[{"role": "user", "content": prompt}],
        )
    return "\n".join(
        b.text for b in resp.content
        if hasattr(b, "text") and getattr(b, "type", None) == "text"
    )


def check(answer: str, expected: list[str]) -> bool:
    return any(e in answer for e in expected)


print("=== Thinking vs No-Thinking Accuracy Benchmark ===\n")
results = {"thinking": 0, "no_thinking": 0}

for case in TEST_CASES:
    q = case["question"]
    expected = case["expected_contains"]

    ans_think   = call(q, thinking=True)
    ans_nothink = call(q, thinking=False)

    ok_think   = check(ans_think,   expected)
    ok_nothink = check(ans_nothink, expected)

    results["thinking"]    += ok_think
    results["no_thinking"] += ok_nothink

    print(f"Q: {q[:60]}")
    print(f"  Thinking:    {'✓' if ok_think   else '✗'} {ans_think[:80]}")
    print(f"  No thinking: {'✓' if ok_nothink else '✗'} {ans_nothink[:80]}\n")

print(f"Score — Thinking: {results['thinking']}/{len(TEST_CASES)} | "
      f"No thinking: {results['no_thinking']}/{len(TEST_CASES)}")
```

**Expected Token Savings:** Benchmarking identifies which task types actually benefit from thinking — prevents paying thinking costs on tasks where the accuracy improvement is negligible, saving thinking budget for where it matters.

**Environment:** Development/staging; run benchmark quarterly as your task distribution evolves.

---

## Option 6 — Cost-gated thinking: only use thinking when expected accuracy gain justifies cost

**Calculate the expected value of using thinking vs. standard mode based on task type and retry probability.**

```python
import anthropic

client = anthropic.Anthropic()

# Cost per token (approximate, USD)
INPUT_COST_PER_M  = 3.00    # claude-sonnet-4-6 input
OUTPUT_COST_PER_M = 15.00   # claude-sonnet-4-6 output

# Accuracy improvement from extended thinking, by task type
THINKING_ACCURACY_GAIN = {
    "math":        0.35,   # 35% fewer wrong answers
    "code_review": 0.25,
    "planning":    0.20,
    "factual":     0.05,
    "simple":      0.02,
}

# Cost of a retry round (avg tokens per round)
RETRY_ROUND_TOKENS = 1_500
RETRY_PROBABILITY  = {
    "math":        0.40,   # 40% of math questions need a correction round without thinking
    "code_review": 0.30,
    "planning":    0.25,
    "factual":     0.10,
    "simple":      0.05,
}


def should_use_thinking(task_type: str, thinking_budget: int = 8_000) -> bool:
    """Return True if extended thinking is cost-effective for this task type."""
    thinking_extra_cost = (thinking_budget / 1_000_000) * INPUT_COST_PER_M

    accuracy_gain = THINKING_ACCURACY_GAIN.get(task_type, 0.1)
    retry_prob    = RETRY_PROBABILITY.get(task_type, 0.2)

    # Expected savings from avoiding a retry round
    retry_cost    = (RETRY_ROUND_TOKENS / 1_000_000) * (INPUT_COST_PER_M + OUTPUT_COST_PER_M)
    expected_saving = accuracy_gain * retry_prob * retry_cost

    worthwhile = expected_saving > thinking_extra_cost
    print(f"  [{task_type}] thinking_cost=${thinking_extra_cost:.5f} "
          f"expected_saving=${expected_saving:.5f} → {'USE' if worthwhile else 'SKIP'}")
    return worthwhile


def ask(prompt: str, task_type: str) -> str:
    use_thinking = should_use_thinking(task_type)

    if use_thinking:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=10_000,
            thinking={"type": "enabled", "budget_tokens": 8_000},
            messages=[{"role": "user", "content": prompt}],
        )
    else:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1_024,
            messages=[{"role": "user", "content": prompt}],
        )

    return "\n".join(
        b.text for b in response.content
        if hasattr(b, "text") and getattr(b, "type", None) == "text"
    )


# Math: worthwhile (high retry probability)
result = ask(
    "A cyclist averages 15 mph for the first half of a trip and 10 mph for the second half. "
    "What is their average speed for the entire trip?",
    task_type="math",
)
print(f"Answer: {result[:120]}\n")

# Simple: not worthwhile
result = ask("What is the capital of Germany?", task_type="simple")
print(f"Answer: {result}\n")
```

**Expected Token Savings:** Cost-gated thinking avoids paying for thinking on simple tasks while ensuring complex tasks get the scratchpad they need — typically saves 60–70% of thinking token spend vs. always-on thinking, while maintaining accuracy gains where they matter.

**Environment:** Mixed-workload agents; calibrate the accuracy and retry probability constants using your actual production data.

---

## Comparison

| Option | Thinking Trigger | Budget Strategy | Streaming | Best For |
|--------|-----------------|----------------|-----------|----------|
| 1. Keyword classifier | Task keywords | Fixed | No | General agents |
| 2. Streaming thinking | All complex tasks | Fixed | Yes | Interactive UI |
| 3. Calibrated budget | Complexity score | Dynamic | No | Varied complexity |
| 4. Tool-use planning | Multi-tool tasks | Fixed | No | Planning agents |
| 5. A/B benchmark | Dev measurement | Fixed | No | Calibration |
| 6. Cost-gated | Expected value | Fixed | No | Cost-sensitive |

**Recommended path:** Start with Option 1 (keyword classifier) — a simple heuristic that applies thinking to known-complex patterns. Measure accuracy improvement over one week. Then use Option 6 (cost-gated) to set the exact budget that maximises ROI for your task distribution.

**Note:** Extended thinking requires `claude-sonnet-4-6` or `claude-opus-4-6`; `budget_tokens` minimum is 1,024; `max_tokens` must exceed `budget_tokens`.
