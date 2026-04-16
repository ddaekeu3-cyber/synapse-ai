---
layout: solution
title: "Agent Doesn't Implement Streaming Multiplexer for Multi-Agent Output"
category: streaming
description: "Merge parallel streaming responses from multiple agents into a single ordered output stream, with per-agent labeling, interleaving, and backpressure."
tags: [streaming, multiplexer, multi-agent, parallel, interleave, real-time]
---

# Agent Doesn't Implement Streaming Multiplexer for Multi-Agent Output

When multiple agents run in parallel, their streaming outputs arrive independently. Without a multiplexer, either the caller blocks on one agent at a time (losing parallelism) or all streams are buffered and concatenated after completion (losing streaming). A multiplexer merges live streams from N agents into a single ordered output, labeling each chunk with its source.

## Option 1: Simple Async Stream Interleaver

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()


async def stream_agent(label: str, prompt: str, queue: asyncio.Queue) -> None:
    """Stream one agent's output into a shared queue."""
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for chunk in stream.text_stream:
            await queue.put((label, chunk))
    await queue.put((label, None))  # sentinel


async def multiplex_streams(agents: list[tuple[str, str]]) -> None:
    """Merge N agent streams into a single print output."""
    queue: asyncio.Queue = asyncio.Queue()
    tasks = [asyncio.create_task(stream_agent(label, prompt, queue)) for label, prompt in agents]

    active = len(agents)
    while active > 0:
        label, chunk = await queue.get()
        if chunk is None:
            active -= 1
        else:
            print(f"[{label}] {chunk}", end="", flush=True)

    await asyncio.gather(*tasks)
    print()  # final newline


async def main() -> None:
    agents = [
        ("Python", "List 3 Python async best practices, briefly."),
        ("Go",     "List 3 Go concurrency best practices, briefly."),
        ("Rust",   "List 3 Rust async best practices, briefly."),
    ]
    print("=== Multiplexed Stream ===")
    await multiplex_streams(agents)


asyncio.run(main())

# Expected Token Savings: N/A (throughput pattern); parallel streams cut wall time by N
# Environment: Python 3.11+; queue buffer size can be set to limit memory (asyncio.Queue(maxsize=100))
```

## Option 2: Labeled Buffer with Ordered Output per Agent

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()


@dataclass
class AgentBuffer:
    label: str
    chunks: list[str] = field(default_factory=list)
    done: bool = False

    def flush(self) -> str:
        text = "".join(self.chunks)
        return text


async def stream_into_buffer(label: str, prompt: str, buf: AgentBuffer, notify: asyncio.Event) -> None:
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for chunk in stream.text_stream:
            buf.chunks.append(chunk)
            notify.set()
    buf.done = True
    notify.set()


async def ordered_multiplex(agents: list[tuple[str, str]]) -> dict[str, str]:
    """
    Stream all agents in parallel, display each agent's output as it arrives.
    Each agent's output is displayed in its own section.
    """
    buffers = {label: AgentBuffer(label=label) for label, _ in agents}
    notify = asyncio.Event()

    tasks = [
        asyncio.create_task(stream_into_buffer(label, prompt, buffers[label], notify))
        for label, prompt in agents
    ]

    last_lens = {label: 0 for label in buffers}

    while not all(b.done for b in buffers.values()):
        await notify.wait()
        notify.clear()

        for label, buf in buffers.items():
            new_chunks = buf.chunks[last_lens[label]:]
            if new_chunks:
                print(f"\033[1m[{label}]\033[0m ", end="")
                print("".join(new_chunks), end="", flush=True)
                last_lens[label] = len(buf.chunks)

    await asyncio.gather(*tasks)
    return {label: buf.flush() for label, buf in buffers.items()}


async def main() -> None:
    agents = [
        ("Analyst",    "Analyze risks of deploying AI agents in production in 2 sentences."),
        ("Optimist",   "Describe benefits of AI agents in production in 2 sentences."),
        ("Pragmatist", "Give 2 practical steps for safely deploying AI agents."),
    ]
    results = await ordered_multiplex(agents)
    print("\n\n=== All done ===")
    for label, text in results.items():
        print(f"\n[{label}]\n{text[:150]}")


asyncio.run(main())

# Expected Token Savings: Parallel streams; notify pattern avoids polling overhead
# Environment: Python 3.11+; terminal ANSI codes can be removed for non-TTY output
```

## Option 3: Priority Queue Multiplexer with Token Rate Tracking

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class StreamChunk:
    agent_id: str
    text: str
    timestamp: float
    token_index: int


async def stream_agent_chunks(
    agent_id: str,
    prompt: str,
    out_queue: asyncio.PriorityQueue,
    priority: int = 0,
) -> None:
    token_idx = 0
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for chunk in stream.text_stream:
            item = (priority, time.monotonic(), StreamChunk(
                agent_id=agent_id,
                text=chunk,
                timestamp=time.monotonic(),
                token_index=token_idx,
            ))
            await out_queue.put(item)
            token_idx += 1
    # Sentinel
    await out_queue.put((priority, time.monotonic(), StreamChunk(agent_id=agent_id, text="", timestamp=0, token_index=-1)))


