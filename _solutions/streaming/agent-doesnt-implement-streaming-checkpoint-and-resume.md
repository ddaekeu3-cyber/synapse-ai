---
title: "Agent Doesn't Implement Streaming Checkpoint and Resume"
description: "Long streaming responses that are interrupted mid-way are lost entirely. Checkpointing partial streamed output lets agents resume from where they left off instead of restarting from scratch."
difficulty: intermediate
category: streaming
tags: [streaming, checkpoint, resume, fault-tolerance, partial-output, reliability]
---

## Problem

A streaming response that takes 30 seconds to generate fails at second 25 — a network hiccup, client disconnect, or timeout — and the entire output is lost. The agent restarts from zero, paying full token costs again and making the user wait twice. Streaming checkpoints save accumulated output at intervals so a resume can skip already-delivered content.

```python
# BAD: all-or-nothing streaming, lost on interruption
async def stream_response(prompt: str) -> str:
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        result = ""
        async for text in stream.text_stream:
            result += text
        return result  # lost if interrupted before here
```

## Solution 1: File-Based Streaming Checkpoint

Save streamed chunks to disk periodically and resume from the last checkpoint.

```python
import asyncio
import json
import os
import time
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

CHECKPOINT_DIR = Path("/tmp/stream_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)

def checkpoint_path(session_id: str) -> Path:
    return CHECKPOINT_DIR / f"{session_id}.json"

def save_checkpoint(session_id: str, accumulated: str, metadata: dict):
    data = {
        "accumulated": accumulated,
        "char_count": len(accumulated),
        "timestamp": time.time(),
        **metadata
    }
    path = checkpoint_path(session_id)
    path.write_text(json.dumps(data))

def load_checkpoint(session_id: str) -> dict | None:
    path = checkpoint_path(session_id)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None

def clear_checkpoint(session_id: str):
    path = checkpoint_path(session_id)
    if path.exists():
        path.unlink()

async def stream_with_checkpoint(
    session_id: str,
    prompt: str,
    checkpoint_interval: int = 200,  # chars
    model: str = "claude-haiku-4-5-20251001"
) -> str:
    # Check for existing checkpoint
    checkpoint = load_checkpoint(session_id)
    accumulated = ""
    resume_hint = ""

    if checkpoint:
        accumulated = checkpoint["accumulated"]
        resume_hint = (
            f"\n\n[RESUME] You were generating a response and were interrupted. "
            f"Here is what you had so far (do NOT repeat it, continue from where you left off):\n\n"
            f"{accumulated}\n\n[Continue from here:]"
        )
        print(f"[Checkpoint] Resuming from {len(accumulated)} chars")

    full_prompt = prompt + resume_hint if resume_hint else prompt
    chars_since_checkpoint = 0

    try:
        async with client.messages.stream(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": full_prompt}]
        ) as stream:
            async for text in stream.text_stream:
                accumulated += text
                chars_since_checkpoint += len(text)

                if chars_since_checkpoint >= checkpoint_interval:
                    save_checkpoint(session_id, accumulated, {"prompt": prompt[:100]})
                    chars_since_checkpoint = 0

        clear_checkpoint(session_id)
        return accumulated

    except Exception as e:
        # Save what we have before propagating
        save_checkpoint(session_id, accumulated, {"prompt": prompt[:100], "error": str(e)})
        print(f"[Checkpoint] Saved at {len(accumulated)} chars after error: {e}")
        raise

async def main():
    session = "demo-session-001"
    result = await stream_with_checkpoint(
        session,
        "Write a detailed explanation of how transformers work in deep learning.",
        checkpoint_interval=150
    )
    print(f"Total chars: {len(result)}")
    print(result[:300])

asyncio.run(main())
```

## Solution 2: Redis-Backed Distributed Checkpoint

For multi-instance deployments where any worker can resume.

