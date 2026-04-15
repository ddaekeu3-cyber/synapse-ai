---
layout: solution
title: "Agent Doesn't Implement Actor Model for Isolated State"
category: concurrency
description: "Agents that share mutable state across coroutines suffer race conditions, deadlocks, and hard-to-debug corruption. The actor model gives each agent its own private state and communicates only through message passing."
tags: [actor-model, concurrency, isolated-state, message-passing, asyncio, multi-agent]
---

# Agent Doesn't Implement Actor Model for Isolated State

## The Problem

When multiple agent coroutines share a mutable dictionary, database connection, or context object, they race each other for writes. Locks prevent some races but create deadlock opportunities and reduce parallelism. The actor model eliminates shared state entirely: each actor owns its state privately, and the only way to read or change another actor's state is to send it a message. This makes concurrent agent systems dramatically easier to reason about.

---

## Option 1: Simple Asyncio Actor with Message Queue

Each agent is an async task with a private inbox queue and isolated state.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()

@dataclass
class Message:
    sender: str
    msg_type: str
    payload: Any
    reply_to: asyncio.Queue | None = None  # For request-reply pattern

class AgentActor:
    """Base actor: private state + message queue + async run loop."""

    def __init__(self, actor_id: str):
        self.actor_id = actor_id
        self._inbox: asyncio.Queue = asyncio.Queue()
        # Private state — never accessed directly from outside
        self._state: dict = {}
        self._message_count = 0

    async def send(self, msg: Message):
        """Send a message to this actor's inbox."""
        await self._inbox.put(msg)

    async def run(self):
        """Main message processing loop."""
        while True:
            msg = await self._inbox.get()
            try:
                await self._handle(msg)
            except Exception as e:
                print(f"[{self.actor_id}] Error handling {msg.msg_type}: {e}")
            finally:
                self._inbox.task_done()
                self._message_count += 1

    async def _handle(self, msg: Message):
        raise NotImplementedError

class ConversationActor(AgentActor):
    """Actor that maintains an isolated conversation history and answers questions."""

    def __init__(self, actor_id: str, system_prompt: str):
        super().__init__(actor_id)
        # Private conversation history — no other actor can touch this
        self._state["history"] = []
        self._state["system_prompt"] = system_prompt
        self._state["total_tokens"] = 0

    async def _handle(self, msg: Message):
        if msg.msg_type == "query":
            response = await self._answer(msg.payload)
            if msg.reply_to:
                await msg.reply_to.put({"actor": self.actor_id, "response": response})

        elif msg.msg_type == "reset":
            self._state["history"] = []
            print(f"[{self.actor_id}] State reset")

        elif msg.msg_type == "get_stats":
            stats = {
                "actor_id": self.actor_id,
                "history_turns": len(self._state["history"]),
                "total_tokens": self._state["total_tokens"],
                "messages_processed": self._message_count
            }
            if msg.reply_to:
                await msg.reply_to.put(stats)

    async def _answer(self, question: str) -> str:
        self._state["history"].append({"role": "user", "content": question})

        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=self._state["system_prompt"],
            messages=self._state["history"]
        )
        answer = resp.content[0].text
        self._state["history"].append({"role": "assistant", "content": answer})
        self._state["total_tokens"] += resp.usage.input_tokens + resp.usage.output_tokens
        return answer

async def demo_actor_isolation():
    """Show that two actors maintain completely separate state."""
    # Create two actors with different personas — state is 100% isolated
    alice = ConversationActor("alice", "You are Alice, a helpful math tutor.")
    bob = ConversationActor("bob", "You are Bob, a creative writing coach.")

    # Start actor run loops
    alice_task = asyncio.create_task(alice.run())
    bob_task = asyncio.create_task(bob.run())

    # Send messages concurrently — no shared state, no race conditions
    reply_q: asyncio.Queue = asyncio.Queue()

    await asyncio.gather(
        alice.send(Message("user", "query", "What is 12 * 15?", reply_q)),
        bob.send(Message("user", "query", "Give me an opening line for a mystery novel.", reply_q)),
        alice.send(Message("user", "query", "Now what is that result squared?", reply_q)),
    )

    # Collect 3 replies
    for _ in range(3):
        result = await asyncio.wait_for(reply_q.get(), timeout=30)
        print(f"[{result['actor']}]: {result['response'][:100]}")

    # Get stats from each actor via message passing
    stats_q: asyncio.Queue = asyncio.Queue()
    await alice.send(Message("system", "get_stats", None, stats_q))
    await bob.send(Message("system", "get_stats", None, stats_q))

    for _ in range(2):
        stats = await asyncio.wait_for(stats_q.get(), timeout=10)
        print(f"Stats: {stats}")

    alice_task.cancel()
    bob_task.cancel()

