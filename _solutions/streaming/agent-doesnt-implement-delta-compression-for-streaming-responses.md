---
title: "Agent Doesn't Implement Delta Compression for Streaming Responses"
slug: agent-doesnt-implement-delta-compression-for-streaming-responses
category: streaming
tags: [streaming, delta, compression, bandwidth, sse, websocket, anthropic-sdk]
description: >
  The agent streams raw token-by-token text to clients without any compression
  or delta encoding, wasting bandwidth on repeated context, consuming excessive
  client-side processing, and making resumption after network drops expensive.
symptoms:
  - High egress costs for streaming-heavy workloads
  - Clients re-receive large unchanged preamble after reconnection
  - Mobile clients degrade under streaming load due to byte volume
  - No ability to diff or patch incremental state in structured outputs
related_solutions:
  - agent-doesnt-handle-stream-reconnection-after-network-interruption
  - agent-doesnt-implement-sse-endpoint-for-frontend-clients
  - agent-doesnt-implement-async-generator-pipeline-for-streaming-results
---

## Problem

Streaming the raw API token stream to end clients is bandwidth-inefficient when:
(a) the same system preamble is re-sent on every reconnection, (b) structured
JSON outputs are streamed with full object snapshots instead of patches, or (c)
repeated phrases appear in long responses. Delta compression — at the character,
word, or structural level — can reduce egress 30–70 % and make reconnection
cheap by letting clients resume from a cursor rather than retransmitting from
the beginning.

---

## Solution 1 — Character-Level Delta (Myers Diff)

After each token chunk arrives, compute a minimal character-level diff against
the previously sent string and only transmit the diff operation. Clients apply
patches locally.

```python
import anthropic
import asyncio
import difflib
import json


def char_delta(old: str, new: str) -> list[dict]:
    """
    Return a compact list of operations needed to transform `old` into `new`.
    op: "eq" | "insert" | "delete"
    """
    ops = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            ops.append({"op": "eq", "n": i2 - i1})
        elif tag == "insert":
            ops.append({"op": "ins", "v": new[j1:j2]})
        elif tag == "delete":
            ops.append({"op": "del", "n": i2 - i1})
        elif tag == "replace":
            ops.append({"op": "del", "n": i2 - i1})
            ops.append({"op": "ins", "v": new[j1:j2]})
    return ops


def apply_delta(base: str, ops: list[dict]) -> str:
    result = []
    pos = 0
    for op in ops:
        if op["op"] == "eq":
            result.append(base[pos: pos + op["n"]])
            pos += op["n"]
        elif op["op"] == "ins":
            result.append(op["v"])
        elif op["op"] == "del":
            pos += op["n"]
    return "".join(result)


async def stream_with_char_delta(messages: list, model: str = "claude-sonnet-4-6"):
    """
    Yields SSE-style delta frames instead of raw text chunks.
    Client reconstructs full text by applying each delta to its local buffer.
    """
    client = anthropic.AsyncAnthropic()
    sent_so_far = ""
    accumulated = ""

    async with client.messages.stream(
        model=model,
        max_tokens=1024,
        messages=messages,
    ) as stream:
        async for chunk in stream.text_stream:
            accumulated += chunk
            # Emit delta every ~50 chars to balance frequency vs diff cost
            if len(accumulated) - len(sent_so_far) >= 50 or chunk in (".","!","?","\n"):
                ops = char_delta(sent_so_far, accumulated)
                raw_bytes    = len(chunk.encode())
                delta_bytes  = len(json.dumps(ops).encode())
                saving_pct   = (1 - delta_bytes / max(raw_bytes, 1)) * 100
                frame = {"seq": len(sent_so_far), "ops": ops}
                yield frame
                sent_so_far = accumulated

    # Flush remainder
    if accumulated != sent_so_far:
        ops = char_delta(sent_so_far, accumulated)
        yield {"seq": len(sent_so_far), "ops": ops, "done": True}
    else:
        yield {"done": True}


async def demo():
    messages = [{"role": "user", "content": "List 5 distributed systems papers with one-line summaries."}]
    client_buffer = ""
    async for frame in stream_with_char_delta(messages):
        if frame.get("done") and not frame.get("ops"):
            print("\n[stream complete]")
            break
        if "ops" in frame:
            client_buffer = apply_delta(client_buffer, frame["ops"])
            print(f"\r[client] {len(client_buffer)} chars reconstructed", end="", flush=True)


asyncio.run(demo())
```

