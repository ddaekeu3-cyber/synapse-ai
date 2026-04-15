---
layout: solution
title: "Agent Streams Response But Discards Partial Output on Error"
category: token-cost
description: "Agent opens a streaming connection, the LLM generates 800 tokens before a network error, and the agent discards all partial output and re-issues the full prompt — paying twice for the same content."
tags: [token-cost, streaming, error-handling, latency, retry]
---

## Symptom

Logs show a stream interrupted at ~80% completion, followed immediately by an identical request:

```
[10:01:00] stream started: prompt_tokens=1200
[10:01:08] stream error: ReadTimeout after 8s, output_tokens=847
[10:01:08] retrying full request...
[10:01:08] stream started: prompt_tokens=1200
[10:01:17] stream complete: output_tokens=1024
```

The agent paid for 847 output tokens that were discarded, then paid for the full 1,024 again. Total output tokens consumed: 1,871. Needed: 1,024.

## Root Cause

The retry loop catches the exception and re-issues the original prompt, ignoring the partially collected stream:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

def ask_streaming(prompt: str) -> str:
    for attempt in range(3):
        try:
            result = []
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}]
            ) as stream:
                for chunk in stream.text_stream:
                    result.append(chunk)
            return "".join(result)
        except Exception:
            result = []  # ← All partial output discarded here
            continue      # ← Re-issues full prompt on retry
    raise RuntimeError("All retries failed")
```

On retry, the full prompt is sent again and the model regenerates everything from scratch.

---

## Fix

### Option 1 — Preserve partial output and continue with assistant prefill

Collect partial output before the error, then continue the generation by prefilling the assistant turn with what was already received.

```python
import anthropic
import time

client = anthropic.Anthropic(api_key="sk-live-...")


def ask_with_continuation(
    prompt: str,
    max_tokens: int = 2048,
    max_retries: int = 3,
) -> str:
    messages = [{"role": "user", "content": prompt}]
    collected: list[str] = []
    tokens_remaining = max_tokens

    for attempt in range(max_retries):
        try:
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=tokens_remaining,
                messages=messages,
            ) as stream:
                for chunk in stream.text_stream:
                    if chunk:
                        collected.append(chunk)

                final = stream.get_final_message()
                if final:
                    tokens_remaining -= final.usage.output_tokens

            break  # Success

        except Exception as exc:
            partial = "".join(collected)
            if partial and attempt < max_retries - 1:
                # Continue from where we left off using assistant prefill
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": partial},  # resume here
                ]
                tokens_remaining = max(64, tokens_remaining - len(partial.split()) * 4 // 3)
                print(f"[retry {attempt+1}] continuing from {len(partial)} chars, ~{tokens_remaining} tokens left")
                time.sleep(1)
            else:
                raise

    return "".join(collected)


result = ask_with_continuation("Write a detailed explanation of neural network backpropagation.")
print(result)

# Expected Token Savings: on interrupt at 80%, saves ~80% of re-generation cost
# Environment: long-response streaming agents where network interruptions are occasional
```

---

### Option 2 — Track output token budget and reduce max_tokens on retry

After a partial failure, reduce `max_tokens` on retry to only request the remaining tokens, so even a full retry costs less.

```python
import anthropic
import time

client = anthropic.Anthropic(api_key="sk-live-...")


def ask_budget_aware(prompt: str, target_tokens: int = 1024) -> str:
    collected: list[str] = []
    output_tokens_used = 0

    for attempt in range(3):
        try:
            remaining = max(64, target_tokens - output_tokens_used)

            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=remaining,
                messages=[
                    {"role": "user", "content": prompt},
                    # On retry, prepend what we already have
                    *([{"role": "assistant", "content": "".join(collected)}] if collected else []),
                ],
            ) as stream:
                for chunk in stream.text_stream:
                    if chunk:
                        collected.append(chunk)

                final = stream.get_final_message()
                if final:
                    output_tokens_used += final.usage.output_tokens

            # Check if model signaled completion
            if final and final.stop_reason == "end_turn":
                break

        except Exception:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise

    return "".join(collected)


# Usage
result = ask_budget_aware("Explain the CAP theorem in depth.", target_tokens=800)
print(f"Result length: {len(result)} chars")

