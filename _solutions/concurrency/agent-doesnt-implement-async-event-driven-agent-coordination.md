---
title: "Agent Doesn't Implement Async Event-Driven Agent Coordination"
description: "Coordinate multiple agents through an async event bus instead of tight coupling, enabling reactive, loosely coupled multi-agent systems."
category: concurrency
difficulty: advanced
tags: [multi-agent, event-driven, asyncio, pub-sub, coordination, reactive]
---

# Agent Doesn't Implement Async Event-Driven Agent Coordination

## Problem

Multi-agent systems that call each other directly create brittle, tightly coupled architectures: one agent's failure cascades to all callers, adding a new agent requires modifying existing ones, and agents block waiting for each other. An event-driven architecture decouples producers from consumers — agents emit events and react to events they care about, with no knowledge of each other.

---

## Option 1: Simple Async Event Bus

```python
import asyncio
import anthropic
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

@dataclass
class Event:
    type: str
    payload: dict
    source: str

EventHandler = Callable[[Event], Awaitable[None]]

class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler):
        self._handlers[event_type].append(handler)

    async def publish(self, event: Event):
        handlers = self._handlers.get(event.type, []) + self._handlers.get("*", [])
        await asyncio.gather(*[h(event) for h in handlers], return_exceptions=True)

bus = EventBus()

# Agent 1: Research agent — listens for "research_request", emits "research_complete"
async def research_agent(event: Event):
    if event.type != "research_request":
        return
    query = event.payload["query"]
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Research: {query}. Be concise."}]
    )
    await bus.publish(Event(
        type="research_complete",
        payload={"query": query, "findings": resp.content[0].text, "request_id": event.payload.get("request_id")},
        source="research_agent"
    ))

# Agent 2: Summary agent — listens for "research_complete", emits "summary_ready"
async def summary_agent(event: Event):
    if event.type != "research_complete":
        return
    findings = event.payload["findings"]
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": f"Summarize in 2 sentences:\n{findings}"}]
    )
    await bus.publish(Event(
        type="summary_ready",
        payload={"summary": resp.content[0].text, "request_id": event.payload.get("request_id")},
        source="summary_agent"
    ))

# Agent 3: Logger — listens for all events
async def logger_agent(event: Event):
    print(f"[LOG] {event.source} -> {event.type}: {str(event.payload)[:80]}")

bus.subscribe("research_request", research_agent)
bus.subscribe("research_complete", summary_agent)
bus.subscribe("*", logger_agent)

async def main():
    results: dict[str, str] = {}
    done = asyncio.Event()

    async def collect_result(event: Event):
        if event.type == "summary_ready":
            results[event.payload["request_id"]] = event.payload["summary"]
            done.set()

    bus.subscribe("summary_ready", collect_result)

    await bus.publish(Event(
        type="research_request",
        payload={"query": "Benefits of event-driven architecture", "request_id": "req-1"},
        source="orchestrator"
    ))
    await asyncio.wait_for(done.wait(), timeout=30)
    print("Final summary:", results.get("req-1"))

asyncio.run(main())
```

---

## Option 2: Typed Event Queue with Priority Lanes

