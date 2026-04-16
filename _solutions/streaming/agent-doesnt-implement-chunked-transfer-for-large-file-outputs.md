---
title: "Agent Doesn't Implement Chunked Transfer for Large File Outputs"
slug: agent-doesnt-implement-chunked-transfer-for-large-file-outputs
category: streaming
tags: [streaming, chunked-transfer, large-files, download, generator, anthropic-sdk]
description: >
  When the agent generates large outputs — long reports, full codebases, CSV
  exports — it buffers the entire response in memory before sending it to the
  client. This causes high memory consumption, request timeouts on slow networks,
  and poor time-to-first-byte for the end user.
symptoms:
  - Requests generating multi-MB text outputs time out at 30 s
  - Server memory spikes to 500 MB+ when many large generations run concurrently
  - Users see a spinner for 45 s and then receive the full document at once
  - No progress indication during long-running generation jobs
related_solutions:
  - agent-doesnt-implement-async-generator-pipeline-for-streaming-results
  - agent-doesnt-implement-delta-compression-for-streaming-responses
  - agent-doesnt-implement-streaming-progress-indicators-for-long-tasks
---

## Problem

Large file outputs must be streamed to the client as they are generated. The
Anthropic streaming API produces text incrementally; the challenge is plumbing
that stream all the way through the server into the client's HTTP response
without buffering the complete output in memory. This requires chunked HTTP
transfer encoding, backpressure-aware generators, and proper cleanup on client
disconnect.

---

## Solution 1 — Basic Streaming HTTP Response (ASGI / starlette)

Pipe the Anthropic streaming API directly into an HTTP `StreamingResponse`
using an async generator. Each token chunk is flushed to the client
immediately.

```python
import anthropic
import asyncio

# pip install starlette uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route


async def generate_stream(prompt: str):
    """Async generator that yields text chunks from the Anthropic streaming API."""
    client = anthropic.AsyncAnthropic()
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for chunk in stream.text_stream:
            yield chunk.encode("utf-8")


async def generate_report_endpoint(request: Request) -> StreamingResponse:
    body = await request.json()
    prompt = body.get("prompt", "Generate a comprehensive technical report.")
    return StreamingResponse(
        generate_stream(prompt),
        media_type="text/plain; charset=utf-8",
        headers={
            "Transfer-Encoding": "chunked",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache",
        },
    )


app = Starlette(routes=[Route("/generate", generate_report_endpoint, methods=["POST"])])

# To run: uvicorn solution:app
# To test: curl -N -X POST http://localhost:8000/generate -H "Content-Type: application/json" -d '{"prompt":"Write a 5-section report on distributed systems."}'

# Standalone demo without HTTP server:
async def demo_streaming_output():
    prompt = "Write a detailed 5-section report on distributed systems design patterns."
    total_bytes = 0
    chunk_count = 0
    async for chunk in generate_stream(prompt):
        total_bytes += len(chunk)
        chunk_count += 1
        if chunk_count % 10 == 0:
            print(f"\r[stream] {total_bytes:,} bytes  {chunk_count} chunks", end="", flush=True)
    print(f"\n[stream] complete: {total_bytes:,} bytes in {chunk_count} chunks")


asyncio.run(demo_streaming_output())
```

---

## Solution 2 — Multi-Section Document Generator with Section Boundaries

For long structured documents (reports, READMEs, wikis), generate each section
separately in sequence, streaming each one as it completes. Section boundaries
let the client render progressively.