---

## Solution 2 — Word-Token Delta with Cursor Resume

Track a word-level cursor so reconnecting clients send `?cursor=N` and only
receive words from position N onward, avoiding full re-transmission.

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass, field


@dataclass
class WordCursorBuffer:
    words: list[str] = field(default_factory=list)
    _partial: str = ""

    def push(self, chunk: str) -> list[tuple[int, str]]:
        """Returns list of (word_index, word) for newly completed words."""
        self._partial += chunk
        tokens = re.split(r"(\s+)", self._partial)
        new_words = []
        # All but last segment are complete (last may be partial word)
        for seg in tokens[:-1]:
            if seg:
                idx = len(self.words)
                self.words.append(seg)
                new_words.append((idx, seg))
        self._partial = tokens[-1]
        return new_words

    def flush(self) -> list[tuple[int, str]]:
        if self._partial:
            idx = len(self.words)
            self.words.append(self._partial)
            self._partial = ""
            return [(idx, self.words[-1])]
        return []

    def since(self, cursor: int) -> list[tuple[int, str]]:
        return [(i, w) for i, w in enumerate(self.words) if i >= cursor]


# Shared buffers keyed by conversation_id (in production: Redis sorted set)
_buffers: dict[str, WordCursorBuffer] = {}


async def stream_words(
    conversation_id: str,
    messages: list,
    model: str = "claude-sonnet-4-6",
    resume_cursor: int = 0,
):
    """
    Async generator of (word_index, word) frames.
    If resume_cursor > 0, immediately yields buffered words from that point
    before continuing to stream new ones.
    """
    buf = _buffers.setdefault(conversation_id, WordCursorBuffer())

    # Replay from cursor (reconnection case)
    for idx, word in buf.since(resume_cursor):
        yield {"idx": idx, "w": word, "cached": True}

    # Already finished?  The buffer may be complete.
    # Otherwise continue streaming from API
    client = anthropic.AsyncAnthropic()
    async with client.messages.stream(
        model=model, max_tokens=1024, messages=messages
    ) as stream:
        async for chunk in stream.text_stream:
            for idx, word in buf.push(chunk):
                if idx >= resume_cursor:
                    yield {"idx": idx, "w": word, "cached": False}

    for idx, word in buf.flush():
        if idx >= resume_cursor:
            yield {"idx": idx, "w": word, "cached": False}
    yield {"done": True, "total_words": len(buf.words)}


async def client_a():
    conv_id = "conv-delta-001"
    messages = [{"role": "user", "content": "Explain the Raft consensus algorithm."}]
    words = []
    async for frame in stream_words(conv_id, messages):
        if frame.get("done"):
            print(f"\n[client-A done] {frame['total_words']} words")
            break
        words.append(frame["w"])
    print(" ".join(words[:20]) + "...")


async def client_b_reconnect():
    """Reconnects at word 10, should receive only words >= 10."""
    conv_id = "conv-delta-001"
    messages = [{"role": "user", "content": "Explain the Raft consensus algorithm."}]
    await asyncio.sleep(0.5)   # simulate delay before reconnect
    resumed = []
    async for frame in stream_words(conv_id, messages, resume_cursor=10):
        if frame.get("done"):
            print(f"\n[client-B done] resumed from word 10, cached={frame.get('cached')}")
            break
        resumed.append(frame["w"])
        if len(resumed) >= 5:
            print(f"[client-B] first 5 resumed words: {resumed[:5]}")
            break


asyncio.run(client_a())
```

---

## Solution 3 — JSON Patch Streaming for Structured Outputs

When the agent streams structured JSON (tool call arguments, function outputs),
compute RFC 6902 JSON Patches between successive snapshots and send only the
patch, not the full object.

```python
import anthropic
import asyncio
import json
from copy import deepcopy


def json_diff_patch(old: dict, new: dict) -> list[dict]:
    """
    Simplified RFC-6902-style patch generator for flat/nested dicts.
    Handles add, remove, replace at arbitrary depth.
    """
    patches = []

    def _diff(old_node, new_node, path=""):
        if isinstance(new_node, dict) and isinstance(old_node, dict):
            all_keys = set(old_node) | set(new_node)
            for k in all_keys:
                child_path = f"{path}/{k}"
                if k not in old_node:
                    patches.append({"op": "add", "path": child_path, "value": new_node[k]})
                elif k not in new_node:
                    patches.append({"op": "remove", "path": child_path})
                else:
                    _diff(old_node[k], new_node[k], child_path)
        elif old_node != new_node:
            patches.append({"op": "replace", "path": path, "value": new_node})

    _diff(old, new, "")
    return patches


