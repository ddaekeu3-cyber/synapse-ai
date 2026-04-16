---
title: "Agent Doesn't Implement Streaming Response Watermarking"
description: "Streaming responses delivered to clients lack traceability — you can't tell which model, version, or request produced a given chunk. Watermarking embeds invisible or visible metadata into streaming output for attribution, debugging, and audit."
difficulty: advanced
category: streaming
tags: [streaming, watermarking, traceability, attribution, metadata, audit, debugging]
---

## Problem

A streaming response arrives at a client — or gets logged, cached, or forwarded — but there's no way to trace it back to which model version, prompt template, or request ID produced it. When an output causes a problem, you can't tell which configuration generated it. In multi-model systems, you can't attribute quality differences to specific variants. Watermarking embeds traceability metadata into the stream so any downstream consumer can identify the origin.

```python
# BAD: anonymous streaming — no attribution possible
async def stream_response(prompt: str):
    async with client.messages.stream(...) as stream:
        async for text in stream.text_stream:
            yield text  # no origin metadata, no traceability
```

## Solution 1: Metadata Header Injection Before Stream

Prepend a metadata block before the first streamed token.

```python
import asyncio
import json
import uuid
import time
from anthropic import AsyncAnthropic
from typing import AsyncIterator

client = AsyncAnthropic()

def make_stream_header(
    request_id: str,
    model: str,
    session_id: str | None = None,
    extra: dict | None = None
) -> str:
    """
    Invisible metadata header — zero-width Unicode delimiter marks boundaries.
    In practice: use a protocol envelope (HTTP headers, SSE event fields, etc.)
    """
    meta = {
        "request_id": request_id,
        "model": model,
        "timestamp": time.time(),
        "session_id": session_id,
        **(extra or {})
    }
    # Use a delimiter that parsers can strip
    return f"\x02{json.dumps(meta)}\x03"  # STX...ETX wrapping

def make_stream_footer(request_id: str, total_tokens: int) -> str:
    meta = {"request_id": request_id, "total_tokens": total_tokens, "end": True}
    return f"\x02{json.dumps(meta)}\x03"

async def watermarked_stream(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    session_id: str | None = None
) -> AsyncIterator[str]:
    request_id = str(uuid.uuid4())[:8]

    # Yield metadata header first
    yield make_stream_header(request_id, model, session_id)

    total_tokens = 0
    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            yield text
        usage = await stream.get_final_message()
        total_tokens = usage.usage.output_tokens

    # Yield metadata footer last
    yield make_stream_footer(request_id, total_tokens)

def strip_watermark(chunks: list[str]) -> tuple[str, list[dict]]:
    """Parse watermark chunks and return (clean_text, metadata_list)."""
    import re
    metadata_list = []
    clean_parts = []

    for chunk in chunks:
        # Extract all metadata blocks
        for match in re.finditer(r'\x02({.*?})\x03', chunk, re.DOTALL):
            try:
                metadata_list.append(json.loads(match.group(1)))
            except Exception:
                pass
        # Remove metadata blocks from text
        clean = re.sub(r'\x02{.*?}\x03', '', chunk, flags=re.DOTALL)
        if clean:
            clean_parts.append(clean)

    return "".join(clean_parts), metadata_list

async def main():
    chunks = []
    async for chunk in watermarked_stream(
        "What is streaming watermarking?",
        session_id="sess-001"
    ):
        chunks.append(chunk)

    clean_text, metadata = strip_watermark(chunks)
    print(f"Clean response: {clean_text[:200]}")
    print(f"\nMetadata extracted:")
    for m in metadata:
        print(f"  {m}")

asyncio.run(main())
```

## Solution 2: SSE Event-Based Watermarking

Embed watermark data in Server-Sent Event fields alongside content.

