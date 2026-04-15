---
layout: solution
title: "Agent Keeps Failed Tool Results in History — Error Accumulation Bloat"
category: context-window
description: "Agent attempts a tool call, gets an error. Tries again differently, gets another error. Tries a third time. All 3 failed attempts stay in the conversation history. After 10 failed attempts, the context is filled with error messages that provide no value. Model gets confused by the accumulated failure history."
tags: [context-window, tool-failure, history, error-accumulation, pruning, token-cost, retry, bloat]
---

# Agent Keeps Failed Tool Results in History — Error Accumulation Bloat

## Symptom
- After 5 failed search attempts, conversation history has 10 messages (5 tool calls + 5 errors)
- Model begins hallucinating after seeing many errors — "I've been unable to X" pattern repeats
- Context window fills with `{"error": "..."}` tool results — pushes useful context out
- Token cost of a 10-retry loop: 10 × full-history size — exponential growth
- Model gets stuck in a loop apologizing for errors that are no longer relevant
- Pruning the history mid-task causes the model to lose track of what it was doing

## Root Cause
Conversation history grows monotonically by default. Every tool call and its result — successful or failed — is appended to the messages array and never removed. Failed attempts are especially wasteful: they consume tokens but provide no useful information for subsequent steps. After many retries, the model spends more context "remembering" past failures than doing productive work. The fix is to prune or summarize failed attempts while preserving the essential context.

## Fix

### Option 1: Prune failed tool attempts from history after successful retry

```python
from copy import deepcopy

def prune_failed_tool_attempts(messages: list[dict]) -> list[dict]:
    """
    Remove failed tool call / tool result pairs from history.
    Keeps: successful tool calls and their results.
    Removes: tool calls whose result contains an error.
    Preserves: message order and conversation flow.
    """
    if not messages:
        return messages

    pruned = []
    i = 0

    while i < len(messages):
        msg = messages[i]

        # Check if this is an assistant message with tool_use blocks
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if not isinstance(content, list):
                pruned.append(msg)
                i += 1
                continue

            tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]

            if not tool_uses:
                pruned.append(msg)
                i += 1
                continue

            # Next message should be tool results
            if i + 1 >= len(messages):
                pruned.append(msg)
                i += 1
                continue

            next_msg = messages[i + 1]
            if next_msg.get("role") != "user":
                pruned.append(msg)
                i += 1
                continue

            result_content = next_msg.get("content", [])
            if not isinstance(result_content, list):
                pruned.append(msg)
                pruned.append(next_msg)
                i += 2
                continue

            # Check if ALL tool results are errors
            tool_results = [
                r for r in result_content
                if isinstance(r, dict) and r.get("type") == "tool_result"
            ]

            all_errors = all(r.get("is_error", False) for r in tool_results) if tool_results else False

            if all_errors:
                # Skip both the tool call and the error result
                print(
                    f"Pruning failed tool attempt: "
                    f"{[b.get('name') for b in tool_uses]}"
                )
                i += 2  # Skip assistant (tool call) + user (error result)
                continue

            pruned.append(msg)
            pruned.append(next_msg)
            i += 2
        else:
            pruned.append(msg)
            i += 1

    original_count = len(messages)
    pruned_count = len(pruned)
    if pruned_count < original_count:
        print(f"History pruned: {original_count} → {pruned_count} messages "
              f"({original_count - pruned_count} removed)")

    return pruned
```

### Option 2: Error budget — stop retrying after N failed attempts

