---
layout: solution
title: "Agent Output Truncated Mid-Sentence — max_tokens Too Low"
category: general
description: "Agent generates a code block or long response that gets cut off mid-sentence or mid-function because max_tokens is set too low. Response ends abruptly. Agent doesn't detect the truncation. Downstream parser breaks on incomplete output."
tags: [max-tokens, truncation, stop-reason, continuation, output, incomplete]
---

# Agent Output Truncated Mid-Sentence — max_tokens Too Low

## Symptom
- Code generation stops in the middle of a function body
- Essay ends mid-paragraph with no conclusion
- JSON output is missing the closing `}` — downstream parse fails
- Agent says "Here is the full report:" then delivers only the first 3 sections
- `stop_reason` is `max_tokens` not `end_turn` — agent hit the limit, not a natural stop
- Agent doesn't mention the truncation — caller assumes response is complete

## Root Cause
`max_tokens` is set too low for the task at hand. The API enforces the limit by stopping generation, returning `stop_reason: "max_tokens"`. Unlike natural completion (`end_turn`), the model doesn't know it's about to be cut off — it cannot add "continued..." or wrap up gracefully. The caller receives a partial response with no obvious indicator of truncation unless they check `stop_reason`.

## Fix

### Option 1: Always check stop_reason and handle truncation

```python
import anthropic

client = anthropic.Anthropic()

def call_with_truncation_check(
    messages: list,
    system: str = "",
    max_tokens: int = 4096
) -> dict:
    """
    Call API and explicitly check for truncation.
    Returns response with truncation flag so caller can handle it.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        system=system,
        messages=messages,
        max_tokens=max_tokens
    )

    text = response.content[0].text
    stop_reason = response.stop_reason
    was_truncated = stop_reason == "max_tokens"

    if was_truncated:
        print(
            f"WARNING: Response truncated at {max_tokens} tokens. "
            f"Output may be incomplete. "
            f"Consider increasing max_tokens or splitting the task."
        )

    return {
        "text": text,
        "stop_reason": stop_reason,
        "truncated": was_truncated,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens
        }
    }

result = call_with_truncation_check(
    messages=[{"role": "user", "content": "Write a full report on Q4 metrics"}],
    max_tokens=1024  # Might be too low for a full report
)

if result["truncated"]:
    raise ValueError(f"Response incomplete — increase max_tokens above {1024}")
```

### Option 2: Automatic continuation when truncated

```python
async def call_with_continuation(
    messages: list,
    system: str = "",
    max_tokens: int = 4096,
    max_continuations: int = 3
) -> str:
    """
    If response is truncated, automatically continue from where it left off.
    Assembles the full response from multiple calls.
    """
    full_text = ""
    current_messages = list(messages)

    for continuation in range(max_continuations + 1):
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            system=system,
            messages=current_messages,
            max_tokens=max_tokens
        )

        chunk = response.content[0].text
        full_text += chunk

        if response.stop_reason != "max_tokens":
            # Natural completion — done
            if continuation > 0:
                print(f"Completed after {continuation} continuation(s)")
            return full_text

        if continuation == max_continuations:
            print(f"Hit max {max_continuations} continuations — returning partial")
            return full_text

        # Append response and ask to continue
        current_messages = current_messages + [
            {"role": "assistant", "content": chunk},
            {"role": "user", "content": "Continue from where you left off."}
        ]
        print(f"Continuation {continuation + 1}/{max_continuations}...")

    return full_text

# Usage:
full_report = await call_with_continuation(
    messages=[{"role": "user", "content": "Write a full implementation of X"}],
    max_tokens=2048,      # Per-call limit
    max_continuations=3   # Allow up to 3 continuations
)
```

### Option 3: Set max_tokens based on task type

```python
# Map task types to appropriate token budgets
TOKEN_BUDGETS = {
    "classification":        50,    # "positive" or "negative"
    "yes_no":                30,
    "short_answer":         200,
    "paragraph":            500,
    "summary":              400,
    "code_snippet":        1000,
    "full_function":       2000,
    "full_module":         8000,
    "essay":               3000,
    "report":              5000,
    "full_document":      20000,
}

def get_max_tokens(task_type: str, safety_margin: float = 1.5) -> int:
    """
    Get appropriate max_tokens for a task type.
    Safety margin ensures we don't cut off at exactly the expected length.
    """
    base = TOKEN_BUDGETS.get(task_type, 2048)
    return int(base * safety_margin)

# Usage:
response = client.messages.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": prompt}],
    max_tokens=get_max_tokens("full_function")  # → 3000
)
```

