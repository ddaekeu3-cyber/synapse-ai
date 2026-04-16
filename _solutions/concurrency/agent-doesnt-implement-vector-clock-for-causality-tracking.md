---
title: "Agent Doesn't Implement Vector Clock for Causality Tracking"
description: "How to use vector clocks, version vectors, and hybrid logical clocks to track causal ordering of events across distributed agent instances — detecting concurrent updates, resolving conflicts, and enforcing causal consistency without a global clock."
date: 2025-01-16
difficulty: advanced
category: concurrency
slug: agent-doesnt-implement-vector-clock-for-causality-tracking
tags:
  - concurrency
  - vector-clock
  - causality
  - distributed-systems
  - causal-consistency
  - conflict-detection
  - event-ordering
symptoms:
  - "Two agent replicas update the same shared state concurrently with no way to detect the conflict"
  - "Cannot determine which of two events happened before the other across different agents"
  - "Distributed agents disagree on the order of tool call results"
  - "No way to tell if a received message is stale or was produced after the sender's last known state"
  - "Conflict resolution requires human intervention because causal relationships are unknown"
  - "Agent coordination breaks down when network partitions cause event reordering"
---

## Why This Happens

In a distributed multi-agent system, there is no global clock that all agents can use to reliably order events. System timestamps vary across machines and can jump backward. Two agents may update the same shared state at the same millisecond and have no way to determine which update should take precedence — or whether they are genuinely concurrent and need conflict resolution.

Vector clocks solve this by replacing wall-clock time with a logical clock that captures *causality*: if event A happened before event B (A caused B, or B was informed by A's result), then A's vector timestamp is strictly less than B's. If neither is less than the other, the events are concurrent and may conflict. This enables agents to make correct coordination decisions without a central coordinator or synchronized clocks.

---

## Solution 1: Basic Vector Clock

A vector clock assigns each agent a position in a vector. Each agent increments its own counter on every event. Messages carry the sender's vector; receivers merge it with their own.

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class VectorClock:
    """
    A vector clock mapping agent IDs to logical timestamps.
    Supports the three fundamental operations: tick, send, receive.
    """
    _clock: dict[str, int] = field(default_factory=dict)

    def __getitem__(self, agent_id: str) -> int:
        return self._clock.get(agent_id, 0)

    def __setitem__(self, agent_id: str, value: int) -> None:
        self._clock[agent_id] = value

    def tick(self, agent_id: str) -> "VectorClock":
        """Increment this agent's component before a local event."""
        new = self.copy()
        new[agent_id] = new[agent_id] + 1
        return new

    def send(self, agent_id: str) -> "VectorClock":
        """Increment and return clock to attach to an outgoing message."""
        return self.tick(agent_id)

    def receive(self, agent_id: str, remote: "VectorClock") -> "VectorClock":
        """Merge with a received clock, then tick local component."""
        merged = self.merge(remote)
        return merged.tick(agent_id)

    def merge(self, other: "VectorClock") -> "VectorClock":
        """Component-wise maximum of two clocks."""
        all_agents = set(self._clock) | set(other._clock)
        return VectorClock({a: max(self[a], other[a]) for a in all_agents})

    def copy(self) -> "VectorClock":
        return VectorClock(dict(self._clock))

    def to_dict(self) -> dict[str, int]:
        return dict(self._clock)

    @classmethod
    def from_dict(cls, d: dict[str, int]) -> "VectorClock":
        return cls(dict(d))

    # --- Comparison operators ---

    def __le__(self, other: "VectorClock") -> bool:
        """self happened-before-or-concurrent-with other (self ≤ other)."""
        all_agents = set(self._clock) | set(other._clock)
        return all(self[a] <= other[a] for a in all_agents)

    def __lt__(self, other: "VectorClock") -> bool:
        """self strictly happened-before other."""
        return self <= other and self != other

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VectorClock):
            return NotImplemented
        all_agents = set(self._clock) | set(other._clock)
        return all(self[a] == other[a] for a in all_agents)

    def is_concurrent_with(self, other: "VectorClock") -> bool:
        """Neither clock happened-before the other — genuine concurrent update."""
        return not (self <= other) and not (other <= self)

    def __repr__(self) -> str:
        return f"VC({dict(sorted(self._clock.items()))})"