```python
from dataclasses import dataclass, field
import anthropic

@dataclass
class ToolErrorBudget:
    """Track tool failures per tool name — stop retrying when budget exhausted"""
    max_failures_per_tool: int = 3
    max_total_failures: int = 10
    _failures: dict[str, int] = field(default_factory=dict)
    _total: int = 0

    def record_failure(self, tool_name: str) -> bool:
        """
        Record a failure. Returns True if we should continue retrying,
        False if the budget is exhausted.
        """
        self._failures[tool_name] = self._failures.get(tool_name, 0) + 1
        self._total += 1

        tool_failures = self._failures[tool_name]

        if self._total >= self.max_total_failures:
            print(
                f"Total error budget exhausted ({self._total} failures). "
                f"Stopping tool execution."
            )
            return False

        if tool_failures >= self.max_failures_per_tool:
            print(
                f"Tool '{tool_name}' error budget exhausted "
                f"({tool_failures}/{self.max_failures_per_tool} failures). "
                f"Will not retry this tool."
            )
            return False

        return True  # Still within budget — can retry

    def is_tool_blocked(self, tool_name: str) -> bool:
        return self._failures.get(tool_name, 0) >= self.max_failures_per_tool

    @property
    def summary(self) -> str:
        if not self._failures:
            return "No failures"
        return f"Failures: {dict(self._failures)} (total: {self._total})"

error_budget = ToolErrorBudget(max_failures_per_tool=3, max_total_failures=10)

def process_tool_results(tool_results: list[dict], messages: list[dict]) -> list[dict]:
    """
    Process tool results and return updated messages.
    Failed results are recorded but prunable after threshold.
    """
    for result in tool_results:
        if result.get("is_error"):
            tool_name = result.get("tool_name", "unknown")
            can_retry = error_budget.record_failure(tool_name)

            if not can_retry:
                # Replace error with a concise failure summary instead of full error
                result["content"] = (
                    f"[Tool '{tool_name}' blocked after repeated failures: "
                    f"{error_budget.summary}. Try a different approach.]"
                )

    return messages
```

### Option 3: Compact failed history — replace errors with summary

```python
def compact_error_history(
    messages: list[dict],
    max_errors_to_keep: int = 2
) -> list[dict]:
    """
    Instead of pruning all errors, keep a summary.
    Replaces multiple errors with a single summary message.
    Preserves enough context for the model to understand what was tried.
    """
    errors_seen = []
    non_error_messages = []
    i = 0

    while i < len(messages):
        msg = messages[i]

        # Detect failed tool call pairs (assistant tool call + error result)
        if (msg.get("role") == "assistant"
                and i + 1 < len(messages)
                and messages[i + 1].get("role") == "user"):

            content = msg.get("content", [])
            next_content = messages[i + 1].get("content", [])

            if isinstance(content, list) and isinstance(next_content, list):
                tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                tool_results = [b for b in next_content if isinstance(b, dict) and b.get("type") == "tool_result"]
                error_results = [r for r in tool_results if r.get("is_error")]

                if tool_uses and error_results:
                    # Record this error
                    for tu in tool_uses:
                        for er in error_results:
                            errors_seen.append({
                                "tool": tu.get("name"),
                                "input": tu.get("input", {}),
                                "error": er.get("content", "unknown error")
                            })
                    i += 2
                    continue

        non_error_messages.append(msg)
        i += 1

    if not errors_seen:
        return messages

    # Build a summary of all errors seen
    if len(errors_seen) <= max_errors_to_keep:
        # Few errors — keep them as-is for context
        return messages

    # Many errors — replace with a compact summary
    error_summary = {
        "role": "user",
        "content": [{
            "type": "text",
            "text": (
                f"[Previous attempts summary: {len(errors_seen)} tool call(s) failed. "
                f"Tools tried: {list(set(e['tool'] for e in errors_seen))}. "
                f"Last error: {errors_seen[-1]['error'][:200]}. "
                f"These approaches did not work — try a different strategy.]"
            )
        }]
    }

    # Find insertion point (after first non-error content)
    insert_after = 0
    for j, msg in enumerate(non_error_messages):
        if msg.get("role") in ("system", "user"):
            insert_after = j + 1

    result = non_error_messages[:insert_after] + [error_summary] + non_error_messages[insert_after:]
    print(f"Compacted {len(errors_seen)} error messages into 1 summary")
    return result
```

### Option 4: History size monitor — trigger pruning automatically