def apply_json_patch(doc: dict, patches: list[dict]) -> dict:
    doc = deepcopy(doc)
    for op in patches:
        parts = op["path"].lstrip("/").split("/")
        if op["op"] == "replace" or op["op"] == "add":
            target = doc
            for p in parts[:-1]:
                target = target[p]
            target[parts[-1]] = op["value"]
        elif op["op"] == "remove":
            target = doc
            for p in parts[:-1]:
                target = target[p]
            del target[parts[-1]]
    return doc


TOOL = {
    "name": "update_report",
    "description": "Incrementally update a structured report object.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title":    {"type": "string"},
            "summary":  {"type": "string"},
            "sections": {"type": "array", "items": {"type": "string"}},
            "word_count": {"type": "integer"},
        },
        "required": ["title"],
    },
}


async def stream_json_patches(user_query: str):
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=[TOOL],
        messages=[{"role": "user", "content": user_query}],
    )

    # Simulate incremental snapshots by building the object progressively
    previous: dict = {}
    if resp.stop_reason == "tool_use":
        for block in resp.content:
            if block.type == "tool_use":
                full_args = block.input
                # Simulate streaming by chunking fields
                for i, (k, v) in enumerate(full_args.items()):
                    current = dict(list(full_args.items())[: i + 1])
                    patches = json_diff_patch(previous, current)
                    patch_bytes = len(json.dumps(patches).encode())
                    full_bytes  = len(json.dumps(current).encode())
                    print(
                        f"[patch] step={i+1}  patch={patch_bytes}B  "
                        f"full={full_bytes}B  saving={100*(1-patch_bytes/full_bytes):.0f}%"
                    )
                    print(f"  patches: {patches}")

                    # Client applies patch
                    previous = apply_json_patch(previous, patches)

    print("\n[client] final reconstructed object:")
    print(json.dumps(previous, indent=2))


asyncio.run(stream_json_patches(
    "Create a structured report about Byzantine fault tolerance with 3 sections."
))
```

---

## Solution 4 — zlib Streaming Compressor (Binary Transport)

For WebSocket or HTTP/2 binary frames, run each chunk through an incremental
zlib compressor. Reusing the same `Compress` object across chunks lets zlib
exploit repetition in the sliding window, achieving 50–70 % compression on
typical LLM prose.

```python
import anthropic
import asyncio
import zlib
import base64


class IncrementalCompressor:
    """Wraps zlib.compressobj for streaming, reusing the same window."""

    def __init__(self, level: int = 6):
        self._c = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS)
        self._total_raw = 0
        self._total_compressed = 0

    def compress(self, data: str) -> bytes:
        raw = data.encode("utf-8")
        compressed = self._c.compress(raw) + self._c.flush(zlib.Z_SYNC_FLUSH)
        self._total_raw += len(raw)
        self._total_compressed += len(compressed)
        return compressed

    def finish(self) -> bytes:
        return self._c.flush(zlib.Z_FINISH)

    @property
    def ratio(self) -> float:
        if self._total_raw == 0:
            return 1.0
        return self._total_compressed / self._total_raw


class IncrementalDecompressor:
    def __init__(self):
        self._d = zlib.decompressobj(-zlib.MAX_WBITS)
        self._buf = b""

    def feed(self, compressed: bytes) -> str:
        self._buf += self._d.decompress(compressed)
        text = self._buf.decode("utf-8", errors="replace")
        self._buf = b""
        return text


async def stream_compressed_websocket_frames(
    messages: list,
    model: str = "claude-sonnet-4-6",
    chunk_size: int = 128,
):
    """
    Async generator yielding base64-encoded compressed binary frames,
    simulating what you'd send over a WebSocket binary message.
    """
    client = anthropic.AsyncAnthropic()
    compressor = IncrementalCompressor()
    pending = ""

    async with client.messages.stream(
        model=model, max_tokens=1024, messages=messages
    ) as stream:
        async for text in stream.text_stream:
            pending += text
            if len(pending) >= chunk_size:
                compressed = compressor.compress(pending)
                frame = base64.b64encode(compressed).decode()
                yield frame
                pending = ""

    if pending:
        compressed = compressor.compress(pending)
        yield base64.b64encode(compressed).decode()
    # Flush
    tail = compressor.finish()
    if tail:
        yield base64.b64encode(tail).decode()

    yield f"__RATIO__:{compressor.ratio:.3f}"


