---
title: "Agent Doesn't Implement Async Channel with Select"
description: "AI agent workers communicate through shared mutable state or polling loops; this causes busy-waiting, missed events, and deadlocks when multiple event sources must be handled simultaneously."
category: concurrency
difficulty: advanced
tags: [asyncio, channel, select, queue, pubsub, fanout, backpressure, concurrency]
---

# Agent Doesn't Implement Async Channel with Select

## Problem

Agents that route messages between components often poll shared lists, use `time.sleep` loops, or build ad-hoc event dispatch. This wastes CPU, misses simultaneous events, and cannot prioritize across multiple event streams. The solution is typed async channels with `select`-style fan-in: wait on multiple sources simultaneously and handle whichever is ready first — just like Go's `select` or Rust's `tokio::select!`.

## Solution 1: asyncio.Queue as a Typed Channel with Select Fan-In

Python's `asyncio.Queue` is a channel. Fan-in across multiple queues with `asyncio.wait`.

```python
import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class AgentMessage:
    source: str
    payload: Any
    priority: int = 0

async def select_channels(*queues: asyncio.Queue) -> tuple[asyncio.Queue, Any]:
    """
    Wait for the first item available across any of the given queues.
    Returns (queue, item). Equivalent to Go's select on channels.
    """
    # Create a future per queue
    pending: dict[asyncio.Future, asyncio.Queue] = {}
    loop = asyncio.get_event_loop()

    async def _get(q: asyncio.Queue) -> tuple[asyncio.Queue, Any]:
        item = await q.get()
        return q, item

    tasks = {asyncio.ensure_future(_get(q)): q for q in queues}

    done, pending_tasks = await asyncio.wait(
        tasks.keys(), return_when=asyncio.FIRST_COMPLETED
    )

    # Cancel the rest — we only take the first
    for t in pending_tasks:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    result_q, item = next(iter(done)).result()
    return result_q, item

async def producer(name: str, channel: asyncio.Queue, count: int = 5):
    for i in range(count):
        msg = AgentMessage(source=name, payload=f"msg-{i}")
        await channel.put(msg)
        await asyncio.sleep(0.05)

async def agent_router(user_chan: asyncio.Queue, tool_chan: asyncio.Queue, done: asyncio.Event):
    """Router that handles whichever channel has data first."""
    while not done.is_set():
        try:
            source_q, msg = await asyncio.wait_for(
                select_channels(user_chan, tool_chan),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            continue

        if msg.source == "user":
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": str(msg.payload)}],
            )
            print(f"[user] -> {resp.content[0].text[:60]}")
        else:
            print(f"[tool:{msg.source}] handled: {msg.payload}")
        source_q.task_done()

async def main():
    user_chan: asyncio.Queue[AgentMessage] = asyncio.Queue()
    tool_chan: asyncio.Queue[AgentMessage] = asyncio.Queue()
    done = asyncio.Event()

    await asyncio.gather(
        producer("user", user_chan, 3),
        producer("tool_result", tool_chan, 3),
    )

    router_task = asyncio.create_task(agent_router(user_chan, tool_chan, done))
    await user_chan.join()
    await tool_chan.join()
    done.set()
    await router_task
```

**When to use**: Any agent that must handle inputs from multiple sources (user messages, tool results, webhooks) without polling.

---

## Solution 2: Priority Channel — High-Priority Messages Skip the Queue

Agent events have different urgencies: errors > tool results > user messages. Use a `PriorityQueue` to ensure critical events are processed first.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass(order=True)
class PriorityItem:
    priority: int          # lower number = higher priority
    sequence: int          # tiebreak by arrival order
    payload: Any = field(compare=False)

class PriorityChannel:
    """asyncio.PriorityQueue wrapper with typed send/receive."""

    def __init__(self, maxsize: int = 0):
        self._q: asyncio.PriorityQueue[PriorityItem] = asyncio.PriorityQueue(maxsize)
        self._seq = 0

    async def send(self, payload: Any, priority: int = 5) -> None:
        self._seq += 1
        item = PriorityItem(priority=priority, sequence=self._seq, payload=payload)
        await self._q.put(item)

    async def recv(self) -> Any:
        item = await self._q.get()
        self._q.task_done()
        return item.payload

    def send_nowait(self, payload: Any, priority: int = 5) -> None:
        self._seq += 1
        self._q.put_nowait(PriorityItem(priority=priority, sequence=self._seq, payload=payload))

    async def join(self) -> None:
        await self._q.join()

