---
layout: solution
title: "Agent loops after hitting max_tokens mid-task"
category: loop-stuck
description: "Agent receives a truncated response due to max_tokens being too low, then re-attempts the same request in an infinite loop instead of adapting."
tags: [loop-stuck, max-tokens, truncation, stop-reason, agentic]
---

## Symptom

The agent sends a request and receives a response with `stop_reason: "max_tokens"`. Instead of handling the truncation — by continuing the generation, splitting the task, or raising an error — it re-submits the same request. The truncated assistant turn is appended to history, the next request is identical, and the cycle repeats until the session times out or the user intervenes.

```
Turn 1: response.stop_reason == "max_tokens"  (response cut off mid-sentence)
Turn 2: [same request again] → stop_reason == "max_tokens"
Turn 3: [same request again] → stop_reason == "max_tokens"
...
```

## Root Cause

The agent's main loop only checks `stop_reason == "tool_use"` to dispatch tool calls and `stop_reason == "end_turn"` to return the final answer. The `"max_tokens"` case falls through to neither branch. The loop continues, appending the truncated assistant message to history and retrying without changing `max_tokens`, the prompt, or the task structure.

## Fix

Explicitly check `stop_reason == "max_tokens"` and handle it with one of: continuation, task splitting, dynamic token budget increase, or graceful truncation acknowledgment.

---

### Option 1 — Continuation: append the truncated turn and ask the model to continue

```python
import anthropic

client = anthropic.Anthropic()

MAX_TOKENS_PER_CALL = 256      # intentionally low to demonstrate truncation
MAX_CONTINUATIONS   = 4        # prevent infinite continuation chains

def run_with_continuation(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    full_response_parts: list[str] = []
    continuations = 0

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=MAX_TOKENS_PER_CALL,
            messages=messages,
        )

        # Collect text from this response
        text = next((b.text for b in response.content if hasattr(b, "text")), "")
        full_response_parts.append(text)

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "max_tokens":
            continuations += 1
            print(f"[TRUNCATED] continuation {continuations}/{MAX_CONTINUATIONS}")

            if continuations >= MAX_CONTINUATIONS:
                print("[ABORT] max continuations reached — returning partial response")
                full_response_parts.append("\n[Response truncated: continuation limit reached]")
                break

            # Append the truncated assistant turn and prompt to continue
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": "Continue exactly from where you left off."})
            continue

        # Any other stop reason (tool_use, etc.) — handle as needed
        break

    return "".join(full_response_parts)

result = run_with_continuation(
    "Write a detailed technical explanation of how the Anthropic API handles "
    "streaming, token counting, and context windows. Include code examples for each."
)
print(result)
print(f"\nTotal length: {len(result)} chars")
```

**Expected Token Savings:** 0 direct savings; prevents infinite loops which would waste tokens indefinitely; continuation approach recovers the full response without raising an error.

**Environment:** Any task requiring long-form output; set `MAX_CONTINUATIONS` to limit worst-case cost.

---

### Option 2 — Dynamic max_tokens increase on truncation

```python
import anthropic

client = anthropic.Anthropic()

def run_with_dynamic_budget(
    user_message: str,
    initial_max_tokens: int = 256,
    max_budget: int = 4096,
    multiplier: float = 2.0,
) -> str:
    """
    On truncation, double the max_tokens budget and retry from scratch.
    This is appropriate when the task cannot be split and the full answer is needed.
    """
    current_budget = initial_max_tokens
    attempt = 0

    while current_budget <= max_budget:
        attempt += 1
        print(f"[ATTEMPT {attempt}] max_tokens={current_budget}")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=current_budget,
            messages=[{"role": "user", "content": user_message}],
        )

        if response.stop_reason == "end_turn":
            text = next(b.text for b in response.content if hasattr(b, "text"))
            print(f"[SUCCESS] completed in {response.usage.output_tokens} tokens")
            return text

        if response.stop_reason == "max_tokens":
            current_budget = min(int(current_budget * multiplier), max_budget)
            print(f"[TRUNCATED] budget insufficient — increasing to {current_budget}")
            continue

        # Unexpected stop reason
        break

    # If we exhausted the budget, return the best we have
    text = next((b.text for b in response.content if hasattr(b, "text")), "")
    return text + "\n[Warning: response may be incomplete]"

result = run_with_dynamic_budget(
    "Explain async/await in Python with three complete code examples.",
    initial_max_tokens=128,
    max_budget=2048,
)
print(result)
```

**Expected Token Savings:** Avoids paying for failed truncated attempts in a retry loop; final call uses the minimum budget that succeeds.

**Environment:** Tasks with unpredictable output length (code generation, document drafting); the doubling strategy converges quickly.

---

### Option 3 — Task splitter: break long tasks into bounded subtasks

