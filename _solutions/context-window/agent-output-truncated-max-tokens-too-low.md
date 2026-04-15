---
layout: solution
title: "Agent Output Truncated Mid-Sentence — max_tokens Set Too Low"
category: context-window
description: "Agent response cuts off mid-sentence or mid-code-block. stop_reason is 'max_tokens' instead of 'end_turn'. Response is incomplete but agent doesn't realize it or retry with continuation."
tags: [max-tokens, truncation, output, stop-reason, continuation, incomplete]
---

# Agent Output Truncated Mid-Sentence — max_tokens Set Too Low

## Symptom
- Response ends abruptly mid-sentence: "The function takes three parameters and retur"
- Code block opened with ` ``` ` but never closed — syntax invalid
- `response.stop_reason == "max_tokens"` instead of `"end_turn"`
- Agent doesn't notice truncation and treats incomplete output as complete
- JSON response cut off mid-object: `{"key": "val` — unparseable

## Root Cause
`max_tokens` caps the output length. When output reaches the limit, generation stops immediately — even mid-word. The caller is responsible for checking `stop_reason` and requesting continuation if needed. Many implementations ignore `stop_reason` and use the truncated output directly.

## Fix

### Option 1: Always check stop_reason and continue if truncated

```python
import anthropic

client = anthropic.Anthropic()

def complete_with_continuation(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    max_continuations: int = 3
) -> str:
    """Complete response, automatically continuing if truncated"""
    full_response = ""
    current_messages = list(messages)

    for attempt in range(max_continuations + 1):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=current_messages
        )

        chunk = response.content[0].text
        full_response += chunk

        if response.stop_reason == "end_turn":
            break  # Complete response

        if response.stop_reason == "max_tokens":
            if attempt == max_continuations:
                print(f"Warning: Response still truncated after {max_continuations} continuations")
                break

            # Continue from where we left off
            current_messages = current_messages + [
                {"role": "assistant", "content": chunk},
                {"role": "user", "content": "Please continue exactly where you left off."}
            ]
            print(f"Response truncated, requesting continuation {attempt + 1}/{max_continuations}...")
        else:
            break  # tool_use or other stop reason

    return full_response
```

### Option 2: Set max_tokens high enough for expected output

```python
# Rough token estimates for common output types
MAX_TOKENS_BY_TASK = {
    "short_answer": 256,
    "explanation": 1024,
    "code_function": 2048,
    "code_module": 4096,
    "essay": 2048,
    "detailed_analysis": 4096,
    "full_codebase_file": 8192,
}

# Claude model output limits (as of 2025)
MODEL_MAX_OUTPUT = {
    "claude-opus-4-6": 32000,
    "claude-sonnet-4-6": 16000,
    "claude-haiku-4-5-20251001": 8096,
    "claude-3-5-sonnet-20241022": 8192,
}

def get_safe_max_tokens(task_type: str, model: str) -> int:
    suggested = MAX_TOKENS_BY_TASK.get(task_type, 2048)
    model_limit = MODEL_MAX_OUTPUT.get(model, 8096)
    return min(suggested, model_limit)
```

### Option 3: Detect truncation in output programmatically

```python
def is_truncated(text: str, stop_reason: str) -> bool:
    """Detect truncation even when stop_reason isn't checked"""
    if stop_reason == "max_tokens":
        return True

    # Check for unclosed code blocks
    if text.count("```") % 2 != 0:
        return True

    # Check for unclosed JSON
    text_stripped = text.strip()
    if text_stripped.startswith("{") and not text_stripped.endswith("}"):
        return True
    if text_stripped.startswith("[") and not text_stripped.endswith("]"):
        return True

    # Check for incomplete sentence
    if text_stripped and text_stripped[-1] not in ".!?\"'`}])":
        # Ends without sentence terminator — possibly truncated
        # (heuristic, not always accurate)
        return False  # Don't assume — check stop_reason instead

    return False

response = client.messages.create(...)
if is_truncated(response.content[0].text, response.stop_reason):
    print("WARNING: Response may be truncated. Consider increasing max_tokens.")
```

### Option 4: Use streaming to detect truncation early

```python
async def stream_with_truncation_detection(messages: list, max_tokens: int = 4096) -> str:
    """Stream response and detect truncation from stream events"""
    full_text = ""
    stop_reason = None

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=messages
    ) as stream:
        for text in stream.text_stream:
            full_text += text
            print(text, end="", flush=True)

        final_message = stream.get_final_message()
        stop_reason = final_message.stop_reason

    if stop_reason == "max_tokens":
        print(f"\n\n[WARNING: Response truncated at {max_tokens} tokens]")

    return full_text, stop_reason
```

### Option 5: Request compact output when context is large

```python
def add_output_budget_instruction(prompt: str, available_tokens: int) -> str:
    """Instruct agent to fit within token budget"""
    if available_tokens < 1000:
        return prompt + "\n\nIMPORTANT: Be extremely concise. Maximum 3 sentences."
    elif available_tokens < 2000:
        return prompt + "\n\nBe concise. Focus on the most important points only."
    else:
        return prompt

# Calculate available output tokens
input_tokens_estimate = len(" ".join(str(m) for m in messages)) // 4
model_context_limit = 200_000  # claude-sonnet-4-6
available = min(model_context_limit - input_tokens_estimate, 4096)

adjusted_prompt = add_output_budget_instruction(user_prompt, available)
```

## stop_reason Reference

| stop_reason | Meaning | Action |
|---|---|---|
| `end_turn` | Model finished naturally | Use response as-is |
| `max_tokens` | Hit output token limit | Increase max_tokens or continue |
| `stop_sequence` | Hit a stop sequence | Expected — check your sequences |
| `tool_use` | Model is calling a tool | Process tool call |

## Expected Token Savings
Truncated JSON causing re-runs: ~5,000 tokens
Checking stop_reason + continuation: ~500 tokens per continuation

## Environment
- Any agent producing long outputs: code generation, reports, analysis
- Source: direct experience, Anthropic API documentation