# Expected Token Savings: retry budget = remaining tokens only; saves pro-rata partial progress
# Environment: agents with strict token budgets where partial retries must stay within cost limits
```

---

### Option 3 — Async streaming with partial output recovery

For async agents, capture partial output and resume using the assistant turn in an `asyncio`-safe way.

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def ask_async_resilient(prompt: str, max_tokens: int = 1024) -> str:
    collected: list[str] = []

    async def stream_once(messages: list[dict], budget: int) -> tuple[list[str], bool]:
        """Returns (chunks, completed)."""
        chunks: list[str] = []
        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=budget,
                messages=messages,
            ) as stream:
                async for chunk in stream.text_stream:
                    if chunk:
                        chunks.append(chunk)

                final = await stream.get_final_message()
                completed = final.stop_reason == "end_turn"
                return chunks, completed

        except (anthropic.APIConnectionError, asyncio.TimeoutError):
            return chunks, False  # Return what we have; caller decides

    messages: list[dict] = [{"role": "user", "content": prompt}]
    budget = max_tokens

    for attempt in range(3):
        new_chunks, done = await stream_once(messages, budget)
        collected.extend(new_chunks)

        if done:
            break

        partial_text = "".join(collected)
        if partial_text and attempt < 2:
            # Build continuation messages
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": partial_text},
            ]
            # Rough budget estimate: 1 token ≈ 4 chars
            used_estimate = len(partial_text) // 4
            budget = max(64, max_tokens - used_estimate)
            await asyncio.sleep(1)

    return "".join(collected)


async def main():
    result = await ask_async_resilient("Describe the history of computing in detail.")
    print(result[:200], "...")

asyncio.run(main())

# Expected Token Savings: continuation avoids full re-generation; async-safe for parallel agents
# Environment: async agents handling long-running streaming responses
```

---

### Option 4 — Checkpoint to disk and resume across process restarts

For very long generations (documents, reports), checkpoint partial output to disk so even a process crash doesn't lose progress.

```python
import anthropic
import hashlib
import time
from pathlib import Path

client = anthropic.Anthropic(api_key="sk-live-...")

CHECKPOINT_DIR = Path(".stream_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)


def checkpoint_path(prompt: str) -> Path:
    key = hashlib.md5(prompt.encode()).hexdigest()[:12]
    return CHECKPOINT_DIR / f"{key}.txt"


def load_checkpoint(prompt: str) -> str:
    path = checkpoint_path(prompt)
    if path.exists():
        text = path.read_text()
        print(f"[checkpoint] Resuming from {len(text)} chars")
        return text
    return ""


def save_checkpoint(prompt: str, text: str) -> None:
    checkpoint_path(prompt).write_text(text)


def clear_checkpoint(prompt: str) -> None:
    p = checkpoint_path(prompt)
    if p.exists():
        p.unlink()


def ask_with_checkpoint(prompt: str, max_tokens: int = 2048) -> str:
    partial = load_checkpoint(prompt)
    messages = [{"role": "user", "content": prompt}]
    if partial:
        messages.append({"role": "assistant", "content": partial})
        # Reduce budget by estimated tokens already generated
        max_tokens = max(64, max_tokens - len(partial.split()))

    collected = list(partial)

    for attempt in range(3):
        try:
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=max_tokens,
                messages=messages,
            ) as stream:
                for chunk in stream.text_stream:
                    if chunk:
                        collected.append(chunk)
                        # Checkpoint every 500 chars
                        if len(collected) % 100 == 0:
                            save_checkpoint(prompt, "".join(collected))

            # Completed — remove checkpoint
            clear_checkpoint(prompt)
            return "".join(collected)

        except Exception as exc:
            save_checkpoint(prompt, "".join(collected))
            print(f"[checkpoint] Saved {len(collected)} chars after error: {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
                partial = "".join(collected)
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": partial},
                ]
                max_tokens = max(64, max_tokens - len(partial.split()))
            else:
                raise

    return "".join(collected)


result = ask_with_checkpoint("Write a 2000-word technical overview of transformer architectures.")
print(f"Done: {len(result)} chars")

# Expected Token Savings: survives process restarts; pays only for remaining tokens
# Environment: document generation agents, report writers, batch content pipelines
```

---

### Option 5 — Stream with `stop_sequence` chunking to avoid large losses

Break a long generation into smaller chunks using `stop_sequences`. Each chunk is short enough that an interruption loses at most one chunk worth of tokens.