```python
import anthropic
import json

client = anthropic.Anthropic()

SUBTASK_MAX_TOKENS = 512

def split_task_with_llm(original_task: str, max_subtasks: int = 4) -> list[str]:
    """Ask the model to decompose a large task into small, self-contained subtasks."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Decompose this task into at most {max_subtasks} small, self-contained subtasks "
                f"that each produce a short output (under 200 words). "
                f"Return JSON: {{\"subtasks\": [\"subtask 1\", ...]}}\n\nTask: {original_task}"
            ),
        }],
    )
    raw = response.content[0].text
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    parsed = json.loads(raw[start:end])
    subtasks = parsed.get("subtasks", [original_task])
    print(f"[SPLIT] Task decomposed into {len(subtasks)} subtasks")
    return subtasks

def execute_subtask(subtask: str, context: str = "") -> str:
    """Execute a single bounded subtask, handling truncation with continuation."""
    messages = [{"role": "user", "content": f"{context}\n\nSubtask: {subtask}".strip()}]

    for attempt in range(3):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=SUBTASK_MAX_TOKENS,
            messages=messages,
        )
        text = next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "end_turn":
            return text

        if response.stop_reason == "max_tokens":
            # Single continuation attempt
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": "Continue and finish."})
            continue

        return text

    return text + " [truncated]"

def run_split_agent(user_message: str) -> str:
    subtasks = split_task_with_llm(user_message)

    results = []
    for i, subtask in enumerate(subtasks):
        print(f"[SUBTASK {i+1}/{len(subtasks)}] {subtask[:60]}")
        context = f"Prior results:\n" + "\n".join(results[-2:]) if results else ""
        result = execute_subtask(subtask, context)
        results.append(f"**{subtask}**\n{result}")

    return "\n\n".join(results)

answer = run_split_agent(
    "Explain Python decorators: what they are, why they are useful, "
    "how to write one, and a real-world example from web frameworks."
)
print(answer)
```

**Expected Token Savings:** 20–40% savings by eliminating failed truncated calls; decomposition ensures each subtask fits comfortably within the budget.

**Environment:** Complex analytical or writing tasks; decomposition adds ~50 tokens overhead but prevents unlimited retry cost.

---

### Option 4 — Truncation guard with stop_reason enforcement

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

class MaxTokensError(Exception):
    """Raised when a response is truncated and continuation is not configured."""
    def __init__(self, partial_text: str, tokens_used: int):
        self.partial_text = partial_text
        self.tokens_used  = tokens_used
        super().__init__(f"Response truncated after {tokens_used} output tokens")

@dataclass
class AgentConfig:
    max_tokens: int = 1024
    allow_truncation: bool = False   # if False, raise on max_tokens stop
    truncation_message: str = "[Response was truncated due to token limit]"

def safe_completion(
    messages: list[dict],
    config: AgentConfig,
    system: str = "",
) -> str:
    """
    A completion call that explicitly guards against max_tokens loops.
    Raises MaxTokensError unless allow_truncation=True.
    """
    kwargs: dict = dict(
        model="claude-haiku-4-5-20251001",
        max_tokens=config.max_tokens,
        messages=messages,
    )
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    text = next((b.text for b in response.content if hasattr(b, "text")), "")

    if response.stop_reason == "max_tokens":
        if config.allow_truncation:
            print(f"[WARN] Response truncated at {config.max_tokens} tokens")
            return text + f"\n\n{config.truncation_message}"
        else:
            raise MaxTokensError(text, response.usage.output_tokens)

    return text

def run_agent(user_message: str) -> str:
    config = AgentConfig(max_tokens=256, allow_truncation=False)
    messages = [{"role": "user", "content": user_message}]

    try:
        return safe_completion(messages, config)
    except MaxTokensError as e:
        print(f"[ERROR] {e}")
        # Retry with a larger budget
        print("[RETRY] Increasing max_tokens to 1024")
        config_retry = AgentConfig(max_tokens=1024, allow_truncation=True)
        return safe_completion(messages, config_retry)

result = run_agent(
    "Write a Python function that parses a JWT token and validates its signature."
)
print(result)
```

**Expected Token Savings:** Converts silent truncation loops into explicit errors; fail-fast behavior prevents indefinite token burn on a task that will never succeed at the current budget.

**Environment:** Production agents where silent truncation is unacceptable; integrate `MaxTokensError` into your error-monitoring pipeline.

---

### Option 5 — Streaming with early truncation detection

```python
import anthropic

client = anthropic.Anthropic()