asyncio.run(demo_actor_isolation())

# Expected Token Savings: Actor isolation prevents accidental shared-state bugs that require expensive re-runs
# Environment: multi-agent systems, concurrent user sessions, agent orchestration
```

---

## Option 2: Actor Registry with Named Message Routing

Maintain a registry of named actors; route messages by actor name without direct references.

```python
import anthropic
import asyncio
from dataclasses import dataclass
from typing import Any

client = anthropic.AsyncAnthropic()

@dataclass
class Envelope:
    to: str       # Target actor name
    from_: str    # Sender actor name
    msg_type: str
    payload: Any
    correlation_id: str | None = None

class ActorRegistry:
    """Central registry for actor lookup and message routing."""

    def __init__(self):
        self._actors: dict[str, "RegistryActor"] = {}
        self._reply_boxes: dict[str, asyncio.Queue] = {}

    def register(self, actor: "RegistryActor"):
        self._actors[actor.name] = actor

    async def send(self, envelope: Envelope):
        """Route envelope to named actor."""
        target = self._actors.get(envelope.to)
        if target:
            await target.inbox.put(envelope)
        else:
            print(f"[Registry] Unknown actor: {envelope.to}")

    async def request(self, envelope: Envelope, timeout: float = 30) -> Any:
        """Send message and wait for reply."""
        import uuid
        cid = str(uuid.uuid4())[:8]
        envelope.correlation_id = cid
        reply_q: asyncio.Queue = asyncio.Queue()
        self._reply_boxes[cid] = reply_q
        await self.send(envelope)
        try:
            return await asyncio.wait_for(reply_q.get(), timeout=timeout)
        finally:
            self._reply_boxes.pop(cid, None)

    async def reply(self, correlation_id: str, response: Any):
        """Post reply to a request."""
        q = self._reply_boxes.get(correlation_id)
        if q:
            await q.put(response)

class RegistryActor:
    def __init__(self, name: str, registry: ActorRegistry):
        self.name = name
        self.inbox: asyncio.Queue = asyncio.Queue()
        self._registry = registry
        self._state: dict = {}
        registry.register(self)

    async def run(self):
        while True:
            envelope = await self.inbox.get()
            try:
                result = await self._handle(envelope)
                if envelope.correlation_id and result is not None:
                    await self._registry.reply(envelope.correlation_id, result)
            except Exception as e:
                print(f"[{self.name}] Error: {e}")
            finally:
                self.inbox.task_done()

    async def _handle(self, envelope: Envelope) -> Any:
        raise NotImplementedError

class SummarizerActor(RegistryActor):
    """Summarizes text passed to it — private state: summary cache."""

    async def _handle(self, envelope: Envelope) -> Any:
        if envelope.msg_type == "summarize":
            text = envelope.payload
            cache_key = text[:50]

            if cache_key in self._state:
                return {"cached": True, "summary": self._state[cache_key]}

            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": f"Summarize in one sentence: {text[:500]}"}]
            )
            summary = resp.content[0].text
            self._state[cache_key] = summary  # Private cache, no contention
            return {"cached": False, "summary": summary}
        return None

class ClassifierActor(RegistryActor):
    """Classifies text sentiment — private state: classification history."""

    async def _handle(self, envelope: Envelope) -> Any:
        if envelope.msg_type == "classify":
            text = envelope.payload
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=20,
                messages=[{"role": "user", "content": f"Sentiment (positive/negative/neutral): {text[:200]}\nReply one word only."}]
            )
            label = resp.content[0].text.strip().lower()
            self._state.setdefault("history", []).append(label)
            return {"label": label, "total_classified": len(self._state["history"])}
        return None