```python
import asyncio
import json
import uuid
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import AsyncIterator

client = AsyncAnthropic()

@dataclass
class SSEEvent:
    event_type: str     # "metadata" | "content" | "done"
    data: str
    id: str | None = None
    retry: int | None = None

    def to_wire(self) -> str:
        """Format as SSE wire protocol."""
        lines = []
        if self.id:
            lines.append(f"id: {self.id}")
        lines.append(f"event: {self.event_type}")
        lines.append(f"data: {self.data}")
        lines.append("")  # blank line terminates event
        return "\n".join(lines) + "\n"

    @classmethod
    def from_wire(cls, raw: str) -> "SSEEvent | None":
        lines = raw.strip().split("\n")
        fields: dict[str, str] = {}
        for line in lines:
            if ": " in line:
                k, _, v = line.partition(": ")
                fields[k] = v
        if "data" not in fields:
            return None
        return cls(
            event_type=fields.get("event", "message"),
            data=fields["data"],
            id=fields.get("id"),
        )

async def sse_watermarked_stream(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    request_id: str | None = None
) -> AsyncIterator[SSEEvent]:
    request_id = request_id or str(uuid.uuid4())[:8]
    chunk_seq = 0

    # Metadata event first
    yield SSEEvent(
        event_type="metadata",
        data=json.dumps({
            "request_id": request_id,
            "model": model,
            "started_at": time.time(),
            "prompt_preview": prompt[:50],
        }),
        id=f"{request_id}-meta"
    )

    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            yield SSEEvent(
                event_type="content",
                data=json.dumps({"text": text, "seq": chunk_seq}),
                id=f"{request_id}-{chunk_seq}"
            )
            chunk_seq += 1

        final = await stream.get_final_message()

    # Done event with usage
    yield SSEEvent(
        event_type="done",
        data=json.dumps({
            "request_id": request_id,
            "total_chunks": chunk_seq,
            "output_tokens": final.usage.output_tokens,
            "input_tokens": final.usage.input_tokens,
            "completed_at": time.time(),
        }),
        id=f"{request_id}-done"
    )

async def consume_sse_stream(prompt: str) -> tuple[str, dict, dict]:
    """Returns (content, start_metadata, end_metadata)."""
    start_meta = {}
    end_meta = {}
    content_parts = []

    async for event in sse_watermarked_stream(prompt):
        if event.event_type == "metadata":
            start_meta = json.loads(event.data)
        elif event.event_type == "content":
            content_parts.append(json.loads(event.data)["text"])
        elif event.event_type == "done":
            end_meta = json.loads(event.data)

    return "".join(content_parts), start_meta, end_meta

async def main():
    content, start_meta, end_meta = await consume_sse_stream(
        "Explain SSE-based streaming in one paragraph."
    )
    print(f"Request ID: {start_meta.get('request_id')}")
    print(f"Model: {start_meta.get('model')}")
    print(f"Tokens: {end_meta.get('output_tokens')}")
    print(f"\nContent:\n{content[:300]}")

asyncio.run(main())
```

## Solution 3: Invisible Unicode Watermark in Text

Embed request ID bits directly into the streamed text using zero-width Unicode characters.

```python
import asyncio
import uuid
from anthropic import AsyncAnthropic
from typing import AsyncIterator

client = AsyncAnthropic()

# Zero-width characters for encoding bits
ZWJ = "\u200d"   # Zero Width Joiner = bit 1
ZWNJ = "\u200c"  # Zero Width Non-Joiner = bit 0
ZWS = "\u200b"   # Zero Width Space = delimiter/start marker

def encode_bits(data: str) -> str:
    """Encode a short string as zero-width Unicode characters."""
    result = ZWS  # start marker
    for char in data[:16]:  # limit to 16 chars
        for bit in format(ord(char), "08b"):
            result += ZWJ if bit == "1" else ZWNJ
    result += ZWS  # end marker
    return result

def decode_bits(text: str) -> str | None:
    """Extract watermark from zero-width characters in text."""
    start = text.find(ZWS)
    if start == -1:
        return None
    end = text.find(ZWS, start + 1)
    if end == -1:
        return None

    bit_chars = text[start+1:end]
    bits = ""
    for c in bit_chars:
        if c == ZWJ:
            bits += "1"
        elif c == ZWNJ:
            bits += "0"

    # Decode bits back to string
    chars = []
    for i in range(0, len(bits), 8):
        byte = bits[i:i+8]
        if len(byte) == 8:
            chars.append(chr(int(byte, 2)))
    return "".join(chars) if chars else None

async def invisible_watermark_stream(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    watermark: str | None = None
) -> AsyncIterator[str]:
    """Stream with invisible watermark embedded in first chunk."""
    watermark = watermark or str(uuid.uuid4())[:8]
    first_chunk = True

    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            if first_chunk:
                # Embed watermark before first visible character
                encoded = encode_bits(watermark)
                yield encoded + text
                first_chunk = False
            else:
                yield text

async def main():
    watermark_id = "req-7f3a"
    chunks = []
    async for chunk in invisible_watermark_stream(
        "What is invisible watermarking?",
        watermark=watermark_id
    ):
        chunks.append(chunk)

    full_text = "".join(chunks)

    # Text appears normal to users
    visible_text = "".join(
        c for c in full_text if c not in (ZWJ, ZWNJ, ZWS)
    )
    print(f"Visible text: {visible_text[:200]}")

    # But watermark is recoverable
    extracted = decode_bits(full_text)
    print(f"\nExtracted watermark: {extracted!r}")
    print(f"Original watermark: {watermark_id!r}")
    print(f"Match: {extracted == watermark_id}")

asyncio.run(main())
```