```python
import asyncio
import json
import time
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Simulated Redis client (replace with `import redis.asyncio as redis`)
class FakeRedis:
    _store: dict = {}

    async def set(self, key: str, value: str, ex: int | None = None):
        self._store[key] = value

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def delete(self, key: str):
        self._store.pop(key, None)

redis_client = FakeRedis()

async def redis_stream_with_checkpoint(
    session_id: str,
    prompt: str,
    ttl_seconds: int = 3600,
    chunk_size: int = 256
) -> str:
    key = f"stream:checkpoint:{session_id}"

    # Load checkpoint
    existing = await redis_client.get(key)
    accumulated = ""
    messages: list[dict] = [{"role": "user", "content": prompt}]

    if existing:
        data = json.loads(existing)
        accumulated = data["accumulated"]
        print(f"[Redis Checkpoint] Resuming: {len(accumulated)} chars from {data['timestamp']:.0f}")
        messages.append({"role": "assistant", "content": accumulated})
        messages.append({"role": "user", "content": "Continue exactly where you left off."})

    new_content = ""
    chars_since_save = 0

    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=messages
        ) as stream:
            async for text in stream.text_stream:
                new_content += text
                chars_since_save += len(text)

                if chars_since_save >= chunk_size:
                    checkpoint_data = json.dumps({
                        "accumulated": accumulated + new_content,
                        "timestamp": time.time(),
                        "session_id": session_id
                    })
                    await redis_client.set(key, checkpoint_data, ex=ttl_seconds)
                    chars_since_save = 0

        await redis_client.delete(key)
        return accumulated + new_content

    except Exception as e:
        # Persist before re-raising
        checkpoint_data = json.dumps({
            "accumulated": accumulated + new_content,
            "timestamp": time.time(),
            "error": str(e)
        })
        await redis_client.set(key, checkpoint_data, ex=ttl_seconds)
        raise

async def main():
    result = await redis_stream_with_checkpoint(
        "user-123-task-456",
        "Explain the history of programming languages from Fortran to today."
    )
    print(f"Result length: {len(result)}")
    print(result[:200])

asyncio.run(main())
```

## Solution 3: Token-Position Resumption with Continuation Prompt

Resume by telling the model exactly where to continue using the last token boundary.

```python
import asyncio
import re
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def find_resume_point(text: str) -> tuple[str, str]:
    """
    Find the last complete sentence or paragraph boundary.
    Returns (clean_text, resume_from) where resume_from is a hint for the model.
    """
    # Try paragraph boundary first
    paragraphs = text.rsplit("\n\n", 1)
    if len(paragraphs) == 2 and len(paragraphs[0]) > 100:
        return paragraphs[0], paragraphs[1] or ""

    # Try sentence boundary
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(sentences) > 1:
        complete = " ".join(sentences[:-1])
        incomplete = sentences[-1]
        return complete, incomplete

    return text, ""

async def token_position_resume(
    session_id: str,
    prompt: str,
    existing_output: str | None = None
) -> str:
    """Resume streaming from a token position boundary."""
    if existing_output:
        clean, partial = find_resume_point(existing_output)
        print(f"[Resume] Clean: {len(clean)} chars, Partial fragment: '{partial[:50]}'")

        # Ask model to continue, giving it the clean portion
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": clean},
            {"role": "user", "content": "Please continue the response from where it was cut off."}
        ]
    else:
        messages = [{"role": "user", "content": prompt}]
        clean = ""

    new_output = ""
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=messages
    ) as stream:
        async for text in stream.text_stream:
            new_output += text

    return clean + new_output

async def simulate_interrupted_stream(prompt: str, interrupt_at: int = 200) -> str:
    """Simulate a stream that gets interrupted, then resume it."""
    partial_output = ""

    # First attempt - "interrupted"
    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            async for text in stream.text_stream:
                partial_output += text
                if len(partial_output) >= interrupt_at:
                    raise ConnectionError("Simulated network interruption")
    except ConnectionError:
        print(f"[Interrupted] at {len(partial_output)} chars")

    # Resume
    print("[Resuming from checkpoint...]")
    return await token_position_resume("sim-session", prompt, partial_output)

async def main():
    result = await simulate_interrupted_stream(
        "Write a step-by-step guide to building a REST API with Python.",
        interrupt_at=300
    )
    print(f"Final length: {len(result)}")
    print(result[:400])

asyncio.run(main())
```

## Solution 4: Streaming Delta Journal with Replay

Record each delta as a journal entry; replay the journal for instant resume.