```python
import anthropic
import asyncio
from dataclasses import dataclass


@dataclass
class Section:
    title:  str
    prompt: str


async def stream_section(
    section: Section,
    context: str,
    model: str = "claude-sonnet-4-6",
):
    """Yields (header_marker, chunk) tuples."""
    client = anthropic.AsyncAnthropic()
    yield "SECTION_START", f"## {section.title}\n\n"
    async with client.messages.stream(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Context: {context}\n\n"
                f"Write the '{section.title}' section of a technical report. "
                f"{section.prompt} "
                f"Write 2-3 substantial paragraphs. Do not include the section title."
            ),
        }],
    ) as stream:
        async for chunk in stream.text_stream:
            yield "CHUNK", chunk
    yield "SECTION_END", "\n\n"


async def stream_full_document(
    title: str,
    sections: list[Section],
    model: str = "claude-sonnet-4-6",
):
    """
    Async generator yielding (event_type, text) pairs.
    Clients can detect SECTION_START/END for progress tracking.
    """
    yield "DOC_START", f"# {title}\n\n"
    for i, section in enumerate(sections):
        print(f"[doc] generating section {i+1}/{len(sections)}: {section.title}")
        async for event_type, chunk in stream_section(section, context=title, model=model):
            yield event_type, chunk
    yield "DOC_END", ""


async def demo_multisection():
    sections = [
        Section("Introduction",    "Introduce the topic and its importance."),
        Section("Core Concepts",   "Explain the fundamental concepts and terminology."),
        Section("Best Practices",  "Describe key best practices with concrete examples."),
        Section("Common Pitfalls", "List common mistakes and how to avoid them."),
        Section("Conclusion",      "Summarise key takeaways."),
    ]

    total_bytes = 0
    sections_done = 0
    output_path = "/tmp/distributed_systems_report.md"

    with open(output_path, "w") as f:
        async for event, text in stream_full_document(
            "Distributed Systems Design Patterns", sections
        ):
            if text:
                f.write(text)
                total_bytes += len(text.encode())
            if event == "SECTION_END":
                sections_done += 1
                print(f"[doc] section {sections_done}/{len(sections)} written  "
                      f"total={total_bytes:,} bytes")

    print(f"\n[doc] complete: {output_path}  {total_bytes:,} bytes")


asyncio.run(demo_multisection())
```

---

## Solution 3 — Chunked CSV / JSONL Export Generator

When generating structured data (CSV rows, JSONL records), stream each row as
it is generated instead of collecting all rows first. This allows the client
to start processing records immediately and keeps memory constant.

```python
import anthropic
import asyncio
import csv
import io
import json
import re


async def stream_csv_rows(
    schema: list[str],
    generation_prompt: str,
    target_rows: int = 100,
    batch_size: int = 10,
    model: str = "claude-sonnet-4-6",
):
    """
    Yields CSV rows (as bytes) one at a time.
    First yields the header row, then data rows as they are generated.
    """
    client = anthropic.AsyncAnthropic()

    # Yield header
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(schema)
    yield buf.getvalue().encode()

    rows_yielded = 0
    while rows_yielded < target_rows:
        remaining = min(batch_size, target_rows - rows_yielded)
        prompt = (
            f"{generation_prompt}\n\n"
            f"Generate exactly {remaining} rows of CSV data with these columns: "
            f"{', '.join(schema)}.\n"
            f"Output ONLY the data rows, no header, comma-separated. One row per line."
        )
        resp = await client.messages.create(
            model=model, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            buf = io.StringIO()
            writer = csv.writer(buf)
            # Parse generated CSV line safely
            try:
                row = next(csv.reader([line]))
                if len(row) == len(schema):
                    writer.writerow(row)
                    yield buf.getvalue().encode()
                    rows_yielded += 1
            except Exception:
                continue

    yield b""  # signal end


async def stream_jsonl_records(
    record_schema: dict,
    generation_prompt: str,
    target_records: int = 50,
    model: str = "claude-sonnet-4-6",
):
    """Yields JSONL lines (bytes) one at a time."""
    client = anthropic.AsyncAnthropic()
    produced = 0

    while produced < target_records:
        batch = min(10, target_records - produced)
        prompt = (
            f"{generation_prompt}\n\n"
            f"Generate {batch} JSON objects (one per line, no array wrapper) "
            f"matching this schema: {json.dumps(record_schema)}. "
            f"Output ONLY valid JSON lines."
        )
        resp = await client.messages.create(
            model=model, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        for line in resp.content[0].text.strip().splitlines():
            line = line.strip()
            try:
                obj = json.loads(line)
                yield (json.dumps(obj) + "\n").encode()
                produced += 1
                if produced >= target_records:
                    break
            except json.JSONDecodeError:
                continue


async def demo_csv():
    schema = ["company", "industry", "employee_count", "founded_year", "country"]
    prompt = "Generate realistic fictional company data."
    rows = 0
    async for row_bytes in stream_csv_rows(schema, prompt, target_rows=15, batch_size=5):
        if row_bytes:
            print(row_bytes.decode().rstrip())
            rows += 1
    print(f"\n[csv] streamed {rows} rows")


asyncio.run(demo_csv())
```

---

## Solution 4 — Backpressure-Aware File Writer with Progress Callbacks

Write large generated content to a file while honouring write backpressure.
Emit progress callbacks at configurable byte intervals so callers can update
progress bars or send SSE progress events.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable


@dataclass
class WriteProgress:
    bytes_written:   int   = 0
    chunks_written:  int   = 0
    started_at:      float = field(default_factory=time.monotonic)
    finished_at:     float = 0.0
    complete:        bool  = False

    @property
    def elapsed_s(self) -> float:
        end = self.finished_at or time.monotonic()
        return end - self.started_at

    @property
    def throughput_kbps(self) -> float:
        return (self.bytes_written / 1024) / max(self.elapsed_s, 0.001)


ProgressCallback = Callable[[WriteProgress], Awaitable[None]]


async def stream_to_file(
    output_path: str,
    prompt: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 8096,
    progress_interval_bytes: int = 4096,
    on_progress: ProgressCallback | None = None,
) -> WriteProgress:
    progress = WriteProgress()
    client   = anthropic.AsyncAnthropic()
    next_progress_at = progress_interval_bytes

    with open(output_path, "w", encoding="utf-8") as f:
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for chunk in stream.text_stream:
                # Write chunk
                f.write(chunk)
                chunk_bytes = len(chunk.encode("utf-8"))
                progress.bytes_written += chunk_bytes
                progress.chunks_written += 1

                # Flush periodically (every ~64KB or on sentence boundary)
                if progress.bytes_written >= next_progress_at:
                    f.flush()
                    next_progress_at += progress_interval_bytes
                    if on_progress:
                        await on_progress(progress)

    progress.finished_at = time.monotonic()
    progress.complete = True
    if on_progress:
        await on_progress(progress)
    return progress


async def _progress_cb(p: WriteProgress) -> None:
    status = "DONE" if p.complete else "..."
    print(
        f"\r[progress] {p.bytes_written:>8,} bytes  "
        f"{p.throughput_kbps:5.1f} KB/s  "
        f"{p.elapsed_s:5.1f}s  {status}",
        end="",
        flush=True,
    )
    if p.complete:
        print()


async def demo_file_writer():
    output = "/tmp/agent_report.md"
    prompt = (
        "Write a comprehensive 10-section technical guide on Kubernetes "
        "with detailed explanations, YAML examples, and troubleshooting tips."
    )
    prog = await stream_to_file(
        output_path=output,
        prompt=prompt,
        max_tokens=8096,
        progress_interval_bytes=2048,
        on_progress=_progress_cb,
    )
    print(f"[done] wrote {prog.bytes_written:,} bytes in {prog.elapsed_s:.1f}s "
          f"({prog.throughput_kbps:.1f} KB/s) -> {output}")


asyncio.run(demo_file_writer())
```

---

## Solution 5 — Chunked Transfer with Client Disconnect Detection

Detect when the HTTP client disconnects mid-stream and cancel the upstream API
call to avoid wasting tokens and incurring cost for a response nobody will read.

```python
import anthropic
import asyncio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route