```python
import asyncio
import anthropic
from dataclasses import dataclass, field
from enum import IntEnum
import heapq
import time

client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3

@dataclass
class PriorityEvent:
    priority: Priority
    timestamp: float
    type: str
    payload: dict
    source: str

    def __lt__(self, other: "PriorityEvent"):
        return (self.priority, self.timestamp) < (other.priority, other.timestamp)

class PriorityEventBus:
    def __init__(self):
        self._queue: list[PriorityEvent] = []
        self._handlers: dict[str, list] = {}
        self._running = False

    def subscribe(self, event_type: str, handler, priority_filter: Priority | None = None):
        self._handlers.setdefault(event_type, []).append((handler, priority_filter))

    async def publish(self, event: PriorityEvent):
        heapq.heappush(self._queue, event)

    async def run(self, max_events: int = 100):
        self._running = True
        processed = 0
        while self._running and processed < max_events:
            if not self._queue:
                await asyncio.sleep(0.01)
                continue
            event = heapq.heappop(self._queue)
            handlers = self._handlers.get(event.type, []) + self._handlers.get("*", [])
            tasks = []
            for handler, pf in handlers:
                if pf is None or event.priority <= pf:
                    tasks.append(handler(event))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            processed += 1

    def stop(self):
        self._running = False

bus = PriorityEventBus()

async def classifier_agent(event: PriorityEvent):
    """Classifies incoming user queries and routes them."""
    query = event.payload.get("query", "")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system='Classify as "technical", "creative", or "factual". Return only the word.',
        messages=[{"role": "user", "content": query}]
    )
    category = resp.content[0].text.strip().lower()
    await bus.publish(PriorityEvent(
        priority=Priority.HIGH if category == "technical" else Priority.NORMAL,
        timestamp=time.time(),
        type=f"query.{category}",
        payload={**event.payload, "category": category},
        source="classifier_agent"
    ))

async def technical_agent(event: PriorityEvent):
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="You are a technical expert. Be precise.",
        messages=[{"role": "user", "content": event.payload["query"]}]
    )
    print(f"[TECHNICAL] {resp.content[0].text[:100]}...")

async def factual_agent(event: PriorityEvent):
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": event.payload["query"]}]
    )
    print(f"[FACTUAL] {resp.content[0].text[:100]}...")

async def creative_agent(event: PriorityEvent):
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="You are a creative writer.",
        messages=[{"role": "user", "content": event.payload["query"]}]
    )
    print(f"[CREATIVE] {resp.content[0].text[:100]}...")

bus.subscribe("query.incoming", classifier_agent)
bus.subscribe("query.technical", technical_agent)
bus.subscribe("query.factual", factual_agent)
bus.subscribe("query.creative", creative_agent)

async def main():
    queries = [
        "How does TCP/IP work?",
        "Write a haiku about async programming.",
        "What is the capital of France?",
    ]
    for q in queries:
        await bus.publish(PriorityEvent(
            priority=Priority.NORMAL, timestamp=time.time(),
            type="query.incoming", payload={"query": q}, source="user"
        ))
    await bus.run(max_events=20)

asyncio.run(main())
```

---

## Option 3: Saga Pattern for Multi-Step Agent Workflows

```python
import asyncio
import anthropic
import uuid
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class SagaStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPENSATING = "compensating"
    FAILED = "failed"

@dataclass
class SagaContext:
    saga_id: str
    status: SagaStatus
    steps_completed: list[str]
    data: dict
    compensations: list

@dataclass
class SagaEvent:
    type: str
    saga_id: str
    step: str
    payload: dict
    success: bool = True
    error: str | None = None

class SagaEventBus:
    def __init__(self):
        self._handlers: dict[str, list] = {}
        self._sagas: dict[str, SagaContext] = {}

    def subscribe(self, event_type: str, handler):
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event: SagaEvent):
        if event.saga_id not in self._sagas:
            self._sagas[event.saga_id] = SagaContext(
                saga_id=event.saga_id, status=SagaStatus.RUNNING,
                steps_completed=[], data={}, compensations=[]
            )
        ctx = self._sagas[event.saga_id]

        if event.success:
            ctx.steps_completed.append(event.step)
            ctx.data.update(event.payload)
        else:
            ctx.status = SagaStatus.COMPENSATING
            await self._compensate(ctx)
            return

        for handler in self._handlers.get(event.type, []):
            await handler(event, ctx)

    async def _compensate(self, ctx: SagaContext):
        print(f"[SAGA {ctx.saga_id}] Compensating {len(ctx.compensations)} steps...")
        for comp in reversed(ctx.compensations):
            try:
                await comp(ctx)
            except Exception as e:
                print(f"[SAGA] Compensation failed: {e}")
        ctx.status = SagaStatus.FAILED

bus = SagaEventBus()

# Step 1: Research
async def handle_start(event: SagaEvent, ctx: SagaContext):
    if event.step != "start":
        return
    query = ctx.data["query"]
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": f"Research: {query}"}]
    )
    # Register compensation
    ctx.compensations.append(lambda c: print(f"[COMP] Undoing research for saga {c.saga_id}"))
    await bus.publish(SagaEvent(
        type="saga.researched", saga_id=event.saga_id, step="research",
        payload={"research": resp.content[0].text}
    ))

# Step 2: Analyze
async def handle_researched(event: SagaEvent, ctx: SagaContext):
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"Analyze key points:\n{ctx.data['research']}"}]
    )
    ctx.compensations.append(lambda c: print(f"[COMP] Undoing analysis for saga {c.saga_id}"))
    await bus.publish(SagaEvent(
        type="saga.analyzed", saga_id=event.saga_id, step="analysis",
        payload={"analysis": resp.content[0].text}
    ))

# Step 3: Synthesize final answer
async def handle_analyzed(event: SagaEvent, ctx: SagaContext):
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": f"Synthesize into a final answer:\n\nResearch: {ctx.data['research']}\n\nAnalysis: {ctx.data['analysis']}"}]
    )
    ctx.status = SagaStatus.COMPLETED
    print(f"[SAGA COMPLETE] {resp.content[0].text[:150]}...")

bus.subscribe("saga.start", handle_start)
bus.subscribe("saga.researched", handle_researched)
bus.subscribe("saga.analyzed", handle_analyzed)

async def run_saga(query: str):
    saga_id = str(uuid.uuid4())[:8]
    await bus.publish(SagaEvent(
        type="saga.start", saga_id=saga_id, step="start",
        payload={"query": query}
    ))

asyncio.run(run_saga("What are the trade-offs between microservices and monoliths?"))
```

