---
layout: solution
title: "Agent Doesn't Implement Partial Response Recovery on Error"
category: streaming
description: "When a streaming response fails mid-way, agents discard all received content and restart from scratch. Implementing partial recovery saves tokens, reduces latency, and makes long streamed responses resilient to transient errors."
tags: [streaming, error-recovery, resilience, partial-response, retry, long-responses]
---

# Agent Doesn't Implement Partial Response Recovery on Error

## Problem

Streaming responses can fail mid-generation due to network interruptions, API timeouts, or server errors. Without recovery logic, the agent discards all received text and re-sends the full prompt, paying twice for the same generation. For long responses (code generation, documents, analysis), this doubles cost and adds noticeable latency.

## Why This Happens

The Anthropic streaming API delivers tokens incrementally via SSE. If the connection drops at token 800 of a 1000-token response, there is no built-in resume mechanism. Naive implementations simply retry the entire request, generating content already received.

## Solutions

### Option 1: Accumulate-and-Retry — Buffer received text; retry with continuation prompt on failure

```python
import anthropic
import time
from dataclasses import dataclass, field

@dataclass
class StreamBuffer:
    accumulated_text: str = ""
    completed: bool = False
    error: Exception | None = None

    def append(self, chunk: str) -> None:
        self.accumulated_text += chunk

    def continuation_prompt(self, original_prompt: str) -> str:
        if not self.accumulated_text:
            return original_prompt
        return (
            f"{original_prompt}\n\n"
            f"[You were generating a response and it was interrupted. "
            f"You had written the following so far. Continue from exactly where you left off, "
            f"do not repeat what was already written:]\n\n{self.accumulated_text}"
        )


def stream_with_recovery(
    client: anthropic.Anthropic,
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 2048,
    max_retries: int = 3,
) -> str:
    buffer = StreamBuffer()
    original_user_content = messages[-1]["content"]

    for attempt in range(max_retries):
        try:
            # Use continuation prompt if we have partial content
            if buffer.accumulated_text:
                messages = messages[:-1] + [{
                    "role": "user",
                    "content": buffer.continuation_prompt(original_user_content)
                }]
                print(f"\n[Retry {attempt}: continuing from {len(buffer.accumulated_text)} chars]")

            with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    buffer.append(text)
                    print(text, end="", flush=True)

            buffer.completed = True
            return buffer.accumulated_text

        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            buffer.error = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"\n[Stream error: {e}. Retrying in {wait}s...]")
                time.sleep(wait)
            else:
                print(f"\n[Max retries reached. Returning partial content.]")
                return buffer.accumulated_text

        except anthropic.APIStatusError as e:
            if e.status_code in (529, 503):  # Overloaded
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            raise

    return buffer.accumulated_text


# Usage
client = anthropic.Anthropic()
result = stream_with_recovery(
    client=client,
    messages=[{"role": "user", "content": "Write a complete Python implementation of a binary search tree with insert, search, and delete methods."}],
    max_tokens=2048,
)
print(f"\n\n[Total: {len(result)} chars]")

# Expected Token Savings: 40-80% on retry — only pays for tokens after interruption point
# Environment: Long code generation, document drafting, analysis tasks over slow/unreliable networks
```

### Option 2: Checkpoint Saver — Save partial response to disk; resume across process restarts