async def demo_registry():
    registry = ActorRegistry()
    summarizer = SummarizerActor("summarizer", registry)
    classifier = ClassifierActor("classifier", registry)

    tasks = [
        asyncio.create_task(summarizer.run()),
        asyncio.create_task(classifier.run()),
    ]

    texts = [
        "Python is a versatile programming language loved by data scientists and web developers.",
        "This product broke after two days and customer service was completely unhelpful.",
        "The weather today is partly cloudy with a chance of rain in the afternoon.",
    ]

    # Send to both actors concurrently — each has isolated state
    results = await asyncio.gather(*[
        asyncio.gather(
            registry.request(Envelope("summarizer", "user", "summarize", t)),
            registry.request(Envelope("classifier", "user", "classify", t))
        )
        for t in texts
    ])

    for (summ, cls), text in zip(results, texts):
        print(f"Text: {text[:60]}")
        print(f"  Summary: {summ['summary'][:80]} (cached={summ['cached']})")
        print(f"  Sentiment: {cls['label']} (total={cls['total_classified']})\n")

    for t in tasks:
        t.cancel()

asyncio.run(demo_registry())

# Expected Token Savings: Summarizer cache in private actor state avoids duplicate API calls with zero lock overhead
# Environment: pipeline agents, multi-role orchestration, microservices-style agent graphs
```

---

## Option 3: Supervised Actor Tree with Restart Policy

Build a supervision tree where parent actors monitor and restart failing children.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class RestartPolicy(str, Enum):
    ALWAYS = "always"      # Restart on any failure
    ON_ERROR = "on_error"  # Restart only on exceptions
    NEVER = "never"        # Don't restart

@dataclass
class ActorSpec:
    actor_id: str
    factory: callable  # () -> Actor instance
    restart_policy: RestartPolicy = RestartPolicy.ON_ERROR
    max_restarts: int = 3

class SupervisedActor:
    def __init__(self, actor_id: str):
        self.actor_id = actor_id
        self.inbox: asyncio.Queue = asyncio.Queue()
        self._state: dict = {}
        self._running = True

    async def handle(self, msg: dict) -> dict | None:
        raise NotImplementedError

    async def run(self):
        while self._running:
            try:
                msg = await asyncio.wait_for(self.inbox.get(), timeout=5.0)
                result = await self.handle(msg)
                if msg.get("reply_q") and result is not None:
                    await msg["reply_q"].put(result)
                self.inbox.task_done()
            except asyncio.TimeoutError:
                continue  # Idle, keep running
            except Exception as e:
                raise  # Let supervisor handle

    def stop(self):
        self._running = False

class LLMActor(SupervisedActor):
    """Actor that calls LLM — may fail due to API errors."""

    def __init__(self, actor_id: str, role: str):
        super().__init__(actor_id)
        self._state["role"] = role
        self._state["calls"] = 0
        self._state["errors"] = 0

    async def handle(self, msg: dict) -> dict | None:
        if msg.get("type") == "query":
            self._state["calls"] += 1
            try:
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    system=f"You are {self._state['role']}.",
                    messages=[{"role": "user", "content": msg["text"]}]
                )
                return {
                    "actor": self.actor_id,
                    "response": resp.content[0].text,
                    "calls": self._state["calls"]
                }
            except Exception as e:
                self._state["errors"] += 1
                raise
        return None

class Supervisor:
    """Monitors child actors and restarts them on failure."""

    def __init__(self):
        self._specs: dict[str, ActorSpec] = {}
        self._actors: dict[str, SupervisedActor] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._restart_counts: dict[str, int] = {}

    def add_child(self, spec: ActorSpec):
        self._specs[spec.actor_id] = spec
        self._restart_counts[spec.actor_id] = 0

    async def start(self):
        """Start all child actors."""
        for spec in self._specs.values():
            await self._start_actor(spec)

    async def _start_actor(self, spec: ActorSpec):
        actor = spec.factory()
        self._actors[spec.actor_id] = actor
        task = asyncio.create_task(self._watched_run(spec, actor))
        self._tasks[spec.actor_id] = task

    async def _watched_run(self, spec: ActorSpec, actor: SupervisedActor):
        """Run actor and handle restarts."""
        try:
            await actor.run()
        except Exception as e:
            restarts = self._restart_counts[spec.actor_id]
            print(f"[Supervisor] {spec.actor_id} failed (restart {restarts}/{spec.max_restarts}): {e}")

            if spec.restart_policy == RestartPolicy.NEVER:
                print(f"[Supervisor] {spec.actor_id}: no restart policy, stopping")
                return

            if restarts < spec.max_restarts:
                self._restart_counts[spec.actor_id] += 1
                await asyncio.sleep(0.5 * (2 ** restarts))  # Exponential backoff
                print(f"[Supervisor] Restarting {spec.actor_id}")
                # Drain old inbox into new actor
                old_inbox = self._actors[spec.actor_id].inbox
                await self._start_actor(spec)
                new_actor = self._actors[spec.actor_id]
                while not old_inbox.empty():
                    msg = old_inbox.get_nowait()
                    await new_actor.inbox.put(msg)
            else:
                print(f"[Supervisor] {spec.actor_id}: max restarts reached, giving up")

    async def send(self, actor_id: str, msg: dict) -> dict | None:
        """Send message to a child actor."""
        actor = self._actors.get(actor_id)
        if actor:
            reply_q: asyncio.Queue = asyncio.Queue()
            msg["reply_q"] = reply_q
            await actor.inbox.put(msg)
            try:
                return await asyncio.wait_for(reply_q.get(), timeout=30)
            except asyncio.TimeoutError:
                return {"error": "timeout"}
        return {"error": f"unknown actor: {actor_id}"}

    async def stop_all(self):
        for actor in self._actors.values():
            actor.stop()
        for task in self._tasks.values():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

async def demo_supervision():
    supervisor = Supervisor()

    supervisor.add_child(ActorSpec(
        actor_id="researcher",
        factory=lambda: LLMActor("researcher", "a research assistant"),
        restart_policy=RestartPolicy.ON_ERROR,
        max_restarts=3
    ))
    supervisor.add_child(ActorSpec(
        actor_id="writer",
        factory=lambda: LLMActor("writer", "a creative writer"),
        restart_policy=RestartPolicy.ON_ERROR,
        max_restarts=3
    ))

    await supervisor.start()
    await asyncio.sleep(0.1)  # Let actors start

    # Send queries to isolated actors
    results = await asyncio.gather(
        supervisor.send("researcher", {"type": "query", "text": "What is the actor model in computing?"}),
        supervisor.send("writer", {"type": "query", "text": "Write a haiku about concurrency."}),
    )

    for r in results:
        if r and "response" in r:
            print(f"[{r['actor']}] (call #{r['calls']}): {r['response'][:120]}")

    await supervisor.stop_all()

asyncio.run(demo_supervision())

# Expected Token Savings: Supervision prevents silent actor death; isolated restart preserves in-flight state
# Environment: long-running agent systems, production multi-agent pipelines, fault-tolerant orchestrators
```