```python
import asyncio
import json
import time
from pathlib import Path
from anthropic import AsyncAnthropic
from typing import AsyncIterator

client = AsyncAnthropic()
JOURNAL_DIR = Path("/tmp/stream_journals")
JOURNAL_DIR.mkdir(exist_ok=True)

class StreamJournal:
    def __init__(self, session_id: str):
        self.path = JOURNAL_DIR / f"{session_id}.jsonl"
        self._handle = None

    def open(self):
        self._handle = self.path.open("a")
        return self

    def write_delta(self, text: str, seq: int):
        if self._handle:
            entry = json.dumps({"seq": seq, "t": text, "ts": time.time()})
            self._handle.write(entry + "\n")
            self._handle.flush()

    def close(self):
        if self._handle:
            self._handle.close()

    def replay(self) -> str:
        """Reconstruct accumulated text from journal."""
        if not self.path.exists():
            return ""
        entries = []
        for line in self.path.read_text().splitlines():
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
        entries.sort(key=lambda e: e["seq"])
        return "".join(e["t"] for e in entries)

    def clear(self):
        if self.path.exists():
            self.path.unlink()

    def last_seq(self) -> int:
        text = self.replay()
        if not self.path.exists():
            return -1
        count = sum(1 for _ in self.path.read_text().splitlines() if _.strip())
        return count - 1

async def journaled_stream(
    session_id: str,
    prompt: str
) -> AsyncIterator[str]:
    journal = StreamJournal(session_id)
    existing = journal.replay()
    seq = journal.last_seq() + 1

    if existing:
        print(f"[Journal] Replaying {len(existing)} chars from journal")
        yield existing  # yield cached portion immediately

        # Resume generation
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": existing},
            {"role": "user", "content": "Continue."}
        ]
    else:
        messages = [{"role": "user", "content": prompt}]

    journal.open()
    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=messages
        ) as stream:
            async for text in stream.text_stream:
                journal.write_delta(text, seq)
                seq += 1
                yield text

        journal.clear()
    except Exception:
        journal.close()
        raise
    finally:
        journal.close()

async def main():
    session = "journal-demo-001"
    full_output = ""

    async for chunk in journaled_stream(session, "Describe the water cycle in detail."):
        full_output += chunk

    print(f"Total: {len(full_output)} chars")
    print(full_output[:300])

asyncio.run(main())
```

## Solution 5: Client-Side Progressive Delivery with Server Resume

Deliver chunks to the client as they arrive; client sends the offset for resume.

```python
import asyncio
import hashlib
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class StreamResumptionToken:
    prompt_hash: str
    delivered_chars: int
    delivered_text: str  # last 200 chars for overlap detection

def make_token(prompt: str, delivered: str) -> StreamResumptionToken:
    return StreamResumptionToken(
        prompt_hash=hashlib.sha256(prompt.encode()).hexdigest()[:16],
        delivered_chars=len(delivered),
        delivered_text=delivered[-200:] if len(delivered) > 200 else delivered
    )

async def stream_with_resumption_token(
    prompt: str,
    token: StreamResumptionToken | None = None
) -> tuple[str, StreamResumptionToken]:
    """
    Stream a response. If a token is provided, resume from that offset.
    Returns (new_content, new_token).
    """
    if token:
        # Validate prompt hash
        current_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        if current_hash != token.prompt_hash:
            raise ValueError("Resumption token is for a different prompt")

        # Build resume messages using the overlap to find join point
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": token.delivered_text},
            {"role": "user", "content": "Continue from where you left off. Do not repeat the previous text."}
        ]
        prefix = token.delivered_text
        print(f"[Resume] Continuing after {token.delivered_chars} delivered chars")
    else:
        messages = [{"role": "user", "content": prompt}]
        prefix = ""

    new_content = ""
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=messages
    ) as stream:
        async for text in stream.text_stream:
            new_content += text
            # In a real server, yield to client here

    full_output = prefix + new_content
    new_token = make_token(prompt, full_output)
    return new_content, new_token

async def main():
    prompt = "Explain machine learning in simple terms with examples."

    # Simulate first delivery (interrupted early)
    print("=== First attempt (simulated interrupt) ===")
    first_token = make_token(prompt, "Machine learning is a type of AI that")
    print(f"Token: delivered={first_token.delivered_chars} chars")

    # Resume
    print("\n=== Resuming ===")
    new_content, final_token = await stream_with_resumption_token(prompt, first_token)
    print(f"New content: {len(new_content)} chars")
    print(f"Total delivered: {final_token.delivered_chars} chars")
    print(new_content[:300])

asyncio.run(main())
```