# --- Usage ---

def demo_vector_clock():
    # Two agents updating shared state
    vc_a = VectorClock()
    vc_b = VectorClock()

    # Agent A performs two local events
    vc_a = vc_a.tick("agent-A")
    vc_a = vc_a.tick("agent-A")
    print(f"Agent A after 2 events: {vc_a}")  # VC({'agent-A': 2})

    # Agent B receives from A, then does local work
    vc_b = vc_b.receive("agent-B", vc_a)
    print(f"Agent B after receiving from A: {vc_b}")  # VC({'agent-A': 2, 'agent-B': 1})

    # Independent event on B — now B and A have concurrent clocks
    vc_a_new = vc_a.tick("agent-A")
    vc_b_new = vc_b.tick("agent-B")
    print(f"Concurrent: {vc_a_new.is_concurrent_with(vc_b_new)}")  # True
    print(f"A < B: {vc_a_new < vc_b_new}")  # False
```

---

## Solution 2: Version Vector for Conflict Detection in Shared State

A version vector tracks which version of each agent's writes is included in a shared object's state — enabling precise conflict detection on concurrent updates.

```python
from dataclasses import dataclass
import copy
import time

@dataclass
class VersionedObject:
    """A shared object annotated with a version vector for conflict detection."""
    key: str
    value: object
    version_vector: VectorClock
    last_writer: str
    updated_at: float

    def conflicts_with(self, other: "VersionedObject") -> bool:
        """Returns True if this and other are concurrent updates (neither dominates)."""
        return self.version_vector.is_concurrent_with(other.version_vector)

    def dominates(self, other: "VersionedObject") -> bool:
        """Returns True if this update happened-after (or equal-to) other."""
        return other.version_vector <= self.version_vector


class VersionVectorStore:
    """
    Key-value store with version vector tracking for distributed conflict detection.
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._store: dict[str, VersionedObject] = {}
        self._clock = VectorClock()

    def write(self, key: str, value: object) -> VersionedObject:
        """Write a value, advancing the agent's clock."""
        self._clock = self._clock.tick(self.agent_id)
        obj = VersionedObject(
            key=key,
            value=value,
            version_vector=self._clock.copy(),
            last_writer=self.agent_id,
            updated_at=time.time(),
        )
        self._store[key] = obj
        return obj

    def read(self, key: str) -> Optional[VersionedObject]:
        return self._store.get(key)

    def merge_remote(self, remote_obj: VersionedObject) -> tuple[VersionedObject, str]:
        """
        Attempt to merge a remote update.
        Returns (resolved_object, resolution_strategy).
        """
        local = self._store.get(remote_obj.key)

        if local is None:
            # No local version — accept remote
            self._store[remote_obj.key] = remote_obj
            self._clock = self._clock.merge(remote_obj.version_vector)
            return remote_obj, "accepted_new"

        if remote_obj.dominates(local):
            # Remote is strictly newer — update
            self._store[remote_obj.key] = remote_obj
            self._clock = self._clock.merge(remote_obj.version_vector)
            return remote_obj, "accepted_newer"

        if local.dominates(remote_obj):
            # Local is strictly newer — discard remote
            return local, "kept_local"

        # Concurrent update — conflict!
        resolved = self._resolve_conflict(local, remote_obj)
        self._store[remote_obj.key] = resolved
        self._clock = self._clock.merge(remote_obj.version_vector)
        return resolved, "conflict_resolved"

    def _resolve_conflict(
        self, local: VersionedObject, remote: VersionedObject
    ) -> VersionedObject:
        """
        Default: last-write-wins by wall clock.
        Override with domain-specific merge (e.g., CRDT, user preference).
        """
        winner = local if local.updated_at >= remote.updated_at else remote
        merged_vv = local.version_vector.merge(remote.version_vector)
        return VersionedObject(
            key=winner.key,
            value=winner.value,
            version_vector=merged_vv,
            last_writer=winner.last_writer,
            updated_at=max(local.updated_at, remote.updated_at),
        )
```

---

## Solution 3: Causal Message Ordering

Buffer incoming messages until their causal dependencies have been delivered, ensuring agents process events in causal order.

```python
import asyncio
from dataclasses import dataclass, field