---

## Option 4: Reactive Agent Pipeline with Back-Pressure

```python
import asyncio
import anthropic
from dataclasses import dataclass
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

@dataclass
class AgentMessage:
    type: str
    content: str
    metadata: dict

class ReactiveAgent:
    def __init__(self, name: str, buffer_size: int = 10):
        self.name = name
        self._inbox: asyncio.Queue[AgentMessage | None] = asyncio.Queue(maxsize=buffer_size)
        self._subscribers: list["ReactiveAgent"] = []

    def subscribe(self, downstream: "ReactiveAgent"):
        self._subscribers.append(downstream)

    async def send(self, msg: AgentMessage):
        """Send with back-pressure: blocks if downstream is full."""
        try:
            await asyncio.wait_for(self._inbox.put(msg), timeout=5.0)
        except asyncio.TimeoutError:
            print(f"[{self.name}] Back-pressure: dropping message (inbox full)")

    async def emit(self, msg: AgentMessage):
        """Broadcast to all subscribers."""
        await asyncio.gather(*[sub.send(msg) for sub in self._subscribers], return_exceptions=True)

    async def run(self):
        while True:
            msg = await self._inbox.get()
            if msg is None:
                break
            try:
                await self.process(msg)
            except Exception as e:
                print(f"[{self.name}] Error: {e}")
            finally:
                self._inbox.task_done()

    async def process(self, msg: AgentMessage):
        raise NotImplementedError

class ExtractorAgent(ReactiveAgent):
    async def process(self, msg: AgentMessage):
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system="Extract 3 key facts as bullet points.",
            messages=[{"role": "user", "content": msg.content}]
        )
        await self.emit(AgentMessage(
            type="facts_extracted",
            content=resp.content[0].text,
            metadata={**msg.metadata, "stage": "extraction"}
        ))

class TranslatorAgent(ReactiveAgent):
    def __init__(self, name: str, target_lang: str, buffer_size: int = 10):
        super().__init__(name, buffer_size)
        self.target_lang = target_lang

    async def process(self, msg: AgentMessage):
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": f"Translate to {self.target_lang}:\n{msg.content}"}]
        )
        await self.emit(AgentMessage(
            type="translated",
            content=resp.content[0].text,
            metadata={**msg.metadata, "language": self.target_lang}
        ))

class SinkAgent(ReactiveAgent):
    def __init__(self, name: str):
        super().__init__(name)
        self.results: list[AgentMessage] = []

    async def process(self, msg: AgentMessage):
        self.results.append(msg)
        print(f"[SINK] {msg.metadata}: {msg.content[:80]}...")

async def main():
    extractor = ExtractorAgent("extractor", buffer_size=5)
    translator_es = TranslatorAgent("translator_es", "Spanish", buffer_size=5)
    translator_fr = TranslatorAgent("translator_fr", "French", buffer_size=5)
    sink = SinkAgent("sink")

    # Pipeline: extractor -> [translator_es, translator_fr] -> sink
    extractor.subscribe(translator_es)
    extractor.subscribe(translator_fr)
    translator_es.subscribe(sink)
    translator_fr.subscribe(sink)

    # Start all agents
    tasks = [
        asyncio.create_task(extractor.run()),
        asyncio.create_task(translator_es.run()),
        asyncio.create_task(translator_fr.run()),
        asyncio.create_task(sink.run()),
    ]

    # Feed input
    texts = [
        "Quantum computing uses qubits to perform calculations exponentially faster than classical computers.",
        "Machine learning models learn patterns from data without being explicitly programmed.",
    ]
    for i, text in enumerate(texts):
        await extractor.send(AgentMessage(type="raw_text", content=text, metadata={"id": i}))

    await asyncio.sleep(15)  # let pipeline drain

    # Shutdown
    for agent in [extractor, translator_es, translator_fr, sink]:
        await agent._inbox.put(None)
    await asyncio.gather(*tasks, return_exceptions=True)

asyncio.run(main())
```

