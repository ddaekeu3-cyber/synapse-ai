---
layout: solution
title: "Agent Doesn't Implement Streaming Retry on Incomplete Response"
category: streaming
description: "Detect when a streaming response is cut short by network errors or server disconnects and retry from the last complete chunk, avoiding full regeneration."
tags: [streaming, retry, incomplete, recovery, resilience, network]
---

# Agent Doesn't Implement Streaming Retry on Incomplete Response

Streaming API responses can be interrupted mid-stream by network blips, server restarts, or client-side timeouts. Without retry logic, the caller gets a truncated response and the user sees partial output. A naive retry regenerates the entire response from scratch, wasting tokens. Smart streaming retry checkpoints completed chunks and resumes from the last good position or, when resumption is impossible, performs a targeted retry for only the missing portion.

## Option 1: Detect Incomplete Stream and Full Retry

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MAX_RETRIES = 3
MIN_COMPLETE_CHARS = 20  # A response shorter than this is likely incomplete


async def stream_with_retry(messages: list[dict], system: str = "") -> str:
    """Stream response, retrying on disconnect or incomplete output."""
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        collected: list[str] = []
        completed = False

        try:
            kwargs: dict = dict(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=messages,
            )
            if system:
                kwargs["system"] = system

            async with client.messages.stream(**kwargs) as stream:
                async for chunk in stream.text_stream:
                    collected.append(chunk)
                    print(chunk, end="", flush=True)

                # Check stop reason on the final message
                final = await stream.get_final_message()
                if final.stop_reason in ("end_turn", "max_tokens"):
                    completed = True

        except (asyncio.TimeoutError, Exception) as e:
            last_error = e
            print(f"\n[attempt {attempt}] Stream interrupted: {type(e).__name__}: {e}")

        result = "".join(collected)

        if completed and len(result) >= MIN_COMPLETE_CHARS:
            print()  # newline after stream
            return result

        print(f"\n[attempt {attempt}] Incomplete ({len(result)} chars). Retrying...")
        await asyncio.sleep(0.5 * attempt)  # backoff

    raise RuntimeError(f"Stream failed after {MAX_RETRIES} attempts. Last error: {last_error}")


async def main() -> None:
    messages = [{"role": "user", "content": "Explain asyncio event loops in Python."}]
    try:
        result = await stream_with_retry(messages)
        print(f"\n[complete] {len(result)} chars received")
    except RuntimeError as e:
        print(f"[failed] {e}")


asyncio.run(main())

# Expected Token Savings: Retries cost full tokens; but this is correct behavior — partial answers are worth less than a complete retry
# Environment: Python 3.11+; tune MIN_COMPLETE_CHARS based on typical response length; consider stop_reason == "max_tokens" as complete
```

## Option 2: Checkpoint Chunks and Resume with Continuation Prompt

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MAX_RETRIES = 3
CHUNK_CHECKPOINT_EVERY = 50  # Save checkpoint every N characters


async def stream_with_checkpoint(messages: list[dict]) -> str:
    """
    Stream with checkpointing. On failure, ask the model to continue
    from where it left off rather than regenerating from scratch.
    """
    checkpoint: str = ""  # Last saved complete portion
    full_result: str = ""
    current_messages = list(messages)

    for attempt in range(1, MAX_RETRIES + 1):
        collected: list[str] = []
        interrupted = False

        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=current_messages,
            ) as stream:
                async for chunk in stream.text_stream:
                    collected.append(chunk)
                    current_text = "".join(collected)

                    # Update checkpoint every N chars
                    if len(current_text) - len(checkpoint) >= CHUNK_CHECKPOINT_EVERY:
                        checkpoint = current_text
                        print(".", end="", flush=True)  # progress indicator
                    print(chunk, end="", flush=True)

                final = await stream.get_final_message()
                if final.stop_reason in ("end_turn", "max_tokens"):
                    full_result += "".join(collected)
                    print(f"\n[complete] attempt={attempt} total_len={len(full_result)}")
                    return full_result
                else:
                    interrupted = True

        except Exception as e:
            interrupted = True
            print(f"\n[attempt {attempt}] Error: {e}")

        if interrupted and checkpoint:
            # Build continuation prompt from what we have so far
            full_result += checkpoint
            print(f"\n[resume] Checkpointed {len(checkpoint)} chars. Asking model to continue...")
            current_messages = list(messages) + [
                {"role": "assistant", "content": full_result},
                {"role": "user", "content": "Please continue exactly where you left off."},
            ]
            checkpoint = ""
            collected = []
            await asyncio.sleep(0.5 * attempt)
        elif interrupted:
            print(f"\n[attempt {attempt}] No checkpoint. Full retry...")
            await asyncio.sleep(0.5 * attempt)

    return full_result or "".join(collected)


async def main() -> None:
    messages = [{"role": "user", "content": "List 10 Python async best practices with explanations."}]
    result = await stream_with_checkpoint(messages)
    print(f"\nFinal result ({len(result)} chars):\n{result[:300]}")


asyncio.run(main())

# Expected Token Savings: 40-70% on retry vs full regeneration — continuation only generates the missing portion
# Environment: Python 3.11+; continuation quality depends on model following "continue where you left off" accurately
```