# Priority constants
P_CRITICAL = 0   # safety/error escalations
P_HIGH     = 1   # tool errors
P_NORMAL   = 5   # tool results, user messages
P_LOW      = 9   # background analytics

async def priority_agent_loop(channel: PriorityChannel, stop: asyncio.Event):
    history: list[dict] = []

    while not stop.is_set():
        try:
            event = await asyncio.wait_for(channel.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            continue

        kind = event.get("kind")
        if kind == "critical":
            print(f"[CRITICAL] {event['message']}")
            # Flush history, alert, escalate
            history.clear()
        elif kind == "user":
            history.append({"role": "user", "content": event["text"]})
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=history,
            )
            reply = resp.content[0].text
            history.append({"role": "assistant", "content": reply})
            print(f"[agent] {reply[:80]}")
        elif kind == "tool_result":
            history.append({
                "role": "user",
                "content": f"[Tool result: {event['result']}]",
            })

async def demo():
    chan = PriorityChannel(maxsize=100)
    stop = asyncio.Event()

    # Simulate mixed-priority events
    await chan.send({"kind": "user", "text": "Hello"}, priority=P_NORMAL)
    await chan.send({"kind": "tool_result", "result": "data"}, priority=P_NORMAL)
    await chan.send({"kind": "critical", "message": "Rate limit hit!"}, priority=P_CRITICAL)

    # Critical message processes first despite arriving last
    loop_task = asyncio.create_task(priority_agent_loop(chan, stop))
    await asyncio.sleep(2)
    stop.set()
    await loop_task