---

## Option 5: Event Sourcing with Agent State Replay

```python
import asyncio
import anthropic
import json
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class DomainEvent:
    event_id: int
    timestamp: float
    type: str
    agent_id: str
    payload: dict

class EventStore:
    def __init__(self):
        self._events: list[DomainEvent] = []
        self._counter = 0
        self._subscribers: list = []

    def subscribe(self, handler):
        self._subscribers.append(handler)

    async def append(self, event_type: str, agent_id: str, payload: dict) -> DomainEvent:
        self._counter += 1
        event = DomainEvent(
            event_id=self._counter,
            timestamp=time.time(),
            type=event_type,
            agent_id=agent_id,
            payload=payload
        )
        self._events.append(event)
        await asyncio.gather(*[h(event) for h in self._subscribers], return_exceptions=True)
        return event

    def replay(self, after_id: int = 0) -> list[DomainEvent]:
        return [e for e in self._events if e.event_id > after_id]

    def agent_history(self, agent_id: str) -> list[DomainEvent]:
        return [e for e in self._events if e.agent_id == agent_id]

store = EventStore()

class StatefulAgent:
    def __init__(self, agent_id: str, store: EventStore):
        self.agent_id = agent_id
        self.store = store
        self.state: dict = {}
        self.last_seen_event: int = 0

    async def restore_from_events(self):
        """Rebuild state from event log."""
        for event in self.store.replay(after_id=0):
            if event.agent_id == self.agent_id:
                self.state.update(event.payload.get("state_delta", {}))
                self.last_seen_event = event.event_id

    async def handle(self, event: DomainEvent):
        raise NotImplementedError

class ResearchAgent(StatefulAgent):
    async def handle(self, event: DomainEvent):
        if event.type != "research.requested":
            return
        query = event.payload["query"]
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": f"Research: {query}"}]
        )
        findings = resp.content[0].text
        await self.store.append("research.completed", self.agent_id, {
            "query": query,
            "findings": findings,
            "request_id": event.payload.get("request_id"),
            "state_delta": {"last_query": query, "query_count": self.state.get("query_count", 0) + 1}
        })

class SynthesisAgent(StatefulAgent):
    async def handle(self, event: DomainEvent):
        if event.type != "research.completed":
            return
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": f"Synthesize:\n{event.payload['findings']}"}]
        )
        await self.store.append("synthesis.ready", self.agent_id, {
            "request_id": event.payload.get("request_id"),
            "synthesis": resp.content[0].text,
            "state_delta": {"syntheses_produced": self.state.get("syntheses_produced", 0) + 1}
        })

async def main():
    ra = ResearchAgent("research-1", store)
    sa = SynthesisAgent("synthesis-1", store)
    store.subscribe(ra.handle)
    store.subscribe(sa.handle)

    await store.append("research.requested", "orchestrator", {
        "query": "How does event sourcing improve system resilience?",
        "request_id": "r1"
    })
    await asyncio.sleep(15)

    print("\n=== Event Log ===")
    for e in store.replay():
        print(f"[{e.event_id}] {e.type} from {e.agent_id}: {str(e.payload)[:80]}")

asyncio.run(main())
```