@dataclass
class CausalMessage:
    sender: str
    payload: object
    sender_clock: VectorClock     # Sender's clock at send time
    causal_deps: VectorClock      # Minimum clock needed before delivering this message

class CausalOrderingBuffer:
    """
    Buffers incoming messages and delivers them only after all causally
    preceding messages have been delivered.

    Invariant: message M is deliverable when local_clock >= M.causal_deps
    for all entries except the sender's.
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._clock = VectorClock()
        self._buffer: list[CausalMessage] = []
        self._delivered: list[CausalMessage] = []
        self._handlers: list[callable] = []

    def on_deliver(self, handler: callable) -> None:
        self._handlers.append(handler)

    async def receive(self, msg: CausalMessage) -> None:
        """Buffer the message and try to deliver any ready messages."""
        self._buffer.append(msg)
        await self._try_deliver()

    async def _try_deliver(self) -> None:
        """Deliver all causally ready messages in order."""
        delivered_any = True
        while delivered_any:
            delivered_any = False
            for msg in list(self._buffer):
                if self._is_deliverable(msg):
                    self._buffer.remove(msg)
                    await self._deliver(msg)
                    delivered_any = True

    def _is_deliverable(self, msg: CausalMessage) -> bool:
        """
        A message from sender S with clock C is deliverable when:
        1. local_clock[S] == C[S] - 1 (exactly the next expected from S)
        2. local_clock[A] >= C[A] for all A != S (all causal deps satisfied)
        """
        sender = msg.sender
        expected = self._clock[sender] + 1
        if msg.sender_clock[sender] != expected:
            return False

        all_agents = set(msg.sender_clock._clock) | set(self._clock._clock)
        for agent in all_agents:
            if agent == sender:
                continue
            if self._clock[agent] < msg.sender_clock[agent]:
                return False
        return True

    async def _deliver(self, msg: CausalMessage) -> None:
        self._clock = self._clock.receive(self.agent_id, msg.sender_clock)
        self._delivered.append(msg)
        for handler in self._handlers:
            await handler(msg)

    def local_send(self, payload: object) -> CausalMessage:
        """Create a message to send with the current causal context."""
        self._clock = self._clock.tick(self.agent_id)
        return CausalMessage(
            sender=self.agent_id,
            payload=payload,
            sender_clock=self._clock.copy(),
            causal_deps=self._clock.copy(),
        )
```

---

## Solution 4: Hybrid Logical Clock (HLC)

Hybrid Logical Clocks combine wall-clock time with a logical component — providing causality tracking while keeping timestamps close to real time for human readability.

```python
import time
from dataclasses import dataclass

@dataclass(order=True)
class HLCTimestamp:
    """
    Hybrid Logical Clock timestamp: (wall_time_ms, logical_counter).
    Comparable and totally ordered; causally correct.
    """
    wall_ms: int   # Wall-clock milliseconds
    counter: int   # Logical counter for same-millisecond disambiguation
    node_id: str = ""  # Tiebreaker for concurrent events on different nodes

    def __lt__(self, other: "HLCTimestamp") -> bool:
        if self.wall_ms != other.wall_ms:
            return self.wall_ms < other.wall_ms
        if self.counter != other.counter:
            return self.counter < other.counter
        return self.node_id < other.node_id

    def __le__(self, other: "HLCTimestamp") -> bool:
        return self == other or self < other

    def __repr__(self) -> str:
        return f"HLC({self.wall_ms}, {self.counter}, {self.node_id})"


class HybridLogicalClock:
    """
    HLC implementation per Kulkarni et al. 2014.
    Provides causality + approximate real-time ordering.
    """

    def __init__(self, node_id: str, max_drift_ms: int = 60_000):
        self.node_id = node_id
        self.max_drift_ms = max_drift_ms
        self._last = HLCTimestamp(0, 0, node_id)

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def tick(self) -> HLCTimestamp:
        """Advance clock for a local event."""
        pt = self._now_ms()
        if pt > self._last.wall_ms:
            self._last = HLCTimestamp(pt, 0, self.node_id)
        else:
            self._last = HLCTimestamp(self._last.wall_ms, self._last.counter + 1, self.node_id)
        return HLCTimestamp(self._last.wall_ms, self._last.counter, self.node_id)

    def receive(self, remote: HLCTimestamp) -> HLCTimestamp:
        """Advance clock on receiving a remote event."""
        pt = self._now_ms()
        drift = remote.wall_ms - pt
        if drift > self.max_drift_ms:
            raise ValueError(f"Remote clock drift {drift}ms exceeds maximum {self.max_drift_ms}ms")

        max_wall = max(pt, self._last.wall_ms, remote.wall_ms)
        if max_wall == self._last.wall_ms == remote.wall_ms:
            counter = max(self._last.counter, remote.counter) + 1
        elif max_wall == self._last.wall_ms:
            counter = self._last.counter + 1
        elif max_wall == remote.wall_ms:
            counter = remote.counter + 1
        else:
            counter = 0

        self._last = HLCTimestamp(max_wall, counter, self.node_id)
        return HLCTimestamp(max_wall, counter, self.node_id)

    def now(self) -> HLCTimestamp:
        return self.tick()


# --- Usage: causally ordered agent events with real-time timestamps ---

def demo_hlc():
    agent_a = HybridLogicalClock("agent-A")
    agent_b = HybridLogicalClock("agent-B")

    t1 = agent_a.tick()
    t2 = agent_b.tick()
    print(f"A's event: {t1}")
    print(f"B's event: {t2}")

    # B receives A's event — B's clock is now causally after A's
    t3 = agent_b.receive(t1)
    print(f"B after receiving A: {t3}")
    print(f"t1 < t3: {t1 < t3}")  # True — causal ordering preserved
```

---

## Solution 5: Causal Context Propagation Across Agent Calls

Propagate causal context through agent-to-agent calls so that downstream agents know the causal history of a request.

```python
import contextvars
from dataclasses import dataclass, field
import json

_causal_ctx: contextvars.ContextVar["CausalContext"] = contextvars.ContextVar("_causal_ctx")

@dataclass
class CausalContext:
    agent_id: str
    clock: VectorClock
    trace_id: str = ""
    parent_event_id: str = ""
    metadata: dict = field(default_factory=dict)

    def to_headers(self) -> dict[str, str]:
        return {
            "X-Causal-Clock":  json.dumps(self.clock.to_dict()),
            "X-Causal-Agent":  self.agent_id,
            "X-Trace-ID":      self.trace_id,
            "X-Parent-Event":  self.parent_event_id,
        }

    @classmethod
    def from_headers(cls, headers: dict[str, str], receiving_agent: str) -> "CausalContext":
        clock_data = json.loads(headers.get("X-Causal-Clock", "{}"))
        return cls(
            agent_id=receiving_agent,
            clock=VectorClock.from_dict(clock_data),
            trace_id=headers.get("X-Trace-ID", ""),
            parent_event_id=headers.get("X-Parent-Event", ""),
        )


def get_causal_context() -> Optional[CausalContext]:
    return _causal_ctx.get(None)

def set_causal_context(ctx: CausalContext) -> contextvars.Token:
    return _causal_ctx.set(ctx)


class CausalAgent:
    """Agent that propagates causal context through all inter-agent calls."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._clock = VectorClock()

    def _tick(self) -> VectorClock:
        self._clock = self._clock.tick(self.agent_id)
        return self._clock.copy()

    def _receive(self, remote_clock: VectorClock) -> VectorClock:
        self._clock = self._clock.receive(self.agent_id, remote_clock)
        return self._clock.copy()

    def build_outgoing_context(self) -> CausalContext:
        clock = self._tick()
        ctx = get_causal_context()
        return CausalContext(
            agent_id=self.agent_id,
            clock=clock,
            trace_id=ctx.trace_id if ctx else "",
        )

    def accept_incoming_context(self, remote_ctx: CausalContext) -> None:
        self._receive(remote_ctx.clock)
        new_ctx = CausalContext(
            agent_id=self.agent_id,
            clock=self._clock.copy(),
            trace_id=remote_ctx.trace_id,
            parent_event_id=remote_ctx.agent_id,
        )
        set_causal_context(new_ctx)

    def causally_precedes(self, event_clock: VectorClock) -> bool:
        """Returns True if this agent's current clock happened-before event_clock."""
        return self._clock < event_clock