```python
import anthropic
import json
import hashlib
import time
from pathlib import Path

CHECKPOINT_DIR = Path("/tmp/stream_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)


def request_key(messages: list[dict], model: str) -> str:
    content = json.dumps({"messages": messages, "model": model}, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def load_checkpoint(key: str) -> str:
    path = CHECKPOINT_DIR / f"{key}.txt"
    if path.exists():
        return path.read_text()
    return ""


def save_checkpoint(key: str, text: str) -> None:
    path = CHECKPOINT_DIR / f"{key}.txt"
    path.write_text(text)


def clear_checkpoint(key: str) -> None:
    path = CHECKPOINT_DIR / f"{key}.txt"
    path.unlink(missing_ok=True)


def stream_with_checkpoint(
    client: anthropic.Anthropic,
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    max_retries: int = 3,
) -> str:
    key = request_key(messages, model)
    accumulated = load_checkpoint(key)

    if accumulated:
        print(f"[Resuming from checkpoint: {len(accumulated)} chars already saved]")

    original_content = messages[-1]["content"]

    for attempt in range(max_retries):
        try:
            # Build request — continue from checkpoint if available
            current_messages = messages
            if accumulated:
                continuation = (
                    f"{original_content}\n\n"
                    f"[RESUME] You started writing a response. Continue from where you stopped. "
                    f"Do NOT repeat already-written content. Already written:\n\n{accumulated}"
                )
                current_messages = messages[:-1] + [{"role": "user", "content": continuation}]

            new_text = ""
            with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                messages=current_messages,
            ) as stream:
                for chunk in stream.text_stream:
                    new_text += chunk
                    accumulated_total = accumulated + new_text
                    # Save checkpoint every 500 chars
                    if len(new_text) % 500 < len(chunk):
                        save_checkpoint(key, accumulated_total)
                    print(chunk, end="", flush=True)

            final = accumulated + new_text
            clear_checkpoint(key)  # Clean up on success
            return final

        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            # Save what we have and retry
            save_checkpoint(key, accumulated + new_text if 'new_text' in dir() else accumulated)
            accumulated = load_checkpoint(key)
            print(f"\n[Checkpoint saved ({len(accumulated)} chars). Retrying in {2**attempt}s...]")
            time.sleep(2 ** attempt)

    return accumulated


# Usage
client = anthropic.Anthropic()
# If this process is killed mid-stream and rerun, it resumes from checkpoint
result = stream_with_checkpoint(
    client=client,
    messages=[{"role": "user", "content": "Write a detailed guide to PostgreSQL indexing strategies."}],
    max_tokens=4096,
)
print(f"\n[Final: {len(result)} chars]")

# Expected Token Savings: Near 100% savings on resumed runs after crash/restart
# Environment: Long-form content generation, batch jobs, CI pipeline agents, overnight runs
```

### Option 3: Sliding Window Splitter — Break long requests into chunks; recover per-chunk

```python
import anthropic
import time
from dataclasses import dataclass

CHUNK_TOKEN_TARGET = 500  # Aim for ~500 output tokens per chunk


@dataclass
class ChunkResult:
    index: int
    content: str
    completed: bool = True


def stream_chunk(
    client: anthropic.Anthropic,
    messages: list[dict],
    model: str,
    max_tokens: int,
    stop_sequences: list[str] | None = None,
) -> tuple[str, bool]:
    """Stream one chunk. Returns (text, stopped_by_sequence)."""
    text = ""
    try:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            stop_sequences=stop_sequences or ["[CHUNK_END]"],
        ) as stream:
            for chunk in stream.text_stream:
                text += chunk
                print(chunk, end="", flush=True)

        final = stream.get_final_message()
        stopped = final.stop_reason == "stop_sequence"
        return text, stopped

    except (anthropic.APIConnectionError, anthropic.APITimeoutError):
        return text, False


def chunked_stream_with_recovery(
    client: anthropic.Anthropic,
    prompt: str,
    model: str = "claude-sonnet-4-6",
    total_max_tokens: int = 4096,
    chunk_size: int = CHUNK_TOKEN_TARGET,
    max_chunk_retries: int = 3,
) -> str:
    chunks: list[ChunkResult] = []
    chunk_index = 0
    tokens_remaining = total_max_tokens

    system = (
        "You are generating a long response in chunks. "
        "After each chunk of approximately 500 tokens, write [CHUNK_END] on its own line. "
        "Continue from where you left off when given previous content."
    )

    messages = [{"role": "user", "content": prompt}]

    while tokens_remaining > 0:
        chunk_tokens = min(chunk_size + 50, tokens_remaining)  # +50 for [CHUNK_END]

        for attempt in range(max_chunk_retries):
            text, hit_stop = stream_chunk(
                client=client,
                messages=messages,
                model=model,
                max_tokens=chunk_tokens,
            )

            if text:
                break  # Got some content

            if attempt < max_chunk_retries - 1:
                time.sleep(2 ** attempt)
                print(f"\n[Chunk {chunk_index} retry {attempt+1}]")

        # Strip the stop sequence from output
        content = text.replace("[CHUNK_END]", "").strip()
        chunks.append(ChunkResult(index=chunk_index, content=content))
        chunk_index += 1
        tokens_remaining -= chunk_tokens

        if not hit_stop:
            break  # Natural completion or max tokens

        # Continue: add accumulated content as context
        full_so_far = "\n".join(c.content for c in chunks)
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": full_so_far},
            {"role": "user", "content": "Continue from where you left off."},
        ]

    return "\n".join(c.content for c in chunks)


# Usage
client = anthropic.Anthropic()
result = chunked_stream_with_recovery(
    client=client,
    prompt="Write a complete tutorial on building a REST API with FastAPI, including authentication, database integration, and deployment.",
    total_max_tokens=4096,
    chunk_size=500,
)
print(f"\n\nTotal: {len(result)} chars across multiple chunks")

# Expected Token Savings: 50-90% on chunk retries; only failed chunk is re-generated
# Environment: Very long responses (>2000 tokens), tutorials, technical documentation generation
```

