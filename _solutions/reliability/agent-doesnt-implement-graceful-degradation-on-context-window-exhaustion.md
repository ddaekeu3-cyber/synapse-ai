---
title: "Agent Doesn't Implement Graceful Degradation on Context Window Exhaustion"
description: "How to detect and handle context window exhaustion before it causes hard failures, using summarization, pruning, and model escalation strategies."
categories: [reliability]
difficulty: advanced
---

When a conversation's accumulated tokens approach the model's context limit, the next API call will fail with a context-length error. Without proactive handling, the agent crashes mid-task. Graceful degradation detects the approaching limit and either compresses history, escalates to a larger context model, or partitions the work before hitting the wall.

## Solution 1: Token Budget Monitor with Early Warning

Track input tokens on every response and trigger compression before the limit is reached.

```python
import anthropic

client = anthropic.AsyncAnthropic()

MODEL = "claude-sonnet-4-6"
CONTEXT_LIMIT = 180_000      # tokens
COMPRESSION_THRESHOLD = 0.80  # compress at 80% full
CRITICAL_THRESHOLD = 0.95     # hard stop at 95%


def usage_ratio(used: int) -> float:
    return used / CONTEXT_LIMIT


async def compress_messages(messages: list[dict]) -> list[dict]:
    """Summarize the middle portion of message history, keep first + last."""
    if len(messages) < 4:
        return messages

    system_msgs = messages[:1]
    recent_msgs = messages[-4:]
    middle_msgs = messages[1:-4]

    if not middle_msgs:
        return messages

    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[tool interaction]'}"
        for m in middle_msgs
    )

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[
            {
                "role": "user",
                "content": (
                    "Summarize the following conversation history in ≤300 words, "
                    "preserving all decisions, tool results, and key facts:\n\n"
                    + history_text
                ),
            }
        ],
    )
    summary = resp.content[0].text
    summary_msg = {
        "role": "user",
        "content": f"[Compressed conversation history]\n{summary}",
    }
    placeholder = {"role": "assistant", "content": "Understood, I have the history summary."}
    return system_msgs + [summary_msg, placeholder] + recent_msgs


async def agent_with_budget_monitor(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]
    total_input_tokens = 0

    for iteration in range(20):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=messages,
        )
        total_input_tokens = resp.usage.input_tokens

        ratio = usage_ratio(total_input_tokens)

        if ratio >= CRITICAL_THRESHOLD:
            return "[DEGRADED] Context window critically full — stopping to avoid hard failure."

        if ratio >= COMPRESSION_THRESHOLD:
            print(f"[warn] Context {ratio:.0%} full — compressing history")
            messages = await compress_messages(messages)

        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})

    return "Max iterations reached."
```

## Solution 2: Sliding Window with Pinned System Context

Maintain a fixed-size sliding window of recent messages while pinning an immutable system context block.

```python
from collections import deque
import anthropic

client = anthropic.AsyncAnthropic()

MODEL = "claude-sonnet-4-6"
MAX_WINDOW_MESSAGES = 20       # max non-system messages in window
PIN_RECENT = 4                 # always keep last N messages


class SlidingWindowAgent:
    def __init__(self, system_prompt: str, window_size: int = MAX_WINDOW_MESSAGES):
        self.system_prompt = system_prompt
        self.window_size = window_size
        self._window: deque[dict] = deque(maxlen=window_size)
        self._pinned_facts: list[str] = []

    def _add_to_window(self, message: dict) -> None:
        self._window.append(message)

    def _extract_facts(self, text: str) -> list[str]:
        """Heuristic: lines starting with '•' or 'KEY:' are facts to pin."""
        return [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith(("•", "KEY:", "FACT:"))
        ]

    def _build_messages(self) -> list[dict]:
        msgs = list(self._window)
        if self._pinned_facts:
            fact_block = "Pinned facts from earlier context:\n" + "\n".join(self._pinned_facts)
            msgs = [{"role": "user", "content": fact_block},
                    {"role": "assistant", "content": "Noted."}] + msgs
        return msgs

    async def chat(self, user_message: str) -> str:
        self._add_to_window({"role": "user", "content": user_message})

        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=self.system_prompt,
            messages=self._build_messages(),
        )

        ratio = resp.usage.input_tokens / 180_000
        reply_text = resp.content[0].text

        # Pin newly discovered facts before they scroll out of the window
        new_facts = self._extract_facts(reply_text)
        self._pinned_facts.extend(new_facts)
        if len(self._pinned_facts) > 50:
            self._pinned_facts = self._pinned_facts[-50:]

        self._add_to_window({"role": "assistant", "content": reply_text})

        if ratio > 0.85:
            print(f"[warn] Sliding window at {ratio:.0%} — old messages evicted automatically.")

        return reply_text


async def main():
    agent = SlidingWindowAgent(
        system_prompt="You are a helpful research assistant.",
        window_size=16,
    )
    for turn in range(30):
        reply = await agent.chat(f"Turn {turn}: continue the research discussion.")
        print(f"Turn {turn}: {reply[:80]}…")
```