### Option 4: Validate structured output completeness

```python
import json
import re

def validate_completeness(text: str, expected_format: str) -> tuple[bool, str]:
    """
    Check if truncated output is structurally complete.
    Returns (is_complete, reason).
    """
    text = text.strip()

    if expected_format == "json":
        try:
            json.loads(text)
            return True, "Valid JSON"
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON (likely truncated): {e}"

    if expected_format == "python":
        # Check for common truncation indicators
        open_blocks = text.count("def ") + text.count("class ") + text.count("if ") + text.count("for ")
        # Very rough: if code ends mid-line (no newline at end), likely truncated
        if not text.endswith("\n") and not text.endswith(")") and not text.endswith(":"):
            return False, "Code appears to end mid-line"
        # Check for matching brackets
        if text.count("{") != text.count("}"):
            return False, f"Mismatched braces: {text.count('{')} open, {text.count('}')} closed"
        return True, "Code appears complete"

    if expected_format == "markdown":
        # Check for unclosed code blocks
        code_fences = text.count("```")
        if code_fences % 2 != 0:
            return False, f"Unclosed code block ({code_fences} backtick fences)"
        return True, "Markdown appears complete"

    # Generic: check if ends in mid-sentence
    ends_cleanly = text.endswith((".", "!", "?", "```", "}", "]", "\n"))
    if not ends_cleanly:
        return False, "Response ends mid-sentence"
    return True, "Response appears complete"

# After each API call:
result = call_with_truncation_check(messages, max_tokens=2048)
if result["truncated"]:
    complete, reason = validate_completeness(result["text"], "python")
    if not complete:
        print(f"Output incomplete: {reason}")
        # Trigger continuation or error handling
```

### Option 5: Use extended thinking for complex tasks that need more tokens

```python
async def call_with_extended_thinking(
    prompt: str,
    thinking_budget: int = 5000,
    max_tokens: int = 16000
) -> str:
    """
    For complex tasks that need a lot of tokens, use extended thinking.
    Thinking tokens don't count against the output limit.
    """
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        thinking={
            "type": "enabled",
            "budget_tokens": thinking_budget
        },
        messages=[{"role": "user", "content": prompt}]
    )

    # Extract only the text content (not thinking blocks)
    text_blocks = [
        block.text for block in response.content
        if block.type == "text"
    ]
    return "\n".join(text_blocks)
```

### Option 6: System prompt that instructs agent to be concise

```python
def build_length_aware_system_prompt(max_tokens: int, task: str) -> str:
    """
    Add length awareness to system prompt so agent self-limits appropriately.
    """
    # Rough char budget (4 chars per token on average)
    char_budget = max_tokens * 3  # Conservative estimate

    return (
        f"You are helping with: {task}\n\n"
        f"Response constraints:\n"
        f"- Your response must fit within approximately {char_budget:,} characters\n"
        f"- If the full response would exceed this, prioritize the most important content\n"
        f"- End with a complete sentence — never leave a thought unfinished\n"
        f"- If you must abbreviate, say 'Note: abbreviated for length. Full version available on request.'\n"
        f"- NEVER let your response end mid-sentence or mid-code-block"
    )

# Agent now knows its budget and can self-manage:
system = build_length_aware_system_prompt(max_tokens=1024, task="code generation")
```

## max_tokens by Model and Task

| Task | Recommended max_tokens | Notes |
|---|---|---|
| Classification / yes/no | 50–100 | Always enough |
| Short answer | 200–500 | Adjust if answers vary |
| Code function | 1,000–3,000 | Depends on function size |
| Full module | 8,000–16,000 | Use continuation if needed |
| Report / essay | 2,000–8,000 | Check stop_reason |
| Multi-file code | 16,000+ | Consider splitting by file |

## Expected Token Savings
Truncated response requiring debug + retry + continuation: ~10,000 tokens
Correct max_tokens from the start: 0 wasted

## Environment
- All agent deployments; most critical for code generation, document creation, and structured output tasks
- Source: direct experience; max_tokens truncation is the most common silent data loss in agent pipelines