---

## Option 4: State Machine Actor

Each actor progresses through defined states; invalid state transitions are impossible.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

client = anthropic.AsyncAnthropic()

class TaskState(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    DONE = "done"
    FAILED = "failed"

# Valid state transitions
TRANSITIONS = {
    TaskState.IDLE: [TaskState.PLANNING],
    TaskState.PLANNING: [TaskState.EXECUTING, TaskState.FAILED],
    TaskState.EXECUTING: [TaskState.REVIEWING, TaskState.FAILED],
    TaskState.REVIEWING: [TaskState.DONE, TaskState.EXECUTING],  # Can loop back
    TaskState.DONE: [],
    TaskState.FAILED: [TaskState.IDLE],  # Can retry
}

@dataclass
class WorkItem:
    task_id: str
    description: str

class StateMachineActor:
    """Actor whose private state follows a strict state machine."""

    def __init__(self, actor_id: str):
        self.actor_id = actor_id
        self.inbox: asyncio.Queue = asyncio.Queue()
        # Private state with enforced state machine
        self._current_state = TaskState.IDLE
        self._work_item: WorkItem | None = None
        self._plan: str = ""
        self._result: str = ""
        self._history: list[dict] = []

    def _transition(self, new_state: TaskState) -> bool:
        """Attempt a state transition. Returns True if valid."""
        allowed = TRANSITIONS.get(self._current_state, [])
        if new_state not in allowed:
            print(f"[{self.actor_id}] Invalid transition: {self._current_state} → {new_state}")
            return False
        self._history.append({
            "from": self._current_state.value,
            "to": new_state.value
        })
        self._current_state = new_state
        return True

    async def run(self):
        while True:
            msg = await self.inbox.get()
            await self._handle(msg)
            self.inbox.task_done()

    async def _handle(self, msg: dict):
        msg_type = msg.get("type")
        reply_q = msg.get("reply_q")

        if msg_type == "assign" and self._transition(TaskState.PLANNING):
            self._work_item = msg["work_item"]
            await self._plan_task()
            result = {"actor": self.actor_id, "state": self._current_state.value, "plan": self._plan}
            if reply_q:
                await reply_q.put(result)

        elif msg_type == "execute" and self._transition(TaskState.EXECUTING):
            await self._execute_task()
            result = {"actor": self.actor_id, "state": self._current_state.value, "result": self._result}
            if reply_q:
                await reply_q.put(result)

        elif msg_type == "get_status":
            status = {
                "actor": self.actor_id,
                "state": self._current_state.value,
                "work_item": self._work_item.task_id if self._work_item else None,
                "transitions": len(self._history)
            }
            if reply_q:
                await reply_q.put(status)

        elif msg_type == "invalid_test":
            # Test that invalid transitions are blocked
            success = self._transition(TaskState.DONE)  # Should fail from IDLE
            if reply_q:
                await reply_q.put({"blocked": not success, "current_state": self._current_state.value})

    async def _plan_task(self):
        assert self._work_item is not None
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": f"Create a 3-step plan: {self._work_item.description}"}]
        )
        self._plan = resp.content[0].text
        self._transition(TaskState.EXECUTING)

    async def _execute_task(self):
        assert self._work_item is not None
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": f"Execute this task: {self._work_item.description}\nPlan: {self._plan}"}]
        )
        self._result = resp.content[0].text
        self._transition(TaskState.REVIEWING)
        self._transition(TaskState.DONE)