### Option 4: SSE-Level Recovery — Reconnect to SSE stream using Last-Event-ID header

```python
import anthropic
import time
from dataclasses import dataclass, field

# NOTE: The Anthropic SDK manages SSE internally. This pattern implements
# application-level recovery on top of the SDK's stream abstraction.

@dataclass
class StreamSession:
    prompt_messages: list[dict]
    model: str
    max_tokens: int
    received_text: str = ""
    attempt: int = 0
    max_attempts: int = 4

    @property
    def is_empty(self) -> bool:
        return not self.received_text.strip()

    def continuation_messages(self) -> list[dict]:
        if self.is_empty:
            return self.prompt_messages

        # Inject the partial response as an assistant turn, then ask to continue
        return self.prompt_messages + [
            {"role": "assistant", "content": self.received_text},
            {"role": "user", "content": "Your response was cut off. Please continue exactly from where you stopped."},
        ]


def resilient_stream(session: StreamSession, client: anthropic.Anthropic) -> str:
    while session.attempt < session.max_attempts:
        session.attempt += 1
        new_text = ""

        try:
            with client.messages.stream(
                model=session.model,
                max_tokens=session.max_tokens,
                messages=session.continuation_messages(),
            ) as stream:
                for chunk in stream.text_stream:
                    new_text += chunk
                    print(chunk, end="", flush=True)

            session.received_text += new_text
            return session.received_text  # Complete

        except anthropic.APIConnectionError as e:
            session.received_text += new_text
            backoff = min(2 ** session.attempt, 30)
            print(f"\n[Connection error on attempt {session.attempt}: {e}. "
                  f"Saved {len(session.received_text)} chars. Retrying in {backoff}s]")
            time.sleep(backoff)

        except anthropic.APITimeoutError as e:
            session.received_text += new_text
            backoff = min(2 ** session.attempt, 30)
            print(f"\n[Timeout on attempt {session.attempt}. "
                  f"Saved {len(session.received_text)} chars. Retrying in {backoff}s]")
            time.sleep(backoff)

        except anthropic.RateLimitError:
            time.sleep(60)  # Rate limit: wait longer

        except anthropic.APIStatusError as e:
            if e.status_code == 529:  # Overloaded
                time.sleep(30)
                continue
            raise  # Non-retryable

    print(f"\n[Gave up after {session.max_attempts} attempts]")
    return session.received_text


# Usage
client = anthropic.Anthropic()
session = StreamSession(
    prompt_messages=[{
        "role": "user",
        "content": "Write a comprehensive comparison of PostgreSQL vs MongoDB for a fintech application."
    }],
    model="claude-sonnet-4-6",
    max_tokens=3000,
)
result = resilient_stream(session, client)
print(f"\n\nFinal ({len(result)} chars, {session.attempt} attempt(s))")

# Expected Token Savings: 60-85% on retries by using partial assistant turn as context
# Environment: Unreliable networks, mobile apps, high-latency connections, API overload periods
```

### Option 5: Multi-Turn Streaming with Interrupt Detection — Detect truncation and self-heal

```python
import anthropic
from dataclasses import dataclass, field
import time
import re

TRUNCATION_SIGNALS = [
    r"\.\.\.$",              # Ends with ellipsis
    r"\[truncated\]$",       # Explicit truncation marker
    r"```\s*$",              # Unclosed code block
    r"\(\s*$",               # Unclosed parenthesis
    r",\s*$",                # Sentence ending with comma
    r"\band\s*$",            # Ends mid-clause
]


def is_truncated(text: str) -> bool:
    text = text.strip()
    if not text:
        return True
    for pattern in TRUNCATION_SIGNALS:
        if re.search(pattern, text, re.MULTILINE):
            return True
    # Check for unclosed code blocks
    triple_backtick_count = text.count("```")
    if triple_backtick_count % 2 != 0:
        return True
    return False