## Solution 6: Chunked Streaming with Idempotent Delivery

Split generation into idempotent chunks; each chunk can be independently retried.

```python
import asyncio
import hashlib
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class StreamChunk:
    index: int
    content: str
    is_final: bool
    chunk_id: str = field(init=False)

    def __post_init__(self):
        self.chunk_id = hashlib.md5(
            f"{self.index}:{self.content[:50]}".encode()
        ).hexdigest()[:8]

class IdempotentChunkStream:
    def __init__(self, target_chunk_chars: int = 500):
        self.target_chunk_chars = target_chunk_chars
        self.delivered_chunks: set[str] = set()

    def is_delivered(self, chunk: StreamChunk) -> bool:
        return chunk.chunk_id in self.delivered_chunks

    def mark_delivered(self, chunk: StreamChunk):
        self.delivered_chunks.add(chunk.chunk_id)

    async def generate_chunks(
        self,
        prompt: str,
        start_chunk_index: int = 0
    ):
        """Generate content in logical chunks; skip already-delivered ones."""
        messages: list[dict] = [{"role": "user", "content": prompt}]

        # If resuming, first get already-delivered content as context
        if start_chunk_index > 0:
            resume_prompt = (
                f"{prompt}\n\n"
                f"[This is a resumed generation. You previously generated {start_chunk_index} chunks. "
                f"Continue generating the remaining content without repeating what came before. "
                f"Start with chunk {start_chunk_index + 1}.]"
            )
            messages = [{"role": "user", "content": resume_prompt}]

        buffer = ""
        chunk_index = start_chunk_index
        full_text = ""

        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            messages=messages
        ) as stream:
            async for text in stream.text_stream:
                buffer += text
                full_text += text

                while len(buffer) >= self.target_chunk_chars:
                    # Find a good split point (sentence boundary)
                    split_at = self.target_chunk_chars
                    for i in range(min(split_at + 100, len(buffer)), split_at - 1, -1):
                        if i < len(buffer) and buffer[i] in ".!?\n":
                            split_at = i + 1
                            break

                    chunk_content = buffer[:split_at]
                    buffer = buffer[split_at:]
                    chunk = StreamChunk(chunk_index, chunk_content, False)

                    if not self.is_delivered(chunk):
                        self.mark_delivered(chunk)
                        yield chunk

                    chunk_index += 1

            # Yield remaining buffer as final chunk
            if buffer:
                chunk = StreamChunk(chunk_index, buffer, True)
                if not self.is_delivered(chunk):
                    self.mark_delivered(chunk)
                    yield chunk

async def main():
    streamer = IdempotentChunkStream(target_chunk_chars=200)
    all_content = ""
    chunk_count = 0

    async for chunk in streamer.generate_chunks(
        "Write a short story about a robot learning to paint.",
        start_chunk_index=0
    ):
        all_content += chunk.content
        chunk_count += 1
        print(f"[Chunk {chunk.index}] {len(chunk.content)} chars, id={chunk.chunk_id}, final={chunk.is_final}")

    print(f"\nTotal: {chunk_count} chunks, {len(all_content)} chars")
    print(all_content[:300])

asyncio.run(main())
```

## Comparison

| Approach | Storage | Resume Fidelity | Overhead | Best For |
|---|---|---|---|---|
| File-Based Checkpoint | Local disk | High | Low | Single-instance, dev |
| Redis Distributed | Redis | High | Low | Multi-instance prod |
| Token-Position Resume | None (stateless) | Medium | Low | Stateless APIs |
| Delta Journal | Local disk | Exact | Medium | Audit/replay needs |
| Client-Side Token | Client state | Medium | Low | Browser/mobile clients |
| Idempotent Chunks | In-memory | High | Medium | Unreliable networks |

**Rule of thumb**: Use file-based checkpoints for single-server deployments, Redis for distributed systems, and client-side tokens for browser clients where server storage is impractical.