## Solution 4: Chunk-Level Sequence Watermarking

Attach a verifiable sequence number and hash to each chunk for tamper detection.

```python
import asyncio
import hashlib
import json
import time
import uuid
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import AsyncIterator

client = AsyncAnthropic()

@dataclass
class WatermarkedChunk:
    seq: int
    text: str
    request_id: str
    chunk_hash: str        # hash of (prev_hash + text)
    prev_hash: str         # previous chunk hash (chain)
    timestamp: float = field(default_factory=time.time)

    def to_envelope(self) -> dict:
        return {
            "seq": self.seq,
            "text": self.text,
            "request_id": self.request_id,
            "hash": self.chunk_hash,
            "prev_hash": self.prev_hash,
        }

def compute_chunk_hash(prev_hash: str, text: str, seq: int) -> str:
    data = f"{prev_hash}:{seq}:{text}"
    return hashlib.sha256(data.encode()).hexdigest()[:16]

def verify_chain(chunks: list[WatermarkedChunk]) -> tuple[bool, list[str]]:
    """Verify the hash chain integrity. Returns (valid, list_of_errors)."""
    errors = []
    expected_prev = "genesis"

    for chunk in chunks:
        expected_hash = compute_chunk_hash(expected_prev, chunk.text, chunk.seq)
        if chunk.chunk_hash != expected_hash:
            errors.append(f"Seq {chunk.seq}: hash mismatch (expected {expected_hash}, got {chunk.chunk_hash})")
        if chunk.prev_hash != expected_prev:
            errors.append(f"Seq {chunk.seq}: prev_hash mismatch")
        expected_prev = chunk.chunk_hash

    return len(errors) == 0, errors

async def hash_chained_stream(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001"
) -> AsyncIterator[WatermarkedChunk]:
    request_id = str(uuid.uuid4())[:8]
    seq = 0
    prev_hash = "genesis"

    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            chunk_hash = compute_chunk_hash(prev_hash, text, seq)
            chunk = WatermarkedChunk(
                seq=seq,
                text=text,
                request_id=request_id,
                chunk_hash=chunk_hash,
                prev_hash=prev_hash,
            )
            prev_hash = chunk_hash
            seq += 1
            yield chunk

async def main():
    received_chunks: list[WatermarkedChunk] = []

    async for chunk in hash_chained_stream("What is chain-of-custody for data?"):
        received_chunks.append(chunk)

    full_text = "".join(c.text for c in received_chunks)
    valid, errors = verify_chain(received_chunks)

    print(f"Chunks received: {len(received_chunks)}")
    print(f"Request ID: {received_chunks[0].request_id if received_chunks else 'none'}")
    print(f"Chain integrity: {'✓ Valid' if valid else '✗ INVALID'}")
    if errors:
        print(f"Errors: {errors}")
    print(f"\nContent: {full_text[:200]}")

    # Simulate tamper detection
    if received_chunks:
        received_chunks[2].text = "TAMPERED"
        valid, errors = verify_chain(received_chunks)
        print(f"\nAfter tampering: {'✓ Valid' if valid else '✗ INVALID'}")
        print(f"Detected: {errors}")

asyncio.run(main())
```

## Solution 5: Model Attribution Watermark

Embed model and version information in a way that survives copy-paste.

```python
import asyncio
import base64
import json
from anthropic import AsyncAnthropic
from typing import AsyncIterator

client = AsyncAnthropic()

ATTRIBUTION_SUFFIX_TEMPLATE = "\n\n<!-- gen:{encoded} -->"

def encode_attribution(metadata: dict) -> str:
    payload = json.dumps(metadata, separators=(",", ":"))
    return base64.b64encode(payload.encode()).decode()

def decode_attribution(text: str) -> dict | None:
    import re
    match = re.search(r'<!-- gen:([A-Za-z0-9+/=]+) -->', text)
    if not match:
        return None
    try:
        payload = base64.b64decode(match.group(1)).decode()
        return json.loads(payload)
    except Exception:
        return None

async def attributed_stream(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    attribution_meta: dict | None = None
) -> AsyncIterator[str]:
    import uuid, time
    meta = {
        "model": model,
        "ts": int(time.time()),
        "rid": str(uuid.uuid4())[:8],
        **(attribution_meta or {})
    }

    all_text = ""
    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            all_text += text
            yield text

    # Append attribution suffix after stream completes
    encoded = encode_attribution(meta)
    suffix = ATTRIBUTION_SUFFIX_TEMPLATE.format(encoded=encoded)
    yield suffix

async def main():
    chunks = []
    async for chunk in attributed_stream(
        "Explain model attribution in one paragraph.",
        attribution_meta={"version": "v2.1", "env": "prod"}
    ):
        chunks.append(chunk)

    full_output = "".join(chunks)

    # Visible to user (sans attribution comment)
    visible = full_output[:full_output.find("<!-- gen:")]
    print(f"Visible output:\n{visible[:200]}\n")

    # Decode attribution
    attribution = decode_attribution(full_output)
    if attribution:
        print(f"Attribution: {attribution}")
    else:
        print("No attribution found")

asyncio.run(main())
```