async def demo_compressed():
    messages = [{"role": "user", "content": "Write a detailed explanation of Paxos consensus."}]
    decompressor = IncrementalDecompressor()
    full_text = ""
    frame_count = 0

    async for frame in stream_compressed_websocket_frames(messages):
        if frame.startswith("__RATIO__:"):
            ratio = float(frame.split(":")[1])
            print(f"\n[compression] ratio={ratio:.3f}  saving={100*(1-ratio):.1f}%  frames={frame_count}")
            break
        compressed_bytes = base64.b64decode(frame)
        chunk = decompressor.feed(compressed_bytes)
        full_text += chunk
        frame_count += 1

    print(f"Reconstructed: {full_text[:100]}...")


asyncio.run(demo_compressed())
```

---

## Solution 5 — Sentence-Boundary Delta with Fingerprint Deduplication

Split the stream at sentence boundaries and compute a fingerprint (hash) for
each sentence. Skip sending sentences whose hash the client already has —
useful for regeneration or templated responses where sentences repeat.

```python
import anthropic
import asyncio
import hashlib
import re


def split_sentences(text: str) -> tuple[list[str], str]:
    """Returns (complete_sentences, partial_remainder)."""
    pattern = re.compile(r'(?<=[.!?])\s+')
    parts = pattern.split(text)
    if text and not re.search(r'[.!?]\s*$', text):
        return parts[:-1], parts[-1]
    return parts, ""


def fingerprint(sentence: str) -> str:
    return hashlib.md5(sentence.encode()).hexdigest()[:8]


async def stream_deduplicated_sentences(
    messages: list,
    client_known_fps: set[str] | None = None,
    model: str = "claude-sonnet-4-6",
):
    """
    Yields frames: {"fp": str, "text": str} for new sentences,
    or {"fp": str, "cached": True} for sentences the client already has.
    """
    client_known_fps = client_known_fps or set()
    api_client = anthropic.AsyncAnthropic()
    buffer = ""

    async with api_client.messages.stream(
        model=model, max_tokens=1024, messages=messages
    ) as stream:
        async for chunk in stream.text_stream:
            buffer += chunk
            sentences, buffer = split_sentences(buffer)
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                fp = fingerprint(sentence)
                if fp in client_known_fps:
                    yield {"fp": fp, "cached": True}
                else:
                    client_known_fps.add(fp)
                    yield {"fp": fp, "text": sentence}

    # Flush partial last sentence
    if buffer.strip():
        fp = fingerprint(buffer.strip())
        if fp in client_known_fps:
            yield {"fp": fp, "cached": True}
        else:
            yield {"fp": fp, "text": buffer.strip()}

    yield {"done": True}


async def demo_dedup():
    messages = [{"role": "user", "content": "Explain eventual consistency in 5 sentences."}]
    known: set[str] = set()
    new_bytes = 0
    saved_bytes = 0

    async for frame in stream_deduplicated_sentences(messages, known):
        if frame.get("done"):
            print(f"\n[dedup] sent={new_bytes}B  saved={saved_bytes}B  "
                  f"ratio={saved_bytes/(new_bytes+saved_bytes+1)*100:.1f}%")
            break
        if frame.get("cached"):
            saved_bytes += 60   # avg sentence length estimate
            print(f"[dedup] SKIP cached sentence fp={frame['fp']}")
        else:
            new_bytes += len(frame["text"].encode())
            print(f"[dedup] SEND fp={frame['fp']}  text={frame['text'][:60]}")


asyncio.run(demo_dedup())
```

---

## Solution 6 — SSE Resume Protocol with Content-Range Equivalent

Implement a stateless resume protocol: the server stores the full response
keyed by a `stream_id` and `ETag`. On reconnect the client sends `Last-Event-ID`
and the server resumes from that byte offset — identical to HTTP range requests
but over SSE.

```python
import anthropic
import asyncio
import hashlib
import time
from dataclasses import dataclass, field