```

---

## Solution 6: Conflict-Free Replicated Counter (G-Counter CRDT)

Build a simple CRDT counter using vector-clock semantics — a grow-only counter that merges correctly across distributed agents.

```python
from dataclasses import dataclass
import copy

@dataclass
class GCounter:
    """
    Grow-only counter CRDT.
    Each agent only increments its own slot; merge takes the max of each slot.
    The total is the sum of all slots.
    """
    _counts: dict[str, int] = field(default_factory=dict)

    def increment(self, agent_id: str, amount: int = 1) -> None:
        self._counts[agent_id] = self._counts.get(agent_id, 0) + amount

    @property
    def value(self) -> int:
        return sum(self._counts.values())

    def merge(self, other: "GCounter") -> "GCounter":
        """Merge two G-Counters — component-wise max."""
        all_agents = set(self._counts) | set(other._counts)
        return GCounter({a: max(self._counts.get(a, 0), other._counts.get(a, 0)) for a in all_agents})

    def __ge__(self, other: "GCounter") -> bool:
        """self dominates other (has seen all of other's increments)."""
        return all(self._counts.get(a, 0) >= other._counts.get(a, 0) for a in other._counts)

    def to_dict(self) -> dict:
        return dict(self._counts)

    @classmethod
    def from_dict(cls, d: dict) -> "GCounter":
        return cls(dict(d))