## Solution 6: Streaming Audit Log with Chunk Registry

Log every streamed chunk with metadata to an audit store for post-hoc traceability.

```python
import asyncio
import json
import time
import uuid
from pathlib import Path
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field, asdict
from typing import AsyncIterator

client = AsyncAnthropic()
AUDIT_LOG = Path("/tmp/stream_audit.jsonl")

@dataclass
class ChunkAuditEntry:
    request_id: str
    seq: int
    text: str
    model: str
    timestamp: float = field(default_factory=time.time)
    cumulative_chars: int = 0

def log_chunk(entry: ChunkAuditEntry):
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(asdict(entry)) + "\n")

def load_audit_trail(request_id: str) -> list[ChunkAuditEntry]:
    if not AUDIT_LOG.exists():
        return []
    entries = []
    for line in AUDIT_LOG.read_text().splitlines():
        try:
            data = json.loads(line)
            if data.get("request_id") == request_id:
                entries.append(ChunkAuditEntry(**data))
        except Exception:
            continue
    return sorted(entries, key=lambda e: e.seq)

async def audited_stream(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    request_id: str | None = None
) -> AsyncIterator[tuple[str, str]]:
    """Yields (text_chunk, request_id). Logs every chunk to audit store."""
    request_id = request_id or str(uuid.uuid4())[:8]
    seq = 0
    cumulative = 0

    # Log request start
    log_chunk(ChunkAuditEntry(
        request_id=request_id,
        seq=-1,  # -1 = metadata entry
        text=f"[START] prompt={prompt[:80]}",
        model=model,
        cumulative_chars=0
    ))

    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            cumulative += len(text)
            log_chunk(ChunkAuditEntry(
                request_id=request_id,
                seq=seq,
                text=text,
                model=model,
                cumulative_chars=cumulative
            ))
            yield text, request_id
            seq += 1

    log_chunk(ChunkAuditEntry(
        request_id=request_id,
        seq=seq,
        text=f"[END] total_chars={cumulative}",
        model=model,
        cumulative_chars=cumulative
    ))

async def replay_from_audit(request_id: str) -> str:
    """Reconstruct full response from audit trail."""
    entries = load_audit_trail(request_id)
    return "".join(
        e.text for e in entries
        if e.seq >= 0 and not e.text.startswith("[")
    )

async def main():
    rid = None
    collected = []

    async for text, request_id in audited_stream("What is streaming audit logging?"):
        collected.append(text)
        rid = request_id

    print(f"Request ID: {rid}")
    print(f"Live output: {''.join(collected)[:200]}")

    if rid:
        replayed = await replay_from_audit(rid)
        print(f"\nReplayed from audit: {replayed[:200]}")
        print(f"Match: {''.join(collected)[:200] == replayed[:200]}")

asyncio.run(main())
```

## Comparison

| Approach | Visibility | Tamper-Proof | Overhead | Best For |
|---|---|---|---|---|
| Metadata Header/Footer | Protocol-level | No | None | Internal services with structured protocols |
| SSE Event Fields | Protocol-level | No | None | Server-Sent Events APIs |
| Invisible Unicode | Embedded in text | No | Minimal | Copy-paste attribution tracking |
| Hash-Chained Chunks | Embedded | Yes | Minimal | Tamper detection, legal audit |
| Attribution Suffix | End of text | No | None | Document-level attribution |
| Audit Log | External | Yes (immutable log) | Low I/O | Compliance, post-hoc debugging |

**Rule of thumb**: Use SSE event fields for structured streaming APIs (cleanest, no text modification). Use audit logging for compliance scenarios. Use hash-chained chunks when you need cryptographic tamper detection. Never embed invisible Unicode in user-facing text without disclosure.