## Solution 3: Context-Aware Model Escalation

Automatically escalate to a higher-context model when the current model's limit approaches.

```python
import anthropic

client = anthropic.AsyncAnthropic()

# Models ordered by context window size (ascending)
MODEL_LADDER = [
    ("claude-haiku-4-5-20251001", 180_000),
    ("claude-sonnet-4-6",         180_000),
    ("claude-opus-4-6",           200_000),
]
ESCALATION_THRESHOLD = 0.82


def select_model(used_tokens: int) -> tuple[str, int]:
    """Pick the smallest model whose remaining capacity fits."""
    for model, limit in MODEL_LADDER:
        if used_tokens / limit < ESCALATION_THRESHOLD:
            return model, limit
    # All models full — return largest
    return MODEL_LADDER[-1]


async def escalating_agent(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]
    current_model, current_limit = MODEL_LADDER[0]
    used_tokens = 0

    for iteration in range(20):
        resp = await client.messages.create(
            model=current_model,
            max_tokens=1024,
            messages=messages,
        )
        used_tokens = resp.usage.input_tokens

        # Check if we should escalate before next call
        new_model, new_limit = select_model(used_tokens)
        if new_model != current_model:
            print(f"[escalate] {current_model} → {new_model} ({used_tokens}/{current_limit} tokens)")
            current_model, current_limit = new_model, new_limit

        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})

        # Simulate long tool result
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "fake_id",
                    "content": "result " * 500,
                }
            ],
        })

    return "Max iterations."
```

## Solution 4: Segment-and-Merge for Long Tasks

Split a long task into independent segments that each fit in the context window, then merge results.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MODEL = "claude-sonnet-4-6"
SEGMENT_TOKEN_BUDGET = 60_000   # tokens per segment
CHARS_PER_TOKEN_APPROX = 4


def split_into_segments(documents: list[str], budget_chars: int) -> list[list[str]]:
    segments = []
    current: list[str] = []
    current_chars = 0
    for doc in documents:
        doc_chars = len(doc)
        if current_chars + doc_chars > budget_chars and current:
            segments.append(current)
            current = []
            current_chars = 0
        current.append(doc)
        current_chars += doc_chars
    if current:
        segments.append(current)
    return segments


async def process_segment(segment: list[str], task_description: str) -> str:
    combined = "\n\n---\n\n".join(segment)
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Task: {task_description}\n\n"
                    f"Process the following documents and provide a partial result:\n\n"
                    f"{combined}"
                ),
            }
        ],
    )
    return resp.content[0].text


async def merge_partial_results(partial_results: list[str], task_description: str) -> str:
    combined = "\n\n---PARTIAL RESULT---\n\n".join(partial_results)
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Task: {task_description}\n\n"
                    f"Merge the following partial results into a single coherent final answer:\n\n"
                    f"{combined}"
                ),
            }
        ],
    )
    return resp.content[0].text


async def segment_and_merge_agent(documents: list[str], task: str) -> str:
    budget_chars = SEGMENT_TOKEN_BUDGET * CHARS_PER_TOKEN_APPROX
    segments = split_into_segments(documents, budget_chars)

    if len(segments) == 1:
        # Fits in one context — process directly
        return await process_segment(segments[0], task)

    print(f"[segment-merge] Splitting into {len(segments)} segments")
    partials = await asyncio.gather(*[process_segment(seg, task) for seg in segments])
    return await merge_partial_results(list(partials), task)


async def main():
    # Simulate 50 long documents
    documents = [f"Document {i}: " + "content " * 200 for i in range(50)]
    result = await segment_and_merge_agent(documents, "Summarize all documents.")
    print(result[:300])
```

## Solution 5: Checkpoint-and-Resume with Persistent State

Save agent progress to disk at each step; if context exhausts, restart from the last checkpoint with a fresh context.

```python
import asyncio
import json
import time
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()