## Option 3: Timeout-Protected Stream with Partial Fallback

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

STREAM_TIMEOUT = 30.0   # Total stream budget
CHUNK_TIMEOUT = 5.0     # Max silence between chunks
MAX_RETRIES = 2


async def stream_with_chunk_timeout(messages: list[dict]) -> tuple[str, str]:
    """
    Stream with per-chunk timeout detection.
    Returns (text, status) where status is "complete" | "timeout" | "partial".
    """
    collected: list[str] = []
    last_chunk_time = asyncio.get_event_loop().time()

    async def watchdog() -> None:
        """Cancel stream if no chunks arrive within CHUNK_TIMEOUT."""
        while True:
            await asyncio.sleep(1.0)
            idle = asyncio.get_event_loop().time() - last_chunk_time
            if idle > CHUNK_TIMEOUT:
                raise asyncio.TimeoutError(f"No chunk received for {idle:.1f}s")

    try:
        async with asyncio.timeout(STREAM_TIMEOUT):
            watchdog_task = asyncio.create_task(watchdog())
            try:
                async with client.messages.stream(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=messages,
                ) as stream:
                    async for chunk in stream.text_stream:
                        collected.append(chunk)
                        last_chunk_time = asyncio.get_event_loop().time()
                        print(chunk, end="", flush=True)

                    final = await stream.get_final_message()
                    watchdog_task.cancel()
                    print()
                    stop = final.stop_reason
                    return "".join(collected), "complete" if stop in ("end_turn", "max_tokens") else "partial"
            finally:
                if not watchdog_task.done():
                    watchdog_task.cancel()

    except asyncio.TimeoutError:
        print(f"\n[timeout] Collected {len(''.join(collected))} chars before timeout")
        return "".join(collected), "timeout"


async def smart_retry(messages: list[dict]) -> str:
    """Use timeout-aware streaming; fall back to non-streaming on repeated timeout."""
    for attempt in range(1, MAX_RETRIES + 1):
        text, status = await stream_with_chunk_timeout(messages)

        if status == "complete":
            return text

        print(f"[attempt {attempt}] Status={status}. ", end="")

        if status == "timeout" and attempt == MAX_RETRIES:
            # Fall back: non-streaming guaranteed response
            print("Falling back to non-streaming...")
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=messages,
            )
            return response.content[0].text

        print("Retrying stream...")
        await asyncio.sleep(1.0 * attempt)

    return text  # Return best partial result


async def main() -> None:
    messages = [{"role": "user", "content": "Describe the asyncio event loop lifecycle."}]
    result = await smart_retry(messages)
    print(f"\nResult: {result[:300]}")


asyncio.run(main())

# Expected Token Savings: Non-streaming fallback costs same tokens but is guaranteed to complete; avoids infinite retry loops
# Environment: Python 3.11+; asyncio.timeout requires Python 3.11+; use asyncio.wait_for on older versions
```

## Option 4: SQLite-Tracked Stream Sessions with Resume

```python
import asyncio
import sqlite3
import json
import time
import anthropic