@dataclass
class PNCounter:
    """
    Positive-Negative counter CRDT.
    Supports both increment and decrement using two G-Counters.
    """
    _increments: GCounter = field(default_factory=GCounter)
    _decrements: GCounter = field(default_factory=GCounter)

    def increment(self, agent_id: str, amount: int = 1) -> None:
        self._increments.increment(agent_id, amount)

    def decrement(self, agent_id: str, amount: int = 1) -> None:
        self._decrements.increment(agent_id, amount)

    @property
    def value(self) -> int:
        return self._increments.value - self._decrements.value

    def merge(self, other: "PNCounter") -> "PNCounter":
        return PNCounter(
            _increments=self._increments.merge(other._increments),
            _decrements=self._decrements.merge(other._decrements),
        )


# --- Usage: distributed token usage tracking across agents ---

def demo_crdt_counter():
    # Each agent tracks its own token usage
    agent_a_counter = PNCounter()
    agent_b_counter = PNCounter()

    agent_a_counter.increment("agent-A", 1500)  # 1500 tokens
    agent_b_counter.increment("agent-B", 2200)  # 2200 tokens

    # Merge gives correct total — no coordination needed
    merged = agent_a_counter.merge(agent_b_counter)
    print(f"Total tokens used: {merged.value}")  # 3700

    # Merging is commutative and idempotent
    merged2 = agent_b_counter.merge(agent_a_counter)
    print(f"Same result: {merged.value == merged2.value}")  # True
```

---

## Comparison

| Solution | Clock Type | Conflict Detection | Total Order | Distributed | Best For |
|---|---|---|---|---|---|
| Basic Vector Clock | Logical | Yes (concurrent detection) | No | Yes | Causal ordering of events |
| Version Vector Store | Per-object | Yes (per-key) | No | Yes | Conflict detection on shared objects |
| Causal Ordering Buffer | Logical | No (ordering only) | No | Yes | In-order message delivery |
| Hybrid Logical Clock | Logical + Wall | Partial | Yes (approximate) | Yes | Real-time + causal ordering |
| Causal Context Propagation | Logical | No (tracking only) | No | Yes | Tracing causal chains |
| G-Counter / PN-Counter CRDT | Logical | No (conflict-free) | No | Yes | Distributed counters without conflicts |

**Use vector clocks** when you need to know whether two events are causally related or concurrent. **Use the causal ordering buffer** when message delivery order must respect causality. **Use HLC** when you need both causality and human-readable timestamps close to wall time. **Use CRDTs** (G-Counter, PN-Counter) when the data structure can be designed to be conflict-free — they eliminate the need for conflict resolution entirely. **Propagate causal context** through all inter-agent calls to maintain a consistent causal view across the system.