@dataclass
class SelfHealingStream:
    client: anthropic.Anthropic
    model: str = "claude-sonnet-4-6"
    max_tokens_per_turn: int = 2000
    max_heal_attempts: int = 3

    def generate(self, messages: list[dict]) -> str:
        history = list(messages)
        full_response = ""
        heal_count = 0

        while heal_count <= self.max_heal_attempts:
            chunk_text = ""
            error_occurred = False

            try:
                with self.client.messages.stream(
                    model=self.model,
                    max_tokens=self.max_tokens_per_turn,
                    messages=history,
                ) as stream:
                    for text in stream.text_stream:
                        chunk_text += text
                        print(text, end="", flush=True)

            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
                error_occurred = True
                print(f"\n[Stream error: {e}]")

            full_response += chunk_text

            # Check if we should continue
            if not error_occurred and not is_truncated(chunk_text):
                break  # Clean completion

            if heal_count >= self.max_heal_attempts:
                print(f"\n[Max heal attempts reached]")
                break

            heal_count += 1
            truncated_reason = "connection error" if error_occurred else "detected truncation"
            print(f"\n[{truncated_reason} — self-healing attempt {heal_count}]")

            # Add what we got as assistant turn, ask to continue
            history.append({"role": "assistant", "content": full_response})
            history.append({
                "role": "user",
                "content": (
                    "Your response appears to be incomplete. "
                    "Please continue from where you left off without repeating yourself."
                )
            })
            time.sleep(1)

        return full_response


# Usage
client_wrapper = SelfHealingStream(client=anthropic.Anthropic(), max_tokens_per_turn=1500)
result = client_wrapper.generate([
    {"role": "user", "content": "Write complete Python code for a web scraper that handles pagination, rate limiting, and retry logic."}
])
print(f"\n\nFinal: {len(result)} chars, truncation-healed")

# Expected Token Savings: 30-60% by healing in-place rather than full restart
# Environment: Code generation, structured document output, any response with detectable incompleteness
```

### Option 6: Async Parallel Streams with Failover — Race two streams; use whichever completes first

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

@dataclass
class StreamRacer:
    """Race two parallel streams. First complete response wins; other is cancelled."""
    client: anthropic.AsyncAnthropic
    model_primary: str = "claude-sonnet-4-6"
    model_fallback: str = "claude-haiku-4-5-20251001"
    delay_before_fallback: float = 3.0  # seconds before starting fallback stream

    async def _stream_to_string(self, messages: list[dict], model: str,
                                 max_tokens: int) -> str:
        text = ""
        async with self.client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        ) as stream:
            async for chunk in stream.text_stream:
                text += chunk
        return text

    async def _delayed_fallback(self, messages: list[dict], max_tokens: int) -> str:
        """Start fallback stream after a delay."""
        await asyncio.sleep(self.delay_before_fallback)
        return await self._stream_to_string(messages, self.model_fallback, max_tokens)

    async def race(self, messages: list[dict], max_tokens: int = 1024) -> tuple[str, str]:
        """Returns (result_text, winning_model)."""
        primary_task = asyncio.create_task(
            self._stream_to_string(messages, self.model_primary, max_tokens),
            name="primary"
        )
        fallback_task = asyncio.create_task(
            self._delayed_fallback(messages, max_tokens),
            name="fallback"
        )

        done, pending = await asyncio.wait(
            [primary_task, fallback_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel the losing task
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        completed = done.pop()
        winner = completed.get_name()

        try:
            result = completed.result()
            return result, winner
        except Exception as e:
            # Winner also failed — try remaining pending (already cancelled, so re-run)
            print(f"[Both streams failed: {e}. Falling back to sequential retry.]")
            return await self._stream_to_string(messages, self.model_fallback, max_tokens), "fallback-sequential"


async def main():
    client = anthropic.AsyncAnthropic()
    racer = StreamRacer(client=client, delay_before_fallback=2.0)

    messages = [{"role": "user", "content": "Explain the CAP theorem with examples."}]

    result, winner = await racer.race(messages, max_tokens=512)
    print(result)
    print(f"\n[Won by: {winner}]")


asyncio.run(main())

# Expected Token Savings: Pays for primary only (usually); fallback cost only on primary failure/slowness
# Environment: Latency-sensitive applications, high-availability systems, SLA-bound API endpoints
```

## Comparison

| Option | Recovery Scope | Persistence | Cost Savings on Retry | Complexity |
|--------|---------------|-------------|----------------------|------------|
| Accumulate-and-Retry | Session (in-memory) | None | 40-80% | Low |
| Checkpoint Saver | Cross-process | Disk | ~100% on resume | Medium |
| Chunk Splitter | Per-chunk | None | 50-90% per chunk | Medium |
| SSE-Level Recovery | Session | None | 60-85% | Medium |
| Self-Healing Stream | Truncation detection | None | 30-60% | Medium |
| Async Parallel Failover | Concurrent | None | 0-50% (dual cost) | High |