client = anthropic.AsyncAnthropic()
DB_PATH = ":memory:"


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stream_sessions (
            session_id TEXT PRIMARY KEY,
            messages_json TEXT NOT NULL,
            collected_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'in_progress',
            attempt INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    conn.commit()


def save_progress(conn: sqlite3.Connection, session_id: str, text: str, status: str, attempt: int) -> None:
    conn.execute(
        "UPDATE stream_sessions SET collected_text=?, status=?, attempt=?, updated_at=? WHERE session_id=?",
        (text, status, attempt, time.time(), session_id)
    )
    conn.commit()


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM stream_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not row:
        return None
    cols = ["session_id", "messages_json", "collected_text", "status", "attempt", "created_at", "updated_at"]
    return dict(zip(cols, row))


async def stream_session(conn: sqlite3.Connection, session_id: str,
                         messages: list[dict], max_retries: int = 3) -> str:
    # Initialize or resume session
    existing = get_session(conn, session_id)
    if existing and existing["status"] == "complete":
        print(f"[resume] Session {session_id} already complete")
        return existing["collected_text"]

    if not existing:
        conn.execute(
            "INSERT INTO stream_sessions VALUES (?,?,?,?,?,?,?)",
            (session_id, json.dumps(messages), "", "in_progress", 1, time.time(), time.time())
        )
        conn.commit()
        checkpoint_text = ""
        attempt = 1
    else:
        checkpoint_text = existing["collected_text"]
        attempt = existing["attempt"]
        print(f"[resume] Found {len(checkpoint_text)} chars from previous attempt {attempt}")

    for current_attempt in range(attempt, max_retries + 1):
        save_progress(conn, session_id, checkpoint_text, "in_progress", current_attempt)
        collected = [checkpoint_text] if checkpoint_text else []

        # Build messages for this attempt
        current_msgs = list(messages)
        if checkpoint_text:
            current_msgs += [
                {"role": "assistant", "content": checkpoint_text},
                {"role": "user", "content": "Continue."},
            ]

        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=current_msgs,
            ) as stream:
                async for chunk in stream.text_stream:
                    collected.append(chunk)
                    print(chunk, end="", flush=True)

                final = await stream.get_final_message()
                full = "".join(collected)
                if final.stop_reason in ("end_turn", "max_tokens"):
                    save_progress(conn, session_id, full, "complete", current_attempt)
                    print(f"\n[complete] session={session_id} attempt={current_attempt} len={len(full)}")
                    return full

        except Exception as e:
            partial = "".join(collected)
            print(f"\n[error] attempt={current_attempt}: {e}")
            save_progress(conn, session_id, partial, "interrupted", current_attempt)
            checkpoint_text = partial
            await asyncio.sleep(0.5 * current_attempt)

    final_text = "".join(collected) if collected else checkpoint_text
    save_progress(conn, session_id, final_text, "failed", max_retries)
    return final_text


async def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    result = await stream_session(
        conn, "session-abc-123",
        [{"role": "user", "content": "Explain Python generators in detail."}]
    )
    print(f"\nResult ({len(result)} chars): {result[:200]}")

    # Second call — returns from DB immediately
    result2 = await stream_session(
        conn, "session-abc-123",
        [{"role": "user", "content": "Explain Python generators in detail."}]
    )
    print(f"Cached result: {result2[:50]}...")


asyncio.run(main())

# Expected Token Savings: 40-60% on resume — only generates missing portion; DB prevents duplicate work across restarts
# Environment: Python 3.11+; replace :memory: with persistent DB; use session_id = hash(user_id + conversation_id)
```

## Option 5: Exponential Backoff Stream Retry with Jitter

```python
import asyncio
import random
import anthropic

client = anthropic.AsyncAnthropic()


async def stream_with_backoff(
    messages: list[dict],
    max_retries: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 16.0,
) -> str:
    """Stream with exponential backoff + full jitter on failure."""

    def jitter(attempt: int) -> float:
        """Full jitter: random value in [0, min(max_delay, base * 2^attempt)]."""
        cap = min(max_delay, base_delay * (2 ** attempt))
        return random.uniform(0, cap)

    last_partial = ""
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        collected: list[str] = []
        stop_reason = None

        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=messages,
            ) as stream:
                async for chunk in stream.text_stream:
                    collected.append(chunk)
                    print(chunk, end="", flush=True)

                final = await stream.get_final_message()
                stop_reason = final.stop_reason

        except Exception as e:
            last_exc = e
            last_partial = "".join(collected)
            delay = jitter(attempt)
            print(f"\n[attempt {attempt+1}/{max_retries}] {type(e).__name__}. Retry in {delay:.2f}s...")
            await asyncio.sleep(delay)
            continue

        result = "".join(collected)

        if stop_reason in ("end_turn", "max_tokens"):
            print()
            return result

        # stop_reason is something unexpected
        last_partial = result
        delay = jitter(attempt)
        print(f"\n[attempt {attempt+1}] Unexpected stop_reason={stop_reason}. Retry in {delay:.2f}s...")
        await asyncio.sleep(delay)

    # All retries exhausted — return best partial
    if last_partial:
        print(f"\n[exhausted] Returning partial result ({len(last_partial)} chars)")
        return last_partial

    raise RuntimeError(f"Stream failed after {max_retries} attempts") from last_exc