async def demo_state_machine():
    actors = [
        StateMachineActor(f"worker_{i}")
        for i in range(3)
    ]

    tasks_list = [asyncio.create_task(a.run()) for a in actors]

    reply_q: asyncio.Queue = asyncio.Queue()

    # Test invalid transition
    await actors[0].inbox.put({"type": "invalid_test", "reply_q": reply_q})
    guard = await asyncio.wait_for(reply_q.get(), timeout=5)
    print(f"Invalid transition blocked: {guard['blocked']} (state: {guard['current_state']})")

    # Assign work items
    work_items = [
        WorkItem("task_001", "Write a Python function to sort a list"),
        WorkItem("task_002", "Explain REST vs GraphQL"),
        WorkItem("task_003", "Summarize actor model benefits"),
    ]

    for actor, item in zip(actors, work_items):
        await actor.inbox.put({"type": "assign", "work_item": item, "reply_q": reply_q})

    # Collect plans
    for _ in range(3):
        result = await asyncio.wait_for(reply_q.get(), timeout=30)
        print(f"[{result['actor']}] state={result['state']}")
        print(f"  Plan: {result.get('plan', '')[:100]}")

    for t in tasks_list:
        t.cancel()

asyncio.run(demo_state_machine())

# Expected Token Savings: State machine prevents impossible double-execution; no wasted API calls from state corruption
# Environment: multi-step autonomous agents, workflow engines, agents with approval steps
```

---

## Option 5: Actor Pool with Work Stealing

Pool of identical worker actors; idle workers steal work from busy workers' queues.

```python
import anthropic
import asyncio
import random
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class WorkUnit:
    unit_id: str
    prompt: str
    priority: int = 5

class PooledWorkerActor:
    """Worker actor with isolated state; participates in work-stealing pool."""

    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self.local_queue: asyncio.Queue = asyncio.Queue()
        # Completely private state
        self._processed = 0
        self._stolen = 0
        self._results: list[dict] = []

    async def process(self, unit: WorkUnit) -> dict:
        """Process a single work unit."""
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": unit.prompt}]
        )
        self._processed += 1
        result = {
            "unit_id": unit.unit_id,
            "worker": self.worker_id,
            "response": resp.content[0].text[:100],
            "stolen": False
        }
        self._results.append(result)
        return result

    def get_stats(self) -> dict:
        return {
            "worker": self.worker_id,
            "processed": self._processed,
            "stolen": self._stolen,
            "queue_depth": self.local_queue.qsize()
        }