```python
import anthropic

client = anthropic.Anthropic()

class HistoryManager:
    """
    Conversation history manager with automatic pruning.
    Triggers error pruning when history grows too large.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        max_history_tokens: int = 50_000,
        prune_on_error_count: int = 3
    ):
        self.model = model
        self.max_history_tokens = max_history_tokens
        self.prune_on_error_count = prune_on_error_count
        self._messages: list[dict] = []
        self._consecutive_errors = 0

    def append(self, message: dict):
        self._messages.append(message)
        self._check_and_prune()

    def _check_and_prune(self):
        """Check if pruning is needed and apply appropriate strategy"""
        # Count consecutive errors in recent history
        recent_errors = self._count_recent_errors(last_n=6)

        if recent_errors >= self.prune_on_error_count:
            print(f"Detected {recent_errors} recent errors — pruning failed attempts")
            self._messages = prune_failed_tool_attempts(self._messages)
            self._consecutive_errors = 0

        # Check token count
        token_count = self._estimate_tokens()
        if token_count > self.max_history_tokens:
            print(f"History too large ({token_count} tokens) — compacting errors")
            self._messages = compact_error_history(self._messages)

    def _count_recent_errors(self, last_n: int) -> int:
        """Count error results in the last N messages"""
        recent = self._messages[-last_n:]
        count = 0
        for msg in recent:
            if msg.get("role") == "user":
                content = msg.get("content", [])
                if isinstance(content, list):
                    count += sum(
                        1 for b in content
                        if isinstance(b, dict) and b.get("type") == "tool_result"
                        and b.get("is_error")
                    )
        return count

    def _estimate_tokens(self) -> int:
        total_chars = sum(len(str(m)) for m in self._messages)
        return total_chars // 4  # Rough estimate: 4 chars per token

    @property
    def messages(self) -> list[dict]:
        return list(self._messages)

    @property
    def stats(self) -> dict:
        return {
            "message_count": len(self._messages),
            "estimated_tokens": self._estimate_tokens(),
            "recent_errors": self._count_recent_errors(10)
        }
```

### Option 5: Tool retry with automatic history reset

```python
async def retry_tool_with_fresh_context(
    tool_name: str,
    tool_fn,
    params: dict,
    base_messages: list[dict],
    max_attempts: int = 3
) -> tuple[any, list[dict]]:
    """
    Retry a failing tool while keeping history clean.
    On retry: prune failed attempts, add brief failure note, try again.
    Returns: (result, updated_messages)
    """
    messages = list(base_messages)
    last_error = None

    for attempt in range(max_attempts):
        try:
            result = await tool_fn(**params)

            if attempt > 0:
                print(f"Tool '{tool_name}' succeeded on attempt {attempt + 1}")

            return result, messages

        except Exception as e:
            last_error = e
            print(f"Tool '{tool_name}' failed (attempt {attempt + 1}/{max_attempts}): {e}")

            if attempt < max_attempts - 1:
                # Prune failure from history — don't let errors accumulate
                messages = prune_failed_tool_attempts(messages)

                # Add a brief note that this approach failed (compact, not verbose)
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            f"Note: {tool_name} failed with: {str(e)[:100]}. "
                            f"Trying a different approach..."
                        )
                    }]
                })

    raise RuntimeError(
        f"Tool '{tool_name}' failed after {max_attempts} attempts. "
        f"Last error: {last_error}"
    )
```

### Option 6: System prompt — instruct model to acknowledge and move on

```
System prompt:
"Error handling in tool calls:

When a tool call fails:
1. Note the failure briefly (1 sentence)
2. Try a DIFFERENT approach — do not repeat the same call
3. After 3 failed attempts with the same tool, STOP retrying that tool
   and either: try a different tool, or tell the user you cannot complete that step

Error accumulation rules:
- Do not dwell on past errors — they have been noted
- Do not list all previous error messages — summarize them in one sentence
- Do not apologize repeatedly for the same error
- If a tool keeps failing, say: 'I've been unable to X after 3 attempts.
  I'll proceed with what I have, or let me know if you want me to try differently.'

Context management:
- Treat error context as low-value — don't reference old errors unless directly relevant
- Focus on the current state and what can still be accomplished
- If context fills with errors, summarize the situation and continue"
```

## Error Accumulation Impact

| Scenario | Messages Added | Tokens Wasted | Model Impact |
|---|---|---|---|
| 1 failed tool attempt | 2 | ~500 | Minimal |
| 5 failed attempts, same tool | 10 | ~5,000 | Starts repeating apologies |
| 10 failed attempts | 20 | ~10,000 | Context dominated by errors |
| 20 failed attempts | 40 | ~20,000 | High confusion/looping risk |
| After pruning (10→2) | 4 | ~1,000 | Clean context |

## Expected Token Savings
10 failed attempts kept in history throughout 20-turn task: ~50,000 extra tokens
Pruning errors after 3 failures: ~45,000 tokens saved per task

## Environment
- Any agent with tool use in an agentic loop; critical for agents with unreliable external tools, network calls, or complex multi-step tasks where early failures don't prevent task completion
- Source: direct experience; error accumulation in history is the most common cause of context window exhaustion in long-running agentic tasks