MODEL = "claude-sonnet-4-6"
CONTEXT_LIMIT = 180_000
CHECKPOINT_DIR = Path("/tmp/agent_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)


def save_checkpoint(task_id: str, step: int, state: dict) -> None:
    path = CHECKPOINT_DIR / f"{task_id}_step{step:04d}.json"
    path.write_text(json.dumps(state))
    print(f"[checkpoint] Saved step {step} to {path}")


def load_latest_checkpoint(task_id: str) -> tuple[int, dict] | None:
    checkpoints = sorted(CHECKPOINT_DIR.glob(f"{task_id}_step*.json"))
    if not checkpoints:
        return None
    latest = checkpoints[-1]
    step = int(latest.stem.split("step")[1])
    return step, json.loads(latest.read_text())


async def resumable_agent(task_id: str, goal: str, subtasks: list[str]) -> str:
    # Attempt to resume from checkpoint
    checkpoint = load_latest_checkpoint(task_id)
    if checkpoint:
        start_step, state = checkpoint
        completed = state["completed"]
        accumulated = state["accumulated_results"]
        print(f"[resume] Resuming from step {start_step}, {len(completed)} subtasks done")
    else:
        start_step = 0
        completed = []
        accumulated = []

    for i, subtask in enumerate(subtasks):
        if i < start_step:
            continue  # Already completed

        # Fresh context for each subtask (avoids window exhaustion)
        context_summary = (
            f"Progress: {len(completed)}/{len(subtasks)} subtasks done.\n"
            f"Accumulated findings:\n" + "\n".join(accumulated[-5:])  # Last 5 results
            if accumulated else ""
        )

        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Overall goal: {goal}\n\n"
                        f"{context_summary}\n\n"
                        f"Current subtask: {subtask}"
                    ),
                }
            ],
        )

        result = resp.content[0].text
        completed.append(subtask)
        accumulated.append(f"Subtask {i}: {result[:200]}")

        save_checkpoint(task_id, i + 1, {
            "completed": completed,
            "accumulated_results": accumulated,
            "timestamp": time.time(),
        })

    # Final synthesis from accumulated results
    synthesis_resp = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Goal: {goal}\n\nSynthesize these results:\n"
                    + "\n".join(accumulated)
                ),
            }
        ],
    )
    return synthesis_resp.content[0].text


async def main():
    subtasks = [f"Research aspect {i} of the topic" for i in range(10)]
    result = await resumable_agent(
        task_id="research_001",
        goal="Comprehensive analysis of distributed systems",
        subtasks=subtasks,
    )
    print(result[:500])
```

## Solution 6: Adaptive Pruning with Importance Scoring

Score each message by importance and prune the lowest-scoring ones when context fills up.

```python
import anthropic

client = anthropic.AsyncAnthropic()

MODEL = "claude-sonnet-4-6"
CONTEXT_LIMIT = 180_000
PRUNE_THRESHOLD = 0.78
TARGET_AFTER_PRUNE = 0.55


def score_message(msg: dict, recency_rank: int, total: int) -> float:
    """
    Heuristic importance score (0-1):
    - Tool results score high (contain factual data)
    - Recent messages score high
    - Short messages score low
    """
    content = msg.get("content", "")
    text = content if isinstance(content, str) else str(content)

    recency = recency_rank / max(total, 1)  # 0 = oldest, 1 = newest

    is_tool_result = isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    )
    tool_bonus = 0.3 if is_tool_result else 0.0

    length_score = min(len(text) / 500, 0.3)  # longer = more content (capped)

    return recency * 0.5 + tool_bonus + length_score


def prune_messages(messages: list[dict], current_tokens: int) -> list[dict]:
    target_tokens = int(CONTEXT_LIMIT * TARGET_AFTER_PRUNE)
    tokens_to_remove = current_tokens - target_tokens

    if tokens_to_remove <= 0:
        return messages

    # Score all messages (keep first user message always)
    scored = [
        (i, msg, score_message(msg, i, len(messages)))
        for i, msg in enumerate(messages)
    ]

    # Never prune index 0 (initial user query)
    prunable = sorted(scored[1:], key=lambda x: x[2])

    pruned_indices = set()
    estimated_removed = 0

    for idx, msg, score in prunable:
        if estimated_removed >= tokens_to_remove:
            break
        content = msg.get("content", "")
        text = content if isinstance(content, str) else str(content)
        estimated_removed += len(text) // 4  # approx tokens
        pruned_indices.add(idx)

    result = [msg for i, msg in enumerate(messages) if i not in pruned_indices]
    print(f"[prune] Removed {len(pruned_indices)} messages, ~{estimated_removed} tokens")
    return result


async def importance_pruning_agent(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]

    for iteration in range(25):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=messages,
        )

        used = resp.usage.input_tokens
        ratio = used / CONTEXT_LIMIT

        if ratio >= PRUNE_THRESHOLD:
            print(f"[warn] {ratio:.0%} full — pruning low-importance messages")
            messages = prune_messages(messages, used)

        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})

        # Simulate accumulated tool results
        if iteration % 3 == 0:
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"tool_{iteration}",
                        "content": "result data " * 150,
                    }
                ],
            })

    return "Max iterations reached."
```

## Comparison

| Solution | Proactive? | Loses history? | Parallelizable? | Cost overhead | Best for |
|---|---|---|---|---|---|
| **Budget monitor + compress** | Yes | Partially | No | Low (Haiku) | General chat agents |
| **Sliding window** | Yes | Yes (old msgs) | No | None | Long ongoing conversations |
| **Model escalation** | Yes | No | No | High (Opus) | High-stakes long tasks |
| **Segment-and-merge** | Yes | No | Yes | Medium | Large document processing |
| **Checkpoint-and-resume** | Yes | No | No | Low | Multi-hour autonomous tasks |
| **Importance pruning** | Yes | Partially | No | None | Tool-heavy agent loops |

Start with **sliding window** (Solution 2) for conversational agents — zero extra cost, simple to reason about. Use **segment-and-merge** (Solution 4) for document-processing pipelines. Add **checkpoint-and-resume** (Solution 5) for long autonomous tasks where crashing mid-way is expensive.