class WorkStealingPool:
    """Pool of worker actors with work-stealing for load balancing."""

    def __init__(self, n_workers: int):
        self._workers = [PooledWorkerActor(f"worker_{i}") for i in range(n_workers)]
        self._results: asyncio.Queue = asyncio.Queue()
        self._total_submitted = 0

    async def submit(self, unit: WorkUnit):
        """Submit work to the least-loaded worker."""
        self._total_submitted += 1
        # Route to worker with shortest queue
        target = min(self._workers, key=lambda w: w.local_queue.qsize())
        await target.local_queue.put(unit)

    async def _worker_loop(self, worker: PooledWorkerActor, other_workers: list[PooledWorkerActor]):
        """Worker loop with work-stealing from other workers."""
        while True:
            try:
                unit = worker.local_queue.get_nowait()
                result = await worker.process(unit)
                await self._results.put(result)
                worker.local_queue.task_done()
            except asyncio.QueueEmpty:
                # Try to steal from the busiest other worker
                victims = [w for w in other_workers if w.local_queue.qsize() > 1]
                if victims:
                    busiest = max(victims, key=lambda w: w.local_queue.qsize())
                    try:
                        stolen_unit = busiest.local_queue.get_nowait()
                        busiest.local_queue.task_done()
                        worker._stolen += 1
                        result = await worker.process(stolen_unit)
                        result["stolen"] = True
                        await self._results.put(result)
                    except asyncio.QueueEmpty:
                        await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(0.05)

    async def run(self, work_units: list[WorkUnit]) -> list[dict]:
        """Submit all work and collect results."""
        # Start worker tasks
        worker_tasks = [
            asyncio.create_task(
                self._worker_loop(w, [x for x in self._workers if x is not w])
            )
            for w in self._workers
        ]

        # Submit all work
        for unit in work_units:
            await self.submit(unit)

        # Collect results
        collected = []
        while len(collected) < len(work_units):
            try:
                result = await asyncio.wait_for(self._results.get(), timeout=60)
                collected.append(result)
            except asyncio.TimeoutError:
                break

        for t in worker_tasks:
            t.cancel()

        return collected

async def demo_pool():
    pool = WorkStealingPool(n_workers=3)

    work_units = [
        WorkUnit(f"unit_{i:03d}", f"Give a one-sentence fact about topic #{i}")
        for i in range(9)
    ]

    print(f"Processing {len(work_units)} units with 3 workers (work-stealing)...\n")
    results = await pool.run(work_units)

    for r in sorted(results, key=lambda x: x["unit_id"]):
        stolen_str = " [STOLEN]" if r["stolen"] else ""
        print(f"  {r['unit_id']} → {r['worker']}{stolen_str}: {r['response'][:70]}")

    print("\nWorker stats:")
    for w in pool._workers:
        stats = w.get_stats()
        print(f"  {stats['worker']}: processed={stats['processed']}, stolen={stats['stolen']}")

asyncio.run(demo_pool())

# Expected Token Savings: Work stealing maximizes throughput with same worker count; no wasted idle capacity
# Environment: batch processing pipelines, high-volume inference, parallel document processing
```

---

## Option 6: Persistent Actor with SQLite State Snapshot

Actor state survives process restarts by snapshotting to SQLite on every state change.

```python
import anthropic
import asyncio
import sqlite3
import json
import time
from contextlib import contextmanager

client = anthropic.AsyncAnthropic()