def stream_with_truncation_guard(
    user_message: str,
    max_tokens: int = 512,
    continuation_budget: int = 1024,
) -> str:
    """
    Stream the response. If the stream ends with stop_reason=max_tokens,
    immediately start a continuation stream rather than re-running the full call.
    """
    messages = [{"role": "user", "content": user_message}]
    full_text_parts: list[str] = []
    turn = 0

    while turn < 5:
        turn += 1
        current_tokens = max_tokens if turn == 1 else continuation_budget
        accumulated = []
        stop_reason = None

        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=current_tokens,
            messages=messages,
        ) as stream:
            for text_chunk in stream.text_stream:
                accumulated.append(text_chunk)
                print(text_chunk, end="", flush=True)

            stop_reason = stream.get_final_message().stop_reason

        chunk_text = "".join(accumulated)
        full_text_parts.append(chunk_text)

        if stop_reason == "end_turn":
            print()  # newline after streaming
            break

        if stop_reason == "max_tokens":
            print(f"\n[STREAM] Truncated at turn {turn}, continuing...")
            messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": chunk_text}],
            })
            messages.append({"role": "user", "content": "Continue from where you left off."})
            continue

        break  # other stop reasons

    return "".join(full_text_parts)

result = stream_with_truncation_guard(
    "List 10 Python best practices with a one-sentence explanation for each.",
    max_tokens=200,         # too small — will truncate
    continuation_budget=512,
)
print(f"\n\nFull length: {len(result)} chars")
```

**Expected Token Savings:** Streaming detects truncation at the earliest possible point; continuation starts immediately without re-sending the full conversation prefix; saves re-sending input tokens on each retry.

**Environment:** Interactive agents where streaming output is shown to users; continuation is seamless from the user's perspective.

---

### Option 6 — Adaptive max_tokens based on task complexity classifier

```python
import anthropic
import re

client = anthropic.Anthropic()

# Complexity → token budget mapping
COMPLEXITY_BUDGETS = {
    "simple":   128,
    "medium":   512,
    "complex": 2048,
    "verbose": 4096,
}

COMPLEXITY_PATTERNS = {
    "verbose": re.compile(r"\b(write a (detailed|comprehensive|full)|list \d+ |step.by.step|complete guide)\b", re.I),
    "complex": re.compile(r"\b(explain|implement|design|architect|compare|analyze)\b", re.I),
    "medium":  re.compile(r"\b(describe|summarize|what is|how does)\b", re.I),
}

def classify_complexity(user_message: str) -> str:
    for level, pattern in COMPLEXITY_PATTERNS.items():
        if pattern.search(user_message):
            return level
    return "simple"

def run_adaptive_agent(user_message: str) -> str:
    complexity = classify_complexity(user_message)
    max_tokens = COMPLEXITY_BUDGETS[complexity]
    print(f"[COMPLEXITY] '{complexity}' → max_tokens={max_tokens}")

    messages = [{"role": "user", "content": user_message}]

    for attempt in range(3):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "max_tokens":
            # Upgrade complexity tier
            tiers = list(COMPLEXITY_BUDGETS.keys())
            current_idx = tiers.index(complexity)
            if current_idx < len(tiers) - 1:
                complexity = tiers[current_idx + 1]
                max_tokens = COMPLEXITY_BUDGETS[complexity]
                print(f"[UPGRADE] Upgrading to '{complexity}' → max_tokens={max_tokens}")
            else:
                # At max tier — use continuation
                text = next((b.text for b in response.content if hasattr(b, "text")), "")
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": "Continue."})
                continue

    text = next((b.text for b in response.content if hasattr(b, "text")), "")
    return text

# Test different complexity levels
for query in [
    "What is asyncio?",
    "Explain how Python's GIL works.",
    "Write a detailed guide to building a REST API with FastAPI including auth.",
]:
    print(f"\nQuery: {query[:60]}")
    result = run_adaptive_agent(query)
    print(f"Result length: {len(result)} chars")
    print(result[:120] + "..." if len(result) > 120 else result)
```

**Expected Token Savings:** 30–60% savings by starting with a tight budget for simple tasks and only escalating when truncation actually occurs; prevents over-provisioning tokens for every call.

**Environment:** Mixed-workload agents handling both quick lookups and complex generation; classifier adds ~0 latency and prevents the most common truncation scenarios proactively.

---

## Comparison

| Option | Truncation Response | Re-sends Input Tokens | Extra API Calls | Best For |
|--------|--------------------|-----------------------|----------------|---------|
| 1 — Continuation | Append + "continue" | No | Yes (per chunk) | Long-form writing |
| 2 — Dynamic budget | Retry from scratch | Yes | Yes (per attempt) | Short tasks |
| 3 — Task splitter | Decompose + split | No | Yes (decompose) | Complex multi-part tasks |
| 4 — Truncation guard | Raise + retry | Yes (once) | Yes (on error) | Production safety |
| 5 — Streaming | Immediate continuation | No | Minimal | Interactive agents |
| 6 — Adaptive classifier | Tier upgrade | Yes | Yes (on upgrade) | Mixed workloads |

**Recommended default:** Option 1 (continuation) for general use — it never re-sends the full prompt and converges quickly. Use Option 4 (truncation guard) for production agents where silent truncation must be surfaced as an error.