async def priority_multiplex(agent_specs: list[tuple[str, str, int]]) -> dict[str, str]:
    """
    Multiplex streams with priority ordering.
    Higher priority agents' chunks are output first when multiple are queued.
    """
    queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
    tasks = [
        asyncio.create_task(stream_agent_chunks(aid, prompt, queue, priority=prio))
        for aid, prompt, prio in agent_specs
    ]

    active = len(agent_specs)
    outputs: dict[str, list[str]] = {aid: [] for aid, _, _ in agent_specs}
    token_rates: dict[str, list[float]] = {aid: [] for aid, _, _ in agent_specs}
    last_token_time: dict[str, float] = {aid: time.monotonic() for aid, _, _ in agent_specs}

    while active > 0:
        _, _, chunk = await queue.get()

        if chunk.token_index == -1:
            active -= 1
            if outputs[chunk.agent_id]:
                rate = len(outputs[chunk.agent_id]) / (time.monotonic() - last_token_time[chunk.agent_id] + 0.001)
                print(f"\n[{chunk.agent_id}] DONE — ~{rate:.1f} tok/s")
            continue

        outputs[chunk.agent_id].append(chunk.text)
        print(f"[{chunk.agent_id}:{chunk.token_index}] {chunk.text}", end="", flush=True)

    await asyncio.gather(*tasks)
    return {"".join(v) if v else "" for k, v in outputs.items()}


async def main() -> None:
    agents = [
        ("CRITICAL",  "Explain why context limits matter for AI agents. Be brief.", 0),  # highest priority
        ("NORMAL",    "Explain why rate limits matter for AI agents. Be brief.", 5),
        ("LOW",       "Explain why token costs matter for AI agents. Be brief.", 10),
    ]
    await priority_multiplex(agents)


asyncio.run(main())

# Expected Token Savings: Priority queue ensures critical agent output isn't buried in noise
# Environment: Python 3.11+; lower priority number = processed first (min-heap)
```

## Option 4: SSE-Compatible Multiplexer for Web Streaming

```python
import asyncio
import json
import time
import anthropic
from collections.abc import AsyncIterator

client = anthropic.AsyncAnthropic()


async def sse_event(agent_id: str, text: str | None, done: bool = False) -> str:
    """Format a Server-Sent Event."""
    if done:
        data = json.dumps({"agent": agent_id, "done": True})
    else:
        data = json.dumps({"agent": agent_id, "text": text, "ts": time.time()})
    return f"data: {data}\n\n"


async def agent_stream(agent_id: str, prompt: str) -> AsyncIterator[str]:
    """Yield SSE-formatted events for one agent."""
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for chunk in stream.text_stream:
            yield await sse_event(agent_id, chunk)
    yield await sse_event(agent_id, None, done=True)


async def multiplex_to_sse(agents: list[tuple[str, str]]) -> AsyncIterator[str]:
    """Merge N agent streams into a single SSE stream."""
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def drain(agent_id: str, prompt: str) -> None:
        async for event in agent_stream(agent_id, prompt):
            await queue.put(event)
        await queue.put(None)

    tasks = [asyncio.create_task(drain(aid, p)) for aid, p in agents]
    active = len(agents)

    while active > 0:
        event = await queue.get()
        if event is None:
            active -= 1
        else:
            yield event

    await asyncio.gather(*tasks)


async def simulate_sse_endpoint(agents: list[tuple[str, str]]) -> None:
    """Simulate an HTTP endpoint streaming SSE to a client."""
    print("HTTP/1.1 200 OK")
    print("Content-Type: text/event-stream")
    print("Cache-Control: no-cache\n")

    async for event in multiplex_to_sse(agents):
        data = json.loads(event.replace("data: ", "").strip())
        if data.get("done"):
            print(f"[SSE] Agent {data['agent']} completed")
        else:
            print(f"[SSE:{data['agent']}] {data['text']}", end="", flush=True)


async def main() -> None:
    agents = [
        ("agent-1", "Describe asyncio in one paragraph."),
        ("agent-2", "Describe threading in one paragraph."),
    ]
    await simulate_sse_endpoint(agents)


asyncio.run(main())