STATE_DB = "actor_state.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(STATE_DB)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_state_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS actor_snapshots (
                actor_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                snapshot_at REAL NOT NULL,
                message_count INTEGER DEFAULT 0
            )
        """)

def save_actor_state(actor_id: str, state: dict, message_count: int):
    with get_db() as db:
        db.execute("""
            INSERT INTO actor_snapshots (actor_id, state_json, snapshot_at, message_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(actor_id) DO UPDATE SET
                state_json = excluded.state_json,
                snapshot_at = excluded.snapshot_at,
                message_count = excluded.message_count
        """, (actor_id, json.dumps(state), time.time(), message_count))

def load_actor_state(actor_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute(
            "SELECT state_json, message_count FROM actor_snapshots WHERE actor_id = ?",
            (actor_id,)
        ).fetchone()
        if row:
            return {"state": json.loads(row[0]), "message_count": row[1]}
    return None

class PersistentActor:
    """Actor that snapshots state to SQLite; survives restarts with full history."""

    def __init__(self, actor_id: str, initial_state: dict | None = None):
        self.actor_id = actor_id
        self.inbox: asyncio.Queue = asyncio.Queue()

        # Try to restore from snapshot
        saved = load_actor_state(actor_id)
        if saved:
            self._state = saved["state"]
            self._message_count = saved["message_count"]
            print(f"[{actor_id}] Restored: {self._message_count} msgs, "
                  f"{len(self._state.get('history', []))} history turns")
        else:
            self._state = initial_state or {}
            self._message_count = 0
            print(f"[{actor_id}] Fresh start")

    def _snapshot(self):
        """Persist current state."""
        save_actor_state(self.actor_id, self._state, self._message_count)

    async def run(self):
        while True:
            msg = await self.inbox.get()
            await self._handle(msg)
            self._message_count += 1
            self._snapshot()  # Persist after every message
            self.inbox.task_done()

    async def _handle(self, msg: dict):
        raise NotImplementedError

class PersistentConversationActor(PersistentActor):
    def __init__(self, actor_id: str):
        super().__init__(actor_id, initial_state={"history": [], "preferences": {}})

    async def _handle(self, msg: dict):
        msg_type = msg.get("type")
        reply_q = msg.get("reply_q")

        if msg_type == "chat":
            self._state["history"].append({"role": "user", "content": msg["text"]})
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=self._state["history"][-10:]  # Keep last 10 turns
            )
            answer = resp.content[0].text
            self._state["history"].append({"role": "assistant", "content": answer})

            result = {
                "actor": self.actor_id,
                "answer": answer,
                "history_length": len(self._state["history"]),
                "total_messages": self._message_count + 1
            }
            if reply_q:
                await reply_q.put(result)

        elif msg_type == "get_history":
            if reply_q:
                await reply_q.put({"history": self._state["history"], "actor": self.actor_id})

async def demo_persistence():
    init_state_db()
    actor = PersistentConversationActor("session_user_42")
    task = asyncio.create_task(actor.run())

    reply_q: asyncio.Queue = asyncio.Queue()

    # Simulate conversation
    for question in [
        "What is async/await in Python?",
        "Can you give a simple example?"
    ]:
        await actor.inbox.put({"type": "chat", "text": question, "reply_q": reply_q})
        result = await asyncio.wait_for(reply_q.get(), timeout=30)
        print(f"[{result['actor']}] (msg #{result['total_messages']}, {result['history_length']} turns)")
        print(f"  {result['answer'][:120]}\n")

    task.cancel()
    print("[Actor state persisted to SQLite — survives restart]")

    # Simulate restart: create new actor with same ID — state restores automatically
    print("\n[Simulating restart...]")
    restarted_actor = PersistentConversationActor("session_user_42")
    task2 = asyncio.create_task(restarted_actor.run())
    await restarted_actor.inbox.put({"type": "chat", "text": "What were we just talking about?", "reply_q": reply_q})
    result = await asyncio.wait_for(reply_q.get(), timeout=30)
    print(f"After restart (msg #{result['total_messages']}):")
    print(f"  {result['answer'][:150]}")
    task2.cancel()

asyncio.run(demo_persistence())

# Expected Token Savings: Persistent actor avoids re-sending full conversation history on restart; saves proportional tokens
# Environment: long-running user sessions, autonomous agents, checkpoint-critical workflows
```

---

## Comparison

| Option | Isolation Level | State Persistence | Communication | Best For |
|--------|----------------|------------------|---------------|----------|
| 1. Basic Asyncio Actor | Per-actor queue | In-memory | Direct queue | Simple multi-agent systems |
| 2. Registry Routing | Named actors | In-memory | Envelope routing | Microservices-style pipelines |
| 3. Supervised Tree | Per-actor + restart | In-memory | Parent-child | Fault-tolerant production systems |
| 4. State Machine | Per-actor + FSM | In-memory | Inbox messages | Workflows with strict state transitions |
| 5. Work Stealing Pool | Per-worker | In-memory | Pool coordinator | Batch processing, load balancing |
| 6. Persistent Actor | Per-actor + SQLite | SQLite | Direct queue | Long-running sessions, crash recovery |

**Recommended defaults:**
- **Multi-user sessions** → Option 6 (persistent) + Option 1 (basic)
- **Fault-tolerant pipelines** → Option 3 (supervised tree)
- **Batch workloads** → Option 5 (work-stealing pool)
- **Strict workflow** → Option 4 (state machine)
- **Simple orchestration** → Option 2 (registry routing)
