---
layout: solution
title: "Wrong Model ID Causes 404 or Invalid Request Error"
category: config
description: "Agent fails with 404 or 'model not found' because model ID string is wrong. Common causes: outdated model names, typos, wrong API format, or using OpenAI model IDs with Anthropic SDK."
tags: [model-id, 404, config, anthropic, model-name, invalid-request]
---

# Wrong Model ID Causes 404 or Invalid Request Error

## Symptom
- `Error: 404 {"type":"error","error":{"type":"not_found_error","message":"model: ..."}}`
- `anthropic.BadRequestError: Invalid model`
- `openai.NotFoundError: The model 'claude-3-opus' does not exist`
- Agent worked yesterday, fails today (model was deprecated/renamed)
- Works in one environment, fails in another (different SDK version uses different defaults)

## Root Cause
Model IDs must match exactly. Anthropic regularly releases new model versions with updated IDs. Old aliases get deprecated. Common mistakes: using deprecated short names, copy-pasting OpenAI model IDs, missing the date suffix on versioned models.

## Anthropic Model IDs (Current as of 2025)

```python
# Claude 4 family (latest as of 2025)
"claude-opus-4-6"         # Most capable
"claude-sonnet-4-6"       # Balanced — recommended default
"claude-haiku-4-5-20251001"  # Fastest, lowest cost

# Claude 3.7
"claude-3-7-sonnet-20250219"

# Claude 3.5
"claude-3-5-sonnet-20241022"
"claude-3-5-haiku-20241022"

# Claude 3
"claude-3-opus-20240229"
"claude-3-sonnet-20240229"
"claude-3-haiku-20240307"
```

## Fix

### Option 1: Use the correct current model ID

```python
import anthropic

client = anthropic.Anthropic()

# WRONG — these will 404
client.messages.create(model="claude-3-opus", ...)         # Missing date suffix
client.messages.create(model="claude-opus", ...)           # Not a valid ID
client.messages.create(model="gpt-4", ...)                 # OpenAI model on Anthropic API
client.messages.create(model="claude-v1", ...)             # Very old deprecated alias
client.messages.create(model="claude-3-5-sonnet", ...)     # Missing version suffix

# RIGHT — use full versioned IDs
client.messages.create(model="claude-sonnet-4-6", ...)
client.messages.create(model="claude-opus-4-6", ...)
client.messages.create(model="claude-haiku-4-5-20251001", ...)
```

### Option 2: Centralize model ID as a named constant

```python
# config.py — define once, use everywhere
class Models:
    # Current recommended models (update here when Anthropic releases new versions)
    DEFAULT = "claude-sonnet-4-6"
    FAST = "claude-haiku-4-5-20251001"
    POWERFUL = "claude-opus-4-6"

    # Legacy (keep for reference during migration)
    CLAUDE_35_SONNET = "claude-3-5-sonnet-20241022"
    CLAUDE_35_HAIKU = "claude-3-5-haiku-20241022"

# Usage — never hardcode model strings inline
response = client.messages.create(
    model=Models.DEFAULT,
    max_tokens=1024,
    messages=[...]
)
```

### Option 3: Validate model ID at startup

```python
import anthropic

CONFIGURED_MODEL = "claude-sonnet-4-6"

def validate_model_id(model: str) -> bool:
    """Test model ID with a minimal request at startup"""
    client = anthropic.Anthropic()
    try:
        client.messages.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}]
        )
        return True
    except anthropic.NotFoundError:
        print(f"ERROR: Model '{model}' not found. Check the model ID.")
        return False
    except anthropic.BadRequestError as e:
        print(f"ERROR: Invalid model ID '{model}': {e}")
        return False

# Call at startup, not on every request
if not validate_model_id(CONFIGURED_MODEL):
    import sys; sys.exit(1)
```

### Option 4: Load model from environment with validation

```python
import os, anthropic

VALID_MODELS = {
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
    "claude-3-haiku-20240307",
}

def get_model() -> str:
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    if model not in VALID_MODELS:
        print(f"WARNING: Model '{model}' not in known valid list: {VALID_MODELS}")
        print("Proceeding anyway — list may be outdated.")
    return model
```

### Option 5: Handle model errors gracefully with fallback

```python
FALLBACK_CHAIN = [
    "claude-sonnet-4-6",
    "claude-3-5-sonnet-20241022",
    "claude-3-sonnet-20240229",
]

async def complete_with_model_fallback(messages: list, preferred_model: str) -> tuple[str, str]:
    """Try preferred model, fall back on 404"""
    models_to_try = [preferred_model] + [m for m in FALLBACK_CHAIN if m != preferred_model]

    for model in models_to_try:
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=1024,
                messages=messages
            )
            if model != preferred_model:
                print(f"Warning: Fell back from {preferred_model} to {model}")
            return response.content[0].text, model
        except (anthropic.NotFoundError, anthropic.BadRequestError):
            print(f"Model {model} not available, trying next...")
            continue

    raise RuntimeError(f"No working model found from: {models_to_try}")
```

## Common Model ID Mistakes

| Wrong | Right | Error |
|---|---|---|
| `claude-3-opus` | `claude-opus-4-6` | 404 Not Found |
| `claude-3-5-sonnet` | `claude-3-5-sonnet-20241022` | 404 Not Found |
| `claude-sonnet` | `claude-sonnet-4-6` | 404 Not Found |
| `gpt-4` | `claude-sonnet-4-6` | 404 (wrong API) |
| `claude-instant-1` | `claude-haiku-4-5-20251001` | 404 (deprecated) |
| `claude-2` | `claude-3-haiku-20240307` | 404 (deprecated) |

## Expected Token Savings
Not applicable — prevents failed requests from the start.

## Environment
- Any Anthropic SDK usage; most common when migrating from old model versions
- Source: Anthropic model documentation, direct experience with version migrations