# Expected Token Savings: SSE format adds ~50 bytes/event overhead; worth it for real-time web UX
# Environment: Python 3.11+; plug multiplex_to_sse() directly into FastAPI StreamingResponse
```

## Option 5: Buffered Multiplexer with Chunk Aggregation and Flush Interval

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()
FLUSH_INTERVAL = 0.1  # seconds between output flushes


@dataclass
class AgentState:
    label: str
    buffer: list[str] = field(default_factory=list)
    done: bool = False
    total_tokens: int = 0


async def fill_buffer(state: AgentState, prompt: str, tick: asyncio.Event) -> None:
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for chunk in stream.text_stream:
            state.buffer.append(chunk)
            state.total_tokens += 1
            tick.set()
    state.done = True
    tick.set()


async def buffered_multiplex(agents: list[tuple[str, str]]) -> dict[str, str]:
    states = {label: AgentState(label=label) for label, _ in agents}
    tick = asyncio.Event()

    tasks = [
        asyncio.create_task(fill_buffer(states[label], prompt, tick))
        for label, prompt in agents
    ]

    last_flush: dict[str, int] = {label: 0 for label in states}
    start = time.monotonic()

    while not all(s.done for s in states.values()):
        try:
            await asyncio.wait_for(asyncio.shield(tick.wait()), timeout=FLUSH_INTERVAL)
            tick.clear()
        except asyncio.TimeoutError:
            pass

        # Flush all accumulated chunks
        for label, state in states.items():
            new = state.buffer[last_flush[label]:]
            if new:
                text = "".join(new)
                elapsed = time.monotonic() - start
                print(f"[{label}@{elapsed:.1f}s] {text}", end="", flush=True)
                last_flush[label] = len(state.buffer)

    await asyncio.gather(*tasks)

    print(f"\n\n[STATS]")
    for label, state in states.items():
        print(f"  {label}: {state.total_tokens} chunks")

    return {label: "".join(state.buffer) for label, state in states.items()}


async def main() -> None:
    agents = [
        ("Haiku-A", "Explain coroutines in Python in 3 sentences."),
        ("Haiku-B", "Explain event loops in Python in 3 sentences."),
        ("Haiku-C", "Explain tasks in asyncio in 3 sentences."),
    ]
    await buffered_multiplex(agents)


asyncio.run(main())

# Expected Token Savings: Flush interval batches small chunks; reduces print() syscall overhead
# Environment: Python 3.11+; tune FLUSH_INTERVAL (0.05-0.2s) based on UX responsiveness requirements
```

## Option 6: Multi-Agent Stream Aggregator with Final Synthesis

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()


async def collect_stream(label: str, prompt: str) -> tuple[str, str]:
    """Stream and collect full output for one agent."""
    chunks = []
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for chunk in stream.text_stream:
            chunks.append(chunk)
            print(f"[{label}] {chunk}", end="", flush=True)
    print()
    return label, "".join(chunks)


async def synthesize(topic: str, results: dict[str, str]) -> str:
    """Stream a synthesis of all agent outputs."""
    context = "\n\n".join(f"[{label}]:\n{text}" for label, text in results.items())
    prompt = f"Synthesize these expert perspectives on '{topic}' into one concise answer:\n\n{context}"

    chunks = []
    print("\n[SYNTHESIS] ", end="", flush=True)
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for chunk in stream.text_stream:
            chunks.append(chunk)
            print(chunk, end="", flush=True)
    print()
    return "".join(chunks)


async def stream_and_synthesize(topic: str, agent_prompts: dict[str, str]) -> str:
    print(f"=== Streaming {len(agent_prompts)} agents in parallel ===\n")

    # Phase 1: Stream all agents simultaneously
    label_prompt_pairs = list(agent_prompts.items())
    raw_results = await asyncio.gather(*[
        collect_stream(label, prompt) for label, prompt in label_prompt_pairs
    ])
    results = dict(raw_results)

    # Phase 2: Stream synthesis
    print("\n=== Synthesis ===")
    return await synthesize(topic, results)


async def main() -> None:
    topic = "best practices for error handling in async Python agents"
    agents = {
        "Reliability":  f"What are the top 2 reliability practices for {topic}?",
        "Performance":  f"What are the top 2 performance practices for {topic}?",
        "Observability": f"What are the top 2 observability practices for {topic}?",
    }
    final = await stream_and_synthesize(topic, agents)
    print(f"\n\n=== Final Answer ===\n{final}")


asyncio.run(main())

# Expected Token Savings: Parallel collection 3x faster; synthesis costs ~300 tokens on haiku
# Environment: Python 3.11+; replace haiku with sonnet for synthesis on complex topics
```

## Comparison

| Option | Interleaving | Ordering | SSE-Ready | Synthesis | Best For |
|--------|-------------|---------|-----------|-----------|----------|
| 1. Simple Queue | Real-time interleaved | Arrival order | No | No | Debug/dev output |
| 2. Labeled Buffer | Per-agent sections | Per-agent | No | No | Structured display |
| 3. Priority Queue | Priority-ordered | Priority | No | No | Critical-path agents |
| 4. SSE Multiplexer | Real-time interleaved | Arrival order | Yes | No | Web streaming APIs |
| 5. Flush Interval | Batched flush | Arrival order | No | No | High-frequency chunks |
| 6. Collect + Synthesize | Sequential per-agent | Complete | No | Yes | Final merged answer |