async def cancellable_stream(
    prompt: str,
    request: Request,
    model: str = "claude-sonnet-4-6",
):
    """
    Async generator that cancels the upstream Anthropic stream when the
    client disconnects (detected via request.is_disconnected()).
    """
    client = anthropic.AsyncAnthropic()
    bytes_sent = 0
    cancelled = False

    try:
        async with client.messages.stream(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for chunk in stream.text_stream:
                # Check for client disconnect every chunk
                if await request.is_disconnected():
                    print(f"[disconnect] client left after {bytes_sent:,} bytes — cancelling stream")
                    cancelled = True
                    break

                encoded = chunk.encode("utf-8")
                bytes_sent += len(encoded)
                yield encoded

    except asyncio.CancelledError:
        print(f"[disconnect] stream task cancelled at {bytes_sent:,} bytes")
        return

    if not cancelled:
        print(f"[stream] complete: {bytes_sent:,} bytes sent")


async def generate_endpoint(request: Request) -> StreamingResponse:
    body = await request.json()
    prompt = body.get("prompt", "Write a detailed technical document.")
    return StreamingResponse(
        cancellable_stream(prompt, request),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache", "Transfer-Encoding": "chunked"},
    )


app = Starlette(routes=[Route("/generate", generate_endpoint, methods=["POST"])])

# Standalone demo without real HTTP disconnect detection
async def demo_cancellable():
    disconnect_flag = False

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return disconnect_flag

    fake_req = FakeRequest()
    prompt = "Write a comprehensive guide to microservices architecture."
    bytes_recv = 0

    async for chunk in cancellable_stream(prompt, fake_req):
        bytes_recv += len(chunk)
        if bytes_recv > 2000:
            disconnect_flag = True   # simulate client disconnect
        if bytes_recv % 500 == 0:
            print(f"[client] received {bytes_recv:,} bytes")


asyncio.run(demo_cancellable())
```

---

## Solution 6 — Parallel Section Generation with Ordered Assembly

Generate multiple document sections in parallel (up to a concurrency limit)
and assemble them in the correct order as they complete, streaming each section
to the client as soon as it is ready and its predecessors have been sent.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field


@dataclass
class SectionTask:
    index:  int
    title:  str
    prompt: str
    result: str = ""
    done:   bool = False
    event:  asyncio.Event = field(default_factory=asyncio.Event)


async def generate_section(
    task: SectionTask,
    semaphore: asyncio.Semaphore,
    model: str = "claude-sonnet-4-6",
) -> None:
    async with semaphore:
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": (
                    f"Write the '{task.title}' section for a technical document.\n"
                    f"{task.prompt}\n"
                    f"Write 2-3 paragraphs. Do not include the section title."
                ),
            }],
        )
        task.result = resp.content[0].text
        task.done = True
        task.event.set()
        print(f"[parallel] section {task.index + 1} '{task.title}' ready  {len(task.result)} chars")


async def stream_parallel_sections(
    title: str,
    sections: list[dict],
    model: str = "claude-sonnet-4-6",
    max_concurrent: int = 3,
):
    """
    Generates all sections in parallel but streams them in original order.
    Yields bytes chunks for the caller to write or send.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [
        SectionTask(index=i, title=s["title"], prompt=s["prompt"])
        for i, s in enumerate(sections)
    ]

    # Launch all section generation tasks concurrently
    gen_tasks = [asyncio.create_task(generate_section(t, semaphore, model)) for t in tasks]

    # Yield header
    yield f"# {title}\n\n".encode()

    # Yield sections in order, waiting for each to complete
    for task in tasks:
        await task.event.wait()
        section_text = f"## {task.title}\n\n{task.result}\n\n"
        yield section_text.encode()

    await asyncio.gather(*gen_tasks)


async def demo_parallel():
    sections = [
        {"title": "Introduction",    "prompt": "Introduce distributed systems and why they matter."},
        {"title": "Consistency",     "prompt": "Explain consistency models: strong, eventual, causal."},
        {"title": "Availability",    "prompt": "Discuss availability trade-offs and SLA design."},
        {"title": "Partition Tolerance", "prompt": "Describe network partitions and how systems handle them."},
        {"title": "Conclusion",      "prompt": "Summarise the key design decisions teams face."},
    ]

    total_bytes = 0
    output_path = "/tmp/parallel_report.md"

    with open(output_path, "wb") as f:
        async for chunk in stream_parallel_sections(
            "Distributed Systems: CAP Theorem in Practice",
            sections,
            max_concurrent=3,
        ):
            f.write(chunk)
            total_bytes += len(chunk)

    print(f"\n[parallel] wrote {total_bytes:,} bytes -> {output_path}")


asyncio.run(demo_parallel())
```

---

## Comparison

| Approach | Memory footprint | Time to first byte | Disconnect handling | Parallelism | Complexity |
|---|---|---|---|---|---|
| Basic streaming HTTP response | O(1) | Immediate (first token) | No | No | Very low |
| Multi-section sequential | O(section) | After first section | No | No | Low |
| CSV/JSONL row streaming | O(batch) | After first batch | No | No | Low |
| Backpressure file writer | O(chunk) | Immediate | No | No | Medium |
| Cancellable stream with disconnect | O(1) | Immediate | Yes | No | Medium |
| Parallel + ordered assembly | O(sections) | After first section done | No | Yes | Medium |

**Rule of thumb:**
- HTTP API serving any large text → Solution 1 (basic streaming response) always
- Multi-section documents → Solution 2 (sequential) or Solution 6 (parallel) for speed
- Data exports (CSV, JSONL) → Solution 3 — keeps memory constant regardless of output size
- Long-running generations → Solution 4 (file writer) with progress events for UX
- WebSocket / HTTP/2 clients that disconnect frequently → Solution 5 to avoid wasted tokens
- Time-constrained long documents → Solution 6 (parallel) cuts wall-clock time by 2–4x