@dataclass
class StreamStore:
    """In-memory store; replace with Redis in production."""
    chunks: list[str] = field(default_factory=list)
    complete: bool = False
    etag: str = ""
    created_at: float = field(default_factory=time.time)

    def append(self, text: str) -> int:
        """Returns the new chunk index."""
        self.chunks.append(text)
        self.etag = hashlib.md5("".join(self.chunks).encode()).hexdigest()[:12]
        return len(self.chunks) - 1

    def since(self, chunk_idx: int) -> list[tuple[int, str]]:
        return [(i, c) for i, c in enumerate(self.chunks) if i >= chunk_idx]


_stores: dict[str, StreamStore] = {}


async def produce_stream(stream_id: str, messages: list, model: str = "claude-sonnet-4-6"):
    """Run once per stream_id — stores chunks for all clients to resume from."""
    store = _stores.setdefault(stream_id, StreamStore())
    if store.complete:
        return

    client = anthropic.AsyncAnthropic()
    async with client.messages.stream(
        model=model, max_tokens=1024, messages=messages
    ) as stream:
        async for chunk in stream.text_stream:
            store.append(chunk)
            await asyncio.sleep(0)   # yield to other coroutines

    store.complete = True


async def consume_stream(
    stream_id: str,
    messages: list,
    last_chunk_id: int = -1,
    model: str = "claude-sonnet-4-6",
):
    """
    Yields (chunk_id, text) frames. Passes `last_chunk_id` to resume.
    Starts the producer if not already running.
    """
    # Ensure producer is running
    if stream_id not in _stores:
        asyncio.create_task(produce_stream(stream_id, messages, model))
        await asyncio.sleep(0.05)  # let producer start

    store = _stores[stream_id]

    # Replay cached chunks
    for idx, chunk in store.since(last_chunk_id + 1):
        yield {"id": idx, "data": chunk, "cached": True}

    # Follow live stream
    seen_up_to = max(last_chunk_id, len(store.chunks) - 1)
    while not store.complete:
        await asyncio.sleep(0.05)
        for idx, chunk in store.since(seen_up_to + 1):
            yield {"id": idx, "data": chunk, "cached": False}
            seen_up_to = idx

    # Final tail
    for idx, chunk in store.since(seen_up_to + 1):
        yield {"id": idx, "data": chunk, "cached": False}

    yield {"done": True, "etag": store.etag, "total_chunks": len(store.chunks)}


async def demo_resume():
    stream_id = f"stream-{int(time.time())}"
    messages = [{"role": "user", "content": "List 5 famous distributed systems with one-line descriptions."}]

    # Client A: consumes from the start
    async def client_a():
        text = ""
        async for frame in consume_stream(stream_id, messages):
            if frame.get("done"):
                print(f"\n[A] done  etag={frame['etag']}  chunks={frame['total_chunks']}")
                return len(text)
            text += frame["data"]
        return len(text)

    # Client B: connects late, resumes from chunk 5
    async def client_b():
        await asyncio.sleep(0.3)
        received = []
        async for frame in consume_stream(stream_id, messages, last_chunk_id=5):
            if frame.get("done"):
                print(f"\n[B] done  resumed chunks: {len(received)}")
                return
            received.append(frame)

    await asyncio.gather(client_a(), client_b())


asyncio.run(demo_resume())
```

---

## Comparison

| Approach | Compression mechanism | Bandwidth saving | Resumption | Complexity |
|---|---|---|---|---|
| Character-level Myers diff | Per-character ops | 10–40% for incremental | No (stateful) | Medium |
| Word-cursor resume | Word index tracking | 0% (cursor only) | Yes — cursor-based | Low |
| JSON Patch streaming | RFC-6902 object patches | 40–80% for structured | No | Medium |
| zlib incremental compressor | DEFLATE sliding window | 50–70% on prose | No | Low |
| Sentence fingerprint dedup | Hash-based skip | 0–100% for repeated | No | Low |
| SSE resume protocol | Chunk store + offset | 0% (skip re-download) | Yes — chunk-index | Medium |

**Rule of thumb:**
- Prose text over WebSocket → zlib incremental compressor (highest raw saving)
- Structured JSON outputs → JSON Patch streaming
- Reconnecting clients → SSE resume protocol or word-cursor resume
- Template-heavy regeneration → sentence fingerprint dedup