async def main() -> None:
    messages = [{"role": "user", "content": "Write a detailed guide to Python error handling."}]
    result = await stream_with_backoff(messages, max_retries=3, base_delay=0.5)
    print(f"\n[done] {len(result)} chars")


asyncio.run(main())

# Expected Token Savings: N/A for retries; full jitter prevents thundering herd when many clients retry simultaneously
# Environment: Python 3.11+; for rate limit errors (429), parse Retry-After header and use that delay instead of jitter
```

## Option 6: Multi-Segment Stream with Per-Segment Integrity Check

```python
import asyncio
import hashlib
import anthropic

client = anthropic.AsyncAnthropic()

SEGMENT_SIZE = 200  # Characters per segment for integrity checking


class IntegrityCheckedStream:
    """
    Streams response in segments, verifying each segment's integrity before proceeding.
    On corruption or truncation, retries only the affected segment.
    """

    def __init__(self) -> None:
        self.segments: list[tuple[str, str]] = []  # (text, sha256[:8])
        self.total_chars = 0

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:8]

    def add_segment(self, text: str) -> None:
        h = self._hash(text)
        self.segments.append((text, h))
        self.total_chars += len(text)

    def verify_all(self) -> list[int]:
        """Return indices of corrupted segments."""
        bad = []
        for i, (text, stored_hash) in enumerate(self.segments):
            if self._hash(text) != stored_hash:
                bad.append(i)
        return bad

    def full_text(self) -> str:
        return "".join(text for text, _ in self.segments)


async def segmented_stream(messages: list[dict], max_retries: int = 3) -> str:
    checker = IntegrityCheckedStream()

    for attempt in range(1, max_retries + 1):
        buffer = ""
        completed = False

        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=messages,
            ) as stream:
                async for chunk in stream.text_stream:
                    buffer += chunk
                    print(chunk, end="", flush=True)

                    # Flush complete segments
                    while len(buffer) >= SEGMENT_SIZE:
                        segment = buffer[:SEGMENT_SIZE]
                        buffer = buffer[SEGMENT_SIZE:]
                        checker.add_segment(segment)

                final = await stream.get_final_message()
                completed = final.stop_reason in ("end_turn", "max_tokens")

                # Flush remaining buffer as final segment
                if buffer:
                    checker.add_segment(buffer)

        except Exception as e:
            if buffer:
                checker.add_segment(buffer)
            print(f"\n[attempt {attempt}] Stream error: {e}")

        # Verify integrity
        bad_segments = checker.verify_all()
        if not bad_segments and completed:
            print(f"\n[integrity OK] {len(checker.segments)} segments, {checker.total_chars} chars")
            return checker.full_text()

        if bad_segments:
            print(f"\n[integrity FAIL] Corrupted segments: {bad_segments}")

        if attempt < max_retries:
            await asyncio.sleep(0.5 * attempt)

    # Return best effort
    return checker.full_text()


async def main() -> None:
    messages = [{"role": "user", "content": "Explain the difference between processes and threads."}]
    result = await segmented_stream(messages)
    print(f"\nFinal ({len(result)} chars): {result[:200]}")


asyncio.run(main())

# Expected Token Savings: N/A; integrity checking adds negligible CPU but catches memory/serialization corruption in high-throughput systems
# Environment: Python 3.11+; corruption is rare in practice — this pattern is for extremely high-reliability requirements
```

## Comparison

| Option | Retry Strategy | Continuation | Checkpoint | Backoff | Best For |
|--------|---------------|-------------|------------|---------|----------|
| 1. Full Retry | Restart from scratch | No | No | Linear | Simple scripts, short responses |
| 2. Continuation Prompt | Resume from last chunk | Yes | Yes (in-memory) | Linear | Medium-length responses |
| 3. Chunk Timeout + Fallback | Timeout watchdog → non-streaming | No | No | Linear | UX-critical streaming with SLA |
| 4. SQLite Session | DB-persisted checkpoint | Yes | Yes (DB) | Linear | Long tasks, cross-restart resume |
| 5. Backoff + Jitter | Exponential backoff | No | In-memory | Jitter | High-concurrency, rate-limit-prone |
| 6. Segmented Integrity | Per-segment hash verify | No | In-memory | Linear | High-reliability, integrity-critical |
