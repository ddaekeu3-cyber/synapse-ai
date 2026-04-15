---
layout: solution
title: "Error Handling Code Spawns More Errors — Exception Cascade Loop"
category: loop-stuck
description: "Agent catches an error and tries to fix it, but the fix itself raises another error. Each error-handling attempt generates a new error. Agent enters a cascade loop catching its own catches."
tags: [error-handling, cascade, exception, loop, error-recovery]
---

# Error Handling Code Spawns More Errors — Exception Cascade Loop

## Symptom
- Agent catches `TypeError`, tries to fix → raises `AttributeError`
- Agent catches `AttributeError`, tries to fix → raises `KeyError`
- Agent catches `KeyError`, tries to fix → raises `TypeError` again
- Circular chain of error-fix-error cycles
- Each iteration adds to context length, compounding token cost
- Stack trace grows longer with each loop
- Agent is confident each fix should work — but none do

## Root Cause
The agent doesn't distinguish between "fix attempt succeeded" and "fix attempt eliminated the original error but created a new one." Without a root cause model of the error, each fix addresses only the symptom, often creating a new symptom.

## Fix

### Option 1: Root cause first, fix second

```
System prompt:
"When fixing errors:
1. STOP. Read the FULL stack trace, not just the error message.
2. Identify the ROOT cause — the first thing that went wrong.
3. Propose ONE fix for the root cause.
4. If the fix creates a new error: re-read from step 1, not from the new error.
5. If you have tried 3 fixes without resolution: stop and report the error chain.

Never fix an error by working around it — fix the cause."
```

### Option 2: Attempt limit with escalation

```python
class ErrorCascadeDetector:
    def __init__(self, max_attempts=3):
        self.attempts = []
        self.max_attempts = max_attempts

    def record_attempt(self, error_type, fix_applied):
        self.attempts.append({
            "error": error_type,
            "fix": fix_applied,
            "timestamp": time.time()
        })

        if len(self.attempts) >= self.max_attempts:
            raise MaxAttemptsError(
                f"Tried {self.max_attempts} error fixes without success. "
                f"Error chain: {' → '.join(a['error'] for a in self.attempts)}. "
                f"Stopping — manual review needed."
            )

        # Detect cycles
        error_types = [a['error'] for a in self.attempts]
        if len(set(error_types)) < len(error_types):
            raise CyclicErrorError(
                f"Cyclic error detected: {error_types}. "
                f"Same error type appearing twice — fix is not working."
            )
```

### Option 3: Error isolation — test fix in isolation

```python
async def fix_with_isolation(error, agent):
    """Test fix in sandbox before applying"""
    fix = await agent.generate_fix(error)

    # Test the fix in isolation
    test_result = await sandbox.test_fix(fix)
    if test_result.success:
        return await apply_fix(fix)
    else:
        # Fix created new error — report both
        return {
            "original_error": error,
            "attempted_fix": fix,
            "new_error": test_result.error,
            "status": "fix_failed",
            "recommendation": "Manual intervention needed"
        }
```

### Option 4: Binary search debugging for complex cascades

```
System prompt:
"For error cascades (multiple related errors):
1. Add a minimal reproduction: reduce code to smallest version that shows the error
2. Remove half the code — does error still occur?
3. If yes: bug is in the remaining half. If no: bug was in removed half.
4. Repeat until you find the exact line causing the original error.

This prevents chasing symptoms — find the source directly."
```

### Option 5: Token budget circuit breaker for error loops

```python
MAX_ERROR_HANDLING_TOKENS = 10000

async def run_with_error_budget(task, agent):
    error_tokens = 0
    errors_seen = set()

    while True:
        try:
            return await agent.execute(task)
        except Exception as e:
            error_type = type(e).__name__
            error_tokens += 1000  # Approximate tokens per error handling

            if error_tokens > MAX_ERROR_HANDLING_TOKENS:
                raise RuntimeError(
                    f"Error handling budget exceeded ({error_tokens} tokens). "
                    f"Errors encountered: {errors_seen}. Manual review needed."
                )

            if error_type in errors_seen:
                raise RuntimeError(
                    f"Cyclic error: {error_type} appeared twice. Fix is not working."
                )

            errors_seen.add(error_type)
            task.context += f"\nPrevious error: {e}\nFix attempt {len(errors_seen)}:"
```

## Expected Token Savings
5-iteration error cascade without limit: ~40,000 tokens
3-attempt limit with cascade detection: ~12,000 tokens

## Environment
- Any code-generating or code-executing agent
- Source: direct experience, common pattern in agent-assisted debugging