---

## Option 6: Distributed Agent Coordination via asyncio Streams

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class Envelope:
    sender: str
    recipient: str  # agent name or "*" for broadcast
    message_type: str
    body: dict

class AgentRouter:
    def __init__(self):
        self._agents: dict[str, asyncio.Queue] = {}
        self._running: dict[str, asyncio.Task] = {}

    def register(self, name: str, handler) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._agents[name] = q
        self._running[name] = asyncio.create_task(self._run_agent(name, q, handler))
        return q

    async def _run_agent(self, name: str, q: asyncio.Queue, handler):
        while True:
            env: Envelope | None = await q.get()
            if env is None:
                break
            try:
                await handler(env, self)
            except Exception as e:
                print(f"[ROUTER] Agent {name} error: {e}")

    async def send(self, env: Envelope):
        if env.recipient == "*":
            for name, q in self._agents.items():
                if name != env.sender:
                    await q.put(env)
        elif env.recipient in self._agents:
            await self._agents[env.recipient].put(env)

    async def shutdown(self):
        for q in self._agents.values():
            await q.put(None)
        await asyncio.gather(*self._running.values(), return_exceptions=True)

router = AgentRouter()

async def orchestrator(env: Envelope, r: AgentRouter):
    if env.message_type == "start":
        query = env.body["query"]
        # Fan out to multiple specialist agents
        await asyncio.gather(
            r.send(Envelope("orchestrator", "analyst", "analyze", {"query": query, "session": env.body["session"]})),
            r.send(Envelope("orchestrator", "critic", "critique", {"query": query, "session": env.body["session"]})),
        )
    elif env.message_type == "result":
        print(f"[ORCHESTRATOR] Got result from {env.sender}: {env.body.get('result', '')[:80]}...")

async def analyst(env: Envelope, r: AgentRouter):
    if env.message_type != "analyze":
        return
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="You are an analytical agent. Provide structured analysis.",
        messages=[{"role": "user", "content": env.body["query"]}]
    )
    await r.send(Envelope("analyst", "orchestrator", "result", {
        "result": resp.content[0].text,
        "role": "analysis",
        "session": env.body["session"]
    }))

async def critic(env: Envelope, r: AgentRouter):
    if env.message_type != "critique":
        return
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="You are a critical thinking agent. Identify weaknesses and risks.",
        messages=[{"role": "user", "content": env.body["query"]}]
    )
    await r.send(Envelope("critic", "orchestrator", "result", {
        "result": resp.content[0].text,
        "role": "critique",
        "session": env.body["session"]
    }))

router.register("orchestrator", orchestrator)
router.register("analyst", analyst)
router.register("critic", critic)

async def main():
    await router.send(Envelope(
        sender="user", recipient="orchestrator",
        message_type="start",
        body={"query": "Should we migrate to a microservices architecture?", "session": "sess-1"}
    ))
    await asyncio.sleep(15)
    await router.shutdown()

asyncio.run(main())
```

---

## Comparison

| Option | Pattern | Coupling | Failure Isolation | Best For |
|--------|---------|----------|-------------------|----------|
| 1 – Simple Event Bus | Pub/sub | Low | Good | Small agent networks |
| 2 – Priority Queue | Priority pub/sub | Low | Good | Mixed-criticality workloads |
| 3 – Saga | Choreography + compensation | Medium | Excellent | Multi-step transactions |
| 4 – Reactive Pipeline | Stream pipeline + back-pressure | Low | Good | High-throughput pipelines |
| 5 – Event Sourcing | Append-only log + replay | Low | Excellent | Auditable, recoverable workflows |
| 6 – Agent Router | Message routing | Low | Good | Distributed agent meshes |

**Recommendation:** Start with Option 1 for prototypes. Use Option 3 (Saga) when multi-step workflows need compensation logic. Use Option 5 (Event Sourcing) in production when you need audit trails, replay, and crash recovery. Combine Option 4's back-pressure with any pattern when throughput is high.