```

**When to use**: Agents that mix routine messages with error/safety signals. Ensures critical events are never buried behind a backlog of normal messages.

---

## Solution 3: Pub/Sub Fan-Out — One Producer, Multiple Subscriber Agents

Broadcast agent events to multiple subscribers without the producer knowing who is listening.

```python
import asyncio
from typing import Any, Callable
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class PubSubBus:
    """Simple in-process pub/sub. Topics are strings; payloads are arbitrary."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str, maxsize: int = 64) -> asyncio.Queue:
        async with self._lock:
            q: asyncio.Queue = asyncio.Queue(maxsize)
            self._subscribers.setdefault(topic, []).append(q)
            return q

    async def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subscribers.get(topic, [])
            try:
                subs.remove(q)
            except ValueError:
                pass

    async def publish(self, topic: str, payload: Any) -> int:
        """Publish to all subscribers. Returns number of subscribers reached."""
        async with self._lock:
            queues = list(self._subscribers.get(topic, []))

        sent = 0
        for q in queues:
            try:
                q.put_nowait(payload)
                sent += 1
            except asyncio.QueueFull:
                pass  # slow subscriber — drop or use a bounded buffer strategy
        return sent

bus = PubSubBus()

async def monitoring_agent(topic: str):
    """Subscriber: logs all events on a topic."""
    q = await bus.subscribe(topic)
    while True:
        event = await q.get()
        print(f"[monitor:{topic}] {event}")
        q.task_done()

async def llm_agent(name: str):
    """Subscriber: sends user messages to LLM."""
    q = await bus.subscribe("user_message")
    while True:
        event = await q.get()
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": event["text"]}],
        )
        # Publish response back to bus
        await bus.publish("agent_response", {
            "agent": name,
            "text": resp.content[0].text,
            "request_id": event.get("request_id"),
        })
        q.task_done()

async def demo():
    # Start subscribers
    monitor = asyncio.create_task(monitoring_agent("agent_response"))
    agent = asyncio.create_task(llm_agent("claude-1"))

    await asyncio.sleep(0.1)  # let subscribers register

    # Publish user messages — both monitor and llm_agent receive them independently
    for i in range(3):
        await bus.publish("user_message", {"text": f"Question {i}", "request_id": f"req-{i}"})

    await asyncio.sleep(2)
    monitor.cancel()
    agent.cancel()
```

**When to use**: Agents with cross-cutting concerns (logging, billing, analytics) that must observe message flows without being coupled to the main pipeline.

---

## Solution 4: Backpressure-Aware Channel — Block Producers When Consumers Lag

Prevent memory blowout when agent tool calls produce results faster than the LLM can process them.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class ChannelStats:
    sent: int = 0
    received: int = 0
    dropped: int = 0
    backpressure_events: int = 0

class BackpressureChannel:
    """
    Bounded channel that signals backpressure to producers.
    Producers can check capacity before sending to avoid blocking.
    """

    def __init__(self, maxsize: int, high_watermark: float = 0.8):
        self._q: asyncio.Queue = asyncio.Queue(maxsize)
        self._maxsize = maxsize
        self._high_watermark = high_watermark
        self.stats = ChannelStats()
        self._pressure_event = asyncio.Event()

    @property
    def size(self) -> int:
        return self._q.qsize()

    @property
    def capacity(self) -> float:
        return self.size / self._maxsize

    @property
    def under_pressure(self) -> bool:
        return self.capacity >= self._high_watermark

    async def send(self, item, timeout: float | None = None) -> bool:
        """
        Send item. Returns False if channel is full and times out.
        Emits backpressure signal when watermark crossed.
        """
        if self.under_pressure and not self._pressure_event.is_set():
            self._pressure_event.set()
            self.stats.backpressure_events += 1

        try:
            if timeout is not None:
                await asyncio.wait_for(self._q.put(item), timeout=timeout)
            else:
                await self._q.put(item)
            self.stats.sent += 1
            return True
        except asyncio.TimeoutError:
            self.stats.dropped += 1
            return False

    async def recv(self):
        item = await self._q.get()
        self.stats.received += 1
        if not self.under_pressure and self._pressure_event.is_set():
            self._pressure_event.clear()
        self._q.task_done()
        return item

    async def wait_for_capacity(self, timeout: float = 5.0) -> bool:
        """Producer calls this to wait until pressure drops."""
        try:
            await asyncio.wait_for(
                self._wait_not_full(),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def _wait_not_full(self):
        while self.under_pressure:
            await asyncio.sleep(0.05)

async def tool_producer(channel: BackpressureChannel, n: int):
    for i in range(n):
        if channel.under_pressure:
            print(f"[producer] backpressure! waiting... (size={channel.size})")
            await channel.wait_for_capacity()

        sent = await channel.send({"tool_result": f"result-{i}"}, timeout=2.0)
        if not sent:
            print(f"[producer] dropped result-{i}")

async def llm_consumer(channel: BackpressureChannel, stop: asyncio.Event):
    while not stop.is_set():
        try:
            item = await asyncio.wait_for(channel.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            continue

        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": str(item)}],
        )
        print(f"[consumer] processed: {resp.content[0].text[:40]}")

async def demo():
    channel = BackpressureChannel(maxsize=10, high_watermark=0.7)
    stop = asyncio.Event()

    consumer = asyncio.create_task(llm_consumer(channel, stop))
    await tool_producer(channel, 20)

    # Wait for consumer to drain
    while channel.size > 0:
        await asyncio.sleep(0.1)

    stop.set()
    await consumer
    print(f"Stats: {channel.stats}")
```

**When to use**: Agents where tool execution is faster than LLM processing (parallel tool calls feeding a single LLM loop). Prevents unbounded queue growth and OOM.

---

## Solution 5: Timeout Channel — Dead-Letter Queue for Expired Messages

Messages that are not consumed within a deadline are moved to a dead-letter queue rather than blocking the pipeline.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class TimedMessage:
    payload: Any
    deadline: float  # Unix timestamp
    source: str = ""

    def is_expired(self) -> bool:
        return time.monotonic() > self.deadline

class TimeoutChannel:
    """
    Messages expire after `ttl_seconds`. Expired messages go to dead_letter_queue.
    """

    def __init__(self, maxsize: int = 128, ttl_seconds: float = 30.0):
        self._q: asyncio.Queue[TimedMessage] = asyncio.Queue(maxsize)
        self._ttl = ttl_seconds
        self.dead_letter: list[TimedMessage] = []
        self._reaper: asyncio.Task | None = None

    async def start(self):
        self._reaper = asyncio.create_task(self._reap_expired())

    async def stop(self):
        if self._reaper:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass

    async def send(self, payload: Any, source: str = "") -> None:
        msg = TimedMessage(
            payload=payload,
            deadline=time.monotonic() + self._ttl,
            source=source,
        )
        await self._q.put(msg)

    async def recv(self) -> Optional[Any]:
        """Returns payload or None if message expired before delivery."""
        msg = await self._q.get()
        self._q.task_done()
        if msg.is_expired():
            self.dead_letter.append(msg)
            return None
        return msg.payload

    async def _reap_expired(self):
        """Periodically scan and remove expired messages from queue front."""
        while True:
            await asyncio.sleep(1.0)
            # Drain expired items from the front
            drained = []
            while not self._q.empty():
                try:
                    msg = self._q.get_nowait()
                    if msg.is_expired():
                        self.dead_letter.append(msg)
                    else:
                        drained.append(msg)
                    self._q.task_done()
                except asyncio.QueueEmpty:
                    break
            for msg in drained:
                await self._q.put(msg)

async def agent_consumer(channel: TimeoutChannel, stop: asyncio.Event):
    while not stop.is_set():
        try:
            payload = await asyncio.wait_for(channel.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if payload is None:
            continue  # expired — already in dead_letter

        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": str(payload)}],
        )
        print(f"[agent] {resp.content[0].text[:60]}")

async def demo():
    channel = TimeoutChannel(ttl_seconds=2.0)
    await channel.start()
    stop = asyncio.Event()

    consumer = asyncio.create_task(agent_consumer(channel, stop))

    await channel.send("urgent message", source="user")
    await asyncio.sleep(3.0)  # simulate slow consumer
    await channel.send("stale message", source="user")  # arrives after TTL window

    await asyncio.sleep(1.0)
    stop.set()
    await consumer
    await channel.stop()

    if channel.dead_letter:
        print(f"Dead-lettered {len(channel.dead_letter)} message(s):")
        for msg in channel.dead_letter:
            print(f"  - {msg.payload!r} from {msg.source!r}")
```

**When to use**: Agents processing time-sensitive requests (real-time voice, trading, alerts) where stale results are worse than no result.

---

## Solution 6: Select with Done/Cancel Channel — Clean Shutdown Propagation

Structured concurrent workers that stop cleanly when a cancellation signal is sent through a channel.

```python
import asyncio
from typing import Any, AsyncIterator
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class CancellableChannel:
    """
    Channel pair: data channel + cancel channel.
    Receivers exit cleanly when cancel fires.
    """

    def __init__(self, maxsize: int = 64):
        self._data: asyncio.Queue = asyncio.Queue(maxsize)
        self._cancel: asyncio.Event = asyncio.Event()

    async def send(self, item: Any) -> bool:
        if self._cancel.is_set():
            return False
        await self._data.put(item)
        return True

    async def recv(self) -> AsyncIterator[Any]:
        """Async generator that yields items until cancelled."""
        while not self._cancel.is_set():
            # Select: data OR cancel, whichever fires first
            data_task = asyncio.create_task(self._data.get())
            cancel_task = asyncio.create_task(self._cancel.wait())

            done, pending = await asyncio.wait(
                [data_task, cancel_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

            if cancel_task in done:
                # Drain remaining items
                if data_task in done:
                    item = data_task.result()
                    self._data.task_done()
                    yield item
                return

            if data_task in done:
                item = data_task.result()
                self._data.task_done()
                yield item

    def cancel(self) -> None:
        self._cancel.set()

async def worker(name: str, channel: CancellableChannel):
    """Worker that processes items until channel is cancelled."""
    async for item in channel.recv():
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": str(item)}],
        )
        print(f"[{name}] {resp.content[0].text[:60]}")
    print(f"[{name}] graceful shutdown complete")

async def demo():
    chan = CancellableChannel()

    # Start N workers on the same channel (fan-out)
    workers = [
        asyncio.create_task(worker(f"w{i}", chan))
        for i in range(3)
    ]

    # Send work
    for i in range(9):
        await chan.send(f"task {i}")

    await asyncio.sleep(3.0)  # let workers process

    # Signal shutdown: all workers exit after finishing current item
    chan.cancel()
    await asyncio.gather(*workers)
    print("All workers stopped.")
```

**When to use**: Long-running agent worker pools that need cooperative shutdown without `asyncio.CancelledError` leaking through business logic.

---

## Comparison

| Solution | Fan-In | Fan-Out | Priority | Backpressure | TTL/Expiry | Cancellation | Best For |
|---|---|---|---|---|---|---|---|
| Queue + select | Yes | No | No | No | No | No | Multiple input sources |
| Priority channel | No | No | Yes | No | No | No | Mixed urgency events |
| Pub/Sub fan-out | No | Yes | No | No | No | No | Cross-cutting subscribers |
| Backpressure channel | No | No | No | Yes | No | No | Fast producer / slow LLM |
| Timeout channel | No | No | No | No | Yes | No | Time-sensitive requests |
| Cancellable channel | No | Yes | No | No | No | Yes | Worker pool lifecycle |

**Rule of thumb**: Start with `asyncio.Queue` + `asyncio.wait` for fan-in. Add `PriorityQueue` for mixed-urgency signals. Add backpressure bounds when tool parallelism exceeds LLM throughput.