```python
import anthropic
import time

client = anthropic.Anthropic(api_key="sk-live-...")

CHUNK_STOP = "[[CONTINUE]]"
CHUNK_MAX_TOKENS = 300  # Each chunk is small — max loss per interruption


def ask_chunked(prompt: str, total_budget: int = 1500) -> str:
    all_chunks: list[str] = []
    messages = [{"role": "user", "content": prompt}]
    tokens_used = 0

    system = f"""Complete the task in sections. After each section (roughly 200 words),
write '{CHUNK_STOP}' and stop. Continue from where you left off each time.
When the task is fully complete, do NOT write '{CHUNK_STOP}'."""

    while tokens_used < total_budget:
        remaining = min(CHUNK_MAX_TOKENS, total_budget - tokens_used)
        try:
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=remaining,
                system=system,
                stop_sequences=[CHUNK_STOP],
                messages=messages,
            ) as stream:
                chunk_text = ""
                for text in stream.text_stream:
                    if text:
                        chunk_text += text

                final = stream.get_final_message()
                tokens_used += final.usage.output_tokens if final else 0

            all_chunks.append(chunk_text)

            if final and final.stop_reason == "end_turn":
                break  # Model finished naturally

            # Add chunk to history and continue
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "".join(all_chunks)},
            ]

        except Exception as exc:
            print(f"[chunk error] Lost ~{CHUNK_MAX_TOKENS} tokens: {exc}")
            time.sleep(2)
            # Retry the same chunk — only lost one small chunk

    return "".join(all_chunks)


result = ask_chunked("Write a comprehensive guide to Python async programming.")
print(result[:300], "...")

# Expected Token Savings: max loss per interruption = CHUNK_MAX_TOKENS (300) vs full response
# Environment: long document agents where reliability matters more than single-turn efficiency
```

---

### Option 6 — Monitor stream health and pre-emptively checkpoint on slow throughput

Detect slow or stalled streams before they time out and checkpoint proactively.

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

STALL_THRESHOLD_SECONDS = 10.0   # Consider stream stalled after 10s without a chunk
MIN_CHUNK_RATE = 5                # Expect at least 5 chars per second on average


async def ask_health_monitored(prompt: str, max_tokens: int = 1024) -> str:
    collected: list[str] = []
    last_chunk_time = time.monotonic()
    start_time = time.monotonic()

    async def monitor_stall(stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(2)
            idle = time.monotonic() - last_chunk_time
            if idle > STALL_THRESHOLD_SECONDS and not stop_event.is_set():
                print(f"[health] Stream stalled {idle:.1f}s — will retry with partial output")
                stop_event.set()

    stop_event = asyncio.Event()
    monitor_task = asyncio.create_task(monitor_stall(stop_event))

    try:
        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for chunk in stream.text_stream:
                if stop_event.is_set():
                    break  # Stall detected — bail out with partial text
                if chunk:
                    collected.append(chunk)
                    last_chunk_time = time.monotonic()

    finally:
        stop_event.set()
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    partial = "".join(collected)
    if not partial:
        raise RuntimeError("Stream produced no output")

    # If stalled mid-response, continue with prefill
    elapsed = time.monotonic() - start_time
    chars_per_sec = len(partial) / max(elapsed, 1)
    print(f"[health] Got {len(partial)} chars at {chars_per_sec:.1f} chars/s")

    if stop_event.is_set() and len(partial) > 50:
        # Resume with continuation
        continuation = await ask_health_monitored(
            prompt,
            max_tokens=max(64, max_tokens - len(partial.split()) * 4 // 3),
        )
        return partial + continuation

    return partial


async def main():
    result = await ask_health_monitored("Explain distributed consensus algorithms.")
    print(result[:200])

asyncio.run(main())

# Expected Token Savings: detect stalls before timeout; partial output preserved on every retry
# Environment: async agents where stream health monitoring improves reliability and cost
```

---

## Comparison

| Option | Partial Output Preserved | Cross-Restart | Overhead | Best For |
|--------|--------------------------|---------------|----------|----------|
| 1 | Yes (assistant prefill) | No | Minimal | General streaming retry |
| 2 | Yes + budget tracking | No | Minimal | Cost-budgeted agents |
| 3 | Yes (async) | No | Minimal | Async agents |
| 4 | Yes (disk checkpoint) | Yes | File I/O | Long document generation |
| 5 | Yes (chunk-based) | Partial | Extra prompts | Maximum reliability |
| 6 | Yes (stall detection) | No | Monitor task | Unreliable networks |

**Recommended starting point:** Option 1 for most agents — preserve partial output in a list, pass it as an assistant prefill on retry. Zero dependencies, immediate savings. Add Option 4's disk checkpointing for document-generation workloads where a process crash cannot afford to lose thousands of tokens of generated content.
